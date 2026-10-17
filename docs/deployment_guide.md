# Deployment & Configuration Guide

This guide details the deployment of the **12-Lead Smart Holter Backend** (`ecg_app_MDPM.py`) on Google Cloud Platform (GCP) Compute Engine and local developer workstations.

---

## 1. System Environment Variables Dictionary

The backend server is fully configurable via standard POSIX environment variables or a `.env` file:

| Environment Variable | Default Value | Required | Description |
| :--- | :--- | :--- | :--- |
| `GCP_PROJECT_ID` | `ecg12lead-project` | **Yes** | Google Cloud Project ID hosting BigQuery |
| `DATASET_ID` | `ecg_data` | No | BigQuery dataset name where tables are housed |
| `RAW_TABLE_ID` | `ecg_raw_per_sample` | No | Table name for raw 12-lead sample streams |
| `CLS_TABLE_ID` | `ecg_classifications`| No | Table name for arrhythmia classification outcomes |
| `PERF_TABLE_ID` | `ecg_performance_metrics`| No | Table name for latency and network QoS metrics |
| `MODEL_PATH` | `/home/jeffryfne/ecg_models` | **Yes** | Absolute directory path containing the `.joblib` model files |
| `FLASK_PORT` | `5000` | No | HTTP listening port for Flask and Socket.IO |
| `MQTT_BROKER` | `broker.hivemq.com` | No | Hostname of the target MQTT broker |
| `MQTT_PORT` | `1883` | No | TCP port of the MQTT broker |
| `MQTT_TOPIC` | `ecg/+/data` | No | Subscription topic (uses `+` single-level wildcard) |
| `SPS` | `853` | No | Hardware sampling rate in Hz (must match AFE decimation) |
| `BUFFER_SIZE` | `8000` | No | Number of samples required per recording session |
| `DEVICE_TIMEOUT_S` | `15` | No | Inactivity threshold before a device is purged from dashboard |

---

## 2. Production Deployment on Google Cloud Platform (Compute Engine)

### Step 1: GCP Project & Service Account Setup
1. Log in to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create or select your project (e.g., `ecg-holter-prod-42000`). Note your **Project ID**.
3. Enable the required APIs:
   ```bash
   gcloud services enable bigquery.googleapis.com compute.googleapis.com
   ```
4. Create a dedicated Service Account for the backend VM:
   * Navigate to **IAM & Admin > Service Accounts > Create Service Account**.
   * Name: `ecg-backend-sa`.
   * Grant the following IAM Roles:
     * **BigQuery Data Editor** (`roles/bigquery.dataEditor`): Allows writing sample buffers and classification rows.
     * **BigQuery Job User** (`roles/bigquery.jobUser`): Allows running query jobs.

---

### Step 2: Provision Compute Engine VM Instance
1. Go to **Compute Engine > VM instances > Create Instance**.
2. **Name**: `ecg-backend-vm`
3. **Region / Zone**: Choose the region closest to your devices (e.g., `asia-southeast2` for Jakarta).
4. **Machine Type**: `e2-medium` (2 vCPU, 4 GB RAM recommended for multi-device DSP processing).
5. **Boot Disk**: Ubuntu 22.04 LTS (x86/64), 20 GB Standard Persistent Disk.
6. **Identity and API Access**: Select the `ecg-backend-sa` service account created above.
7. **Firewall**: Check **Allow HTTP traffic** and **Allow HTTPS traffic**.
8. Click **Create**.

---

### Step 3: Configure VPC Firewall Rules
To allow external clients to access the web dashboard and connect to local telemetry ports:
1. Navigate to **VPC Network > Firewall > Create Firewall Rule**.
2. **Name**: `allow-ecg-platform`
3. **Targets**: All instances in the network
4. **Source IPv4 Ranges**: `0.0.0.0/0`
5. **Protocols and Ports**: Specified TCP ports `5000` (Flask) and `1883` (if hosting local MQTT).
6. Click **Create**.

---

### Step 4: System Dependencies & Environment Setup
Connect to your VM via SSH:
```bash
gcloud compute ssh ecg-backend-vm --zone=<YOUR_VM_ZONE>
```

Execute system updates and install Python build tools and linear algebra libraries:
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-pip python3-venv git libopenblas-base libatlas-base-dev
```

Create an application directory and virtual environment:
```bash
mkdir -p ~/ecg-app
cd ~/ecg-app
python3 -m venv venv
source venv/bin/activate
```

Clone the repository or copy project files into `~/ecg-app`:
```bash
# Verify directory contents
ls -la
# You should see: ecg_app_MDPM.py, requirements.txt
```

Install Python packages:
```bash
pip install -U pip wheel setuptools
pip install -r requirements.txt
```

---

### Step 5: Transfer Machine Learning Model Files
The backend requires three serialized model artifacts:
* `scaler.joblib` (Feature normalizer)
* `model_xgb_binary.joblib` (XGBoost binary classifier)
* `kmeans_abnormal_model.joblib` (K-Means abnormal cluster model)

On the VM, create the target models directory:
```bash
sudo mkdir -p /home/jeffryfne/ecg_models
sudo chown -R $USER:$USER /home/jeffryfne/ecg_models
```

From your **local workstation**, copy the model files via `gcloud compute scp`:
```bash
gcloud compute scp /local/path/to/scaler.joblib $USER@ecg-backend-vm:/home/jeffryfne/ecg_models/
gcloud compute scp /local/path/to/model_xgb_binary.joblib $USER@ecg-backend-vm:/home/jeffryfne/ecg_models/
gcloud compute scp /local/path/to/kmeans_abnormal_model.joblib $USER@ecg-backend-vm:/home/jeffryfne/ecg_models/
```

---

### Step 6: Environment Configuration File (`.env`)
Create `~/ecg-app/.env`:
```bash
nano ~/ecg-app/.env
```
Populate with your GCP project configuration:
```ini
GCP_PROJECT_ID=your-project-id-here
DATASET_ID=ecg_data
RAW_TABLE_ID=ecg_raw_per_sample
CLS_TABLE_ID=ecg_classifications
PERF_TABLE_ID=ecg_performance_metrics
MODEL_PATH=/home/jeffryfne/ecg_models
FLASK_PORT=5000
MQTT_BROKER=broker.hivemq.com
MQTT_PORT=1883
MQTT_TOPIC=ecg/+/data
```

---

### Step 7: Systemd Service Configuration
To ensure continuous operation with automated restarts on boot or unexpected process termination, register the app with `systemd`:

Create `/etc/systemd/system/ecg-app.service`:
```bash
sudo nano /etc/systemd/system/ecg-app.service
```

Paste the unit configuration (replace `ubuntu` with your VM's SSH username):
```ini
[Unit]
Description=12-Lead ECG Holter Backend Service
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/ecg-app
EnvironmentFile=/home/ubuntu/ecg-app/.env
ExecStart=/home/ubuntu/ecg-app/venv/bin/python3 ecg_app_MDPM.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Reload systemd daemon, enable, and start the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable ecg-app.service
sudo systemctl start ecg-app.service
```

Check status and follow live logs:
```bash
# Check current status
sudo systemctl status ecg-app.service

# Live log stream
sudo journalctl -u ecg-app.service -f
```

---

## 3. Local Developer Setup

For local testing or offline development:

1. **Clone the repository**:
   ```bash
   git clone https://github.com/your-username/12LeadSmartHolterDev.git
   cd 12LeadSmartHolterDev
   ```
2. **Create Python virtual environment**:
   ```bash
   python -m venv .venv
   # Windows PowerShell:
   .venv\Scripts\Activate.ps1
   # Linux/macOS:
   source .venv/bin/activate
   ```
3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```
4. **Authenticate Google Cloud SDK locally**:
   ```bash
   gcloud auth application-default login
   ```
5. **Launch Application**:
   ```bash
   python ecg_app_MDPM.py
   ```
6. Open your browser at `http://127.0.0.1:5000`.

