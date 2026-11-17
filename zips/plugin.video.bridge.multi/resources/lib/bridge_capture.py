#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Web Stream Bridge - Capture Helper (Universal Engine)
Launches Chromium / Edge / Chrome with strict popup blocking,
pure-Python CDP network interception, and local receiver fallback.
Works 100% in-process within Kodi's Python or as a standalone CLI script.
No external Python dependencies required.
"""

import sys
import os
import json
import glob
import subprocess
import socket
import shutil
import time
import base64
import struct
import urllib.request
import urllib.parse
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
    """Lightweight RFC 6455 client implemented purely with standard library."""
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

    def recv_msg(self, timeout=0.05):
        self.sock.settimeout(timeout)
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

def is_ad_domain(url):
    if not url or not isinstance(url, str):
        return False
    u = url.lower()
    patterns = [
        "aliexpress", "adcash", "popunder", "propeller", "ay267", "cdclub",
        "doubleclick", "double?", "onclickperf", "roadster24", "official-app",
        "clickadu", "adsterra", "exoclick", "bet365", "1xbet", "casino",
        "syndication", "adservices"
    ]
    return any(p in u for p in patterns)

def capture_stream(target_url, timeout=180, custom_browser=None, app_mode=True, load_adblock=True, progress_dialog=None, monitor=None):
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

    # Wait for DevTools port to be ready
    ws_ready = False
    for _ in range(30):
        if proc.poll() is not None:
            break
        try:
            v_url = f"http://127.0.0.1:{cdp_port}/json/version"
            resp = urllib.request.urlopen(v_url, timeout=1.0).read()
            v_info = json.loads(resp.decode('utf-8'))
            browser_ws_url = v_info.get("webSocketDebuggerUrl")
            if browser_ws_url:
                b_ws = SimpleWebSocket(browser_ws_url)
                b_ws.send_json({"id": 1, "method": "Target.setDiscoverTargets", "params": {"discover": True}})
                b_ws.send_json({"id": 2, "method": "Target.setAutoAttach", "params": {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True}})
                ws_ready = True
                break
        except Exception:
            time.sleep(0.2)

    # Connect to the main page target
    if ws_ready:
        for _ in range(20):
            try:
                t_url = f"http://127.0.0.1:{cdp_port}/json"
                targets = json.loads(urllib.request.urlopen(t_url, timeout=1.0).read().decode('utf-8'))
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
                time.sleep(0.2)

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

            # 1. Check Browser WS events (Strict Popup Killer)
            if b_ws:
                b_msg = b_ws.recv_msg(timeout=0.04)
                if b_msg:
                    try:
                        d = json.loads(b_msg)
                        method = d.get("method", "")
                        if "Target.targetCreated" in method:
                            t_info = d.get("params", {}).get("targetInfo", {})
                            t_id = t_info.get("targetId")
                            t_type = t_info.get("type")
                            t_url = t_info.get("url", "")
                            if t_type == "page" and t_id != main_page_id:
                                # Immediately kill popup tab/window
                                b_ws.send_json({"id": 99, "method": "Target.closeTarget", "params": {"targetId": t_id}})
                        elif "Target.attachedToTarget" in method:
                            t_info = d.get("params", {}).get("targetInfo", {})
                            t_id = t_info.get("targetId")
                            if t_id and t_id != main_page_id:
                                b_ws.send_json({"id": 99, "method": "Target.closeTarget", "params": {"targetId": t_id}})
                    except Exception:
                        pass

            # 2. Check Page WS events (Network & Media Stream Sniffer)
            if p_ws:
                p_msg = p_ws.recv_msg(timeout=0.04)
                if p_msg:
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
            else:
                time.sleep(0.08)

    finally:
        if p_ws:
            p_ws.close()
        if b_ws:
            b_ws.close()
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
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
