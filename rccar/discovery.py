"""
Find the car on whatever network we're on — no IP addresses to edit, ever.

The trick that makes this work with ANY firmware version: the car answers every
valid control packet with telemetry, sent back to the sender. So we send an
all-neutral control packet (harmless: it's what the car does in failsafe anyway)
to every place the car might be, and whoever answers with telemetry is the car.

Where we look, all in parallel, until someone answers:
  1. RC_CAR_IP / --car-ip, if you set one (optional).
  2. The last IP the car had (cached in .rccar/car.json) — instant on a re-launch.
  3. mDNS: "rc-car.local" (ArduinoOTA advertises it).
  4. The OS resolver for rc-car.local (Windows 10+ speaks mDNS too).
  5. UDP broadcast on every real LAN interface — finds the car even if mDNS is
     blocked by the router, as long as broadcasts get through.

After the car is found we talk to it by unicast only. If its telemetry goes
silent for CAR_LOST_S (reboot, new DHCP lease, out of range), we fall back to
searching — sending neutral probes, never stale steering — and re-lock the moment
it answers again, even if it came back with a different IP.
"""

import json
import os
import select
import socket
import threading
import time

from . import config, mdns, netinfo, protocol

PROBE_PERIOD_S = 0.25
RESOLVE_PERIOD_S = 3.0
CACHE_PATH = os.path.join(config.STATE_DIR, "car.json")


def load_cached_ip():
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f).get("ip")
    except (OSError, ValueError):
        return None


def save_cached_ip(ip):
    try:
        os.makedirs(config.STATE_DIR, exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({"ip": ip, "seen": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
    except OSError:
        pass


def _resolve_os(hostname, timeout=2.0):
    """getaddrinfo can block for seconds on a miss; run it in a throwaway thread."""
    result = []

    def work():
        try:
            for info in socket.getaddrinfo(hostname + ".local", None, socket.AF_INET):
                if info[4][0] not in result:
                    result.append(info[4][0])
        except OSError:
            pass

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    return list(result)


class CarFinder:
    """Keeps track of where the car is. Driven from the reader's send loop.

    sock: the caller's non-blocking UDP socket (control + telemetry). Unicast probes
    go out on it so the car's replies come back to the same place real telemetry does.
    """

    def __init__(self, sock, port=config.CAR_UDP_PORT, hostname=config.CAR_HOSTNAME,
                 override_ip=config.CAR_IP_OVERRIDE):
        self.sock = sock
        self.port = port
        self.hostname = hostname
        self.ip = None                 # locked car IP (None = searching)
        self.via = None                # how we found it
        self._cands = {}               # ip -> source label
        self._lock = threading.Lock()
        self._bcast = []               # [(socket, Iface)]
        self._bcast_key = None
        self._next_probe = 0.0
        self._stop = threading.Event()
        if override_ip:
            self._cands[override_ip] = "override"
        cached = load_cached_ip()
        if cached and cached not in self._cands:
            self._cands[cached] = "last known"
        self._thread = threading.Thread(target=self._resolver_loop, daemon=True)
        self._thread.start()

    # --- lifecycle -----------------------------------------------------------
    def close(self):
        self._stop.set()
        for s, _ in self._bcast:
            s.close()
        self._bcast = []

    @property
    def locked(self):
        return self.ip is not None

    def lock(self, ip):
        with self._lock:
            src = self._cands.get(ip, "broadcast")
        self.ip, self.via = ip, src
        save_cached_ip(ip)

    def unlock(self):
        """Car went silent: keep its last IP as a candidate and start searching."""
        if self.ip:
            with self._lock:
                self._cands.setdefault(self.ip, "last known")
        self.ip, self.via = None, None
        self._next_probe = 0.0

    # --- searching -------------------------------------------------------------
    def _resolver_loop(self):
        while not self._stop.is_set():
            if self.ip is None:
                for ip in mdns.resolve_host(self.hostname, timeout=1.2):
                    with self._lock:
                        self._cands.setdefault(ip, "mDNS")
                for ip in _resolve_os(self.hostname):
                    with self._lock:
                        self._cands.setdefault(ip, "DNS")
            self._stop.wait(RESOLVE_PERIOD_S)

    def _refresh_broadcast_sockets(self):
        lans = netinfo.lan_interfaces()
        key = tuple(i.ip for i in lans)
        if key == self._bcast_key:
            return
        for s, _ in self._bcast:
            s.close()
        self._bcast, self._bcast_key = [], key
        for iface in lans:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.bind((iface.ip, 0))
                s.setblocking(False)
                self._bcast.append((s, iface))
            except OSError:
                s.close()

    def probe(self, seq):
        """While searching: send a neutral probe everywhere (rate-limited).

        Returns True if a probe round went out (the caller should then advance seq).
        """
        now = time.monotonic()
        if self.ip is not None or now < self._next_probe:
            return False
        self._next_probe = now + PROBE_PERIOD_S
        pkt = protocol.pack_neutral(seq)
        with self._lock:
            targets = list(self._cands)
        for ip in targets:
            try:
                self.sock.sendto(pkt, (ip, self.port))
            except OSError:
                pass            # network down / unreachable: keep trying
        self._refresh_broadcast_sockets()
        for s, iface in self._bcast:
            for dest in (iface.broadcast, "255.255.255.255"):
                try:
                    s.sendto(pkt, (dest, self.port))
                except OSError:
                    pass
        return True

    def drain_broadcast_replies(self):
        """Telemetry that answered a broadcast probe: [(data, (ip, port))]."""
        out = []
        for s, _ in self._bcast:
            while True:
                try:
                    data, addr = s.recvfrom(256)
                except (BlockingIOError, InterruptedError):
                    break
                except OSError:
                    break
                if protocol.is_telemetry(data):
                    out.append((data, addr))
        return out

    def describe_search(self):
        with self._lock:
            cands = [f"{ip} ({src})" for ip, src in self._cands.items()]
        nets = [i.label() for _, i in self._bcast] or ["no LAN interface up"]
        parts = [f"'{self.hostname}.local'"]
        if cands:
            parts.append("tried " + ", ".join(cands))
        parts.append("broadcast on " + ", ".join(nets))
        return "; ".join(parts)


def find_car(timeout=6.0, verbose=False):
    """One-shot blocking search (for tools). Returns (ip, via, telemetry) or (None, None, None)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    sock.setblocking(False)
    finder = CarFinder(sock)
    seq = 0
    deadline = time.monotonic() + timeout
    shown = False
    try:
        while time.monotonic() < deadline:
            if finder.probe(seq):
                seq += 1
            replies = finder.drain_broadcast_replies()
            try:
                ready, _, _ = select.select([sock], [], [], 0.05)
                if ready:
                    data, addr = sock.recvfrom(256)
                    if protocol.is_telemetry(data):
                        replies.append((data, addr))
            except OSError:
                pass
            if replies:
                data, addr = replies[0]
                finder.lock(addr[0])
                return addr[0], finder.via, protocol.parse_telemetry(data)
            if verbose and not shown and time.monotonic() > deadline - timeout + 1.5:
                print("  searching:", finder.describe_search())
                shown = True
        return None, None, None
    finally:
        finder.close()
        sock.close()
