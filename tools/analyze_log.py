#!/usr/bin/env python3
"""Root-cause analyzer for rc_log_*.csv produced by rccar/g29_reader.py.

Usage:
    python tools/analyze_log.py [path/to/log.csv]

With no argument it picks the newest logs/rc_log_*.csv. It separates the failure
modes and reports each it finds:
  - CAR REBOOT / HANG: the chip restarted mid-drive (lost counter reset) or the
    watchdog fired. The reset reason (telemetry v3+) says why: watchdog = a loop hang,
    panic = crash, brownout = power, poweron = manual/power cycle.
  - CAR-SIDE FREEZE: control loop stalled (telemetry gaps >1s + pps spikes = RX
    buffer overflow then burst-drain). Usually a blocking firmware call.
  - LAPTOP STALL: the Python sender froze (send_late jitter) before any RF loss.
  - RF / SIGNAL: losses cluster at weaker RSSI (range / interference).
"""
import csv
import glob
import os
import sys
from collections import Counter
from statistics import median

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from rccar.config import LOG_DIR  # noqa: E402
from rccar.protocol import FAULT_RESETS, RESET_NAMES  # noqa: E402,F401

SEND_HZ = 50
PPS_SPIKE = 80


def newest_log():
    files = glob.glob(os.path.join(LOG_DIR, "rc_log_*.csv"))
    return max(files, key=os.path.getmtime) if files else None


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def rssis_of(rows):
    return [v for v in (fnum(r.get("rssi")) for r in rows) if v not in (None, 0.0)]


def describe(vals):
    if not vals:
        return "n/a"
    return f"min {min(vals):.0f}  median {median(vals):.0f}  max {max(vals):.0f} dBm"


def reset_name(code):
    try:
        return RESET_NAMES.get(int(code), f"code {int(code)}")
    except (TypeError, ValueError):
        return "?"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    path = sys.argv[1] if len(sys.argv) > 1 else newest_log()
    if not path or not os.path.exists(path):
        print("No log found. Pass a CSV path, or run a drive to create logs/rc_log_*.csv.")
        sys.exit(1)

    rows = load(path)
    if not rows:
        print(f"{path} is empty.")
        sys.exit(1)

    tele = [r for r in rows if r["event"] == "tele"]
    counts = Counter(r["event"] for r in rows)

    t_all = [fnum(r["t_ms"]) for r in rows if fnum(r["t_ms"]) is not None]
    dur_s = (max(t_all) - min(t_all)) / 1000.0 if t_all else 0.0
    tele_hz = len(tele) / dur_s if dur_s else 0.0

    overall_rssi = rssis_of(tele)
    lost_vals = [fnum(r["lost"]) for r in tele if fnum(r["lost"]) is not None]
    total_lost = (max(lost_vals) - min(lost_vals)) if lost_vals else 0

    failsafe = [r for r in rows if r["event"] == "FAILSAFE_enter"]
    lost_ev = [r for r in rows if r["event"] == "lost"]
    stalls = [r for r in rows if r["event"] == "tele_stall"]
    late = [r for r in rows if r["event"] == "send_late"]
    freezes = [r for r in rows if r["event"] == "car_freeze"]
    reboots = [r for r in rows if r["event"] == "car_reboot"]
    pps_spikes = [r for r in tele if (fnum(r["pps"]) or 0) > PPS_SPIKE]
    max_gap = max((fnum(r["tele_gap_ms"]) or 0 for r in tele), default=0)
    worst_jit = max((fnum(r["send_jit_ms"]) or 0 for r in late), default=0)

    reset_codes = {int(fnum(r["reset"])) for r in tele
                   if fnum(r.get("reset")) is not None}
    heaps = [fnum(r["heap"]) for r in tele if fnum(r.get("heap"))]

    print("=" * 64)
    print(f"  Log: {path}")
    print(f"  Duration {dur_s:.1f}s   telemetry {len(tele)} pkts (~{tele_hz:.1f} Hz)")
    print(f"  RSSI over drive: {describe(overall_rssi)}")
    print(f"  Total control packets the car missed (lost): {int(total_lost)}")
    print(f"  Car-freeze signs: {len(freezes)} gaps >1s (max {max_gap:.0f}ms),"
          f" {len(pps_spikes)} pps spikes >{PPS_SPIKE}")
    if reset_codes:
        print(f"  Reset reasons seen: {', '.join(reset_name(c) for c in sorted(reset_codes))}")
    if heaps:
        leak = "  <- declining, possible leak" if heaps[-1] < heaps[0] - 20 else ""
        print(f"  Free heap: {min(heaps):.0f}-{max(heaps):.0f} KB{leak}")
    print("-" * 64)
    print("  Event counts:")
    for k in ("car_reboot", "boot_reason", "FAILSAFE_enter", "failsafe_exit", "lost",
              "tele_gap", "car_freeze", "tele_stall", "send_late"):
        if counts.get(k):
            print(f"    {k:16} {counts[k]}")
    if not any(counts.get(k) for k in
               ("FAILSAFE_enter", "lost", "tele_stall", "send_late", "car_freeze", "car_reboot")):
        print("    (no failures recorded — clean drive)")
    print("-" * 64)

    if reboots:
        print("  Reboots mid-drive:")
        for r in reboots:
            print(f"    t={float(r['t_ms'])/1000:7.1f}s  reset={reset_name(r.get('reset'))}")
        print("-" * 64)

    bursts = sorted(lost_ev, key=lambda r: fnum(r["dlost"]) or 0, reverse=True)[:8]
    if bursts:
        print("  Worst packet-loss bursts (pkts lost @ RSSI):")
        for r in bursts:
            print(f"    t={float(r['t_ms'])/1000:7.1f}s  -{int(fnum(r['dlost']))} pkts"
                  f"   rssi {r['rssi'] or '?'} dBm   pps {r['pps'] or '?'}")
        print("-" * 64)

    fail_rows = failsafe + lost_ev
    fail_rssi = rssis_of(fail_rows)
    rf_correlated = False
    if fail_rssi and overall_rssi:
        omed = median(overall_rssi)
        fmed = median(fail_rssi)
        print(f"  RSSI at failures: {describe(fail_rssi)}")
        print(f"  (overall median {omed:.0f} dBm; at failures {fmed:.0f} dBm)")
        rf_correlated = fmed <= omed - 4

    fault_seen = reset_codes & FAULT_RESETS
    car_freeze_sig = len(freezes) >= 1 or len(pps_spikes) >= 5
    laptop_sig = bool(late and (worst_jit > 200 or len(late) >= 5))

    print("=" * 64)
    print("  VERDICT:")
    found = False
    if reboots or fault_seen:
        found = True
        print(f"  * CAR REBOOT/HANG (primary): {len(reboots)} mid-drive restart(s).")
        if fault_seen:
            for c in sorted(fault_seen):
                print(f"    reset reason {reset_name(c)} -> "
                      + ("loop hung; watchdog auto-rebooted" if c in (5, 6, 7)
                         else "firmware crash" if c == 4
                         else "power supply sag" if c == 9 else "fault"))
        else:
            print("    Reset reason POWERON only = manual power cycle / power loss, not a")
            print("    software fault. If you rebooted it, the car had already gone")
            print("    unresponsive — look at the events just before each reboot.")
    if car_freeze_sig:
        found = True
        print(f"  * CAR-SIDE FREEZE: {len(freezes)} gaps >1s, {len(pps_spikes)} pps spikes.")
        print("    Control loop stalled (RX overflow + burst-drain). Blocking firmware call.")
    if laptop_sig:
        found = True
        print(f"  * LAPTOP STALL: {len(late)} late sends (worst {worst_jit:.0f}ms).")
    if rf_correlated:
        found = True
        print("  * RF / SIGNAL: losses cluster at weaker RSSI — range/interference contributes.")
    if not found:
        if failsafe or lost_ev or stalls:
            print("  Failures present but no clear reboot / freeze / jitter / RSSI signature.")
        else:
            print("  No link failures in this log. Drive longer or push to weaker signal.")
    print("=" * 64)


if __name__ == "__main__":
    main()
