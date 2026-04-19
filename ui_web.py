"""
J.A.R.V.I.S — Desktop UI (pywebview + aiohttp)

Uses pywebview's evaluate_js() for reliable state push (no WebSocket).
Uses pywebview js_api bridge for actions (settings, autostart).
Falls back to WS+HTTP in browser mode.
"""
import os
import sys
import subprocess
import shutil

class _DummyWriter:
    def write(self, *args, **kwargs): pass
    def flush(self): pass
    def reconfigure(self, *args, **kwargs): pass

try:
    print("", end="")
    _stdout_encoding = getattr(sys.stdout, 'encoding', None)
    if _stdout_encoding and hasattr(sys.stdout, 'reconfigure') and _stdout_encoding.lower() != 'utf-8':
        sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    sys.stdout = _DummyWriter()

try:
    print("", file=sys.stderr, end="")
except Exception:
    sys.stderr = _DummyWriter()

import json
import time
import threading
from pathlib import Path
from collections import deque

import asyncio
from aiohttp import web

# ── Paths ──
def _get_bundle_dir():
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent

def _get_user_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BUNDLE_DIR = _get_bundle_dir()
USER_DIR   = _get_user_dir()
BASE_DIR   = Path(__file__).resolve().parent

CONFIG_DIR = USER_DIR / "config"
API_FILE   = CONFIG_DIR / "api_keys.json"
STATIC_DIR = BUNDLE_DIR / "static"

PORT = 5050

try:
    import winreg
    _HAS_WINREG = True
except ImportError:
    _HAS_WINREG = False


# ═══════════════════════════════════════════════════
#  JS API — exposed to pywebview as window.pywebview.api
# ═══════════════════════════════════════════════════
class _JarvisApi:
    def __init__(self, ui):
        self._ui = ui

    def get_settings(self):
        key = ""
        if API_FILE.exists():
            try:
                with open(API_FILE, "r", encoding="utf-8") as f:
                    key = json.load(f).get("gemini_api_key", "")
            except Exception:
                pass
        return {
            "api_key":   key,
            "autostart": self._ui._get_autostart(),
        }

    def save_api_key(self, key):
        key = (key or "").strip()
        if not key:
            return {"success": False, "error": "Empty"}
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(API_FILE, "w", encoding="utf-8") as f:
                json.dump({"gemini_api_key": key}, f, indent=4)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def start_session(self, payload):
        lang = payload.get("language", "English").strip()
        key  = payload.get("api_key")
        
        self._ui.spoken_language = lang
        
        if key:
            key = key.strip()
            try:
                os.makedirs(CONFIG_DIR, exist_ok=True)
                with open(API_FILE, "w", encoding="utf-8") as f:
                    json.dump({"gemini_api_key": key}, f, indent=4)
                self._ui._api_key_ready = True
            except Exception:
                return False
                
        self._ui._language_ready = True
        self._ui.status_text = "ONLINE"
        self._ui.write_log(f"SYS: Systems initialised. Configuration: {lang}.")
        return True

    def sleep_mode(self):
        """Called from JS — puts JARVIS to sleep."""
        self._ui.enter_sleep()
        return True

    def toggle_autostart(self):
        cur = self._ui._get_autostart()
        enable = not cur
        
        if getattr(sys, "frozen", False):
            ok = self._ui._set_autostart(enable)
            return {"enabled": enable if ok else cur}
        
        self._clean_artifacts()
        
        if enable:
            threading.Thread(target=self._build_and_enable, daemon=True).start()
            return {"status": "building"}
        
        ok  = self._ui._set_autostart(enable)
        return {"enabled": enable if ok else cur}

    def _clean_artifacts(self):
        self._ui.write_log("SYS: Cleaning up previous build artifacts...")
        for name in ["build", "dist"]:
            p = BASE_DIR / name
            if p.exists() and p.is_dir():
                try: shutil.rmtree(p)
                except Exception: pass
        spec = BASE_DIR / "JARVIS.spec"
        if spec.exists():
            try: spec.unlink()
            except Exception: pass
        exe_name = "JARVIS.exe" if "win" in sys.platform else "JARVIS"
        mac_linux_exe = BASE_DIR / exe_name
        if mac_linux_exe.exists() and not mac_linux_exe.is_dir():
            try: mac_linux_exe.unlink()
            except Exception: pass

    def _build_and_enable(self):
        self._ui.is_building = True
        self._ui.write_log("SYS: Building native application (this will take a minute)...")
        try:
            cmd = [sys.executable, "build.py"]
            p = subprocess.Popen(
                cmd,
                cwd=str(BASE_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )
            for line in p.stdout:
                line_stripped = line.strip()
                if line_stripped:
                    self._ui.write_log(line_stripped)
            p.wait()
            if p.returncode == 0:
                self._ui.write_log("SYS: Build completed. Setting auto-run for native binary...")
                ok = self._ui._set_autostart(True)
                self._ui._eval_js(f"setAutoBtn({'true' if ok else 'false'})")
                if self._ui._window is None:
                    self._ui._broadcast({"type": "autostart_result", "enabled": ok})
            else:
                self._ui.write_log(f"SYS: Build failed with exit code {p.returncode}")
        except Exception as e:
            self._ui.write_log(f"SYS: Build failed: {e}")
        finally:
            self._ui.is_building = False
            try:
                cur = self._ui._get_autostart()
                self._ui._eval_js(f"setAutoBtn({'true' if cur else 'false'})")
            except Exception: pass
            if self._ui._window is None:
                self._ui._broadcast({"type": "autostart_result", "enabled": self._ui._get_autostart()})


# ═══════════════════════════════════════════════════
#  Main UI Class
# ═══════════════════════════════════════════════════
class JarvisUI:
    def __init__(self, face_path=None, size=None):
        self._speaking      = False
        self.mic_level      = 0.0
        self.jarvis_level   = 0.0
        self._conn_state    = "CONNECTING"
        self._status_text   = "INITIALISING"
        self.is_building    = False
        self.last_audio_played_time = 0.0

        self._log_queue: deque = deque(maxlen=200)
        self._api_key_ready = self._api_keys_exist()
        self._language_ready = False
        self.spoken_language = "English"
        self._log_counter   = 0
        self._window        = None
        self._window_ready  = False

        # Sleep / Wake
        self._sleep_event    = threading.Event()   # Set when sleeping
        self._woken_event    = None                # Set by main.py, signaled on wake

        # Mobile Device connection tracking
        self.mobile_connected = False
        self.mobile_locked = False
        self._mobile_ip = None
        self.mobile_mic_queue = None
        self.mobile_out_queue = None
        self._desktop_ws = None
        self._mobile_ws = None

        # aiohttp server (for serving static files + WS fallback)
        self._ws_clients: list = []
        self._loop       = None
        self._server_ready = threading.Event()
        threading.Thread(target=self._run_server, daemon=True).start()
        self._server_ready.wait(timeout=10)

    # ── Properties for automatic terminal logging ──
    @property
    def speaking(self):
        return self._speaking

    @speaking.setter
    def speaking(self, val):
        if val != self._speaking:
            self._speaking = val
            print(f"[STATE] {'🔊 Speaking ON' if val else '🔇 Speaking OFF'}")

    @property
    def conn_state(self):
        return self._conn_state

    @conn_state.setter
    def conn_state(self, val):
        if val != self._conn_state:
            self._conn_state = val
            icons = {"CONNECTING": "🔌", "ONLINE": "✅", "RECONNECTING": "🔄", "FAILED": "❌"}
            print(f"[STATE] {icons.get(val, '📡')} Connection → {val}")

    @property
    def status_text(self):
        return self._status_text

    @status_text.setter
    def status_text(self, val):
        if val != self._status_text:
            self._status_text = val
            # Don't log RETRY N or minor updates to reduce noise
            if val not in ("CONNECTING",):
                print(f"[STATE] 📊 Status → {val}")

    # ── Public API ──
    def write_log(self, text: str):
        tl = text.lower()
        tag = "user" if tl.startswith("you:") else \
              "ai"   if tl.startswith("jarvis:") or tl.startswith("ai:") else "sys"
        
        self._log_counter += 1
        entry_id = f"log_{self._log_counter}"
        entry = {"text": text, "tag": tag, "id": entry_id}
        self._log_queue.append(entry)

        # Console logging with tag formatting
        tag_icons = {"user": "🗣️  USER", "ai": "🤖 JARVIS", "sys": "⚙️  SYS"}
        print(f"[LOG] {tag_icons.get(tag, '📝 LOG')} │ {text}")

        # Push to JS directly (pywebview mode)
        safe_text = json.dumps(text)
        safe_tag  = json.dumps(tag)
        safe_id   = json.dumps(entry_id)
        self._eval_js(f"_onLog({safe_text},{safe_tag},{safe_id})")

        # Broadcast to all connected WebSockets (including mobile)
        self._broadcast({"type": "log", **entry})

        # Status is now driven exclusively by the audio pipeline
        # (speaking flag, _is_executing_tool, _play_audio timeout).
        # Do NOT set status_text here — it caused race conditions
        # where log-driven status overwrote pipeline-driven status.

    def start_speaking(self):
        self.speaking    = True
        self.status_text = "SPEAKING"
        print(f"[STATE] 🔊 Speaking → ON | Status → SPEAKING")

    def stop_speaking(self):
        self.speaking    = False
        self.status_text = "ONLINE"
        print(f"[STATE] 🔇 Speaking → OFF | Status → ONLINE")

    def wait_for_api_key(self):
        print(f"[STATE] 🔑 Waiting for session configuration...")
        while not (self._api_key_ready and self._language_ready):
            time.sleep(0.1)
        print(f"[STATE] ✅ Session configured")

    # ── Sleep / Wake ──
    def enter_sleep(self):
        """Put JARVIS to sleep — minimize window, signal main.py to disconnect."""
        self._sleep_event.set()
        self.status_text = "SLEEPING"
        self.conn_state  = "CONNECTING"  # Will show SLEEPING due to statusText priority
        self.speaking    = False
        self.mic_level   = 0.0
        self.jarvis_level = 0.0
        self.mobile_locked = False
        self.write_log("SYS: Entering sleep mode. Say 'wake up' to resume.")
        print("[STATE] 😴 Entering SLEEP mode")

        # Minimize window
        if self._window:
            try:
                self._window.minimize()
            except Exception as e:
                print(f"[UI] ⚠️ Minimize failed: {e}")

    def wake_up(self, manual_restore=False):
        """Wake JARVIS — restore window, signal main.py to reconnect."""
        if not self.is_sleeping:
            return  # Prevent double-waking

        self._sleep_event.clear()
        self.status_text = "CONNECTING"
        self.conn_state  = "CONNECTING"
        self.write_log("SYS: Wake word detected. Reconnecting...")
        print(f"[STATE] ☀️ WAKING UP (Manual Restore: {manual_restore})")

        # Restore window on a separate thread to avoid deadlocking pywebview
        if not manual_restore:
            def _do_restore():
                try:
                    if self._window:
                        self._window.restore()
                except Exception as e:
                    print(f"[UI] ⚠️ Restore failed: {e}")

            threading.Thread(target=_do_restore, daemon=True).start()

        # Signal the sleep gate in main.py to unblock
        if self._woken_event:
            self._woken_event.set()

    @property
    def is_sleeping(self) -> bool:
        return self._sleep_event.is_set()

    # ── evaluate_js wrapper ──
    def _eval_js(self, code):
        if self._window and self._window_ready:
            try:
                self._window.evaluate_js(code)
            except Exception:
                self._window_ready = False

    # ── State push loop (runs after pywebview window loads) ──
    def _push_state_loop(self):
        try:
            """Called by webview.start(func=...) in a separate thread."""
            time.sleep(1.5)  # Wait for page + JS to fully load
            self._window_ready = True

            # Always mandate the Setup screen on component load
            # It will dynamically hide the API key field if _api_key_ready is already True
            safe_has_key = str(self._api_key_ready).lower()
            self._eval_js(f"_onSetupRequired({{'has_key': {safe_has_key}}})")

            # Replay existing logs (only those queued before window was ready)
            for entry in list(self._log_queue):
                safe_text = json.dumps(entry["text"])
                safe_tag  = json.dumps(entry.get("tag", "sys"))
                safe_id   = json.dumps(entry.get("id", ""))
                self._eval_js(f"_onLog({safe_text},{safe_tag},{safe_id})")
                time.sleep(0.05)

            # Continuous state push
            while self._window_ready:
                state = json.dumps({
                    "speaking":     self.speaking,
                    "mic_level":    round(self.mic_level, 4),
                    "jarvis_level": round(self.jarvis_level, 4),
                    "conn_state":   self.conn_state,
                    "status_text":  self.status_text,
                    "is_building":  self.is_building,
                    "mobile_connected": self.mobile_connected,
                })
                self._eval_js(f"_onState({state})")
                time.sleep(1 / 30)
        except Exception as e:
            print(f"[FATAL] _push_state_loop crashed: {e}")

    # ── aiohttp (static files + WS fallback) ──
    def _run_server(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    def _generate_ssl_context(self):
        try:
            import ssl
            import datetime
            from cryptography import x509
            from cryptography.x509.oid import NameOID
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.hazmat.primitives import serialization

            cert_path = CONFIG_DIR / "cert.pem"
            key_path = CONFIG_DIR / "key.pem"

            if not cert_path.exists() or not key_path.exists():
                print("[UI] 🔐 Generating Ephemeral Self-Signed SSL Certificate for Mobile WebRTC...")
                key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
                subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, u"JARVIS Localhost")])
                cert = x509.CertificateBuilder().subject_name(
                    subject
                ).issuer_name(
                    issuer
                ).public_key(
                    key.public_key()
                ).serial_number(
                    x509.random_serial_number()
                ).not_valid_before(
                    datetime.datetime.utcnow() - datetime.timedelta(days=1)
                ).not_valid_after(
                    datetime.datetime.utcnow() + datetime.timedelta(days=365)
                ).add_extension(
                    x509.SubjectAlternativeName([x509.DNSName(u"localhost")]),
                    critical=False,
                ).sign(key, hashes.SHA256())

                os.makedirs(CONFIG_DIR, exist_ok=True)
                with open(key_path, "wb") as f:
                    f.write(key.private_bytes(
                        encoding=serialization.Encoding.PEM,
                        format=serialization.PrivateFormat.TraditionalOpenSSL,
                        encryption_algorithm=serialization.NoEncryption(),
                    ))
                with open(cert_path, "wb") as f:
                    f.write(cert.public_bytes(serialization.Encoding.PEM))

            ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ssl_context.load_cert_chain(str(cert_path), str(key_path))
            return ssl_context
        except Exception as e:
            print(f"[UI] ⚠️ Failed to generate SSL Certificate. HTTPS will be disabled. Error: {e}")
            return None

    @web.middleware
    async def _security_middleware(self, req, handler):
        port = req.url.port
        if port == PORT:
            if req.remote not in ("127.0.0.1", "::1"):
                return web.Response(status=403, text="403 Forbidden")
        elif port == PORT + 1:
            user_agent = req.headers.get("User-Agent", "").lower()
            is_mobile = any(x in user_agent for x in ["android", "iphone", "ipad", "ipod", "webos", "blackberry", "iemobile", "opera mini"])
            if not is_mobile:
                return web.Response(status=403, text="403 Forbidden: Mobile Slot Exclusive.")
            if req.path == "/" and getattr(self, 'mobile_connected', False):
                if req.remote != getattr(self, '_mobile_ip', None):
                    return web.Response(status=403, text="403 Forbidden: Slot Occupied.")
        return await handler(req)

    async def _serve(self):
        app = web.Application(middlewares=[self._security_middleware])
        app.router.add_get("/ws", self._ws_handler)
        app.router.add_get("/", self._index)
        app.router.add_static("/static", str(STATIC_DIR), show_index=False)

        self.mobile_mic_queue = asyncio.Queue()
        self.mobile_out_queue = asyncio.Queue()

        runner = web.AppRunner(app)
        await runner.setup()
        
        # 1. Desktop Interface binds strictly to Local HTTP
        await web.TCPSite(runner, "127.0.0.1", PORT).start()
        
        # 2. Mobile Interface attempts binding to HTTPS
        ssl_ctx = self._generate_ssl_context()
        MOBILE_PORT = PORT + 1
        try:
            if ssl_ctx:
                await web.TCPSite(runner, "0.0.0.0", MOBILE_PORT, ssl_context=ssl_ctx).start()
            else:
                await web.TCPSite(runner, "0.0.0.0", MOBILE_PORT).start() # fallback
        except Exception as e:
            print(f"[UI] ⚠️ Failed to bind Mobile Port {MOBILE_PORT}: {e}")
        
        local_ips = set()
        try:
            import socket
            # Method 1: Get all IPs assigned to the local host
            hostname = socket.gethostname()
            for info in socket.getaddrinfo(hostname, None):
                ip = info[4][0]
                if "." in ip and not ip.startswith("127.") and not ip.startswith("169.254."):
                    local_ips.add(ip)
            # Method 2: Default route
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ips.add(s.getsockname()[0])
            s.close()
        except Exception:
            pass

        # Prioritize standard home network prefixes over VPN/CGNAT
        lan_ips = [ip for ip in local_ips if ip.startswith(("192.168.", "10.", "172."))]
        best_ips = lan_ips if lan_ips else (list(local_ips) or ["127.0.0.1"])
        
        proto_str = "https" if ssl_ctx else "http"
        ip_display = " or ".join([f"{proto_str}://{ip}:{MOBILE_PORT}" for ip in best_ips])
            
        print(f"[UI] Desktop Core running on http://127.0.0.1:{PORT}")
        print(f"[UI] 📱 Mobile Assistant slot open! Connect your phone to: {ip_display}")
        self._server_ready.set()

        async def _mobile_audio_sender():
            while True:
                chunk = await self.mobile_out_queue.get()
                if self._mobile_ws is not None and not self._mobile_ws.closed:
                    try:
                        await self._mobile_ws.send_bytes(chunk)
                    except Exception:
                        pass

        self._loop.create_task(_mobile_audio_sender())

        # WS fallback state push (for browser mode)
        while True:
            if self._ws_clients:
                await self._broadcast_async({
                    "type":         "state",
                    "speaking":     self.speaking,
                    "mic_level":    round(self.mic_level, 4),
                    "jarvis_level": round(self.jarvis_level, 4),
                    "conn_state":   self.conn_state,
                    "status_text":  self.status_text,
                    "is_building":  self.is_building,
                    "mobile_connected": self.mobile_connected,
                })
            await asyncio.sleep(1 / 30)

    async def _index(self, req):
        return web.FileResponse(STATIC_DIR / "index.html")

    async def _ws_handler(self, req):
        device_type = req.query.get("device", "desktop")

        if device_type == "mobile":
            if self._mobile_ws is not None and req.remote != getattr(self, '_mobile_ip', None):
                return web.Response(status=409, text="Mobile slot already full.")
        else:
            if self._desktop_ws is not None:
                return web.Response(status=409, text="Desktop slot already full.")

        ws = web.WebSocketResponse()
        await ws.prepare(req)

        if device_type == "mobile":
            self._mobile_ws = ws
            self._mobile_ip = req.remote
            self.mobile_connected = True
            self.mobile_locked = True
            print("[UI] 📱 Mobile device connected and took control.")
            while not self.mobile_mic_queue.empty():
                try: self.mobile_mic_queue.get_nowait()
                except asyncio.QueueEmpty: break
            while not self.mobile_out_queue.empty():
                try: self.mobile_out_queue.get_nowait()
                except asyncio.QueueEmpty: break
        else:
            self._desktop_ws = ws
            self.mobile_locked = False
            print("[UI] 💻 Desktop UI connected.")

        self._ws_clients.append(ws)

        # Send current state + logs immediately
        await ws.send_json({
            "type": "state",
            "speaking": self.speaking,
            "mic_level": round(self.mic_level, 4),
            "jarvis_level": round(self.jarvis_level, 4),
            "conn_state": self.conn_state,
            "status_text": self.status_text,
            "is_building": self.is_building,
            "mobile_connected": self.mobile_connected,
        })
        if not (self._api_key_ready and self._language_ready):
            await ws.send_json({"type": "setup_required", "has_key": self._api_key_ready})
        else:
            await ws.send_json({"type": "setup_ok"})

        for entry in list(self._log_queue):
            await ws.send_json({"type": "log", **entry})

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        d = json.loads(msg.data)
                        await self._handle_ws(ws, d)
                    except Exception:
                        pass
                elif msg.type == web.WSMsgType.BINARY and device_type == "mobile":
                    self.mobile_mic_queue.put_nowait(msg.data)
        finally:
            try: self._ws_clients.remove(ws)
            except ValueError: pass

            if device_type == "mobile":
                self._mobile_ws = None
                self.mobile_connected = False
                print("[UI] 📱 Mobile device disconnected. Control returned to laptop.")
            else:
                self._desktop_ws = None
                print("[UI] 💻 Desktop UI disconnected.")

        return ws

    async def _handle_ws(self, ws, d):
        t = d.get("type", "")
        if t == "get_settings":
            key = ""
            if API_FILE.exists():
                try:
                    with open(API_FILE, "r", encoding="utf-8") as f:
                        key = json.load(f).get("gemini_api_key", "")
                except Exception:
                    pass
            await ws.send_json({"type": "settings", "api_key": key, "autostart": self._get_autostart()})
        elif t == "save_api_key":
            key = d.get("key", "").strip()
            if not key:
                await ws.send_json({"type": "save_result", "success": False, "error": "Empty"})
                return
            try:
                os.makedirs(CONFIG_DIR, exist_ok=True)
                with open(API_FILE, "w", encoding="utf-8") as f:
                    json.dump({"gemini_api_key": key}, f, indent=4)
                await ws.send_json({"type": "save_result", "success": True})
            except Exception as e:
                await ws.send_json({"type": "save_result", "success": False, "error": str(e)})
        elif t == "start_session":
            payload = d.get("payload", {})
            lang = payload.get("language", "English").strip()
            key  = payload.get("api_key")
            
            self.spoken_language = lang
            
            if key:
                key = key.strip()
                os.makedirs(CONFIG_DIR, exist_ok=True)
                with open(API_FILE, "w", encoding="utf-8") as f:
                    json.dump({"gemini_api_key": key}, f, indent=4)
                self._api_key_ready = True
                
            self._language_ready = True
            self._broadcast({"type": "setup_ok"})
            self.status_text = "ONLINE"
            self.write_log(f"SYS: Systems initialised. Configuration: {lang}.")
        elif t == "toggle_autostart":
            cur = self._get_autostart()
            ok  = self._set_autostart(not cur)
            await ws.send_json({"type": "autostart_result", "enabled": (not cur) if ok else cur})
        elif t == "sleep_mode":
            self.enter_sleep()

    async def _broadcast_async(self, data):
        dead = []
        for ws in list(self._ws_clients):
            try: await ws.send_json(data)
            except Exception: dead.append(ws)
        for ws in dead:
            try: self._ws_clients.remove(ws)
            except ValueError: pass

    def _broadcast(self, data):
        if self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._broadcast_async(data), self._loop)

    # ── API key ──
    @staticmethod
    def _api_keys_exist():
        return API_FILE.exists()

    # ── Auto-start ──
    def _get_autostart(self):
        if sys.platform == "darwin":
            return os.path.exists(os.path.expanduser("~/Library/LaunchAgents/com.jarvis.autorun.plist"))
        if sys.platform.startswith("linux"):
            return os.path.exists(os.path.expanduser("~/.config/autostart/jarvis.desktop"))
        if not _HAS_WINREG:
            return False
        try:
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
            try: winreg.QueryValueEx(k, "JARVIS"); winreg.CloseKey(k); return True
            except FileNotFoundError: winreg.CloseKey(k); return False
        except Exception: return False

    def _set_autostart(self, enable):
        if sys.platform == "darwin":
            plist = os.path.expanduser("~/Library/LaunchAgents/com.jarvis.autorun.plist")
            try:
                if enable:
                    exe_path = BASE_DIR / "dist" / "JARVIS"
                    exec_cmd = f"<string>{exe_path}</string>" if exe_path.exists() else f"<string>{sys.executable}</string>\n  <string>{BASE_DIR / 'main.py'}</string>"
                    content = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.jarvis.autorun</string>
<key>ProgramArguments</key><array>
  {exec_cmd}
</array>
<key>WorkingDirectory</key><string>{BASE_DIR}</string>
<key>RunAtLoad</key><true/>
</dict></plist>'''
                    with open(plist, "w") as f: f.write(content)
                else:
                    if os.path.exists(plist): os.remove(plist)
                return True
            except Exception: return False

        if sys.platform.startswith("linux"):
            desktop_file = Path.home() / ".config" / "autostart" / "jarvis.desktop"
            try:
                if enable:
                    exe_path = BASE_DIR / "dist" / "JARVIS"
                    exec_cmd = f"Exec={exe_path}" if exe_path.exists() else f"Exec={sys.executable} {BASE_DIR / 'main.py'}"
                    os.makedirs(desktop_file.parent, exist_ok=True)
                    content = f"""[Desktop Entry]
Type=Application
{exec_cmd}
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
Name=J.A.R.V.I.S
Comment=Jarvis AI Assistant
"""
                    desktop_file.write_text(content)
                else:
                    if desktop_file.exists():
                        desktop_file.unlink()
                return True
            except Exception: return False

        if not _HAS_WINREG:
            return False
        try:
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
            if enable:
                exe = BASE_DIR / "dist" / "JARVIS.exe"
                cmd = f'"{exe}"' if exe.exists() else \
                      f'"{os.path.abspath(sys.executable)}" "{BASE_DIR / "main.py"}"'
                winreg.SetValueEx(k, "JARVIS", 0, winreg.REG_SZ, cmd)
            else:
                try: winreg.DeleteValue(k, "JARVIS")
                except FileNotFoundError: pass
            winreg.CloseKey(k)
            return True
        except Exception: return False

    # ── Main loop ──
    def mainloop(self):
        try:
            import webview
        except ImportError:
            print("[UI] pywebview not installed - opening in browser")
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{PORT}")
            try:
                while True: time.sleep(1)
            except KeyboardInterrupt:
                os._exit(0)
            return

        api = _JarvisApi(self)
        self._window = webview.create_window(
            "J.A.R.V.I.S",
            f"http://127.0.0.1:{PORT}",
            width=960, height=720,
            resizable=True,
            min_size=(550, 450),
            background_color="#06060f",
            text_select=False,
            js_api=api,
        )
        
        def _on_closed():
            self._window_ready = False
            try:
                sys.stderr = open(os.devnull, 'w')
                sys.stdout = open(os.devnull, 'w')
                threading.Timer(0.5, lambda: os._exit(0)).start()
            except Exception: pass
            
        def _on_restored():
            if self.is_sleeping:
                self.wake_up(manual_restore=True)

        self._window.events.closed += _on_closed
        self._window.events.restored += _on_restored
        self._window.events.maximized += _on_restored
        self._window.events.shown += _on_restored
        
        webview.start(func=self._push_state_loop, debug=False)
        os._exit(0)
