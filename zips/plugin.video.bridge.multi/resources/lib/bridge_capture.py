#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Web Stream Bridge - Capture Helper (Universal Engine)
Launches Chromium / Edge / Chrome with strict popup blocking,
pure-Python non-blocking CDP network interception, and ultra-low latency capture.
Works 100% in-process within Kodi's Python or as a standalone CLI script.
No external Python dependencies required.
"""

import sys
import os
import re
import json
import glob
import subprocess
import socket
import select
import shutil
import time
import base64
import struct
import urllib.request
import urllib.parse
import http.server
import http.cookiejar
import socketserver
import threading

CHROME_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
    r"C:\Program Files\Opera\launcher.exe",
    r"C:\Program Files (x86)\Opera\launcher.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Opera\launcher.exe"),
    r"C:\Program Files\Vivaldi\Application\vivaldi.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Vivaldi\Application\vivaldi.exe"),
]

KNOWN_AD_DOMAINS = [
    "*aliexpress*", "*adcash*", "*popunder*", "*propeller*", "*ay267*", "*cdclub*",
    "*doubleclick*", "*double?*", "*onclickperf*", "*roadster24*", "*official-app*",
    "*clickadu*", "*adsterra*", "*exoclick*", "*bet365*", "*1xbet*", "*casino*",
    "*betting*", "*track*", "*analytics*", "*adservices*", "*syndication*"
]

class SimpleWebSocket:
    """Ultra-fast RFC 6455 client implemented purely with standard library."""
    def __init__(self, ws_url):
        parts = ws_url.replace("ws://", "").split("/", 1)
        host, port_str = parts[0].split(":")
        self.host = host
        self.port = int(port_str)
        self.path = "/" + parts[1] if len(parts) > 1 else "/"
        self.sock = socket.create_connection((self.host, self.port), timeout=10)
        self._handshake()

    def _handshake(self):
        sec_key = base64.b64encode(os.urandom(16)).decode('utf-8')
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {sec_key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode('utf-8'))
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            resp += chunk
        if b" 101 " not in resp:
            raise RuntimeError(f"WebSocket handshake failed: {resp.decode('latin1', errors='ignore')}")

    def send_json(self, obj):
        payload = json.dumps(obj).encode('utf-8')
        length = len(payload)
        mask_key = os.urandom(4)
        header = bytearray([0x81])
        if length <= 125:
            header.append(0x80 | length)
        elif length <= 65535:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask_key)
        masked_payload = bytearray(length)
        for i in range(length):
            masked_payload[i] = payload[i] ^ mask_key[i % 4]
        try:
            self.sock.sendall(header + masked_payload)
        except Exception:
            pass

    def has_data(self):
        try:
            r, _, _ = select.select([self.sock], [], [], 0)
            return bool(r)
        except Exception:
            return False

    def recv_msg(self, timeout=0.0):
        if timeout > 0:
            try:
                r, _, _ = select.select([self.sock], [], [], timeout)
                if not r:
                    return None
            except Exception:
                return None
        elif not self.has_data():
            return None

        try:
            head = self.sock.recv(2)
            if not head or len(head) < 2:
                return None
            b1, b2 = head[0], head[1]
            opcode = b1 & 0x0F
            is_masked = (b2 & 0x80) != 0
            payload_len = b2 & 0x7F
            if payload_len == 126:
                ext = self.sock.recv(2)
                payload_len = struct.unpack("!H", ext)[0]
            elif payload_len == 127:
                ext = self.sock.recv(8)
                payload_len = struct.unpack("!Q", ext)[0]

            mask = self.sock.recv(4) if is_masked else None
            data = bytearray()
            while len(data) < payload_len:
                chunk = self.sock.recv(min(8192, payload_len - len(data)))
                if not chunk:
                    break
                data.extend(chunk)

            if is_masked and mask:
                for i in range(len(data)):
                    data[i] ^= mask[i % 4]

            if opcode == 0x01:
                return data.decode('utf-8', errors='ignore')
            elif opcode == 0x08:
                return None
            return None
        except (socket.timeout, socket.error):
            return None

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

def find_browser_from_registry():
    try:
        import winreg
        for hkey in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hkey, r"Software\Clients\StartMenuInternet") as key:
                    num_subkeys = winreg.QueryInfoKey(key)[0]
                    for i in range(num_subkeys):
                        sub = winreg.EnumKey(key, i)
                        try:
                            with winreg.OpenKey(key, sub + r"\shell\open\command") as cmd_key:
                                raw = winreg.QueryValue(cmd_key, "").strip().strip('"')
                                if raw and os.path.exists(raw):
                                    return raw
                        except Exception:
                            pass
            except Exception:
                pass
    except Exception:
        pass
    return None

def find_browser(custom_path=None):
    if custom_path and os.path.exists(custom_path):
        return custom_path

    reg_browser = find_browser_from_registry()
    if reg_browser:
        return reg_browser

    for path in CHROME_CANDIDATES:
        if os.path.exists(path):
            return path

    dynamic_patterns = [
        r"C:\Program Files (x86)\Microsoft\Edge*\**\msedge.exe",
        r"C:\Program Files\Microsoft\Edge*\**\msedge.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge*\**\msedge.exe"),
        r"C:\Program Files\Google\Chrome*\**\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome*\**\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome*\**\chrome.exe"),
        r"C:\Program Files\BraveSoftware\Brave-Browser*\**\brave.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser*\**\brave.exe"),
    ]
    for pattern in dynamic_patterns:
        try:
            matches = glob.glob(pattern, recursive=True)
            for m in matches:
                if os.path.isfile(m):
                    return m
        except Exception:
            pass

    for name in ["chrome.exe", "msedge.exe", "brave.exe", "chrome", "msedge"]:
        found = shutil.which(name)
        if found and os.path.exists(found):
            return found

    return None

def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]

def bring_browser_to_front(pid, is_app=False):
    if os.name != 'nt':
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        found_hwnds = []

        def enum_cb(hwnd, lparam):
            if user32.IsWindowVisible(hwnd):
                w_pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(w_pid))
                if w_pid.value == pid:
                    found_hwnds.append(hwnd)
            return True

        ENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
        user32.EnumWindows(ENUMPROC(enum_cb), 0)

        for hwnd in found_hwnds:
            if is_app:
                user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)
            user32.ShowWindow(hwnd, 9)
            user32.SetForegroundWindow(hwnd)
    except Exception:
        pass

def sync_cookies_from_main_browser(dest_profile):
    src_roots = [
        os.path.expandvars(r"%LOCALAPPDATA%\Chromium\User Data"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data"),
    ]
    for src_dir in src_roots:
        src_state = os.path.join(src_dir, "Local State")
        src_cookies = os.path.join(src_dir, "Default", "Network", "Cookies")
        if os.path.exists(src_state) and os.path.exists(src_cookies):
            try:
                dest_net = os.path.join(dest_profile, "Default", "Network")
                os.makedirs(dest_net, exist_ok=True)
                dest_state = os.path.join(dest_profile, "Local State")
                dest_cookies = os.path.join(dest_net, "Cookies")
                shutil.copy2(src_state, dest_state)
                shutil.copy2(dest_cookies, dest_cookies)
                break
            except Exception:
                pass

def is_stream_url(url):
    if not url or not isinstance(url, str):
        return False
    if url.startswith("data:") or url.startswith("blob:"):
        return False
    u = url.lower().split("?")[0].split("#")[0]
    if u.endswith(".ts") and "playlist" not in u:
        return False
    if u.endswith(".vtt") or u.endswith(".srt") or u.endswith(".jpg") or u.endswith(".png") or u.endswith(".js") or u.endswith(".css"):
        return False
    if any(ad in u for ad in ["google-analytics", "doubleclick", "recaptcha", "turnstile", "hcaptcha"]):
        return False

    if u.endswith(".mpd") or ".mpd" in u:
        return True
    if u.endswith(".m3u8") or ".m3u8" in u:
        return True
    if "/dash/" in u or "/hls/" in u:
        return True
    if not any(chunk in u for chunk in ["init-", "seg-", "chunk-", "frag-"]):
        if u.endswith(".mp4") or u.endswith(".webm") or u.endswith(".mkv"):
            return True
    return False

def is_android_platform():
    try:
        import xbmc
        if xbmc.getCondVisibility('System.Platform.Android'):
            return True
    except Exception:
        pass
    if 'android' in sys.platform.lower():
        return True
    if 'ANDROID_STORAGE' in os.environ or 'ANDROID_ROOT' in os.environ:
        return True
    return False

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def detect_packed(source):
    if not source or not isinstance(source, str):
        return False
    return "eval(function(p,a,c,k,e,d)" in source or "eval(function(p,a,c,k,e,r)" in source

def unpack_simple(source):
    try:
        match = re.search(r"}\s*\('(.*)',\s*(\d+),\s*(\d+),\s*'(.*)'\.split\('\|'\)", source, re.DOTALL)
        if not match:
            match = re.search(r"}\s*\('(.*)',\s*(\d+),\s*(\d+),\s*'(.*)'\s*\)", source, re.DOTALL)
        if not match:
            return source
        payload, radix, count, symtab = match.groups()
        radix = int(radix)
        count = int(count)
        syms = symtab.split('|') if '|' in symtab else symtab.split(',')
        
        def unbase(val_str, r):
            digits = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
            val = 0
            for c in val_str:
                val = val * r + digits.index(c)
            return val

        def replace_word(m):
            w = m.group(0)
            try:
                idx = unbase(w, radix)
                if idx < len(syms) and syms[idx]:
                    return syms[idx]
            except Exception:
                pass
            return w

        return re.sub(r'\b[0-9a-zA-Z]+\b', replace_word, payload)
    except Exception:
        return source

def extract_stream_from_html(html):
    if not html or not isinstance(html, str):
        return None
    # 1. Direct file matches
    m = re.search(r'file\s*:\s*["\'](https?://[^"\']+\.(?:m3u8|mpd|mp4)[^"\']*)["\']', html, re.I)
    if m and is_stream_url(m.group(1)):
        return m.group(1)
    m = re.search(r'sources\s*:\s*\[\s*\{\s*["\']?file["\']?\s*:\s*["\'](https?://[^"\']+)["\']', html, re.I)
    if m and is_stream_url(m.group(1)):
        return m.group(1)
    m = re.search(r'<source\s+[^>]*src=["\'](https?://[^"\']+)["\']', html, re.I)
    if m and is_stream_url(m.group(1)):
        return m.group(1)
    # 2. Packed JS
    if detect_packed(html):
        unpacked = unpack_simple(html)
        m = re.search(r'file\s*:\s*["\'](https?://[^"\']+)["\']', unpacked, re.I)
        if m and is_stream_url(m.group(1)):
            return m.group(1)
        m = re.search(r'(https?://[^\s"\'<>]+\.(?:m3u8|mpd)[^\s"\'<>]*)', unpacked, re.I)
        if m and is_stream_url(m.group(1)):
            return m.group(1)
    # 3. Generic stream URL fallback
    m = re.search(r'(https?://[^\s"\'<>]+\.(?:m3u8|mpd)[^\s"\'<>]*)', html, re.I)
    if m and is_stream_url(m.group(1)):
        return m.group(1)
    return None

class AndroidCaptchaHTTPHandler(http.server.BaseHTTPRequestHandler):
    target_info = {}
    captured_data = {}
    opener = None
    cookie_jar = None
    user_agent = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        clean_path = self.path.split("?")[0]
        if clean_path in ["/", "/captcha"]:
            sitekey = self.target_info.get("sitekey", "")
            title = self.target_info.get("title", "Multi Bridge")
            target_url = self.target_info.get("target_url", "")
            
            html = f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>{title} - Verificacion</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            background: #0d1117;
            color: #e6edf3;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }}
        .card {{
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 16px;
            box-shadow: 0 12px 36px rgba(0,0,0,0.6);
            max-width: 520px;
            width: 100%;
            padding: 32px 28px;
            text-align: center;
        }}
        h1 {{ font-size: 24px; font-weight: 700; margin-bottom: 8px; color: #ffffff; }}
        .subtitle {{ font-size: 15px; color: #8b949e; margin-bottom: 24px; }}
        .captcha-container {{
            display: flex;
            justify-content: center;
            margin: 20px 0;
            min-height: 78px;
        }}
        .status-box {{
            margin-top: 18px;
            padding: 14px;
            border-radius: 10px;
            background: #0d1117;
            border: 1px solid #21262d;
            font-size: 14px;
            color: #8b949e;
            transition: all 0.3s ease;
        }}
        .status-box.success {{
            background: #0f3521;
            border-color: #238636;
            color: #3fb950;
            font-weight: 600;
        }}
        .btn {{
            display: inline-block;
            margin-top: 16px;
            padding: 12px 24px;
            background: #238636;
            color: #ffffff;
            border-radius: 8px;
            text-decoration: none;
            font-size: 14px;
            font-weight: 600;
            transition: background 0.2s;
        }}
        .btn:hover {{ background: #2ea043; }}
        .btn-link {{
            background: transparent;
            color: #8b949e;
            text-decoration: underline;
            font-size: 12px;
            margin-top: 14px;
        }}
    </style>
    <script src="https://www.google.com/recaptcha/api.js" async defer></script>
    <script>
        function playChime() {{
            try {{
                var ctx = new (window.AudioContext || window.webkitAudioContext)();
                var now = ctx.currentTime;
                [523.25, 659.25, 783.99].forEach(function(freq, i) {{
                    var osc = ctx.createOscillator();
                    var gain = ctx.createGain();
                    osc.type = 'sine';
                    osc.frequency.setValueAtTime(freq, now + (i * 0.1));
                    gain.gain.setValueAtTime(0.18, now + (i * 0.1));
                    gain.gain.exponentialRampToValueAtTime(0.001, now + (i * 0.1) + 0.35);
                    osc.connect(gain);
                    gain.connect(ctx.destination);
                    osc.start(now + (i * 0.1));
                    osc.stop(now + (i * 0.1) + 0.4);
                }});
            }} catch(e) {{}}
        }}

        function returnToKodi() {{
            window.location.href = "intent:#Intent;package=org.xbmc.kodi;action=android.intent.action.MAIN;category=android.intent.category.LAUNCHER;end;";
        }}

        function onCaptchaSuccess(token) {{
            var statusEl = document.getElementById('status-box');
            statusEl.className = 'status-box';
            statusEl.innerHTML = 'Verificando con el servidor... Obteniendo enlace...';
            
            fetch('/token', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ token: token }})
            }})
            .then(function(res) {{ return res.json(); }})
            .then(function(data) {{
                if (data.status === 'ok') {{
                    playChime();
                    statusEl.className = 'status-box success';
                    statusEl.innerHTML = 'STREAM OBTENIDO CON EXITO<br>Iniciando reproduccion en Kodi...';
                    var btnKodi = document.getElementById('btn-kodi');
                    if (btnKodi) btnKodi.style.display = 'inline-block';
                    setTimeout(returnToKodi, 700);
                }} else {{
                    statusEl.innerHTML = 'Error: ' + (data.message || 'No se pudo obtener el stream');
                }}
            }})
            .catch(function(err) {{
                statusEl.innerHTML = 'Error de conexion con el puente de Kodi: ' + err;
            }});
        }}
    </script>
</head>
<body>
    <div class="card">
        <h1>Multi Bridge</h1>
        <div class="subtitle">Verificacion para {title}</div>
        <div class="captcha-container">
            <div class="g-recaptcha" data-sitekey="{sitekey}" data-callback="onCaptchaSuccess" data-theme="dark"></div>
        </div>
        <div id="status-box" class="status-box">
            Resuelve el captcha para continuar la reproduccion.
        </div>
        <a id="btn-kodi" class="btn" style="display: none;" href="javascript:returnToKodi()">Volver a Kodi</a>
        <div>
            <a class="btn btn-link" href="{target_url}" target="_blank">Abrir enlace original</a>
        </div>
    </div>
</body>
</html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
        elif clean_path == "/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(self.captured_data.get("result", {})).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length)
        clean_path = self.path.split("?")[0]
        
        if clean_path == "/token":
            try:
                body = json.loads(post_data.decode("utf-8"))
                token = body.get("token", "")
                
                form_action = self.target_info.get("form_action", "") or self.target_info.get("target_url", "")
                hidden_fields = self.target_info.get("hidden_fields", {}).copy()
                hidden_fields["g-recaptcha-response"] = token
                hidden_fields["token"] = token
                
                encoded = urllib.parse.urlencode(hidden_fields).encode("utf-8")
                req_headers = {
                    "User-Agent": self.user_agent,
                    "Referer": self.target_info.get("target_url", ""),
                    "Origin": urllib.parse.urlsplit(self.target_info.get("target_url", "")).scheme + "://" + urllib.parse.urlsplit(self.target_info.get("target_url", "")).netloc,
                    "Content-Type": "application/x-www-form-urlencoded"
                }
                
                req = urllib.request.Request(form_action, data=encoded, headers=req_headers)
                resp = self.opener.open(req, timeout=18)
                resp_content = resp.read().decode("utf-8", errors="ignore")
                
                stream_url = extract_stream_from_html(resp_content)
                if not stream_url:
                    m_if = re.search(r'<iframe\b[^>]*src=["\']([^"\']+)["\']', resp_content, re.I)
                    if m_if:
                        sub_url = urllib.parse.urljoin(form_action, m_if.group(1))
                        sub_req = urllib.request.Request(sub_url, headers=req_headers)
                        sub_resp = self.opener.open(sub_req, timeout=12)
                        sub_content = sub_resp.read().decode("utf-8", errors="ignore")
                        stream_url = extract_stream_from_html(sub_content)

                if stream_url:
                    if "/dash/" in stream_url.lower() and not stream_url.lower().endswith(".mpd"):
                        clean_base = stream_url.split("?")[0].rsplit("/", 1)[0]
                        query = ("?" + stream_url.split("?", 1)[1]) if "?" in stream_url else ""
                        stream_url = f"{clean_base}/manifest.mpd{query}"
                    
                    cookie_str = "; ".join([f"{c.name}={c.value}" for c in self.cookie_jar]) if self.cookie_jar else ""
                    self.captured_data["result"] = {
                        "status": "ok",
                        "url": stream_url,
                        "headers": {
                            "User-Agent": self.user_agent,
                            "Referer": self.target_info.get("target_url", ""),
                            "Cookie": cookie_str
                        },
                        "source": "android_captcha_bridge"
                    }
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "ok", "url": stream_url}).encode("utf-8"))
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "error", "message": "No se encontro stream de video en la respuesta."}).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode("utf-8"))
        elif clean_path == "/stream":
            try:
                body = json.loads(post_data.decode("utf-8"))
                if body.get("url"):
                    self.captured_data["result"] = {
                        "status": "ok",
                        "url": body["url"],
                        "headers": body.get("headers", {}),
                        "source": body.get("source", "android_web_fetch")
                    }
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "ok"}).encode("utf-8"))
            except Exception:
                self.send_response(500)
                self.end_headers()

def capture_stream_android(target_url, timeout=180, progress_dialog=None, monitor=None):
    user_agent = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    
    req_headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"
    }
    
    initial_html = ""
    resolved_target_url = target_url
    try:
        req = urllib.request.Request(target_url, headers=req_headers)
        resp = opener.open(req, timeout=12)
        initial_html = resp.read().decode("utf-8", errors="ignore")
        resolved_target_url = resp.geturl() or target_url
    except Exception:
        pass
    
    direct_stream = extract_stream_from_html(initial_html)
    if direct_stream:
        return {
            "status": "ok",
            "url": direct_stream,
            "headers": {"User-Agent": user_agent, "Referer": target_url},
            "source": "android_direct_scrape"
        }
    
    m_key = re.search(r'data-sitekey=["\']([^"\']+)', initial_html, re.I) or re.search(r'["\']sitekey["\']\s*:\s*["\']([^"\']+)', initial_html, re.I)
    sitekey = m_key.group(1) if m_key else ""
    
    m_action = re.search(r'<form\b[^>]*action=["\']([^"\']*)["\']', initial_html, re.I)
    form_action = m_action.group(1) if m_action else resolved_target_url
    form_action = urllib.parse.urljoin(resolved_target_url, form_action)
    
    hidden_fields = {}
    for m in re.finditer(r'<input[^>]+name=["\']([^"\']+)["\'][^>]+value=["\']([^"\']*)["\']', initial_html, re.I):
        hidden_fields[m.group(1)] = m.group(2)
    for m in re.finditer(r'<input[^>]+value=["\']([^"\']*)["\'][^>]+name=["\']([^"\']+)["\']', initial_html, re.I):
        hidden_fields.setdefault(m.group(2), m.group(1))

    parsed_netloc = urllib.parse.urlsplit(resolved_target_url).netloc
    host_title = parsed_netloc.replace("www.", "").capitalize()

    captured_data = {"result": None}
    
    class LocalServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    AndroidCaptchaHTTPHandler.target_info = {
        "sitekey": sitekey,
        "title": host_title,
        "target_url": resolved_target_url,
        "form_action": form_action,
        "hidden_fields": hidden_fields
    }
    AndroidCaptchaHTTPHandler.captured_data = captured_data
    AndroidCaptchaHTTPHandler.opener = opener
    AndroidCaptchaHTTPHandler.cookie_jar = cj
    AndroidCaptchaHTTPHandler.user_agent = user_agent

    try:
        httpd = LocalServer(("127.0.0.1", 0), AndroidCaptchaHTTPHandler)
    except Exception:
        httpd = LocalServer(("0.0.0.0", 0), AndroidCaptchaHTTPHandler)
    
    port = httpd.server_address[1]
    AndroidCaptchaHTTPHandler.port = port
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    local_url = f"http://127.0.0.1:{port}/captcha"
    local_ip = get_local_ip()
    network_url = f"http://{local_ip}:{port}/captcha"

    try:
        import xbmc
        xbmc.executebuiltin(f'StartAndroidActivity(,android.intent.action.VIEW,,{local_url})')
    except Exception:
        try:
            import webbrowser
            webbrowser.open(local_url)
        except Exception:
            pass

    try:
        import xbmcgui
        xbmcgui.Dialog().notification(
            f"Multi Bridge - {host_title}",
            "Resuelve el captcha en el navegador...",
            "",
            5000
        )
    except Exception:
        pass

    start_time = time.time()
    try:
        while True:
            if captured_data.get("result"):
                break
            
            if time.time() - start_time > timeout:
                return {"status": "error", "message": "Tiempo de espera agotado sin resolver el captcha."}
            
            if progress_dialog:
                try:
                    if progress_dialog.iscanceled():
                        return {"status": "error", "message": "Cancelado por el usuario."}
                    elapsed = time.time() - start_time
                    pct = min(99, int((elapsed / float(timeout)) * 100))
                    rem = max(0, int(timeout - elapsed))
                    mins = rem // 60
                    secs = rem % 60
                    rem_str = f"{mins}m {secs:02d}s" if mins else f"{secs}s"
                    progress_dialog.update(
                        pct,
                        f"Resuelve el captcha en el navegador de la TV...\n(O desde tu movil: {network_url})\nTiempo restante: {rem_str}"
                    )
                except Exception:
                    pass

            if monitor:
                try:
                    if monitor.abortRequested():
                        return {"status": "error", "message": "Kodi se esta cerrando."}
                except Exception:
                    pass

            time.sleep(0.2)
    finally:
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass

    return captured_data.get("result") or {"status": "error", "message": "No se capturo el stream de video."}

def capture_stream(target_url, timeout=180, custom_browser=None, app_mode=True, load_adblock=True, progress_dialog=None, monitor=None):
    if is_android_platform():
        return capture_stream_android(
            target_url=target_url,
            timeout=timeout,
            progress_dialog=progress_dialog,
            monitor=monitor
        )
    browser_path = find_browser(custom_browser)
    if not browser_path:
        return {"status": "error", "message": "No se encontró ningún navegador Chromium (Chrome o Edge) en el sistema."}

    user_data_dir = os.path.expandvars(r"%APPDATA%\Kodi\userdata\addon_data\plugin.video.bridge.multi\web_profile")
    os.makedirs(user_data_dir, exist_ok=True)
    sync_cookies_from_main_browser(user_data_dir)

    cdp_port = find_free_port()

    width = 900
    height = 680
    pos_x = 0
    pos_y = 0
    try:
        if os.name == 'nt':
            import ctypes
            sw = ctypes.windll.user32.GetSystemMetrics(0)
            sh = ctypes.windll.user32.GetSystemMetrics(1)
            pos_x = max(0, (sw - width) // 2)
            pos_y = max(0, (sh - height) // 2)
    except Exception:
        pass

    args = [
        browser_path,
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-fre",
        "--disable-signin-promo",
        "--disable-default-apps",
        f"--remote-debugging-port={cdp_port}",
        f"--window-size={width},{height}",
        f"--window-position={pos_x},{pos_y}",
        "--new-window",
        target_url
    ]

    creationflags = 0x08000000 if os.name == 'nt' else 0
    clean_env = {k: v for k, v in os.environ.items() if 'python' not in k.lower()}

    proc = None
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=clean_env,
            creationflags=creationflags
        )
    except Exception as pe:
        return {"status": "error", "message": f"Error al ejecutar navegador: {pe}"}

    def _bring_delayed():
        for _ in range(8):
            time.sleep(0.35)
            if proc.poll() is not None:
                break
            bring_browser_to_front(proc.pid, is_app=app_mode)

    threading.Thread(target=_bring_delayed, daemon=True).start()

    captured_data = None
    b_ws = None
    p_ws = None
    main_page_id = None

    # Ultra-fast connection to DevTools port (50ms interval)
    ws_ready = False
    for _ in range(60):
        if proc.poll() is not None:
            break
        try:
            v_url = f"http://127.0.0.1:{cdp_port}/json/version"
            resp = urllib.request.urlopen(v_url, timeout=0.8).read()
            v_info = json.loads(resp.decode('utf-8'))
            browser_ws_url = v_info.get("webSocketDebuggerUrl")
            if browser_ws_url:
                b_ws = SimpleWebSocket(browser_ws_url)
                b_ws.send_json({"id": 1, "method": "Target.setDiscoverTargets", "params": {"discover": True}})
                b_ws.send_json({"id": 2, "method": "Target.setAutoAttach", "params": {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True}})
                ws_ready = True
                break
        except Exception:
            time.sleep(0.04)

    # Connect to the main page target
    if ws_ready:
        for _ in range(30):
            try:
                t_url = f"http://127.0.0.1:{cdp_port}/json"
                targets = json.loads(urllib.request.urlopen(t_url, timeout=0.8).read().decode('utf-8'))
                main_page = next((t for t in targets if t.get('type') == 'page'), None)
                if main_page and main_page.get("webSocketDebuggerUrl"):
                    main_page_id = main_page.get("id")
                    p_ws = SimpleWebSocket(main_page["webSocketDebuggerUrl"])
                    p_ws.send_json({"id": 10, "method": "Network.enable"})
                    p_ws.send_json({"id": 11, "method": "Page.enable"})
                    p_ws.send_json({"id": 12, "method": "Runtime.enable"})
                    p_ws.send_json({
                        "id": 13,
                        "method": "Network.setBlockedURLs",
                        "params": {"urls": KNOWN_AD_DOMAINS}
                    })
                    # Strict popup blocker and token synchronizer injected on every document
                    injected_script = """
                    window.open = function(url) {
                        console.log('[MultiBridge] Neutralized popup window.open:', url);
                        return null;
                    };
                    window.showModalDialog = function() { return null; };
                    try {
                        Object.defineProperty(window, 'puOverlay', {
                            get: function() { return function() { return false; }; },
                            set: function() {},
                            configurable: false
                        });
                    } catch(e) {}

                    // Neutralize popups and ad redirects on links and forms
                    document.addEventListener('click', function(e) {
                        var el = e.target;
                        while (el && el !== document.body) {
                            if (el.tagName === 'A') {
                                if (el.target === '_blank' || el.target === '_new') {
                                    el.target = '_self';
                                }
                                var href = (el.href || '').toLowerCase();
                                if (href && (href.startsWith('http://') || href.startsWith('https://'))) {
                                    if (!href.includes(location.hostname)) {
                                        if (href.match(/(aliexpress|click|double\\?|adcash|propeller|popunder|ay267|cdclub|roadster|official-app|onclickperf|bet\\d+|casino)/i)) {
                                            console.log('[MultiBridge] Blocked ad click:', href);
                                            e.preventDefault();
                                            e.stopPropagation();
                                            return false;
                                        }
                                    }
                                }
                            }
                            el = el.parentElement;
                        }
                    }, true);

                    document.addEventListener('submit', function(e) {
                        if (e.target && e.target.tagName === 'FORM') {
                            e.target.target = '_self';
                        }
                    }, true);

                    // Synchronize captcha response tokens (reCAPTCHA / Powvideo / Streamplay)
                    function syncTokens() {
                        try {
                            var ta = document.querySelector('textarea[name="g-recaptcha-response"]');
                            var inpResp = document.querySelector('input[name="g-recaptcha-response"]');
                            var inpToken = document.querySelector('input[name="token"]');
                            var val = (ta && ta.value) ? ta.value : (inpResp ? inpResp.value : '');
                            if (val && val.length > 20) {
                                if (inpResp && inpResp.value !== val) inpResp.value = val;
                                if (inpToken && inpToken.value !== val) inpToken.value = val;
                            }
                        } catch(e) {}
                    }
                    setInterval(syncTokens, 300);
                    document.addEventListener('submit', syncTokens, true);

                    // Hook HTMLMediaElement for direct video src / play interception
                    try {
                        var origPlay = HTMLMediaElement.prototype.play;
                        HTMLMediaElement.prototype.play = function() {
                            var s = this.currentSrc || this.src;
                            if (s && (s.startsWith('http://') || s.startsWith('https://'))) {
                                console.log('[MULTI_BRIDGE_STREAM:' + s + ']');
                            }
                            return origPlay.apply(this, arguments);
                        };

                        var srcDesc = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'src');
                        if (srcDesc && srcDesc.set) {
                            var origSrcSet = srcDesc.set;
                            Object.defineProperty(HTMLMediaElement.prototype, 'src', {
                                set: function(val) {
                                    if (val && typeof val === 'string' && (val.startsWith('http://') || val.startsWith('https://'))) {
                                        console.log('[MULTI_BRIDGE_STREAM:' + val + ']');
                                    }
                                    return origSrcSet.apply(this, arguments);
                                },
                                get: srcDesc.get,
                                configurable: true
                            });
                        }
                    } catch(e) {}
                    """
                    p_ws.send_json({
                        "id": 14,
                        "method": "Page.addScriptToEvaluateOnNewDocument",
                        "params": {"source": injected_script}
                    })
                    break
            except Exception:
                time.sleep(0.04)

    start_time = time.time()
    captured_headers = {}

    try:
        while True:
            if proc.poll() is not None:
                return {"status": "error", "message": "Navegador cerrado antes de completar la verificación."}

            if captured_data and captured_data.get("status") == "ok":
                break

            if time.time() - start_time > timeout:
                return {"status": "error", "message": "Tiempo de espera agotado sin detectar stream de vídeo."}

            if progress_dialog:
                try:
                    if progress_dialog.iscanceled():
                        return {"status": "error", "message": "Cancelado por el usuario."}
                    elapsed = time.time() - start_time
                    pct = min(99, int((elapsed / float(timeout)) * 100))
                    rem = max(0, int(timeout - elapsed))
                    mins = rem // 60
                    secs = rem % 60
                    rem_str = f"{mins}m {secs:02d}s" if mins else f"{secs}s"
                    progress_dialog.update(
                        pct,
                        f'Resuelve el captcha en la ventana del navegador...\nTiempo restante: {rem_str}\nEl vídeo se reproducirá automáticamente al finalizar.'
                    )
                except Exception:
                    pass

            if monitor:
                try:
                    if monitor.abortRequested():
                        return {"status": "error", "message": "Kodi se está cerrando."}
                except Exception:
                    pass

            # 1. Non-blocking check of Browser WS events (Strict Popup Killer)
            if b_ws and b_ws.has_data():
                while b_ws.has_data():
                    b_msg = b_ws.recv_msg(timeout=0)
                    if not b_msg:
                        break
                    try:
                        d = json.loads(b_msg)
                        method = d.get("method", "")
                        if "Target.targetCreated" in method or "Target.attachedToTarget" in method:
                            t_info = d.get("params", {}).get("targetInfo", {})
                            t_id = t_info.get("targetId")
                            t_type = t_info.get("type", "")
                            if (t_type == "page" or "page" in str(t_type)) and t_id != main_page_id:
                                b_ws.send_json({"id": 99, "method": "Target.closeTarget", "params": {"targetId": t_id}})
                    except Exception:
                        pass

            # 2. Non-blocking drain of Page WS events (Network & Media Stream Sniffer)
            if p_ws and p_ws.has_data():
                while p_ws.has_data():
                    p_msg = p_ws.recv_msg(timeout=0)
                    if not p_msg:
                        break
                    try:
                        d = json.loads(p_msg)
                        method = d.get("method", "")
                        if "Network.requestWillBeSent" in method:
                            req = d.get("params", {}).get("request", {})
                            u = req.get("url", "")
                            headers = req.get("headers", {})
                            for k, v in headers.items():
                                captured_headers[k] = v
                            if is_stream_url(u):
                                captured_data = {
                                    "status": "ok",
                                    "url": u,
                                    "headers": captured_headers,
                                    "source": "cdp_request"
                                }
                                break
                        elif "Network.responseReceived" in method:
                            res = d.get("params", {}).get("response", {})
                            mime = (res.get("mimeType") or "").lower()
                            u = res.get("url", "")
                            res_headers = res.get("headers", {})
                            for k, v in res_headers.items():
                                captured_headers[k] = v
                            if ("video" in mime or "mpegurl" in mime or "dash" in mime) and is_stream_url(u):
                                captured_data = {
                                    "status": "ok",
                                    "url": u,
                                    "headers": captured_headers,
                                    "mime_type": mime,
                                    "source": "cdp_response"
                                }
                                break
                        elif "Runtime.consoleAPICalled" in method:
                            args = d.get("params", {}).get("args", [])
                            for a in args:
                                val = str(a.get("value", ""))
                                if "[MULTI_BRIDGE_STREAM:" in val:
                                    s_url = val.split("[MULTI_BRIDGE_STREAM:")[1].split("]")[0].strip()
                                    if is_stream_url(s_url):
                                        captured_data = {
                                            "status": "ok",
                                            "url": s_url,
                                            "headers": captured_headers,
                                            "source": "cdp_console"
                                        }
                                        break
                            if captured_data:
                                break
                    except Exception:
                        pass
                if captured_data:
                    break

            time.sleep(0.015)

    finally:
        if p_ws:
            try: p_ws.close()
            except Exception: pass
        if b_ws:
            try: b_ws.close()
            except Exception: pass
        try:
            proc.kill()
        except Exception:
            pass

    if captured_data and captured_data.get("status") == "ok":
        return captured_data

    return {"status": "error", "message": "No se detectó enlace de reproducción."}

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Web Stream Bridge Capturer")
    parser.add_argument("url", help="URL objetivo a abrir y capturar")
    parser.add_argument("--timeout", type=int, default=180, help="Tiempo maximo de espera en segundos")
    parser.add_argument("--browser", default=None, help="Ruta personalizada al ejecutable del navegador")
    parser.add_argument("--app", action="store_true", help="Abrir en modo ventana de aplicacion")
    parser.add_argument("--no-adblock", action="store_true", help="No intentar bloquear publicidad")

    args = parser.parse_args()

    result = capture_stream(
        target_url=args.url,
        timeout=args.timeout,
        custom_browser=args.browser,
        app_mode=args.app,
        load_adblock=not args.no_adblock
    )

    print(json.dumps(result))
    if result.get("status") == "ok":
        sys.exit(0)
    else:
        sys.exit(1)

if __name__ == "__main__":
    main()
