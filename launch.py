"""
One command for the whole laptop side:

    python launch.py                 # start everything, open the cockpit
    python launch.py --car-ip X      # skip discovery, use this car IP first
    python launch.py --no-browser    # don't open the cockpit page
    python launch.py --only server   # just the video/HUD server (or: --only reader)
    python launch.py --doctor        # diagnose network / car / phone problems

Starts the stream server (rccar/stream_server.py) and the wheel reader
(rccar/g29_reader.py) as two processes, interleaves their output with
[SERVER] / [G29] tags, restarts either one if it crashes, and stops both on
Ctrl+C or when this window is closed. Leftover copies from an earlier session
(which would hold the HTTPS port — or worse, a second wheel reader fighting over
the car) are cleaned up first.
"""
import argparse
import ctypes
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
from rccar import config  # noqa: E402

PYTHON = sys.executable
EXIT_PORT_IN_USE = 3

# (tag, module, ANSI color)
CHILDREN = {
    "server": ("SERVER", "rccar.stream_server", "36"),   # cyan
    "reader": ("G29", "rccar.g29_reader", "33"),         # yellow
}
OUR_MODULES = ("rccar.stream_server", "rccar.g29_reader",
               "stream_server.py", "g29_reader.py")       # (last two: pre-2026-09 layout)

_print_lock = threading.Lock()
_procs = {}             # key -> Popen
_shutting_down = threading.Event()


def say(msg):
    with _print_lock:
        print(f"\033[1m[launch]\033[0m {msg}", flush=True)


def _pump(name, color, stream):
    """Print a child's output line by line, tagged, as it arrives."""
    prefix = f"\033[{color}m[{name}]\033[0m"
    for line in iter(stream.readline, ""):
        with _print_lock:
            sys.stdout.write(f"{prefix} {line}")
            sys.stdout.flush()
    stream.close()


def _start(key, extra_env):
    name, module, color = CHILDREN[key]
    env = os.environ.copy()
    env.update(extra_env)
    env["PYTHONUNBUFFERED"] = "1"      # piped stdout is block-buffered otherwise
    env["PYTHONIOENCODING"] = "utf-8"  # QR codes + dashes; the default cp1252 pipe mangles them
    env["RC_LAUNCHER"] = "1"           # reader: plain status lines, not the full-screen HUD
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        [PYTHON, "-u", "-m", module], cwd=BASE_DIR, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=flags)
    threading.Thread(target=_pump, args=(name, color, proc.stdout), daemon=True).start()
    return proc


def _stop_all(reason):
    if _shutting_down.is_set():
        return
    _shutting_down.set()
    say(f"{reason} — stopping...")
    # Ask nicely first: CTRL_BREAK / SIGINT reach each script's KeyboardInterrupt
    # path, so the reader still sends its neutral packet and closes its log.
    for proc in _procs.values():
        if proc.poll() is None:
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT)
            except Exception:
                pass
    deadline = time.time() + 2.5
    for proc in _procs.values():
        try:
            proc.wait(timeout=max(0.0, deadline - time.time()))
        except subprocess.TimeoutExpired:
            pass
    for key, proc in _procs.items():
        if proc.poll() is None:
            say(f"{CHILDREN[key][0]} didn't exit in time — killing it.")
            try:
                proc.kill()
            except Exception:
                pass
    say("Stopped.")


# --- leftover instances from earlier sessions ------------------------------------------

def _our_stale_processes():
    """Leftover copies of our components: [(pid, cmdline)].

    If a leftover component was started by an older launch.py that's still running,
    that launcher is returned instead (killing its tree), so it can't restart them.
    """
    if sys.platform != "win32":
        return []
    ps = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
          "Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                             text=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW).stdout
        rows = json.loads(out) if out.strip() else []
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    if isinstance(rows, dict):
        rows = [rows]
    me = os.getpid()
    by_pid = {r.get("ProcessId"): r for r in rows}
    stale = {}
    for r in rows:
        cmd = r.get("CommandLine") or ""
        pid = r.get("ProcessId")
        if pid == me or not any(m in cmd for m in OUR_MODULES):
            continue
        parent = by_pid.get(r.get("ParentProcessId"))
        if parent and parent.get("ProcessId") != me and "launch.py" in (parent.get("CommandLine") or ""):
            stale[parent["ProcessId"]] = parent.get("CommandLine") or ""
        else:
            stale[pid] = cmd
    return list(stale.items())


def _port_busy(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def _clean_up_leftovers():
    stale = _our_stale_processes()
    for pid, cmd in stale:
        say(f"Stopping a leftover copy from an earlier session (PID {pid}): {cmd[-70:]}")
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    if stale:
        time.sleep(1.0)
    if _port_busy(config.HTTPS_PORT):
        say(f"Port {config.HTTPS_PORT} is used by some other program. Close it, or run with "
            f"RC_HTTPS_PORT set to a free port.")
        return False
    return True


def _open_cockpit_when_ready():
    url = f"https://localhost:{config.HTTPS_PORT}/"
    for _ in range(40):
        if _shutting_down.is_set():
            return
        if _port_busy(config.HTTPS_PORT):
            say(f"Opening the cockpit: {url}  (first time: accept the certificate warning)")
            webbrowser.open(url)
            return
        time.sleep(0.25)


# --- Windows: catch the console window's X button, logoff, shutdown ----------------
# Ctrl+C alone only covers KeyboardInterrupt. Closing the window sends
# CTRL_CLOSE_EVENT, which only SetConsoleCtrlHandler can intercept in time.
if sys.platform == "win32":
    _HandlerRoutine = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)
    _EVENT_NAMES = {0: "Ctrl+C", 1: "Ctrl+Break", 2: "console window closed",
                    5: "user logging off", 6: "system shutting down"}

    def _console_ctrl_handler(event):
        _stop_all(_EVENT_NAMES.get(event, f"console event {event}"))
        return 1

    _handler_ref = _HandlerRoutine(_console_ctrl_handler)  # keep a reference: no GC
    ctypes.windll.kernel32.SetConsoleCtrlHandler(_handler_ref, 1)


def main():
    ap = argparse.ArgumentParser(description="FPV RC car — laptop side")
    ap.add_argument("--car-ip", help="try this car IP first (discovery still runs)")
    ap.add_argument("--no-browser", action="store_true", help="don't open the cockpit page")
    ap.add_argument("--only", choices=sorted(CHILDREN), help="run just one component")
    ap.add_argument("--doctor", action="store_true", help="diagnose network/car/phone setup")
    args = ap.parse_args()

    os.system("")  # enable ANSI escapes on classic Windows consoles
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if args.doctor:
        sys.exit(subprocess.call([PYTHON, os.path.join(BASE_DIR, "tools", "doctor.py")]))

    extra_env = {"RC_CAR_IP": args.car_ip} if args.car_ip else {}
    keys = [args.only] if args.only else ["server", "reader"]

    say("FPV RC car — Ctrl+C or close this window to stop.")
    if not _clean_up_leftovers():
        sys.exit(1)

    for key in keys:
        _procs[key] = _start(key, extra_env)
        time.sleep(0.4)   # let each banner print before the next starts
    if "server" in keys and not args.no_browser:
        threading.Thread(target=_open_cockpit_when_ready, daemon=True).start()

    restarts = {k: [] for k in keys}
    try:
        while not _shutting_down.is_set():
            for key in keys:
                proc = _procs[key]
                code = proc.poll()
                if code is None or _shutting_down.is_set():
                    continue
                name = CHILDREN[key][0]
                if key == "server" and code == EXIT_PORT_IN_USE:
                    _stop_all(f"{name} couldn't get its port")
                    break
                now = time.time()
                restarts[key] = [t for t in restarts[key] if now - t < 60] + [now]
                if len(restarts[key]) > 5:
                    _stop_all(f"{name} keeps crashing (5x in a minute) — see its output above")
                    break
                delay = min(10, 2 ** (len(restarts[key]) - 1))
                say(f"{name} exited (code {code}); restarting in {delay}s...")
                time.sleep(delay)
                if not _shutting_down.is_set():
                    _procs[key] = _start(key, extra_env)
            time.sleep(0.3)
    except KeyboardInterrupt:
        _stop_all("Ctrl+C")
    sys.exit(0)


if __name__ == "__main__":
    main()
