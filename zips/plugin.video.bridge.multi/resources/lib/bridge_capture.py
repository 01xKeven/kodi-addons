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
                shutil.copy2(src_cookies, dest_cookies)
                break
            except Exception:
                pass

def _cleanup_profile_dir(path, retries=4, delay=0.1):
    """Elimina de forma segura perfiles temporales del navegador capturador."""
    if not path or not os.path.exists(path):
        return
    for _ in range(max(1, int(retries))):
        try:
            shutil.rmtree(path, ignore_errors=False)
        except Exception:
            pass
        if not os.path.exists(path):
            return
        time.sleep(delay)
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def fix_chrome_crash_state(dest_profile):
    if not dest_profile:
        return
    pref_path = os.path.join(dest_profile, "Default", "Preferences")
    if os.path.isfile(pref_path):
        try:
            with open(pref_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            prof = d.setdefault("profile", {})
            prof["exit_type"] = "Normal"
            prof["exited_cleanly"] = True
            with open(pref_path, "w", encoding="utf-8") as f:
                json.dump(d, f)
        except Exception:
            pass

    state_path = os.path.join(dest_profile, "Local State")
    if os.path.isfile(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                sd = json.load(f)
            metrics = sd.setdefault("user_experience_metrics", {})
            stability = metrics.setdefault("stability", {})
            stability["exited_cleanly"] = True
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(sd, f)
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

def get_kodi_package():
    try:
        import re
        p = ""
        try:
            import xbmcvfs
            p = xbmcvfs.translatePath("special://home")
        except Exception:
            try:
                import xbmc
                p = xbmc.translatePath("special://home")
            except Exception:
                pass
        if p:
            m = re.search(r'Android/data/([^/]+)', p)
            if m:
                return m.group(1)
        try:
            import xbmc
            p2 = xbmc.translatePath("special://xbmc")
            m2 = re.search(r'Android/data/([^/]+)', p2)
            if m2:
                return m2.group(1)
        except Exception:
            pass
    except Exception:
        pass
    return "org.xbmc.kodi"

_last_bring_to_front = 0.0
_active_android_server = None

def bring_kodi_to_front():
    global _last_bring_to_front
    now = time.time()
    if now - _last_bring_to_front < 3.0:
        return
    _last_bring_to_front = now
    try:
        import xbmc
        pkg = get_kodi_package()
        xbmc.executebuiltin(f'StartAndroidActivity({pkg})')
    except Exception:
        pass

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

def normalize_stream_url(url):
    if not url or not isinstance(url, str):
        return url
    url = url.strip()
    if "pkcdn.org/dash/" in url.lower():
        m_dash = re.search(r'(https?://[^/]+)/dash/([^/?#]+)(?:/[^/?#]+)?', url, re.I)
        if m_dash:
            base_host = m_dash.group(1)
            video_hash = m_dash.group(2)
            q = ("?" + url.split("?", 1)[1]) if "?" in url else ""
            return f"{base_host}/{video_hash}/v.mp4{q}"
    elif "/dash/" in url.lower() and not url.lower().endswith(".mpd"):
        clean_base = url.split("?")[0].rsplit("/", 1)[0]
        q = ("?" + url.split("?", 1)[1]) if "?" in url else ""
        return f"{clean_base}/manifest.mpd{q}"
    return url

def extract_stream_from_html(html):
    if not html or not isinstance(html, str):
        return None
    candidates = []
    # 1. Direct file matches
    for m in re.finditer(r'file\s*:\s*["\'](https?://[^"\']+\.(?:m3u8|mpd|mp4)[^"\']*)["\']', html, re.I):
        if is_stream_url(m.group(1)):
            candidates.append(normalize_stream_url(m.group(1)))
    for m in re.finditer(r'sources\s*:\s*\[\s*\{\s*["\']?file["\']?\s*:\s*["\'](https?://[^"\']+)["\']', html, re.I):
        if is_stream_url(m.group(1)):
            candidates.append(normalize_stream_url(m.group(1)))
    for m in re.finditer(r'<source\s+[^>]*src=["\'](https?://[^"\']+)["\']', html, re.I):
        if is_stream_url(m.group(1)):
            candidates.append(normalize_stream_url(m.group(1)))
    # 2. Packed JS
    if detect_packed(html):
        unpacked = unpack_simple(html)
        if unpacked:
            for m in re.finditer(r'file\s*:\s*["\'](https?://[^"\']+)["\']', unpacked, re.I):
                if is_stream_url(m.group(1)):
                    candidates.append(normalize_stream_url(m.group(1)))
            for m in re.finditer(r'(https?://[^\s"\'<>]+\.(?:m3u8|mpd|mp4)[^\s"\'<>]*)', unpacked, re.I):
                if is_stream_url(m.group(1)):
                    candidates.append(normalize_stream_url(m.group(1)))
    # 3. Generic stream URL fallback
    for m in re.finditer(r'(https?://[^\s"\'<>]+\.(?:m3u8|mpd|mp4)[^\s"\'<>]*)', html, re.I):
        if is_stream_url(m.group(1)):
            candidates.append(normalize_stream_url(m.group(1)))

    # Filter candidates: skip pkcdn.org and /dash/ because their hashes are scrambled until decoded by browser JS
    clean_candidates = [c for c in candidates if "pkcdn.org" not in c.lower() and "/dash/" not in c.lower()]
    if not clean_candidates:
        return None

    # Sort candidates by playback priority: .mp4 (direct) > .m3u8 (HLS) > .mpd (DASH)
    def stream_priority(u):
        u_low = u.lower().split("?")[0]
        if u_low.endswith(".mp4") or "/v.mp4" in u_low:
            return 1
        if u_low.endswith(".m3u8") or ".m3u8" in u_low:
            return 2
        if u_low.endswith(".mpd") or ".mpd" in u_low or "/dash/" in u_low:
            return 3
        return 4

    clean_candidates.sort(key=stream_priority)
    return clean_candidates[0]

class AndroidCaptchaHTTPHandler(http.server.BaseHTTPRequestHandler):
    target_info = {}
    captured_data = {}
    opener = None
    cookie_jar = None
    user_agent = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
    port = None
    recaptcha_cache = {}
    recaptcha_cookie_jar = http.cookiejar.CookieJar()
    recaptcha_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(recaptcha_cookie_jar))
    recent_logs = []

    @classmethod
    def record_log(cls, msg):
        import time
        t_str = time.strftime("%H:%M:%S")
        entry = f"[{t_str}] {msg}"
        cls.recent_logs.append(entry)
        if len(cls.recent_logs) > 30:
            cls.recent_logs.pop(0)

    @property
    def local_base(self):
        host = self.headers.get("Host") or f"127.0.0.1:{self.port}"
        return f"http://{host}"

    def log_message(self, format, *args):
        try:
            import xbmc
            xbmc.log(f"[MultiBridge HTTP] {format % args}", xbmc.LOGINFO)
        except Exception:
            pass

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Requested-With, Authorization, Origin, Accept")
        self.end_headers()

    def handle_recaptcha_proxy(self, is_post=False, post_data=None):
        parsed_req = urllib.parse.urlsplit(self.path)
        path = parsed_req.path

        is_gstatic = "/gstatic/" in path or "/releases/" in path
        is_api2 = "/api2/" in path
        is_enterprise = "/enterprise/" in path and not path.endswith("/enterprise.js")
        is_apijs = path.endswith("/api.js") or "/api.js" in path
        is_enterprisejs = path.endswith("/enterprise.js")

        if is_gstatic:
            if "/gstatic/" in path:
                sub = path.split("/gstatic/", 1)[1]
            elif "/releases/" in path:
                sub = "releases/" + path.split("/releases/", 1)[1]
            else:
                sub = path.lstrip("/")
            target_url = f"https://www.gstatic.com/recaptcha/{sub}"
            if parsed_req.query:
                target_url += f"?{parsed_req.query}"
        elif is_api2:
            sub = path.split("/api2/", 1)[1]
            target_url = f"https://www.google.com/recaptcha/api2/{sub}"
            if parsed_req.query:
                target_url += f"?{parsed_req.query}"
        elif is_enterprise:
            sub = path.split("/enterprise/", 1)[1]
            target_url = f"https://www.google.com/recaptcha/enterprise/{sub}"
            if parsed_req.query:
                target_url += f"?{parsed_req.query}"
        elif is_apijs:
            target_url = f"https://www.google.com/recaptcha/api.js?{parsed_req.query}" if parsed_req.query else "https://www.google.com/recaptcha/api.js"
        elif is_enterprisejs:
            target_url = f"https://www.google.com/recaptcha/enterprise.js?{parsed_req.query}" if parsed_req.query else "https://www.google.com/recaptcha/enterprise.js"
        else:
            qs = urllib.parse.parse_qs(parsed_req.query)
            target_url = qs.get("url", [""])[0]

        if not target_url:
            self.send_response(400)
            self.end_headers()
            return

        is_cache_hit = False
        resp_headers = {}
        if not is_post and target_url in self.recaptcha_cache:
            c_type, content = self.recaptcha_cache[target_url]
            status_code = 200
            is_cache_hit = True
        else:
            parsed_origin = urllib.parse.urlsplit(self.target_info.get("target_url", target_url))
            target_origin = f"{parsed_origin.scheme}://{parsed_origin.netloc}"
            target_co = base64.urlsafe_b64encode(f"{target_origin}:443".encode("utf-8")).decode("utf-8").rstrip("=")

            parsed = urllib.parse.urlsplit(target_url)
            q_dict = urllib.parse.parse_qs(parsed.query)
            if "co" in q_dict or "anchor" in parsed.path or "bframe" in parsed.path:
                q_dict["co"] = [target_co]
            new_query = urllib.parse.urlencode(q_dict, doseq=True)
            real_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment))

            req_ua = self.headers.get("User-Agent") or self.user_agent

            if parsed.path.endswith("/anchor"):
                headers = {
                    "User-Agent": req_ua,
                    "Referer": target_origin + "/",
                    "Origin": target_origin
                }
            elif is_api2 or is_enterprise:
                k_val = q_dict.get("k", [""])[0]
                v_val = q_dict.get("v", [""])[0]
                prefix = "api2" if is_api2 else "enterprise"
                headers = {
                    "User-Agent": req_ua,
                    "Referer": f"https://www.google.com/recaptcha/{prefix}/anchor?ar=1&k={k_val}&co={target_co}&hl=es&v={v_val}&size=normal",
                    "Origin": "https://www.google.com"
                }
            else:
                headers = {
                    "User-Agent": req_ua,
                    "Referer": target_origin + "/",
                    "Origin": target_origin
                }

            client_hints = ["Sec-Ch-Ua", "Sec-Ch-Ua-Mobile", "Sec-Ch-Ua-Platform", "Accept-Language"]
            for ch in client_hints:
                if ch in self.headers:
                    headers[ch] = self.headers[ch]

            if "Cookie" in self.headers:
                headers["Cookie"] = self.headers["Cookie"]

            if is_post:
                if post_data is None:
                    length = int(self.headers.get("Content-Length", 0))
                    post_data = self.rfile.read(length)
                if "Content-Type" in self.headers:
                    headers["Content-Type"] = self.headers["Content-Type"]
                else:
                    headers["Content-Type"] = "application/x-www-form-urlencoded"

            req = urllib.request.Request(real_url, data=post_data, headers=headers)
            try:
                resp = self.recaptcha_opener.open(req, timeout=14)
                content = resp.read()
                c_type = resp.headers.get("Content-Type", "text/html")
                status_code = resp.status if hasattr(resp, "status") else 200
                resp_headers = resp.headers
                if not is_post and (is_gstatic or is_apijs or is_enterprisejs):
                    self.recaptcha_cache[target_url] = (c_type, content)
            except Exception as e:
                AndroidCaptchaHTTPHandler.record_log(f"ERR {path.split('/')[-1]}: {str(e)[:40]}")
                self.send_response(500)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.end_headers()
                self.wfile.write(str(e).encode("utf-8"))
                return

        try:
            self.send_response(status_code)
            self.send_header("Content-Type", c_type)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Requested-With, Authorization, Origin, Accept")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")

            if not is_cache_hit:
                for h_name, h_val in resp_headers.items():
                    if h_name.lower() == "set-cookie":
                        clean_c = re.sub(r';\s*Domain=[^;]+', '', h_val, flags=re.I)
                        clean_c = re.sub(r';\s*Secure', '', clean_c, flags=re.I)
                        self.send_header("Set-Cookie", clean_c)

            self.end_headers()

            if is_post and "userverify" in path:
                resp_preview = content[:80].decode("utf-8", "ignore").replace("\n", " ")
                if "uvresp" in resp_preview:
                    if ",1]" in resp_preview or "[1]" in resp_preview:
                        AndroidCaptchaHTTPHandler.record_log("POST userverify -> 200 (desafio requerido)")
                    else:
                        AndroidCaptchaHTTPHandler.record_log("POST userverify -> 200 (resuelto)")
                else:
                    AndroidCaptchaHTTPHandler.record_log(f"POST userverify -> {status_code}")
            elif "anchor" in path:
                AndroidCaptchaHTTPHandler.record_log(f"GET anchor -> {status_code}")
            elif "bframe" in path:
                AndroidCaptchaHTTPHandler.record_log(f"GET bframe -> {status_code}")

            if "text/html" in c_type:
                html_str = content.decode("utf-8", errors="ignore")
                html_str = html_str.replace("https://www.google.com/recaptcha/api2/", f"{self.local_base}/recaptcha_proxy/api2/")
                html_str = html_str.replace("//www.google.com/recaptcha/api2/", f"{self.local_base}/recaptcha_proxy/api2/")
                html_str = html_str.replace("https://www.google.com/recaptcha/enterprise/", f"{self.local_base}/recaptcha_proxy/enterprise/")
                html_str = html_str.replace("//www.google.com/recaptcha/enterprise/", f"{self.local_base}/recaptcha_proxy/enterprise/")
                html_str = html_str.replace("https://www.gstatic.com/recaptcha/", f"{self.local_base}/recaptcha_proxy/gstatic/")
                html_str = html_str.replace("//www.gstatic.com/recaptcha/", f"{self.local_base}/recaptcha_proxy/gstatic/")

                iframe_shim = f"""<script>
(function() {{
    try {{
        var origPostMessage = Window.prototype.postMessage;
        Window.prototype.postMessage = function(msg, targetOrigin, transfer) {{
            if (typeof targetOrigin === 'string' && targetOrigin !== '*') {{
                targetOrigin = '*';
            }}
            return origPostMessage.call(this, msg, targetOrigin, transfer);
        }};
    }} catch(e) {{}}
}})();
</script>"""
                if "<head>" in html_str.lower():
                    idx_head = html_str.lower().find("<head>") + 6
                    html_str = html_str[:idx_head] + iframe_shim + html_str[idx_head:]
                else:
                    html_str = iframe_shim + html_str

                self.wfile.write(html_str.encode("utf-8"))
            elif "javascript" in c_type:
                js_str = content.decode("utf-8", errors="ignore")
                if is_apijs or is_enterprisejs:
                    js_str = re.sub(r'po\.integrity\s*=\s*["\'][^"\']*["\'];?', 'po.integrity="";', js_str)
                    js_str = re.sub(r'po\.crossOrigin\s*=\s*["\'][^"\']*["\'];?', '', js_str)
                    js_str = js_str.replace("https://www.gstatic.com/recaptcha/", f"{self.local_base}/recaptcha_proxy/gstatic/")
                    js_str = js_str.replace("//www.gstatic.com/recaptcha/", f"{self.local_base}/recaptcha_proxy/gstatic/")
                    js_str = js_str.replace("w['__recaptcha_api']='https://www.google.com/recaptcha/api2/';",
                                            f"w['__recaptcha_api']='{self.local_base}/recaptcha_proxy/api2/';")
                    js_str = js_str.replace("w['__recaptcha_api']='https://www.google.com/recaptcha/enterprise/';",
                                            f"w['__recaptcha_api']='{self.local_base}/recaptcha_proxy/enterprise/';")
                    js_str = js_str.replace("https://www.google.com/recaptcha/api2/", f"{self.local_base}/recaptcha_proxy/api2/")
                    js_str = js_str.replace("//www.google.com/recaptcha/api2/", f"{self.local_base}/recaptcha_proxy/api2/")
                    js_str = js_str.replace("https://www.google.com/recaptcha/enterprise/", f"{self.local_base}/recaptcha_proxy/enterprise/")
                    js_str = js_str.replace("//www.google.com/recaptcha/enterprise/", f"{self.local_base}/recaptcha_proxy/enterprise/")
                elif is_gstatic or is_api2:
                    js_str = js_str.replace("https://www.gstatic.com/recaptcha/", f"{self.local_base}/recaptcha_proxy/gstatic/")
                    js_str = js_str.replace("//www.gstatic.com/recaptcha/", f"{self.local_base}/recaptcha_proxy/gstatic/")
                    js_str = js_str.replace("https://www.google.com/recaptcha/api2/", f"{self.local_base}/recaptcha_proxy/api2/")
                    js_str = js_str.replace("//www.google.com/recaptcha/api2/", f"{self.local_base}/recaptcha_proxy/api2/")
                    js_str = js_str.replace("https://www.recaptcha.net/recaptcha/api2/", f"{self.local_base}/recaptcha_proxy/api2/")
                    js_str = js_str.replace("//www.recaptcha.net/recaptcha/api2/", f"{self.local_base}/recaptcha_proxy/api2/")
                    pattern_pm = r'm\.postMessage\(([^,]+),\s*w\[\d+\]\([^)]+\)\s*,\s*(\[[^\]]+\])\)'
                    js_str = re.sub(pattern_pm, r'm.postMessage(\1,"*",\2)', js_str)
                else:
                    js_str = js_str.replace("https://www.google.com/recaptcha/api2/", f"{self.local_base}/recaptcha_proxy/api2/")
                    js_str = js_str.replace("//www.google.com/recaptcha/api2/", f"{self.local_base}/recaptcha_proxy/api2/")
                    js_str = js_str.replace("https://www.recaptcha.net/recaptcha/api2/", f"{self.local_base}/recaptcha_proxy/api2/")
                    js_str = js_str.replace("//www.recaptcha.net/recaptcha/api2/", f"{self.local_base}/recaptcha_proxy/api2/")
                    pattern_pm = r'm\.postMessage\(([^,]+),\s*w\[\d+\]\([^)]+\)\s*,\s*(\[[^\]]+\])\)'
                    js_str = re.sub(pattern_pm, r'm.postMessage(\1,"*",\2)', js_str)

                resp_bytes = js_str.encode("utf-8")
                self.wfile.write(resp_bytes)
            else:
                self.wfile.write(content)
        except Exception as e:
            AndroidCaptchaHTTPHandler.record_log(f"ERR {path.split('/')[-1]}: {str(e)[:40]}")
            try:
                self.send_response(500)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.end_headers()
                self.wfile.write(str(e).encode("utf-8"))
            except Exception:
                pass


    def transform_html(self, content_bytes, final_url, source_name):
        html_str = content_bytes.decode("utf-8", errors="ignore")
        final_origin = f"{urllib.parse.urlsplit(final_url).scheme}://{urllib.parse.urlsplit(final_url).netloc}"
        final_domain = urllib.parse.urlsplit(final_url).netloc
        self.target_info["target_url"] = final_url
        self.target_info["final_origin"] = final_origin

        # Update dynamic form action and hidden fields from current page
        m_act = re.search(r'<form\b[^>]*action=["\']([^"\']*)["\']', html_str, re.I)
        if m_act:
            self.target_info["form_action"] = urllib.parse.urljoin(final_url, m_act.group(1))
        for m in re.finditer(r'<input[^>]+name=["\']([^"\']+)["\'][^>]+value=["\']([^"\']*)["\']', html_str, re.I):
            self.target_info.setdefault("hidden_fields", {})[m.group(1)] = m.group(2)
        for m in re.finditer(r'<input[^>]+value=["\']([^"\']*)["\'][^>]+name=["\']([^"\']+)["\']', html_str, re.I):
            self.target_info.setdefault("hidden_fields", {})[m.group(2)] = m.group(1)

        # 1. Stream detection
        stream_url = extract_stream_from_html(html_str)
        if not stream_url:
            m_if = re.search(r'<iframe\b[^>]*src=["\']([^"\']+)["\']', html_str, re.I)
            if m_if:
                sub_url = urllib.parse.urljoin(final_url, m_if.group(1))
                try:
                    sub_req = urllib.request.Request(sub_url, headers={"User-Agent": self.user_agent, "Referer": final_url})
                    sub_resp = self.opener.open(sub_req, timeout=12) if self.opener else urllib.request.urlopen(sub_req, timeout=12)
                    sub_content = sub_resp.read().decode("utf-8", errors="ignore")
                    stream_url = extract_stream_from_html(sub_content)
                except Exception:
                    pass

        if stream_url and not detect_packed(html_str):
            stream_url = normalize_stream_url(stream_url)
            cookie_str = "; ".join([f"{c.name}={c.value}" for c in self.cookie_jar]) if self.cookie_jar else ""
            self.captured_data["result"] = {
                "status": "ok",
                "url": stream_url,
                "headers": {
                    "User-Agent": self.user_agent,
                    "Referer": final_url,
                    "Cookie": cookie_str
                },
                "source": source_name
            }

        # 2. Static reCAPTCHA script rewriting
        def rewrite_recaptcha_script(match):
            full_url = match.group(0)
            parsed = urllib.parse.urlsplit(full_url)
            fname = os.path.basename(parsed.path) or "api.js"
            query = f"?{parsed.query}" if parsed.query else ""
            return f"{self.local_base}/recaptcha_proxy/{fname}{query}"

        html_str = re.sub(
            r'https?://(?:www\.)?google\.com/recaptcha/(?:api|enterprise)\.js(?:\?[^"\'<>\s]*)?',
            rewrite_recaptcha_script,
            html_str,
            flags=re.I
        )
        html_str = re.sub(
            r'//(?:www\.)?google\.com/recaptcha/(?:api|enterprise)\.js(?:\?[^"\'<>\s]*)?',
            rewrite_recaptcha_script,
            html_str,
            flags=re.I
        )

        # 3. Unified injection script
        injected_script = """
        <base href="__FINAL_ORIGIN__/">
        <script>
        (function() {
            var localBase = '__LOCAL_BASE__';

            try {
                window.open = function() { return null; };
                window.showModalDialog = function() { return null; };
            } catch(e) {}

            try {
                Object.defineProperty(document, 'domain', {
                    get: function() { return '__FINAL_DOMAIN__'; },
                    configurable: true
                });
            } catch(e) {}

            try {
                var origPostMessage = Window.prototype.postMessage;
                Window.prototype.postMessage = function(msg, targetOrigin, transfer) {
                    if (typeof targetOrigin === 'string' && targetOrigin !== '*') {
                        targetOrigin = '*';
                    }
                    return origPostMessage.call(this, msg, targetOrigin, transfer);
                };
            } catch(e) {}

            try {
                var origCWDesc = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow');
                if (origCWDesc && origCWDesc.get) {
                    var origCWGet = origCWDesc.get;
                    Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
                        get: function() {
                            var cw = origCWGet.call(this);
                            if (cw && !cw.__postMessageHooked) {
                                try {
                                    cw.__postMessageHooked = true;
                                    var origPM = cw.postMessage;
                                    cw.postMessage = function(msg, targetOrigin, transfer) {
                                        if (typeof targetOrigin === 'string' && targetOrigin !== '*') {
                                            targetOrigin = '*';
                                        }
                                        return origPM.call(this, msg, targetOrigin, transfer);
                                    };
                                } catch(e) {}
                            }
                            return cw;
                        },
                        configurable: true
                    });
                }
            } catch(e) {}

            var streamNotified = false;
            function notifyParent(streamUrl) {
                if (streamNotified) return;
                if (!streamUrl || typeof streamUrl !== 'string') return;
                if (streamUrl.startsWith('data:') || streamUrl.startsWith('blob:')) return;
                var low = streamUrl.toLowerCase();
                if (low.endsWith('.vtt') || low.endsWith('.srt') || low.endsWith('.jpg') || low.endsWith('.png') || low.endsWith('.js') || low.endsWith('.css') || low.endsWith('.svg')) return;
                if (low.includes('google') || low.includes('analytics') || low.includes('doubleclick') || low.includes('recaptcha') || low.includes('turnstile') || low.includes('hcaptcha')) return;

                streamNotified = true;

                try {
                    var mediaEls = document.querySelectorAll('video, audio');
                    for (var m = 0; m < mediaEls.length; m++) {
                        try {
                            mediaEls[m].pause();
                            mediaEls[m].muted = true;
                            mediaEls[m].src = '';
                            mediaEls[m].removeAttribute('src');
                            mediaEls[m].load();
                        } catch(me) {}
                    }
                } catch(e) {}

                if (window.__mbPlayInterval) {
                    try { clearInterval(window.__mbPlayInterval); } catch(e) {}
                }

                try { window.parent.postMessage({type: 'MB_STREAM_FOUND', url: streamUrl}, '*'); } catch(e) {}
                try {
                    fetch(localBase + '/stream', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({url: streamUrl, status: 'ok', source: 'android_client_sniffer'})
                    });
                } catch(e) {}

                try { window.stop(); } catch(e) {}
            }

            try {
                var origPlay = HTMLMediaElement.prototype.play;
                HTMLMediaElement.prototype.play = function() {
                    var s = this.currentSrc || this.src;
                    if (s && (s.startsWith('http://') || s.startsWith('https://'))) {
                        notifyParent(s);
                    }
                    return origPlay.apply(this, arguments);
                };

                var srcDesc = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'src');
                if (srcDesc && srcDesc.set) {
                    var origSrcSet = srcDesc.set;
                    Object.defineProperty(HTMLMediaElement.prototype, 'src', {
                        set: function(val) {
                            if (val && typeof val === 'string' && (val.startsWith('http://') || val.startsWith('https://'))) {
                                notifyParent(val);
                            }
                            return origSrcSet.apply(this, arguments);
                        },
                        get: srcDesc.get,
                        configurable: true
                    });
                }
            } catch(e) {}

            try {
                var origFetch = window.fetch;
                if (origFetch) {
                    window.fetch = function(input, init) {
                        try {
                            var u = (typeof input === 'string') ? input : (input && input.url ? input.url : '');
                            if (u && typeof u === 'string') {
                                var uLow = u.toLowerCase();
                                if (uLow.includes('.m3u8') || uLow.includes('.mpd') || uLow.includes('.mp4') || uLow.includes('/dash/') || uLow.includes('/hls/')) {
                                    notifyParent(u);
                                }
                            }
                        } catch(e) {}
                        return origFetch.apply(this, arguments);
                    };
                }

                var origXhrOpen = XMLHttpRequest.prototype.open;
                XMLHttpRequest.prototype.open = function(method, url) {
                    try {
                        if (url && typeof url === 'string') {
                            var uLow = url.toLowerCase();
                            if (uLow.includes('.m3u8') || uLow.includes('.mpd') || uLow.includes('.mp4') || uLow.includes('/dash/') || uLow.includes('/hls/')) {
                                notifyParent(url);
                            }
                        }
                    } catch(e) {}
                    return origXhrOpen.apply(this, arguments);
                };
            } catch(e) {}

            try {
                var tokenSubmitted = false;
                var syncTokens = function() {
                    try {
                        var ta = document.querySelector('textarea[name="g-recaptcha-response"]');
                        var inpResp = document.querySelector('input[name="g-recaptcha-response"]');
                        var inpToken = document.querySelector('input[name="token"]');
                        var val = (ta && ta.value) ? ta.value : (inpResp ? inpResp.value : '');
                        if (val && val.length > 20) {
                            if (inpResp && inpResp.value !== val) inpResp.value = val;
                            if (inpToken && inpToken.value !== val) inpToken.value = val;
                            var btn = document.getElementById('btn_download') || document.querySelector('input[type="submit"]') || document.querySelector('button[type="submit"]');
                            if (btn && btn.disabled) btn.disabled = false;
                            if (!tokenSubmitted) {
                                tokenSubmitted = true;
                                var f = document.getElementById('d0Form') || document.querySelector('form[method="POST"]') || document.querySelector('form');
                                if (f) {
                                    setTimeout(function() {
                                        try {
                                            if (btn) btn.click();
                                            else f.submit();
                                        } catch(e) {
                                            try { f.submit(); } catch(e2) {}
                                        }
                                    }, 250);
                                } else {
                                    fetch(localBase + '/token', {
                                        method: 'POST',
                                        headers: {'Content-Type': 'application/json'},
                                        body: JSON.stringify({token: val, url: '__FINAL_URL__'})
                                    }).catch(function() {});
                                }
                            }
                        }
                    } catch(err) {}
                };
                setInterval(syncTokens, 300);
                document.addEventListener('submit', syncTokens, true);
            } catch(e) {}

            try {
                setTimeout(function() {
                    try {
                        var rcwp = document.getElementById('rcwp');
                        if (rcwp && rcwp.children.length === 0 && typeof window.rcwpCallback === 'function' && window.grecaptcha && window.grecaptcha.render) {
                            window.rcwpCallback();
                        }
                    } catch(e) {}
                }, 800);
            } catch(e) {}

            try {
                var origFormSubmit = HTMLFormElement.prototype.submit;
                HTMLFormElement.prototype.submit = function() {
                    try {
                        var rawAct = this.getAttribute('action');
                        var act = '';
                        if (!rawAct || rawAct === '#' || rawAct === '') {
                            act = '__FINAL_URL__';
                        } else if (rawAct.startsWith('http://') || rawAct.startsWith('https://')) {
                            if (rawAct.indexOf(localBase) !== -1) {
                                var m = rawAct.match(/[?&]url=([^&]+)/);
                                act = m ? decodeURIComponent(m[1]) : '__FINAL_URL__';
                            } else {
                                act = rawAct;
                            }
                        } else if (rawAct.startsWith('/')) {
                            act = '__FINAL_ORIGIN__' + rawAct;
                        } else {
                            act = '__FINAL_ORIGIN__/' + rawAct;
                        }
                        this.action = localBase + '/proxy_submit?url=' + encodeURIComponent(act);
                    } catch(e) {}
                    return origFormSubmit.apply(this, arguments);
                };
            } catch(e) {}

            try {
                document.addEventListener('submit', function(e) {
                    if (e.target && e.target.tagName === 'FORM') {
                        var rawAct = e.target.getAttribute('action');
                        var act = '';
                        if (!rawAct || rawAct === '#' || rawAct === '') {
                            act = '__FINAL_URL__';
                        } else if (rawAct.startsWith('http://') || rawAct.startsWith('https://')) {
                            if (rawAct.indexOf(localBase) !== -1) {
                                var m = rawAct.match(/[?&]url=([^&]+)/);
                                act = m ? decodeURIComponent(m[1]) : '__FINAL_URL__';
                            } else {
                                act = rawAct;
                            }
                        } else if (rawAct.startsWith('/')) {
                            act = '__FINAL_ORIGIN__' + rawAct;
                        } else {
                            act = '__FINAL_ORIGIN__/' + rawAct;
                        }
                        e.target.action = localBase + '/proxy_submit?url=' + encodeURIComponent(act);
                    }
                }, true);
            } catch(e) {}

            try {
                window.__mbPlayInterval = setInterval(function() {
                    if (streamNotified) {
                        try { clearInterval(window.__mbPlayInterval); } catch(e) {}
                        return;
                    }
                    try {
                        var vids = document.querySelectorAll('video');
                        for (var i = 0; i < vids.length; i++) {
                            try { vids[i].muted = true; } catch(me) {}
                            if (vids[i].paused) {
                                vids[i].play().catch(function() {});
                            }
                        }
                        var playBtns = document.querySelectorAll('.player-play-button, .vjs-big-play-button, [data-player] button, .play-wrapper, [class*="play"], [id*="play"]');
                        for (var j = 0; j < playBtns.length; j++) {
                            var b = playBtns[j];
                            if (b.offsetParent !== null && b.id !== 'btn_download' && b.type !== 'submit') {
                                b.click();
                            }
                        }
                    } catch(e) {}
                }, 800);
            } catch(e) {}

            try {
                document.addEventListener('click', function(e) {
                    var a = e.target.closest ? e.target.closest('a') : null;
                    if (a && a.href) {
                        var href = a.href;
                        if ((href.startsWith('http://') || href.startsWith('https://')) && href.indexOf('__FINAL_ORIGIN__') === 0) {
                            var rel = href.substring('__FINAL_ORIGIN__'.length);
                            if (rel && rel !== '#' && !rel.startsWith('#') && !rel.startsWith('javascript:')) {
                                e.preventDefault();
                                location.href = localBase + '/proxy?url=' + encodeURIComponent(href);
                            }
                        }
                    }
                }, true);
            } catch(e) {}

            try {
                var origScriptSrc = Object.getOwnPropertyDescriptor(HTMLScriptElement.prototype, 'src');
                if (origScriptSrc && origScriptSrc.set) {
                    var origScriptSrcSet = origScriptSrc.set;
                    Object.defineProperty(HTMLScriptElement.prototype, 'src', {
                        set: function(val) {
                            var sVal = String(val || '');
                            if (sVal.indexOf('google.com/recaptcha/') !== -1 && sVal.indexOf('/recaptcha_proxy') === -1) {
                                if (sVal.indexOf('/api.js') !== -1) {
                                    var q = sVal.indexOf('?') !== -1 ? sVal.substring(sVal.indexOf('?')) : '';
                                    val = localBase + '/recaptcha_proxy/api.js' + q;
                                } else if (sVal.indexOf('/enterprise.js') !== -1) {
                                    var q = sVal.indexOf('?') !== -1 ? sVal.substring(sVal.indexOf('?')) : '';
                                    val = localBase + '/recaptcha_proxy/enterprise.js' + q;
                                } else if (sVal.indexOf('/api2/') !== -1) {
                                    val = localBase + '/recaptcha_proxy/api2/' + sVal.split('google.com/recaptcha/api2/')[1];
                                } else if (sVal.indexOf('/enterprise/') !== -1) {
                                    val = localBase + '/recaptcha_proxy/enterprise/' + sVal.split('google.com/recaptcha/enterprise/')[1];
                                }
                            } else if (sVal.indexOf('gstatic.com/recaptcha/') !== -1 && sVal.indexOf('/recaptcha_proxy') === -1) {
                                val = localBase + '/recaptcha_proxy/gstatic/' + sVal.split('gstatic.com/recaptcha/')[1];
                            }
                            return origScriptSrcSet.call(this, val);
                        },
                        get: origScriptSrc.get,
                        configurable: true
                    });
                }

                var origIframeSrc = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'src');
                if (origIframeSrc && origIframeSrc.set) {
                    var origIframeSrcSet = origIframeSrc.set;
                    Object.defineProperty(HTMLIFrameElement.prototype, 'src', {
                        set: function(val) {
                            var sVal = String(val || '');
                            if (sVal.indexOf('google.com/recaptcha/') !== -1 && sVal.indexOf('/recaptcha_proxy') === -1) {
                                if (sVal.indexOf('/api2/') !== -1) {
                                    val = localBase + '/recaptcha_proxy/api2/' + sVal.split('google.com/recaptcha/api2/')[1];
                                } else if (sVal.indexOf('/enterprise/') !== -1) {
                                    val = localBase + '/recaptcha_proxy/enterprise/' + sVal.split('google.com/recaptcha/enterprise/')[1];
                                }
                            } else if (sVal.indexOf('gstatic.com/recaptcha/') !== -1 && sVal.indexOf('/recaptcha_proxy') === -1) {
                                val = localBase + '/recaptcha_proxy/gstatic/' + sVal.split('gstatic.com/recaptcha/')[1];
                            }
                            return origIframeSrcSet.call(this, val);
                        },
                        get: origIframeSrc.get,
                        configurable: true
                    });
                }

                var origSetAttr = Element.prototype.setAttribute;
                Element.prototype.setAttribute = function(name, val) {
                    if (name && name.toLowerCase() === 'src') {
                        var sVal = String(val || '');
                        if (sVal.indexOf('google.com/recaptcha/') !== -1 && sVal.indexOf('/recaptcha_proxy') === -1) {
                            if (sVal.indexOf('/api.js') !== -1) {
                                var q = sVal.indexOf('?') !== -1 ? sVal.substring(sVal.indexOf('?')) : '';
                                val = localBase + '/recaptcha_proxy/api.js' + q;
                            } else if (sVal.indexOf('/enterprise.js') !== -1) {
                                var q = sVal.indexOf('?') !== -1 ? sVal.substring(sVal.indexOf('?')) : '';
                                val = localBase + '/recaptcha_proxy/enterprise.js' + q;
                            } else if (sVal.indexOf('/api2/') !== -1) {
                                val = localBase + '/recaptcha_proxy/api2/' + sVal.split('google.com/recaptcha/api2/')[1];
                            } else if (sVal.indexOf('/enterprise/') !== -1) {
                                val = localBase + '/recaptcha_proxy/enterprise/' + sVal.split('google.com/recaptcha/enterprise/')[1];
                            }
                        } else if (sVal.indexOf('gstatic.com/recaptcha/') !== -1 && sVal.indexOf('/recaptcha_proxy') === -1) {
                            val = localBase + '/recaptcha_proxy/gstatic/' + sVal.split('gstatic.com/recaptcha/')[1];
                        }
                    }
                    return origSetAttr.call(this, name, val);
                };
            } catch(e) {}
        })();
        </script>
        """
        injected_script = injected_script.replace("__FINAL_ORIGIN__", final_origin)
        injected_script = injected_script.replace("__LOCAL_BASE__", self.local_base)
        injected_script = injected_script.replace("__FINAL_DOMAIN__", final_domain)
        injected_script = injected_script.replace("__FINAL_URL__", final_url)


        if "<head>" in html_str:
            html_str = html_str.replace("<head>", "<head>" + injected_script, 1)
        else:
            html_str = injected_script + html_str

        return html_str.encode("utf-8")


    def do_GET(self):
        clean_path = self.path.split("?")[0]
        if clean_path in ["/", "/captcha"]:
            target_url = self.target_info.get("target_url", "")
            title = self.target_info.get("title", "Multi Bridge")
            proxy_src = f"{self.local_base}/proxy?url={urllib.parse.quote(target_url)}" if target_url else ""

            html = f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Multi Bridge - {title}</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        html, body {{
            width: 100%;
            height: 100%;
            overflow: hidden;
            background: #0d1117;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            color: #e6edf3;
        }}
        #top-bar {{
            height: 48px;
            background: #161b22;
            border-bottom: 2px solid #238636;
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0 16px;
            font-size: 13px;
            z-index: 999999;
            position: relative;
        }}
        .brand {{
            font-weight: 700;
            color: #58a6ff;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 25%;
        }}
        .debug-badge {{
            background: #21262d;
            border: 1px solid #30363d;
            color: #f0883e;
            padding: 3px 8px;
            border-radius: 4px;
            font-family: monospace;
            font-size: 11px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 35%;
        }}
        .status-msg {{
            color: #8b949e;
            font-size: 13px;
            text-align: center;
            flex-grow: 1;
            padding: 0 10px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            transition: all 0.3s ease;
        }}
        .status-msg.success {{
            color: #3fb950;
            font-weight: 700;
        }}
        .btn-kodi {{
            background: #238636;
            color: #ffffff;
            border: none;
            padding: 6px 14px;
            border-radius: 6px;
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
            text-decoration: none;
            display: none;
            white-space: nowrap;
        }}
        .btn-kodi:hover {{ background: #2ea043; }}
        #frame-wrap {{
            width: 100%;
            height: calc(100% - 48px);
            position: relative;
            background: #ffffff;
        }}
        iframe {{
            width: 100%;
            height: 100%;
            border: none;
            display: block;
        }}
    </style>
    <script>
        var streamHandled = false;

        window.onerror = function(msg, url, line) {{
            var dbg = document.getElementById('debug-badge');
            if (dbg) dbg.innerText = 'JS Err: ' + msg;
        }};
        window.addEventListener('unhandledrejection', function(e) {{
            var dbg = document.getElementById('debug-badge');
            if (dbg) dbg.innerText = 'Rejection: ' + (e.reason ? (e.reason.message || e.reason) : 'unknown');
        }});

        function playChime() {{
            try {{
                var ctx = new (window.AudioContext || window.webkitAudioContext)();
                var now = ctx.currentTime;
                [523.25, 659.25, 783.99].forEach(function(freq, i) {{
                    var osc = ctx.createOscillator();
                    var gain = ctx.createGain();
                    osc.type = 'sine';
                    osc.frequency.setValueAtTime(freq, now + (i * 0.1));
                    gain.gain.setValueAtTime(0.2, now + (i * 0.1));
                    gain.gain.exponentialRampToValueAtTime(0.001, now + (i * 0.1) + 0.35);
                    osc.connect(gain);
                    gain.connect(ctx.destination);
                    osc.start(now + (i * 0.1));
                    osc.stop(now + (i * 0.1) + 0.4);
                }});
            }} catch(e) {{}}
        }}

        var returnedToKodi = false;
        function returnToKodi() {{
            if (returnedToKodi) return;
            returnedToKodi = true;
            try {{
                window.location.href = "kodi://launch";
            }} catch(e) {{}}
            try {{
                window.close();
            }} catch(e) {{}}
        }}

        function onStreamCaptured(url, fromStatus) {{
            if (streamHandled) return;
            streamHandled = true;
            playChime();

            try {{ if (pollTimer) clearInterval(pollTimer); }} catch(e) {{}}
            try {{ if (logTimer) clearInterval(logTimer); }} catch(e) {{}}

            try {{
                var f = document.getElementById('main-frame');
                if (f) {{
                    f.src = 'about:blank';
                }}
            }} catch(e) {{}}

            var st = document.getElementById('status-msg');
            if (st) {{
                st.className = 'status-msg success';
                st.innerHTML = 'STREAM OBTENIDO CON EXITO - Reproduciendo en Kodi...';
            }}
            var btn = document.getElementById('btn-kodi');
            if (btn) {{
                btn.style.display = 'inline-block';
                btn.href = 'kodi://launch';
            }}

            var ov = document.getElementById('success-overlay');
            if (ov) ov.style.display = 'flex';

            if (!fromStatus) {{
                fetch('/stream', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ url: url, status: 'ok', source: 'android_client_event' }})
                }}).then(function() {{
                    setTimeout(returnToKodi, 300);
                }}).catch(function() {{
                    setTimeout(returnToKodi, 300);
                }});
            }} else {{
                setTimeout(returnToKodi, 300);
            }}
        }}

        window.addEventListener('message', function(e) {{
            if (e.data && e.data.type === 'MB_STREAM_FOUND' && e.data.url) {{
                onStreamCaptured(e.data.url, false);
            }}
        }});

        var pollTimer = setInterval(function() {{
            if (streamHandled) {{
                clearInterval(pollTimer);
                return;
            }}
            fetch('/status')
                .then(function(r) {{ return r.json(); }})
                .then(function(d) {{
                    if (d && d.status === 'ok' && d.url) {{
                        clearInterval(pollTimer);
                        onStreamCaptured(d.url, true);
                    }}
                }})
                .catch(function() {{}});
        }}, 800);

        var logTimer = setInterval(function() {{
            fetch('/logs')
                .then(function(r) {{ return r.json(); }})
                .then(function(logs) {{
                    if (logs && logs.length > 0) {{
                        var last = logs[logs.length - 1];
                        var dbg = document.getElementById('debug-badge');
                        if (dbg && !dbg.innerText.startsWith('JS Err')) {{
                            dbg.innerText = last;
                        }}
                    }}
                }})
                .catch(function() {{}});
        }}, 600);
    </script>
</head>
<body>
    <div id="top-bar">
        <span class="brand">Multi Bridge v1.0.139</span>
        <span id="debug-badge" class="debug-badge">Iniciando...</span>
        <span id="status-msg" class="status-msg">Resuelve el captcha o pulsa reproducir para desbloquear el video</span>
        <a id="btn-kodi" class="btn-kodi" href="kodi://launch" onclick="returnToKodi()">Volver a Kodi</a>
    </div>
    <div id="frame-wrap">
        <iframe id="main-frame" src="{proxy_src}" allow="autoplay; fullscreen; encrypted-media; notifications"></iframe>
        <div id="success-overlay" style="display:none;position:absolute;top:0;left:0;width:100%;height:100%;background:#0d1117;z-index:999999;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:24px;box-sizing:border-box;">
            <div style="width:64px;height:64px;border-radius:50%;background:#238636;color:#ffffff;display:flex;align-items:center;justify-content:center;font-size:36px;font-weight:700;margin-bottom:20px;">&#10003;</div>
            <h2 style="color:#ffffff;margin-bottom:12px;font-size:22px;font-weight:600;">Stream capturado con exito</h2>
            <p style="color:#8b949e;margin-bottom:28px;font-size:15px;max-width:440px;line-height:1.5;">
                El video ya se esta reproduciendo en Kodi.<br>Pulsa el boton para volver o usa el boton Volver de tu mando.
            </p>
            <a id="btn-overlay-kodi" href="kodi://launch" onclick="returnToKodi()" style="background:#238636;color:#ffffff;text-decoration:none;padding:14px 32px;border-radius:8px;font-size:16px;font-weight:700;box-shadow:0 4px 16px rgba(35,134,54,0.4);display:inline-block;cursor:pointer;">VOLVER A KODI</a>
        </div>
    </div>
</body>
</html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
        elif clean_path == "/proxy":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            target = qs.get("url", [""])[0] or self.target_info.get("target_url", "")
            if "/proxy" in target and ("127.0.0.1" in target or "localhost" in target):
                inner_qs = urllib.parse.parse_qs(urllib.parse.urlsplit(target).query)
                target = inner_qs.get("url", [""])[0] or self.target_info.get("target_url", target)
            if not target:
                self.send_response(400)
                self.end_headers()
                return

            req_ua = self.headers.get("User-Agent") or self.user_agent
            req = urllib.request.Request(target, headers={
                "User-Agent": req_ua,
                "Accept": self.headers.get("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
                "Referer": target
            })
            try:
                resp = self.opener.open(req, timeout=15) if self.opener else urllib.request.urlopen(req, timeout=15)
                content = resp.read()
                content_type = resp.headers.get("Content-Type", "text/html")
                final_url = resp.geturl() or target
                final_origin = f"{urllib.parse.urlsplit(final_url).scheme}://{urllib.parse.urlsplit(final_url).netloc}"

                self.send_response(resp.status if hasattr(resp, "status") else 200)
                self.send_header("Content-Type", content_type)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()

                if "text/html" in content_type:
                    self.wfile.write(self.transform_html(content, final_url, "android_proxy_direct"))
                else:
                    self.wfile.write(content)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode("utf-8"))
        elif clean_path.startswith("/recaptcha") or clean_path.startswith("/gstatic") or clean_path.startswith("/api2") or clean_path.endswith("/api.js") or clean_path.endswith("/enterprise.js"):
            self.handle_recaptcha_proxy(is_post=False)
        elif clean_path == "/logs":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(json.dumps(self.recent_logs).encode("utf-8"))
        elif clean_path == "/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(json.dumps(self.captured_data.get("result", {})).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        clean_path = self.path.split("?")[0]
        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length)

        if clean_path.startswith("/recaptcha") or clean_path.startswith("/gstatic") or clean_path.startswith("/api2"):
            self.handle_recaptcha_proxy(is_post=True, post_data=post_data)
        elif clean_path == "/stream":
            try:
                body = json.loads(post_data.decode("utf-8"))
                url = body.get("url")
                if url:
                    url = normalize_stream_url(url)
                    if not self.captured_data.get("result"):
                        cookie_str = "; ".join([f"{c.name}={c.value}" for c in self.cookie_jar]) if self.cookie_jar else ""
                        self.captured_data["result"] = {
                            "status": "ok",
                            "url": url,
                            "headers": {
                                "User-Agent": self.user_agent,
                                "Referer": self.target_info.get("target_url", ""),
                                "Cookie": cookie_str
                            },
                            "source": body.get("source", "android_smart_proxy")
                        }
                        bring_kodi_to_front()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "ok"}).encode("utf-8"))
            except Exception:
                self.send_response(500)
                self.end_headers()
        elif clean_path in ["/proxy_submit", "/proxy"]:
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            target = qs.get("url", [""])[0]
            if not target:
                target = self.target_info.get("form_action") or self.target_info.get("target_url")
            if not target:
                self.send_response(400)
                self.end_headers()
                return

            if "/proxy" in target and ("127.0.0.1" in target or "localhost" in target):
                inner_qs = urllib.parse.parse_qs(urllib.parse.urlsplit(target).query)
                target = inner_qs.get("url", [""])[0] or self.target_info.get("target_url", target)

            # Ensure imhuman submit field for Powvideo
            if ("powvideo" in target.lower() or "powwideo" in target.lower()) and b"imhuman=" not in post_data:
                post_data = (post_data + b"&imhuman=Continue+to+Video") if post_data else b"imhuman=Continue+to+Video"

            req_ua = self.headers.get("User-Agent") or self.user_agent
            req = urllib.request.Request(target, data=post_data, headers={
                "User-Agent": req_ua,
                "Content-Type": self.headers.get("Content-Type", "application/x-www-form-urlencoded"),
                "Referer": self.target_info.get("target_url", target)
            })
            try:
                resp = self.opener.open(req, timeout=18) if self.opener else urllib.request.urlopen(req, timeout=18)
                content = resp.read()
                content_type = resp.headers.get("Content-Type", "text/html")
                final_url = resp.geturl() or target
                final_origin = f"{urllib.parse.urlsplit(final_url).scheme}://{urllib.parse.urlsplit(final_url).netloc}"

                self.send_response(resp.status if hasattr(resp, "status") else 200)
                self.send_header("Content-Type", content_type)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()

                if "text/html" in content_type:
                    self.wfile.write(self.transform_html(content, final_url, "android_proxy_submit"))
                else:
                    self.wfile.write(content)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode("utf-8"))
        elif clean_path == "/token":
            try:
                body = json.loads(post_data.decode("utf-8"))
                token = body.get("token", "")
                AndroidCaptchaHTTPHandler.record_log(f"POST /token -> Token captcha recibido ({token[:15]}...)")
                form_action = self.target_info.get("form_action", "") or self.target_info.get("target_url", "")
                hidden_fields = self.target_info.get("hidden_fields", {}).copy()
                hidden_fields["g-recaptcha-response"] = token
                hidden_fields["token"] = token
                if "imhuman" not in hidden_fields:
                    hidden_fields["imhuman"] = "Continue to Video"
                encoded = urllib.parse.urlencode(hidden_fields).encode("utf-8")
                req_ua = self.headers.get("User-Agent") or self.user_agent
                req = urllib.request.Request(form_action, data=encoded, headers={
                    "User-Agent": req_ua,
                    "Referer": self.target_info.get("target_url", ""),
                    "Content-Type": "application/x-www-form-urlencoded"
                })
                resp = self.opener.open(req, timeout=18)
                resp_content = resp.read().decode("utf-8", errors="ignore")
                stream_url = extract_stream_from_html(resp_content)
                if stream_url and not detect_packed(resp_content):
                    stream_url = normalize_stream_url(stream_url)
                    AndroidCaptchaHTTPHandler.record_log(f"STREAM DETECTADO: {stream_url[:35]}...")
                    cookie_str = "; ".join([f"{c.name}={c.value}" for c in self.cookie_jar]) if self.cookie_jar else ""
                    self.captured_data["result"] = {
                        "status": "ok",
                        "url": stream_url,
                        "headers": {
                            "User-Agent": req_ua,
                            "Referer": self.target_info.get("target_url", ""),
                            "Cookie": cookie_str
                        },
                        "source": "android_captcha_token"
                    }
                    bring_kodi_to_front()
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
                    self.wfile.write(json.dumps({"status": "error", "message": "No stream found"}).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.end_headers()

def capture_stream_android(target_url, timeout=180, progress_dialog=None, monitor=None):
    global _active_android_server
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

    if _active_android_server:
        try:
            _active_android_server.shutdown()
            _active_android_server.server_close()
        except Exception:
            pass
        _active_android_server = None

    AndroidCaptchaHTTPHandler.recaptcha_cache.clear()
    AndroidCaptchaHTTPHandler.recent_logs.clear()
    try:
        AndroidCaptchaHTTPHandler.recaptcha_cookie_jar.clear()
    except Exception:
        pass

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

    httpd = None
    for test_port in [39969, 39970, 39971]:
        try:
            httpd = LocalServer(("0.0.0.0", test_port), AndroidCaptchaHTTPHandler)
            break
        except Exception:
            try:
                httpd = LocalServer(("127.0.0.1", test_port), AndroidCaptchaHTTPHandler)
                break
            except Exception:
                pass
    if httpd is None:
        try:
            httpd = LocalServer(("0.0.0.0", 0), AndroidCaptchaHTTPHandler)
        except Exception:
            httpd = LocalServer(("127.0.0.1", 0), AndroidCaptchaHTTPHandler)
    
    _active_android_server = httpd
    port = httpd.server_address[1]
    AndroidCaptchaHTTPHandler.port = port
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    local_url = f"http://127.0.0.1:{port}/captcha?t={int(time.time())}"
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

    def delayed_shutdown(srv, delay=8.0):
        def _runner():
            time.sleep(delay)
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass
            global _active_android_server
            if _active_android_server is srv:
                _active_android_server = None
        t = threading.Thread(target=_runner, daemon=True)
        t.start()

    start_time = time.time()
    result = None
    try:
        while True:
            if captured_data.get("result"):
                bring_kodi_to_front()
                result = captured_data["result"]
                delayed_shutdown(httpd, delay=8.0)
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
        if not result and not captured_data.get("result"):
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:
                pass
            if _active_android_server is httpd:
                _active_android_server = None

    return result or captured_data.get("result") or {"status": "error", "message": "No se capturo el stream de video."}

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

    addon_data_dir = os.path.expandvars(r"%APPDATA%\Kodi\userdata\addon_data\plugin.video.bridge.multi")
    user_data_dir = os.path.join(addon_data_dir, "web_profile")
    legacy_test_dir = os.path.join(addon_data_dir, "test_profile")
    _cleanup_profile_dir(legacy_test_dir)
    _cleanup_profile_dir(user_data_dir)
    os.makedirs(user_data_dir, exist_ok=True)
    sync_cookies_from_main_browser(user_data_dir)
    fix_chrome_crash_state(user_data_dir)

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
        "--disable-session-crashed-bubble",
        "--hide-crash-restore-bubble",
        "--disable-infobars",
        "--disable-component-update",
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
        if b_ws:
            try:
                b_ws.send_msg(json.dumps({"id": 99999, "method": "Browser.close"}))
                for _ in range(25):
                    if proc.poll() is not None:
                        break
                    time.sleep(0.04)
            except Exception:
                pass
        if p_ws:
            try: p_ws.close()
            except Exception: pass
        if b_ws:
            try: b_ws.close()
            except Exception: pass
        try:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)
        except Exception:
            pass
        _cleanup_profile_dir(user_data_dir, retries=6, delay=0.15)
        _cleanup_profile_dir(legacy_test_dir, retries=1, delay=0.05)

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
