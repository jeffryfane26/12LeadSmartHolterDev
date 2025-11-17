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
# Konfigurasi Aplikasi (MODIFIED)
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
DEVICE_TIMEOUT_S = 15 # <-- ADDED: Detik sebelum device dianggap offline

# ==================================================================
# Inisialisasi Aplikasi (MODIFIED)
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
        <div id="sock" class="pill">Socket: Menghubungkan...</div>
    </div>
    <div class="panel">
        <h3>Pilih Device (untuk Tampilan Detail)</h3>
        <div class="row">
            <select id="device-selector" style="min-width: 250px;"></select>
            <div id="selected-device" class="pill">Device Terpilih: <span class="value">-</span></div>
        </div>
    </div>
    <div class="tabs">
        <button class="tab-button active" onclick="openTab(event, 'ecg-view')">12-Lead View</button>
        <button class="tab-button" onclick="openTab(event, 'perf-view')">Performance Metrics (Detail)</button>
        <button class="tab-button" onclick="openTab(event, 'overview-view')">Multi-Device Overview</button>
    </div>
    <div id="ecg-view" class="tab-content" style="display: block;">
        <div class="panel">
            <h3>Kontrol Perekaman</h3>
            <div class="row">
                <input id="subject" type="text" placeholder="ID Subjek (wajib diisi)" size="30"/>
                <button id="start">Mulai Perekaman</button>
                <button id="stop">Hentikan Manual</button>
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
            <h3>Riwayat Klasifikasi</h3>
            <table id="history-table">
                <thead>
                    <tr>
                        <th>Timestamp (UTC)</th>
                        <th>Device ID</th>
                        <th>Subject ID</th>
                        <th>Recording ID</th>
                        <th>Hasil Klasifikasi</th>
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
        socket.on('connect', () => { sockEl.textContent = 'Socket: Terhubung'; sockEl.classList.add('ok'); });
        socket.on('disconnect', () => { sockEl.textContent = 'Socket: Terputus'; sockEl.classList.add('bad'); sockEl.classList.remove('ok'); });
        
        socket.on('device_list_update', (devices) => {
            console.log('Available devices:', devices);
            const previouslySelected = deviceSelector.value;
            deviceSelector.innerHTML = '<option value="">-- Pilih Device --</option>';
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
                    // Kosongkan tampilan detail jika device yang dipilih hilang
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

        // --- MODIFIED: deviceSelector.onchange ---
        deviceSelector.onchange = () => {
            const selectedDevId = deviceSelector.value;

            // Bersihkan semua buffer dan UI elemen yang relevan
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
            if (!currentDevice) { alert('Silakan pilih device terlebih dahulu.'); return; }
            if (!subjectId) { alert('Silakan masukkan ID Subjek.'); return; }
            socket.emit('start_recording', { device_id: currentDevice, subject_id: subjectId });
        };

        stopBtn.onclick = () => {
            if (!currentDevice) return;
            socket.emit('stop_recording', { device_id: currentDevice });
        };
        
        // --- MODIFIED: state_update ---
        socket.on('state_update', data => {
            if (data.device_id !== currentDevice) return; // <-- ADDED FILTER
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
        
        // --- MODIFIED: live_data ---
        socket.on('live_data', pkt => {
            if (pkt.device_id !== currentDevice) return; // <-- ADDED FILTER
            liveDataBuffer.push(pkt);
            if (!isRendering) {
                isRendering = true;
                requestAnimationFrame(renderLoop);
            }
        });

        // --- MODIFIED: performance_update ---
        socket.on('performance_update', data => {
            if (data.device_id !== currentDevice) return; // <-- ADDED FILTER
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
            overviewTableBody.innerHTML = ''; // Selalu bersihkan dan gambar ulang
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
        
        // --- MODIFIED: progress_update ---
        socket.on('progress_update', data => {
            if (data.device_id !== currentDevice) return; // <-- ADDED FILTER
            const percent = (data.current / data.total) * 100;
            progressBar.style.width = percent + '%';
            progressLabel.textContent = `${data.current} / ${data.total} sampel`;
        });
        
        function fetchHistory() {
            fetch('/api/history')
                .then(response => response.json())
                .then(data => {
                    if (data.error) { console.error("Gagal mengambil riwayat:", data.error); return; }
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
            console.log('Menerima event history_updated, memuat ulang riwayat...');
            fetchHistory();
        });
        
        fetchHistory();
    });
</script>
</body>
</html>
"""

# ==================================================================
# Fungsi Bantuan & Inisialisasi BigQuery
# ==================================================================
def ensure_bq_tables():
    print("Memastikan tabel BigQuery...")
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
        print(f"Tabel {RAW_TABLE_ID} berhasil dibuat.")
        
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
        print(f"Tabel {CLS_TABLE_ID} berhasil dibuat.")
        
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
        print(f"Tabel {PERF_TABLE_ID} berhasil dibuat.")

ensure_bq_tables()

# ==================================================================
# DSP & Model ML (No Changes)
# ==================================================================
scaler = model_xgb = kmeans_abnormal = None
try:
    scaler = joblib.load(os.path.join(MODEL_PATH, 'scaler.joblib'))
    model_xgb = joblib.load(os.path.join(MODEL_PATH, 'model_xgb_binary.joblib'))
    kmeans_abnormal = joblib.load(os.path.join(MODEL_PATH, 'kmeans_abnormal_model.joblib'))
except Exception as e:
    print(f"[PERINGATAN] Gagal memuat model ML: {e}")

LEAD_KEYS_STRING = ["I", "II", "III", "AVR", "AVL", "AVF", "V1", "V2", "V3", "V4", "V5", "V6"]
LEAD_KEYS_COLUMN = ["lead_I", "lead_II", "lead_III", "avr", "avl", "avf", "v1", "v2", "v3", "v4", "v5", "v6"]

VREF, GAIN, RESOLUTION = 2.42, 200, 24
LSB_SIZE = VREF / (2**RESOLUTION - 1)
ADC_EDGE = 0.95 * (2**23 - 1)

def to_mV(adc):
    return adc * LSB_SIZE * 1000.0 / GAIN
def despike_hampel(x, k=7, nsigma=6.0):
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
    x = np.asarray(x, dtype=float)
    mask = (np.abs(x) >= ADC_EDGE)
    xi = x.copy()
    if np.any(mask):
        idx = np.arange(len(x))
        xi[mask] = np.interp(idx[mask], idx[~mask], x[~mask])
    return xi, mask
def apply_iec_diagnostic_filter(signal, fs):
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
    is_normal = xgb_binary_model.predict(new_data)
    labels_int = np.full(len(new_data), -999, dtype=int)
    idx_norm = np.where(is_normal == 1)[0]; labels_int[idx_norm] = 0
    idx_abn = np.where(is_normal == 0)[0]
    if len(idx_abn):
        cls_nums = kmeans_abnormal.predict(new_data[idx_abn]) + abnormal_label_offset
        labels_int[idx_abn] = cls_nums
    name_map = {0: "Normal", 1: "Potential Slow Arrhytmia", 2: "Potential Fast Arrhytmia"}
    return np.array([name_map.get(i, f"Cluster {i}") for i in labels_int])

# MODIFIED
def run_analysis_pipeline(recording_id, subject_id, device_id):
    try:
        print(f"[{recording_id}][{device_id}] Memulai pipeline analisis...")
        # ... (kode analisis yang ada tetap sama)
        # ... (setelah semua analisis selesai, baik berhasil atau gagal) ...
        query = f"SELECT * FROM `{RAW_TABLE_REF}` WHERE recording_id = @rec_id ORDER BY timestamp"
        params = [bigquery.ScalarQueryParameter("rec_id", "STRING", recording_id)]
        config = bigquery.QueryJobConfig(query_parameters=params)
        df = bq.query(query, job_config=config).to_dataframe()

        if len(df) < 500: raise ValueError(f"Data tidak cukup untuk analisis (hanya {len(df)} baris)")
        
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
                print(f"[{recording_id}][{device_id}] Gagal memproses lead {lead_str}: {e_lead}")

        classification = "Error: Model tidak dimuat atau data fitur gagal diekstrak."
        if all([scaler, model_xgb, kmeans_abnormal]):
            FEATURE_FAMILIES = ['rr', 'rr_std', 'pr', 'qs', 'qtc', 'st', 'rs_ratio', 'heartrate']
            EXPECTED_FEATURES = [f"{fam}_{l.lower()}" for l in LEAD_KEYS_STRING for fam in FEATURE_FAMILIES]
            X = pd.DataFrame([packedData], columns=EXPECTED_FEATURES).astype(float).fillna(0)
            if hasattr(scaler, "feature_names_in_"): X = X.reindex(columns=scaler.feature_names_in_, fill_value=0.0)
            X_scaled = scaler.transform(X)
            classification = predict_clusters_xgb_integrated(X_scaled, model_xgb, kmeans_abnormal)[0]

        print(f"[{recording_id}][{device_id}] Hasil klasifikasi final: {classification}")
        cls_row = [{"timestamp": datetime.now(timezone.utc).isoformat(), "device_id": device_id, "recording_id": recording_id, "subject_id": subject_id, "classification": classification}]
        errors = bq.insert_rows_json(CLS_TABLE_REF, cls_row)
        if not errors: 
            print(f"[{recording_id}][{device_id}] Hasil analisis berhasil disimpan.")
            socketio.emit('history_updated')
        else: 
            print(f"[{recording_id}][{device_id}] Gagal menyimpan hasil analisis: {errors}")
            
    except Exception as e:
        print(f"[{recording_id}][{device_id}] Gagal total dalam pipeline analisis: {e}\n{traceback.format_exc()}")
        cls_row = [{"timestamp": datetime.now(timezone.utc).isoformat(), "device_id": device_id, "recording_id": recording_id, "subject_id": subject_id, "classification": "Error Analisis Sistem"}]
        bq.insert_rows_json(CLS_TABLE_REF, cls_row)
        socketio.emit('history_updated')
    finally:
        # <-- ADDED BLOCK
        if device_id in device_states:
            state = device_states[device_id]
            state["status_message"] = "Idle"
            state["recording_id"] = None
            state["subject_id"] = None
            state["samples_collected"] = 0
            # Kirim pembaruan status terakhir ke UI
            send_state_update(device_id, room=device_id)
            print(f"[{device_id}] Status dikembalikan ke Idle.")


# ==================================================================
# Rute Flask & Handler Socket.IO
# ==================================================================
def send_state_update(device_id, room):
    if device_id not in device_states: return
    
    state = device_states[device_id]
    state_to_send = state.copy()
    
    if 'latencies' in state_to_send and isinstance(state_to_send['latencies'], deque):
        state_to_send['latencies'] = list(state_to_send['latencies'])
        
    state_to_send['device_id'] = device_id
    socketio.emit('state_update', state_to_send, room=room)

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE, broker=MQTT_BROKER, port=MQTT_PORT, topic=MQTT_TOPIC)

@app.route("/api/history")
def get_history():
    try:
        query = f"SELECT * FROM `{CLS_TABLE_REF}` ORDER BY timestamp DESC LIMIT 50"
        return jsonify([dict(row) for row in bq.query(query).result()])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@socketio.on('connect')
def on_connect_socket():
    print(f"Klien terhubung: {request.sid}")
    socketio.emit('device_list_update', list(d for d in device_states if d is not None))

@socketio.on('disconnect')
def on_disconnect():
    print(f"Klien terputus: {request.sid}")

@socketio.on('subscribe_to_device')
def handle_subscribe(data):
    device_id = data.get('device_id')
    if not device_id: return
    
    join_room(device_id)
    print(f"Klien {request.sid} subscribe ke device: {device_id}")
    send_state_update(device_id, room=request.sid)

@socketio.on('start_recording')
def start_recording(data):
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
    
    print(f"Memulai perekaman untuk device {device_id} (Subjek: {subject_id}) dengan ID: {state['recording_id']}")
    send_state_update(device_id, room=device_id)

@socketio.on('stop_recording')
def stop_recording_handler(data):
    device_id = (data or {}).get("device_id")
    stop_recording(device_id=device_id, manual=True)

def stop_recording(device_id, manual):
    state = device_states.get(device_id)
    if not device_id or not state or not state.get("is_recording"): return

    rec_id = state['recording_id']
    sub_id = state['subject_id']
    
    state["is_recording"] = False
    state["status_message"] = "Perekaman selesai. Memulai analisis..."
    
    print(f"Perekaman {rec_id} untuk device {device_id} dihentikan. Memulai analisis di latar belakang.")
    send_state_update(device_id, room=device_id)
    socketio.start_background_task(run_analysis_pipeline, rec_id, sub_id, device_id)


# ==================================================================
# Klien MQTT
# ==================================================================
try:
    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
except TypeError:
    mqtt_client = mqtt.Client()

def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        client.subscribe(MQTT_TOPIC)
        print(f"[MQTT] Terhubung dan berlangganan ke topik: {MQTT_TOPIC}")
    else:
        print(f"[MQTT] Gagal terhubung, kode: {rc}")

def on_message(client, userdata, msg):
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
        state['last_seen'] = time.time() # <-- ADDED
        
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
        print(f"[MQTT] Error memproses pesan dari topik {msg.topic}: {e}\n{traceback.format_exc()}")

# ==================================================================
# Background Tasks (MODIFIED)
# ==================================================================
def device_cleanup_task():
    """Periodically removes devices that have timed out."""
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
            
            # Beri tahu semua klien tentang daftar perangkat yang diperbarui
            socketio.emit('device_list_update', list(device_states.keys()))

        socketio.sleep(5) # Jalankan pemeriksaan setiap 5 detik


def global_perf_emitter():
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
    print("Memulai klien MQTT...")
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    mqtt_client.reconnect_delay_set(min_delay=1, max_delay=30)
    mqtt_client.connect_async(MQTT_BROKER, MQTT_PORT, 60)
    mqtt_client.loop_start()

def bq_inserter():
    while True:
        if len(live_buffer) > 0:
            items_to_insert = []
            while len(live_buffer) > 0:
                items_to_insert.append(live_buffer.popleft())
            if items_to_insert:
                try:
                    errors = bq.insert_rows_json(RAW_TABLE_REF, items_to_insert)
                    if errors:
                        print(f"Kesalahan saat memasukkan data EKG ke BigQuery: {errors}")
                except Exception as e:
                    print(f"Exception saat memasukkan data EKG ke BigQuery: {e}")
        socketio.sleep(1)

def bq_perf_inserter():
    while True:
        if len(perf_buffer) > 0:
            items_to_insert = []
            while len(perf_buffer) > 0:
                items_to_insert.append(perf_buffer.popleft())
            if items_to_insert:
                try:
                    errors = bq.insert_rows_json(PERF_TABLE_REF, items_to_insert)
                    if errors:
                        print(f"Kesalahan saat memasukkan data performa ke BigQuery: {errors}")
                except Exception as e:
                    print(f"Exception saat memasukkan data performa ke BigQuery: {e}")
        socketio.sleep(2) 

# ==================================================================
# Main (MODIFIED)
# ==================================================================
if __name__ == "__main__":
    start_mqtt_client()
    socketio.start_background_task(bq_inserter)
    socketio.start_background_task(bq_perf_inserter)
    socketio.start_background_task(global_perf_emitter) 
    socketio.start_background_task(device_cleanup_task) # <-- ADDED
    print(f"Menjalankan server web di http://0.0.0.0:{FLASK_PORT}")
    socketio.run(app, host="0.0.0.0", port=FLASK_PORT, use_reloader=False)

