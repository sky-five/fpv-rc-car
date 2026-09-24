# Notes for AI assistants working on this repo

FPV RC car: G29 wheel → laptop (Python) → ESP32 in the car over UDP; phone camera →
laptop browser over WebRTC. Start with README.md; details in docs/ and firmware/README.md.

## Hard constraints

- **The car's ESP32 has a dead USB port. OTA is the only way to flash it.**
  Never remove or weaken the "OTA rollback protection" section of
  `firmware/car_link_rx/car_link_rx.ino` (`verifyRollbackLater`, `startRollbackGuard`,
  `confirmImageIfHealthy`). Anything that could stop the car from joining WiFi or
  running `ArduinoOTA.handle()` can brick it. Build with `python tools/flash.py
  --build-only` before proposing firmware changes. Never flash without the user's
  explicit go-ahead.
- **Public repo.** Never commit `secrets.h`, `.rccar/` (contains the CA private
  key), logs, or real SSIDs/passwords. `.gitignore` covers these; check
  `git status` before committing.
- The packet formats are shared between `rccar/protocol.py` and the firmware
  structs. Change both together (see docs/protocol.md).
- No hard-coded IPs. The car is discovered (rccar/discovery.py); the phone URL is
  chosen per network (rccar/netinfo.py).

## Environment (the owner's laptop)

- Windows 11, Python 3.14, `pygame-ce` (not `pygame`), aiohttp, cryptography, segno.
- arduino-cli: `C:\Program Files\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe`;
  ESP32 core 3.3.10; espota under `%LOCALAPPDATA%\Arduino15\packages\esp32\hardware\esp32\3.3.10\tools\`.
- GitHub CLI: `C:\Program Files\GitHub CLI\gh.exe` (not on the Git Bash PATH).

## Testing without hardware

- `python tools/sim_car.py` pretends to be the car (same protocol, answers
  broadcast discovery). Then `python launch.py --no-browser`. Delete
  `.rccar/car.json` afterwards: it will have cached the laptop's own IP.
- Browser automation tools can't click through the self-signed/private-CA
  certificate interstitial. For a screenshot of the cockpit use headless Edge:
  `msedge --headless=new --ignore-certificate-errors --window-size=1280,720
  --virtual-time-budget=4000 --screenshot=out.png https://localhost:8443/`
- Real WebRTC needs the phone; the launcher console logs every signaling step.
