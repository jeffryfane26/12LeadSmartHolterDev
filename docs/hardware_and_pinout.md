# Hardware Architecture & Pinout Specification

This document details the hardware configuration, pin mappings, SPI/I2C communication topologies, AFE register configurations, and lead calculation equations for the **12-Lead Smart Holter System**.

---

## 1. Hardware Bill of Materials (BOM)

| Subsystem | Component | Key Specifications | Interface |
| :--- | :--- | :--- | :--- |
| **Main MCU** | ESP32-WROOM-32 / DevKit V1 | Dual-core Tensilica Xtensa 32-bit LX6 @ 240 MHz, 520 KB SRAM, 2.4 GHz Wi-Fi | Master Controller |
| **ECG AFE (x3)** | Low-Power 3-Channel 24-bit ECG AFE (ADS1293 architecture) | Cascaded Master/Slave, high-impedance instrumentation amps, 853 SPS | HSPI (Shared Bus, Dedicated CS) |
| **Display** | 0.96" Monochrome OLED (SSD1306) | 128 $\times$ 64 pixels, $I^2C$ address `0x3C` | $I^2C$ (Default Wire) |
| **Storage** | MicroSD Card Module | FAT32 file system, binary packet logging | VSPI (Shared bus, CS on GPIO 15) |
| **Power Supply** | 2S Li-ion Battery Pack | 7.4V nominal (6.4V cutoff, 8.4V full charge) | Resistor Voltage Divider to GPIO 34 |

---

## 2. Complete ESP32 GPIO Pinout Matrix

```
                          ESP32-WROOM-32 Pinout
                             +---------------+
                       3V3 --| 1          38 |-- GND
                        EN --| 2          37 |-- GPIO 23 (VSPI MOSI - MicroSD)
          (ADC1_CH6) GPIO 36 --| 3          36 |-- GPIO 22 (I2C SCL - OLED)
          (ADC1_CH7) GPIO 39 --| 4          35 |-- GPIO 21 (I2C SDA - OLED)
          (ADC1_CH6) GPIO 34 --| 5          34 |-- GPIO 1  (U0TXD)
                     GPIO 35 --| 6          33 |-- GPIO 3  (U0RXD)
      (PIN_SS_MASTER) GPIO 32 --| 7          32 |-- GPIO 19 (VSPI MISO - MicroSD)
      (PIN_SS_MASTER) GPIO 33 --| 8          31 |-- GPIO 18 (VSPI SCK - MicroSD)
          (PIN_DRDYB) GPIO 25 --| 9          30 |-- GPIO 5
                     GPIO 26 --| 10         29 |-- GPIO 4  (PIN_SS_SLAVE1)
                     GPIO 27 --| 11         28 |-- GPIO 2  (PIN_SS_SLAVE2)
        (PIN_CS_SD) GPIO 14 --| 12         27 |-- GPIO 15 (PIN_CS_SD)
      (PIN_SCLK_ECG) GPIO 12 --| 13         26 |-- GPIO 8
      (PIN_MISO_ECG) GPIO 13 --| 14         25 |-- GPIO 7
      (PIN_MOSI_ECG) GPIO 9  --| 15         24 |-- GPIO 6
                     GPIO 10 --| 16         23 |-- GPIO 11
                     GPIO 11 --| 17         22 |-- GPIO 12 (PIN_MISO_ECG)
                     VIN    --| 18         21 |-- GPIO 13 (PIN_MOSI_ECG)
                     GND    --| 19         20 |-- GPIO 14 (PIN_SCLK_ECG)
                             +---------------+
```

### Detailed Functional Table

| ESP32 Pin | Signal Name in Code | Connected Peripheral | Direction | Logic Level | Description |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **GPIO 14** | `PIN_SCLK_ECG` | ECG AFE Master + Slaves | Output | 3.3V | HSPI Clock (1.0 MHz, Mode 0) |
| **GPIO 12** | `PIN_MISO_ECG` | ECG AFE Master + Slaves | Input | 3.3V | HSPI Master In, Slave Out |
| **GPIO 13** | `PIN_MOSI_ECG` | ECG AFE Master + Slaves | Output | 3.3V | HSPI Master Out, Slave In |
| **GPIO 25** | `PIN_DRDYB` | Master AFE Data Ready | Input | 3.3V Active LOW | Triggers conversion read loop when low |
| **GPIO 33** | `PIN_SS_MASTER` | ECG AFE Master | Output | 3.3V Active LOW | Chip Select for Master (Leads I, II) |
| **GPIO 4** | `PIN_SS_SLAVE1` | ECG AFE Slave 1 | Output | 3.3V Active LOW | Chip Select for Slave 1 (V1, V2, V3) |
| **GPIO 2** | `PIN_SS_SLAVE2` | ECG AFE Slave 2 | Output | 3.3V Active LOW | Chip Select for Slave 2 (V4, V5, V6) |
| **GPIO 21** | Wire SDA | SSD1306 OLED | Bi-dir | 3.3V | $I^2C$ Serial Data |
| **GPIO 22** | Wire SCL | SSD1306 OLED | Output | 3.3V | $I^2C$ Serial Clock |
| **GPIO 15** | `PIN_CS_SD` | MicroSD Module | Output | 3.3V Active LOW | Chip Select for VSPI MicroSD Bus |
| **GPIO 34** | `BATTERY_PIN` | Battery Voltage Divider | Input | Analog (0–3.3V) | ADC1 channel 6 voltage readout |

---

## 3. ECG Front-End Architecture & Lead Derivation

The 12-lead ECG standard requires 12 simultaneous electrical perspectives of the cardiac vector. To minimize hardware footprint, the system acquires **8 independent physical channels** and mathematically derives the remaining **4 limb leads**:

```
+----------------------------------------------------------------------------------------+
|                                12-LEAD DERIVATION SCHEME                               |
+----------------------------------------------------------------------------------------+
| Chip Allocation  | Channel   | Physical Leads Captured | Derived Leads (Einthoven/Goldberger) |
+------------------+-----------+-------------------------+-------------------------------+
| Master (SS: 33)  | Ch 1      | Lead I                  |                               |
|                  | Ch 2      | Lead II                 | Lead III = Lead II - Lead I   |
|                  |           |                         | aVR = - (Lead I + Lead II) / 2|
|                  |           |                         | aVL = Lead I - (Lead II / 2)  |
|                  |           |                         | aVF = Lead II - (Lead I / 2)  |
+------------------+-----------+-------------------------+-------------------------------+
| Slave 1 (SS: 4)  | Ch 1      | V1                      | (Direct physical readout)     |
|                  | Ch 2      | V2                      | (Direct physical readout)     |
|                  | Ch 3      | V3                      | (Direct physical readout)     |
+------------------+-----------+-------------------------+-------------------------------+
| Slave 2 (SS: 2)  | Ch 1      | V4                      | (Direct physical readout)     |
|                  | Ch 2      | V5                      | (Direct physical readout)     |
|                  | Ch 3      | V6                      | (Direct physical readout)     |
+----------------------------------------------------------------------------------------+
```

### Mathematical Definitions

$$\begin{aligned}
\text{Lead III} &= \text{Lead II} - \text{Lead I} \\
aVR &= -\frac{\text{Lead I} + \text{Lead II}}{2} \\
aVL &= \text{Lead I} - \frac{\text{Lead II}}{2} \\
aVF &= \text{Lead II} - \frac{\text{Lead I}}{2}
\end{aligned}$$

---

## 4. SPI Protocol & 24-Bit Sign Extension

Communication with the AFE occurs over HSPI at $1\text{ MHz}$ in **SPI Mode 0** ($\text{CPOL}=0, \text{CPHA}=0$), MSB First.

### Read/Write Operations
* **Read**: Register address is OR'd with `0x80` (`reg |= 0x80`).
* **Write**: Register address is AND'd with `0x7F` (`reg &= 0x7F`).

### 24-Bit Two's Complement Unpacking
Each channel produces a 24-bit integer spread across 3 consecutive 8-bit registers:
* Channel 1: Registers `0x37`, `0x38`, `0x39`
* Channel 2: Registers `0x3A`, `0x3B`, `0x3C`
* Channel 3: Registers `0x3D`, `0x3E`, `0x3F`

Because standard C++ integer types are 32-bit (`int32_t`), sign extension must be performed manually:
```cpp
int32_t val = ((int32_t)r1 << 16) | ((int32_t)r2 << 8) | (int32_t)r3;
if (val & 0x00800000) {  // Check bit 23 (sign bit)
    val |= 0xFF000000;   // Sign-extend to 32 bits
}
```

---

## 5. AFE Register Configuration Maps

### Master Chip Initialization (`setup_ECG_Master()`)
| Register | Value (Hex) | Purpose |
| :--- | :--- | :--- |
| `0x00` | `0x00` | Stop conversions and reset configuration |
| `0x01` | `0x11` | Input multiplexer configuration for Channel 1 |
| `0x02` | `0x19` | Input multiplexer configuration for Channel 2 |
| `0x0A` | `0x07` | Common mode detect and Wilson Central Terminal (WCT) routing |
| `0x0C` | `0x04` | Lead-off detection current sources |
| `0x0D` | `0x01` | Right Leg Drive (RLD) reference amplifier control |
| `0x0E` | `0x02` | WCT amplifier positive feedback |
| `0x0F` | `0x03` | Reference buffer bandwidth and internal reference enable |
| `0x12` | `0x01` $\rightarrow$ `0x05` | Oscillator mode: internal crystal clock source, output clock enabled |
| `0x14` | `0x24` | Decimation factor setting for $853\text{ SPS}$ output data rate |
| `0x21` | `0x02` | Channel 1 decimation filter enable |
| `0x22` | `0x02` | Channel 2 decimation filter enable |
| `0x23` | `0x02` | Channel 3 decimation filter enable |
| `0x27` | `0x08` | DRDYB pin interrupt mapping |
| `0x28` | `0x08` | Error interrupt masking |
| `0x2F` | `0x30` | Analog front-end power-down and bias current settings |

### Slave Chip Initialization (`setup_ECG_Slave()`)
| Register | Value (Hex) | Purpose |
| :--- | :--- | :--- |
| `0x00` | `0x00` | Stop conversions |
| `0x01` | `0x0C` | Channel 1 precordial multiplexer configuration |
| `0x02` | `0x14` | Channel 2 precordial multiplexer configuration |
| `0x03` | `0x1C` | Channel 3 precordial multiplexer configuration |
| `0x12` | `0x02` $\rightarrow$ `0x06` | External clock slave mode (driven by Master clock) |
| `0x21`–`0x24` | `0x02` | Decimation filters enabled across all precordial channels |
| `0x27` | `0x00` | DRDYB disabled on slaves (Master acts as single interrupt sync) |
| `0x28` | `0x40` | Slave sync and clock input routing |
| `0x2F` | `0x70` | Low-power operating mode |

### Simultaneous Conversion Start
Writing `0x01` to register `0x00` on Master, Slave 1, and Slave 2 synchronously launches the internal $\Delta\Sigma$ modulators:
```cpp
writeRegister(PIN_SS_MASTER, 0x00, 0x01);
writeRegister(PIN_SS_SLAVE1, 0x00, 0x01);
writeRegister(PIN_SS_SLAVE2, 0x00, 0x01);
```

---

## 6. Battery Monitoring Subsystem

The device is powered by a 2-cell Lithium-Ion battery pack ($7.4\text{V}$ nominal). A precision resistor voltage divider steps down the voltage to within the ESP32 ADC safe range ($0\text{--}3.3\text{V}$):

```
       V_BATTERY (6.4V - 8.4V)
           +
           |
          [R1] = 220 kOhm
           |
           +-----> GPIO 34 (ADC1_CH6)
           |
          [R2] = 100 kOhm
           |
          GND
```

* **Divider Ratio**:
  $$\text{Ratio} = \frac{R_1 + R_2}{R_2} = \frac{220\text{ k}\Omega + 100\text{ k}\Omega}{100\text{ k}\Omega} \approx 3.24 \pm 1\%$$
* **Voltage Calculation**:
  $$V_{\text{raw}} = \text{analogRead}(34) \times \left(\frac{3.3\text{ V}}{4095}\right)$$
  $$V_{\text{battery}} = V_{\text{raw}} \times 3.24$$
* **State of Charge (SoC)**:
  Linearly mapped between $6.40\text{V}$ ($0\%$, safe low cutoff) and $8.40\text{V}$ ($100\%$, fully charged 2S pack).

