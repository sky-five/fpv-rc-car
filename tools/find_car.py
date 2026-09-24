"""
Where is the car right now?

    python tools/find_car.py

Prints its IP, how it was found, its firmware version and a telemetry snapshot.
Uses the same discovery as the wheel reader (last-known IP, mDNS rc-car.local,
OS resolver, UDP broadcast). Sends only neutral probe packets.
"""

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from rccar import discovery, mdns, protocol  # noqa: E402


def main():
    ip, via, tele = discovery.find_car(timeout=8, verbose=True)
    if not ip:
        print("Car not found. Run `python launch.py --doctor` for a full network check.")
        sys.exit(1)
    fw = next((s["txt"].get("fw") for s in mdns.car_service(2.0) if s["txt"].get("fw")), None)
    print(f"Car: {ip}  (found via {via})")
    print(f"Firmware: {fw or '1.x (no version advertised)'}")
    if tele:
        print(f"WiFi {tele['rssi']} dBm, heap {tele['heap']} KB, trim {tele['trim']:+d} us, "
              f"last boot: {protocol.reset_name(tele['reset'])}")


if __name__ == "__main__":
    main()
