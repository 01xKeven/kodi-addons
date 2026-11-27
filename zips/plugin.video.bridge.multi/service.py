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
    def __init__(self, *args, default_mod_ref=None, **kwargs):
        super(BridgePlayerService, self).__init__(*args, **kwargs)
        self._torrent_start_ts = 0.0
        self._default_mod = default_mod_ref

    def _get_default_mod(self):
        ref = getattr(self, '_default_mod', None)
        if isinstance(ref, list) and ref and ref[0] is not None:
            return ref[0]
        return None

    def _prewarm_torrent_pause_cache(self):
        try:
            _def = self._get_default_mod()
            if _def and hasattr(_def, '_load_cached_links_for_dialog'):
                _, _, _cm, _ = _def._load_cached_links_for_dialog()
                if isinstance(_cm, dict) and _cm.get('season') and _cm.get('episode') and hasattr(_def, '_get_next_episode_info'):
                    _def._get_next_episode_info(_cm)
        except Exception:
            pass

    def _close_startup_overlays(self, include_seekbar=True):
        try:
            xbmc.executebuiltin('Dialog.Close(busydialog,true)')
            xbmc.executebuiltin('Dialog.Close(busydialognocancel,true)')
            xbmc.executebuiltin('Dialog.Close(progressdialog,true)')
            if include_seekbar:
                xbmc.executebuiltin('Dialog.Close(seekbar,true)')
                xbmc.executebuiltin('Dialog.Close(videoosd,true)')
                xbmc.executebuiltin('Dialog.Close(fullscreeninfo,true)')
        except Exception:
            pass

    def onPlayBackStarted(self):
        try:
            now_ts = time.time()
            self._torrent_start_ts = now_ts
            self._last_start_ts = now_ts
            w = xbmcgui.Window(10000)
            if w.getProperty('BridgeMulti.HotSwitching') == 'true':
                self._hot_switch_ts = now_ts
            # Invalidar cache en RAM del modulo residente antes de pre-calentar nueva pelicula/episodio
            _def = self._get_default_mod()
            if _def is not None:
                try:
                    _def._RAM_SEARCH_CACHE = None
                    _def._LAST_SEARCH_CACHE_DATA = None
                    _def._LAST_SEARCH_CACHE_MTIME = 0
                except Exception:
                    pass
            if self.isPlaying():
                _inject_active_playback_metadata(self)
        except Exception as e:
            xbmc.log("Multi Bridge PlayerService: error en onPlayBackStarted: " + str(e), xbmc.LOGWARNING)

    def onAVStarted(self):
        try:
            now_ts = time.time()
            self._torrent_start_ts = now_ts
            w = xbmcgui.Window(10000)
            is_hot = (w.getProperty('BridgeMulti.HotSwitching') == 'true') or ((now_ts - getattr(self, '_hot_switch_ts', 0.0)) < 12.0)
            is_tor = (w.getProperty('MultiBridge.IsTorrentPlayback') == 'true')
            if w.getProperty('BridgeMulti.HotSwitching') == 'true':
                self._hot_switch_ts = now_ts
                w.clearProperty('BridgeMulti.HotSwitching')
            if is_hot or is_tor:
                self._close_startup_overlays(include_seekbar=True)
            if self.isPlayingVideo() or self.isPlaying():
                _has_ids = False
                try:
                    _cur_vt = self.getVideoInfoTag()
                    if _cur_vt and (_cur_vt.getUniqueID('tmdb') or _cur_vt.getUniqueID('imdb') or _cur_vt.getIMDBNumber()):
                        _has_ids = True
                except Exception:
                    pass
                # Evitar re-llamar updateInfoTag si onPlayBackStarted ya inyecto IDs (previene parpadeo de SeekBar/subtitulos arriba)
                _inject_active_playback_metadata(self, properties_only=_has_ids)
            if is_tor and self._get_default_mod() is not None:
                import threading
                threading.Thread(target=self._prewarm_torrent_pause_cache, daemon=True, name="BridgeTorrentPauseWarm").start()
            # Re-asegurar script.trakt.ids y TagVideo antes de que script.trakt / monitores lean tarde
            xbmc.sleep(500)
            if self.isPlayingVideo():
                _need_tag = False
                try:
                    _cur_vt = self.getVideoInfoTag()
                    if _cur_vt and not (_cur_vt.getUniqueID('tmdb') or _cur_vt.getUniqueID('imdb') or _cur_vt.getIMDBNumber()):
                        _need_tag = True
                except Exception:
                    pass
                _inject_active_playback_metadata(self, properties_only=(not _need_tag))
                if (is_hot or is_tor) and not xbmc.getCondVisibility("Player.Paused"):
                    self._close_startup_overlays(include_seekbar=True)
        except Exception as e:
            xbmc.log("Multi Bridge PlayerService: error en onAVStarted: " + str(e), xbmc.LOGWARNING)

    def onPlayBackSeek(self, seek_time=0, seekOffset=0):
        try:
            now_ts = time.time()
            is_startup_or_hot = ((now_ts - getattr(self, '_last_start_ts', 0.0)) < 10.0) or ((now_ts - getattr(self, '_hot_switch_ts', 0.0)) < 12.0)
            if not is_startup_or_hot:
                return
            if not xbmc.getCondVisibility("Player.Paused"):
                self._close_startup_overlays(include_seekbar=True)
                import threading
                def _clear_seekbar_later():
                    try:
                        for delay in (0.35, 1.0, 2.0):
                            xbmc.sleep(int(delay * 1000))
                            if not self.isPlayingVideo() or xbmc.getCondVisibility("Player.Paused"):
                                break
                            if not xbmc.getCondVisibility("Player.HasPerformedSeek(3)"):
                                self._close_startup_overlays(include_seekbar=True)
                                break
                            self._close_startup_overlays(include_seekbar=True)
                    except Exception:
                        pass
                threading.Thread(target=_clear_seekbar_later, daemon=True, name="BridgeSeekOverlayClear").start()
        except Exception:
            pass

    def onPlayBackResumed(self):
        try:
            w = xbmcgui.Window(10000)
            res_ts = 0.0
            try:
                res_ts = float(w.getProperty('BridgeMulti.JustResumedFromPrompt') or '0')
            except Exception:
                res_ts = 0.0
            is_recent = ((time.time() - res_ts) < 3.5) or ((time.time() - getattr(self, '_hot_switch_ts', 0.0)) < 12.0)
            if is_recent and not xbmc.getCondVisibility("Player.Paused"):
                xbmc.sleep(60)
                self._close_startup_overlays(include_seekbar=True)
        except Exception:
            pass

    def onPlayBackPaused(self):
        try:
            w = xbmcgui.Window(10000)
            if w.getProperty('MultiBridge.IsTorrentPlayback') != 'true':
                return
            if not self.isPlayingVideo():
                return
            if (time.time() - getattr(self, '_torrent_start_ts', 0.0)) < 1.5:
                return
            if xbmc.getCondVisibility("Player.Buffering"):
                return
            try:
                if (self.getTime() or 0.0) < 0.5:
                    return
            except Exception:
                return
            if w.getProperty('BridgeMulti.DialogActive') == 'true':
                return
            try:
                if time.time() < float(w.getProperty('BridgeMulti.PauseCooldownTs') or '0'):
                    return
            except Exception:
                pass
            pause_setting = str(xbmcaddon.Addon('plugin.video.bridge.multi').getSetting('live_switch_on_pause') or '').strip().lower()
            if pause_setting in ('false', '2'):
                return
            w.setProperty('BridgeMulti.PauseCooldownTs', str(time.time() + 2.0))
            _def = self._get_default_mod()
            if _def is not None and hasattr(_def, '_run_pause_prompt_flow'):
                try:
                    _cur_t = float(self.getTime() or 0.0)
                except Exception:
                    _cur_t = 0.0
                _m_key = w.getProperty('BridgeMulti.CurrentMediaKey') or ''
                try:
                    _a_idx_s = w.getProperty('BridgeMulti.CurrentPlayingIndex')
                    _a_idx = int(_a_idx_s) if (_a_idx_s and _a_idx_s.lstrip('-').isdigit()) else -1
                except Exception:
                    _a_idx = -1
                import threading
                def _run_instant_pause(d_mod=_def, mk=_m_key, idx_v=_a_idx, ct=_cur_t):
                    try:
                        # SOLO UI en memoria dentro de service.py: cualquier reproducción o monitor
                        # posterior se despacha al CPythonInvoker propio de default.py vía RunPlugin
                        # para evitar colisiones PyContext / crashes en Player::play al cambiar en caliente.
                        d_mod._run_pause_prompt_flow(media_key=mk, active_idx=idx_v, cur_time=ct, monitor_token=None, ui_only=True)
                    except Exception as _pe:
                        xbmc.log("Multi Bridge PlayerService: error en pause instantaneo: " + str(_pe), xbmc.LOGWARNING)
                xbmc.log("Multi Bridge PlayerService: pausa detectada en torrent -> popup instantaneo en memoria", xbmc.LOGINFO)
                threading.Thread(target=_run_instant_pause, daemon=True, name="BridgeInstantPause").start()
            else:
                xbmc.log("Multi Bridge PlayerService: pausa detectada en torrent -> abriendo popup via RunPlugin", xbmc.LOGINFO)
                xbmc.executebuiltin('RunPlugin(plugin://plugin.video.bridge.multi/?action=pause_prompt)')
        except Exception as e:
            xbmc.log("Multi Bridge PlayerService: error en onPlayBackPaused: " + str(e), xbmc.LOGWARNING)

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


def _inject_active_playback_metadata(player, properties_only=False):
    raw_meta = xbmcgui.Window(10000).getProperty('MultiBridge.ActivePlaybackMeta')
    if not raw_meta:
        return
    try:
        t_str = xbmcgui.Window(10000).getProperty('MultiBridge.ActivePlaybackTime') or '0'
        if time.time() - float(t_str) > 86400:
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
    tagline_val = str(meta.get('tagline') or '')
    poster_val = str(meta.get('poster') or meta.get('thumbnail') or '')
    thumb_val = str(meta.get('thumbnail') or poster_val or '')
    fanart_val = str(meta.get('fanart') or '')
    clearlogo_val = str(meta.get('clearlogo') or '')
    genre_val = str(meta.get('genre') or '')
    director_val = str(meta.get('director') or '')

    # 1. Inyectar directamente en el VideoInfoTag del Player de Kodi (Nexus/Omega 20/21+)
    # En Kodi 20+, getVideoInfoTag() retorna una copia: player.updateInfoTag(li) aplica los cambios reales.
    if not properties_only:
        try:
            has_update_tag = hasattr(player, 'updateInfoTag')
            li = None
            if has_update_tag:
                try:
                    if hasattr(player, 'getPlayingItem'):
                        li = player.getPlayingItem()
                except Exception:
                    li = None
                try:
                    pf = player.getPlayingFile() or ''
                except Exception:
                    pf = ''
                if li is None:
                    li = xbmcgui.ListItem(path=pf, offscreen=True)
                else:
                    try:
                        if not li.getPath() and pf:
                            li.setPath(pf)
                    except Exception:
                        pass
            if li is not None:
                if title_val:
                    try:
                        lbl = f"{showname_val} {int(s_val)}x{int(e_val):02d} - {title_val}" if (is_series and showname_val and s_val and e_val) else title_val
                        li.setLabel(lbl)
                    except Exception:
                        pass
                art_dict = {}
                if poster_val: art_dict['poster'] = poster_val
                if thumb_val: art_dict['thumb'] = thumb_val
                if fanart_val: art_dict['fanart'] = fanart_val
                if clearlogo_val: art_dict['clearlogo'] = clearlogo_val
                if poster_val or thumb_val: art_dict['icon'] = poster_val or thumb_val
                if art_dict:
                    try: li.setArt(art_dict)
                    except Exception: pass
                vt = li.getVideoInfoTag()
            else:
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
                    except Exception: pass
                if imdb_id:
                    try: vt.setIMDBNumber(imdb_id)
                    except Exception: pass
                vt.setMediaType('episode' if is_series else 'movie')
                if is_series:
                    if showname_val: vt.setTvShowTitle(showname_val)
                    if title_val: vt.setTitle(title_val)
                    elif showname_val and s_val and e_val:
                        try: vt.setTitle(f"{showname_val} {int(s_val)}x{int(e_val):02d}")
                        except Exception: vt.setTitle(f"{showname_val} {s_val}x{e_val}")
                    if s_val:
                        try: vt.setSeason(int(s_val))
                        except Exception: pass
                    if e_val:
                        try: vt.setEpisode(int(e_val))
                        except Exception: pass
                else:
                    if title_val: vt.setTitle(title_val)
                if year_val:
                    try: vt.setYear(int(year_val))
                    except Exception: pass
                if plot_val: vt.setPlot(plot_val)
                if tagline_val:
                    try: vt.setTagLine(tagline_val)
                    except Exception: pass
                if genre_val:
                    g_list = [g.strip() for g in genre_val.split('/') if g.strip()]
                    try: vt.setGenres(g_list)
                    except Exception: pass
                if director_val:
                    d_list = [d.strip() for d in director_val.split('/') if d.strip()]
                    try: vt.setDirectors(d_list)
                    except Exception: pass
                if li is not None and has_update_tag:
                    player.updateInfoTag(li)
                xbmc.log("Multi Bridge PlayerService: VideoInfoTag de Kodi inyectado con exito (tmdb=%s imdb=%s)" % (tmdb_id, imdb_id), xbmc.LOGINFO)
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


def _is_addon_installed_and_enabled(addon_id):
    """Comprueba de forma pasiva si un complemento existe y está habilitado en Kodi."""
    try:
        if xbmc.getCondVisibility('System.HasAddon("%s")' % addon_id):
            return True, True
    except Exception:
        pass
    try:
        req = json.dumps({
            "jsonrpc": "2.0",
            "method": "Addons.GetAddonDetails",
            "params": {"addonid": addon_id, "properties": ["enabled"]},
            "id": 1
        })
        raw = xbmc.executeJSONRPC(req)
        data = json.loads(raw) if raw else {}
        addon_info = (data.get('result') or {}).get('addon')
        if isinstance(addon_info, dict) and addon_info:
            return True, bool(addon_info.get('enabled', True))
    except Exception:
        pass
    return False, False


def _enable_installed_addon(addon_id):
    """Habilita silenciosamente un complemento ya instalado que estuviera desactivado."""
    try:
        req = json.dumps({
            "jsonrpc": "2.0",
            "method": "Addons.SetAddonEnabled",
            "params": {"addonid": addon_id, "enabled": True},
            "id": 1
        })
        raw = xbmc.executeJSONRPC(req)
        data = json.loads(raw) if raw else {}
        return 'result' in data
    except Exception:
        return False


def _wait_native_install_cycle(addon_id, monitor, max_wait=45.0):
    """Espera de forma pasiva hasta que termine la instalación nativa o el usuario cierre el diálogo."""
    # Dar margen inicial para que aparezca la ventana nativa de confirmación/progreso de Kodi
    if monitor.waitForAbort(2.5):
        return False
    elapsed = 2.5
    modal_cond = (
        "Window.IsVisible(yesnodialog) | Window.IsVisible(progressdialog) | "
        "Window.IsVisible(addoninformation) | Window.IsVisible(extendedprogressdialog)"
    )
    while elapsed < max_wait and not monitor.abortRequested():
        try:
            if xbmc.getCondVisibility('System.HasAddon("%s")' % addon_id):
                return True
            if not xbmc.getCondVisibility(modal_cond):
                break
        except Exception:
            break
        if monitor.waitForAbort(0.5):
            return False
        elapsed += 0.5
    try:
        return bool(xbmc.getCondVisibility('System.HasAddon("%s")' % addon_id))
    except Exception:
        return False


def _check_inputstream_addons_startup(monitor):
    """Revisa pasiva y calmadamente al iniciar Kodi (tras esperar a que cargue todo)
    si inputstream.adaptive e inputstream.ffmpegdirect están instalados. Si falta alguno,
    abre directamente la ventana nativa de instalación de Kodi para ese complemento."""
    # Esperar ~15s tras arrancar Kodi para no competir con la carga de skin, widgets y servicios
    if monitor.waitForAbort(15):
        return

    # Si Kodi sigue en pantalla de arranque o cargando ocupadamente, esperar con calma unos segundos extra
    for _ in range(10):
        if monitor.abortRequested():
            return
        try:
            if not xbmc.getCondVisibility('Window.IsVisible(startup) | Window.IsActive(startup)'):
                break
        except Exception:
            break
        if monitor.waitForAbort(1.5):
            return

    required_addons = (
        'inputstream.adaptive',
        'inputstream.ffmpegdirect',
    )
    missing_addons = []

    for addon_id in required_addons:
        if monitor.abortRequested():
            return
        installed, enabled = _is_addon_installed_and_enabled(addon_id)
        if installed and enabled:
            continue
        if installed and not enabled:
            xbmc.log("Multi Bridge Service: %s estaba deshabilitado -> habilitando..." % addon_id, xbmc.LOGINFO)
            if _enable_installed_addon(addon_id):
                if monitor.waitForAbort(0.5):
                    return
                if xbmc.getCondVisibility('System.HasAddon("%s")' % addon_id):
                    continue
        missing_addons.append(addon_id)
        if monitor.waitForAbort(0.25):
            return

    if not missing_addons:
        xbmc.log("Multi Bridge Service: InputStream Adaptive y FFmpeg Direct verificados OK.", xbmc.LOGINFO)
        return

    for idx, addon_id in enumerate(missing_addons):
        if monitor.abortRequested():
            return
        if idx > 0:
            if monitor.waitForAbort(1.5):
                return
        # Esperar calmadamente si el usuario está reproduciendo vídeo o con otro diálogo modal abierto
        for _ in range(20):
            if monitor.abortRequested():
                return
            try:
                if not xbmc.getCondVisibility('Player.HasMedia | Window.IsVisible(yesnodialog) | Window.IsVisible(okdialog)'):
                    break
            except Exception:
                break
            if monitor.waitForAbort(1.5):
                return
        if monitor.abortRequested():
            return
        try:
            xbmc.log("Multi Bridge Service: falta '%s' -> abriendo ventana nativa InstallAddon(%s)" % (addon_id, addon_id), xbmc.LOGINFO)
            xbmc.executebuiltin('InstallAddon(%s)' % addon_id)
        except Exception as e:
            xbmc.log("Multi Bridge Service: error lanzando InstallAddon(%s): %s" % (addon_id, e), xbmc.LOGWARNING)
            continue
        _wait_native_install_cycle(addon_id, monitor, max_wait=45.0)


def check_first_run_device_service(monitor=None):
    try:
        if monitor and monitor.abortRequested():
            return
        addon = xbmcaddon.Addon()
        if addon.getSetting('first_install_setup_done') == 'true':
            return
        addon.setSetting('hide_captcha_links', 'false')
        addon.setSetting('first_install_setup_done', 'true')
        xbmc.log("Multi Bridge Service: Configuracion inicial automatica completada (Captchas activos)", xbmc.LOGINFO)
    except Exception as e:
        xbmc.log("Multi Bridge Service: Error en check_first_run_device_service: " + str(e), xbmc.LOGWARNING)


def run_service():
    monitor = xbmc.Monitor()

    # Limpieza inicial de variables y locks de busqueda
    try:
        xbmcgui.Window(10000).setProperty('BridgeMulti.SearchProcessRunning', 'false')
        xbmcgui.Window(10000).clearProperty('BridgeMulti.SearchProcessStartTime')
        xbmcgui.Window(10000).clearProperty('BridgeMulti.SearchProcessTitle')
        b_path = xbmcvfs.translatePath('special://userdata/addon_data/plugin.video.bridge.multi/')
        l_file = os.path.join(b_path, 'bridge_search.lock')
        if os.path.exists(l_file):
            os.remove(l_file)
        import shutil
        for _prof in ('test_profile', 'web_profile'):
            _p_dir = os.path.join(b_path, _prof)
            if os.path.isdir(_p_dir):
                shutil.rmtree(_p_dir, ignore_errors=True)
    except Exception:
        pass

    # Mantener vivo el listener de eventos de reproduccion en el servicio 24/7
    import threading
    _default_mod = [None]
    player_service = BridgePlayerService(default_mod_ref=_default_mod)

    def _run_startup_tasks(mon):
        if mon.waitForAbort(2):
            return
        try:
            import default as _def
            _default_mod[0] = _def
            if mon.abortRequested():
                return
            if hasattr(_def, 'check_and_run_migration'):
                _def.check_and_run_migration()
            if mon.abortRequested():
                return
            if hasattr(_def, 'check_orphan_players'):
                _def.check_orphan_players()
            if mon.abortRequested():
                return
            if hasattr(_def, 'sync_captcha_servers'):
                _def.sync_captcha_servers()
        except Exception as e:
            xbmc.log("Multi Bridge Service: Error running startup migration: " + str(e), xbmc.LOGWARNING)

    t_init = threading.Thread(target=_run_startup_tasks, args=(monitor,), daemon=True, name="BridgeMultiStartupInit")
    t_init.start()

    # Iniciar monitor de conexion a internet en hilo daemon (cierre inmediato de Kodi en 0s)
    t_net = threading.Thread(target=_network_monitor_loop, args=(monitor,), daemon=True)
    t_net.name = "BridgeMultiNetworkMonitor"
    t_net.start()

    # Iniciar comprobacion de configuracion inicial de dispositivo (PC vs Android)
    t_dev = threading.Thread(target=check_first_run_device_service, args=(monitor,), daemon=True)
    t_dev.name = "BridgeMultiFirstRunDevice"
    t_dev.start()

    # Iniciar verificacion pasiva y retardada de InputStream Adaptive y FFmpeg Direct
    t_inp = threading.Thread(target=_check_inputstream_addons_startup, args=(monitor,), daemon=True)
    t_inp.name = "BridgeMultiInputStreamCheck"
    t_inp.start()

    _sync_timer = 0
    while not monitor.abortRequested():
        if monitor.waitForAbort(0.25):
            break
        _sync_timer += 0.25
        if _sync_timer >= 600:
            _sync_timer = 0
            try:
                _def = _default_mod[0]
                if _def and hasattr(_def, 'sync_captcha_servers'):
                    _def.sync_captcha_servers()
            except Exception:
                pass

    # Cierre limpio e inmediato al apagar Kodi
    try:
        _def = _default_mod[0]
        if _def:
            if hasattr(_def, '_SHUTDOWN_REQUESTED'):
                _def._SHUTDOWN_REQUESTED = True
            if hasattr(_def, '_BG_TASK_ABORT_EVENT'):
                _def._BG_TASK_ABORT_EVENT.set()
            if hasattr(_def, '_stop_shutdown_watchdog'):
                _def._stop_shutdown_watchdog(timeout=0.4)
            if hasattr(_def, 'abort_all_network_sockets'):
                _def.abort_all_network_sockets()
            if hasattr(_def, '_bridge_addon'):
                _def._bridge_addon = None
    except Exception:
        pass
    try:
        for _th, _tmo in ((t_net, 0.6), (t_dev, 0.3), (t_inp, 0.3), (t_init, 0.6)):
            if _th and _th.is_alive():
                _th.join(timeout=_tmo)
    except Exception:
        pass
    try:
        _default_mod[0] = None
        del player_service
    except Exception:
        pass


if __name__ == '__main__':
    run_service()
