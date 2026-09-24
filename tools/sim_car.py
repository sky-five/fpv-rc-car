"""
A pretend car for testing the laptop side without the real one.

    python tools/sim_car.py              # listens on UDP 4210 like the ESP32
    python launch.py --car-ip 127.0.0.1  # in another window

Speaks the same protocol as firmware/car_link_rx: answers control packets with
10 Hz telemetry sent back to the sender, echoes steering/brake/handbrake, applies
the same 250 ms failsafe and gear scaling (roughly), and counts lost packets.
Broadcast discovery finds it too (it listens on all interfaces).
"""

import os
import socket
import struct
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from rccar import config, protocol  # noqa: E402

FAILSAFE_S = 0.25


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", config.CAR_UDP_PORT))
    sock.settimeout(0.02)
    print(f"sim car listening on UDP {config.CAR_UDP_PORT} — Ctrl+C to stop")

    sender, last_pkt_t, last_seq, lost, count, pps = None, 0.0, None, 0, 0, 0
    pkt = (0, 0, 0, 0, 0, 0)   # steer, drive, brake, hb, gear, flags
    tele_seq, next_tele, rate_t = 0, 0.0, time.monotonic()
    esc = 0
    while True:
        try:
            data, addr = sock.recvfrom(64)
            if len(data) == protocol.DRIVE_SIZE:
                magic, ver, seq, *rest = struct.unpack(protocol.DRIVE_FMT, data)
                if magic == protocol.DRIVE_MAGIC and ver == protocol.DRIVE_VERSION:
                    if last_seq is not None and seq > last_seq + 1:
                        lost += seq - last_seq - 1
                    if sender != addr:
                        print(f"controller: {addr[0]}:{addr[1]}")
                    last_seq, sender, last_pkt_t, pkt = seq, addr, time.monotonic(), tuple(rest)
                    count += 1
        except socket.timeout:
            pass
        except OSError:
            pass

        now = time.monotonic()
        if now - rate_t >= 1.0:
            pps, count, rate_t = count, 0, now
        live = last_pkt_t and now - last_pkt_t < FAILSAFE_S
        steer, drive, brake, hb, gear, flags = pkt if live else (0, 0, 0, 0, pkt[4], 0)
        if gear in protocol.REVERSE_GEARS:
            target = -int(max(0, -drive) / 1000 * 500 * (1.0 if gear == 7 else 0.5))
        elif 1 <= gear <= 5 and drive > 0:
            target = int(drive / 1000 * 500 * gear / 5)
        elif brake > 50 or hb > 50:
            target = -int(max(brake, hb) / 1000 * 400)
        else:
            target = 0
        esc += max(-80, min(80, target - esc))        # crude slew
        if sender and now >= next_tele:
            next_tele = now + 0.1
            drv = 1 if esc > 0 else 2 if esc < 0 and gear not in (6, 7) else 4 if esc < 0 else 0
            t = struct.pack(protocol.TELE_FMT, protocol.TELE_MAGIC, protocol.TELE_VERSION,
                            tele_seq, gear, drv, 1 if live else 0, flags, steer, esc, brake, hb,
                            0, pps, lost, -48, 1, 190)
            tele_seq += 1
            try:
                sock.sendto(t, sender)
            except OSError:
                pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
