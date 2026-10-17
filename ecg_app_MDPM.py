"""
12-Lead Smart Holter Backend & Multi-Device Monitoring Platform (MDPM)
======================================================================
Author: CTAI Laboratory
Technologies: Python, Flask, Flask-SocketIO, eventlet, Paho-MQTT, Google Cloud BigQuery,
              NumPy, SciPy, PyWavelets, Joblib, NeuroKit2, Scikit-learn, XGBoost.

This module acts as the centralized cloud ingestion server and real-time dashboard
for continuous 12-lead ECG telemetry streamed from ESP32 edge devices. It receives
compact binary telemetry frames (63-byte metrics or 51-byte legacy) via MQTT, decodes
24-bit multi-lead signals, measures transmission QoS (latency, jitter, packet loss),
buffers data asynchronously for Google BigQuery ingestion, and executes a clinical-grade
DSP and machine learning classification pipeline upon recording triggers.
"""

import eventlet
eventlet.monkey_patch()

import os
import json
import time
import uuid
import traceback
import math
from datetime import datetime, timezone
from collections import deque
import struct

from flask import Flask, jsonify, render_template_string, request
from flask_socketio import SocketIO, join_room

import paho.mqtt.client as mqtt
from google.cloud import bigquery
from google.api_core.exceptions import NotFound

import numpy as np
import pandas as pd
import scipy.signal
import pywt
import joblib
import neurokit2 as nk

# ==================================================================
# Application Configuration & Environment Settings
# ==================================================================
MQTT_BROKER = os.getenv("MQTT_BROKER", "broker.hivemq.com")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "ecg/+/data")

GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "ecg12lead-project")
DATASET_ID = os.getenv("DATASET_ID", "ecg_data")
RAW_TABLE_ID = os.getenv("RAW_TABLE_ID", "ecg_raw_per_sample")
CLS_TABLE_ID = os.getenv("CLS_TABLE_ID", "ecg_classifications")
PERF_TABLE_ID = os.getenv("PERF_TABLE_ID", "ecg_performance_metrics") 
RAW_TABLE_REF = f"{GCP_PROJECT_ID}.{DATASET_ID}.{RAW_TABLE_ID}"
CLS_TABLE_REF = f"{GCP_PROJECT_ID}.{DATASET_ID}.{CLS_TABLE_ID}"
PERF_TABLE_REF = f"{GCP_PROJECT_ID}.{DATASET_ID}.{PERF_TABLE_ID}" 

MODEL_PATH = os.getenv("MODEL_PATH", "/home/jeffryfne/ecg_models")
FLASK_PORT = int(os.getenv("FLASK_PORT", "5000"))

SPS = 853
BUFFER_SIZE = 8000
DEVICE_TIMEOUT_S = 15  # Seconds of silence before device is considered offline

# ==================================================================
# Application Initialization
# ==================================================================
app = Flask(__name__)
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins="*")

bq = bigquery.Client(project=GCP_PROJECT_ID)

device_states = {}
live_buffer = deque(maxlen=2000)
perf_buffer = deque(maxlen=2000) 

# ==================================================================
# Template HTML (MODIFIED for UI Bug Fix)
# ==================================================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8"/>
    <title>ECG Live Platform (Multi-Device)</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, 'Open Sans', 'Helvetica Neue', sans-serif; margin: 0; background-color: #0d1117; color: #c9d1d9; font-size: 14px; }
        .container { max-width: 1200px; margin: 0 auto; padding: 20px; }
        header { padding-bottom: 10px; border-bottom: 1px solid #30363d; margin-bottom: 20px; }
        h1, h2, h3 { color: #58a6ff; font-weight: 400; }
        .row { display: flex; gap: 16px; flex-wrap: wrap; align-items: center; margin-bottom: 16px; }
        .pill { background-color: #161b22; border: 1px solid #30363d; border-radius: 999px; padding: 6px 12px; font-size: 12px; color: #8b949e; }
        .pill .value { color: #c9d1d9; font-weight: 500; }
        .ok { color: #3fb950; }
        .bad { color: #f85149; }
        .panel { background-color: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 20px; margin-bottom: 20px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 16px; }
        .card { background-color: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 12px; }
        .card .label { font-size: 12px; color: #8b949e; margin-bottom: 4px; }
        .card .value { font-size: 20px; font-weight: 600; color: #c9d1d9; }
        canvas { background-color: #0d1117; border: 1px solid #30363d; border-radius: 6px; }
        input[type="text"], select { background-color: #0d1117; border: 1px solid #30363d; color: #c9d1d9; padding: 8px 12px; border-radius: 6px; font-size: 14px; }
        input[type="text"]:focus, select:focus { outline: none; border-color: #58a6ff; }
        button { cursor: pointer; padding: 8px 16px; border-radius: 6px; border: 1px solid #30363d; font-size: 14px; font-weight: 500; transition: background-color 0.2s ease; }
        #start { background-color: #238636; color: white; }
        #start:hover { background-color: #2ea043; }
        #stop { background-color: #da3633; color: white; display: none; }
        #stop:hover { background-color: #f85149; }
        table { width: 100%; border-collapse: collapse; margin-top: 16px; }
        th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid #30363d; }
        th { font-size: 12px; color: #8b949e; font-weight: 500; }
        td { font-family: 'Courier New', Courier, monospace; }
        .history-panel { max-height: 400px; overflow-y: auto; }
        .progress-container { width: 100%; background-color: #30363d; border-radius: 4px; overflow: hidden; height: 12px; margin-top: 10px; display: none; }
        .progress-bar { width: 0%; height: 100%; background-color: #2ea043; transition: width 0.1s linear; }
        .progress-label { text-align: center; font-size: 12px; color: #8b949e; margin-top: 4px; }
        .tabs { border-bottom: 1px solid #30363d; margin-bottom: 20px; }
        .tab-button { background-color: transparent; border: none; color: #8b949e; padding: 10px 15px; cursor: pointer; border-bottom: 2px solid transparent; margin-bottom: -1px; }
        .tab-button.active { color: #58a6ff; border-bottom-color: #58a6ff; font-weight: 600; }
        .tab-content { display: none; }
    </style>
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
</head>
<body>
<div class="container">
    <header><h1>ECG Live Platform (Multi-Device)</h1></header>
    <div class="row">
        <div class="pill">Broker: <span class="value">{{broker}}:{{port}}</span></div>
        <div class="pill">Topic: <span class="value">{{topic}}</span></div>
        <div id="sock" class="pill">Socket: Connecting...</div>
    </div>
    <div class="panel">
        <h3>Select Device (for Detailed View)</h3>
        <div class="row">
            <select id="device-selector" style="min-width: 250px;"></select>
            <div id="selected-device" class="pill">Selected Device: <span class="value">-</span></div>
        </div>
    </div>
    <div class="tabs">
        <button class="tab-button active" onclick="openTab(event, 'ecg-view')">12-Lead View</button>
        <button class="tab-button" onclick="openTab(event, 'perf-view')">Performance Metrics (Detail)</button>
        <button class="tab-button" onclick="openTab(event, 'overview-view')">Multi-Device Overview</button>
    </div>
    <div id="ecg-view" class="tab-content" style="display: block;">
        <div class="panel">
            <h3>Recording Control</h3>
            <div class="row">
                <input id="subject" type="text" placeholder="Subject ID (Required)" size="30"/>
                <button id="start">Start Recording</button>
                <button id="stop">Stop Recording</button>
            </div>
            <div class="row">
                <div class="pill">Status: <span id="status" class="value">Idle</span></div>
                <div class="pill">Recording ID: <span id="rid" class="value">-</span></div>
                <div class="pill">Subject: <span id="sid" class="value">-</span></div>
            </div>
            <div id="progress-container" class="progress-container">
                <div id="progress-bar" class="progress-bar"></div>
            </div>
            <div id="progress-label" class="progress-label"></div>
        </div>
        <div class="panel">
            <h3>Live Waveform (Lead II)</h3>
            <canvas id="chart" height="120"></canvas>
        </div>
        <div class="panel">
            <h3>Live Values (ADC)</h3>
            <div class="grid" id="cards"></div>
        </div>
        <div class="panel history-panel">
            <h3>Classification History</h3>
            <table id="history-table">
                <thead>
                    <tr>
                        <th>Timestamp (UTC)</th>
                        <th>Device ID</th>
                        <th>Subject ID</th>
                        <th>Recording ID</th>
                        <th>Classification Result</th>
                    </tr>
                </thead>
                <tbody></tbody>
            </table>
        </div>
    </div>
    <div id="perf-view" class="tab-content">
        <div class="panel">
            <h3>Live Performance Metrics (Detail)</h3>
             <div class="pill" style="margin-bottom: 16px;">Packet Format: <span id="packet-format" class="value">Unknown</span></div>
            <div class="grid" id="perf-cards-container"></div>
        </div>
    </div>
    <div id="overview-view" class="tab-content">
        <div class="panel">
            <h3>Global Performance Overview</h3>
            <table id="overview-table">
                <thead>
                    <tr>
                        <th>Device ID</th>
                        <th>Status</th>
                        <th>Packet Format</th>
                        <th>Avg Latency (ms)</th>
                        <th>Jitter (ms)</th>
                        <th>Packet Loss (%)</th>
                    </tr>
                </thead>
                <tbody id="overview-table-body"></tbody>
            </table>
        </div>
    </div>
</div>
<script>
    function openTab(evt, tabName) {
        document.querySelectorAll('.tab-content').forEach(tab => tab.style.display = 'none');
        document.querySelectorAll('.tab-button').forEach(btn => btn.classList.remove('active'));
        document.getElementById(tabName).style.display = 'block';
        evt.currentTarget.classList.add('active');
    }
    document.addEventListener('DOMContentLoaded', () => {
        const sockEl = document.getElementById('sock');
        const startBtn = document.getElementById('start'), stopBtn = document.getElementById('stop');
        const subjectInput = document.getElementById('subject');
        const statusEl = document.getElementById('status'), ridEl = document.getElementById('rid'), sidEl = document.getElementById('sid');
        const cardsContainer = document.getElementById('cards');
        const historyTableBody = document.querySelector("#history-table tbody");
        const progressContainer = document.getElementById('progress-container');
        const progressBar = document.getElementById('progress-bar');
        const progressLabel = document.getElementById('progress-label');
        const deviceSelector = document.getElementById('device-selector');
        const selectedDeviceEl = document.querySelector('#selected-device .value');
        let currentDevice = null;
        const LEAD_KEYS = ["lead_I", "lead_II", "lead_III", "avr", "avl", "avf", "v1", "v2", "v3", "v4", "v5", "v6"];
        const valueElements = {};
        LEAD_KEYS.forEach(k => {
            let card = document.createElement('div');
            card.className = 'card';
            card.innerHTML = `<div class='label'>${k.replace('_', ' ')}</div><div class='value' id='v-${k}'>-</div>`;
            cardsContainer.appendChild(card);
            valueElements[k] = card.querySelector('.value');
        });
        const perfCardsContainer = document.getElementById('perf-cards-container');
        const packetFormatEl = document.getElementById('packet-format');
        const PERF_METRICS = {
            latency_ms: { label: 'Latency', unit: 'ms' },
            avg_latency_ms: { label: 'Avg Latency (100pkts)', unit: 'ms' },
            jitter_ms: { label: 'Jitter', unit: 'ms' },
            packet_loss_pct: { label: 'Packet Loss', unit: '%' },
            lost_packets: { label: 'Lost Packets', unit: '' },
            total_received: { label: 'Received Packets', unit: '' },
        };
        const perfValueElements = {};
        Object.entries(PERF_METRICS).forEach(([key, value]) => {
            let card = document.createElement('div');
            card.className = 'card';
            card.innerHTML = `<div class='label'>${value.label}</div><div class='value' id='perf-${key}'>-</div>`;
            perfCardsContainer.appendChild(card);
            perfValueElements[key] = card.querySelector('.value');
        });
        const overviewTableBody = document.getElementById('overview-table-body');
        const chartCtx = document.getElementById('chart').getContext('2d');
        const chartData = [], chartLabels = [];
        const MAX_CHART_POINTS = 500;
        const ecgChart = new Chart(chartCtx, {
            type: 'line', data: { labels: chartLabels, datasets: [{ label: 'Lead II', data: chartData, borderColor: '#58a6ff', borderWidth: 1.5, pointRadius: 0, tension: 0.1 }] },
            options: { animation: false, scales: { x: { display: false }, y: { ticks: { color: '#8b949e' }, grid: { color: '#30363d' } } }, plugins: { legend: { display: false } } }
        });

        // --- START: UI OPTIMIZATION ---
        const liveDataBuffer = [];
        let isRendering = false;

        function renderLoop() {
            if (liveDataBuffer.length === 0) {
                isRendering = false;
                return;
            }
            let lastPkt = null;
            while(liveDataBuffer.length > 0) {
                const pkt = liveDataBuffer.shift();
                lastPkt = pkt;
                const leadIIValue = pkt['lead_II'];
                if (leadIIValue !== null && leadIIValue !== undefined && isFinite(leadIIValue)) {
                    chartData.push(Number(leadIIValue));
                    chartLabels.push('');
                    if (chartData.length > MAX_CHART_POINTS) {
                        chartData.shift();
                        chartLabels.shift();
                    }
                }
            }
            if (lastPkt) {
                LEAD_KEYS.forEach(k => {
                    let v = lastPkt[k];
                    if (v !== null && v !== undefined && isFinite(v)) {
                        valueElements[k].textContent = v;
                    }
                });
            }
            ecgChart.update();
            requestAnimationFrame(renderLoop);
        }
        // --- END: UI OPTIMIZATION ---

        const socket = io({ transports: ['websocket'] });
        socket.on('connect', () => { sockEl.textContent = 'Socket: Connected'; sockEl.classList.add('ok'); });
        socket.on('disconnect', () => { sockEl.textContent = 'Socket: Disconnected'; sockEl.classList.add('bad'); sockEl.classList.remove('ok'); });
        
        socket.on('device_list_update', (devices) => {
            console.log('Available devices:', devices);
            const previouslySelected = deviceSelector.value;
            deviceSelector.innerHTML = '<option value="">-- Select Device --</option>';
            devices.sort().forEach(devId => {
                const option = document.createElement('option');
                option.value = devId;
                option.textContent = devId;
                deviceSelector.appendChild(option);
            });
            if (devices.includes(previouslySelected)) {
                deviceSelector.value = previouslySelected;
            } else {
                if (previouslySelected === currentDevice) {
                    currentDevice = null;
                    selectedDeviceEl.textContent = '-';
                    // Clear detailed view if selected device is removed
                    chartData.length = 0; chartLabels.length = 0; ecgChart.update();
                    Object.values(valueElements).forEach(el => el.textContent = '-');
                    Object.values(perfValueElements).forEach(el => el.textContent = '-');
                    packetFormatEl.textContent = 'Unknown';
                    statusEl.textContent = "Idle";
                    ridEl.textContent = "-";
                    sidEl.textContent = "-";
                }
            }
        });

        deviceSelector.onchange = () => {
            const selectedDevId = deviceSelector.value;

            // Clear buffers and relevant UI elements
            liveDataBuffer.length = 0;
            chartData.length = 0;
            chartLabels.length = 0;
            ecgChart.update();
            Object.values(valueElements).forEach(el => el.textContent = '-');
            Object.values(perfValueElements).forEach(el => el.textContent = '-');
            packetFormatEl.textContent = 'Unknown';
            statusEl.textContent = "Idle";
            ridEl.textContent = "-";
            sidEl.textContent = "-";

            if (selectedDevId) {
                currentDevice = selectedDevId;
                selectedDeviceEl.textContent = currentDevice;
                socket.emit('subscribe_to_device', { device_id: currentDevice });
                console.log(`Subscribed to ${currentDevice}`);
            } else {
                currentDevice = null;
                selectedDeviceEl.textContent = '-';
            }
        };

        startBtn.onclick = () => {
            const subjectId = subjectInput.value.trim();
            if (!currentDevice) { alert('Please select a device first.'); return; }
            if (!subjectId) { alert('Please enter a Subject ID.'); return; }
            socket.emit('start_recording', { device_id: currentDevice, subject_id: subjectId });
        };

        stopBtn.onclick = () => {
            if (!currentDevice) return;
            socket.emit('stop_recording', { device_id: currentDevice });
        };
        
        socket.on('state_update', data => {
            if (data.device_id !== currentDevice) return;
            const isRecording = data.is_recording;
            startBtn.style.display = isRecording ? 'none' : 'inline-block';
            stopBtn.style.display = isRecording ? 'inline-block' : 'none';
            subjectInput.disabled = isRecording;
            statusEl.textContent = data.status_message;
            ridEl.textContent = data.recording_id || '-';
            sidEl.textContent = data.subject_id || '-';
            if (isRecording) {
                progressContainer.style.display = 'block';
            } else {
                progressContainer.style.display = 'none';
                progressLabel.textContent = '';
            }
        });
        
        socket.on('live_data', pkt => {
            if (pkt.device_id !== currentDevice) return;
            liveDataBuffer.push(pkt);
            if (!isRendering) {
                isRendering = true;
                requestAnimationFrame(renderLoop);
            }
        });

        socket.on('performance_update', data => {
            if (data.device_id !== currentDevice) return;
            packetFormatEl.textContent = data.packet_format || 'Legacy (51-byte)';
            Object.entries(data).forEach(([key, value]) => {
                const el = perfValueElements[key];
                if(el) {
                    const unit = PERF_METRICS[key]?.unit || '';
                    el.textContent = `${value} ${unit}`;
                }
            });
        });

        socket.on('global_performance_update', (data) => {
            const activeDeviceIds = data.map(d => d.device_id);
            overviewTableBody.innerHTML = '';
            data.forEach(device => {
                const row = document.createElement('tr');
                row.innerHTML = `
                    <td>${device.device_id}</td>
                    <td>${device.status_message}</td>
                    <td>${device.packet_format}</td>
                    <td>${device.avg_latency_ms.toFixed(2)}</td>
                    <td>${device.jitter_ms.toFixed(2)}</td>
                    <td>${device.packet_loss_pct.toFixed(2)}</td>
                `;
                overviewTableBody.appendChild(row);
            });
        });
        
        socket.on('progress_update', data => {
            if (data.device_id !== currentDevice) return;
            const percent = (data.current / data.total) * 100;
            progressBar.style.width = percent + '%';
            progressLabel.textContent = `${data.current} / ${data.total} samples`;
        });
        
        function fetchHistory() {
            fetch('/api/history')
                .then(response => response.json())
                .then(data => {
                    if (data.error) { console.error("Failed to fetch history:", data.error); return; }
                    historyTableBody.innerHTML = '';
                    data.forEach(rec => {
                        const row = document.createElement('tr');
                        row.innerHTML = `<td>${new Date(rec.timestamp).toLocaleString()}</td><td>${rec.device_id}</td><td>${rec.subject_id}</td><td>${rec.recording_id}</td><td>${rec.classification}</td>`;
                        historyTableBody.insertBefore(row, historyTableBody.firstChild);
                    });
                })
                .catch(error => console.error('Error:', error));
        }

        socket.on('history_updated', () => {
            console.log('Received history_updated event, reloading history...');
            fetchHistory();
        });
        
        fetchHistory();
    });
</script>
</body>
</html>
"""

# ==================================================================
# BigQuery Initialization & Helper Functions
# ==================================================================
def ensure_bq_tables():
    """Verifies and initializes required BigQuery datasets and tables.
    
    Checks for the existence of:
    - Dataset: GCP_PROJECT_ID.DATASET_ID
    - Table: ecg_raw_per_sample (stores raw 12-lead ADC records)
    - Table: ecg_classifications (stores ML arrhythmia prediction outcomes)
    - Table: ecg_performance_metrics (stores packet QoS and telemetry statistics)
    
    Creates any missing datasets or tables with their predefined schema.
    """
    print("Verifying BigQuery tables...")
    dataset = bigquery.Dataset(f"{GCP_PROJECT_ID}.{DATASET_ID}")
    dataset.location = "US"
    try:
        bq.get_dataset(dataset.dataset_id)
    except NotFound:
        bq.create_dataset(dataset)
        
    try:
        bq.get_table(RAW_TABLE_REF)
    except NotFound:
        schema = [
            bigquery.SchemaField("timestamp", "TIMESTAMP", mode="REQUIRED"),
            bigquery.SchemaField("device_id", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("recording_id", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("subject_id", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("lead_I", "INTEGER"), bigquery.SchemaField("lead_II", "INTEGER"),
            bigquery.SchemaField("lead_III", "INTEGER"), bigquery.SchemaField("avr", "INTEGER"),
            bigquery.SchemaField("avl", "INTEGER"), bigquery.SchemaField("avf", "INTEGER"),
            bigquery.SchemaField("v1", "INTEGER"), bigquery.SchemaField("v2", "INTEGER"),
            bigquery.SchemaField("v3", "INTEGER"), bigquery.SchemaField("v4", "INTEGER"),
            bigquery.SchemaField("v5", "INTEGER"), bigquery.SchemaField("v6", "INTEGER"),
        ]
        bq.create_table(bigquery.Table(RAW_TABLE_REF, schema=schema))
        print(f"Table {RAW_TABLE_ID} created successfully.")
        
    try:
        bq.get_table(CLS_TABLE_REF)
    except NotFound:
        schema = [
            bigquery.SchemaField("timestamp", "TIMESTAMP", mode="REQUIRED"),
            bigquery.SchemaField("device_id", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("recording_id", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("subject_id", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("classification", "STRING"),
        ]
        bq.create_table(bigquery.Table(CLS_TABLE_REF, schema=schema))
        print(f"Table {CLS_TABLE_ID} created successfully.")
        
    try:
        bq.get_table(PERF_TABLE_REF)
    except NotFound:
        schema = [
            bigquery.SchemaField("timestamp", "TIMESTAMP", mode="REQUIRED"),
            bigquery.SchemaField("device_id", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("recording_id", "STRING", mode="NULLABLE"),
            bigquery.SchemaField("packet_counter", "INTEGER"),
            bigquery.SchemaField("latency_ms", "FLOAT"),
            bigquery.SchemaField("jitter_ms", "FLOAT"),
            bigquery.SchemaField("lost_packets_cumulative", "INTEGER"),
            bigquery.SchemaField("packet_loss_pct_cumulative", "FLOAT"),
        ]
        bq.create_table(bigquery.Table(PERF_TABLE_REF, schema=schema))
        print(f"Table {PERF_TABLE_ID} created successfully.")

ensure_bq_tables()

# ==================================================================
# DSP & Machine Learning Model Loading
# ==================================================================
scaler = model_xgb = kmeans_abnormal = None
try:
    scaler = joblib.load(os.path.join(MODEL_PATH, 'scaler.joblib'))
    model_xgb = joblib.load(os.path.join(MODEL_PATH, 'model_xgb_binary.joblib'))
    kmeans_abnormal = joblib.load(os.path.join(MODEL_PATH, 'kmeans_abnormal_model.joblib'))
except Exception as e:
    print(f"[WARNING] Failed to load ML model: {e}")

LEAD_KEYS_STRING = ["I", "II", "III", "AVR", "AVL", "AVF", "V1", "V2", "V3", "V4", "V5", "V6"]
LEAD_KEYS_COLUMN = ["lead_I", "lead_II", "lead_III", "avr", "avl", "avf", "v1", "v2", "v3", "v4", "v5", "v6"]

VREF, GAIN, RESOLUTION = 2.42, 200, 24
LSB_SIZE = VREF / (2**RESOLUTION - 1)
ADC_EDGE = 0.95 * (2**23 - 1)

def to_mV(adc):
    """Converts raw 24-bit integer ADC counts to millivolts (mV).
    
    Formula:
        mV = adc * (VREF / (2^RESOLUTION - 1)) * 1000.0 / GAIN
    
    Args:
        adc: Numeric or array-like raw ADC values.
        
    Returns:
        Converted physical potential in millivolts (mV).
    """
    return adc * LSB_SIZE * 1000.0 / GAIN


def despike_hampel(x, k=7, nsigma=6.0):
    """Attenuates high-amplitude impulse spikes using a Hampel outlier filter.
    
    Computes rolling median and Median Absolute Deviation (MAD) over a window
    of size 2k + 1. Samples exceeding nsigma standard deviations are replaced
    via 1D linear interpolation across clean neighboring samples.
    
    Args:
        x (np.ndarray): Input 1D ECG signal array.
        k (int): Half-window length in samples (default 7).
        nsigma (float): Number of standard deviations for outlier gating (default 6.0).
        
    Returns:
        tuple[np.ndarray, np.ndarray]:
            - xi: Cleaned ECG signal array.
            - mask: Boolean mask indicating detected outlier indices.
    """
    x = np.asarray(x, dtype=float)
    med = scipy.signal.medfilt(x, kernel_size=2*k+1)
    diff = np.abs(x - med)
    mad = scipy.signal.medfilt(diff, kernel_size=2*k+1)
    mad = np.maximum(mad, 1e-12)
    mask = diff > (nsigma * 1.4826 * mad)
    xi = x.copy()
    if np.any(mask):
        idx = np.arange(len(x))
        xi[mask] = np.interp(idx[mask], idx[~mask], x[~mask])
    return xi, mask


def desaturate_edges(x):
    """Detects and interpolates clipped/saturated ADC samples.
    
    Clips occurring at >= 95% of the 24-bit headroom (due to electrode detachment
    or rail saturation) are masked and linearly interpolated.
    
    Args:
        x (np.ndarray): Input 1D ECG signal array.
        
    Returns:
        tuple[np.ndarray, np.ndarray]:
            - xi: Desaturated ECG signal array.
            - mask: Boolean mask indicating saturated sample locations.
    """
    x = np.asarray(x, dtype=float)
    mask = (np.abs(x) >= ADC_EDGE)
    xi = x.copy()
    if np.any(mask):
        idx = np.arange(len(x))
        xi[mask] = np.interp(idx[mask], idx[~mask], x[~mask])
    return xi, mask


def apply_iec_diagnostic_filter(signal, fs):
    """Applies IEC 60601-2-51 diagnostic bandpass and notch filtering.
    
    Implements:
    - 2-second edge reflection padding to prevent filter edge transients.
    - 4th-order highpass Butterworth filter at 0.05 Hz (baseline wander removal).
    - 4th-order lowpass Butterworth filter at 150.0 Hz (high-frequency muscle noise suppression).
    - 2nd-order IIR notch filter at 50.0 Hz (Q=30) for AC power line interference.
    
    All stages utilize zero-phase forward-backward filtering (`scipy.signal.filtfilt`).
    
    Args:
        signal (np.ndarray): Input raw detrended millivolt ECG signal.
        fs (float): Sampling frequency in Hz.
        
    Returns:
        np.ndarray: Filtered diagnostic ECG signal trimmed back to original length.
    """
    pad_len = int(fs * 2)
    padded_signal = np.pad(signal, (pad_len, pad_len), mode='edge')
    nyquist = fs / 2.0
    hp_cutoff = min(0.05 / nyquist, 0.99)
    b_hp, a_hp = scipy.signal.butter(4, hp_cutoff, btype='highpass')
    signal_hp = scipy.signal.filtfilt(b_hp, a_hp, padded_signal)
    lp_cutoff = min(150 / nyquist, 0.99)
    b_lp, a_lp = scipy.signal.butter(4, lp_cutoff, btype='lowpass')
    signal_lp = scipy.signal.filtfilt(b_lp, a_lp, signal_hp)
    f0 = 50.0
    Q = 30.0
    if f0 < nyquist:
        w0 = f0 / nyquist
        b_notch, a_notch = scipy.signal.iirnotch(w0, Q)
        signal_filtered = scipy.signal.filtfilt(b_notch, a_notch, signal_lp)
    else:
        signal_filtered = signal_lp
    return signal_filtered[pad_len:-pad_len]


def wavelet_denoise(x, fs, wavelet='sym6', level=None, mode='symmetric', method='soft', protect_low_hz=2.0, protect_band=(1.0, 40.0), protect_factor=0.5):
    """Performs multi-resolution wavelet denoising with cardiac band protection.
    
    Decomposes the signal using Symlet wavelets (default 'sym6'). Uses Donoho's
    universal threshold with a robust noise variance estimator (MAD / 0.6745).
    Crucially, detail coefficients spanning the primary diagnostic cardiac band
    (1.0 Hz to 40.0 Hz) receive threshold attenuation (protect_factor=0.5) to
    preserve sharp R-peak and QRS morphologies, while sub-2.0 Hz details are left untouched.
    
    Args:
        x (np.ndarray): 1D filtered ECG signal array.
        fs (float): Sampling frequency in Hz.
        wavelet (str): Mother wavelet name (default 'sym6').
        level (int, optional): DWT decomposition level (default min(5, max_level)).
        mode (str): Signal extension mode for PyWavelets (default 'symmetric').
        method (str): Thresholding method ('soft' or 'hard').
        protect_low_hz (float): Frequency below which thresholding is disabled.
        protect_band (tuple[float, float]): Frequency band where threshold is scaled.
        protect_factor (float): Multiplier for threshold in the protected band.
        
    Returns:
        np.ndarray: Denoised ECG signal reconstructed via Inverse DWT.
    """
    x = np.asarray(x, dtype=float)
    w = pywt.Wavelet(wavelet)
    maxlev = pywt.dwt_max_level(len(x), w.dec_len)
    if level is None: level = min(5, maxlev)
    coeffs = pywt.wavedec(x, w, mode=mode, level=level)
    cA, details = coeffs[0], coeffs[1:]
    new_coeffs = [cA]
    lo_prot, hi_prot = protect_band
    for i, cD in enumerate(details, start=1):
        f_low, f_high = fs / (2.0 ** (i + 1)), fs / (2.0 ** i)
        sigma = (np.median(np.abs(cD - np.median(cD))) / 0.6745) + 1e-12
        thr_univ = sigma * np.sqrt(2.0 * np.log(max(cD.size, 2)))
        if f_high <= protect_low_hz: cD_th = cD
        else:
            overlaps = (f_low <= hi_prot) and (f_high >= lo_prot)
            thr = thr_univ * (protect_factor if overlaps else 1.0)
            cD_th = pywt.threshold(cD, thr, mode=method)
        new_coeffs.append(cD_th)
    y = pywt.waverec(new_coeffs, w, mode=mode)
    return y[:len(x)]


def _qrs_ms_from_Ronset_Roffset(beat_index, anchor_sample, waves_dwt, rpeaks, Fs):
    """Fallback calculation for QRS duration based on R-onset and R-offset markers.
    
    Used when NeuroKit2 fails to identify distinct Q or S peaks (e.g. QS patterns).
    
    Args:
        beat_index (int): Index of current cardiac cycle.
        anchor_sample (float): Reference sample position.
        waves_dwt (dict): Delineated wave fiducial markers dictionary.
        rpeaks (dict): Detected R-peak locations.
        Fs (float): Sampling frequency in Hz.
        
    Returns:
        float or None: QRS complex duration in milliseconds.
    """
    rpeaks_arr = np.array([x for x in rpeaks['ECG_R_Peaks'] if not np.isnan(x)], dtype=int)
    r_onsets = np.array([x for x in waves_dwt['ECG_R_Onsets'] if not np.isnan(x)], dtype=int)
    r_offsets = np.array([x for x in waves_dwt['ECG_R_Offsets'] if not np.isnan(x)], dtype=int)
    if len(rpeaks_arr) == 0 or len(r_onsets) == 0 or len(r_offsets) == 0: return None
    j = int(np.argmin(np.abs(rpeaks_arr - int(anchor_sample)))) if anchor_sample is not None else min(max(0, beat_index), len(rpeaks_arr) - 1)
    rp = rpeaks_arr[j]
    onset_candidates, offset_candidates = r_onsets[r_onsets <= rp], r_offsets[r_offsets >= rp]
    if len(onset_candidates) == 0 or len(offset_candidates) == 0: return None
    onset, offset = int(onset_candidates.max()), int(offset_candidates.min())
    if offset <= onset: return None
    return ((offset - onset) / Fs) * 1000.0


def _qt_last_resort_pairing(R_onsets, T_marks, Fs, rr_ms=None, min_abs_ms=160.0, max_abs_ms=600.0):
    """Robust fallback algorithm to pair R-onsets with T-wave offsets/peaks.
    
    Enforces physiological boundary constraints (0.20 * RR <= QT <= 0.60 * RR
    within absolute limits [160ms, 600ms]) to prevent mismatching adjacent beats.
    
    Args:
        R_onsets (array-like): Sample positions of R wave onsets.
        T_marks (array-like): Sample positions of T wave offsets or peaks.
        Fs (float): Sampling rate in Hz.
        rr_ms (float, optional): Mean RR interval in ms for adaptive scaling.
        min_abs_ms (float): Minimum permissible absolute QT interval.
        max_abs_ms (float): Maximum permissible absolute QT interval.
        
    Returns:
        tuple[list[float], float]:
            - qt_ms: List of calculated valid QT durations in ms.
            - median_qt: Median QT duration or np.nan if unresolvable.
    """
    R, T = np.asarray(R_onsets, dtype=float), np.asarray(T_marks, dtype=float)
    R, T = R[~np.isnan(R)], T[~np.isnan(T)]
    if len(R) == 0 or len(T) == 0: return [], np.nan
    lo, hi = (max(min_abs_ms, 0.20 * rr_ms), min(max_abs_ms, 0.60 * rr_ms)) if rr_ms is not None and np.isfinite(rr_ms) else (min_abs_ms, max_abs_ms)
    qt_ms, j = [], 0
    for r in R:
        while j < len(T) and T[j] <= r: j += 1
        if j >= len(T): break
        dt_ms = (T[j] - r) * 1000.0 / Fs
        if lo <= dt_ms <= hi: qt_ms.append(dt_ms)
        else:
            jj, chosen = j + 1, False
            while jj < len(T):
                if lo <= ((T[jj] - r) * 1000.0 / Fs) <= hi: qt_ms.append(((T[jj] - r) * 1000.0 / Fs)); j = jj; chosen = True; break
                jj += 1
    return qt_ms, (float(np.nanmedian(qt_ms)) if len(qt_ms) else np.nan)


def predict_clusters_xgb_integrated(new_data, xgb_binary_model, kmeans_abnormal, abnormal_label_offset=1):
    """Executes the two-stage classification and clustering inference pipeline.
    
    Stage 1: XGBoost binary classifier splits cases into Normal (1) vs Abnormal (0).
    Stage 2: K-Means model clusters abnormal cases into slow vs fast arrhythmia categories.
    
    Args:
        new_data (np.ndarray or pd.DataFrame): Standardized 96-feature array.
        xgb_binary_model: Pretrained XGBoost binary classifier.
        kmeans_abnormal: Pretrained K-Means clustering model for abnormal rhythms.
        abnormal_label_offset (int): Offset added to cluster indices.
        
    Returns:
        np.ndarray: Array of diagnostic label strings:
            - 'Normal'
            - 'Potential Slow Arrhytmia'
            - 'Potential Fast Arrhytmia'
    """
    is_normal = xgb_binary_model.predict(new_data)
    labels_int = np.full(len(new_data), -999, dtype=int)
    idx_norm = np.where(is_normal == 1)[0]; labels_int[idx_norm] = 0
    idx_abn = np.where(is_normal == 0)[0]
    if len(idx_abn):
        cls_nums = kmeans_abnormal.predict(new_data[idx_abn]) + abnormal_label_offset
        labels_int[idx_abn] = cls_nums
    name_map = {0: "Normal", 1: "Potential Slow Arrhytmia", 2: "Potential Fast Arrhytmia"}
    return np.array([name_map.get(i, f"Cluster {i}") for i in labels_int])

def run_analysis_pipeline(recording_id, subject_id, device_id):
    """Executes the asynchronous post-recording DSP, feature extraction, and ML pipeline.
    
    Workflow:
    1. Queries BigQuery (`ecg_raw_per_sample`) for the recorded 8000 samples by recording_id.
    2. Iterates across all 12 leads and executes the 6-stage DSP cleaning pipeline:
       - Desaturation (`desaturate_edges`)
       - Hampel impulse despiking (`despike_hampel`)
       - Millivolt scaling (`to_mV`) and DC detrending
       - IEC 60601-2-51 filtering (`apply_iec_diagnostic_filter`)
       - Symlet-6 wavelet denoising (`wavelet_denoise`)
    3. Delineates P, Q, R, S, T waves via NeuroKit2 and extracts 96 morphological features.
    4. Normalizes features with `scaler.joblib` and evaluates with `model_xgb_binary.joblib`
       and `kmeans_abnormal_model.joblib`.
    5. Persists the final diagnosis string into BigQuery (`ecg_classifications`).
    6. Emits `history_updated` to notify all connected web clients to refresh their tables.
    7. Resets device state back to 'Idle'.
    
    Args:
        recording_id (str): UUID4 of the completed recording session.
        subject_id (str): Patient identifier string.
        device_id (str): Originating Holter device ID.
    """
    try:
        print(f"[{recording_id}][{device_id}] Starting analysis pipeline...")
        query = f"SELECT * FROM `{RAW_TABLE_REF}` WHERE recording_id = @rec_id ORDER BY timestamp"
        params = [bigquery.ScalarQueryParameter("rec_id", "STRING", recording_id)]
        config = bigquery.QueryJobConfig(query_parameters=params)
        df = bq.query(query, job_config=config).to_dataframe()

        if len(df) < 500: raise ValueError(f"Insufficient data for analysis (only {len(df)} rows)")
        
        Fs = SPS
        packedData = {}
        for lead in LEAD_KEYS_STRING:
            lead_lower = lead.lower()
            packedData.update({f"rr_{lead_lower}": 0, f"rr_std_{lead_lower}": 0, f"pr_{lead_lower}": 0, f"qs_{lead_lower}": 0, f"qtc_{lead_lower}": 0, f"st_{lead_lower}": 0, f"rs_ratio_{lead_lower}": 0, f"heartrate_{lead_lower}": 0})
        
        for lead_str, lead_col in zip(LEAD_KEYS_STRING, LEAD_KEYS_COLUMN):
            try:
                ecg_adc = df[lead_col].fillna(0).values.astype(float)
                ecg_adc, _ = desaturate_edges(ecg_adc)
                ecg_adc, _ = despike_hampel(ecg_adc, k=7, nsigma=6.0)
                ecgmv = to_mV(ecg_adc)
                detr_ecg = scipy.signal.detrend(ecgmv, type='constant')
                iec_filtered_signal = apply_iec_diagnostic_filter(detr_ecg, Fs)
                y_filt = wavelet_denoise(iec_filtered_signal, Fs, wavelet='sym6')

                RR_avg, RR_stdev, PR_avg, QS_avg, QT_avg, QTc_avg, ST_avg, RS_ratio, bpm = (0,)*9
                RR_list = []
                _, rpeaks = nk.ecg_peaks(y_filt, sampling_rate=Fs)
                _, waves_dwt = nk.ecg_delineate(y_filt, rpeaks, sampling_rate=Fs, method="dwt")
                if len(rpeaks['ECG_R_Peaks']) > 1:
                    RR_list = [((rpeaks['ECG_R_Peaks'][i+1] - rpeaks['ECG_R_Peaks'][i]) / Fs) * 1000.0 for i in range(len(rpeaks['ECG_R_Peaks']) - 1)]
                    if RR_list: RR_avg, RR_stdev, bpm = np.mean(RR_list), np.std(RR_list), (60000 / np.mean(RR_list) if np.mean(RR_list) > 0 else 0)
                if 'ECG_R_Onsets' in waves_dwt and 'ECG_P_Onsets' in waves_dwt and len(waves_dwt['ECG_R_Onsets']) > 1 and len(waves_dwt['ECG_P_Onsets']) > 1:
                    pr_peak_list = [((waves_dwt['ECG_R_Onsets'][i] - waves_dwt['ECG_P_Onsets'][i]) / Fs) * 1000.0 for i in range(min(len(waves_dwt['ECG_R_Onsets']), len(waves_dwt['ECG_P_Onsets'])))]
                    if pr_peak_list: PR_avg = np.mean([p for p in pr_peak_list if p > 0])
                if 'ECG_S_Peaks' in waves_dwt and 'ECG_Q_Peaks' in waves_dwt and len(waves_dwt['ECG_S_Peaks']) > 0 and len(waves_dwt['ECG_Q_Peaks']) > 0:
                    qs_peak_list = []
                    for i in range(min(len(waves_dwt['ECG_S_Peaks']), len(waves_dwt['ECG_Q_Peaks']))):
                        ms_dist = ((waves_dwt['ECG_S_Peaks'][i] - waves_dwt['ECG_Q_Peaks'][i]) / Fs) * 1000.0
                        if abs(ms_dist) < 1e-9:
                            q_i, s_i = waves_dwt['ECG_Q_Peaks'][i], waves_dwt['ECG_S_Peaks'][i]
                            anchor = float(np.nanmean([v for v in [q_i, s_i] if not np.isnan(v)])) if not (np.isnan(q_i) and np.isnan(s_i)) else None
                            fallback_ms = _qrs_ms_from_Ronset_Roffset(i, anchor, waves_dwt, rpeaks, Fs)
                            if fallback_ms: ms_dist = fallback_ms
                        qs_peak_list.append(ms_dist)
                    if qs_peak_list: QS_avg = np.mean([q for q in qs_peak_list if q > 0])
                if 'ECG_T_Offsets' in waves_dwt and 'ECG_R_Onsets' in waves_dwt and len(waves_dwt['ECG_T_Offsets']) > 0 and len(waves_dwt['ECG_R_Onsets']) > 0:
                    qt_peak_list = [((waves_dwt['ECG_T_Offsets'][i] - waves_dwt['ECG_R_Onsets'][i]) / Fs) * 1000.0 for i in range(min(len(waves_dwt['ECG_T_Offsets']), len(waves_dwt['ECG_R_Onsets'])))]
                    if qt_peak_list: QT_avg = np.mean([q for q in qt_peak_list if q > 0])
                    if QT_avg > 0 and RR_avg > 0: QTc_avg = QT_avg / math.sqrt(RR_avg/1000.0)
                if not (QTc_avg > 0):
                    rr_ms_mean = RR_avg if RR_avg > 0 else None
                    _, qt_avg_fb = _qt_last_resort_pairing(waves_dwt.get('ECG_R_Onsets',[]), waves_dwt.get('ECG_T_Offsets',[]), Fs, rr_ms=rr_ms_mean)
                    if not np.isfinite(qt_avg_fb): _, qt_avg_fb = _qt_last_resort_pairing(waves_dwt.get('ECG_R_Onsets',[]), waves_dwt.get('ECG_T_Peaks',[]), Fs, rr_ms=rr_ms_mean)
                    if np.isfinite(qt_avg_fb): QT_avg, QTc_avg = float(qt_avg_fb), (float(qt_avg_fb) / math.sqrt(rr_ms_mean/1000.0) if rr_ms_mean else 0)
                if 'ECG_T_Onsets' in waves_dwt and 'ECG_R_Offsets' in waves_dwt and len(waves_dwt['ECG_T_Onsets']) > 0 and len(waves_dwt['ECG_R_Offsets']) > 0:
                    st_peak_list = [((waves_dwt['ECG_T_Onsets'][i] - waves_dwt['ECG_R_Offsets'][i]) / Fs) * 1000.0 for i in range(min(len(waves_dwt['ECG_T_Onsets']), len(waves_dwt['ECG_R_Offsets'])))]
                    if st_peak_list: ST_avg = np.mean([s for s in st_peak_list if s > 0])
                r_peaks_clean = np.array([p for p in rpeaks['ECG_R_Peaks'] if not np.isnan(p)], dtype=int)
                s_peaks_clean = np.array([p for p in waves_dwt.get('ECG_S_Peaks',[]) if not np.isnan(p)], dtype=int)
                if len(r_peaks_clean) > 0 and len(s_peaks_clean) > 0:
                    R_mean_amp = np.mean([y_filt[i] for i in r_peaks_clean])
                    S_mean_amp = np.mean([y_filt[i] for i in s_peaks_clean])
                    if abs(S_mean_amp) > 1e-9: RS_ratio = R_mean_amp / abs(S_mean_amp)
                lead_lower = lead_str.lower()
                packedData.update({f"rr_{lead_lower}": RR_avg, f"rr_std_{lead_lower}": RR_stdev, f"pr_{lead_lower}": PR_avg, f"qs_{lead_lower}": QS_avg, f"qtc_{lead_lower}": QTc_avg, f"st_{lead_lower}": ST_avg, f"rs_ratio_{lead_lower}": RS_ratio, f"heartrate_{lead_lower}": bpm})
            except Exception as e_lead:
                print(f"[{recording_id}][{device_id}] Failed to process lead {lead_str}: {e_lead}")

        classification = "Error: Models not loaded or feature extraction failed."
        if all([scaler, model_xgb, kmeans_abnormal]):
            FEATURE_FAMILIES = ['rr', 'rr_std', 'pr', 'qs', 'qtc', 'st', 'rs_ratio', 'heartrate']
            EXPECTED_FEATURES = [f"{fam}_{l.lower()}" for l in LEAD_KEYS_STRING for fam in FEATURE_FAMILIES]
            X = pd.DataFrame([packedData], columns=EXPECTED_FEATURES).astype(float).fillna(0)
            if hasattr(scaler, "feature_names_in_"): X = X.reindex(columns=scaler.feature_names_in_, fill_value=0.0)
            X_scaled = scaler.transform(X)
            classification = predict_clusters_xgb_integrated(X_scaled, model_xgb, kmeans_abnormal)[0]

        print(f"[{recording_id}][{device_id}] Final classification result: {classification}")
        cls_row = [{"timestamp": datetime.now(timezone.utc).isoformat(), "device_id": device_id, "recording_id": recording_id, "subject_id": subject_id, "classification": classification}]
        errors = bq.insert_rows_json(CLS_TABLE_REF, cls_row)
        if not errors: 
            print(f"[{recording_id}][{device_id}] Analysis results successfully saved.")
            socketio.emit('history_updated')
        else: 
            print(f"[{recording_id}][{device_id}] Failed to save analysis results: {errors}")
            
    except Exception as e:
        print(f"[{recording_id}][{device_id}] Critical failure in analysis pipeline: {e}\n{traceback.format_exc()}")
        cls_row = [{"timestamp": datetime.now(timezone.utc).isoformat(), "device_id": device_id, "recording_id": recording_id, "subject_id": subject_id, "classification": "System Analysis Error"}]
        bq.insert_rows_json(CLS_TABLE_REF, cls_row)
        socketio.emit('history_updated')
    finally:
        if device_id in device_states:
            state = device_states[device_id]
            state["status_message"] = "Idle"
            state["recording_id"] = None
            state["subject_id"] = None
            state["samples_collected"] = 0
            send_state_update(device_id, room=device_id)
            print(f"[{device_id}] Status reset to Idle.")


# ==================================================================
# Flask Routes & Socket.IO Handlers
# ==================================================================
def send_state_update(device_id, room):
    """Broadcasts the current device state dictionary to a specific Socket.IO room.
    
    Serializes internal deque instances (e.g. latency histories) to lists
    to ensure JSON compatibility.
    
    Args:
        device_id (str): Device identifier.
        room (str): Socket.IO room name or client session ID (sid).
    """
    if device_id not in device_states: return
    
    state = device_states[device_id]
    state_to_send = state.copy()
    
    if 'latencies' in state_to_send and isinstance(state_to_send['latencies'], deque):
        state_to_send['latencies'] = list(state_to_send['latencies'])
        
    state_to_send['device_id'] = device_id
    socketio.emit('state_update', state_to_send, room=room)

@app.route("/")
def index():
    """Serves the single-page HTML application dashboard."""
    return render_template_string(HTML_TEMPLATE, broker=MQTT_BROKER, port=MQTT_PORT, topic=MQTT_TOPIC)

@app.route("/api/history")
def get_history():
    """Fetches the 50 most recent ECG classifications from BigQuery.
    
    Returns:
        JSON response list containing timestamp, device_id, subject_id,
        recording_id, and classification label.
    """
    try:
        query = f"SELECT * FROM `{CLS_TABLE_REF}` ORDER BY timestamp DESC LIMIT 50"
        return jsonify([dict(row) for row in bq.query(query).result()])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@socketio.on('connect')
def on_connect_socket():
    """Handles WebSocket client connection and pushes active device list."""
    print(f"Client connected: {request.sid}")
    socketio.emit('device_list_update', list(d for d in device_states if d is not None))

@socketio.on('disconnect')
def on_disconnect():
    """Handles WebSocket client disconnection."""
    print(f"Client disconnected: {request.sid}")

@socketio.on('subscribe_to_device')
def handle_subscribe(data):
    """Subscribes client socket to a specific device room for filtered data streaming."""
    device_id = data.get('device_id')
    if not device_id: return
    
    join_room(device_id)
    print(f"Client {request.sid} subscribed to device: {device_id}")
    send_state_update(device_id, room=request.sid)

@socketio.on('start_recording')
def start_recording(data):
    """Initializes a new recording session for the target device."""
    device_id = data.get("device_id")
    subject_id = data.get("subject_id")
    if not device_id or not subject_id: return

    state = device_states.get(device_id, {})
    if state.get("is_recording"): return

    state.update({
        "is_recording": True, "subject_id": subject_id, "recording_id": str(uuid.uuid4()), 
        "samples_collected": 0, "status_message": "Recording..."
    })
    device_states[device_id] = state
    
    print(f"Starting recording for device {device_id} (Subject: {subject_id}) with ID: {state['recording_id']}")
    send_state_update(device_id, room=device_id)

@socketio.on('stop_recording')
def stop_recording_handler(data):
    """Handles manual recording halt requested via the web UI."""
    device_id = (data or {}).get("device_id")
    stop_recording(device_id=device_id, manual=True)

def stop_recording(device_id, manual):
    """Halts active recording session and dispatches the background ML analysis pipeline.
    
    Args:
        device_id (str): Target Holter device ID.
        manual (bool): True if triggered by user button; False if triggered by buffer completion.
    """
    state = device_states.get(device_id)
    if not device_id or not state or not state.get("is_recording"): return

    rec_id = state['recording_id']
    sub_id = state['subject_id']
    
    state["is_recording"] = False
    state["status_message"] = "Recording finished. Starting analysis..."
    
    print(f"Recording {rec_id} for device {device_id} stopped. Starting background analysis.")
    send_state_update(device_id, room=device_id)
    socketio.start_background_task(run_analysis_pipeline, rec_id, sub_id, device_id)


# ==================================================================
# MQTT Client
# ==================================================================
try:
    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
except TypeError:
    mqtt_client = mqtt.Client()

def on_connect(client, userdata, flags, rc, properties=None):
    """Callback triggered upon establishing connection with the MQTT broker.
    
    Subscribes to MQTT_TOPIC (default: 'ecg/+/data') upon success (rc == 0).
    """
    if rc == 0:
        client.subscribe(MQTT_TOPIC)
        print(f"[MQTT] Connected and subscribed to topic: {MQTT_TOPIC}")
    else:
        print(f"[MQTT] Failed to connect, code: {rc}")

def on_message(client, userdata, msg):
    """Handles incoming binary MQTT telemetry packets from ECG Holters.
    
    Workflow:
    1. Extracts device_id from topic ('ecg/{device_id}/data').
    2. Initializes device state tracking on first packet receipt.
    3. Validates framing delimiters (STX=0x02, ETX=0x03) and 8-bit checksum.
    4. Decodes binary payload:
       - 63-byte frame: Little-endian uint64 timestamp, uint32 counter, 12x int32 leads.
       - 51-byte frame: Little-endian 12x int32 leads (legacy format).
    5. Computes network QoS metrics (latency, jitter, packet loss).
    6. Broadcasts `live_data` and `performance_update` to device room.
    7. If active recording session, buffers raw samples to `live_buffer` and
       triggers `stop_recording` when BUFFER_SIZE (8000) is reached.
    """
    try:
        parts = msg.topic.split('/')
        if len(parts) != 3 or parts[0] != 'ecg' or parts[2] != 'data':
            return
        
        device_id = parts[1]

        if device_id not in device_states:
            device_states[device_id] = {
                "is_recording": False, "subject_id": None, "recording_id": None,
                "samples_collected": 0, "status_message": "Idle",
                "last_packet_num": 0, "lost_packets": 0, "total_packets": 0,
                "latencies": deque(maxlen=100), "packet_format": "Unknown",
            }
            socketio.emit('device_list_update', list(device_states.keys()))
        
        state = device_states[device_id]
        state['last_seen'] = time.time()
        
        payload = msg.payload
        perform_metrics = False
        
        if len(payload) == 63 and payload[0] == 0x02 and payload[-1] == 0x03:
            perform_metrics = True
            state['packet_format'] = 'Metrics (63-byte)'
            data_bytes = payload[1:61]
            received_checksum = payload[61]
            calculated_checksum = sum(data_bytes) & 0xFF
            if received_checksum != calculated_checksum:
                print(f"[{device_id}] Checksum mismatch for 63-byte packet. Discarding.")
                return
            timestamp_us, packet_counter, *unpacked_values = struct.unpack('<QI12i', data_bytes)
            normalized_pkt = dict(zip(LEAD_KEYS_COLUMN, unpacked_values))

        elif len(payload) == 51 and payload[0] == 0x02 and payload[-1] == 0x03:
            state['packet_format'] = 'Legacy (51-byte)'
            data_bytes = payload[1:49]
            received_checksum = payload[49]
            calculated_checksum = sum(data_bytes) & 0xFF
            if received_checksum != calculated_checksum:
                print(f"[{device_id}] Checksum mismatch for 51-byte packet. Discarding.")
                return
            unpacked_values = struct.unpack('<12i', data_bytes)
            normalized_pkt = dict(zip(LEAD_KEYS_COLUMN, unpacked_values))
        else:
            print(f"[{device_id}] Malformed or unknown binary packet received. Length: {len(payload)}. Discarding.")
            return

        normalized_pkt['device_id'] = device_id
        socketio.emit('live_data', normalized_pkt, room=device_id)
        
        if perform_metrics:
            server_time_us = time.time_ns() // 1000
            latency_ms = (server_time_us - timestamp_us) / 1000.0
            if latency_ms > 0: state['latencies'].append(latency_ms)

            state['total_packets'] += 1
            if state['last_packet_num'] > 0 and packet_counter > state['last_packet_num'] + 1:
                lost = packet_counter - (state['last_packet_num'] + 1)
                state['lost_packets'] += lost
            state['last_packet_num'] = packet_counter
            
            total_received = state['total_packets'] - state['lost_packets']
            packet_loss_pct = (state['lost_packets'] / state['total_packets'] * 100) if state['total_packets'] > 0 else 0
            avg_latency = np.mean(state['latencies']) if state['latencies'] else 0
            jitter = np.std(state['latencies']) if len(state['latencies']) > 1 else 0

            perf_data_for_ui = {
                'device_id': device_id,
                'packet_format': state['packet_format'],
                'latency_ms': round(latency_ms, 2),
                'avg_latency_ms': round(avg_latency, 2),
                'jitter_ms': round(jitter, 2),
                'lost_packets': state['lost_packets'],
                'total_received': total_received,
                'packet_loss_pct': round(packet_loss_pct, 2),
            }
            socketio.emit('performance_update', perf_data_for_ui, room=device_id)

            perf_row_for_bq = {
                "timestamp": datetime.now(timezone.utc).isoformat(), "device_id": device_id,
                "recording_id": state.get("recording_id"), "packet_counter": packet_counter,
                "latency_ms": latency_ms, "jitter_ms": jitter,
                "lost_packets_cumulative": state['lost_packets'],
                "packet_loss_pct_cumulative": packet_loss_pct,
            }
            perf_buffer.append(perf_row_for_bq)
        else:
             socketio.emit('performance_update', {'device_id': device_id, 'packet_format': state['packet_format']}, room=device_id)

        if state and state.get("is_recording"):
            state["samples_collected"] += 1
            
            row_to_insert = {
                "timestamp": datetime.now(timezone.utc).isoformat(), "device_id": device_id,
                "recording_id": state["recording_id"], "subject_id": state["subject_id"]
            }
            row_to_insert.update(normalized_pkt)
            live_buffer.append(row_to_insert)

            if state["samples_collected"] % 25 == 0:
                progress_data = {"device_id": device_id, "current": state["samples_collected"], "total": BUFFER_SIZE}
                socketio.emit('progress_update', progress_data, room=device_id)

            if state["samples_collected"] >= BUFFER_SIZE:
                stop_recording(device_id=device_id, manual=False)
                
    except Exception as e:
        print(f"[MQTT] Error processing message from topic {msg.topic}: {e}\n{traceback.format_exc()}")

# ==================================================================
# Background Tasks & Workers
# ==================================================================
def device_cleanup_task():
    """Periodically purges inactive devices exceeding DEVICE_TIMEOUT_S.
    
    Runs every 5 seconds. If a device has not published a packet within
    DEVICE_TIMEOUT_S (default 15s), it is removed from `device_states` and
    an updated `device_list_update` event is broadcasted to all clients.
    """
    while True:
        now = time.time()
        timed_out_devices = [
            device_id for device_id, state in device_states.items()
            if now - state.get('last_seen', now) > DEVICE_TIMEOUT_S
        ]
        
        if timed_out_devices:
            print(f"Device(s) timed out and will be removed: {timed_out_devices}")
            for device_id in timed_out_devices:
                del device_states[device_id]
            
            socketio.emit('device_list_update', list(device_states.keys()))

        socketio.sleep(5)


def global_perf_emitter():
    """Periodically broadcasts a global multi-device QoS overview table.
    
    Runs every 2 seconds, emitting `global_performance_update` containing
    status, packet format, latency, jitter, and loss percentage for all active devices.
    """
    while True:
        summary = []
        for device_id, state in list(device_states.items()):
            if not state or 'latencies' not in state: continue
            
            packet_loss_pct = (state['lost_packets'] / state['total_packets'] * 100) if state.get('total_packets', 0) > 0 else 0
            avg_latency = np.mean(state['latencies']) if state['latencies'] else 0
            jitter = np.std(state['latencies']) if len(state['latencies']) > 1 else 0

            summary.append({
                'device_id': device_id,
                'status_message': state.get('status_message', 'N/A'),
                'packet_format': state.get('packet_format', 'Unknown'),
                'avg_latency_ms': avg_latency,
                'jitter_ms': jitter,
                'packet_loss_pct': packet_loss_pct,
            })
        
        socketio.emit('global_performance_update', summary)
        socketio.sleep(2)

def start_mqtt_client():
    """Initializes and connects the asynchronous Paho-MQTT client."""
    print("Starting MQTT client...")
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    mqtt_client.reconnect_delay_set(min_delay=1, max_delay=30)
    mqtt_client.connect_async(MQTT_BROKER, MQTT_PORT, 60)
    mqtt_client.loop_start()

def bq_inserter():
    """Background worker continuously draining live_buffer into BigQuery.
    
    Flushes queued raw ECG sample dictionaries in batches every 1 second
    to the `ecg_raw_per_sample` table using `insert_rows_json`.
    """
    while True:
        if len(live_buffer) > 0:
            items_to_insert = []
            while len(live_buffer) > 0:
                items_to_insert.append(live_buffer.popleft())
            if items_to_insert:
                try:
                    errors = bq.insert_rows_json(RAW_TABLE_REF, items_to_insert)
                    if errors:
                        print(f"Error inserting ECG data into BigQuery: {errors}")
                except Exception as e:
                    print(f"Exception inserting ECG data into BigQuery: {e}")
        socketio.sleep(1)

def bq_perf_inserter():
    """Background worker continuously draining perf_buffer into BigQuery.
    
    Flushes queued network QoS metric records in batches every 2 seconds
    to the `ecg_performance_metrics` table using `insert_rows_json`.
    """
    while True:
        if len(perf_buffer) > 0:
            items_to_insert = []
            while len(perf_buffer) > 0:
                items_to_insert.append(perf_buffer.popleft())
            if items_to_insert:
                try:
                    errors = bq.insert_rows_json(PERF_TABLE_REF, items_to_insert)
                    if errors:
                        print(f"Error inserting performance data into BigQuery: {errors}")
                except Exception as e:
                    print(f"Exception inserting performance data into BigQuery: {e}")
        socketio.sleep(2) 

# ==================================================================
# Application Entry Point
# ==================================================================
if __name__ == "__main__":
    start_mqtt_client()
    socketio.start_background_task(bq_inserter)
    socketio.start_background_task(bq_perf_inserter)
    socketio.start_background_task(global_perf_emitter) 
    socketio.start_background_task(device_cleanup_task)
    print(f"Running web server at http://0.0.0.0:{FLASK_PORT}")
    socketio.run(app, host="0.0.0.0", port=FLASK_PORT, use_reloader=False)

