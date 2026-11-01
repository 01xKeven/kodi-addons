# -*- coding: utf-8 -*-
import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs
import os
import socket

def _is_internet_connected():
    # 1. Comprobacion local rapida de Kodi
    try:
        if not xbmc.getCondVisibility("Network.IsConnected"):
            return False
    except Exception:
        pass

    # 2. Comprobacion de socket ligero (TCP SYN/ACK a servidores Anycast globales)
    targets = (('1.1.1.1', 80), ('8.8.8.8', 53), ('1.0.0.1', 80), ('8.8.4.4', 53))
    for host, port in targets:
        try:
            s = socket.create_connection((host, port), timeout=2.0)
            s.close()
            return True
        except Exception:
            continue
    return False

def _show_network_notification(is_online):
    try:
        addon = xbmcaddon.Addon('plugin.video.bridge.multi')
        addon_path = addon.getAddonInfo('path')
        if is_online:
            icon = os.path.join(addon_path, 'resources', 'media', 'icon_net_online.png')
            msg = "Conexión a Internet restablecida"
            time_ms = 4000
        else:
            icon = os.path.join(addon_path, 'resources', 'media', 'icon_net_offline.png')
            msg = "Sin conexión a Internet"
            time_ms = 5000
        
        if not os.path.exists(icon):
            fn = 'icon_net_online.png' if is_online else 'icon_net_offline.png'
            icon = 'special://home/addons/plugin.video.bridge.multi/resources/media/' + fn

        xbmcgui.Dialog().notification("Multi Bridge", msg, icon, time_ms)
    except Exception as e:
        xbmc.log("BridgeMulti NetworkMonitor notification error: " + str(e), xbmc.LOGWARNING)

def _network_monitor_loop(monitor):
    # Periodo de gracia inicial (esperar 8s tras inicio de Kodi para que el sistema y Wi-Fi conecten)
    if monitor.waitForAbort(8):
        return

    try:
        addon = xbmcaddon.Addon('plugin.video.bridge.multi')
    except Exception:
        addon = None

    was_online = _is_internet_connected()
    if not was_online and addon and (addon.getSetting('notify_network_status') != 'false'):
        _show_network_notification(False)

    while not monitor.abortRequested():
        sleep_sec = 6 if was_online else 3
        if monitor.waitForAbort(sleep_sec):
            break

        if addon:
            try:
                if addon.getSetting('notify_network_status') == 'false':
                    continue
            except Exception:
                pass

        now_online = _is_internet_connected()
        if was_online and not now_online:
            # Reconfirmar tras 1.5s para evitar falsos positivos por micro-cortes transitorios
            if monitor.waitForAbort(1.5):
                break
            now_online = _is_internet_connected()
            if not now_online:
                was_online = False
                _show_network_notification(False)
        elif not was_online and now_online:
            was_online = True
            _show_network_notification(True)

def run_service():
    # Limpieza inicial de variables y locks de busqueda
    try:
        xbmcgui.Window(10000).setProperty('BridgeMulti.SearchProcessRunning', 'false')
        xbmcgui.Window(10000).clearProperty('BridgeMulti.SearchProcessStartTime')
        xbmcgui.Window(10000).clearProperty('BridgeMulti.SearchProcessTitle')
        b_path = xbmcvfs.translatePath('special://userdata/addon_data/plugin.video.bridge.multi/')
        l_file = os.path.join(b_path, 'bridge_search.lock')
        if os.path.exists(l_file):
            os.remove(l_file)
    except Exception:
        pass

    try:
        import default
        if hasattr(default, 'check_and_run_migration'):
            default.check_and_run_migration()
        if hasattr(default, 'check_orphan_players'):
            default.check_orphan_players()
    except Exception as e:
        xbmc.log("Multi Bridge Service: Error running startup migration: " + str(e), xbmc.LOGWARNING)

    # Iniciar monitor de conexion a internet continuo
    monitor = xbmc.Monitor()
    _network_monitor_loop(monitor)

if __name__ == '__main__':
    run_service()
