"""
G29 wheel + pedals + handbrake  ->  car, over UDP at 50 Hz.

Also: finds the car automatically (discovery.py), forwards the car's telemetry to
the stream server for the cockpit HUD, and writes a per-drive diagnostic CSV.

Run via `python launch.py` (recommended), or standalone for a full-screen
terminal HUD:  python -m rccar.g29_reader
"""

import csv
import datetime
import json
import os
import signal
import socket
import sys
import time

import pygame

from . import config, protocol
from .discovery import CarFinder

# Set by launch.py: print a plain interleavable status line instead of the
# full-screen ANSI HUD (which assumes it owns the whole terminal).
LAUNCHER_MODE = os.environ.get("RC_LAUNCHER") == "1"

TELE_FORWARD = ("127.0.0.1", config.TELE_UDP_PORT)   # -> stream_server.py

# --- Device + axis mapping (calibrated to one G29 unit) ----------------------
# If a different wheel maps differently, run standalone and watch the "in" probe
# line to find the right indices.
WHEEL_NAME_MATCH = "Driving Force"
HANDBRAKE_NAME_MATCH = "HANDBRAKE"

AX_STEER = 0
AX_GAS = 1
AX_BRAKE = 2
AX_CLUTCH = 3

GEAR_BTNS = {12: 1, 13: 2, 14: 3, 15: 4, 16: 5, 17: 6}   # H-shifter gates -> gear code
REVERSE_BTN = 18                                         # push-down reverse -> code 7

HB_AXIS = 0
HB_BTN = 0

# Steering trim (default: G29 d-pad as a hat). left/right = trim, down = reset.
TRIM_USE_HAT = True
TRIM_HAT_INDEX = 0
# If the d-pad shows up as buttons instead of a hat, set TRIM_USE_HAT = False
# and fill these in using the "in" probe shown on the standalone HUD.
TRIM_LEFT_BTN = 11
TRIM_RIGHT_BTN = 13
TRIM_RESET_BTN = 14

STEER_LEFT = -0.98
STEER_CENTER = -0.016
STEER_RIGHT = 0.95
STEER_DEADZONE = 0.04

REFRESH_HZ = 20        # standalone full-screen HUD redraw rate
STATUS_PERIOD_S = 1.0  # launch.py mode: status line rate while driving
SEARCH_PERIOD_S = 5.0  # launch.py mode: status line rate while searching / no wheel


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def find_device(substr):
    for i in range(pygame.joystick.get_count()):
        js = pygame.joystick.Joystick(i)
        if substr.lower() in js.get_name().lower():
            return js
    return None


def norm_pedal(raw):
    return clamp((1.0 - raw) / 2.0, 0.0, 1.0)


def norm_handbrake(raw):
    return clamp((raw + 1.0) / 2.0, 0.0, 1.0)


def norm_steer(raw):
    if raw <= STEER_CENTER:
        span = STEER_CENTER - STEER_LEFT
        out = -(STEER_CENTER - raw) / span if span else 0.0
    else:
        span = STEER_RIGHT - STEER_CENTER
        out = (raw - STEER_CENTER) / span if span else 0.0
    out = clamp(out, -1.0, 1.0)
    if abs(out) < STEER_DEADZONE:
        return 0.0
    sign = 1.0 if out > 0 else -1.0
    return sign * (abs(out) - STEER_DEADZONE) / (1.0 - STEER_DEADZONE)


def read_gear_code(wheel):
    if wheel.get_button(REVERSE_BTN):
        return 7
    for btn, gear in GEAR_BTNS.items():
        if wheel.get_button(btn):
            return gear
    return 0


def read_trim(wheel):
    if TRIM_USE_HAT and wheel.get_numhats() > TRIM_HAT_INDEX:
        hx, hy = wheel.get_hat(TRIM_HAT_INDEX)
        return (1 if hx < 0 else 0, 1 if hx > 0 else 0, 1 if hy < 0 else 0)
    l = 1 if wheel.get_button(TRIM_LEFT_BTN) else 0
    r = 1 if wheel.get_button(TRIM_RIGHT_BTN) else 0
    rst = 1 if wheel.get_button(TRIM_RESET_BTN) else 0
    return (l, r, rst)


def probe(wheel):
    pressed = [i for i in range(wheel.get_numbuttons()) if wheel.get_button(i)]
    hats = [wheel.get_hat(i) for i in range(wheel.get_numhats())]
    return f"hats {hats} btns {pressed}"


class Inputs:
    """What we're about to send (the local side of 'is my input reaching the car?')."""
    def __init__(self):
        self.steer = self.drive = self.brake = self.hb = 0.0
        self.gear = 0
        self.hb_btn = self.trim_l = self.trim_r = self.trim_rst = 0

    def read(self, wheel, handbrake, moved):
        """moved: (device instance id, axis) pairs that have reported motion.

        pygame reports an axis as 0.0 until the device sends its first update, and
        0.0 maps to a HALF-pressed pedal. Treat untouched pedals as released, or
        shifting into gear before touching the gas would send 50% throttle.
        """
        wid = wheel.get_instance_id()
        self.steer = norm_steer(wheel.get_axis(AX_STEER))
        gas = norm_pedal(wheel.get_axis(AX_GAS)) if (wid, AX_GAS) in moved else 0.0
        self.brake = norm_pedal(wheel.get_axis(AX_BRAKE)) if (wid, AX_BRAKE) in moved else 0.0
        self.gear = read_gear_code(wheel)
        self.trim_l, self.trim_r, self.trim_rst = read_trim(wheel)
        if self.gear in protocol.REVERSE_GEARS:
            self.drive = -gas
        elif self.gear == 0:
            self.drive = 0.0
        else:
            self.drive = gas
        if handbrake is not None:
            hid = handbrake.get_instance_id()
            self.hb = norm_handbrake(handbrake.get_axis(HB_AXIS)) if (hid, HB_AXIS) in moved else 0.0
            self.hb_btn = 1 if handbrake.get_button(HB_BTN) else 0
        else:
            self.hb, self.hb_btn = 0.0, 0

    def neutral(self):
        self.__init__()

    def packet(self, seq):
        flags = ((self.hb_btn and protocol.FLAG_HB)
                 | (protocol.FLAG_TRIM_L if self.trim_l else 0)
                 | (protocol.FLAG_TRIM_R if self.trim_r else 0)
                 | (protocol.FLAG_TRIM_RST if self.trim_rst else 0))
        return protocol.pack_drive(
            seq,
            int(clamp(round(self.steer * 1000), -1000, 1000)),
            int(clamp(round(self.drive * 1000), -1000, 1000)),
            int(clamp(round(self.brake * 1000), 0, 1000)),
            int(clamp(round(self.hb * 1000), 0, 1000)),
            self.gear, flags)


# --- Terminal output ------------------------------------------------------------

def sbar(v, width=20):
    half = width // 2
    n = min(int(round(abs(v) * half)), half)
    if v >= 0:
        return "[" + " " * half + "|" + "#" * n + " " * (half - n) + "]"
    return "[" + " " * (half - n) + "#" * n + "|" + " " * half + "]"


def ubar(v, width=20):
    n = min(max(int(round(v * width)), 0), width)
    return "[" + "#" * n + " " * (width - n) + "]"


def status_line(finder, inp, tele, fresh, wheel_ok, net_err):
    """One plain line for launch.py's shared console."""
    wheel = "" if wheel_ok else "NO WHEEL (sending neutral) | "
    ins = (f"IN str {inp.steer * 100:+4.0f}% drv {inp.drive * 100:+4.0f}% "
           f"brk {inp.brake * 100:3.0f}% hb {inp.hb * 100:3.0f}% G{protocol.gear_name(inp.gear)}")
    if net_err:
        return f"{wheel}NETWORK: {net_err} — is the laptop on the car's WiFi? | {ins}"
    if not finder.locked:
        return f"{wheel}searching for car — {finder.describe_search()}"
    if tele is None or not fresh:
        return f"{wheel}car {finder.ip} ({finder.via}) — waiting for telemetry | {ins}"
    t = tele
    link = "live" if t["link"] else "FAILSAFE"
    return (f"{wheel}CAR {finder.ip} {link} {t['pps']:>2}pps lost {t['lost']} "
            f"{t['rssi']}dBm | {ins} | CAR str {t['steer'] / 10:+4.0f}% "
            f"esc {t['escOff'] / 5:+4.0f}% {protocol.DRV_CODES.get(t['drv'], '?')} "
            f"G{protocol.gear_name(t['gear'])} trim {t['trim']:+d}us")


def render(finder, inp, tele, fresh, wheel, net_err):
    """Full-screen ANSI HUD for standalone runs."""
    out = ["\033[H"]

    def line(s=""):
        out.append(s + "\033[K")

    line("  RC CAR LINK  --  laptop mirror")
    line("  " + "-" * 60)
    if wheel is None:
        line("  G29 not connected — plug it in (car is held at neutral)")
    if net_err:
        line(f"  NETWORK: {net_err}")
    if not finder.locked:
        line("  Searching for the car...")
        line("  " + finder.describe_search()[:110])
    elif tele is None or not fresh:
        line(f"  Car {finder.ip} (via {finder.via}) — waiting for telemetry")
    else:
        t = tele
        link = "live" if t["link"] else "NO SIGNAL (failsafe)"
        line(f"  CAR {finder.ip} via {finder.via}   GEAR {protocol.gear_name(t['gear'])}   "
             f"{t['pps']:>3} pps   LOST {t['lost']}   {t['rssi']} dBm   {link}")
        line(f"  drive state {protocol.DRV_NAMES.get(t['drv'], '?')}   trim {t['trim']:+d} us   "
             f"heap {t['heap']} KB   last boot: {protocol.reset_name(t['reset'])}")
        line()
        line("           sending                          car reports")
        line(f"  STR  {sbar(inp.steer)} {inp.steer * 100:+4.0f}%   {sbar(t['steer'] / 1000)} {t['steer'] / 10:+4.0f}%")
        line(f"  DRV  {sbar(inp.drive)} {inp.drive * 100:+4.0f}%   esc {sbar(t['escOff'] / 500)} {t['escOff'] / 5:+4.0f}%")
        line(f"  BRK  {ubar(inp.brake)} {inp.brake * 100:4.0f}%   {ubar(t['brake'] / 1000)} {t['brake'] / 10:4.0f}%")
        line(f"  HB   {ubar(inp.hb)} {inp.hb * 100:4.0f}%   {ubar(t['handbrake'] / 1000)} {t['handbrake'] / 10:4.0f}%")
    line()
    if wheel is not None:
        trim_disp = ("<" if inp.trim_l else " ") + ("o" if inp.trim_rst else " ") + (">" if inp.trim_r else " ")
        line(f"  controls: dpad {trim_disp}   in {probe(wheel)}")
    out.append("\033[J")
    sys.stdout.write("\n".join(out))
    sys.stdout.flush()


# --- Diagnostic logging ---------------------------------------------------------
LOG_ENABLED = True


class EventLogger:
    """Append-only CSV of control-link diagnostics (read it with tools/analyze_log.py).

    One 'tele' row per telemetry packet plus event rows (failsafe enter/exit, lost
    bursts, telemetry stalls/gaps, late sends, car found/lost, wheel lost...).
    """
    COLS = ["wall", "t_ms", "event", "rssi", "pps", "lost", "dlost",
            "link", "drv", "gear", "tele_gap_ms", "send_jit_ms", "reset", "heap", "note"]

    def __init__(self, path):
        self.path = path
        self.f = open(path, "w", newline="", encoding="utf-8")
        self.w = csv.writer(self.f)
        self.w.writerow(self.COLS)
        self._last_flush = time.perf_counter()

    def write(self, t_ms, event, rssi="", pps="", lost="", dlost="", link="",
              drv="", gear="", tele_gap_ms="", send_jit_ms="", reset="", heap="", note=""):
        gap = f"{tele_gap_ms:.0f}" if tele_gap_ms != "" else ""
        jit = f"{send_jit_ms:.1f}" if send_jit_ms != "" else ""
        self.w.writerow([f"{time.time():.3f}", f"{t_ms:.1f}", event, rssi, pps, lost,
                         dlost, link, drv, gear, gap, jit, reset, heap, note])
        now = time.perf_counter()
        if now - self._last_flush > 1.0:
            self.f.flush()
            self._last_flush = now

    def close(self):
        try:
            self.f.flush()
            self.f.close()
        except Exception:
            pass


def main():
    os.system("")  # enable ANSI escape handling on Windows terminals

    # launch.py stops us with CTRL_BREAK (CTRL_C can't target one process in a
    # group). Route it through KeyboardInterrupt so the neutral packet still goes
    # out and the log still closes.
    if sys.platform == "win32":
        def _on_ctrl_break(signum, frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGBREAK, _on_ctrl_break)

    # Windows' default timer granularity (~15 ms) would jitter the 20 ms send cadence.
    winmm = None
    if sys.platform == "win32":
        try:
            import ctypes
            winmm = ctypes.WinDLL("winmm")
            winmm.timeBeginPeriod(1)
        except Exception:
            winmm = None

    pygame.init()
    pygame.joystick.init()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    sock.setblocking(False)
    fwd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)   # -> stream server
    finder = CarFinder(sock)

    wheel = find_device(WHEEL_NAME_MATCH)
    handbrake = find_device(HANDBRAKE_NAME_MATCH)

    print(f"Looking for the car ('{config.CAR_HOSTNAME}.local' / broadcast on UDP {config.CAR_UDP_PORT})"
          + (f", starting with {config.CAR_IP_OVERRIDE}" if config.CAR_IP_OVERRIDE else ""))
    print(f"Wheel: {wheel.get_name() if wheel else 'NOT FOUND — plug in the G29 (car held at neutral)'}")
    print(f"Handbrake: {handbrake.get_name() if handbrake else 'not found (optional)'}")
    print("Trim: d-pad left/right = trim, d-pad down = reset.  Ctrl+C to quit.")
    if not LAUNCHER_MODE:
        time.sleep(1.0)
        sys.stdout.write("\033[2J")

    logger = None
    if LOG_ENABLED:
        os.makedirs(config.LOG_DIR, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        logger = EventLogger(os.path.join(config.LOG_DIR, f"rc_log_{stamp}.csv"))
        print(f"Logging to {os.path.relpath(logger.path, config.REPO_ROOT)}")

    inp = Inputs()
    seq = 0
    send_period = 1.0 / config.SEND_HZ
    t0 = time.perf_counter()
    next_send = next_draw = next_status = t0
    last_line_t = 0.0
    tele, last_tele_time = None, 0.0
    net_err = None
    prev_lost = prev_link = prev_tele_t = prev_reset = None
    last_send_t, last_send_jit = None, ""
    stalled = False

    def log(event, **kw):
        if logger:
            logger.write((time.perf_counter() - t0) * 1000.0, event, **kw)

    log("start", note=f"search {config.CAR_HOSTNAME} send {config.SEND_HZ}Hz")

    moved = set()    # (instance id, axis) that have reported real motion

    try:
        while True:
            for ev in pygame.event.get():
                if ev.type == pygame.JOYAXISMOTION:
                    moved.add((ev.instance_id, ev.axis))
                elif ev.type in (pygame.JOYDEVICEADDED, pygame.JOYDEVICEREMOVED):
                    had = wheel is not None
                    wheel = find_device(WHEEL_NAME_MATCH)
                    handbrake = find_device(HANDBRAKE_NAME_MATCH)
                    if had and wheel is None:
                        print("G29 disconnected — holding the car at neutral.", flush=True)
                        log("wheel_lost")
                    elif not had and wheel is not None:
                        print(f"Wheel connected: {wheel.get_name()}", flush=True)
                        log("wheel_found")

            if wheel is not None:
                try:
                    inp.read(wheel, handbrake, moved)
                except pygame.error:
                    wheel = None
                    inp.neutral()
            else:
                inp.neutral()          # safety: no wheel = neutral, never stale input

            now = time.perf_counter()

            if finder.locked:
                if now >= next_send:
                    if last_send_t is not None:
                        interval = now - last_send_t
                        last_send_jit = (interval - send_period) * 1000.0
                        if interval > send_period * 1.5:
                            log("send_late", send_jit_ms=last_send_jit,
                                note=f"gap {interval * 1000:.0f}ms")
                    last_send_t = now
                    try:
                        sock.sendto(inp.packet(seq), (finder.ip, config.CAR_UDP_PORT))
                        if net_err:
                            log("net_ok")
                        net_err = None
                    except OSError as e:
                        # e.g. WinError 10051 when WiFi drops. Used to crash the
                        # whole stack; now we just report it and keep trying.
                        if not net_err:
                            log("net_error", note=str(e))
                        net_err = e.strerror or str(e)
                    seq += 1
                    next_send += send_period
                    if now - next_send > send_period:
                        next_send = now + send_period
            elif finder.probe(seq):
                seq += 1

            # Telemetry: from the main socket, plus answers to broadcast probes.
            incoming = []
            while True:
                try:
                    data, addr = sock.recvfrom(256)
                except (BlockingIOError, InterruptedError):
                    break
                except OSError:
                    break          # e.g. WinError 10054 (ICMP port unreachable) — ignore
                incoming.append((data, addr))
            incoming.extend(finder.drain_broadcast_replies())

            for data, addr in incoming:
                t = protocol.parse_telemetry(data)
                if t is None:
                    continue
                if not finder.locked:
                    finder.lock(addr[0])
                    last_tele_time = now
                    next_send = now
                    print(f"Found the car at {finder.ip} (via {finder.via}).", flush=True)
                    log("car_found", note=f"{finder.ip} via {finder.via}")
                elif addr[0] != finder.ip:
                    continue       # a stray reply from an old address
                try:
                    fwd.sendto(data, TELE_FORWARD)
                except OSError:
                    pass
                tele, last_tele_time = t, now

                if logger:
                    t_ms = (now - t0) * 1000.0
                    gap = ((now - prev_tele_t) * 1000.0) if prev_tele_t is not None else 0.0
                    dlost = max(0, (t["lost"] - prev_lost) if prev_lost is not None else 0)
                    logger.write(t_ms, "tele", rssi=t["rssi"], pps=t["pps"], lost=t["lost"],
                                 dlost=dlost, link=int(t["link"]), drv=t["drv"], gear=t["gear"],
                                 tele_gap_ms=gap, send_jit_ms=last_send_jit,
                                 reset=t["reset"], heap=t["heap"])
                    if prev_lost is not None and t["lost"] < prev_lost:
                        logger.write(t_ms, "car_reboot", rssi=t["rssi"], reset=t["reset"],
                                     heap=t["heap"], note=f"reset_reason={t['reset']}")
                    if prev_reset is None or t["reset"] != prev_reset:
                        logger.write(t_ms, "boot_reason", reset=t["reset"], heap=t["heap"],
                                     note=f"reset_reason={t['reset']}")
                    if prev_link and not t["link"]:
                        logger.write(t_ms, "FAILSAFE_enter", rssi=t["rssi"], pps=t["pps"], lost=t["lost"])
                    elif prev_link is not None and not prev_link and t["link"]:
                        logger.write(t_ms, "failsafe_exit", rssi=t["rssi"], pps=t["pps"])
                    if dlost > 0:
                        logger.write(t_ms, "lost", rssi=t["rssi"], pps=t["pps"], lost=t["lost"], dlost=dlost)
                    if gap > 300:
                        logger.write(t_ms, "tele_gap", rssi=t["rssi"], tele_gap_ms=gap)
                    if gap > 1000:
                        logger.write(t_ms, "car_freeze", rssi=t["rssi"], tele_gap_ms=gap,
                                     note="telemetry froze >1s (car loop stall / WiFi)")
                    prev_lost, prev_link, prev_tele_t = t["lost"], t["link"], now
                    prev_reset = t["reset"]
                    stalled = False

            if finder.locked and not stalled and last_tele_time and now - last_tele_time > 0.5:
                stalled = True
                log("tele_stall", note=f"no telemetry {(now - last_tele_time) * 1000:.0f}ms")

            if finder.locked and now - last_tele_time > config.CAR_LOST_S:
                print(f"Lost the car at {finder.ip} (no telemetry for {config.CAR_LOST_S:.0f}s) "
                      f"— searching again (it may have a new IP).", flush=True)
                log("car_lost", note=finder.ip)
                finder.unlock()
                tele = None
                prev_lost = prev_link = prev_tele_t = None

            fresh = (now - last_tele_time) < 0.5

            if now >= next_status:            # tell the stream server what's going on
                next_status = now + 1.0
                status = {
                    "type": "car",
                    "state": ("live" if fresh and tele and tele["link"] else
                              "failsafe" if fresh and tele else
                              "connecting" if finder.locked else "searching"),
                    "ip": finder.ip, "via": finder.via,
                    "wheel": wheel is not None,
                    "netError": net_err,
                    "search": None if finder.locked else finder.describe_search(),
                }
                try:
                    fwd.sendto(json.dumps(status).encode(), TELE_FORWARD)
                except OSError:
                    pass

            if LAUNCHER_MODE:
                driving = finder.locked and fresh and wheel is not None and not net_err
                period = STATUS_PERIOD_S if driving else SEARCH_PERIOD_S
                if now - last_line_t >= period:
                    last_line_t = now
                    print(status_line(finder, inp, tele, fresh, wheel is not None, net_err), flush=True)
            elif now >= next_draw:
                render(finder, inp, tele, fresh, wheel, net_err)
                next_draw = now + 1.0 / REFRESH_HZ

            time.sleep(0.001)
    except KeyboardInterrupt:
        print("\nStopping. Sending neutral packet.", flush=True)
        if finder.ip:
            for _ in range(3):
                try:
                    sock.sendto(protocol.pack_neutral(seq), (finder.ip, config.CAR_UDP_PORT))
                except OSError:
                    pass
                seq += 1
    finally:
        finder.close()
        sock.close()
        fwd.close()
        if logger is not None:
            log("stop")
            logger.close()
            print(f"Log saved: {os.path.relpath(logger.path, config.REPO_ROOT)}")
        if winmm is not None:
            try:
                winmm.timeEndPeriod(1)
            except Exception:
                pass


if __name__ == "__main__":
    main()
