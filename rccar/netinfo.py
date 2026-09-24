"""
Local network interfaces: which IPv4 addresses this laptop has, which are real LAN
links (vs VPN / virtual / link-local), and which one the phone should use.

Why this matters (learned the hard way): the laptop often has several addresses at
once — Wi-Fi on the car's network, a USB-C Ethernet on the home network, and a
Tailscale VPN (100.x). Printing "the" IP picks the wrong one half the time.
"""

import ipaddress
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass

_VIRTUAL_NAME = re.compile(
    r"tailscale|zerotier|vpn|wireguard|openvpn|hyper-v|vethernet|virtualbox|vmware|wsl|loopback",
    re.I)
_WIFI_NAME = re.compile(r"wi-?fi|wireless|wlan", re.I)
_CGNAT = ipaddress.ip_network("100.64.0.0/10")     # Tailscale and carrier-grade NAT


@dataclass(frozen=True)
class Iface:
    name: str          # adapter name ("Wi-Fi", "Ethernet TypeC", "Tailscale") or ""
    ip: str
    netmask: str = "255.255.255.0"

    @property
    def network(self):
        return ipaddress.ip_network(f"{self.ip}/{self.netmask}", strict=False)

    @property
    def broadcast(self):
        return str(self.network.broadcast_address)

    @property
    def kind(self):
        """'lan' (a real local network), 'vpn', 'linklocal' or 'loopback'."""
        addr = ipaddress.ip_address(self.ip)
        if addr.is_loopback:
            return "loopback"
        if addr.is_link_local:
            return "linklocal"
        if addr in _CGNAT or _VIRTUAL_NAME.search(self.name or ""):
            return "vpn"
        return "lan"

    @property
    def is_wifi(self):
        return bool(_WIFI_NAME.search(self.name or ""))

    def contains(self, ip):
        try:
            return ipaddress.ip_address(ip) in self.network
        except ValueError:
            return False

    def label(self):
        return f"{self.name or 'interface'} {self.ip}"


def _windows_ifaces():
    """Parse `ipconfig` for adapter names + IPv4 + subnet mask (stdlib has no API)."""
    try:
        out = subprocess.run(["ipconfig"], capture_output=True, text=True, timeout=5,
                             encoding="utf-8", errors="replace",
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    ifaces, name, ip = [], "", None
    for line in out.splitlines():
        if line and not line[0].isspace() and line.rstrip().endswith(":"):
            name = re.sub(r"^.*?adapter\s+", "", line.rstrip()[:-1]).strip()
            ip = None
            continue
        m = re.search(r"IPv4 Address[ .]*:\s*([\d.]+)", line)
        if m:
            ip = m.group(1)
            continue
        m = re.search(r"Subnet Mask[ .]*:\s*([\d.]+)", line)
        if m and ip:
            ifaces.append(Iface(name, ip, m.group(1)))
            ip = None
    return ifaces


def _generic_ifaces():
    """Fallback for non-Windows: addresses only, /24 assumed."""
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            if info[4][0] not in ips:
                ips.append(info[4][0])
    except OSError:
        pass
    primary = route_ip_to("8.8.8.8")
    if primary and primary not in ips:
        ips.insert(0, primary)
    return [Iface("", ip) for ip in ips]


_cache = (0.0, [])


def interfaces(max_age=5.0):
    """All IPv4 interfaces except loopback. Cached for max_age seconds."""
    global _cache
    now = time.monotonic()
    if now - _cache[0] < max_age:
        return _cache[1]
    found = _windows_ifaces() if sys.platform == "win32" else []
    if not found:
        found = _generic_ifaces()
    found = [i for i in found if i.kind != "loopback"]
    _cache = (now, found)
    return found


def lan_interfaces():
    """Interfaces worth searching for the car / offering to the phone (real LANs)."""
    return [i for i in interfaces() if i.kind == "lan"]


def route_ip_to(dest):
    """The local IP the OS would use to reach dest (no packet is actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((dest, 9))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def iface_for(ip):
    """The local interface on the same subnet as ip, if any."""
    for i in interfaces():
        if i.kind != "loopback" and i.contains(ip):
            return i
    return None


def phone_candidates(car_ip=None):
    """Laptop addresses for the phone URL, best first.

    The phone is on the car's WiFi, so once the car is known the best address is
    the laptop interface on the car's subnet. Before that, prefer Wi-Fi LANs.
    """
    lans = lan_interfaces()
    if car_ip:
        same = [i for i in lans if i.contains(car_ip)]
        if same:
            return same + [i for i in lans if i not in same]
    return sorted(lans, key=lambda i: (not i.is_wifi,))
