# Wire protocol

All multi-byte fields are little-endian, structs are packed. Sources of truth:
`firmware/car_link_rx/car_link_rx.ino` (`DrivePacket`, `TelemetryPacket`) and
`rccar/protocol.py`. Change both together.

## Control: laptop → car (UDP 4210, 50 Hz)

`struct.pack("<BBIhhHHBB")`, 16 bytes.

| Field | Type | Range / meaning |
|---|---|---|
| magic | u8 | `0xA5` |
| version | u8 | `1` |
| seq | u32 | increments per packet; gaps count as `lost` on the car |
| steer | i16 | −1000 (left) … +1000 (right) |
| drive | i16 | −1000 … +1000 (negative only in reverse gears) |
| brake | u16 | 0 … 1000 |
| handbrake | u16 | 0 … 1000 |
| gear | u8 | 0 N · 1–5 forward · 6 R (0.5×) · 7 r2 (1.0×) |
| flags | u8 | `0x01` HB button · `0x02` trim left · `0x04` trim right · `0x08` trim reset |

The car treats any valid packet as "the controller is here": it resets the
failsafe timer and sends telemetry back to that packet's source address/port.
An **all-zero** packet (gear 0, no input) is the neutral packet. It's used when
stopping, when the wheel is unplugged, and as the **discovery probe**.

## Telemetry: car → laptop (UDP, 10 Hz, to the last sender)

`struct.pack("<BBIBBBBhhHHhHIbBH")`, 30 bytes, protocol version 3.

| Field | Type | Meaning |
|---|---|---|
| magic | u8 | `0x5A` |
| version | u8 | `3` |
| seq | u32 | telemetry sequence |
| gear | u8 | echo of the commanded gear |
| drvState | u8 | 0 neutral · 1 forward · 2 braking · 3 blocked ("x rev-armed") · 4 reverse |
| linkLive | u8 | 1 = receiving control packets (not in failsafe) |
| flags | u8 | echo of the last flags |
| steer | i16 | echo of the commanded steering |
| escOff | i16 | actual ESC output, µs from neutral (±500) |
| brake | u16 | echo |
| handbrake | u16 | echo |
| trimUs | i16 | steering trim, µs |
| pps | u16 | control packets received in the last second |
| lost | u32 | cumulative missed control packets (resets on reboot) |
| rssi | i8 | car WiFi RSSI in dBm, 0 = not connected (v3) |
| resetReason | u8 | `esp_reset_reason()` of the last boot (v3) |
| freeHeapKb | u16 | free heap in KB (v3). A steady decline would mean a leak. |

History: v3 (June 2026) added `rssi`, `resetReason` and `freeHeapKb` to v1.

## Laptop-internal: reader → server (UDP 127.0.0.1:5055)

- Raw telemetry datagrams, forwarded unchanged.
- JSON status once a second: `{"type":"car","state":"live|failsafe|connecting|searching",
  "ip":…, "via":…, "wheel":bool, "netError":…, "search":…}`.

## Browser ↔ server (WebSocket `/ws`, JSON)

| type | from → to | purpose |
|---|---|---|
| `hello` `{role: phone\|viewer}` | browser → server | register. When both are present the server sends `start` to the phone. |
| `start` | server → phone | create a fresh WebRTC offer |
| `offer` / `answer` / `ice` | phone ↔ viewer (relayed) | WebRTC signaling |
| `restart` | viewer → server | cockpit wants a new video connection (failed / frozen) → `start` |
| `telemetry` | server → all | decoded car telemetry (drives the HUD and the phone's camera flip) |
| `status` | server → viewer | phone connected?, car status, phone URLs for the QR code |
| `cam` / `camcaps` | viewer ↔ phone | zoom/torch control and capabilities |
| `diag` `{msg}` | browser → server | printed in the launcher console (`[phone] …`, `[viewer] …`) |

## mDNS

- `rc-car.local` A record (ArduinoOTA, all firmware versions).
- `_rccar._udp` service on port 4210 with TXT `fw=<version>`, `proto=1`
  (firmware ≥ 2.0.0).

## OTA

ArduinoOTA on TCP/UDP 3232 (espota protocol), optional password. `tools/flash.py`.
