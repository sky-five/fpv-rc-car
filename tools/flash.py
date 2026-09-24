"""
Build the car firmware and flash it over WiFi (OTA) — the only way to flash this
car, since its ESP32's USB port is dead.

    python tools/flash.py                  # build, find the car, confirm, flash, verify
    python tools/flash.py --build-only     # just compile (safe; nothing is sent)
    python tools/flash.py --ip 192.168.0.101 --yes

What it does:
  1. Compiles firmware/car_link_rx with arduino-cli (bundled with Arduino IDE 2),
     using the exact board settings the car was originally flashed with.
  2. Finds the car on the network (same discovery as the wheel reader).
  3. Asks you to confirm, then uploads with espota (ArduinoOTA protocol).
  4. Waits for the car to come back and reports the firmware version it runs.

Safety: firmware 2.x confirms a new image only once it's back on WiFi with OTA
listening for 20 s; otherwise the bootloader rolls back to the previous image
(within 2 minutes). So a bad build costs you a reboot, not the car.
"""

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from rccar import config, discovery, mdns, protocol  # noqa: E402

SKETCH = os.path.join(REPO, "firmware", "car_link_rx")
BUILD = os.path.join(REPO, "build", "firmware")
BIN = os.path.join(BUILD, "car_link_rx.ino.bin")
# Board settings the car was originally built/flashed with (Arduino IDE, June 2026).
# PartitionScheme=default = two 1.25 MB OTA app slots — required for OTA + rollback.
FQBN = ("esp32:esp32:esp32:UploadSpeed=921600,CPUFreq=240,FlashFreq=80,FlashMode=qio,"
        "FlashSize=4M,PartitionScheme=default,DebugLevel=none,PSRAM=disabled,LoopCore=1,"
        "EventsCore=1,EraseFlash=none,JTAGAdapter=default,ZigbeeMode=default")


def find_arduino_cli():
    cands = [os.environ.get("ARDUINO_CLI"), shutil.which("arduino-cli"),
             r"C:\Program Files\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe",
             os.path.expandvars(r"%LOCALAPPDATA%\Programs\Arduino IDE\resources\app\lib\backend"
                                r"\resources\arduino-cli.exe")]
    return next((c for c in cands if c and os.path.exists(c)), None)


def find_espota():
    if os.environ.get("ESPOTA") and os.path.exists(os.environ["ESPOTA"]):
        return [os.environ["ESPOTA"]]
    base = os.path.expandvars(r"%LOCALAPPDATA%\Arduino15\packages\esp32\hardware\esp32")
    for ext in ("exe", "py"):
        hits = sorted(glob.glob(os.path.join(base, "*", "tools", f"espota.{ext}")))
        if hits:
            return [hits[-1]] if ext == "exe" else [sys.executable, hits[-1]]
    return None


def secrets_path():
    return os.path.join(SKETCH, "secrets.h")


def ota_password():
    try:
        with open(secrets_path(), encoding="utf-8") as f:
            for line in f:
                m = re.match(r'\s*#define\s+OTA_PASSWORD\s+"([^"]*)"', line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return None


def firmware_version():
    with open(os.path.join(SKETCH, "car_link_rx.ino"), encoding="utf-8") as f:
        m = re.search(r'#define\s+FW_VERSION\s+"([^"]+)"', f.read())
    return m.group(1) if m else "?"


def car_fw_version(timeout=2.0):
    for svc in mdns.car_service(timeout):
        fw = svc["txt"].get("fw")
        if fw:
            return fw
    return None


def build():
    cli = find_arduino_cli()
    if not cli:
        sys.exit("arduino-cli not found. Install Arduino IDE 2, or set ARDUINO_CLI to arduino-cli.exe.")
    if not os.path.exists(secrets_path()):
        sys.exit(f"Missing {os.path.relpath(secrets_path(), REPO)} — copy secrets.h.example next to it\n"
                 f"and fill in the WiFi networks the car uses (the car must be able to join one!).")
    os.makedirs(BUILD, exist_ok=True)
    print(f"Building firmware {firmware_version()} ...")
    r = subprocess.run([cli, "compile", "--fqbn", FQBN, "--warnings", "default",
                        "--output-dir", BUILD, SKETCH])
    if r.returncode != 0 or not os.path.exists(BIN):
        sys.exit("Build failed — nothing was sent to the car.")
    print(f"Built {os.path.relpath(BIN, REPO)} ({os.path.getsize(BIN):,} bytes)")


def wait_for_car(ip, timeout=90):
    """After upload the car reboots; wait until it answers telemetry again."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found, via, tele = discovery.find_car(timeout=3)
        if found:
            return found, tele
    return None, None


def main():
    ap = argparse.ArgumentParser(description="Build + OTA-flash the car firmware")
    ap.add_argument("--ip", help="car IP (default: find it automatically)")
    ap.add_argument("--build-only", action="store_true", help="compile only, don't flash")
    ap.add_argument("--no-build", action="store_true", help="flash the last build as-is")
    ap.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    args = ap.parse_args()

    if not args.no_build:
        build()
    if args.build_only:
        return
    if not os.path.exists(BIN):
        sys.exit("No build to flash. Run without --no-build.")

    espota = find_espota()
    if not espota:
        sys.exit("espota not found (it ships with the ESP32 Arduino core). Set ESPOTA to its path.")

    ip, via = args.ip, "--ip"
    if not ip:
        print("Looking for the car...")
        ip, via, tele = discovery.find_car(timeout=8, verbose=True)
        if not ip:
            sys.exit("Car not found. Is it powered on, and is this laptop on the same WiFi?\n"
                     "Run `python launch.py --doctor` for a network check.")
    before = car_fw_version()
    print(f"Car: {ip} (found via {via}); running firmware {before or '1.x (no version advertised)'}")
    print(f"About to flash firmware {firmware_version()} over WiFi.")
    print("The car's wheels must be off the ground or the ESC off — outputs go neutral during the update.")
    if not args.yes and input("Flash now? [y/N] ").strip().lower() != "y":
        print("Cancelled; nothing was sent.")
        return

    cmd = espota + ["-i", ip, "-p", str(config.OTA_PORT), "-r", "-f", BIN]
    pw = ota_password()
    if pw:
        cmd += ["-a", pw]
    print("Uploading (the car's OLED shows progress)...")
    r = subprocess.run(cmd)
    if r.returncode != 0:
        sys.exit("Upload failed; the car keeps running its current firmware. Common causes: Windows\n"
                 "Firewall blocking espota's inbound connection (allow it on Private networks), a\n"
                 "stale/other OTA password, or weak WiFi. See docs/troubleshooting.md.")

    print("Upload done. Waiting for the car to reboot and rejoin WiFi...")
    new_ip, tele = wait_for_car(ip)
    if not new_ip:
        print("The car hasn't answered within 90 s. If the new firmware can't get onto WiFi it\n"
              "rolls itself back to the previous version within 2 minutes; power-cycle the car\n"
              "if it still doesn't show up after that.")
        return
    after = car_fw_version(3.0)
    print(f"Car is back at {new_ip}, running firmware {after or '?'}; last boot: "
          f"{protocol.reset_name(tele['reset']) if tele else '?'}")
    if after == firmware_version():
        print("It confirms itself as good after ~20 s of healthy running (rollback protection).")
    elif after:
        print(f"It is running {after}, not {firmware_version()} — the new image may have rolled back.")


if __name__ == "__main__":
    main()
