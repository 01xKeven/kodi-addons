#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Web Stream Bridge - Capture Helper (Native Extension Engine)
Launches Chromium / Edge with an internal lightweight background sniffer extension,
clean user session, and zero DevTools/CDP debugging flags.
Works 100% in-process within Kodi's Python or as a standalone CLI script.
No external Python executable required.
"""

import sys
import os
import json
import glob
import subprocess
import socket
import shutil
import time
import http.server
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

KNOWN_ADBLOCK_IDS = [
    "ddkjiahejlhfcafbddmgiahcphecmpfh",  # uBlock Origin Lite
    "cjpalhdlnbpafiamejdnhcphjbkeiagm",  # uBlock Origin
    "bgnkhhnnamicmpeenaelnjfhikgbkllg",  # AdGuard
    "gighmmpiobklfepjocnamgkkbiglidom",  # AdBlock
]

def find_adblock_extensions():
    found = []
    search_roots = [
        os.path.expandvars(r"%LOCALAPPDATA%\Chromium\User Data\Default\Extensions"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data\Default\Extensions"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Default\Extensions"),
    ]
    for root in search_roots:
        if not os.path.exists(root):
            continue
        for ext_id in KNOWN_ADBLOCK_IDS:
            ext_dir = os.path.join(root, ext_id)
            if os.path.isdir(ext_dir):
                versions = glob.glob(os.path.join(ext_dir, "*"))
                for v in versions:
                    if os.path.isdir(v) and os.path.isfile(os.path.join(v, "manifest.json")):
                        found.append(v)
                        break
    return found

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

    # Búsqueda dinámica para Edge / EdgeCore y Chrome con carpetas de versiones
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

    for name in ["msedge.exe", "chrome.exe", "brave.exe", "msedge", "chrome"]:
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
                # HWND_TOPMOST (-1) ensures the modal window stays directly on top of Kodi
                user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
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

def capture_stream(target_url, timeout=180, custom_browser=None, app_mode=True, load_adblock=True, progress_dialog=None, monitor=None):
    browser_path = find_browser(custom_browser)
    if not browser_path:
        return {"status": "error", "message": "No se encontró ningún navegador Chromium (Chrome o Edge) en el sistema."}

    script_dir = os.path.dirname(os.path.abspath(__file__))
    ext_dir = os.path.abspath(os.path.join(script_dir, "..", "extension"))

    if not os.path.isdir(ext_dir) or not os.path.isfile(os.path.join(ext_dir, "manifest.json")):
        # Búsqueda alternativa por ruta canónica de Kodi
        fallback_ext = os.path.expandvars(r"%APPDATA%\Kodi\addons\plugin.video.bridge.multi\resources\extension")
        if os.path.isdir(fallback_ext) and os.path.isfile(os.path.join(fallback_ext, "manifest.json")):
            ext_dir = fallback_ext
        else:
            return {"status": "error", "message": "No se encontró la extensión de captura en resources/extension"}

    server_port = find_free_port()
    port_config = os.path.join(ext_dir, "port.json")
    try:
        with open(port_config, "w", encoding="utf-8") as f:
            json.dump({"port": server_port}, f)
    except Exception as e:
        return {"status": "error", "message": f"Error configurando puerto: {e}"}

    captured_data = {}
    captured_event = threading.Event()

    class StreamReceiverHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            nonlocal captured_data
            if self.path == "/stream":
                try:
                    length = int(self.headers.get("content-length", 0))
                    body = self.rfile.read(length)
                    data = json.loads(body.decode("utf-8"))
                    captured_data = data
                    captured_event.set()
                except Exception:
                    pass
                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
            else:
                self.send_response(404)
                self.end_headers()

        def do_OPTIONS(self):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def log_message(self, format, *args):
            pass

    try:
        server = http.server.HTTPServer(("127.0.0.1", server_port), StreamReceiverHandler)
    except Exception as e:
        return {"status": "error", "message": f"No se pudo iniciar el servidor receptor: {e}"}

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    user_data_dir = os.path.expandvars(r"%APPDATA%\Kodi\userdata\addon_data\plugin.video.bridge.multi\web_profile")
    os.makedirs(user_data_dir, exist_ok=True)
    sync_cookies_from_main_browser(user_data_dir)

    extensions_to_load = [ext_dir]
    if load_adblock:
        adblock_exts = find_adblock_extensions()
        for ad_ext in adblock_exts:
            if ad_ext not in extensions_to_load:
                extensions_to_load.append(ad_ext)

    args = [
        browser_path,
        f"--load-extension={','.join(extensions_to_load)}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]

    if app_mode:
        width = 860
        height = 640
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

        args.extend([
            f"--window-size={width},{height}",
            f"--window-position={pos_x},{pos_y}",
            f"--app={target_url}"
        ])
    else:
        args.extend([
            "--start-maximized",
            "--new-window",
            target_url
        ])

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
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass
        return {"status": "error", "message": f"Error al ejecutar navegador: {pe}"}

    # Elevar ventana del navegador sobre Kodi de forma diferida para esperar su renderizado
    def _bring_delayed():
        for _ in range(8):
            time.sleep(0.35)
            if proc.poll() is not None or captured_event.is_set():
                break
            bring_browser_to_front(proc.pid, is_app=app_mode)

    threading.Thread(target=_bring_delayed, daemon=True).start()

    start_time = time.time()
    try:
        while True:
            if proc.poll() is not None:
                # Margen de gracia por si la extensión interceptó justo al cerrarse
                for _ in range(6):
                    if captured_event.is_set():
                        break
                    time.sleep(0.25)
                if captured_event.is_set():
                    break
                return {"status": "error", "message": "Navegador cerrado antes de completar la verificación."}

            if captured_event.is_set():
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

            time.sleep(0.25)

    finally:
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass
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
    parser.add_argument("--app", action="store_true", help="Abrir en modo ventana de aplicacion (sin barra de direcciones)")
    parser.add_argument("--no-adblock", action="store_true", help="No intentar cargar extensiones bloqueadoras de publicidad")

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
