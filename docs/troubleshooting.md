# Troubleshooting

First step for anything network-related:

```bash
python launch.py --doctor
```

It lists the laptop's interfaces, finds the car by every method, checks the
Windows network profile, the server, the phone and the wheel, and prints fixes.

---

## The car isn't found ("searching for car…")

- **Is the laptop on the car's WiFi?** This has been the cause more than once.
  The laptop had Wi-Fi *disconnected* and was only on a USB-C Ethernet adapter
  (home network, 192.168.2.x) plus Tailscale (100.x). None of those can reach a
  car on the RC network (192.168.0.x). The doctor shows every interface; join the
  car's network with the Windows Wi-Fi menu.
- Is the car powered and on WiFi? Its OLED shows its IP in the top band, and WiFi
  bars / `--` if it isn't connected.
- The car's IP doesn't matter anymore. DHCP used to shuffle car/phone/laptop
  addresses between sessions (the car moved between .101, .102, …), which meant
  editing IPs every time. Discovery now uses last-known IP, mDNS, the Windows
  resolver and broadcast. Force one with `--car-ip` only if broadcasts are
  blocked (some guest/enterprise networks isolate clients; use a normal
  router or phone hotspot).

## Cockpit is black / "waiting for the phone"

The cockpit now says which step is missing (phone not connected / connecting /
no frames), and the launcher console logs every step (`[phone] connected`,
`[video] phone -> viewer: offer`, `[viewer] video connection: connected`, …).

| Console shows | Meaning / fix |
|---|---|
| no `[phone] connected` | The phone can't reach the laptop. Same WiFi? Scan the QR code instead of typing an old IP. The Windows network profile must be **Private** (a new network defaults to Public, which blocks inbound connections; the doctor checks this). Allow Python through Windows Firewall. |
| `[phone] connected` but no `offer` | The phone page is open but "Start streaming" wasn't tapped, or camera permission was denied (the phone page says so). |
| `offer`/`answer` but connection `failed` | Media (UDP) is blocked between phone and laptop: firewall, AP client isolation, or phone and laptop on different networks/bands that don't route to each other. The cockpit retries by itself every few seconds. |
| `connected` but "no frames" | Rare. The cockpit auto-restarts after 5 s of no frames. Reload the phone page if it persists. |

History: the "black screen" in September 2026 was the laptop not being on the
same LAN as the phone. Signaling never completed. Once the laptop joined the
right network, the full handshake went through.

## Certificate warnings

The first time, each browser warns about the certificate. Either tap
*Advanced → Proceed*, or install the local CA once so warnings never come back,
even when the laptop's IP changes:

- **Android phone:** open the cockpit's `/ca.crt` link on the phone (or
  `https://<laptop>:8443/ca.crt`), then Settings → Security → Encryption &
  credentials → Install a certificate → CA certificate.
- **The laptop's own browser** (optional): run it yourself in a terminal:
  `certutil -user -addstore Root .rccar\certs\ca.crt`

The CA key lives only in `.rccar/certs/` on this laptop (gitignored). Delete that
folder to start over with a new CA.

## Wheel input doesn't reach the car

The launcher status line shows both sides:

```
CAR 192.168.0.101 live 50pps … | IN str +35% drv +60% … | CAR str +35% esc +24% F G2
```

- `IN` = what the wheel produces, `CAR` = what the car reports it received/applied.
- `IN` stuck at 0 while you turn/press → wheel mapping or G HUB. Run
  `python -m rccar.g29_reader` for the full-screen HUD with a live button/hat
  probe, and check the axis indices at the top of `rccar/g29_reader.py`.
- `IN` moves, `CAR` doesn't → network. Check `pps` / `lost` / `FAILSAFE`.
- `NO WHEEL (sending neutral)` → the G29 isn't connected (hot-plugging is fine).
- Throttle does nothing in gear **N**. Brake does nothing after reverse/stop
  (the "x rev-armed" lockout). Both are by design, see the firmware README.

## The launcher crashed with "unreachable network" (WinError 10051)

This happened when WiFi dropped mid-session and killed the whole stack
(2026-09-11). Fixed: network errors are now reported (`NETWORK: …`) and the reader
keeps retrying until WiFi is back.

## "Port 8443 is already in use"

`launch.py` now stops leftover copies of itself from earlier sessions automatically,
including a stale wheel reader that would otherwise be a *second controller* for
the car. If something else uses 8443, set `RC_HTTPS_PORT`.

## The car reboots mid-drive

Run `python tools/analyze_log.py` on the drive log. It reports the reset reason:
`BROWNOUT` = power (seen 2026-09-06; see [hardware.md](hardware.md#power-read-this-the-logs-show-a-brownout)),
`TASK_WDT` = firmware hang (watchdog saved it), `PANIC` = crash.

## Glitches / lag while driving

`analyze_log.py` separates the causes: car-side loop freezes (telemetry gaps + pps
spikes), laptop send stalls (usually the laptop sleeping or throttling: use a
performance power plan and don't let it sleep), and RF (losses cluster at weak
RSSI). If RF, move the phone's video to 5 GHz (see hardware.md).

## Camera doesn't flip for reverse, or shows the wrong camera

The phone page switches once the gear has been stable for 250 ms, releases the
current camera before opening the other one (most phones can't open two), checks
the facing the browser reports, and waits 2 s before retrying after a failure.
The launcher console shows `[phone] camera switch … failed` with the reason.
Use the **Lens** menu to pick the right back lens (ultra-wide works best
portrait-mounted).

## OTA flash fails

- `tools/flash.py` must find the car first (same as the doctor).
- Windows Firewall must allow espota's inbound connection (the car connects back
  to the laptop to pull the image). Allow it on Private networks when Windows asks.
- If you set `OTA_PASSWORD` in `secrets.h`, it must match what the car runs.
- A failed upload leaves the car on its current firmware. A bad image that
  uploads fine rolls back by itself (firmware ≥ 2.0.0).
