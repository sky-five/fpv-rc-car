"""
Stream server: HTTPS pages + WebRTC signaling + telemetry fan-out.

  https://localhost:8443/        laptop cockpit (video + HUD), shows a QR code for the phone
  https://<laptop>:8443/phone    phone camera page (scan the QR code)
  /ws                            WebSocket: signaling relay, telemetry, status
  /ca.crt                        local CA — install on the phone once to silence cert warnings

The video itself goes phone -> laptop browser directly (WebRTC P2P); this server only
brokers the connection. Telemetry arrives from g29_reader.py on 127.0.0.1:5055.

Run via `python launch.py`, or standalone:  python -m rccar.stream_server
"""

import asyncio
import io
import json
import os
import signal
import ssl
import sys
import time

try:
    from aiohttp import web, WSMsgType
    import segno
except ImportError as e:
    sys.exit(f"Missing dependency ({e.name}). Run:  pip install -r requirements.txt")

from . import certs, config, netinfo, protocol

EXIT_PORT_IN_USE = 3


class Hub:
    """Shared state: who's connected, what the car is doing."""

    def __init__(self):
        self.clients = set()
        self.roles = {"phone": None, "viewer": None}
        self.addrs = {}
        self.car = None                # latest car status from g29_reader
        self.car_seen = 0.0
        self.phone_urls = []           # [(url, iface label)] best first
        self._printed_url = None

    # --- sending -----------------------------------------------------------------
    async def send(self, ws, obj):
        try:
            await ws.send_str(json.dumps(obj))
        except Exception:
            self.clients.discard(ws)

    async def broadcast(self, obj):
        msg = json.dumps(obj)
        for ws in list(self.clients):
            try:
                await ws.send_str(msg)
            except Exception:
                self.clients.discard(ws)

    async def send_to(self, role, obj):
        ws = self.roles.get(role)
        if ws is not None:
            await self.send(ws, obj)

    # --- status ------------------------------------------------------------------
    def status(self):
        car = self.car if (self.car and time.monotonic() - self.car_seen < 3) else None
        return {
            "type": "status",
            "phone": self.roles["phone"] is not None,
            "phoneAddr": self.addrs.get(self.roles["phone"]),
            "car": car,
            "phoneUrls": [{"url": u, "iface": lbl} for u, lbl in self.phone_urls],
            "readerRunning": car is not None,
        }

    async def push_status(self):
        viewer = self.roles["viewer"]
        if viewer is not None:
            await self.send(viewer, self.status())

    def refresh_phone_urls(self):
        car_ip = (self.car or {}).get("ip")
        self.phone_urls = [(f"https://{i.ip}:{config.HTTPS_PORT}/phone", i.label())
                           for i in netinfo.phone_candidates(car_ip)]
        best = self.phone_urls[0][0] if self.phone_urls else None
        if best != self._printed_url:
            self._printed_url = best
            print_phone_banner(self.phone_urls)
            return True
        return False


def print_phone_banner(urls):
    if not urls:
        print("  Phone: no LAN connection — join the laptop to the car's WiFi.", flush=True)
        return
    best, label = urls[0]
    print(f"  Phone camera -> {best}   ({label})", flush=True)
    for u, lbl in urls[1:]:
        print(f"                  {u}   ({lbl})", flush=True)
    buf = io.StringIO()
    segno.make(best, error="l").terminal(out=buf, compact=True, border=2)
    print("  Scan with the phone (or use the QR code on the cockpit page):", flush=True)
    for row in buf.getvalue().splitlines():
        print("  " + row, flush=True)


hub = Hub()


# --- HTTP routes -----------------------------------------------------------------

def _no_cache(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


async def index(request):
    return _no_cache(web.FileResponse(os.path.join(config.WEB_DIR, "viewer.html")))


async def phone(request):
    return _no_cache(web.FileResponse(os.path.join(config.WEB_DIR, "phone.html")))


async def ca_cert(request):
    return web.Response(body=certs.ca_der(), content_type="application/x-x509-ca-cert",
                        headers={"Content-Disposition": 'attachment; filename="rc-fpv-ca.crt"'})


async def api_status(request):
    hub.refresh_phone_urls()
    return _no_cache(web.json_response(hub.status()))


async def qr_svg(request):
    """QR code for one of the phone URLs (?i=index). Only our own URLs, not arbitrary text."""
    try:
        url = hub.phone_urls[int(request.query.get("i", "0"))][0]
    except (ValueError, IndexError):
        raise web.HTTPNotFound()
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=8, border=2,
                                    dark="#000000", light="#ffffff")
    return _no_cache(web.Response(body=buf.getvalue(), content_type="image/svg+xml"))


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    hub.clients.add(ws)
    hub.addrs[ws] = request.remote
    role = None
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            t = data.get("type")
            if t == "hello":
                role = data.get("role")
                if role in hub.roles:
                    replaced = hub.roles[role] is not None
                    hub.roles[role] = ws
                    print(f"[{role}] connected from {request.remote}"
                          + (" (replaced an older one)" if replaced else ""), flush=True)
                if role == "viewer":
                    hub.refresh_phone_urls()
                if hub.roles["phone"] and hub.roles["viewer"]:
                    print("[video] phone + cockpit both here -> starting WebRTC", flush=True)
                    await hub.send_to("phone", {"type": "start"})
                await hub.push_status()
            elif t in ("offer", "answer", "ice", "cam", "camcaps"):
                target = "viewer" if role == "phone" else "phone"
                if t in ("offer", "answer"):
                    ok = hub.roles.get(target) is not None
                    print(f"[video] {role} -> {target}: {t}" + ("" if ok else " (nobody there!)"),
                          flush=True)
                await hub.send_to(target, data)
            elif t == "restart" and role == "viewer":
                print("[video] cockpit asked for a fresh video connection", flush=True)
                await hub.send_to("phone", {"type": "start"})
            elif t == "diag":
                print(f"[{role or '?'}] {str(data.get('msg', ''))[:200]}", flush=True)
    finally:
        hub.clients.discard(ws)
        hub.addrs.pop(ws, None)
        if role and hub.roles.get(role) is ws:
            hub.roles[role] = None
            print(f"[{role}] disconnected", flush=True)
            await hub.push_status()
    return ws


# --- telemetry from g29_reader.py ---------------------------------------------------

class TelemetryProtocol(asyncio.DatagramProtocol):
    def datagram_received(self, data, addr):
        t = protocol.parse_telemetry(data)
        if t is not None:
            t["type"] = "telemetry"
            asyncio.create_task(hub.broadcast(t))
            return
        if data[:1] == b"{":                 # JSON status from the reader
            try:
                status = json.loads(data)
            except ValueError:
                return
            if status.get("type") == "car":
                changed = (hub.car or {}).get("state") != status.get("state") or \
                          (hub.car or {}).get("ip") != status.get("ip")
                hub.car, hub.car_seen = status, time.monotonic()
                if changed:
                    hub.refresh_phone_urls()
                    asyncio.create_task(hub.push_status())


# --- main ------------------------------------------------------------------------------

def _cert_ips():
    return [i.ip for i in netinfo.interfaces() if i.kind in ("lan", "vpn")]


async def _watch_network(ssl_ctx):
    """Every few seconds: new IP? re-issue the cert (live) and update the phone URL."""
    while True:
        await asyncio.sleep(5)
        cert, key, reissued = certs.ensure(_cert_ips())
        if reissued:
            ssl_ctx.load_cert_chain(cert, key)   # new handshakes use the new cert
            print("Network changed: certificate re-issued for the new address.", flush=True)
        if hub.refresh_phone_urls():
            await hub.push_status()


async def main():
    cert, key, reissued = certs.ensure(_cert_ips())
    if reissued:
        print("Issued a new HTTPS certificate (local CA in .rccar/certs).", flush=True)

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/phone", phone)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/ca.crt", ca_cert)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/qr.svg", qr_svg)

    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(cert, key)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "0.0.0.0", config.HTTPS_PORT, ssl_context=ssl_ctx).start()
    except OSError as e:
        print(f"ERROR: port {config.HTTPS_PORT} is already in use ({e.strerror}).\n"
              f"  Another copy of the server is probably still running in another window.\n"
              f"  launch.py normally cleans those up; otherwise find it with\n"
              f"    netstat -ano | findstr :{config.HTTPS_PORT}\n"
              f"  and stop that PID, or set RC_HTTPS_PORT to another port.", flush=True)
        await runner.cleanup()
        sys.exit(EXIT_PORT_IN_USE)

    loop = asyncio.get_running_loop()
    try:
        await loop.create_datagram_endpoint(
            TelemetryProtocol, local_addr=("127.0.0.1", config.TELE_UDP_PORT))
    except OSError as e:
        print(f"WARNING: can't listen for telemetry on UDP {config.TELE_UDP_PORT} ({e.strerror}); "
              f"the HUD will stay empty. Set RC_TELE_PORT to a free port.", flush=True)

    print("=" * 64, flush=True)
    print(f"  Cockpit (this laptop): https://localhost:{config.HTTPS_PORT}/", flush=True)
    hub.refresh_phone_urls()
    print("=" * 64, flush=True)

    await _watch_network(ssl_ctx)


def run():
    # launch.py stops us with CTRL_BREAK; treat it like Ctrl+C.
    if sys.platform == "win32":
        def _on_ctrl_break(signum, frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGBREAK, _on_ctrl_break)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    run()
