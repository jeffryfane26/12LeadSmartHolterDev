#include <Wire.h>
#include <SPI.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <WiFi.h>
#include <WebServer.h>
#include <WiFiManager.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <time.h>
#include "FS.h"
#include "SD.h"

// --- MODES OF OPERATION ---
// Mendefinisikan mode operasi alat menggunakan enum untuk kode yang lebih mudah dibaca.
enum OperatingMode {
  SELECT_MODE,  // Mode pemilihan awal
  AP_MODE,      // Mode Access Point lokal untuk streaming data via TCP
  CLOUD_MODE,   // Mode koneksi ke WiFi & streaming data ke cloud MQTT
  SD_CARD_MODE  // Mode perekaman data ke SD Card
};
OperatingMode currentMode = SELECT_MODE; // Variabel untuk menyimpan mode yang sedang berjalan
OperatingMode nextMode = SELECT_MODE;    // Variabel untuk transisi aman antar mode

// --- MQTT & CLOUD SETTINGS ---
// Konfigurasi untuk koneksi ke broker MQTT publik.
const char* mqtt_broker = "broker.hivemq.com";
const int mqtt_port = 1883;
const char* mqtt_topic = "ecg/device_01/data";
const char* mqtt_client_id = "esp32-ecg-holter-01";

// --- LOCAL AP MODE SETTINGS ---
// Konfigurasi untuk mode Access Point (AP) lokal.
const char* ap_ssid = "ESP32-ECG-AP"; // Nama WiFi yang akan dipancarkan oleh ESP32
const char* ap_password = "password123";    // Password WiFi
const uint16_t local_server_port = 8888;      // Port untuk server TCP lokal

// --- NTP Time Server ---
// Konfigurasi untuk sinkronisasi waktu dari internet.
const char* ntpServer = "pool.ntp.org";
const long gmtOffset_sec = 25200; // Offset GMT untuk WIB (UTC+7), yaitu 7 * 3600 detik
const int daylightOffset_sec = 0;   // Tidak ada daylight saving di Indonesia

// --- WEB SERVERS & CLIENTS ---
WebServer server(80); // Web server utama untuk antarmuka pengguna pada port 80 (HTTP)
WiFiServer localDataServer(local_server_port); // Server TCP untuk streaming data EKG di mode AP
WiFiClient localDataClient; // Client yang terhubung ke server TCP
WiFiClient wifiClient;      // Client WiFi untuk koneksi MQTT
PubSubClient mqttClient(wifiClient); // Client MQTT

// --- PIN DEFINITIONS ---
#define BATTERY_PIN 34      // Pin analog untuk membaca tegangan baterai
#define SCREEN_WIDTH 128    // Lebar layar OLED dalam pixel
#define SCREEN_HEIGHT 64    // Tinggi layar OLED dalam pixel
#define OLED_RESET -1       // Pin reset OLED (-1 karena reset di-handle oleh board)
#define SCREEN_ADDRESS 0x3C // Alamat I2C untuk layar OLED

// Pin untuk MicroSD Card (menggunakan bus VSPI default ESP32)
const int PIN_CS_SD = 15;
// Pin untuk Sensor EKG (menggunakan bus HSPI custom)
const int PIN_DRDYB     = 25; // Data Ready, aktif LOW saat data baru siap dibaca
const int PIN_MISO_ECG  = 12;
const int PIN_MOSI_ECG  = 13;
const int PIN_SCLK_ECG  = 14;
const int PIN_SS_MASTER = 33; // Slave Select untuk chip EKG Master
const int PIN_SS_SLAVE1 = 4;  // Slave Select untuk chip EKG Slave 1
const int PIN_SS_SLAVE2 = 2;  // Slave Select untuk chip EKG Slave 2

// Inisialisasi bus SPI kedua (HSPI) untuk komunikasi dengan chip EKG
SPIClass hspi(HSPI);
static const int spiClk = 1000000; // Kecepatan clock SPI 1 MHz

// --- INITIALIZE LIBRARIES ---
// Inisialisasi library untuk layar OLED
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);
unsigned long previousMillisOLED = 0;
const long intervalOLED = 1000; // Perbarui tampilan layar setiap 1 detik

// --- EKG DATA VARIABLES ---
// Variabel untuk menyimpan nilai mentah dari 12 sadapan (leads) EKG
int32_t lead_I, lead_II, lead_III, avr, avl, avf;
int32_t v1, v2, v3, v4, v5, v6;

// --- Framing & Binary Data Variables ---
const byte STX = 0x02; // Start of Text, penanda awal paket data
const byte ETX = 0x03; // End of Text, penanda akhir paket data
int32_t leads_data[12]; // Array untuk menampung data 12 sadapan untuk pengiriman
uint32_t packet_counter = 0; // Penghitung paket untuk monitoring performa

// --- SD Card Variables ---
const unsigned int MAX_SAMPLES = 5000; // Jumlah sampel maksimal sebelum perekaman berhenti otomatis
volatile unsigned int sampleCount = 0; // Penghitung sampel saat ini
File dataFile;                         // Objek file untuk menulis ke SD Card
char fileName[30];                     // Nama file di SD Card
volatile bool isRecording = false;     // Status perekaman (true jika sedang merekam)
String subjectName = "";               // Nama subjek untuk nama file

// --- Web Page Content ---
const char* REFRESH_PAGE_HTML = "<!DOCTYPE html><html><head><title>Switching Mode</title><meta http-equiv='refresh' content='2;url=/'></head><body><h1>Switching Mode...</h1><p>You will be redirected shortly.</p></body></html>";

// --- FUNCTION PROTOTYPES ---
// Deklarasi semua fungsi agar bisa dipanggil dari mana saja dalam kode.
void handleRoot();
void handleSelectModePage();
void handleAPCloudControlPage();
void handleSDPage();
void handleStatus();
void handleSDStart();
void handleSDStop();
void handleSDView();
void handleReset();
void handleNotFound();
String listSDFiles(fs::FS &fs);
void startSelectMode();
void startAPMode();
void startCloudMode();
void startSDCardMode();
void loopAPMode();
void loopCloudMode();
void loopSDCardMode();
void sendECGDataJson(PubSubClient &client);
void sendECGDataCSV(Stream &port);
void sendECGDataBinary(Stream &port);
void startAnimation();
void drawHeart(float scale);
float readBatteryVoltage();
int calculateBatteryPercentage(float batteryVoltage);
void updateDisplayStatus(bool forceUpdate = false); // Default argument HANYA di sini
bool syncTimeWithNTP();

// --- TIME SYNC FUNCTION ---
// Fungsi untuk sinkronisasi waktu dengan server NTP. Penting untuk timestamp di cloud mode.
bool syncTimeWithNTP() {
    Serial.println("Attempting to sync time with NTP server...");
    configTime(gmtOffset_sec, daylightOffset_sec, ntpServer);
    struct tm timeinfo;
    unsigned long startAttempt = millis();
    // Coba sinkronisasi selama 60 detik
    while (millis() - startAttempt < 60000) { 
        if (getLocalTime(&timeinfo)) {
            Serial.println("Time synchronized successfully!");
            Serial.println(&timeinfo, "%A, %B %d %Y %H:%M:%S");
            return true; // Berhasil
        }
        delay(500);
        Serial.print(".");
    }
    Serial.println("\nFailed to obtain time within the timeout period.");
    return false; // Gagal
}

// --- SETUP ---
// Fungsi ini hanya berjalan sekali saat ESP32 pertama kali dinyalakan atau di-reset.
void setup() {
  Serial.begin(921600); // Inisialisasi komunikasi serial dengan baud rate tinggi

  // --- HARDWARE INITIALIZATION ---
  // Mengatur pin-pin yang terhubung ke hardware sebagai input atau output.
  pinMode(PIN_DRDYB, INPUT);
  pinMode(PIN_SS_MASTER, OUTPUT);
  pinMode(PIN_SS_SLAVE1, OUTPUT);
  pinMode(PIN_SS_SLAVE2, OUTPUT);
  deselectAllChips(); // Pastikan tidak ada chip EKG yang aktif
  hspi.begin(PIN_SCLK_ECG, PIN_MISO_ECG, PIN_MOSI_ECG); // Inisialisasi bus SPI EKG
  Wire.begin(21, 22); // Inisialisasi bus I2C untuk OLED
  if (!display.begin(SSD1306_SWITCHCAPVCC, SCREEN_ADDRESS)) {
    Serial.println("Display Halt!");
    for (;;); // Hentikan program jika OLED gagal diinisialisasi
  }

  startAnimation(); // Tampilkan animasi startup di OLED

  // Mulai mode Access Point agar pengguna bisa terhubung untuk memilih mode
  WiFi.softAP(ap_ssid, ap_password);

  // --- SETUP A SINGLE, UNIFIED WEB SERVER ROUTING TABLE ---
  // Mendefinisikan endpoint URL dan fungsi yang akan menanganinya.
  server.on("/", handleRoot); // Halaman utama akan ditangani oleh handleRoot
  // Endpoint untuk memilih mode
  server.on("/ap", [](){ nextMode = AP_MODE; server.send(200, "text/html", REFRESH_PAGE_HTML); });
  server.on("/cloud", [](){ nextMode = CLOUD_MODE; server.send(200, "text/html", REFRESH_PAGE_HTML); });
  server.on("/sdcard", [](){ nextMode = SD_CARD_MODE; server.send(200, "text/html", REFRESH_PAGE_HTML); });
  // Endpoint khusus untuk mode SD Card
  server.on("/status", handleStatus); // Untuk AJAX update status perekaman
  server.on("/start", handleSDStart); // Memulai perekaman
  server.on("/stop", handleSDStop);   // Menghentikan perekaman
  server.on("/view", handleSDView);   // Melihat isi file di SD Card
  // Endpoint global
  server.on("/reset", handleReset);   // Kembali ke halaman pemilihan mode
  server.onNotFound(handleNotFound);  // Halaman jika URL tidak ditemukan (404)
  server.begin(); // Mulai web server

  updateDisplayStatus(true); // Perbarui tampilan OLED untuk pertama kali
}

// --- MAIN LOOP ---
// Fungsi ini berjalan terus-menerus setelah setup() selesai.
void loop() {
  // --- State machine for switching modes safely ---
  // Bagian ini menangani transisi antar mode (misal dari SELECT_MODE ke CLOUD_MODE).
  if (nextMode != currentMode) {
    // Jika ada sisa proses dari mode sebelumnya (misal perekaman), hentikan dulu.
    if (isRecording) { 
      isRecording = false;
      dataFile.close();
    }
    
    currentMode = nextMode; // Terapkan mode baru yang diminta

    // Panggil fungsi inisialisasi untuk mode yang baru dipilih.
    switch (currentMode) {
      case AP_MODE:       startAPMode(); break;
      case CLOUD_MODE:    startCloudMode(); break;
      case SD_CARD_MODE:  startSDCardMode(); break;
      case SELECT_MODE:   startSelectMode(); break;
    }
    // Langsung perbarui tampilan OLED setelah berganti mode.
    updateDisplayStatus(true);
  }

  // Tangani permintaan HTTP dari client di setiap loop, apapun modenya.
  server.handleClient(); 
  // Perbarui tampilan OLED secara berkala.
  updateDisplayStatus();

  // --- Main operation loops for the current active mode ---
  // Jalankan fungsi loop spesifik sesuai mode yang sedang aktif.
  switch (currentMode) {
    case AP_MODE:       loopAPMode(); break;
    case CLOUD_MODE:    loopCloudMode(); break;
    case SD_CARD_MODE:  loopSDCardMode(); break;
    case SELECT_MODE:   /* Tidak ada aksi per-loop yang dibutuhkan */ break;
  }
}

// --- CENTRAL DISPLAY FUNCTION ---
// Fungsi terpusat untuk mengelola semua yang ditampilkan di layar OLED.
// PERBAIKAN: Argumen default "= false" dihapus dari sini.
void updateDisplayStatus(bool forceUpdate) {
  unsigned long currentMillis = millis();
  // Cek apakah sudah waktunya update (setiap 1 detik) atau jika ada paksaan update.
  if (forceUpdate || (currentMillis - previousMillisOLED >= intervalOLED)) {
    previousMillisOLED = currentMillis;
    
    // Baca status baterai
    float batteryVoltage = readBatteryVoltage();
    int batteryPercentage = calculateBatteryPercentage(batteryVoltage);

    // Bersihkan layar dan siapkan untuk menulis teks baru
    display.clearDisplay();
    display.setTextColor(SSD1306_WHITE);
    display.setTextSize(1);
    display.setCursor(0, 0);

    // Tampilkan informasi sesuai mode yang aktif
    switch(currentMode) {
      case AP_MODE:
        display.println(F("--- AP Mode ---"));
        display.println(F("Status: Transmitting BIN"));
        display.print(F("IP: ")); display.println(WiFi.softAPIP().toString());
        break;
      case CLOUD_MODE:
        display.println(F("--- Cloud Mode ---"));
        display.println(mqttClient.connected() ? F("Status: Streaming BIN") : F("Status: Reconnecting"));
        display.print(F("IP: ")); display.println(WiFi.localIP());
        break;
      case SD_CARD_MODE:
        display.println(F("--- SD Card Mode ---"));
        if(isRecording){
          display.print(F("Rec: "));
          display.print(sampleCount);
          display.print("/");
          display.println(MAX_SAMPLES);
          display.println(fileName);
        } else {
          display.println(F("Status: Standby"));
          display.println("Waiting for input...");
        }
        break;
      case SELECT_MODE:
        {
          IPAddress apIP = WiFi.softAPIP();
          display.println("--- Mode Selection ---");
          display.println("Connect to WiFi:");
          display.println(ap_ssid);
          display.println("Then go to browser:");
          display.println(apIP.toString());
        }
        break;
    }

    // Tampilkan status baterai di bagian bawah layar untuk semua mode kecuali SELECT_MODE
    if (currentMode != SELECT_MODE) {
        display.setCursor(0, 48);
        display.print(F("Batt: ")); display.print(batteryPercentage);
        display.print(F("% (")); display.print(batteryVoltage, 2); display.print(F("V)"));
    }
    // Kirim buffer gambar ke layar OLED untuk ditampilkan
    display.display();
  }
}


// --- UNIFIED & SIMPLIFIED WEB SERVER HANDLERS ---
// Bagian ini berisi fungsi-fungsi yang menangani permintaan dari web browser.

// Fungsi router utama, menentukan halaman mana yang harus ditampilkan berdasarkan mode saat ini.
void handleRoot() {
  switch (currentMode) {
    case SELECT_MODE:   handleSelectModePage(); break;
    case AP_MODE:
    case CLOUD_MODE:    handleAPCloudControlPage(); break;
    case SD_CARD_MODE:  handleSDPage(); break;
  }
}

// Fungsi untuk membuat daftar file yang ada di SD Card dalam format HTML.
String listSDFiles(fs::FS &fs) {
    File root = fs.open("/");
    if(!root || !root.isDirectory()){ return "<p>Failed to open directory</p>"; }
    String fileList = "";
    File file = root.openNextFile();
    while(file){
        if(!file.isDirectory()){
            String fName = String(file.name());
            fileList += "<p><a href='/view?file=" + fName + "'>" + fName + "</a> (" + String(file.size()) + " bytes)</p>";
        }
        file = root.openNextFile();
    }
    if (fileList == "") { return "<p>No files on SD Card.</p>"; }
    return fileList;
}

// Menampilkan halaman web untuk memilih mode operasi.
void handleSelectModePage() {
  String html = "<html><head><title>ECG Mode Selection</title><meta name='viewport' content='width=device-width, initial-scale=1'><style>body{font-family: Arial, sans-serif; text-align: center; margin-top: 50px;} button{width: 80%; padding: 20px; font-size: 1.5em; margin: 10px; border-radius: 10px; border: none; color: white; cursor: pointer;} .ap{background-color: #007BFF;} .cloud{background-color: #28A745;} .sd{background-color: #ffc107; color: black;}</style></head>";
  html += "<body><h1>Select Operating Mode</h1>";
  html += "<a href='/ap'><button class='ap'>Start Local AP Mode</button></a>";
  html += "<a href='/cloud'><button class='cloud'>Start Cloud Upload Mode</button></a>";
  html += "<a href='/sdcard'><button class='sd'>Start SD Card Logging</button></a>";
  html += "</body></html>";
  server.send(200, "text/html", html);
}

// Menampilkan halaman kontrol sederhana untuk mode AP dan Cloud.
void handleAPCloudControlPage() {
  String html = "<html><head><title>ECG Control</title><meta name='viewport' content='width=device-width, initial-scale=1'><style>body{font-family: Arial, sans-serif; text-align: center; margin-top: 50px;} button{width: 80%; padding: 20px; font-size: 1.5em; margin: 10px; border-radius: 10px; border: none; color: white; cursor: pointer; background-color: #DC3545;}</style></head>";
  html += "<body><h1>Device Control</h1><a href='/reset'><button>Return to Mode Selection</button></a></body></html>";
  server.send(200, "text/html", html);
}

// Menampilkan halaman web utama untuk mode SD Card Logging.
void handleSDPage() {
  // Menggunakan R"rawliteral(...)" untuk string HTML multi-baris agar lebih mudah dibaca
  String html = R"rawliteral(
<!DOCTYPE html><html><head><title>ECG SD Logger</title>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<style>
  body{font-family: Arial, sans-serif; text-align: center; margin-top: 30px;}
  .container{max-width: 600px; margin: auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 0 10px rgba(0,0,0,0.1);}
  input[type=text], input[type=submit]{width: calc(100% - 24px); padding: 12px; margin: 10px 0; border-radius: 5px; border: 1px solid #ccc;}
  input[type=submit]:disabled{background-color: #cccccc;}
  button, input[type=submit].start{background-color: #28A745;}
  button{width: 80%; padding: 20px; font-size: 1.5em; margin: 10px; border-radius: 10px; border: none; color: white; cursor: pointer;}
  .stop{background-color: #DC3545;} .reset{background-color: #6c757d;}
  .file-list{text-align:left; margin-top:20px; border:1px solid #ccc; padding:10px; border-radius:5px;}
  .progress-container{margin-top: 20px; background-color: #ddd; border-radius: 5px;}
  .progress-bar{width: 0%; height: 30px; background-color: #4CAF50; text-align: center; line-height: 30px; color: white; border-radius: 5px;}
</style>
</head><body>
<div class='container'>
  <h1>SD Card Logging</h1>)rawliteral";
  
  // Tampilkan UI yang berbeda tergantung apakah sedang merekam atau tidak
  if (isRecording) {
    html += "<h3>File: " + String(fileName) + "</h3>";
    html += "<div id='progressContainer' class='progress-container'><div id='progressBar' class='progress-bar'>0%</div></div>";
    html += "<p id='progressStatus'>Recording...</p>";
    html += "<a href='/stop'><button class='stop'>Stop Recording</button></a>";
  } else {
    html += "<h2>Status: STANDBY</h2><form id='recordForm' action='/start' method='get'><input type='text' id='subject_name' name='subject_name' placeholder='Enter Subject Name' required><input type='submit' id='submitBtn' value='Start Recording' class='start'></form>";
  }
  
  html += "<a href='/reset'><button class='reset'>Return to Mode Selection</button></a>";
  html += "<div class='file-list'><h2>Files on SD Card:</h2>" + listSDFiles(SD) + "</div>";
  html += R"rawliteral(
</div>
<script>
  // Script JavaScript untuk memperbarui progress bar secara live
  function updateProgress() {
    fetch('/status') // Minta status terbaru dari ESP32
      .then(res => res.json())
      .then(data => {
        if (!data.is_recording) {
          clearInterval(progressInterval); // Hentikan update jika perekaman selesai
          document.getElementById('progressStatus').textContent = 'Recording Complete!';
          setTimeout(() => { window.location.reload(); }, 2000); // Muat ulang halaman setelah 2 detik
          return;
        }
        const progress = (data.samples / )rawliteral" + String(MAX_SAMPLES) + R"rawliteral() * 100;
        const progressBar = document.getElementById('progressBar');
        progressBar.style.width = progress + '%';
        progressBar.textContent = Math.round(progress) + '%';
        document.getElementById('progressStatus').textContent = 'Recording... (' + data.samples + ' / )rawliteral" + String(MAX_SAMPLES) + R"rawliteral( samples)';
      });
  }
  
  let progressInterval;
  // Jika elemen progress bar ada di halaman, mulai interval untuk update
  if (document.getElementById('progressContainer')) {
    progressInterval = setInterval(updateProgress, 1000); // Update setiap 1 detik
  }
</script>
</body></html>)rawliteral";

  server.send(200, "text/html", html);
}


// Endpoint API yang mengembalikan status perekaman dalam format JSON.
void handleStatus() {
  String json = "{\"is_recording\":" + String(isRecording ? "true" : "false") + ", \"samples\":" + String(sampleCount) + "}";
  server.send(200, "application/json", json);
}

// Menangani permintaan untuk memulai perekaman ke SD Card.
void handleSDStart() {
  if (currentMode != SD_CARD_MODE) return;
  // Ambil nama subjek dari parameter URL
  if (server.hasArg("subject_name")) {
    subjectName = server.arg("subject_name");
    sprintf(fileName, "/%s.bin", subjectName.c_str()); // Buat nama file
    dataFile = SD.open(fileName, FILE_WRITE); // Buka file untuk ditulis
    if (dataFile) {
      sampleCount = 0; // Reset counter sampel
      isRecording = true; // Set status sedang merekam
    }
  }
  // Redirect browser kembali ke halaman utama
  server.sendHeader("Location", "/", true);
  server.send(302, "text/plain", "");
}

// Menangani permintaan untuk menghentikan perekaman.
void handleSDStop() {
  if (currentMode != SD_CARD_MODE) return;
  if (isRecording) {
    isRecording = false; // Set status tidak merekam
    dataFile.close(); // Tutup file
  }
  // Redirect browser kembali ke halaman utama
  server.sendHeader("Location", "/", true);
  server.send(302, "text/plain", "");
}

// Menangani permintaan untuk melihat konten file biner dari SD Card.
void handleSDView() {
    if (!server.hasArg("file")) {
        server.send(400, "text/plain", "Bad Request: Missing file parameter");
        return;
    }
    String path = "/" + server.arg("file");
    File file = SD.open(path, FILE_READ);

    // Kirim data secara streaming untuk file besar
    server.setContentLength(CONTENT_LENGTH_UNKNOWN);
    server.send(200, "text/html", "");
    String header = "<html><head><title>File Content</title><style>table, th, td {border: 1px solid black; border-collapse: collapse;} th, td {padding: 5px; text-align: right;}</style></head><body><h1>Content of " + path + "</h1><a href='/'>Back to Logger</a><br><br><table>";
    header += "<thead><tr><th>Lead I</th><th>Lead II</th><th>Lead III</th><th>AVR</th><th>AVL</th><th>AVF</th><th>V1</th><th>V2</th><th>V3</th><th>V4</th><th>V5</th><th>V6</th></tr></thead><tbody>";
    server.sendContent(header);

    if (!file || file.isDirectory() || file.size() == 0) {
        server.sendContent("<tr><td colspan='12'>File not found or is empty.</td></tr>");
    } else {
        // Baca file per blok data (satu sampel 12-lead) dan kirim sebagai baris tabel HTML
        while (file.available()) {
            size_t bytesRead = file.read((uint8_t*)leads_data, sizeof(leads_data));
            if (bytesRead == sizeof(leads_data)) {
                String row = "<tr>";
                for (int i = 0; i < 12; i++) row += "<td>" + String(leads_data[i]) + "</td>";
                row += "</tr>";
                server.sendContent(row);
            } else { break; }
        }
    }
    file.close();
    server.sendContent("</tbody></table></body></html>");
    server.sendContent("");
}


// Menangani permintaan untuk kembali ke halaman pemilihan mode.
void handleReset() {
  nextMode = SELECT_MODE;
  server.send(200, "text/html", REFRESH_PAGE_HTML);
}

// Menangani URL yang tidak ditemukan.
void handleNotFound() {
  server.send(404, "text/plain", "404: Not Found");
}


// --- MODE STARTUP FUNCTIONS ---
// Fungsi-fungsi ini dipanggil sekali saat sebuah mode dimulai.

// Inisialisasi untuk mode pemilihan.
void startSelectMode() {
  startAnimation();
  WiFi.mode(WIFI_AP); // Pastikan mode AP aktif
  WiFi.softAP(ap_ssid, ap_password);
}

// Inisialisasi untuk mode AP lokal.
void startAPMode() {
  startAnimation();
  localDataServer.begin(); // Mulai server TCP
  // Setup semua chip EKG
  setup_ECG_Master();
  setup_ECG_Slave(PIN_SS_SLAVE1);
  setup_ECG_Slave(PIN_SS_SLAVE2);
  start_ECG_Conversion_All(); // Mulai konversi ADC pada chip EKG
}

// Inisialisasi untuk mode Cloud.
void startCloudMode() {
  startAnimation();
  // Tampilkan status di OLED
  display.clearDisplay();
  display.setCursor(0,0);
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.println("--- Cloud Mode ---");
  display.println("Connecting to WiFi...");
  display.println("Portal if needed:");
  display.println(ap_ssid);
  display.display();

  Serial.println("Starting Cloud Mode...");

  // --- PERBAIKAN LANJUTAN UNTUK WIFI MANAGER ---
  // 1. Hentikan web server utama untuk sementara agar tidak konflik dengan server WiFiManager.
  Serial.println("Stopping main web server...");
  server.stop();

  // 2. Matikan Soft AP yang berjalan dari mode sebelumnya secara eksplisit.
  Serial.println("Disconnecting soft AP...");
  WiFi.softAPdisconnect(true);
  WiFi.mode(WIFI_STA);
  delay(500); // Beri jeda agar radio WiFi stabil
  Serial.println("WiFi mode set to STA.");

  WiFiManager wm;
  wm.setConfigPortalTimeout(180); // Portal akan timeout setelah 3 menit
  
  // 3. Jalankan WiFiManager. autoConnect akan menjadi blocking call di sini.
  Serial.println("Starting WiFiManager portal...");
  bool connected = wm.autoConnect(ap_ssid, ap_password);

  // 4. Mulai kembali web server utama setelah WiFiManager selesai, apapun hasilnya.
  Serial.println("Restarting main web server...");
  server.begin();

  // 5. Cek hasil koneksi dari WiFiManager.
  if (!connected) {
    Serial.println("Failed to connect via WiFiManager and hit timeout.");
    nextMode = SELECT_MODE; // Jika gagal, kembali ke mode pemilihan
    return; // Hentikan sisa setup cloud mode.
  }
  
  Serial.println("WiFi connected successfully!");
  // Jika berhasil terhubung, sinkronisasi waktu.
  if (!syncTimeWithNTP()) {
      Serial.println("FATAL: Could not sync time. Rebooting in 5 seconds..."); 
      delay(5000); 
      ESP.restart(); // Waktu sangat penting, jadi restart jika gagal.
  }
  // Konfigurasi client MQTT dan chip EKG
  mqttClient.setServer(mqtt_broker, mqtt_port);
  setup_ECG_Master();
  setup_ECG_Slave(PIN_SS_SLAVE1);
  setup_ECG_Slave(PIN_SS_SLAVE2);
  start_ECG_Conversion_All();
}

// Inisialisasi untuk mode SD Card.
void startSDCardMode() {
  startAnimation();
  // Mode SD Card juga butuh AP untuk web interface
  WiFi.mode(WIFI_AP);
  WiFi.softAP(ap_ssid, ap_password);
  delay(500);
  Serial.println("Initializing SD card for logging mode...");
  if (!SD.begin(PIN_CS_SD)) {
    Serial.println("Card Mount Failed!");
    display.clearDisplay(); display.setCursor(0,0);
    display.println("SD Card Mount Failed!"); display.display();
    delay(2000); 
    nextMode = SELECT_MODE; // Kembali ke mode pemilihan jika SD Card gagal
    return;
  }
  Serial.println("SD card initialized.");
  strcpy(fileName, "No file selected");
  // Setup chip EKG
  setup_ECG_Master();
  setup_ECG_Slave(PIN_SS_SLAVE1);
  setup_ECG_Slave(PIN_SS_SLAVE2);
  start_ECG_Conversion_All();
}


// --- MODE LOOP FUNCTIONS ---
// Fungsi-fungsi ini dipanggil berulang kali saat sebuah mode aktif.

// Loop untuk mode AP lokal.
void loopAPMode() {
  // Cek apakah ada client baru yang terhubung ke server TCP
  if (!localDataClient.connected()) {
    localDataClient = localDataServer.available();
  }
  // Jika pin DRDYB LOW, berarti data EKG baru sudah siap
  if (digitalRead(PIN_DRDYB) == LOW) {
    // Baca data dari semua chip
    lead_I = getValFromChannel(PIN_SS_MASTER, 1); lead_II = getValFromChannel(PIN_SS_MASTER, 2);
    v1 = getValFromChannel(PIN_SS_SLAVE1, 1); v2 = getValFromChannel(PIN_SS_SLAVE1, 2); v3 = getValFromChannel(PIN_SS_SLAVE1, 3);
    v4 = getValFromChannel(PIN_SS_SLAVE2, 1); v5 = getValFromChannel(PIN_SS_SLAVE2, 2);
    v6 = getValFromChannel(PIN_SS_SLAVE2, 3);
    // Hitung lead augmentasi
    lead_III = lead_II - lead_I; avr = -(lead_I + lead_II) / 2;
    avl = lead_I - (lead_II / 2); avf = lead_II - (lead_I / 2);
    // Kirim data jika ada client yang terhubung
    if (localDataClient.connected()) sendECGDataBinary(localDataClient);
  }
}

// Loop untuk mode Cloud.
void loopCloudMode() {
  // Jaga koneksi MQTT tetap hidup
  if (!mqttClient.connected()) {
    if (!mqttClient.connect(mqtt_client_id)) { delay(2000); return; }
  }
  mqttClient.loop();
  // Jika data EKG baru siap
  if (digitalRead(PIN_DRDYB) == LOW) {
    // Baca semua data EKG
    lead_I = getValFromChannel(PIN_SS_MASTER, 1); lead_II = getValFromChannel(PIN_SS_MASTER, 2);
    v1 = getValFromChannel(PIN_SS_SLAVE1, 1);
    v2 = getValFromChannel(PIN_SS_SLAVE1, 2); v3 = getValFromChannel(PIN_SS_SLAVE1, 3);
    v4 = getValFromChannel(PIN_SS_SLAVE2, 1); v5 = getValFromChannel(PIN_SS_SLAVE2, 2); v6 = getValFromChannel(PIN_SS_SLAVE2, 3);
    // Hitung lead augmentasi
    lead_III = lead_II - lead_I; avr = -(lead_I + lead_II) / 2;
    avl = lead_I - (lead_II / 2);
    avf = lead_II - (lead_I / 2);
    // Susun data ke dalam array
    leads_data[0] = lead_I;   leads_data[1] = lead_II; leads_data[2] = lead_III; leads_data[3] = avr;
    leads_data[4] = avl;      leads_data[5] = avf; leads_data[6] = v1;       leads_data[7] = v2;
    leads_data[8] = v3;       leads_data[9] = v4;
    leads_data[10] = v5;      leads_data[11] = v6;
    // Buat paket data biner untuk dikirim via MQTT
    struct timeval tv; gettimeofday(&tv, NULL);
    uint64_t timestamp_us = (uint64_t)tv.tv_sec * 1000000L + (uint64_t)tv.tv_usec;
    packet_counter++;
    uint8_t payload[63];
    payload[0] = STX;
    memcpy(&payload[1], &timestamp_us, 8); // 8 byte timestamp
    memcpy(&payload[9], &packet_counter, 4); // 4 byte counter
    memcpy(&payload[13], leads_data, 48);   // 48 byte data EKG (12 * 4 byte)
    uint8_t checksum = 0;
    for (int i = 1; i < 61; i++) checksum += payload[i]; // Hitung checksum
    payload[61] = checksum;
    payload[62] = ETX;
    // Kirim paket ke broker MQTT
    mqttClient.publish(mqtt_topic, payload, sizeof(payload));
  }
}

// Loop untuk mode SD Card.
void loopSDCardMode() {
  // Cek apakah sedang merekam dan ada data baru
  if (isRecording && (digitalRead(PIN_DRDYB) == LOW)) {
    // Cek apakah kuota sampel sudah tercapai
    if (sampleCount < MAX_SAMPLES) {
      sampleCount++;
      // Baca semua data EKG
      lead_I = getValFromChannel(PIN_SS_MASTER, 1); lead_II = getValFromChannel(PIN_SS_MASTER, 2);
      v1 = getValFromChannel(PIN_SS_SLAVE1, 1); v2 = getValFromChannel(PIN_SS_SLAVE1, 2); v3 = getValFromChannel(PIN_SS_SLAVE1, 3);
      v4 = getValFromChannel(PIN_SS_SLAVE2, 1); v5 = getValFromChannel(PIN_SS_SLAVE2, 2); v6 = getValFromChannel(PIN_SS_SLAVE2, 3);
      lead_III = lead_II - lead_I;
      avr = -(lead_I + lead_II) / 2;
      avl = lead_I - (lead_II / 2);
      avf = lead_II - (lead_I / 2);
      // Susun data ke dalam array
      leads_data[0] = lead_I;   leads_data[1] = lead_II; leads_data[2] = lead_III; leads_data[3] = avr;
      leads_data[4] = avl;      leads_data[5] = avf; leads_data[6] = v1;       leads_data[7] = v2;
      leads_data[8] = v3;       leads_data[9] = v4;
      leads_data[10] = v5;      leads_data[11] = v6;
      // Tulis data biner ke file di SD Card
      if (dataFile) {
          dataFile.write((const uint8_t*)leads_data, sizeof(leads_data));
      }
    } else {
      // Jika kuota tercapai, hentikan perekaman secara otomatis
      isRecording = false;
      dataFile.close();
      Serial.println("\nRecording finished automatically. File saved.");
    }
  }
}

// --- DATA SENDING FUNCTIONS ---

// Fungsi untuk mengirim data EKG dalam format JSON (tidak terpakai saat ini, tapi berguna untuk masa depan).
void sendECGDataJson(PubSubClient &client) {
  StaticJsonDocument<512> doc;
  doc["deviceId"] = mqtt_client_id;
  doc["lead_I"] = lead_I;   doc["lead_II"] = lead_II;
  doc["lead_III"] = lead_III; doc["avr"] = avr;
  doc["avl"] = avl;
  doc["avf"] = avf;
  doc["v1"] = v1;         doc["v2"] = v2;
  doc["v3"] = v3;         doc["v4"] = v4;
  doc["v5"] = v5;
  doc["v6"] = v6;
  char jsonBuffer[512];
  serializeJson(doc, jsonBuffer);
  client.publish(mqtt_topic, jsonBuffer);
}

// Fungsi untuk mengirim data EKG dalam format biner yang dibingkai (framed).
void sendECGDataBinary(Stream &port) {
  // Susun data ke dalam array
  leads_data[0] = lead_I;   leads_data[1] = lead_II;
  leads_data[2] = lead_III; leads_data[3] = avr;
  leads_data[4] = avl;      leads_data[5] = avf; leads_data[6] = v1;       leads_data[7] = v2;
  leads_data[8] = v3;       leads_data[9] = v4; leads_data[10] = v5;      leads_data[11] = v6;
  // Hitung checksum
  uint8_t checksum = 0;
  uint8_t* data_ptr = (uint8_t*)leads_data;
  for (int i = 0; i < sizeof(leads_data); i++) {
    checksum += data_ptr[i];
  }
  // Kirim paket: STX | DATA | CHECKSUM | ETX
  port.write(STX);
  port.write((uint8_t*)leads_data, sizeof(leads_data));
  port.write(checksum);
  port.write(ETX);
}


// --- HELPER & HARDWARE FUNCTIONS ---

// Membaca tegangan baterai dari pin analog.
float readBatteryVoltage() {
  int batteryLevel = analogRead(BATTERY_PIN);
  // Konversi nilai ADC (0-4095) ke tegangan (0-3.3V)
  float rawVoltage = batteryLevel * (3.3 / 4095.0);
  // Kalikan dengan faktor pembagi tegangan (voltage divider) untuk mendapatkan tegangan asli baterai
  float batteryVoltage = rawVoltage * 3.24; 
  return batteryVoltage;
}

// Mengonversi tegangan baterai menjadi persentase (%).
int calculateBatteryPercentage(float batteryVoltage) {
  float minVoltage = 6.40; // Tegangan saat 0%
  float maxVoltage = 8.40; // Tegangan saat 100% (untuk baterai Li-ion 2S)
  // Gunakan fungsi map untuk mengubah rentang tegangan ke rentang persentase
  int percentage = map(batteryVoltage * 100, minVoltage * 100, maxVoltage * 100, 0, 100);
  // Batasi nilai antara 0 dan 100
  return constrain(percentage, 0, 100);
}

// Menggambar hati di layar OLED (untuk animasi).
void drawHeart(float scale) {
  display.clearDisplay();
  int xCenter = SCREEN_WIDTH / 2;
  int yCenter = SCREEN_HEIGHT / 2 + 10;
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(1);
  display.setCursor(5, 0);
  display.print(F("12-Lead ECG Device"));
  int radius = (int)(10 * scale);
  int bottomHeight = (int)(20 * scale);
  // Gambar hati dari dua lingkaran dan satu segitiga
  display.fillCircle(xCenter - radius, yCenter - radius / 2, radius, SSD1306_WHITE);
  display.fillCircle(xCenter + radius, yCenter - radius / 2, radius, SSD1306_WHITE);
  display.fillTriangle(xCenter - 2 * radius, yCenter - radius / 2,
                       xCenter + 2 * radius, yCenter - radius / 2,
                       xCenter, yCenter + bottomHeight, SSD1306_WHITE);
  display.display();
}

// Menjalankan animasi startup.
void startAnimation() {
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(1);
  display.setCursor(22, 0);
  display.print(F("12-Lead Holter"));
  display.setCursor(20, 18);
  display.print(F("Initializing..."));
  display.setCursor(20, 50);
  display.print(F("CTAI Laboratory"));
  display.display();
  delay(2000);
  // Animasi hati berdenyut
  for (int i = 0; i < 2; i++) {
    for (float scale = 1.0; scale <= 1.3; scale += 0.05) { drawHeart(scale); delay(80); }
    for (float scale = 1.3; scale >= 1.0; scale -= 0.05) { drawHeart(scale); delay(80); }
  }
}

// --- ECG HARDWARE FUNCTIONS ---
// Fungsi-fungsi level rendah untuk berkomunikasi dengan chip EKG via SPI.

// Mengaktifkan satu chip EKG tertentu dengan menurunkan pin SS-nya.
void selectChip(int chip_SS) {
  digitalWrite(PIN_SS_MASTER, HIGH);
  digitalWrite(PIN_SS_SLAVE1, HIGH);
  digitalWrite(PIN_SS_SLAVE2, HIGH);
  digitalWrite(chip_SS, LOW);
}

// Menonaktifkan semua chip EKG.
void deselectAllChips() {
  digitalWrite(PIN_SS_MASTER, HIGH);
  digitalWrite(PIN_SS_SLAVE1, HIGH);
  digitalWrite(PIN_SS_SLAVE2, HIGH);
}

// Membaca nilai dari satu register pada chip EKG tertentu.
byte readRegister(int chip_SS, byte reg) {
  reg |= 0x80; // Set bit paling signifikan untuk operasi baca
  hspi.beginTransaction(SPISettings(spiClk, MSBFIRST, SPI_MODE0));
  selectChip(chip_SS);
  hspi.transfer(reg); // Kirim alamat register
  byte data = hspi.transfer(0); // Baca data yang dikembalikan
  deselectAllChips();
  hspi.endTransaction();
  return data;
}

// Menulis nilai ke satu register pada chip EKG tertentu.
void writeRegister(int chip_SS, byte reg, byte data) {
  reg &= 0x7F; // Pastikan bit paling signifikan 0 untuk operasi tulis
  hspi.beginTransaction(SPISettings(spiClk, MSBFIRST, SPI_MODE0));
  selectChip(chip_SS);
  hspi.transfer(reg); // Kirim alamat register
  hspi.transfer(data); // Kirim data yang akan ditulis
  deselectAllChips();
  hspi.endTransaction();
}

// Membaca nilai EKG 24-bit dari 3 register yang berurutan.
int32_t getValFromChannel(int chip_SS, int channel_num_on_chip) {
  byte r1, r2, r3;
  // Tentukan alamat register berdasarkan nomor channel
  switch (channel_num_on_chip) {
    case 1: r1 = 0x37; r2 = 0x38; r3 = 0x39; break;
    case 2: r1 = 0x3A; r2 = 0x3B; r3 = 0x3C; break;
    case 3: r1 = 0x3D; r2 = 0x3E; r3 = 0x3F; break;
    default: return 0;
  }
  // Baca 3 byte dan gabungkan menjadi satu nilai 32-bit
  int32_t val = ((int32_t)readRegister(chip_SS, r1) << 16) |
                 ((int32_t)readRegister(chip_SS, r2) << 8)  |
                 (int32_t)readRegister(chip_SS, r3);
  
  // Lakukan sign extension jika nilai negatif (24-bit two's complement)
  if (val & 0x00800000) {
    val |= 0xFF000000;
  }
  return val;
}

// Melakukan konfigurasi register untuk chip EKG Master.
void setup_ECG_Master() {
  writeRegister(PIN_SS_MASTER, 0x00, 0x00);
  writeRegister(PIN_SS_MASTER, 0x01, 0x11);
  writeRegister(PIN_SS_MASTER, 0x02, 0x19);
  writeRegister(PIN_SS_MASTER, 0x0A, 0x07);
  writeRegister(PIN_SS_MASTER, 0x0C, 0x04);
  writeRegister(PIN_SS_MASTER, 0x0D, 0x01);
  writeRegister(PIN_SS_MASTER, 0x0E, 0x02);
  writeRegister(PIN_SS_MASTER, 0x0F, 0x03);
  writeRegister(PIN_SS_MASTER, 0x12, 0x01); delay(1);
  writeRegister(PIN_SS_MASTER, 0x12, 0x05);
  writeRegister(PIN_SS_MASTER, 0x14, 0x24);
  writeRegister(PIN_SS_MASTER, 0x21, 0x02);
  writeRegister(PIN_SS_MASTER, 0x22, 0x02);
  writeRegister(PIN_SS_MASTER, 0x23, 0x02);
  writeRegister(PIN_SS_MASTER, 0x27, 0x08);
  writeRegister(PIN_SS_MASTER, 0x28, 0x08);
  writeRegister(PIN_SS_MASTER, 0x2F, 0x30);
}

// Melakukan konfigurasi register untuk chip EKG Slave.
void setup_ECG_Slave(int chip_SS) {
  writeRegister(chip_SS, 0x00, 0x00);
  writeRegister(chip_SS, 0x01, 0x0C);
  writeRegister(chip_SS, 0x02, 0x14);
  writeRegister(chip_SS, 0x03, 0x1C);
  writeRegister(chip_SS, 0x12, 0x02); delay(1);
  writeRegister(chip_SS, 0x12, 0x06);
  writeRegister(chip_SS, 0x21, 0x02);
  writeRegister(chip_SS, 0x22, 0x02);
  writeRegister(chip_SS, 0x23, 0x02);
  writeRegister(chip_SS, 0x24, 0x02);
  writeRegister(chip_SS, 0x27, 0x00);
  writeRegister(chip_SS, 0x28, 0x40);
  writeRegister(chip_SS, 0x2F, 0x70);
}

// Mengirim perintah untuk memulai konversi ADC pada semua chip EKG secara bersamaan.
void start_ECG_Conversion_All() {
  writeRegister(PIN_SS_MASTER, 0x00, 0x01);
  writeRegister(PIN_SS_SLAVE1, 0x00, 0x01);
  writeRegister(PIN_SS_SLAVE2, 0x00, 0x01);
}
