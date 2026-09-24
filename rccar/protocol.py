"""
Wire protocol between the laptop and the car. Keep in sync with
firmware/car_link_rx/car_link_rx.ino (DrivePacket / TelemetryPacket).
Full description: docs/protocol.md.
"""

import struct

# --- Control: laptop -> car, UDP port 4210, 50 Hz ---------------------------
DRIVE_MAGIC = 0xA5
DRIVE_VERSION = 1
DRIVE_FMT = "<BBIhhHHBB"   # magic, ver, seq, steer, drive, brake, handbrake, gear, flags
DRIVE_SIZE = struct.calcsize(DRIVE_FMT)

FLAG_HB = 0x01         # handbrake button held
FLAG_TRIM_L = 0x02     # d-pad left: nudge steering trim left
FLAG_TRIM_R = 0x04     # d-pad right: nudge steering trim right
FLAG_TRIM_RST = 0x08   # d-pad down: reset steering trim

# Gear codes: 0 neutral, 1-5 forward (output scales with gear/5),
# 6 slow reverse "R" (0.5x), 7 full reverse "r2" (1.0x).
REVERSE_GEARS = (6, 7)


def gear_name(gear):
    return {0: "N", 6: "R", 7: "r2"}.get(gear, str(gear))


def pack_drive(seq, steer, drive, brake, handbrake, gear, flags):
    """steer/drive in -1000..1000, brake/handbrake in 0..1000."""
    return struct.pack(DRIVE_FMT, DRIVE_MAGIC, DRIVE_VERSION, seq & 0xFFFFFFFF,
                       steer, drive, brake, handbrake, gear, flags)


def pack_neutral(seq):
    """All-neutral packet: used on shutdown, when the wheel is unplugged, and as
    the discovery probe (the car answers any valid packet with telemetry)."""
    return pack_drive(seq, 0, 0, 0, 0, 0, 0)


# --- Telemetry: car -> laptop, 10 Hz, sent to whoever sent the last control packet
TELE_MAGIC = 0x5A
TELE_VERSION = 3
TELE_FMT = "<BBIBBBBhhHHhHIbBH"
TELE_SIZE = struct.calcsize(TELE_FMT)

DRV_NAMES = {0: "neutral", 1: "forward", 2: "braking", 3: "rev-armed", 4: "reverse"}
DRV_CODES = {0: "N", 1: "F", 2: "B", 3: "x", 4: "R"}

# esp_reset_reason() -> meaning (why the car last booted).
RESET_NAMES = {
    0: "UNKNOWN", 1: "POWERON (power/manual)", 2: "EXT", 3: "SW (sw reboot / OTA)",
    4: "PANIC (crash!)", 5: "INT_WDT (interrupt watchdog -> hang)",
    6: "TASK_WDT (task watchdog -> loop hang)", 7: "WDT (watchdog)",
    8: "DEEPSLEEP", 9: "BROWNOUT (power sag!)", 10: "SDIO",
}
FAULT_RESETS = {4, 5, 6, 7, 9}


def is_telemetry(data):
    return len(data) == TELE_SIZE and data[0] == TELE_MAGIC and data[1] == TELE_VERSION


def parse_telemetry(data):
    """Decode a telemetry datagram into a dict, or None if it isn't one."""
    if not is_telemetry(data):
        return None
    (_, _, seq, gear, drv, link, flags, steer, esc_off, brake, hb, trim, pps, lost,
     rssi, reset, heap) = struct.unpack(TELE_FMT, data)
    return {
        "seq": seq,
        "gear": gear,
        "drv": drv,
        "drvName": DRV_NAMES.get(drv, "?"),
        "link": bool(link),          # car is receiving control packets (not in failsafe)
        "flags": flags,
        "steer": steer,              # echo of the commanded steering, -1000..1000
        "escOff": esc_off,           # actual ESC output, microseconds from neutral (+-500)
        "brake": brake,
        "handbrake": hb,
        "trim": trim,                # steering trim, microseconds
        "pps": pps,                  # control packets/s the car is receiving
        "lost": lost,                # cumulative control packets the car missed
        "rssi": rssi,                # car WiFi RSSI in dBm; 0 = not connected
        "reset": reset,              # esp_reset_reason() of the last boot
        "heap": heap,                # free heap, KB
    }


def reset_name(code):
    try:
        return RESET_NAMES.get(int(code), f"code {int(code)}")
    except (TypeError, ValueError):
        return "?"
