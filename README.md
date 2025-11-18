Panduan Deploy Aplikasi 12-Lead ECG (ESP32 & GCP)

Sistem ini adalah pemantau EKG 12-sadapan real-time dengan dua komponen:

Perangkat Keras (FINAL_APCLSD.ino): Firmware ESP32 untuk akuisisi data EKG portabel (Holter).

Backend & Dashboard (ecg_app_MDPM.py): Aplikasi web Python (Flask, SocketIO) untuk penerimaan, pemrosesan, analisis ML, penyimpanan (BigQuery), dan visualisasi data.

Dokumen ini merinci arsitektur, penjelasan kode, dan tutorial deployment server.

Daftar Isi

Arsitektur Sistem

Penjelasan Kode

A. Firmware ESP32 (FINAL_APCLSD.ino)

B. Backend Aplikasi Web (ecg_app_MDPM.py)

Inti Kesinambungan: Bagaimana Keduanya Terhubung?

Tutorial: Setup Backend di Google Cloud Platform (GCP) VM

Langkah 0: Prasyarat

Langkah 1: Buat Proyek GCP & Service Account

Langkah 2: Buat VM Instance (Compute Engine)

Langkah 3: Konfigurasi Aturan Firewall

Langkah 4: Hubungkan ke VM via SSH

Langkah 5: Instalasi Software di VM

Langkah 6: Upload Kode & Model ML Anda

Langkah 7: Konfigurasi Environment Variables

Langkah 8: Jalankan Aplikasi sebagai Service (systemctl)

Langkah 9: Akses Aplikasi Anda

Cara Penggunaan Sistem (Alur Kerja)

1. Arsitektur Sistem

Alur data dalam sistem ini adalah sebagai berikut:

ESP32 (FINAL_APCLSD.ino) membaca data 12-lead EKG dari sensor.

Dalam Cloud Mode, ESP32 terhubung ke WiFi dan melakukan streaming data mentah dalam format biner (63-byte) ke broker MQTT publik (broker.hivemq.com).

GCP VM (ecg_app_MDPM.py) di Google Cloud berlangganan (subscribe) ke topik MQTT yang sama di broker HiveMQ.

Saat data diterima, aplikasi Python:

Mem-parsing data biner.

Menyimpan data mentah ke Google BigQuery.

Menyebarkan data (via SocketIO) ke dasbor web untuk visualisasi real-time.

Saat perekaman diinisiasi dari web, data di-buffer.

Setelah buffer penuh, data dianalisis menggunakan model Machine Learning yang sudah di-load.

Hasil klasifikasi disimpan ke Google BigQuery.

Pengguna mengakses http://[IP_VM_ANDA]:5000 untuk melihat dasbor live, memulai perekaman, dan melihat riwayat klasifikasi.

2. Penjelasan Kode

A. Firmware ESP32 (FINAL_APCLSD.ino)

Firmware ini mengelola perangkat keras. Kode ini memiliki 4 mode operasi utama:

SELECT_MODE: Mode awal saat perangkat dinyalakan. ESP32 membuat Access Point (AP) WiFi bernama ESP32-ECG-AP. Pengguna dapat terhubung ke WiFi ini dan membuka IP 192.168.4.1 (biasanya) di browser untuk memilih mode selanjutnya.

AP_MODE: Mode Access Point lokal. Data EKG di-streaming langsung ke klien yang terhubung melalui server TCP. Berguna untuk debugging lokal, tidak terhubung ke backend cloud.

SD_CARD_MODE: Mode Perekaman. Data EKG disimpan secara offline ke file .bin di SD Card. Berguna untuk perekaman jangka panjang tanpa koneksi internet.

CLOUD_MODE: Mode Kunci untuk sistem ini.

Menggunakan WiFiManager, ESP32 akan secara otomatis membuka portal konfigurasi jika belum terhubung ke WiFi. Ini memungkinkan pengguna untuk memasukkan kredensial WiFi (SSID & Password) melalui HP atau laptop tanpa harus mengubah kode.

Setelah terhubung ke internet, perangkat menyinkronkan waktu menggunakan NTP (Network Time Protocol).

Perangkat terhubung ke broker MQTT (broker.hivemq.com:1883).

Perangkat mulai membaca sensor EKG dan mengirimkan data secara kontinu ke topik MQTT (ecg/device_01/data) dalam format biner 63-byte yang terstruktur.

Format Paket Data Biner (63-byte):
Setiap data yang dikirim oleh ESP32 dalam CLOUD_MODE memiliki struktur:

Byte 0: STX (0x02) - Penanda awal paket

Byte 1-8 (8 bytes): Timestamp (dalam microsecond)

Byte 9-12 (4 bytes): Packet Counter

Byte 13-60 (48 bytes): Data 12-Lead EKG (12 lead x @ 4 bytes int32_t)

Byte 61 (1 byte): Checksum (untuk validasi data)

Byte 62: ETX (0x03) - Penanda akhir paket

B. Backend Aplikasi Web (ecg_app_MDPM.py)

Ini adalah server pusat yang menerima, mengolah, dan menyajikan data.

Teknologi: Python, Flask (web server), SocketIO (komunikasi real-time), Paho-MQTT (koneksi broker), dan Google BigQuery (database).

Listener MQTT: Fungsi on_message menangani:

Berlangganan (subscribe) ke topik ecg/+/data. Tanda + adalah wildcard untuk menerima data dari semua perangkat (misal, ecg/device_01/data).

Memvalidasi paket 63-byte yang masuk (memeriksa STX, ETX, dan Checksum).

Mem-parsing data biner menggunakan struct.unpack('<QI12i', ...) agar sesuai dengan struktur ESP32.

Penyimpanan Data: Data mentah dan metrik performa di-buffer dan diunggah ke tabel BigQuery (ecg_raw_per_sample dan ecg_performance_metrics).

Dasbor Live: Data yang telah diparsing dikirim ke dasbor web melalui SocketIO (socketio.emit('live_data', ...)) untuk menggambar grafik real-time.

Analisis ML:

Saat perekaman dimulai dari web, server mengumpulkan BUFFER_SIZE (8000 sampel).

Setelah 8000 sampel terkumpul, perekaman otomatis berhenti.

Fungsi run_analysis_pipeline dipicu. Fungsi ini mengambil data dari BigQuery, membersihkannya (filter DSP, despike, wavelet), mengekstrak fitur (neurokit2), dan mengklasifikasikannya (Normal, Aritmia Lambat, Aritmia Cepat) menggunakan model .joblib yang telah di-load.

Hasil klasifikasi disimpan di tabel BigQuery (ecg_classifications).

3. Inti Kesinambungan: Bagaimana Keduanya Terhubung?

Kesinambungan (synergy) antara kedua kode dicapai melalui MQTT:

Broker yang Sama: Keduanya (ESP32 dan server Python) terhubung ke broker MQTT yang sama: broker.hivemq.com di port 1883.

Topik yang Sinkron:

ESP32 mempublikasikan (Publish) data ke topik spesifik: ecg/device_01/data.

Server Python berlangganan (Subscribe) ke topik wildcard: ecg/+/data. Ini memungkinkan server menerima pesan dari perangkat apa pun (misal, ecg/device_01/data, ecg/device_02/data, dst.).

Format Data yang Sesuai (Paling Penting):

ESP32 mengemas (pack) 12 lead data, timestamp, dan counter ke dalam paket biner 63-byte.

Server Python membuka kemasan (unpack) paket biner 63-byte tersebut dengan urutan dan tipe data yang sama persis.

ESP32 berfungsi sebagai Produsen Data, dan Aplikasi Python berfungsi sebagai Konsumen, Pengolah, dan Presenter Data.

4. Tutorial: Setup Backend di Google Cloud Platform (GCP) VM

Panduan ini merinci langkah-langkah untuk mendeploy file ecg_app_MDPM.py di server Google Cloud (GCP) agar dapat diakses melalui IP publik.

Langkah 0: Prasyarat

Kebutuhan untuk setup backend:

Akun Google Cloud (GCP).

File ecg_app_MDPM.py.

File requirements.txt.

PENTING: File model ML (.joblib) yang dirujuk oleh kode (scaler.joblib, model_xgb_binary.joblib, kmeans_abnormal_model.joblib). Server tidak akan berfungsi tanpa model-model ini.

Langkah 1: Buat Proyek GCP & Service Account

Buka Konsol GCP.

Buat Proyek baru (misal: "proyek-ecg-saya"). Catat Project ID Anda (misal: proyek-ecg-saya-12345).

Aplikasi Python memerlukan izin untuk menulis ke BigQuery. Buat Service Account.

Buka IAM & Admin > Service Accounts.

Klik Create Service Account.

Beri nama (misal: "vm-bigquery-editor").

Klik Create and Continue.

Di bagian Grant this service account access to project, tambahkan dua peran (Roles):

BigQuery Data Editor

BigQuery Job User

Klik Done.

Buka APIs & Services > Library. Cari dan Enable (Aktifkan) BigQuery API.

Langkah 2: Buat VM Instance (Compute Engine)

Buka Compute Engine > VM instances.

Klik Create Instance.

Nama: Beri nama VM (misal: "ecg-backend-server").

Region & Zone: Pilih region terdekat (misal: asia-southeast1 - Jakarta).

Machine Type: e2-medium (2 vCPU, 4 GB memory) direkomendasikan.

Boot Disk: Klik Change. Pilih Ubuntu versi 22.04 LTS.

Service Account: Di bagian Identity and API access, ubah Service account ke yang dibuat di Langkah 1 (misal: "vm-bigquery-editor@..."). Ini langkah kritis.

Firewall: Centang Allow HTTP traffic dan Allow HTTPS traffic.

Klik Create.

Langkah 3: Konfigurasi Aturan Firewall

Buka port 5000 (Flask) dan 1883 (MQTT) agar dapat diakses dari internet.

Buka VPC Network > Firewall.

Klik Create Firewall Rule.

Nama: allow-ecg-app

Targets: All instances in the network

Source IPv4 ranges: 0.0.0.0/0 (Izinkan dari mana saja).

Protocols and ports:

Pilih Specified protocols and ports.

Centang TCP dan masukkan port: 5000, 1883

Klik Create.

Langkah 4: Hubungkan ke VM via SSH

Kembali ke Compute Engine > VM instances.

Di baris VM Anda, klik SSH untuk membuka terminal browser.

Setelah terhubung, catat username Anda. Jalankan perintah:

whoami


(Misalkan outputnya adalah user_anda. Catat ini.)

Langkah 5: Instalasi Software di VM

Semua perintah berikut dijalankan di dalam terminal SSH VM.

Update sistem:

sudo apt update && sudo apt upgrade -y


Instal Python dan dependensi sistem:

sudo apt install python3-pip python3-venv -y
sudo apt install libopenblas-base libatlas-base-dev -y


Buat lingkungan untuk aplikasi:

# Buat direktori 'ecg-app' dan masuk ke dalamnya
mkdir ecg-app && cd ecg-app

# Buat virtual environment bernama 'venv'
python3 -m venv venv

# Aktifkan virtual environment
source venv/bin/activate


(Prompt terminal sekarang akan diawali dengan (venv)).

Instal semua paket Python:

Buat file requirements.txt di dalam VM:

(venv) $ nano requirements.txt


Salin dan tempel (paste) isi file requirements.txt Anda ke editor nano.

Simpan file: Tekan Ctrl+O, Enter, lalu Ctrl+X.

Instal paket dari file tersebut:

(venv) $ pip install -U pip
(venv) $ pip install -r requirements.txt


Langkah 6: Upload Kode & Model ML Anda

Buat file Python:

(venv) $ nano ecg_app_MDPM.py


Salin dan tempel (paste) seluruh isi kode ecg_app_MDPM.py Anda.

Simpan file: Tekan Ctrl+O, Enter, lalu Ctrl+X.

Buat direktori untuk Model ML (SANGAT PENTING!):

Kode merujuk ke /home/jeffryfne/ecg_models. Direktori ini harus dibuat.

(venv) $ sudo mkdir -p /home/jeffryfne/ecg_models

# Ganti 'user_anda' dengan username Anda dari 'whoami'
(venv) $ sudo chown user_anda:user_anda /home/jeffryfne/ecg_models


Upload Model ML Anda:

Buka terminal BARU di komputer LOKAL Anda.

Pastikan Google Cloud SDK terinstal di komputer lokal Anda.

Jalankan gcloud compute scp untuk setiap file model. Ganti user_anda dengan username VM Anda dan ecg-backend-server dengan nama VM Anda.

# Format: gcloud compute scp [PATH_LOKAL] [USER_VM]@[NAMA_VM]:[PATH_TUJUAN_VM]

gcloud compute scp /path/lokal/ke/scaler.joblib user_anda@ecg-backend-server:/home/jeffryfne/ecg_models/scaler.joblib

gcloud compute scp /path/lokal/ke/model_xgb_binary.joblib user_anda@ecg-backend-server:/home/jeffryfne/ecg_models/model_xgb_binary.joblib

gcloud compute scp /path/lokal/ke/kmeans_abnormal_model.joblib user_anda@ecg-backend-server:/home/jeffryfne/ecg_models/kmeans_abnormal_model.joblib


Kembali ke terminal SSH VM setelah selesai.

Langkah 7: Konfigurasi Environment Variables

systemctl memerlukan file environment khusus.

Kembali ke terminal SSH VM Anda (masih di ~/ecg-app dan (venv) aktif).

Buat file .env di dalam direktori aplikasi:

(venv) $ nano .env


Tempel (paste) konten berikut. Ganti proyek-id-anda-12345 dengan Project ID GCP Anda yang sebenarnya.

GCP_PROJECT_ID=proyek-id-anda-12345
DATASET_ID=ecg_data
MODEL_PATH=/home/jeffryfne/ecg_models


Simpan file: Tekan Ctrl+O, Enter, lalu Ctrl+X.

Nonaktifkan virtual environment:

(venv) $ deactivate


Langkah 8: Jalankan Aplikasi sebagai Service (systemctl)

Langkah ini akan menjalankan aplikasi sebagai service latar belakang yang permanen.

Buat file Service Unit:

sudo nano /etc/systemd/system/ecg-app.service


Tempel (paste) konfigurasi berikut. PENTING: Ganti user_anda di 4 tempat dengan username VM Anda (dari whoami di Langkah 4).

[Unit]
Description=ECG 12-Lead Application Service
After=network.target

[Service]
# Ganti 'user_anda' dengan username Anda
User=user_anda

# Ganti 'user_anda' dengan username Anda
WorkingDirectory=/home/user_anda/ecg-app

# Ganti 'user_anda' dengan username Anda
EnvironmentFile=/home/user_anda/ecg-app/.env

# Path lengkap ke Python di dalam venv
# Ganti 'user_anda' dengan username Anda
ExecStart=/home/user_anda/ecg-app/venv/bin/python3 ecg_app_MDPM.py

Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target


Simpan file: Tekan Ctrl+O, Enter, lalu Ctrl+X.

Reload, Aktifkan, dan Jalankan Service:

# Muat ulang konfigurasi systemd
sudo systemctl daemon-reload

# Aktifkan service agar berjalan saat boot
sudo systemctl enable ecg-app.service

# Jalankan service sekarang
sudo systemctl start ecg-app.service


Periksa Status Service:

sudo systemctl status ecg-app.service


Jika output active (running) berwarna hijau, service berhasil berjalan.

Jika failed, gunakan sudo journalctl -u ecg-app.service untuk melihat log error.

Melihat Log Aplikasi Secara Real-time:

sudo journalctl -u ecg-app.service -f --no-pager


(Tekan Ctrl+C untuk keluar dari log).

Langkah 9: Akses Aplikasi Anda

Kembali ke halaman Compute Engine di konsol GCP.

Temukan IP Eksternal (External IP) untuk VM Anda.

Buka browser web dan masukkan alamat: http://[IP_EKSTERNAL_ANDA]:5000 (ganti [IP_EKSTERNAL_ANDA] dengan IP VM Anda).

Dasbor "ECG Live Platform" akan muncul.

5. Cara Penggunaan Sistem (Alur Kerja)

Alur kerja penggunaan sistem adalah sebagai berikut:

Nyalakan Perangkat ESP32.

Jika ini penggunaan pertama, ESP32 akan masuk ke SELECT_MODE.

Hubungkan HP/Laptop ke WiFi ESP32-ECG-AP (password: password123).

Buka browser dan pergi ke http://192.168.4.1.

Pilih "Start Cloud Upload Mode".

ESP32 akan restart dan WiFiManager akan aktif. Hubungkan kembali ke WiFi yang dipancarkan ESP32 (jika diminta).

Sebuah portal akan muncul. Pilih jaringan WiFi lokal (yang memiliki akses internet) dan masukkan passwordnya.

Setelah terhubung, ESP32 akan mengingat kredensial ini dan otomatis terhubung ke internet & MQTT setiap kali dinyalakan, lalu mulai streaming data.

Buka Dasbor Web GCP (http://[IP_EKSTERNAL_VM]:5000).

Tunggu beberapa detik. ID perangkat (misal, device_01) akan muncul di dropdown "Pilih Device".

Pilih device. Grafik live waveform dan live values akan langsung bergerak.

Untuk melakukan analisis, masukkan ID Subjek (misal: "pasien_001").

Klik "Mulai Perekaman".

Tunggu hingga progress bar mencapai 8000/8000 sampel.

Setelah selesai, server akan otomatis memproses data. Hasil klasifikasi akan muncul di tabel "Riwayat Klasifikasi".
