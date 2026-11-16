# -*- coding: utf-8 -*-
import sys
import os
import json
import subprocess
import urllib.parse
import xbmc
import xbmcgui

def get_video_url(page_url, url_referer=''):
    xbmc.log(f"[Balandro Streamplay] Resolviendo enlace: {page_url}", xbmc.LOGINFO)

    bridge_addon = os.path.expandvars(r'%APPDATA%\Kodi\addons\plugin.video.bridge.multi')
    capture_script = os.path.join(bridge_addon, 'resources', 'lib', 'bridge_capture.py')
    
    if not os.path.exists(capture_script):
        return 'Falta el modulo de captura web en Multi Bridge'

    python_exe = sys.executable
    if not python_exe or 'kodi' in python_exe.lower():
        local_app = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            r"C:\Users\ANGEL\AppData\Local\Programs\Python\Python314\python.exe",
            os.path.join(local_app, r"Programs\Python\Python314\python.exe"),
            os.path.join(local_app, r"Programs\Python\Python313\python.exe"),
            os.path.join(local_app, r"Programs\Python\Python312\python.exe"),
            os.path.join(local_app, r"Programs\Python\Python311\python.exe"),
        ]
        for c in candidates:
            if os.path.exists(c):
                python_exe = c
                break

    icon_path = os.path.join(bridge_addon, "icon.png") if os.path.exists(os.path.join(bridge_addon, "icon.png")) else ""
    xbmcgui.Dialog().notification(
        "Streamplay (Balandro)",
        "Resuelve el captcha en la ventana flotante...",
        icon_path,
        5000
    )

    clean_env = os.environ.copy()
    clean_env.pop("PYTHONPATH", None)
    clean_env.pop("PYTHONHOME", None)
    creationflags = 0x08000000 if os.name == 'nt' else 0

    cmd = [python_exe, capture_script, page_url, "--timeout=180", "--app"]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=clean_env,
            creationflags=creationflags
        )
    except Exception as e:
        return f"Error al abrir navegador: {e}"

    monitor = xbmc.Monitor()
    elapsed = 0
    while proc.poll() is None:
        if monitor.waitForAbort(1):
            proc.terminate()
            break
        elapsed += 1
        if elapsed >= 185:
            proc.terminate()
            break

    stdout, stderr = proc.communicate()
    output_text = stdout.strip()

    try:
        data = json.loads(output_text)
    except Exception:
        return 'Tiempo agotado o ventana cerrada sin capturar stream'

    if data.get("status") != "ok":
        return data.get("message", "Error al capturar el video")

    stream_url = data.get("url")
    headers = data.get("headers", {})
    mime_type = data.get("mime_type", "")

    # Normalizar fragmentos DASH a manifest maestro
    if "/dash/" in stream_url.lower() and not stream_url.lower().endswith(".mpd"):
        clean_base = stream_url.split("?")[0].rsplit("/", 1)[0]
        query = ("?" + stream_url.split("?", 1)[1]) if "?" in stream_url else ""
        stream_url = f"{clean_base}/manifest.mpd{query}"

    ua = headers.get("user-agent") or headers.get("User-Agent") or ""
    ref = headers.get("referer") or headers.get("Referer") or ""
    cookie = headers.get("cookie") or headers.get("Cookie") or ""

    pipe_args = []
    if ua:
        pipe_args.append(f"User-Agent={urllib.parse.quote(ua)}")
    if ref:
        pipe_args.append(f"Referer={urllib.parse.quote(ref)}")
    if cookie:
        pipe_args.append(f"Cookie={urllib.parse.quote(cookie)}")

    final_url = stream_url
    if pipe_args:
        final_url += "|" + "&".join(pipe_args)

    video_urls = [[f"Streamplay [Direct]", final_url]]
    return video_urls
