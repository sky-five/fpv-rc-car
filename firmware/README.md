# Firmware (ESP32, in the car)

`car_link_rx/car_link_rx.ino` runs **in the car**. It receives 50 Hz control
packets from the laptop over UDP, drives the steering servo and ESC, runs a
failsafe, renders a 128×64 OLED dashboard, and sends telemetry back.

> **⚠️ This board's USB port is dead. OTA over WiFi is the only way to flash it.**
> Anything that breaks WiFi or OTA would brick the car, so firmware 2.x carries
> rollback protection (below). Keep it in every future version, and always flash
> with `tools/flash.py`.

## Flashing (OTA)

From the repo root:

```bash
python tools/flash.py              # build, find the car, confirm, upload, verify
python tools/flash.py --build-only # compile only — nothing is sent
```

It compiles with the `arduino-cli` bundled in Arduino IDE 2, using the exact
board settings the car was first flashed with. Then it finds the car (same
discovery as the wheel reader), uploads with `espota`, waits for the car to come
back, and reports the firmware version it's running.

Before the first flash from this repo, create `car_link_rx/secrets.h` from
`secrets.h.example` with the **same** networks the car uses today. The original
lives in the old Arduino sketch folder (`Documents/Arduino/sketch_jun14a/secrets.h`).

### Rollback protection (firmware ≥ 2.0.0)

The ESP32 Arduino core's prebuilt bootloader has
`CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y`, and the bootloader on the car (flashed
over USB in June 2026, ESP-IDF 5.5.4) was built with it. So:

1. A newly flashed image boots as **PENDING_VERIFY**.
2. The sketch overrides `verifyRollbackLater()` so the core doesn't approve it
   automatically, and approves it itself (`esp_ota_mark_app_valid_cancel_rollback`)
   only once WiFi is connected, OTA is listening, and the loop has run for
   `ROLLBACK_CONFIRM_MS` (20 s).
3. If it resets before that (crash, watchdog, brownout), the bootloader boots the
   previous image. If it hangs, or can't join WiFi, a one-shot `esp_timer` forces
   the rollback at `ROLLBACK_DEADLINE_MS` (120 s). That timer runs on its own
   task, so it fires even if `setup()`/`loop()` is stuck.

The previous image is by definition one that could receive an OTA, so you can
always flash again. Verified at the binary level: in the linked ELF,
`verifyRollbackLater` is the sketch's strong symbol (returns 1), and
`initArduino()` calls it before its auto-approve path.

The OLED shows `new fw: verifying` during the first boot of a new image.

**Firmware 1.x** (on the car until 2.0.0 is flashed) has no rollback, so the
**first** flash of 2.0.0 is unprotected. It's the same code as 1.x plus the
rollback guard, the mDNS service record and the boot-screen version, and it
compiles with no warnings. Every flash after that is protected.

### OTA password (optional)

OTA is open to anyone on the WiFi by default. To require a password, add
`#define OTA_PASSWORD "..."` to `secrets.h`. `tools/flash.py` reads it and passes it
to espota. The car picks up the new password from the flash that includes it.

## Board & toolchain

- **Board:** ESP32 (generic "ESP32 Dev Module"), Arduino-ESP32 core 3.3.10.
- **Board options** (`FQBN` in `tools/flash.py`): 240 MHz, QIO 80 MHz, 4 MB,
  partition scheme **default** (two 1.25 MB OTA app slots + SPIFFS; needed for
  OTA and rollback), PSRAM off. The image is ~1.03 MB (78% of a slot).
- **Libraries** (Library Manager): `Adafruit GFX Library`, `Adafruit SSD1306`,
  `ESP32Servo`. Built in: `WiFi`, `WiFiUdp`, `ArduinoOTA`, `ESPmDNS`, `Wire`,
  `Preferences`.

## Wiring / pins

| Signal              | GPIO | Notes                                        |
|---------------------|------|----------------------------------------------|
| Steering servo (PWM)| 16   | `STEER_PIN`, 50 Hz, 1000–2000 µs, reversed   |
| ESC throttle (PWM)  | 17   | `ESC_PIN`, 50 Hz, neutral 1500 µs            |
| OLED SDA            | 21   | `Wire.begin(21, 22)`, 400 kHz                |
| OLED SCL            | 22   |                                              |
| OLED                | I²C  | SSD1306 128×64 @ `0x3C`                      |
| BOOT button         | 0    | tap to toggle the OLED (off = zero I²C load) |

Power, grounding and the brownout story: [../docs/hardware.md](../docs/hardware.md).

## Networking

- WiFi credentials: `secrets.h` (gitignored). Two networks, tried in order. At
  boot the car cycles the list until one connects, so it never gives up, because
  it can't be reflashed any other way.
- While connected: modem power-save off (`setSleep(false)`, kills periodic UDP
  latency spikes), max TX power, auto-reconnect. If the link drops, the loop
  calls the non-blocking `WiFi.begin()` on the **next** network every 12 s
  (`WIFI_RETRY_MS`), so the car can fail over without ever stalling the
  control loop.
- mDNS: hostname `rc-car` (so `rc-car.local`), plus in 2.x a `_rccar._udp`
  service with `fw=<version>` in its TXT record.
- Telemetry goes to whoever sent the most recent valid control packet, which is
  why the laptop can find the car by probing.

## Driving behaviour

- **Failsafe:** no valid packet for `FAILSAFE_MS` (250 ms) → hold the last
  steering and freeze the throttle for `FAILSAFE_HOLD_MS` (350 ms, rides out RF
  blips), then ESC neutral and steering centered. The OLED flashes `FAILSAFE`.
- **Gears:** `0` neutral, `1–5` forward (output scales with gear/5), `6` "R"
  slow reverse (0.5×), `7` "r2" full reverse (1.0×).
- **Reverse arming:** brushed-ESC style: a 120 ms reverse "tap" at 1300 µs, an
  80 ms gap, then reverse engages, so the ESC switches direction cleanly instead of
  braking.
- **Brake lockout ("x rev-armed" on the HUD):** after reverse, after a brake to a
  stop, or after a failsafe, brake input is ignored until you drive forward again.
  On a brushed ESC, "below neutral" from a standstill means *reverse*, so
  braking there would back the car up.
- **Brake / handbrake:** drive the ESC below neutral, linear and proportional
  (`BRAKE_ABS 0`; the open-loop ABS pulsing is still in the code but off: no
  wheel-speed feedback means it can't really prevent lockup).
- **Slew limits:** steering 6000 µs/s, ESC 4000 µs/s, so nothing snaps.
- **Steering trim:** d-pad ±2 µs per step (auto-repeat 120 ms, max ±120 µs),
  down resets. Saved to NVS (`Preferences`, namespace `carlink`) 2 s after the
  last change, restored at boot.
- **Watchdog:** task WDT 4 s. A hung loop reboots the car instead of leaving
  it dead. OTA pauses the watchdog while writing.
- **OLED HUD (5 Hz):** gear, own IP, WiFi bars + RSSI, packet rate / FAILSAFE,
  STR/THR/BRK/HB bars, trim, and "Scout", a little dog whose pose follows the
  drive state (trot / brace / reverse / sit). A full-frame I²C flush blocks the
  loop about 20–30 ms, which is why the rate is 5 Hz and why the BOOT button can
  switch the screen off.

## Wire protocol

See [../docs/protocol.md](../docs/protocol.md). Keep `rccar/protocol.py` in sync.
