# -*- coding: utf-8 -*-
import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs
import os
import socket
import urllib.request

def _is_internet_connected(monitor=None):
    if monitor and monitor.abortRequested():
        return False
    # Comprobacion de socket ultra ligero con timeout corto (0.5s) para apagado instantaneo de Kodi
    targets = (
        ('1.1.1.1', 80),
        ('8.8.8.8', 53)
    )
    for host, port in targets:
        if monitor and monitor.abortRequested():
            return False
        try:
            s = socket.create_connection((host, port), timeout=0.5)
            s.close()
            return True
        except Exception:
            continue

    if monitor and monitor.abortRequested():
        return False

    try:
        req = urllib.request.Request(
            'http://connectivitycheck.gstatic.com/generate_204',
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            if resp.status in (200, 204):
                return True
    except Exception:
        pass

    return False


def _show_network_notification(is_online):
    try:
        addon = xbmcaddon.Addon('plugin.video.bridge.multi')
        addon_path = addon.getAddonInfo('path')
        if is_online:
            fn = 'icon_net_online.png'
            msg = "Conexión a Internet restablecida"
            time_ms = 4000
        else:
            fn = 'icon_net_offline.png'
            msg = "Sin conexión a Internet"
            time_ms = 5000

        icon = os.path.join(addon_path, 'resources', 'media', fn)
        if not os.path.exists(icon):
            icon = 'special://home/addons/plugin.video.bridge.multi/resources/media/' + fn

        # 1. Notificacion nativa Builtin de Kodi (universal y limpia)
        try:
            clean_msg = str(msg).replace('"', "'")
            xbmc.executebuiltin('Notification("Multi Bridge", "%s", %d, "%s")' % (clean_msg, time_ms, icon))
        except Exception:
            pass

        # 2. Respaldo nativo estandar con xbmcgui.Dialog().notification
        try:
            xbmcgui.Dialog().notification("Multi Bridge", msg, icon, time_ms)
        except Exception:
            pass

    except Exception as e:
        xbmc.log("BridgeMulti NetworkMonitor notification error: " + str(e), xbmc.LOGWARNING)


def _network_monitor_loop(monitor):
    # Periodo de gracia inicial al arrancar Kodi
    if monitor.waitForAbort(6):
        return

    try:
        addon = xbmcaddon.Addon('plugin.video.bridge.multi')
    except Exception:
        addon = None

    # Estado inicial real
    was_online = _is_internet_connected(monitor)
    xbmc.log('BridgeMulti NetworkMonitor: estado inicial conexion=%s' % was_online, xbmc.LOGINFO)

    # Si arranco sin internet, reconfirmar tras 4s antes de avisar
    if not was_online:
        if monitor.waitForAbort(4):
            return
        was_online = _is_internet_connected(monitor)
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

        now_online = _is_internet_connected(monitor)
        if was_online and not now_online:
            # Reconfirmar tras 2s para evitar falsos positivos por micro-cortes
            if monitor.waitForAbort(2):
                break
            now_online = _is_internet_connected(monitor)
            if not now_online:
                was_online = False
                xbmc.log('BridgeMulti NetworkMonitor: conexion perdida -> notificando', xbmc.LOGINFO)
                _show_network_notification(False)
        elif not was_online and now_online:
            was_online = True
            xbmc.log('BridgeMulti NetworkMonitor: conexion restablecida -> notificando', xbmc.LOGINFO)
            _show_network_notification(True)


import json
import time

class BridgePlayerService(xbmc.Player):
    def onAVStarted(self):
        try:
            w = xbmcgui.Window(10000)
            if w.getProperty('BridgeMulti.HotSwitching') == 'true':
                w.clearProperty('BridgeMulti.HotSwitching')
                xbmc.executebuiltin('Dialog.Close(seekbar,true)')
                xbmc.executebuiltin('Dialog.Close(videoosd,true)')
                xbmc.executebuiltin('Dialog.Close(fullscreeninfo,true)')
            xbmc.sleep(250)
            if self.isPlayingVideo():
                _inject_active_playback_metadata(self)
        except Exception as e:
            xbmc.log("Multi Bridge PlayerService: error en onAVStarted: " + str(e), xbmc.LOGWARNING)

    def onPlayBackStopped(self):
        try:
            xbmcgui.Window(10000).clearProperty('MultiBridge.IsTorrentPlayback')
        except Exception:
            pass

    def onPlayBackEnded(self):
        try:
            xbmcgui.Window(10000).clearProperty('MultiBridge.IsTorrentPlayback')
        except Exception:
            pass

    def onPlayBackError(self):
        try:
            xbmcgui.Window(10000).clearProperty('MultiBridge.IsTorrentPlayback')
        except Exception:
            pass


def _inject_active_playback_metadata(player):
    raw_meta = xbmcgui.Window(10000).getProperty('MultiBridge.ActivePlaybackMeta')
    if not raw_meta:
        return
    try:
        t_str = xbmcgui.Window(10000).getProperty('MultiBridge.ActivePlaybackTime') or '0'
        if time.time() - float(t_str) > 300: # Más de 5 minutos
            return
    except Exception:
        pass

    try:
        meta = json.loads(raw_meta)
    except Exception:
        return

    if not isinstance(meta, dict) or not meta:
        return

    tmdb_id = str(meta.get('tmdb') or '')
    imdb_id = str(meta.get('imdb') or '')
    tvdb_id = str(meta.get('tvdb') or '')
    trakt_id = str(meta.get('trakt') or '')
    s_val = meta.get('season')
    e_val = meta.get('episode')
    is_series = bool(s_val and e_val)

    title_val = str(meta.get('title') or '')
    showname_val = str(meta.get('showname') or '')
    year_val = meta.get('showyear' if is_series else 'year') or meta.get('year')
    plot_val = str(meta.get('plot') or '')
    poster_val = str(meta.get('poster') or meta.get('thumbnail') or '')
    fanart_val = str(meta.get('fanart') or '')
    clearlogo_val = str(meta.get('clearlogo') or '')
    genre_val = str(meta.get('genre') or '')
    director_val = str(meta.get('director') or '')

    # 1. Inyectar directamente en el VideoInfoTag del Player de Kodi (Nexus/Omega 20/21)
    try:
        vt = player.getVideoInfoTag()
        if vt:
            uids = {}
            if tmdb_id:
                uids['tmdb'] = str(tmdb_id)
                if is_series: uids['tvshow.tmdb'] = str(tmdb_id)
            if imdb_id:
                uids['imdb'] = str(imdb_id)
                if is_series: uids['tvshow.imdb'] = str(imdb_id)
            if tvdb_id:
                uids['tvdb'] = str(tvdb_id)
                if is_series: uids['tvshow.tvdb'] = str(tvdb_id)
            if trakt_id:
                uids['trakt'] = str(trakt_id)
            if uids:
                try: vt.setUniqueIDs(uids, 'tmdb' if tmdb_id else ('imdb' if imdb_id else ''))
                except: pass
            if imdb_id:
                try: vt.setIMDbNumber(imdb_id)
                except: pass
            vt.setMediaType('episode' if is_series else 'movie')
            if is_series:
                if showname_val: vt.setTvShowTitle(showname_val)
                if title_val: vt.setTitle(title_val)
                elif showname_val and s_val and e_val:
                    try: vt.setTitle(f"{showname_val} {int(s_val)}x{int(e_val):02d}")
                    except: vt.setTitle(f"{showname_val} {s_val}x{e_val}")
                if s_val:
                    try: vt.setSeason(int(s_val))
                    except: pass
                if e_val:
                    try: vt.setEpisode(int(e_val))
                    except: pass
            else:
                if title_val: vt.setTitle(title_val)
            if year_val:
                try: vt.setYear(int(year_val))
                except: pass
            if plot_val: vt.setPlot(plot_val)
            if genre_val:
                g_list = [g.strip() for g in genre_val.split('/') if g.strip()]
                try: vt.setGenres(g_list)
                except: pass
            if director_val:
                d_list = [d.strip() for d in director_val.split('/') if d.strip()]
                try: vt.setDirectors(d_list)
                except: pass
            xbmc.log("Multi Bridge PlayerService: VideoInfoTag de Kodi inyectado con exito", xbmc.LOGINFO)
    except Exception as _e_vt:
        xbmc.log("Multi Bridge PlayerService: error inyectando VideoInfoTag: " + str(_e_vt), xbmc.LOGINFO)

    # 2. Sincronizar Window(10000) para script.trakt, TMDb Helper y skins
    try:
        w = xbmcgui.Window(10000)
        # Trakt
        tr_ids = {}
        if tmdb_id: tr_ids['tmdb'] = int(tmdb_id) if str(tmdb_id).isdigit() else tmdb_id
        if imdb_id: tr_ids['imdb'] = imdb_id
        if tvdb_id: tr_ids['tvdb'] = int(tvdb_id) if str(tvdb_id).isdigit() else tvdb_id
        if trakt_id: tr_ids['trakt'] = int(trakt_id) if str(trakt_id).isdigit() else trakt_id
        if tr_ids:
            w.setProperty('script.trakt.ids', json.dumps(tr_ids))

        # TMDb Helper PlayerInfoString
        p_dict = {
            'tmdb_type': 'episode' if is_series else 'movie',
            'tmdb_id': int(tmdb_id) if str(tmdb_id).isdigit() else tmdb_id,
        }
        if imdb_id: p_dict['imdb_id'] = imdb_id
        if tvdb_id:
            try: p_dict['tvdb_id'] = int(tvdb_id)
            except: p_dict['tvdb_id'] = tvdb_id
        if is_series:
            try: p_dict['season'] = int(s_val)
            except: p_dict['season'] = str(s_val)
            try: p_dict['episode'] = int(e_val)
            except: p_dict['episode'] = str(e_val)
        w.setProperty('TMDbHelper.PlayerInfoString', json.dumps(p_dict))

        # OSD / Skin labels
        if is_series:
            if showname_val: w.setProperty('VideoPlayer.TVShowTitle', showname_val)
            if s_val: w.setProperty('VideoPlayer.Season', str(s_val))
            if e_val: w.setProperty('VideoPlayer.Episode', str(e_val))
            if title_val: w.setProperty('VideoPlayer.EpisodeName', title_val)
            full_title = f"{showname_val} {int(s_val)}x{int(e_val):02d} - {title_val}" if (showname_val and s_val and e_val and title_val) else (title_val or showname_val)
            w.setProperty('VideoPlayer.Title', full_title)
        else:
            if title_val: w.setProperty('VideoPlayer.Title', title_val)

        if year_val: w.setProperty('VideoPlayer.Year', str(year_val))
        if plot_val: w.setProperty('VideoPlayer.Plot', plot_val)
        if poster_val:
            w.setProperty('VideoPlayer.Cover', poster_val)
            w.setProperty('Player.Art(poster)', poster_val)
            w.setProperty('Player.Art(thumb)', poster_val)
            w.setProperty('TMDbHelper.Player.Thumb', poster_val)
        if fanart_val:
            w.setProperty('Player.Art(fanart)', fanart_val)
        if clearlogo_val:
            w.setProperty('Player.Art(clearlogo)', clearlogo_val)
            w.setProperty('TMDbHelper.ListItem.ClearLogo', clearlogo_val)
            w.setProperty('TMDbHelper.Player.ClearLogo', clearlogo_val)
        if genre_val:
            w.setProperty('VideoPlayer.Genre', genre_val)
            w.setProperty('TMDbHelper.ListItem.Genre', genre_val)
        if director_val:
            w.setProperty('VideoPlayer.Director', director_val)
            w.setProperty('TMDbHelper.ListItem.Director', director_val)
    except Exception as _e_win:
        xbmc.log("Multi Bridge PlayerService: error configurando Window properties: " + str(_e_win), xbmc.LOGINFO)


def check_first_run_device_service(monitor=None):
    try:
        addon = xbmcaddon.Addon('plugin.video.bridge.multi')
        if addon.getSetting('first_install_setup_done') == 'true':
            return
        addon.setSetting('hide_captcha_links', 'false')
        addon.setSetting('first_install_setup_done', 'true')
        xbmc.log("Multi Bridge Service: Configuracion inicial automatica completada (Captchas activos)", xbmc.LOGINFO)
    except Exception as e:
        xbmc.log("Multi Bridge Service: Error en check_first_run_device_service: " + str(e), xbmc.LOGWARNING)


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
        if hasattr(default, 'sync_captcha_servers'):
            default.sync_captcha_servers()
    except Exception as e:
        xbmc.log("Multi Bridge Service: Error running startup migration: " + str(e), xbmc.LOGWARNING)

    # Mantener vivo el listener de eventos de reproduccion en el servicio 24/7
    player_service = BridgePlayerService()

    # Iniciar monitor de conexion a internet en hilo daemon (cierre inmediato de Kodi en 0s)
    import threading
    monitor = xbmc.Monitor()
    t_net = threading.Thread(target=_network_monitor_loop, args=(monitor,), daemon=True)
    t_net.name = "BridgeMultiNetworkMonitor"
    t_net.start()

    # Iniciar comprobacion de configuracion inicial de dispositivo (PC vs Android)
    t_dev = threading.Thread(target=check_first_run_device_service, args=(monitor,), daemon=True)
    t_dev.name = "BridgeMultiFirstRunDevice"
    t_dev.start()

    _sync_timer = 0
    while not monitor.abortRequested():
        if monitor.waitForAbort(0.25):
            break
        _sync_timer += 0.25
        if _sync_timer >= 600:
            _sync_timer = 0
            try:
                if hasattr(default, 'sync_captcha_servers'):
                    default.sync_captcha_servers()
            except Exception:
                pass

    # Cierre limpio e inmediato al apagar Kodi
    try:
        if hasattr(default, '_SHUTDOWN_REQUESTED'):
            default._SHUTDOWN_REQUESTED = True
        if hasattr(default, '_BG_TASK_ABORT_EVENT'):
            default._BG_TASK_ABORT_EVENT.set()
        if hasattr(default, '_stop_shutdown_watchdog'):
            default._stop_shutdown_watchdog(timeout=0.4)
        if hasattr(default, 'abort_all_network_sockets'):
            default.abort_all_network_sockets()
    except Exception:
        pass
    try:
        if t_net and t_net.is_alive():
            t_net.join(timeout=0.8)
        if t_dev and t_dev.is_alive():
            t_dev.join(timeout=0.4)
    except Exception:
        pass
    try:
        if hasattr(default, '_bridge_addon'):
            default._bridge_addon = None
        del default
    except Exception:
        pass
    try:
        del player_service
        del monitor
    except Exception:
        pass


if __name__ == '__main__':
    run_service()
