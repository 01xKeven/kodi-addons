# -*- coding: utf-8 -*-
import sys
import os
import urllib.parse
import xbmc
import xbmcgui

def get_video_url(page_url, url_referer=''):
    xbmc.log(f"[Balandro Streamplay] Resolviendo enlace: {page_url}", xbmc.LOGINFO)

    # Si estamos en busqueda silenciosa, verificacion de fondo o en autoplay, no abrir navegador
    w = xbmcgui.Window(10000)
    if w.getProperty('bridge.silenced_dialogs') == 'true' or w.getProperty('bridge.in_autoplay') == 'true':
        return []

    bridge_addon = os.path.expandvars(r'%APPDATA%\Kodi\addons\plugin.video.bridge.multi')
    lib_path = os.path.join(bridge_addon, 'resources', 'lib')
    if lib_path not in sys.path:
        sys.path.insert(0, lib_path)

    try:
        from bridge_capture import capture_stream
    except Exception as e:
        xbmc.log(f"[Balandro Streamplay] Error importando bridge_capture: {e}", xbmc.LOGERROR)
        return 'Falta el modulo de captura web en Multi Bridge'

    icon_path = os.path.join(bridge_addon, "icon.png") if os.path.exists(os.path.join(bridge_addon, "icon.png")) else ""
    xbmcgui.Dialog().notification(
        "Streamplay",
        "Resuelve el captcha en la ventana flotante...",
        icon_path,
        5000
    )

    p_dialog = xbmcgui.DialogProgress()
    p_dialog.create(
        "Streamplay",
        "Abriendo ventana flotante para resolver captcha...\nResuelve el captcha en la ventana del navegador.\nEl vídeo se reproducirá automáticamente al finalizar."
    )

    monitor = xbmc.Monitor()
    data = capture_stream(
        page_url,
        timeout=180,
        app_mode=True,
        load_adblock=True,
        progress_dialog=p_dialog,
        monitor=monitor
    )

    try:
        p_dialog.close()
    except Exception:
        pass
    xbmc.sleep(120)

    if not data or data.get("status") != "ok":
        msg = data.get("message", "Error al capturar el video") if isinstance(data, dict) else "No se capturó el stream"
        return msg

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
