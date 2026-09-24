"""
Minimal mDNS (multicast DNS) client — just enough to find the car by name.

The car's firmware calls ArduinoOTA.setHostname("rc-car"), which also advertises
"rc-car.local" over mDNS. Firmware 2.x additionally advertises a "_rccar._udp"
service whose TXT record carries the firmware version.

We send "legacy unicast" queries (from an ephemeral port, not 5353), so responders
answer straight back to our socket: no need to bind port 5353, which Windows' own
mDNS service already holds. No third-party dependency.
"""

import random
import select
import socket
import struct
import time

from . import netinfo

MDNS_ADDR = ("224.0.0.251", 5353)
T_A, T_PTR, T_TXT, T_SRV = 1, 12, 16, 33


def _encode_name(name):
    out = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode("utf-8")
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def _read_name(data, off):
    """Decode a (possibly compressed) DNS name. Returns (name, offset after it)."""
    labels, jumped, end, hops = [], False, off, 0
    while True:
        if off >= len(data) or hops > 64:
            raise ValueError("bad name")
        ln = data[off]
        if ln == 0:
            off += 1
            break
        if ln & 0xC0 == 0xC0:                       # compression pointer
            ptr = ((ln & 0x3F) << 8) | data[off + 1]
            if not jumped:
                end = off + 2
            off, jumped, hops = ptr, True, hops + 1
            continue
        labels.append(data[off + 1:off + 1 + ln].decode("utf-8", "replace"))
        off += 1 + ln
    return ".".join(labels), (end if jumped else off)


def build_query(name, qtype):
    qid = random.randint(1, 0xFFFF)
    header = struct.pack("!HHHHHH", qid, 0, 1, 0, 0, 0)
    # Class IN with the QU ("unicast response please") bit set.
    return header + _encode_name(name) + struct.pack("!HH", qtype, 0x8001)


def parse_records(data):
    """All resource records in a DNS message as (name, type, value) tuples."""
    recs = []
    try:
        _, _, qd, an, ns, ar = struct.unpack("!HHHHHH", data[:12])
        off = 12
        for _ in range(qd):
            _, off = _read_name(data, off)
            off += 4
        for _ in range(an + ns + ar):
            rname, off = _read_name(data, off)
            rtype, _, _, rdlen = struct.unpack("!HHIH", data[off:off + 10])
            off += 10
            rdata_off, off = off, off + rdlen
            if rtype == T_A and rdlen == 4:
                val = socket.inet_ntoa(data[rdata_off:rdata_off + 4])
            elif rtype == T_PTR:
                val, _ = _read_name(data, rdata_off)
            elif rtype == T_SRV:
                _, _, port = struct.unpack("!HHH", data[rdata_off:rdata_off + 6])
                target, _ = _read_name(data, rdata_off + 6)
                val = (target, port)
            elif rtype == T_TXT:
                txt, p = {}, rdata_off
                while p < rdata_off + rdlen:
                    ln = data[p]
                    item = data[p + 1:p + 1 + ln].decode("utf-8", "replace")
                    p += 1 + ln
                    if item:
                        k, _, v = item.partition("=")
                        txt[k] = v
                val = txt
            else:
                continue
            recs.append((rname.lower().rstrip("."), rtype, val))
    except (ValueError, struct.error, IndexError):
        pass
    return recs


def query(name, qtype, timeout=1.5, source_ips=None):
    """Ask every LAN interface; return (records, responder_ips) seen before timeout."""
    if source_ips is None:
        source_ips = [i.ip for i in netinfo.lan_interfaces()]
    msg = build_query(name, qtype)
    socks = []
    for ip in source_ips:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind((ip, 0))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            s.setblocking(False)
            s.sendto(msg, MDNS_ADDR)
            socks.append(s)
        except OSError:
            s.close()
    records, responders = [], set()
    deadline = time.monotonic() + timeout
    try:
        while socks:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            ready, _, _ = select.select(socks, [], [], left)
            for s in ready:
                try:
                    data, addr = s.recvfrom(4096)
                except OSError:
                    continue
                recs = parse_records(data)
                if recs:
                    records.extend(recs)
                    responders.add(addr[0])
    finally:
        for s in socks:
            s.close()
    return records, responders


def resolve_host(hostname, timeout=1.5):
    """IPv4 addresses for '<hostname>.local' (hostname given with or without .local)."""
    fqdn = hostname if hostname.endswith(".local") else hostname + ".local"
    records, _ = query(fqdn, T_A, timeout)
    return sorted({v for (n, t, v) in records if t == T_A and n == fqdn.lower()})


def car_service(timeout=1.5):
    """Browse for the firmware-2.x '_rccar._udp' service.

    Returns a list of dicts: {"host", "ip", "port", "txt"} (txt has e.g. fw=2.0.0).
    Empty for firmware 1.x, which only advertises the hostname.
    """
    records, _ = query("_rccar._udp.local", T_PTR, timeout)
    a = {n: v for (n, t, v) in records if t == T_A}
    srv = {n: v for (n, t, v) in records if t == T_SRV}
    txt = {n: v for (n, t, v) in records if t == T_TXT}
    found = []
    for (n, t, inst) in records:
        if t != T_PTR or n != "_rccar._udp.local":
            continue
        target, port = srv.get(inst, (None, None))
        found.append({"host": target, "ip": a.get(target), "port": port,
                      "txt": txt.get(inst, {})})
    return found
