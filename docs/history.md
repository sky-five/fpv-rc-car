# Project history and roadmap

## Timeline

- **2026-06-14/15 — car firmware born.** Arduino sketch (`sketch_jun14a`) with
  UDP control, failsafe, OLED HUD and ArduinoOTA, first flashed over USB (the
  bootloader on the car dates from this flash). The board's USB port later died,
  so from then on OTA is the only way in.
- **2026-06-15 — first streaming prototype** (`rc_stream_v2`): phone camera →
  laptop over WebRTC, with a basic HUD.
- **2026-06-16 — firmware reliability pass** (the image still on the car today;
  built 19:49): modem power-save off, I²C at 400 kHz + slower OLED redraw,
  non-blocking WiFi recovery with network failover, max TX power, BOOT button
  toggles the OLED, 4 s task watchdog, telemetry v3 (RSSI, reset reason, free
  heap). Laptop side: `config.py`, `analyze_log.py`, per-drive CSV logs,
  `flash_ota.bat`.
- **2026-06-19 — web/HUD polish**: rear-lens picker (ultra-wide), zoom/torch,
  FILL/FIT, mirror/flip, H.264 preference, WiFi bars, collapsible telemetry.
  Long test drives.
- **2026-07-02 — `launch.py`**: one command for server + reader. Project moved to
  `D:\projects\rc`.
- **2026-07-04 — drives from a stray copy** in Downloads with a hard-coded IP.
  The kind of drift this repo now prevents.
- **2026-09-06/07 — the IP/network session.** The car, phone and laptop kept
  swapping addresses on the RC network. The laptop's Wi-Fi was sometimes
  disconnected, leaving it only on USB-C Ethernet (home network) and
  Tailscale, so the cockpit stayed black (phone and laptop weren't even on the
  same LAN). Diagnosed by hand with ipconfig, ARP and a ping sweep. Once
  everything was on one network, WebRTC signaling completed end-to-end.
- **2026-09-11 — crash:** WiFi dropped mid-session and `g29_reader.py` died
  with WinError 10051, taking the whole stack down.
- **2026-09-24 — consolidated into this repo** (GitHub `fpv-rc-car`):
  - Car discovery (cache / mDNS / resolver / broadcast). No more IPs anywhere.
  - Phone connects by QR code. Local CA so the certificate warning can go away for good.
  - Cockpit shows video state + stats, auto-recovers failed/frozen video.
  - Phone camera switching made robust (debounce, release-first, facing check, backoff).
  - Reader survives network drops, wheel hot-plug (neutral when unplugged),
    shows sent-vs-received input.
  - Launcher: restarts crashed parts, cleans up stale copies, opens the cockpit,
    UTF-8 console.
  - Tools: `flash.py` (build + OTA with discovery), `doctor.py`, `find_car.py`, `sim_car.py`.
  - Firmware 2.0.0 (built, verified, **not yet flashed**): OTA rollback
    protection, `_rccar._udp` mDNS service with the firmware version, version on
    the boot screen, OTA password read from `secrets.h`.
  - Every scattered copy (Downloads ×2, Arduino sketch folder, a Cowork backup)
    compared and merged; docs rewritten against the actual code.

## What the drive logs say

From `tools/analyze_log.py` over the June–September logs:

| Finding | Evidence | Status |
|---|---|---|
| Power sag reboots the car | `BROWNOUT` reset mid-drive on 2026-09-06; earlier `POWERON` reboots mid-drive | **Open**: add cap / UBEC (hardware.md) |
| No memory leak | free heap steady at 141–165 KB across multi-hour runs | fine |
| Car-side loop freezes | dozens of >1 s telemetry gaps + pps spikes in June logs; near zero by September | fixed by the June reliability pass |
| WiFi signal | car RSSI median −30 to −49 dBm; losses cluster at the weakest readings | fine up close; range-limited at the edges |
| "Laptop stall" | many late sends, mostly in multi-day runs (laptop asleep / throttled) | keep the laptop awake while driving |

## Roadmap

- [ ] **Flash firmware 2.0.0** (`python tools/flash.py`) to get rollback
      protection before any other firmware work. The first flash of 2.0.0 is
      the last unprotected one.
- [ ] **Power fix** (hardware): 470–1000 µF + 0.1 µF at the ESP32 supply, or a
      separate 5 V UBEC. Confirmed needed by the brownout.
- [ ] Put the phone on 5 GHz / pin the 2.4 GHz channel to get video off the
      control link's airtime.
- [ ] Optional: set `OTA_PASSWORD` in `secrets.h` (anyone on the WiFi can flash today).
- [ ] If drops persist: control over ESP-NOW via a laptop-side ESP32 USB dongle
      (packet structs stay the same; video stays on WiFi).
- [ ] Photos / a cockpit screenshot in `docs/`.
- [ ] Optional write-up once it's photogenic.
