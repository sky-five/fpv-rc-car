"""
Network doctor: checks everything between the laptop, the car and the phone, and
says what to fix. Run it when something doesn't connect:

    python launch.py --doctor        (or: python tools/doctor.py)

It only looks (and sends neutral probe packets, which the car ignores safely);
it changes nothing.
"""

import json
import os
import socket
import ssl
import subprocess
import sys
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from rccar import config, discovery, mdns, netinfo, protocol  # noqa: E402

problems = []


def head(title):
    print(f"\n== {title} " + "=" * max(0, 60 - len(title)))


def ok(msg):
    print(f"  ok    {msg}")


def warn(msg, fix=None):
    print(f"  WARN  {msg}")
    if fix:
        print(f"        -> {fix}")
        problems.append(fix)


def info(msg):
    print(f"        {msg}")


def network_profiles():
    """Windows network category per interface (Public blocks inbound connections)."""
    if sys.platform != "win32":
        return {}
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-NetConnectionProfile | Select-Object InterfaceAlias,NetworkCategory,Name "
             "| ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW).stdout
        rows = json.loads(out) if out.strip() else []
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    if isinstance(rows, dict):
        rows = [rows]
    cat = {0: "Public", 1: "Private", 2: "DomainAuthenticated"}
    return {r["InterfaceAlias"]: cat.get(r["NetworkCategory"], str(r["NetworkCategory"]))
            for r in rows}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("FPV RC car — network doctor")

    head("Laptop network interfaces")
    ifaces = netinfo.interfaces(max_age=0)
    profiles = network_profiles()
    for i in ifaces:
        prof = profiles.get(i.name)
        info(f"{i.kind:9} {i.name or '?':24} {i.ip}/{i.network.prefixlen}"
             + (f"   [{prof} network]" if prof else ""))
    lans = netinfo.lan_interfaces()
    if not lans:
        warn("No LAN connection at all.", "Connect the laptop's Wi-Fi to the car's network.")
    if any(i.kind == "vpn" for i in ifaces):
        info("(VPN/virtual adapters are ignored for the car and phone — that's expected.)")

    head("Car")
    cached = discovery.load_cached_ip()
    info(f"last known IP: {cached or 'none yet'}")
    via_mdns = mdns.resolve_host(config.CAR_HOSTNAME, timeout=2.0)
    info(f"mDNS {config.CAR_HOSTNAME}.local: {', '.join(via_mdns) or 'no answer'}")
    ip, via, tele = discovery.find_car(timeout=8)
    if not ip:
        warn("The car did not answer (tried last-known IP, mDNS, OS resolver, broadcast).",
             "Is the car powered and its OLED showing an IP? Is the laptop on the same WiFi as the car?")
    else:
        ok(f"car answered at {ip} (found via {via})")
        iface = netinfo.iface_for(ip)
        if iface:
            ok(f"laptop reaches it through {iface.label()}")
        else:
            warn(f"no laptop interface is on the car's subnet ({ip}); traffic goes via a router",
                 "Join the laptop to the same WiFi as the car.")
        if tele:
            info(f"link {'live' if tele['link'] else 'idle/failsafe (normal when nothing is driving)'}, "
                 f"car WiFi {tele['rssi']} dBm, heap {tele['heap']} KB, "
                 f"last boot: {protocol.reset_name(tele['reset'])}")
            if tele["reset"] in protocol.FAULT_RESETS:
                warn(f"the car's last reboot was a fault: {protocol.reset_name(tele['reset'])}",
                     "Check power (brownout) / run tools/analyze_log.py on the latest drive log.")
            if tele["rssi"] and tele["rssi"] < -75:
                warn(f"weak car WiFi ({tele['rssi']} dBm)", "Move the car/router closer, or add an antenna.")
        svc = mdns.car_service(2.0)
        fw = next((s["txt"].get("fw") for s in svc if s["txt"].get("fw")), None)
        info(f"firmware: {fw or '1.x (no version advertised; 2.x adds rollback protection)'}")
        if not via_mdns:
            info(f"(mDNS didn't answer, but {via} did — fine, discovery tries all of them.)")

    head("Phone")
    cands = netinfo.phone_candidates(ip)
    if cands:
        best = cands[0]
        ok(f"phone should open  https://{best.ip}:{config.HTTPS_PORT}/phone   ({best.label()})")
        prof = profiles.get(best.name)
        if prof == "Public":
            warn(f"Windows treats '{best.name}' as a PUBLIC network, which blocks the phone from "
                 f"reaching this laptop.",
                 f"Settings -> Network & internet -> {best.name} -> Network profile type: Private "
                 f"(or allow Python through Windows Firewall on Public networks).")
    else:
        warn("no LAN address for the phone to reach", "Connect the laptop to the car's WiFi.")

    head("Laptop server")
    s = socket.socket()
    s.settimeout(0.5)
    running = s.connect_ex(("127.0.0.1", config.HTTPS_PORT)) == 0
    s.close()
    if not running:
        info(f"not running (port {config.HTTPS_PORT} free) — start it with: python launch.py")
    else:
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(f"https://127.0.0.1:{config.HTTPS_PORT}/api/status",
                                        context=ctx, timeout=3) as r:
                st = json.load(r)
            ok("stream server is running")
            info(f"phone connected: {'yes (' + str(st.get('phoneAddr')) + ')' if st.get('phone') else 'no'}")
            car = st.get("car")
            info(f"wheel reader: {'running, car ' + car['state'] if car else 'not running'}")
        except Exception as e:
            warn(f"something is on port {config.HTTPS_PORT} but it isn't our server ({e})",
                 "Close the other program, or set RC_HTTPS_PORT.")

    head("Wheel")
    try:
        os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
        import pygame
        pygame.joystick.init()
        names = [pygame.joystick.Joystick(i).get_name() for i in range(pygame.joystick.get_count())]
        if any("driving force" in n.lower() for n in names):
            ok("G29 connected")
        else:
            warn("G29 not detected", "Plug in the wheel (USB) and make sure Logitech G HUB sees it.")
        for n in names:
            info(n)
    except Exception as e:
        info(f"(couldn't check: {e})")

    head("Summary")
    if problems:
        for p in dict.fromkeys(problems):
            print(f"  * {p}")
    else:
        print("  Nothing wrong found.")


if __name__ == "__main__":
    main()
