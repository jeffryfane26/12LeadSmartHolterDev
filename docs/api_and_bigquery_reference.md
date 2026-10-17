# Backend API & BigQuery Schema Reference

This document provides a technical reference for the HTTP REST endpoints, WebSocket events (Flask-SocketIO), and Google BigQuery table schemas utilized in `ecg_app_MDPM.py`.

---

## 1. HTTP REST Endpoints

### 1.1 Root Dashboard Route
* **Endpoint**: `GET /`
* **Description**: Serves the single-page HTML/JavaScript diagnostic dashboard.
* **Template Injections**:
  * `broker`: Hostname of the MQTT broker (e.g., `broker.hivemq.com`).
  * `port`: MQTT TCP port (e.g., `1883`).
  * `topic`: MQTT subscription wildcard topic (e.g., `ecg/+/data`).
* **Response**: `200 OK` (`text/html; charset=utf-8`)

---

### 1.2 Historical Classification Records
* **Endpoint**: `GET /api/history`
* **Description**: Queries the BigQuery classification log (`ecg_classifications`), returning the 50 most recent records ordered by timestamp descending.
* **Response**:
  * **Success (`200 OK`)**:
    ```json
    [
      {
        "timestamp": "2026-10-17T10:30:15.123456+00:00",
        "device_id": "device_01",
        "subject_id": "Patient_001",
        "recording_id": "c9a4e9b8-1f3a-4a81-9df2-6f913d8e5e78",
        "classification": "Normal"
      },
      {
        "timestamp": "2026-10-17T10:25:02.847112+00:00",
        "device_id": "device_01",
        "subject_id": "Patient_002",
        "recording_id": "5a2f8b1c-99d3-4a11-8e01-3c7d612e4b99",
        "classification": "Potential Slow Arrhytmia"
      }
    ]
    ```
  * **Error (`500 Internal Server Error`)**:
    ```json
    {
      "error": "403 Access Denied: BigQuery API error description"
    }
    ```

---

## 2. WebSocket Event Interface (Flask-SocketIO)

Real-time telemetry and client interactions operate over WebSockets via Socket.IO.

```
       Web Browser (Client)                       Flask Server (Backend)
                |                                           |
                |----------- connect ---------------------->|
                |<---------- device_list_update ------------|
                |                                           |
                |--- subscribe_to_device {device_id} ------>| (joins room: device_id)
                |<---------- state_update ------------------|
                |                                           |
                |<---------- live_data (853 Hz) ------------|
                |<---------- performance_update ------------|
                |<---------- global_performance_update -----|
                |                                           |
                |--- start_recording {device_id, subj_id} ->|
                |<---------- state_update ------------------|
                |<---------- progress_update (N/8000) ------|
                |                                           |
                |--- stop_recording {device_id} ----------->| (or buffer reaches 8000)
                |<---------- state_update ------------------|
                |                                           |
                |<---------- history_updated ---------------| (ML pipeline finishes)
                |                                           |
```

### 2.1 Client-to-Server Events

| Event | Payload | Description |
| :--- | :--- | :--- |
| `subscribe_to_device` | `{"device_id": "device_01"}` | Subscribes client socket to the room corresponding to `device_id`. Immediately replies with the latest `state_update`. |
| `start_recording` | `{"device_id": "device_01", "subject_id": "Subj_01"}` | Transitions device state to recording mode. Generates a unique UUID4 `recording_id` and resets sample count. |
| `stop_recording` | `{"device_id": "device_01"}` | Manually halts recording, updates status, and queues the background ML analysis pipeline task. |

---

### 2.2 Server-to-Client Events

#### `device_list_update`
* **Target**: Broadcast to all connected clients.
* **Trigger**: A new device publishes its first MQTT packet, or an inactive device times out ($> 15\text{s}$).
* **Payload**:
  ```json
  ["device_01", "device_02"]
  ```

#### `live_data`
* **Target**: Emitted to room `device_id`.
* **Frequency**: Up to $853\text{ packets/second}$.
* **Payload**:
  ```json
  {
    "device_id": "device_01",
    "lead_I": -12543,
    "lead_II": 48210,
    "lead_III": 60753,
    "avr": -17833,
    "avl": -36648,
    "avf": 54481,
    "v1": 1520,
    "v2": 8940,
    "v3": 24100,
    "v4": 31200,
    "v5": 28900,
    "v6": 19400
  }
  ```

#### `performance_update`
* **Target**: Emitted to room `device_id`.
* **Payload**:
  ```json
  {
    "device_id": "device_01",
    "packet_format": "Metrics (63-byte)",
    "latency_ms": 42.15,
    "avg_latency_ms": 38.60,
    "jitter_ms": 3.82,
    "lost_packets": 2,
    "total_received": 1845,
    "packet_loss_pct": 0.11
  }
  ```

#### `global_performance_update`
* **Target**: Broadcast to all clients every 2 seconds.
* **Payload**:
  ```json
  [
    {
      "device_id": "device_01",
      "status_message": "Recording... (4500/8000)",
      "packet_format": "Metrics (63-byte)",
      "avg_latency_ms": 38.60,
      "jitter_ms": 3.82,
      "packet_loss_pct": 0.11
    }
  ]
  ```

#### `state_update`
* **Target**: Emitted to room `device_id` and specific socket on subscription.
* **Payload**:
  ```json
  {
    "device_id": "device_01",
    "is_recording": true,
    "status_message": "Recording...",
    "recording_id": "7b08d29c-6458-48b0-a359-59eb4f7574b2",
    "subject_id": "Subj_01",
    "samples_collected": 350
  }
  ```

#### `progress_update`
* **Target**: Emitted to room `device_id` every 25 samples during active recording.
* **Payload**:
  ```json
  {
    "device_id": "device_01",
    "current": 4250,
    "total": 8000
  }
  ```

#### `history_updated`
* **Target**: Broadcast to all clients.
* **Trigger**: Background machine learning pipeline finishes inference and writes result to BigQuery. Instructs the client browser to trigger `fetchHistory()`.
* **Payload**: `null`

---

## 3. Google BigQuery Data Storage Architecture

All data tables reside in the dataset designated by `DATASET_ID` (default: `ecg_data`) in GCP Project `GCP_PROJECT_ID`. Tables are automatically created upon application startup if they do not exist (`ensure_bq_tables()`).

### 3.1 Raw ECG Samples Table (`ecg_raw_per_sample`)
Stores high-resolution individual sample vectors during recording sessions:

| Column Name | BigQuery Data Type | Mode | Description |
| :--- | :--- | :--- | :--- |
| `timestamp` | `TIMESTAMP` | `REQUIRED` | UTC ingestion timestamp |
| `device_id` | `STRING` | `REQUIRED` | Unique device identifier (e.g., `device_01`) |
| `recording_id` | `STRING` | `REQUIRED` | UUID4 associated with recording session |
| `subject_id` | `STRING` | `REQUIRED` | Clinical subject or patient identifier |
| `lead_I` | `INTEGER` | `NULLABLE` | Limb Lead I raw ADC count |
| `lead_II` | `INTEGER` | `NULLABLE` | Limb Lead II raw ADC count |
| `lead_III` | `INTEGER` | `NULLABLE` | Limb Lead III raw ADC count |
| `avr` | `INTEGER` | `NULLABLE` | Augmented Lead aVR raw ADC count |
| `avl` | `INTEGER` | `NULLABLE` | Augmented Lead aVL raw ADC count |
| `avf` | `INTEGER` | `NULLABLE` | Augmented Lead aVF raw ADC count |
| `v1` | `INTEGER` | `NULLABLE` | Precordial Lead V1 raw ADC count |
| `v2` | `INTEGER` | `NULLABLE` | Precordial Lead V2 raw ADC count |
| `v3` | `INTEGER` | `NULLABLE` | Precordial Lead V3 raw ADC count |
| `v4` | `INTEGER` | `NULLABLE` | Precordial Lead V4 raw ADC count |
| `v5` | `INTEGER` | `NULLABLE` | Precordial Lead V5 raw ADC count |
| `v6` | `INTEGER` | `NULLABLE` | Precordial Lead V6 raw ADC count |

---

### 3.2 Arrhythmia Classification Table (`ecg_classifications`)
Stores the diagnostic output of the machine learning inference pipeline:

| Column Name | BigQuery Data Type | Mode | Description |
| :--- | :--- | :--- | :--- |
| `timestamp` | `TIMESTAMP` | `REQUIRED` | UTC inference timestamp |
| `device_id` | `STRING` | `REQUIRED` | Originating device identifier |
| `recording_id` | `STRING` | `REQUIRED` | Recording session UUID4 |
| `subject_id` | `STRING` | `REQUIRED` | Patient identifier |
| `classification` | `STRING` | `NULLABLE` | Diagnostic label (`Normal`, `Potential Slow Arrhytmia`, `Potential Fast Arrhytmia`, or error string) |

---

### 3.3 Network Performance Table (`ecg_performance_metrics`)
Stores transmission health and QoS telemetry:

| Column Name | BigQuery Data Type | Mode | Description |
| :--- | :--- | :--- | :--- |
| `timestamp` | `TIMESTAMP` | `REQUIRED` | UTC receipt timestamp |
| `device_id` | `STRING` | `REQUIRED` | Originating device identifier |
| `recording_id` | `STRING` | `NULLABLE` | Active recording session UUID4 (if recording) |
| `packet_counter` | `INTEGER` | `NULLABLE` | Firmware sequence counter |
| `latency_ms` | `FLOAT` | `NULLABLE` | One-way transit latency in milliseconds |
| `jitter_ms` | `FLOAT` | `NULLABLE` | Rolling latency jitter (standard deviation) |
| `lost_packets_cumulative` | `INTEGER` | `NULLABLE` | Cumulative lost packets count |
| `packet_loss_pct_cumulative`| `FLOAT` | `NULLABLE` | Cumulative packet loss percentage |

