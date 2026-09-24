# Hardware

## In the car

| Part | Notes |
|---|---|
| ESP32 dev board ("ESP32 Dev Module", 4 MB flash) | **USB port dead: OTA-only.** See [firmware/README.md](../firmware/README.md). |
| SSD1306 128×64 I²C OLED, two-color (16 px yellow band on top) | SDA 21, SCL 22, addr `0x3C` |
| Steering servo | GPIO 16, 1000–2000 µs, direction reversed in firmware (`STEER_REVERSE`) |
| Brushed ESC with reverse | GPIO 17, neutral 1500 µs. Needs the reverse-arming tap (firmware handles it). |
| Car battery + ESC BEC | Powers the servo. See *Power* below. |
| Android phone, mounted portrait, facing forward | Back camera = driving view. Front (selfie) camera looks backwards = reverse view. The ultra-wide lens gives back horizontal field of view lost to portrait mounting. |

## On the desk

| Part | Notes |
|---|---|
| Laptop, Windows 11, Intel Wi-Fi 6 AX201 | Runs `launch.py`. Python 3.14. |
| Logitech G29 wheel + pedals | pygame name match "Driving Force". Axes: 0 steer, 1 gas, 2 brake, 3 clutch (unused). |
| G29 H-shifter | Buttons 12–17 = gears 1–5 and R (gear code 6, slow reverse); pushing down (button 18) = code 7 (full reverse). |
| USB handbrake | pygame name match "HANDBRAKE", axis 0 + button 0. Optional. |

## Power (read this: the logs show a brownout)

Servo and ESC run off the car's BEC and share ground with the ESP32. Don't power
the servo from the ESP32's 3V3 rail.

The drive log from **2026-09-06** records a mid-drive reboot with reset reason
**BROWNOUT**: the ESP32's supply sagged under load (servo stall current, or
throttle punching the battery). Earlier "mystery" reboots in June were
`POWERON`, meaning the chip lost power completely, which fits the same cause.
Fixes, cheapest first:

1. **470–1000 µF low-ESR electrolytic + 0.1 µF ceramic** across the ESP32's 5 V/VIN
   and GND, as close to the board as possible.
2. A **separate 5 V UBEC** for the ESP32, so servo and motor current can't sag it.
3. Check the BEC's current rating against the servo's stall current.

`tools/analyze_log.py` prints the reset reason of every reboot in a drive log.
`BROWNOUT` = power, `TASK_WDT`/`INT_WDT` = firmware hang, `PANIC` = crash.

## Radio

- The ESP32 only does **2.4 GHz**. Everything else on the car's network competes
  with the 50 Hz control link for that airtime, including the phone's video.
- If the router is dual-band: put the **phone on 5 GHz** and pin the 2.4 GHz
  channel to a quiet one (1, 6 or 11). The laptop can sit on either band.
- Logged car RSSI is mostly −20 to −50 dBm (excellent). Packet-loss bursts
  line up with weaker signal at the edge of range. A DHCP reservation for the
  car on the router isn't needed anymore (discovery handles address changes),
  but it does keep the "last known IP" shortcut instant.
