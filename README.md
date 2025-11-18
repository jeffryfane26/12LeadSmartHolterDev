# 12LeadSmartHolterDev

Perangkat Keras (FINAL_APCLSD.ino): Sebuah firmware untuk ESP32 yang berfungsi sebagai alat akuisisi data EKG portabel (Holter).

Backend & Dashboard (ecg_app_MDPM.py): Sebuah aplikasi web Python (Flask, SocketIO) yang dirancang untuk berjalan di server cloud (GCP), yang menerima, memproses, menganalisis (menggunakan ML), menyimpan (ke BigQuery), dan memvisualisasikan data EKG secara live.

Dokumen ini adalah panduan lengkap Anda untuk memahami, men-deploy, dan menggunakan keseluruhan sistem.

Daftar Isi
 - Arsitektur Sistem
 - Penjelasan Kode
    A. Firmware ESP32 (FINAL_APCLSD.ino)
    B. Backend Aplikasi Web (ecg_app_MDPM.py
 - Tutorial: Setup Backend di Google Cloud Platform (GCP) VM
    Langkah 0: Prasyarat
    Langkah 1: Buat Proyek GCP & Service Account
    Langkah 2: Buat VM Instance (Compute Engine)
    Langkah 3: Konfigurasi Aturan Firewall
    Langkah 4: Hubungkan ke VM via SSH
    Langkah 5: Instalasi Software di VM
    Langkah 6: Upload Kode & Model ML Anda
    Langkah 7: Konfigurasi Environment Variables
    Langkah 8: Jalankan Aplikasi Backend
    Langkah 9: Akses Aplikasi Anda
  - Cara Penggunaan Sistem (Alur Kerja)
    
1. Arsitektur Sistem
Alur data dalam sistem ini adalah sebagai berikut:
  - ESP32 (FINAL_APCLSD.ino) membaca data 12-lead EKG dari sensor.
  - Dalam Cloud Mode, ESP32 terhubung ke WiFi dan melakukan streaming data mentah dalam format biner (63-byte) ke broker MQTT publik (broker.hivemq.com).
  - GCP VM (ecg_app_MDPM.py) di Google Cloud terus mendengarkan (subscribe) topik MQTT yang sama di broker HiveMQ.
    Saat data diterima, aplikasi Python:
      Mem-parsing data biner.
      Menyimpan data mentah ke Google BigQuery.
      Menyebarkan data (via SocketIO) ke dasbor web untuk visualisasi real-time.
      Saat perekaman diinisiasi dari web, data di-buffer.
      Setelah buffer penuh, data dianalisis menggunakan model Machine Learning yang sudah di-load.
      Hasil klasifikasi disimpan ke Google BigQuery.
  - Pengguna mengakses http://[IP_VM_ANDA]:5000 untuk melihat dasbor live, memulai perekaman, dan melihat riwayat klasifikasi.

2. Penjelasan Kode
A. Firmware ESP32 (FINAL_APCLSD.ino)
Ini adalah otak dari perangkat keras. Kode ini sangat kompleks dan memiliki 4 mode operasi utama:
  SELECT_MODE: Mode awal saat perangkat dinyalakan. ESP32 membuat Access Point (AP) WiFi bernama ESP32-ECG-AP. Pengguna dapat terhubung ke WiFi ini dan membuka IP 192.168.4.1 (biasanya) di browser untuk memilih mode selanjutnya.
  AP_MODE: Mode Access Point lokal. Data EKG di-streaming langsung ke klien yang terhubung melalui server TCP. Ini berguna untuk debugging lokal, tetapi tidak terhubung ke backend cloud.
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
Teknologi: Python, Flask (untuk web server), SocketIO (untuk komunikasi real-time ke browser), Paho-MQTT (untuk terhubung ke broker), dan Google BigQuery (untuk database).
Listener MQTT: Fungsi on_message adalah intinya. Fungsi ini:
  Berlangganan (subscribe) ke topik ecg/+/data. Tanda + adalah wildcard, artinya ia akan menerima data dari topik apapun yang cocok, termasuk ecg/device_01/data yang dikirim ESP32.
Memvalidasi paket 63-byte yang masuk (memeriksa STX, ETX, dan Checksum).
Mem-parsing data biner menggunakan struct.unpack('<QI12i', ...) yang secara sempurna sesuai dengan struktur yang dikirim ESP32.
Penyimpanan Data: Data mentah dan metrik performa (seperti latensi) di-buffer dan diunggah ke tabel BigQuery (ecg_raw_per_sample dan ecg_performance_metrics).
Dasbor Live: Data yang telah diparsing langsung dikirim ke dasbor web melalui SocketIO (socketio.emit('live_data', ...)), yang kemudian menggambar grafik real-time di browser pengguna.
Analisis ML:
  Saat pengguna menekan "Mulai Perekaman" di web, server mulai mengumpulkan sampel data hingga BUFFER_SIZE (8000 sampel).
  Setelah 8000 sampel terkumpul, perekaman otomatis berhenti.
  Fungsi run_analysis_pipeline dipicu. Fungsi ini mengambil data yang baru direkam dari BigQuery, membersihkannya (menggunakan filter DSP, despike, wavelet), mengekstrak fitur (menggunakan neurokit2), dan akhirnya mengklasifikasikannya (Normal, Aritmia     Lambat, Aritmia Cepat) menggunakan model ML (.joblib) yang telah di-load.
Hasil klasifikasi disimpan di tabel BigQuery (ecg_classifications).

3. Tutorial: Setup Backend di Google Cloud Platform (GCP) VM

Ini adalah panduan langkah-demi-langkah untuk mendeploy file ecg_app_MDPM.py di server Google Cloud (GCP) agar dapat diakses dari mana saja melalui IP publik.
Langkah 0: Prasyarat
Anda memiliki akun Google Cloud (GCP).
Anda memiliki file ecg_app_MDPM.py.
PENTING: Anda memiliki file model ML (.joblib) yang disebutkan dalam kode (scaler.joblib, model_xgb_binary.joblib, kmeans_abnormal_model.joblib). Server tidak akan berfungsi tanpa model-model ini.

Langkah 1: Buat Proyek GCP & Service Account
Buka Konsol GCP.
Buat Proyek baru (misal: "proyek-ecg-saya"). Catat Project ID Anda (misal: proyek-ecg-saya-12345).
Aplikasi Python Anda perlu izin untuk menulis ke BigQuery. Kita akan menggunakan Service Account.
Buka IAM & Admin > Service Accounts.
Klik Create Service Account.
Beri nama (misal: "vm-bigquery-editor").
Klik Create and Continue.
Di bagian Grant this service account access to project, tambahkan dua peran (Roles):
BigQuery Data Editor
BigQuery Job User
Klik Done. Anda tidak perlu membuat key (kunci JSON), karena kita akan melampirkan service account ini langsung ke VM.
Buka APIs & Services > Library. Cari dan Enable (Aktifkan) BigQuery API.

Langkah 2: Buat VM Instance (Compute Engine)
Buka Compute Engine > VM instances.
Klik Create Instance.
Nama: Beri nama VM Anda (misal: "ecg-backend-server").
Region & Zone: Pilih region yang dekat dengan Anda (misal: asia-southeast1 - Jakarta).
Machine Type: Untuk aplikasi ini, e2-medium (2 vCPU, 4 GB memory) adalah awal yang baik. e2-small mungkin cukup, tetapi pemrosesan ML bisa jadi lambat.
Boot Disk: Klik Change. Pilih Ubuntu versi 22.04 LTS. Ukuran 10 GB cukup.
Service Account: Di bagian Identity and API access, ubah Service account dari "Compute Engine default" menjadi Service Account yang Anda buat di Langkah 1 (misal: "vm-bigquery-editor@..."). Ini adalah langkah kritis.
Firewall: Centang Allow HTTP traffic dan Allow HTTPS traffic.
Klik Create. Tunggu beberapa menit hingga VM dibuat.

Langkah 3: Konfigurasi Aturan Firewall
VM Anda perlu membuka port 5000 (untuk Flask) dan port 1883 (untuk MQTT) agar bisa diakses dari internet.
Buka VPC Network > Firewall.
Klik Create Firewall Rule.
Nama: allow-ecg-app
Targets: All instances in the network (atau gunakan target tag jika Anda ingin lebih spesifik).
Source IPv4 ranges: 0.0.0.0/0 (Artinya "izinkan dari mana saja").
Protocols and ports:
Pilih Specified protocols and ports.
Centang TCP dan masukkan port: 5000, 1883
Klik Create.

Langkah 4: Hubungkan ke VM via SSH
Kembali ke Compute Engine > VM instances.
Di baris VM Anda, klik SSH. Ini akan membuka terminal browser yang terhubung ke VM Anda.

Langkah 5: Instalasi Software di VM
Semua perintah berikut dijalankan di dalam terminal SSH VM Anda.
Update sistem:
sudo apt update
sudo apt upgrade -y
Instal Python dan dependensi sistem:
sudo apt install python3-pip python3-venv -y
sudo apt install libopenblas-base libatlas-base-dev -y 
Buat lingkungan untuk aplikasi:
mkdir ecg-app
cd ecg-app
python3 -m venv venv
source venv/bin/activate
(Sekarang Anda akan melihat (venv) di depan prompt terminal Anda).
Instal semua paket Python yang diperlukan:
pip install -U pip
pip install flask flask-socketio eventlet paho-mqtt google-cloud-bigquery numpy pandas scipy pywavelets joblib neurokit2

Langkah 6: Upload Kode & Model ML Anda
Upload file Python:
Di terminal SSH, jalankan: nano ecg_app_MDPM.py
Salin (copy) seluruh isi kode ecg_app_MDPM.py Anda.
Tempel (paste) ke dalam editor nano.
Simpan file: Tekan Ctrl+O, Enter, lalu Ctrl+X untuk keluar.
atau anda bisa upload kode ke VM
Upload Model ML Anda (SANGAT PENTING!):
Kode Anda mencari model di /home/jeffryfne/ecg_models. Kita harus mengubah ini atau mengunggah file ke sana. Mari kita ikuti jalur yang ada di kode.
Buat direktori (gantilah jeffryfne dengan username Anda di VM. Anda bisa cek dengan perintah whoami):
# Cek username Anda saat ini
whoami
# (Misalkan outputnya "user_anda")
# Buat direktori. Ganti 'jeffryfne' dengan 'user_anda'
sudo mkdir -p /home/jeffryfne/ecg_models
sudo chown $(whoami):$(whoami) /home/jeffryfne/ecg_models
Anda harus meng-upload file model Anda (scaler.joblib, model_xgb_binary.joblib, kmeans_abnormal_model.joblib) ke direktori ini.
Cara termudah adalah menggunakan gcloud compute scp dari komputer lokal Anda (bukan dari VM):
# Buka terminal BARU di komputer LOKAL Anda
# Pastikan Anda punya gcloud SDK
gcloud compute scp /path/lokal/ke/model.joblib user_anda@ecg-backend-server:/home/jeffryfne/ecg_models/model.joblib
# Ulangi untuk semua 3 file model
gcloud compute scp /path/lokal/ke/scaler.joblib user_anda@ecg-backend-server:/home/jeffryfne/ecg_models/scaler.joblib
gcloud compute scp /path/lokal/ke/model_xgb_binary.joblib user_anda@ecg-backend-server:/home/jeffryfne/ecg_models/model_xgb_binary.joblib
gcloud compute scp /path/lokal/ke/kmeans_abnormal_model.joblib user_anda@ecg-backend-server:/home/jeffryfne/ecg_models/kmeans_abnormal_model.joblib

Langkah 7: Konfigurasi Environment Variables
Aplikasi Anda menggunakan os.getenv untuk konfigurasi penting. Kita harus mengaturnya.
Kembali ke terminal SSH VM Anda. Pastikan Anda masih di ~/ecg-app dan (venv) aktif.
Set variabel lingkungan. Ganti proyek-id-anda-12345 dengan Project ID GCP Anda yang sebenarnya.
export GCP_PROJECT_ID="proyek-id-anda-12345"
export DATASET_ID="ecg_data"
export MODEL_PATH="/home/jeffryfne/ecg_models" 
Catatan: Kode Anda (ensure_bq_tables) akan mencoba membuat dataset ecg_data jika belum ada, jadi Anda tidak perlu membuatnya manual di BigQuery.

Langkah 8: Jalankan Aplikasi Backend
Sekarang Anda siap menjalankan server!
Untuk Uji Coba (Testing):
(venv) $ python3 ecg_app_MDPM.py
Anda akan melihat output seperti [MQTT] Terhubung... dan Menjalankan server web.... Ini berarti server berjalan. Biarkan terminal ini terbuka.
Untuk Menjalankan Secara Permanen (Production Sederhana):
Hentikan server uji coba (tekan Ctrl+C).
Gunakan nohup untuk menjalankan aplikasi di latar belakang, agar tetap berjalan meskipun Anda menutup terminal SSH:
(venv) $ nohup python3 ecg_app_MDPM.py > app.log 2>&1 &
Aplikasi sekarang berjalan di latar belakang. Anda dapat menutup jendela SSH.
Anda dapat memeriksa log kapan saja dengan: tail -f app.log

Langkah 9: Akses Aplikasi Anda
Kembali ke halaman Compute Engine di konsol GCP.
Temukan IP Eksternal (External IP) untuk VM Anda.
Buka browser web di komputer atau HP Anda.
Masukkan alamat: http://[IP_EKSTERNAL_ANDA]:5000 (ganti [IP_EKSTERNAL_ANDA] dengan IP VM Anda).
Jika semua berjalan lancar, Anda akan melihat Dasbor "ECG Live Platform"!

4. Cara Penggunaan Sistem (Alur Kerja)
Setelah backend Anda berjalan di GCP dan firmware Anda ada di ESP32:
Nyalakan Perangkat ESP32.
Jika ini pertama kalinya, ESP32 akan masuk ke SELECT_MODE.
Hubungkan HP/Laptop ke WiFi ESP32-ECG-AP (password: password123).
Buka browser dan pergi ke http://192.168.4.1.
Pilih "Start Cloud Upload Mode".
ESP32 akan restart dan WiFiManager akan aktif. Hubungkan kembali ke WiFi yang dipancarkan ESP32 (jika diminta).
Sebuah portal akan muncul, minta Anda untuk memilih jaringan WiFi rumah/kantor Anda (yang memiliki akses internet) dan memasukkan passwordnya.
Setelah terhubung, ESP32 akan mengingat kredensial ini dan otomatis terhubung ke internet & MQTT setiap kali dinyalakan. Perangkat akan mulai streaming data.
Buka Dasbor Web GCP Anda (http://[IP_EKSTERNAL_VM]:5000).
Tunggu beberapa detik. device_01 (atau ID perangkat Anda) akan muncul di dropdown "Pilih Device".
Pilih device Anda. Anda akan langsung melihat grafik live waveform dan live values bergerak.
Untuk melakukan analisis, masukkan ID Subjek (misal: "pasien_001").
Klik "Mulai Perekaman".
Tunggu hingga progress bar mencapai 8000/8000 sampel.

Setelah selesai, server akan otomatis memproses data. Beberapa saat kemudian, hasil klasifikasi akan muncul di tabel "Riwayat Klasifikasi" di bagian bawah.
