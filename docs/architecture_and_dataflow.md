# System Architecture & Data Flow

This document details the architectural design, component interactions, and data flow of the **12-Lead Smart Holter System**.

---

## 1. High-Level System Architecture

The system follows a three-tier distributed architecture designed for real-time biomedical telemetry, high-throughput ingestion, and cloud-based machine learning classification:

```mermaid
graph TD
    subgraph "Tier 1: Edge Acquisition (ESP32 Holter)"
        Patient["Patient / Electrodes"] -->|Analog Signals| AFEs["Cascaded ADS1293 AFEs<br/>(Master + 2x Slaves)"]
        AFEs -->|SPI / DRDYB 853 SPS| ESP32["ESP32 Microcontroller"]
        ESP32 -->|I2C| OLED["SSD1306 OLED Display"]
        ESP32 -->|VSPI| SD["MicroSD Card (Local BIN)"]
        ESP32 -->|ADC Pin 34| BATT["Battery Voltage Monitor"]
    end

    subgraph "Tier 2: Ingestion & Message Broker"
        ESP32 -->|"MQTT Publish (63-byte binary)<br/>Topic: ecg/device_01/data"| MQTT["HiveMQ MQTT Broker<br/>(broker.hivemq.com:1883)"]
    end

    subgraph "Tier 3: Cloud Platform & Processing (GCP VM)"
        MQTT -->|"MQTT Subscribe<br/>Topic: ecg/+/data"| Backend["Flask-SocketIO Backend<br/>(ecg_app_MDPM.py)"]
        Backend -->|"Batch Insert (1s interval)"| BQ_Raw[("BigQuery: ecg_raw_per_sample")]
        Backend -->|"Batch Insert (2s interval)"| BQ_Perf[("BigQuery: ecg_performance_metrics")]
        
        subgraph "ML & DSP Engine"
            Recorder["Buffer Accumulator (8000 samples)"] --> DSP["DSP Pipeline<br/>(IEC 60601-2-51 + Wavelet)"]
            DSP --> Feat["NeuroKit2 Delineation<br/>(96 Features across 12 Leads)"]
            Feat --> ML["XGBoost + K-Means Classifier"]
            ML --> BQ_Cls[("BigQuery: ecg_classifications")]
        end

        Backend --> Recorder
        Backend -->|"WebSocket (Socket.IO)"| WebClient["Web Dashboard<br/>(Chart.js + Real-Time Metrics)"]
    end
```

---

## 2. Component Roles & Responsibilities

### 2.1 Hardware Edge Device (`FINAL_APCLSD.ino`)
* **ECG Front-End**: Reads 8 physical differential ECG channels via three analog front-end (AFE) chips over high-speed SPI (HSPI at 1 MHz) synchronized by the `DRDYB` interrupt pin.
* **Lead Derivation**: Uses Einthoven and Goldberger electrophysiology laws to synthesize 4 augmented limb leads ($Lead\ III$, $aVR$, $aVL$, $aVF$) directly in firmware.
* **Operating Modes**:
  1. `SELECT_MODE`: Initializes an open Captive Access Point (`ESP32-ECG-AP`) serving a responsive HTTP menu at `192.168.4.1`.
  2. `AP_MODE`: Opens a local TCP server on port `8888` streaming 51-byte framed binary packets for bedside inspection.
  3. `SD_CARD_MODE`: Writes uncompressed 12-lead binary data blocks to FAT32 MicroSD cards with an embedded web-based file browser.
  4. `CLOUD_MODE`: Uses `WiFiManager` with automatic fallback to captive portal, synchronizes real-world POSIX time via NTP (`pool.ntp.org`), and publishes 63-byte framed telemetry packets to HiveMQ MQTT.

### 2.2 Ingestion & Processing Backend (`ecg_app_MDPM.py`)
* **Asynchronous Multi-Device Networking**: Built on Python `eventlet` green threads with non-blocking Paho-MQTT listener and WebSocket broadcasts.
* **Network Performance Telemetry**: Tracks real-time transmission latency, jitter (standard deviation over a rolling 100-packet window), and cumulative packet loss via packet sequence tracking.
* **Dynamic Device Discovery & Lifecycle**: Maintains state dictionaries per active device ID. Automatically expires inactive devices from the dashboard if no packets arrive within `DEVICE_TIMEOUT_S` (15 seconds).
* **Dual-Buffer BigQuery Ingestion**: Decouples network packet reception from BigQuery API round-trips using thread-safe deques (`live_buffer` and `perf_buffer`) flushed on 1-second and 2-second timers.

### 2.3 Web Dashboard & Visualization
* **Rendering Performance**: Implements a `requestAnimationFrame` render loop consuming an in-memory packet buffer to prevent UI freeze during high-frequency streaming ($853\text{ SPS}$).
* **Room-Based Socket Architecture**: Web clients join device-specific Socket.IO rooms (`join_room(device_id)`), ensuring only relevant waveform streams and metrics are forwarded to the observer.
* **Real-time Diagnostic Insights**: Visualizes Lead II waveform via Chart.js, 12-lead instantaneous raw ADC cards, network health metrics, and classification history.

---

## 3. End-to-End Recording & Analysis Sequence

The sequence below illustrates the life cycle of a diagnostic recording session:

```mermaid
sequenceDiagram
    autonumber
    actor Clinician as Clinician (Web Browser)
    participant Flask as Backend (ecg_app_MDPM.py)
    participant MQTT as HiveMQ Broker
    participant ESP32 as ESP32 Device
    participant BQ as Google BigQuery

    Clinician->>Flask: Select Device ("device_01")
    Flask->>Clinician: Subscribe to device_01 room
    ESP32->>MQTT: Publish 63-byte packet (ecg/device_01/data)
    MQTT->>Flask: on_message()
    Flask->>Clinician: Emit live_data & performance_update

    Clinician->>Flask: Click "Start Recording" (subject_id="Patient_A")
    Flask->>Flask: Initialize recording_id (UUID4), sample_counter=0
    Flask->>Clinician: Emit state_update (is_recording=True)

    loop Stream 8000 Samples (~9.37 seconds)
        ESP32->>MQTT: Publish packet
        MQTT->>Flask: on_message()
        Flask->>Flask: Accumulate to live_buffer & increment sample count
        alt Every 25 samples
            Flask->>Clinician: Emit progress_update (N / 8000)
        end
    end

    Note over Flask: Buffer reaches 8000 samples
    Flask->>Clinician: Emit state_update (is_recording=False, "Analyzing...")
    Flask->>Flask: Launch background task run_analysis_pipeline()
    
    Flask->>BQ: Query raw samples by recording_id
    BQ-->>Flask: 8000 raw samples dataframe
    Flask->>Flask: Execute DSP (Hampel, IEC 60601-2-51, Wavelet sym6)
    Flask->>Flask: Extract 96 morphological features (NeuroKit2)
    Flask->>Flask: Standardize & Predict (XGBoost + K-Means)
    Flask->>BQ: Insert classification result (ecg_classifications)
    Flask->>Clinician: Emit history_updated
    Clinician->>Flask: GET /api/history
    Flask-->>Clinician: Updated classification table
```

---

## 4. Multi-Device State Machine & Fault Recovery

```mermaid
stateDiagram-v2
    [*] --> DeviceDiscovered: Packet received from ecg/{device_id}/data
    DeviceDiscovered --> ActiveStreaming: Add to device_states & broadcast device_list_update
    
    ActiveStreaming --> ActiveStreaming: Ingest 63-byte metrics packet
    ActiveStreaming --> Recording: User triggers start_recording
    
    Recording --> Recording: Buffer samples to BigQuery queue
    Recording --> Analyzing: samples_collected >= 8000 OR stop_recording
    Analyzing --> ActiveStreaming: Analysis complete & BigQuery committed
    
    ActiveStreaming --> Offline: now - last_seen > 15 seconds
    Recording --> Offline: now - last_seen > 15 seconds
    Offline --> [*]: Removed by device_cleanup_task & broadcast device_list_update
```

