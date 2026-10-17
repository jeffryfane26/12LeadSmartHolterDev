# Binary Telemetry Protocol Specification

This document provides the byte-level specification for the binary transport protocol used by the **12-Lead Smart Holter System**.

---

## 1. Protocol Rationale

Electrocardiogram monitoring at clinical diagnostic quality requires continuous sampling rates ($F_s = 853\text{ Hz}$). Transmitting 12 leads of 24-bit data in ASCII-based representations (e.g., JSON or CSV) creates severe network inefficiencies:

* **JSON Inefficiency**: A single 12-lead JSON packet averages $\sim 350\text{ bytes}$. At $853\text{ SPS}$, this requires $\sim 298.5\text{ KB/s}$ ($2.39\text{ Mbps}$) per active device, overwhelming public MQTT brokers and cellular modems.
* **Binary Efficiency**: The packed binary framing protocol consumes only **63 bytes** per sample ($\sim 53.7\text{ KB/s}$ or $430\text{ Kbps}$), reducing bandwidth and broker parsing overhead by over **82%**.

---

## 2. 63-Byte Cloud Telemetry Protocol (Metrics Enabled)

Used in `CLOUD_MODE` for transmission over MQTT to broker topic `ecg/{device_id}/data`.

```
 +-------+------------------------+-------------------+------------------------------------------+----------+-------+
 | STX   | Timestamp (microsec)   | Packet Counter    | 12-Lead ECG Data                         | Checksum | ETX   |
 | 1 Byte| 8 Bytes (uint64_t)     | 4 Bytes (uint32_t)| 48 Bytes (12 x int32_t)                  | 1 Byte   | 1 Byte|
 +-------+------------------------+-------------------+------------------------------------------+----------+-------+
 Byte 0   Byte 1            Byte 8 Byte 9      Byte 12 Byte 13                            Byte 60 Byte 61    Byte 62
```

### Detailed Byte Layout Table

| Byte Offset | Field Name | Data Type | Endianness | Value / Range | Description |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `0` | `STX` | `uint8_t` | - | `0x02` | ASCII Start of Text delimiter |
| `1 – 8` | `timestamp_us`| `uint64_t` | Little-Endian | $0 \text{ to } 2^{64}-1$ | POSIX timestamp in microseconds derived from NTP |
| `9 – 12` | `packet_counter`| `uint32_t`| Little-Endian | $0 \text{ to } 2^{32}-1$ | Monotonically increasing sequence number for QoS tracking |
| `13 – 16`| `lead_I` | `int32_t` | Little-Endian | $\pm 8{,}388{,}607$ | Raw ADC counts for Limb Lead I |
| `17 – 20`| `lead_II` | `int32_t` | Little-Endian | $\pm 8{,}388{,}607$ | Raw ADC counts for Limb Lead II |
| `21 – 24`| `lead_III` | `int32_t` | Little-Endian | $\pm 8{,}388{,}607$ | Derived Lead III ($II - I$) |
| `25 – 28`| `avr` | `int32_t` | Little-Endian | $\pm 8{,}388{,}607$ | Derived Augmented Lead aVR ($-(I + II) / 2$) |
| `29 – 32`| `avl` | `int32_t` | Little-Endian | $\pm 8{,}388{,}607$ | Derived Augmented Lead aVL ($I - II / 2$) |
| `33 – 36`| `avf` | `int32_t` | Little-Endian | $\pm 8{,}388{,}607$ | Derived Augmented Lead aVF ($II - I / 2$) |
| `37 – 40`| `v1` | `int32_t` | Little-Endian | $\pm 8{,}388{,}607$ | Precordial Lead V1 |
| `41 – 44`| `v2` | `int32_t` | Little-Endian | $\pm 8{,}388{,}607$ | Precordial Lead V2 |
| `45 – 48`| `v3` | `int32_t` | Little-Endian | $\pm 8{,}388{,}607$ | Precordial Lead V3 |
| `49 – 52`| `v4` | `int32_t` | Little-Endian | $\pm 8{,}388{,}607$ | Precordial Lead V4 |
| `53 – 56`| `v5` | `int32_t` | Little-Endian | $\pm 8{,}388{,}607$ | Precordial Lead V5 |
| `57 – 60`| `v6` | `int32_t` | Little-Endian | $\pm 8{,}388{,}607$ | Precordial Lead V6 |
| `61` | `checksum` | `uint8_t` | - | `0x00 to 0xFF` | 8-bit modular checksum of payload bytes `1` through `60` |
| `62` | `ETX` | `uint8_t` | - | `0x03` | ASCII End of Text delimiter |

### Checksum Verification Algorithm
The checksum is calculated over all bytes between `STX` and `checksum` (indices `1` to `60` inclusive):
$$\text{Checksum} = \left(\sum_{i=1}^{60} \text{Payload}[i]\right) \pmod{256}$$

In Python (`ecg_app_MDPM.py`):
```python
data_bytes = payload[1:61]
received_checksum = payload[61]
calculated_checksum = sum(data_bytes) & 0xFF
if received_checksum != calculated_checksum:
    # Discard corrupted packet
    return
```

In C++ (`FINAL_APCLSD.ino`):
```cpp
uint8_t checksum = 0;
for (int i = 1; i < 61; i++) {
    checksum += payload[i];
}
payload[61] = checksum;
```

---

## 3. 51-Byte Legacy / Local AP Protocol

Used in `AP_MODE` over the direct TCP socket (port `8888`), or by legacy firmware iterations that omit telemetry counters.

```
 +-------+------------------------------------------+----------+-------+
 | STX   | 12-Lead ECG Data                         | Checksum | ETX   |
 | 1 Byte| 48 Bytes (12 x int32_t)                  | 1 Byte   | 1 Byte|
 +-------+------------------------------------------+----------+-------+
 Byte 0   Byte 1                            Byte 48 Byte 49    Byte 50
```

| Byte Offset | Field Name | Data Type | Endianness | Description |
| :--- | :--- | :--- | :--- | :--- |
| `0` | `STX` | `uint8_t` | - | ASCII Start of Text (`0x02`) |
| `1 – 48` | `leads_data` | `int32_t[12]` | Little-Endian | 12 leads $\times$ 4 bytes (I, II, III, aVR, aVL, aVF, V1..V6) |
| `49` | `checksum` | `uint8_t` | - | 8-bit sum of bytes 1 to 48 ($\sum \pmod{256}$) |
| `50` | `ETX` | `uint8_t` | - | ASCII End of Text (`0x03`) |

### Struct Unpack Directives (Python `struct`)
* **63-byte Format**: `struct.unpack('<QI12i', payload[1:61])`
  * `<`: Little-Endian
  * `Q`: `unsigned long long` (uint64 timestamp, 8 bytes)
  * `I`: `unsigned int` (uint32 counter, 4 bytes)
  * `12i`: 12 $\times$ `signed int` (int32 lead values, 48 bytes)
* **51-byte Format**: `struct.unpack('<12i', payload[1:49])`
  * `<`: Little-Endian
  * `12i`: 12 $\times$ `signed int` (int32 lead values, 48 bytes)

---

## 4. QoS & Telemetry Metrics Calculation

The 63-byte packet enables real-time network quality assessment in the backend:

### 4.1 Transmission Latency
Calculated upon MQTT packet reception:
$$\text{Latency (ms)} = \frac{T_{\text{server\_us}} - T_{\text{packet\_us}}}{1000.0}$$
Where $T_{\text{server\_us}} = \frac{\text{time.time\_ns}()}{1000}$.

### 4.2 Rolling Average Latency & Jitter
The backend stores the last 100 latency observations in a rolling deque (`maxlen=100`):
$$\bar{L} = \frac{1}{N} \sum_{k=1}^{N} L_k$$
$$\text{Jitter (ms)} = \sigma = \sqrt{\frac{1}{N-1} \sum_{k=1}^{N} (L_k - \bar{L})^2}$$

### 4.3 Packet Loss Estimation
The sequence number (`packet_counter`) detects lost network packets:
$$\text{If } C_{\text{curr}} > C_{\text{last}} + 1 \implies \Delta_{\text{lost}} = C_{\text{curr}} - (C_{\text{last}} + 1)$$
$$\text{Packet Loss Rate (\%)} = \left(\frac{\text{Lost Packets}}{\text{Total Expected Packets}}\right) \times 100$$

