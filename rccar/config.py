"""
Laptop-side settings, in one place.

Nothing here is an IP address: the car is found automatically (see discovery.py)
and the phone gets the laptop's address from a QR code. Every value can still be
overridden with an environment variable, e.g.:

    set RC_CAR_IP=192.168.0.101 && python launch.py      (Windows cmd)
    $env:RC_CAR_IP="192.168.0.101"; python launch.py     (PowerShell)
    python launch.py --car-ip 192.168.0.101              (any shell)
"""

import os


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return int(default)


def _float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(REPO_ROOT, ".rccar")      # certs + car IP cache (gitignored)
LOG_DIR = os.path.join(REPO_ROOT, "logs")          # per-drive CSV logs (gitignored)
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

# --- Car (ESP32) ------------------------------------------------------------
# mDNS hostname the car advertises. Must match OTA_HOSTNAME in the firmware.
CAR_HOSTNAME = os.environ.get("RC_CAR_HOST", "rc-car")
# Optional fixed IP. Normally unset: discovery finds the car on any network.
CAR_IP_OVERRIDE = os.environ.get("RC_CAR_IP") or None
CAR_UDP_PORT = _int("RC_CAR_PORT", 4210)      # control packets -> car, telemetry back
OTA_PORT = _int("RC_OTA_PORT", 3232)          # ArduinoOTA (espota) port on the car
SEND_HZ = _int("RC_SEND_HZ", 50)              # control packet rate
# Telemetry silence (s) after which the car is treated as gone and re-discovered
# (it may have rebooted, or DHCP handed it a new IP).
CAR_LOST_S = _float("RC_CAR_LOST_S", 3.0)

# --- Laptop stream server ----------------------------------------------------
HTTPS_PORT = _int("RC_HTTPS_PORT", 8443)      # cockpit + phone pages
# g29_reader -> stream_server hop on localhost (5005 was taken by another app).
TELE_UDP_PORT = _int("RC_TELE_PORT", 5055)
