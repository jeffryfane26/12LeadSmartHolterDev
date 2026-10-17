/**
 * @file FINAL_APCLSD.ino
 * @brief 12-Lead Smart Holter Edge Acquisition Firmware for ESP32
 * @author CTAI Laboratory
 * @version 1.0.0
 * 
 * This firmware coordinates multi-channel ECG acquisition, real-time lead synthesis,
 * local display rendering, offline SD logging, and high-speed binary cloud streaming.
 * 
 * Hardware Topology:
 * - MCU: ESP32-WROOM-32 @ 240 MHz
 * - Front-End: 3x Cascaded 24-bit ECG AFEs (Master SS: GPIO 33, Slave 1 SS: GPIO 4, Slave 2 SS: GPIO 2)
 * - Sensor Bus: HSPI @ 1.0 MHz (SCLK: 14, MISO: 12, MOSI: 13, DRDYB: 25)
 * - Display: 128x64 SSD1306 OLED via I2C (SDA: 21, SCL: 22, Addr: 0x3C)
 * - Storage: FAT32 MicroSD via VSPI (CS: 15)
 * - Battery: 2S Li-ion Voltage Divider on GPIO 34 (ADC1_CH6)
 * 
 * Operating Modes:
 * 1. SELECT_MODE: SoftAP (ESP32-ECG-AP) web menu for mode selection.
 * 2. AP_MODE: Local TCP server (port 8888) streaming 51-byte framed binary packets.
 * 3. SD_CARD_MODE: Binary logging to MicroSD file (/subject.bin) with web inspector.
 * 4. CLOUD_MODE: WiFiManager captive portal, NTP time sync, and 63-byte MQTT streaming.
 */

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
// Defines operating modes using an enum for structured state management.
enum OperatingMode {
  SELECT_MODE,  // Initial configuration & mode selection portal
  AP_MODE,      // Local Access Point mode for raw TCP binary streaming
  CLOUD_MODE,   // Wi-Fi connection and cloud MQTT telemetry streaming
  SD_CARD_MODE  // Offline FAT32 binary recording to MicroSD
};
OperatingMode currentMode = SELECT_MODE; // Variable holding the active operating mode
OperatingMode nextMode = SELECT_MODE;    // Variable for safe state transitions

// --- MQTT & CLOUD SETTINGS ---
// Configuration for public MQTT broker connection.
const char* mqtt_broker = "broker.hivemq.com";
const int mqtt_port = 1883;
const char* mqtt_topic = "ecg/device_01/data";
const char* mqtt_client_id = "esp32-ecg-holter-01";

// --- LOCAL AP MODE SETTINGS ---
// Configuration for local Access Point (AP) mode.
const char* ap_ssid = "ESP32-ECG-AP"; // SSID broadcast by ESP32 SoftAP
const char* ap_password = "password123";    // SoftAP Wi-Fi password
const uint16_t local_server_port = 8888;      // Port for local TCP streaming server

// --- NTP Time Server ---
// Configuration for real-world network time synchronization.
const char* ntpServer = "pool.ntp.org";
const long gmtOffset_sec = 25200; // GMT offset for WIB (UTC+7): 7 * 3600 seconds
const int daylightOffset_sec = 0;   // Daylight saving offset (0 for Indonesia)

// --- WEB SERVERS & CLIENTS ---
WebServer server(80); // Main HTTP web server on port 80
WiFiServer localDataServer(local_server_port); // TCP server for raw ECG streaming in AP mode
WiFiClient localDataClient; // Connected TCP streaming client
WiFiClient wifiClient;      // Wi-Fi client for MQTT connection
PubSubClient mqttClient(wifiClient); // MQTT client

// --- PIN DEFINITIONS ---
#define BATTERY_PIN 34      // Analog pin for battery voltage divider readout
#define SCREEN_WIDTH 128    // OLED display width in pixels
#define SCREEN_HEIGHT 64    // OLED display height in pixels
#define OLED_RESET -1       // OLED reset pin (-1 as reset is board-controlled)
#define SCREEN_ADDRESS 0x3C // I2C address for SSD1306 OLED display

// MicroSD Card Chip Select (using default ESP32 VSPI bus)
const int PIN_CS_SD = 15;
// ECG AFE Pins (using custom HSPI bus)
const int PIN_DRDYB     = 25; // Data Ready interrupt (Active LOW when new sample is ready)
const int PIN_MISO_ECG  = 12;
const int PIN_MOSI_ECG  = 13;
const int PIN_SCLK_ECG  = 14;
const int PIN_SS_MASTER = 33; // Chip Select for Master ECG AFE
const int PIN_SS_SLAVE1 = 4;  // Chip Select for Slave 1 ECG AFE
const int PIN_SS_SLAVE2 = 2;  // Chip Select for Slave 2 ECG AFE

// Initialize second SPI bus (HSPI) for ECG AFE communication
SPIClass hspi(HSPI);
static const int spiClk = 1000000; // SPI clock speed 1 MHz

// --- INITIALIZE LIBRARIES ---
// Initialize OLED display driver
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);
unsigned long previousMillisOLED = 0;
const long intervalOLED = 1000; // Refresh display every 1000 ms

// --- ECG DATA VARIABLES ---
// Variables holding raw 24-bit counts across all 12 ECG leads
int32_t lead_I, lead_II, lead_III, avr, avl, avf;
int32_t v1, v2, v3, v4, v5, v6;

// --- Framing & Binary Data Variables ---
const byte STX = 0x02; // Start of Text delimiter
const byte ETX = 0x03; // End of Text delimiter
int32_t leads_data[12]; // Array holding 12-lead sample vector for transmission
uint32_t packet_counter = 0; // Monotonic packet sequence counter for QoS tracking

// --- SD Card Variables ---
const unsigned int MAX_SAMPLES = 5000; // Maximum sample limit before automatic stop
volatile unsigned int sampleCount = 0; // Current collected sample count
File dataFile;                         // File handle for MicroSD writes
char fileName[30];                     // SD card target filename
volatile bool isRecording = false;     // Recording flag (true while actively logging)
String subjectName = "";               // Subject / Patient identifier for filename

// --- Web Page Content ---
const char* REFRESH_PAGE_HTML = "<!DOCTYPE html><html><head><title>Switching Mode</title><meta http-equiv='refresh' content='2;url=/'></head><body><h1>Switching Mode...</h1><p>You will be redirected shortly.</p></body></html>";

// --- FUNCTION PROTOTYPES ---
// Function declarations to allow non-linear definition order.
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
void updateDisplayStatus(bool forceUpdate = false); // Default argument in prototype only
bool syncTimeWithNTP();

// --- TIME SYNC FUNCTION ---
// Synchronizes real-world POSIX time with NTP server for microsecond timestamps.
bool syncTimeWithNTP() {
    Serial.println("Attempting to sync time with NTP server...");
    configTime(gmtOffset_sec, daylightOffset_sec, ntpServer);
    struct tm timeinfo;
    unsigned long startAttempt = millis();
    // Attempt synchronization for up to 60 seconds
    while (millis() - startAttempt < 60000) { 
        if (getLocalTime(&timeinfo)) {
            Serial.println("Time synchronized successfully!");
            Serial.println(&timeinfo, "%A, %B %d %Y %H:%M:%S");
            return true;
        }
        delay(500);
        Serial.print(".");
    }
    Serial.println("\nFailed to obtain time within the timeout period.");
    return false;
}

// --- SETUP ---
// Executes once upon ESP32 power-on or hard reset.
void setup() {
  Serial.begin(921600); // Initialize high-speed serial interface

  // --- HARDWARE INITIALIZATION ---
  // Configure GPIO pin directions.
  pinMode(PIN_DRDYB, INPUT);
  pinMode(PIN_SS_MASTER, OUTPUT);
  pinMode(PIN_SS_SLAVE1, OUTPUT);
  pinMode(PIN_SS_SLAVE2, OUTPUT);
  deselectAllChips(); // Ensure all ECG AFE chip select lines are deasserted
  hspi.begin(PIN_SCLK_ECG, PIN_MISO_ECG, PIN_MOSI_ECG); // Initialize ECG HSPI bus
  Wire.begin(21, 22); // Initialize I2C bus for OLED
  if (!display.begin(SSD1306_SWITCHCAPVCC, SCREEN_ADDRESS)) {
    Serial.println("Display Halt!");
    for (;;); // Halt execution if OLED display initialization fails
  }

  startAnimation(); // Show startup splash screen on OLED

  // Launch Access Point so user can connect to select mode
  WiFi.softAP(ap_ssid, ap_password);

  // --- SETUP UNIFIED WEB SERVER ROUTING TABLE ---
  // Map HTTP endpoints to their respective handler functions.
  server.on("/", handleRoot); // Root route handled by handleRoot router
  // Mode selection routes
  server.on("/ap", [](){ nextMode = AP_MODE; server.send(200, "text/html", REFRESH_PAGE_HTML); });
  server.on("/cloud", [](){ nextMode = CLOUD_MODE; server.send(200, "text/html", REFRESH_PAGE_HTML); });
  server.on("/sdcard", [](){ nextMode = SD_CARD_MODE; server.send(200, "text/html", REFRESH_PAGE_HTML); });
  // Dedicated SD card logging routes
  server.on("/status", handleStatus); // AJAX status poll for recording progress
  server.on("/start", handleSDStart); // Start SD recording
  server.on("/stop", handleSDStop);   // Stop SD recording
  server.on("/view", handleSDView);   // View binary file contents in browser
  // Global management routes
  server.on("/reset", handleReset);   // Return to mode selection
  server.onNotFound(handleNotFound);  // HTTP 404 handler
  server.begin(); // Start web server

  updateDisplayStatus(true); // Force first OLED status render
}

// --- MAIN LOOP ---
// Continuous execution loop following setup().
void loop() {
  // --- State machine for switching modes safely ---
  // Handles safe transitions between operating modes (e.g., from SELECT_MODE to CLOUD_MODE).
  if (nextMode != currentMode) {
    // If a recording was active in the previous mode, close the file handle cleanly.
    if (isRecording) { 
      isRecording = false;
      dataFile.close();
    }
    
    currentMode = nextMode; // Apply newly requested mode

    // Invoke initialization routine for the newly selected mode.
    switch (currentMode) {
      case AP_MODE:       startAPMode(); break;
      case CLOUD_MODE:    startCloudMode(); break;
      case SD_CARD_MODE:  startSDCardMode(); break;
      case SELECT_MODE:   startSelectMode(); break;
    }
    // Refresh OLED display immediately upon mode transition.
    updateDisplayStatus(true);
  }

  // Handle incoming HTTP client requests continuously.
  server.handleClient(); 
  // Refresh OLED display periodically.
  updateDisplayStatus();

  // --- Main operation loops for the current active mode ---
  // Execute the active mode's service loop.
  switch (currentMode) {
    case AP_MODE:       loopAPMode(); break;
    case CLOUD_MODE:    loopCloudMode(); break;
    case SD_CARD_MODE:  loopSDCardMode(); break;
    case SELECT_MODE:   /* Idle waiting for user web interaction */ break;
  }
}

// --- CENTRAL DISPLAY FUNCTION ---
// Centralized manager for rendering graphical status onto the OLED display.
void updateDisplayStatus(bool forceUpdate) {
  unsigned long currentMillis = millis();
  // Check if update interval elapsed (1000 ms) or if forced update requested.
  if (forceUpdate || (currentMillis - previousMillisOLED >= intervalOLED)) {
    previousMillisOLED = currentMillis;
    
    // Read battery voltage and compute percentage
    float batteryVoltage = readBatteryVoltage();
    int batteryPercentage = calculateBatteryPercentage(batteryVoltage);

    // Clear display buffer and configure default text typography
    display.clearDisplay();
    display.setTextColor(SSD1306_WHITE);
    display.setTextSize(1);
    display.setCursor(0, 0);

    // Render mode-specific operational telemetry
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

    // Display battery telemetry at the bottom line for all modes except SELECT_MODE
    if (currentMode != SELECT_MODE) {
        display.setCursor(0, 48);
        display.print(F("Batt: ")); display.print(batteryPercentage);
        display.print(F("% (")); display.print(batteryVoltage, 2); display.print(F("V)"));
    }
    // Flush frame buffer to physical OLED display
    display.display();
  }
}


// --- UNIFIED & SIMPLIFIED WEB SERVER HANDLERS ---
// Web server endpoints handling browser control and streaming requests.

// Master routing handler directing client to mode-specific web views.
void handleRoot() {
  switch (currentMode) {
    case SELECT_MODE:   handleSelectModePage(); break;
    case AP_MODE:
    case CLOUD_MODE:    handleAPCloudControlPage(); break;
    case SD_CARD_MODE:  handleSDPage(); break;
  }
}

// Builds HTML markup listing binary recording files stored on the microSD card.
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

// Serves the initial mode selection portal.
void handleSelectModePage() {
  String html = "<html><head><title>ECG Mode Selection</title><meta name='viewport' content='width=device-width, initial-scale=1'><style>body{font-family: Arial, sans-serif; text-align: center; margin-top: 50px;} button{width: 80%; padding: 20px; font-size: 1.5em; margin: 10px; border-radius: 10px; border: none; color: white; cursor: pointer;} .ap{background-color: #007BFF;} .cloud{background-color: #28A745;} .sd{background-color: #ffc107; color: black;}</style></head>";
  html += "<body><h1>Select Operating Mode</h1>";
  html += "<a href='/ap'><button class='ap'>Start Local AP Mode</button></a>";
  html += "<a href='/cloud'><button class='cloud'>Start Cloud Upload Mode</button></a>";
  html += "<a href='/sdcard'><button class='sd'>Start SD Card Logging</button></a>";
  html += "</body></html>";
  server.send(200, "text/html", html);
}

// Serves simple control dashboard for AP and Cloud streaming modes.
void handleAPCloudControlPage() {
  String html = "<html><head><title>ECG Control</title><meta name='viewport' content='width=device-width, initial-scale=1'><style>body{font-family: Arial, sans-serif; text-align: center; margin-top: 50px;} button{width: 80%; padding: 20px; font-size: 1.5em; margin: 10px; border-radius: 10px; border: none; color: white; cursor: pointer; background-color: #DC3545;}</style></head>";
  html += "<body><h1>Device Control</h1><a href='/reset'><button>Return to Mode Selection</button></a></body></html>";
  server.send(200, "text/html", html);
}

// Serves the primary web dashboard for standalone SD Card Logging mode.
void handleSDPage() {
  // Uses R"rawliteral(...)" for multi-line HTML template formatting
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
  
  // Render dynamic UI state depending on recording status
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
  // Client-side JavaScript polling telemetry and updating recording progress bar
  function updateProgress() {
    fetch('/status') // Query live recording state from ESP32
      .then(res => res.json())
      .then(data => {
        if (!data.is_recording) {
          clearInterval(progressInterval); // Halt periodic polling when acquisition finishes
          document.getElementById('progressStatus').textContent = 'Recording Complete!';
          setTimeout(() => { window.location.reload(); }, 2000); // Refresh view after 2s
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
  // If progress container is mounted, begin polling interval (1000 ms)
  if (document.getElementById('progressContainer')) {
    progressInterval = setInterval(updateProgress, 1000);
  }
</script>
</body></html>)rawliteral";

  server.send(200, "text/html", html);
}


// JSON API endpoint reporting live acquisition status and sample counter.
void handleStatus() {
  String json = "{\"is_recording\":" + String(isRecording ? "true" : "false") + ", \"samples\":" + String(sampleCount) + "}";
  server.send(200, "application/json", json);
}

// Handles HTTP GET request to initiate microSD recording.
void handleSDStart() {
  if (currentMode != SD_CARD_MODE) return;
  // Parse subject name from URL query parameter
  if (server.hasArg("subject_name")) {
    subjectName = server.arg("subject_name");
    sprintf(fileName, "/%s.bin", subjectName.c_str()); // Build binary filename
    dataFile = SD.open(fileName, FILE_WRITE); // Open file for write
    if (dataFile) {
      sampleCount = 0; // Reset sample counter
      isRecording = true; // Set recording active flag
    }
  }
  // Redirect browser back to main dashboard
  server.sendHeader("Location", "/", true);
  server.send(302, "text/plain", "");
}

// Handles HTTP GET request to stop microSD recording cleanly.
void handleSDStop() {
  if (currentMode != SD_CARD_MODE) return;
  if (isRecording) {
    isRecording = false; // Clear recording flag
    dataFile.close(); // Close file handle
  }
  // Redirect browser back to main dashboard
  server.sendHeader("Location", "/", true);
  server.send(302, "text/plain", "");
}

// Handles request to view binary file contents decoded into an HTML table.
void handleSDView() {
    if (!server.hasArg("file")) {
        server.send(400, "text/plain", "Bad Request: Missing file parameter");
        return;
    }
    String path = "/" + server.arg("file");
    File file = SD.open(path, FILE_READ);

    // Stream response chunks to optimize RAM usage for large binary recordings
    server.setContentLength(CONTENT_LENGTH_UNKNOWN);
    server.send(200, "text/html", "");
    String header = "<html><head><title>File Content</title><style>table, th, td {border: 1px solid black; border-collapse: collapse;} th, td {padding: 5px; text-align: right;}</style></head><body><h1>Content of " + path + "</h1><a href='/'>Back to Logger</a><br><br><table>";
    header += "<thead><tr><th>Lead I</th><th>Lead II</th><th>Lead III</th><th>AVR</th><th>AVL</th><th>AVF</th><th>V1</th><th>V2</th><th>V3</th><th>V4</th><th>V5</th><th>V6</th></tr></thead><tbody>";
    server.sendContent(header);

    if (!file || file.isDirectory() || file.size() == 0) {
        server.sendContent("<tr><td colspan='12'>File not found or is empty.</td></tr>");
    } else {
        // Read sample by sample (12-lead frame) and stream as HTML table rows
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


// Handles request to reset state machine and return to mode selection.
void handleReset() {
  nextMode = SELECT_MODE;
  server.send(200, "text/html", REFRESH_PAGE_HTML);
}

// Standard HTTP 404 handler for unrecognized routes.
void handleNotFound() {
  server.send(404, "text/plain", "404: Not Found");
}


// --- MODE STARTUP FUNCTIONS ---
// Initialization routines executed once upon entering a specific operating mode.

// Initialization for interactive mode selection.
void startSelectMode() {
  startAnimation();
  WiFi.mode(WIFI_AP); // Ensure SoftAP mode is active
  WiFi.softAP(ap_ssid, ap_password);
}

// Initialization for local AP binary streaming mode.
void startAPMode() {
  startAnimation();
  localDataServer.begin(); // Launch TCP socket server
  // Configure all ECG AFEs
  setup_ECG_Master();
  setup_ECG_Slave(PIN_SS_SLAVE1);
  setup_ECG_Slave(PIN_SS_SLAVE2);
  start_ECG_Conversion_All(); // Broadcast ADC conversion start across all chips
}

// Initialization for Cloud MQTT streaming mode.
void startCloudMode() {
  startAnimation();
  // Render status update on OLED
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

  // --- WiFiManager Configuration Sequence ---
  // 1. Temporarily pause main web server to prevent port conflicts with WiFiManager.
  Serial.println("Stopping main web server...");
  server.stop();

  // 2. Explicitly tear down SoftAP from previous mode and switch radio to STA mode.
  Serial.println("Disconnecting soft AP...");
  WiFi.softAPdisconnect(true);
  WiFi.mode(WIFI_STA);
  delay(500); // Allow WiFi radio state to stabilize
  Serial.println("WiFi mode set to STA.");

  WiFiManager wm;
  wm.setConfigPortalTimeout(180); // Captive portal times out after 3 minutes
  
  // 3. Launch WiFiManager portal (blocking call).
  Serial.println("Starting WiFiManager portal...");
  bool connected = wm.autoConnect(ap_ssid, ap_password);

  // 4. Restart primary web server regardless of outcome.
  Serial.println("Restarting main web server...");
  server.begin();

  // 5. Evaluate WiFi connection outcome.
  if (!connected) {
    Serial.println("Failed to connect via WiFiManager and hit timeout.");
    nextMode = SELECT_MODE; // Revert to mode selection if connection fails
    return; // Abort remaining cloud mode initialization
  }
  
  Serial.println("WiFi connected successfully!");
  // Synchronize system clock with NTP server
  if (!syncTimeWithNTP()) {
      Serial.println("FATAL: Could not sync time. Rebooting in 5 seconds..."); 
      delay(5000); 
      ESP.restart(); // Precise timestamps are mandatory; restart MCU if sync fails
  }
  // Configure MQTT client broker and initialize all ECG AFEs
  mqttClient.setServer(mqtt_broker, mqtt_port);
  setup_ECG_Master();
  setup_ECG_Slave(PIN_SS_SLAVE1);
  setup_ECG_Slave(PIN_SS_SLAVE2);
  start_ECG_Conversion_All();
}

// Initialization for standalone SD Card logging mode.
void startSDCardMode() {
  startAnimation();
  // SoftAP is enabled to host the control web server
  WiFi.mode(WIFI_AP);
  WiFi.softAP(ap_ssid, ap_password);
  delay(500);
  Serial.println("Initializing SD card for logging mode...");
  if (!SD.begin(PIN_CS_SD)) {
    Serial.println("Card Mount Failed!");
    display.clearDisplay(); display.setCursor(0,0);
    display.println("SD Card Mount Failed!"); display.display();
    delay(2000); 
    nextMode = SELECT_MODE; // Revert to mode selection if SD mount fails
    return;
  }
  Serial.println("SD card initialized.");
  strcpy(fileName, "No file selected");
  // Setup ECG AFEs
  setup_ECG_Master();
  setup_ECG_Slave(PIN_SS_SLAVE1);
  setup_ECG_Slave(PIN_SS_SLAVE2);
  start_ECG_Conversion_All();
}


// --- MODE LOOP FUNCTIONS ---
// Mode-specific worker functions executed repeatedly in loop().

// Service loop for local AP TCP streaming.
void loopAPMode() {
  // Accept new incoming TCP client connection if socket is free
  if (!localDataClient.connected()) {
    localDataClient = localDataServer.available();
  }
  // DRDYB goes LOW when new synchronized ECG sample is ready
  if (digitalRead(PIN_DRDYB) == LOW) {
    // Acquire differential leads from Master and precordial leads from Slaves
    lead_I = getValFromChannel(PIN_SS_MASTER, 1); lead_II = getValFromChannel(PIN_SS_MASTER, 2);
    v1 = getValFromChannel(PIN_SS_SLAVE1, 1); v2 = getValFromChannel(PIN_SS_SLAVE1, 2); v3 = getValFromChannel(PIN_SS_SLAVE1, 3);
    v4 = getValFromChannel(PIN_SS_SLAVE2, 1); v5 = getValFromChannel(PIN_SS_SLAVE2, 2);
    v6 = getValFromChannel(PIN_SS_SLAVE2, 3);
    // Calculate augmented limb leads
    lead_III = lead_II - lead_I; avr = -(lead_I + lead_II) / 2;
    avl = lead_I - (lead_II / 2); avf = lead_II - (lead_I / 2);
    // Dispatch framed binary packet over active TCP socket
    if (localDataClient.connected()) sendECGDataBinary(localDataClient);
  }
}

// Service loop for Cloud MQTT streaming.
void loopCloudMode() {
  // Maintain MQTT broker connection with auto-reconnect
  if (!mqttClient.connected()) {
    if (!mqttClient.connect(mqtt_client_id)) { delay(2000); return; }
  }
  mqttClient.loop();
  // Check if synchronized ECG sample is ready on DRDYB
  if (digitalRead(PIN_DRDYB) == LOW) {
    // Read all ECG channels from AFEs
    lead_I = getValFromChannel(PIN_SS_MASTER, 1); lead_II = getValFromChannel(PIN_SS_MASTER, 2);
    v1 = getValFromChannel(PIN_SS_SLAVE1, 1);
    v2 = getValFromChannel(PIN_SS_SLAVE1, 2); v3 = getValFromChannel(PIN_SS_SLAVE1, 3);
    v4 = getValFromChannel(PIN_SS_SLAVE2, 1); v5 = getValFromChannel(PIN_SS_SLAVE2, 2); v6 = getValFromChannel(PIN_SS_SLAVE2, 3);
    // Compute augmented limb leads
    lead_III = lead_II - lead_I; avr = -(lead_I + lead_II) / 2;
    avl = lead_I - (lead_II / 2);
    avf = lead_II - (lead_I / 2);
    // Assemble 12-lead array
    leads_data[0] = lead_I;   leads_data[1] = lead_II; leads_data[2] = lead_III; leads_data[3] = avr;
    leads_data[4] = avl;      leads_data[5] = avf; leads_data[6] = v1;       leads_data[7] = v2;
    leads_data[8] = v3;       leads_data[9] = v4;
    leads_data[10] = v5;      leads_data[11] = v6;
    // Pack 63-byte binary protocol packet with microsecond timestamp and packet counter
    struct timeval tv; gettimeofday(&tv, NULL);
    uint64_t timestamp_us = (uint64_t)tv.tv_sec * 1000000L + (uint64_t)tv.tv_usec;
    packet_counter++;
    uint8_t payload[63];
    payload[0] = STX;
    memcpy(&payload[1], &timestamp_us, 8); // 8-byte uint64 timestamp (microseconds)
    memcpy(&payload[9], &packet_counter, 4); // 4-byte uint32 packet counter
    memcpy(&payload[13], leads_data, 48);   // 48-byte int32[12] lead array
    uint8_t checksum = 0;
    for (int i = 1; i < 61; i++) checksum += payload[i]; // Compute 8-bit modular checksum
    payload[61] = checksum;
    payload[62] = ETX;
    // Publish binary packet to MQTT broker
    mqttClient.publish(mqtt_topic, payload, sizeof(payload));
  }
}

// Service loop for local microSD recording.
void loopSDCardMode() {
  // Check if recording is active and DRDYB indicates new sample ready
  if (isRecording && (digitalRead(PIN_DRDYB) == LOW)) {
    // Verify sample quota has not been exceeded
    if (sampleCount < MAX_SAMPLES) {
      sampleCount++;
      // Acquire all lead data and compute augmented leads
      lead_I = getValFromChannel(PIN_SS_MASTER, 1); lead_II = getValFromChannel(PIN_SS_MASTER, 2);
      v1 = getValFromChannel(PIN_SS_SLAVE1, 1); v2 = getValFromChannel(PIN_SS_SLAVE1, 2); v3 = getValFromChannel(PIN_SS_SLAVE1, 3);
      v4 = getValFromChannel(PIN_SS_SLAVE2, 1); v5 = getValFromChannel(PIN_SS_SLAVE2, 2); v6 = getValFromChannel(PIN_SS_SLAVE2, 3);
      lead_III = lead_II - lead_I;
      avr = -(lead_I + lead_II) / 2;
      avl = lead_I - (lead_II / 2);
      avf = lead_II - (lead_I / 2);
      // Assemble 12-lead array
      leads_data[0] = lead_I;   leads_data[1] = lead_II; leads_data[2] = lead_III; leads_data[3] = avr;
      leads_data[4] = avl;      leads_data[5] = avf; leads_data[6] = v1;       leads_data[7] = v2;
      leads_data[8] = v3;       leads_data[9] = v4;
      leads_data[10] = v5;      leads_data[11] = v6;
      // Write 48-byte raw binary lead block to file
      if (dataFile) {
          dataFile.write((const uint8_t*)leads_data, sizeof(leads_data));
      }
    } else {
      // Automatically terminate and close file once sample quota is reached
      isRecording = false;
      dataFile.close();
      Serial.println("\nRecording finished automatically. File saved.");
    }
  }
}

// --- DATA SENDING FUNCTIONS ---

// Serializes 12-lead ECG sample into JSON payload and publishes to MQTT (debug/legacy alternative).
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

// Frames and writes a 51-byte binary packet (STX + 48-byte leads + Checksum + ETX) to a stream (TCP socket).
void sendECGDataBinary(Stream &port) {
  // Assemble 12-lead array
  leads_data[0] = lead_I;   leads_data[1] = lead_II;
  leads_data[2] = lead_III; leads_data[3] = avr;
  leads_data[4] = avl;      leads_data[5] = avf; leads_data[6] = v1;       leads_data[7] = v2;
  leads_data[8] = v3;       leads_data[9] = v4; leads_data[10] = v5;      leads_data[11] = v6;
  // Compute 8-bit checksum across 48 bytes of lead data
  uint8_t checksum = 0;
  uint8_t* data_ptr = (uint8_t*)leads_data;
  for (int i = 0; i < sizeof(leads_data); i++) {
    checksum += data_ptr[i];
  }
  // Transmit framed packet: STX | DATA | CHECKSUM | ETX
  port.write(STX);
  port.write((uint8_t*)leads_data, sizeof(leads_data));
  port.write(checksum);
  port.write(ETX);
}


// --- HELPER & HARDWARE FUNCTIONS ---

// Reads battery analog voltage via ADC pin and applies voltage divider factor.
float readBatteryVoltage() {
  int batteryLevel = analogRead(BATTERY_PIN);
  // Convert ADC reading (0-4095) to reference voltage (0-3.3V)
  float rawVoltage = batteryLevel * (3.3 / 4095.0);
  // Multiply by voltage divider scale factor (R1+R2)/R2 to obtain actual pack voltage
  float batteryVoltage = rawVoltage * 3.24; 
  return batteryVoltage;
}

// Converts battery pack voltage into battery percentage (0-100%).
int calculateBatteryPercentage(float batteryVoltage) {
  float minVoltage = 6.40; // Minimum voltage at 0% discharge
  float maxVoltage = 8.40; // Maximum voltage at 100% full charge (for 2S Li-ion battery pack)
  // Linear interpolation between voltage limits
  int percentage = map(batteryVoltage * 100, minVoltage * 100, maxVoltage * 100, 0, 100);
  // Clamp percentage between 0% and 100%
  return constrain(percentage, 0, 100);
}

// Renders pulsating heart graphic on OLED display during initialization.
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
  // Draw heart shape using two circles and a triangle
  display.fillCircle(xCenter - radius, yCenter - radius / 2, radius, SSD1306_WHITE);
  display.fillCircle(xCenter + radius, yCenter - radius / 2, radius, SSD1306_WHITE);
  display.fillTriangle(xCenter - 2 * radius, yCenter - radius / 2,
                       xCenter + 2 * radius, yCenter - radius / 2,
                       xCenter, yCenter + bottomHeight, SSD1306_WHITE);
  display.display();
}

// Executes startup splash screen animation on OLED display.
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
  // Pulsating heart animation sequence
  for (int i = 0; i < 2; i++) {
    for (float scale = 1.0; scale <= 1.3; scale += 0.05) { drawHeart(scale); delay(80); }
    for (float scale = 1.3; scale >= 1.0; scale -= 0.05) { drawHeart(scale); delay(80); }
  }
}

// --- ECG HARDWARE FUNCTIONS ---
// Low-level SPI interface and register configuration functions for ADS1293 AFEs.

/**
 * @brief Asserts Chip Select for a specific ECG AFE.
 * @param chip_SS GPIO pin corresponding to the target chip's Slave Select.
 */
void selectChip(int chip_SS) {
  digitalWrite(PIN_SS_MASTER, HIGH);
  digitalWrite(PIN_SS_SLAVE1, HIGH);
  digitalWrite(PIN_SS_SLAVE2, HIGH);
  digitalWrite(chip_SS, LOW);
}

/**
 * @brief Deasserts all ECG AFE Chip Select lines (sets HIGH).
 */
void deselectAllChips() {
  digitalWrite(PIN_SS_MASTER, HIGH);
  digitalWrite(PIN_SS_SLAVE1, HIGH);
  digitalWrite(PIN_SS_SLAVE2, HIGH);
}

/**
 * @brief Reads a single 8-bit register from an ECG AFE over HSPI.
 * @param chip_SS Target chip Slave Select GPIO.
 * @param reg Register address (MSB will be set to 1 for read).
 * @return Byte read from the register.
 */
byte readRegister(int chip_SS, byte reg) {
  reg |= 0x80; // Set MSB for SPI read operation
  hspi.beginTransaction(SPISettings(spiClk, MSBFIRST, SPI_MODE0));
  selectChip(chip_SS);
  hspi.transfer(reg); // Send register address
  byte data = hspi.transfer(0); // Clock in response byte
  deselectAllChips();
  hspi.endTransaction();
  return data;
}

/**
 * @brief Writes a single 8-bit value to an ECG AFE register over HSPI.
 * @param chip_SS Target chip Slave Select GPIO.
 * @param reg Register address (MSB must be 0 for write).
 * @param data Byte payload to write.
 */
void writeRegister(int chip_SS, byte reg, byte data) {
  reg &= 0x7F; // Clear MSB for SPI write operation
  hspi.beginTransaction(SPISettings(spiClk, MSBFIRST, SPI_MODE0));
  selectChip(chip_SS);
  hspi.transfer(reg); // Send register address
  hspi.transfer(data); // Send data byte
  deselectAllChips();
  hspi.endTransaction();
}

/**
 * @brief Reads a 24-bit ECG channel sample and sign-extends to 32 bits.
 * @param chip_SS Target chip Slave Select GPIO.
 * @param channel_num_on_chip Channel index (1, 2, or 3).
 * @return Signed 32-bit integer representing two's complement ADC counts.
 */
int32_t getValFromChannel(int chip_SS, int channel_num_on_chip) {
  byte r1, r2, r3;
  switch (channel_num_on_chip) {
    case 1: r1 = 0x37; r2 = 0x38; r3 = 0x39; break;
    case 2: r1 = 0x3A; r2 = 0x3B; r3 = 0x3C; break;
    case 3: r1 = 0x3D; r2 = 0x3E; r3 = 0x3F; break;
    default: return 0;
  }
  int32_t val = ((int32_t)readRegister(chip_SS, r1) << 16) |
                 ((int32_t)readRegister(chip_SS, r2) << 8)  |
                 (int32_t)readRegister(chip_SS, r3);
  
  // 24-bit sign extension
  if (val & 0x00800000) {
    val |= 0xFF000000;
  }
  return val;
}

/**
 * @brief Configures register map for the Master ECG AFE (Leads I and II).
 * 
 * Sets internal clock master, 853 SPS decimation factor, Wilson Central
 * Terminal (WCT), Right Leg Drive (RLD), and maps DRDYB interrupt.
 */
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

/**
 * @brief Configures register map for a Slave ECG AFE (Precordial leads V1-V6).
 * @param chip_SS Target Slave Select GPIO (PIN_SS_SLAVE1 or PIN_SS_SLAVE2).
 * 
 * Configures slave clock input from master and disables individual DRDYB interrupts.
 */
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

/**
 * @brief Broadcasts synchronous conversion start command (0x01 to reg 0x00) across all AFEs.
 */
void start_ECG_Conversion_All() {
  writeRegister(PIN_SS_MASTER, 0x00, 0x01);
  writeRegister(PIN_SS_SLAVE1, 0x00, 0x01);
  writeRegister(PIN_SS_SLAVE2, 0x00, 0x01);
}
