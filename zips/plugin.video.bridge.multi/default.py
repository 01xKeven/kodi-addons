import sqlite3
# -*- coding: utf-8 -*-
import sys
import os
import re
import json
import base64
import threading
import time
import datetime
import random
import socket
import builtins
import types

builtins.sys = sys
builtins.basestring = str
builtins.unicode = str
# Timeout global de sockets: ninguna operacion de red puede colgarse mas de 10s
# en la capa del SO (antes 30s). httptools impone sus propios timeouts (15s
# por defecto); este es el techo de seguridad para sockets crudos (TMDB, etc.).
# Causa raiz del freeze de series: pelispanda.episodios() tardaba 100-150s en
# un regex catastrofico reteniendo el GIL; ya corregido en el canal via JSON.
socket.setdefaulttimeout(10)

import urllib
import urllib.parse as uparse
import urllib.request as urequest
import urllib.response as uresponse
import urllib.error as uerror
import html, html.parser, html.entities
import http.cookiejar
from contextlib import contextmanager

urllib.quote = uparse.quote
urllib.quote_plus = uparse.quote_plus
urllib.unquote = uparse.unquote
urllib.unquote_plus = uparse.unquote_plus
urllib.urlencode = uparse.urlencode
urllib.addinfourl = uresponse.addinfourl
urllib.request = urequest
urllib.response = uresponse

urequest.HTTPError = uerror.HTTPError
urequest.URLError = uerror.URLError
sys.modules['urllib2'] = urequest

html.parser.HTMLParser.unescape = staticmethod(html.unescape)
html_parser_mod = types.ModuleType('HTMLParser')
html_parser_mod.HTMLParser = html.parser.HTMLParser
sys.modules['HTMLParser'] = html_parser_mod
sys.modules['htmlentitydefs'] = html.entities
sys.modules['urlparse'] = uparse
sys.modules['cookielib'] = http.cookiejar

handle = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1] else -1
original_argv = list(sys.argv)

import xbmc
import xbmcaddon
import xbmcvfs
import xbmcplugin
import xbmcgui

# Guardar la clase Dialog original de Kodi de forma inmutable para diálogos interactivos seguros
_KODI_ORIG_DIALOG = xbmcgui.Dialog

if not hasattr(xbmc, 'translatePath'):
    xbmc.translatePath = xbmcvfs.translatePath

_bridge_addon = xbmcaddon.Addon('plugin.video.bridge.multi')

def _show_bridge_notification(heading, message, time_ms=3500):
    """Muestra una notificación en pantalla de forma infalible en cualquier dispositivo y skin,
    utilizando el icono oficial del addon y Kodi Builtin + _KODI_ORIG_DIALOG."""
    try:
        icon_path = ''
        try:
            if '_bridge_addon' in globals() and _bridge_addon:
                icon_path = _bridge_addon.getAddonInfo('icon') or ''
        except Exception:
            pass
        if not icon_path:
            try: icon_path = xbmcgui.NOTIFICATION_INFO
            except Exception: icon_path = ''

        clean_h = str(heading).replace('"', "'").replace('\n', ' ')
        clean_m = str(message).replace('"', "'").replace('\n', ' ')

        # 1. Builtin Notification nativo de Kodi (C++, universal y no afectado por _SilentDialog)
        try:
            if icon_path:
                xbmc.executebuiltin('Notification("%s", "%s", %d, "%s")' % (clean_h, clean_m, time_ms, icon_path))
            else:
                xbmc.executebuiltin('Notification("%s", "%s", %d)' % (clean_h, clean_m, time_ms))
        except Exception:
            pass

        # 2. Respaldo directo con _KODI_ORIG_DIALOG
        try:
            dlg_cls = _KODI_ORIG_DIALOG or getattr(xbmcgui, 'Dialog')
            if dlg_cls and (not ('_SilentDialog' in globals()) or dlg_cls is not _SilentDialog):
                dlg_cls().notification(clean_h, clean_m, icon_path or xbmcgui.NOTIFICATION_INFO, time_ms)
        except Exception:
            pass
    except Exception:
        pass

# Auto-load all script.module dependencies from Kodi addons directory
addons_root = xbmcvfs.translatePath('special://home/addons/')
alfa_path = os.path.join(addons_root, 'plugin.video.alfa')
_alfa_lib = os.path.join(alfa_path, 'lib')
balandro_path = os.path.join(addons_root, 'plugin.video.balandro')
_balandro_lib = os.path.join(balandro_path, 'lib')

if os.path.exists(addons_root):
    for _ad in os.listdir(addons_root):
        if _ad.startswith('script.module.'):
            _mod_lib = os.path.join(addons_root, _ad, 'lib')
            if os.path.isdir(_mod_lib):
                if _mod_lib not in sys.path: sys.path.append(_mod_lib)
            else:
                _ad_path = os.path.join(addons_root, _ad)
                if os.path.isdir(_ad_path) and _ad_path not in sys.path: sys.path.append(_ad_path)

TMDB_PLAYERS_PATH = xbmcvfs.translatePath('special://userdata/addon_data/plugin.video.themoviedb.helper/players/')
BRIDGE_DATA_PATH = xbmcvfs.translatePath('special://userdata/addon_data/plugin.video.bridge.multi/')
if not os.path.exists(BRIDGE_DATA_PATH):
    try: os.makedirs(BRIDGE_DATA_PATH)
    except: pass
if not os.path.exists(TMDB_PLAYERS_PATH):
    try: os.makedirs(TMDB_PLAYERS_PATH)
    except: pass

SEARCH_CACHE_FILE = os.path.join(BRIDGE_DATA_PATH, 'bridge_multi_search_cache.json')
BOOKMARKS_FILE = os.path.join(BRIDGE_DATA_PATH, 'bridge_multi_bookmarks.json')
CONTINUE_WATCHING_FILE = os.path.join(BRIDGE_DATA_PATH, 'bridge_multi_continue_watching.json')
CLOUD_SYNC_FILE = os.path.join(BRIDGE_DATA_PATH, 'cloud_sync_info.json')
CHANNELS_JSON_FILE = os.path.join(BRIDGE_DATA_PATH, 'channels.json')

_SEARCH_CACHE_LOCK = threading.Lock()

def _norm_id_str(val):
    if val is None: return ''
    s = str(val).strip()
    if s.lower() in ('none', '_', '0', ''): return ''
    try:
        if s.isdigit(): return str(int(s))
    except: pass
    return s

def _save_search_cache_atomic(cache_dict):
    """Guarda SEARCH_CACHE_FILE de forma completamente atomica con reemplazo seguro
    y copia de respaldo (.bak) para evitar lecturas concurrentes corruptas o vacias."""
    if not isinstance(cache_dict, dict):
        return False
    with _SEARCH_CACHE_LOCK:
        try:
            dir_name = os.path.dirname(SEARCH_CACHE_FILE)
            if not os.path.exists(dir_name):
                try: os.makedirs(dir_name, exist_ok=True)
                except: pass
            tmp_file = "%s.tmp.%d.%d" % (SEARCH_CACHE_FILE, os.getpid(), threading.get_ident())
            with open(tmp_file, 'w', encoding='utf-8') as fw:
                json.dump(cache_dict, fw)
                fw.flush()
                try: os.fsync(fw.fileno())
                except: pass

            replaced = False
            for attempt in range(8):
                try:
                    os.replace(tmp_file, SEARCH_CACHE_FILE)
                    replaced = True
                    break
                except (OSError, PermissionError):
                    time.sleep(0.04)

            if not replaced:
                try:
                    import shutil
                    shutil.copyfile(tmp_file, SEARCH_CACHE_FILE)
                    replaced = True
                except: pass

            try:
                if os.path.exists(tmp_file):
                    os.remove(tmp_file)
            except: pass

            if replaced and cache_dict.get('links'):
                try:
                    bak_file = SEARCH_CACHE_FILE + '.bak'
                    import shutil
                    shutil.copyfile(SEARCH_CACHE_FILE, bak_file)
                except: pass
            return replaced
        except Exception as e:
            try: xbmc.log("Multi Bridge: _save_search_cache_atomic error: %s" % e, xbmc.LOGWARNING)
            except: pass
            return False

def _read_search_cache_atomic(max_attempts=6, delay=0.05):
    """Lee SEARCH_CACHE_FILE con reintentos para tolerar escrituras simultaneas
    en Windows y recurre a la copia .bak si el fichero principal falla."""
    if not os.path.exists(SEARCH_CACHE_FILE):
        bak = SEARCH_CACHE_FILE + '.bak'
        if os.path.exists(bak):
            try:
                with open(bak, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except: pass
        return None

    last_err = None
    for attempt in range(max_attempts):
        try:
            with open(SEARCH_CACHE_FILE, 'r', encoding='utf-8') as f:
                content = f.read()
            if content and content.strip():
                data = json.loads(content)
                if isinstance(data, dict):
                    return data
        except (OSError, PermissionError, json.JSONDecodeError, ValueError) as e:
            last_err = e
            time.sleep(delay)
        except Exception as e:
            last_err = e
            break

    try:
        bak = SEARCH_CACHE_FILE + '.bak'
        if os.path.exists(bak):
            with open(bak, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    xbmc.log("Multi Bridge: cache leida exitosamente desde copia de respaldo .bak", xbmc.LOGINFO)
                    return data
    except: pass

    if last_err:
        xbmc.log("Multi Bridge: _read_search_cache_atomic fallo tras %d intentos: %s" % (max_attempts, last_err), xbmc.LOGWARNING)
    return None

def load_channels_data():
    if os.path.exists(CHANNELS_JSON_FILE):
        try:
            with open(CHANNELS_JSON_FILE, 'r', encoding='utf-8') as f:
                d = json.load(f)
            if isinstance(d, dict) and 'channels' in d:
                return d
        except Exception as e:
            xbmc.log("Multi Bridge: error cargando channels.json: %s" % e, xbmc.LOGWARNING)
    return {'version': 1, 'channels': {'alfa': {}, 'balandro': {}}}

def save_channels_data(data):
    try:
        if not os.path.exists(BRIDGE_DATA_PATH):
            os.makedirs(BRIDGE_DATA_PATH)
        with open(CHANNELS_JSON_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        return True
    except Exception as e:
        xbmc.log("Multi Bridge: error guardando channels.json: %s" % e, xbmc.LOGWARNING)
        return False

_RAM_SEARCH_CACHE = {}

# Recolector tardio: hilos que terminan despues del cierre pierden sus
# enlaces. Aqui se guardan para que el recolector los anexe a la lista
# abierta (sin reordenar lo ya mostrado) con refresco del contenedor.
_LAST_SEARCH_TOKEN = None
_LAST_MERGED_PFS = set()
_LAST_RESULTS_DICT = {}

def _store_ram_search_cache(links, matched_item, meta, engine):
    global _RAM_SEARCH_CACHE
    try:
        _RAM_SEARCH_CACHE = {
            'links': list(links or []),
            'matched_item': matched_item,
            'meta': dict(meta or {}),
            'engine': engine or 'alfa',
            'time': time.time(),
            'tmdb': _norm_id_str((meta or {}).get('tmdb')),
            'season': _norm_id_str((meta or {}).get('season')),
            'episode': _norm_id_str((meta or {}).get('episode'))
        }
    except Exception:
        pass

def _get_ram_search_cache(target_tmdb='', target_season='', target_episode='', max_age=300):
    global _RAM_SEARCH_CACHE
    if not _RAM_SEARCH_CACHE:
        return None
    if (time.time() - _RAM_SEARCH_CACHE.get('time', 0)) > max_age:
        return None
    t_tmdb = _norm_id_str(target_tmdb)
    r_tmdb = _norm_id_str(_RAM_SEARCH_CACHE.get('tmdb'))
    if t_tmdb and r_tmdb and t_tmdb != r_tmdb:
        return None
    t_sea = _norm_id_str(target_season)
    r_sea = _norm_id_str(_RAM_SEARCH_CACHE.get('season'))
    if t_sea and r_sea and t_sea != r_sea:
        return None
    t_epi = _norm_id_str(target_episode)
    r_epi = _norm_id_str(_RAM_SEARCH_CACHE.get('episode'))
    if t_epi and r_epi and t_epi != r_epi:
        return None
    return _RAM_SEARCH_CACHE

def _get_media_key(meta, item=None):
    if not meta: meta = {}
    s_val = meta.get('season') if meta.get('season') is not None else _safe_get_item_attr(item, 'season')
    e_val = meta.get('episode') if meta.get('episode') is not None else _safe_get_item_attr(item, 'episode')
    has_se = (s_val is not None and str(s_val).strip() != '') and (e_val is not None and str(e_val).strip() != '')
    is_series = bool(has_se or _safe_get_item_attr(item, 'contentType') == 'episode' or _safe_get_item_attr(item, 'contentSeason'))
    tmdb = meta.get('tmdb') or _safe_get_item_attr(item, 'tmdb_id') or _safe_get_item_attr(item, 'tmdb') or ''
    imdb = meta.get('imdb') or _safe_get_item_attr(item, 'imdb_id') or _safe_get_item_attr(item, 'imdb') or ''
    season = meta.get('season') or _safe_get_item_attr(item, 'season') or _safe_get_item_attr(item, 'contentSeason') or ''
    episode = meta.get('episode') or _safe_get_item_attr(item, 'episode') or _safe_get_item_attr(item, 'contentEpisodeNumber') or ''
    title = meta.get('title') or _safe_get_item_attr(item, 'contentTitle') or _safe_get_item_attr(item, 'title') or ''
    show = meta.get('showname') or _safe_get_item_attr(item, 'contentSerieName') or _safe_get_item_attr(item, 'show') or ''

    if is_series:
        s_str = '%02d' % int(season) if str(season).isdigit() else str(season)
        e_str = '%02d' % int(episode) if str(episode).isdigit() else str(episode)
        if tmdb: return 'tv_%s_s%s_e%s' % (tmdb, s_str, e_str)
        if imdb: return 'tv_%s_s%s_e%s' % (imdb, s_str, e_str)
        clean_s = clean_title(show or title)
        return 'tv_%s_s%s_e%s' % (clean_s, s_str, e_str)
    else:
        if tmdb: return 'movie_%s' % tmdb
        if imdb: return 'movie_%s' % imdb
        year = meta.get('year') or _safe_get_item_attr(item, 'year') or ''
        clean_t = clean_title(title)
        return 'movie_%s_%s' % (clean_t, year)

def _is_same_media_item(cached_meta, cur_meta):
    """Verifica si dos diccionarios meta corresponden exactamente al mismo contenido.
    Para series: comprueba que season y episode coincidan numéricamente y que
    sea la misma serie (mismo tmdb, imdb, tvdb o título/showname).
    Para películas: comprueba tmdb, imdb o título + año.
    """
    if not cached_meta or not cur_meta:
        return False

    c_s = cached_meta.get('season')
    c_e = cached_meta.get('episode')
    cur_s = cur_meta.get('season')
    cur_e = cur_meta.get('episode')

    c_is_series = bool(c_s is not None and str(c_s).strip() != '' and c_e is not None and str(c_e).strip() != '')
    cur_is_series = bool(cur_s is not None and str(cur_s).strip() != '' and cur_e is not None and str(cur_e).strip() != '')

    # Uno es serie y el otro película -> No es el mismo contenido
    if c_is_series != cur_is_series:
        return False

    if cur_is_series:
        # En series: temporada y episodio DEBEN coincidir numéricamente
        try:
            if int(c_s) != int(cur_s) or int(c_e) != int(cur_e):
                return False
        except (ValueError, TypeError):
            if str(c_s).strip() != str(cur_s).strip() or str(c_e).strip() != str(cur_e).strip():
                return False

        # Verificar que sea la misma serie (tmdb, imdb, tvdb o título)
        c_tmdb = str(cached_meta.get('tmdb') or '')
        cur_tmdb = str(cur_meta.get('tmdb') or '')
        if c_tmdb and cur_tmdb:
            return c_tmdb == cur_tmdb

        c_imdb = str(cached_meta.get('imdb') or '')
        cur_imdb = str(cur_meta.get('imdb') or '')
        if c_imdb and cur_imdb:
            return c_imdb == cur_imdb

        c_tvdb = str(cached_meta.get('tvdb') or '')
        cur_tvdb = str(cur_meta.get('tvdb') or '')
        if c_tvdb and cur_tvdb:
            return c_tvdb == cur_tvdb

        # Comparar por nombre de serie si no hay IDs
        c_name = clean_title(cached_meta.get('showname') or cached_meta.get('title') or '')
        cur_name = clean_title(cur_meta.get('showname') or cur_meta.get('title') or '')
        return bool(c_name and cur_name and c_name == cur_name)
    else:
        # En películas: Comprobar tmdb, imdb o título + año
        c_tmdb = str(cached_meta.get('tmdb') or '')
        cur_tmdb = str(cur_meta.get('tmdb') or '')
        if c_tmdb and cur_tmdb:
            return c_tmdb == cur_tmdb

        c_imdb = str(cached_meta.get('imdb') or '')
        cur_imdb = str(cur_meta.get('imdb') or '')
        if c_imdb and cur_imdb:
            return c_imdb == cur_imdb

        c_title = clean_title(cached_meta.get('title') or '')
        cur_title = clean_title(cur_meta.get('title') or '')
        c_year = str(cached_meta.get('year') or '')
        cur_year = str(cur_meta.get('year') or '')
        if c_title and cur_title and c_title == cur_title:
            if c_year and cur_year:
                return c_year == cur_year
            return True
        return False


def get_bookmark(media_key, tmdb_id=None, is_series=False, season=None, episode=None):
    # 1. Local bookmark database
    if media_key and os.path.exists(BOOKMARKS_FILE):
        try:
            with open(BOOKMARKS_FILE, 'r', encoding='utf-8') as f: data = json.load(f)
            bm = data.get(media_key)
            if bm and isinstance(bm, dict):
                r_time = float(bm.get('resume_time', 0))
                t_time = float(bm.get('total_time', 0))
                if r_time > 20 and (t_time == 0 or (r_time / t_time < 0.93)):
                    return bm
        except Exception: pass

    # 2. Sync from TMDb Helper's Trakt cache (ItemDetails.db)
    if tmdb_id:
        try:
            tmdb_base = xbmcvfs.translatePath('special://userdata/addon_data/plugin.video.themoviedb.helper/')
            db_files = []
            if os.path.exists(tmdb_base):
                for root, _, files in os.walk(tmdb_base):
                    for fn in files:
                        if fn.lower() == 'itemdetails.db':
                            db_files.append(os.path.join(root, fn))

            for db_path in db_files:
                try:
                    conn = sqlite3.connect(db_path)
                    c = conn.cursor()
                    if is_series and season is not None and episode is not None:
                        c.execute("SELECT playback_progress, runtime, title FROM simplecache WHERE tmdb_id = ? AND season_number = ? AND episode_number = ? AND item_type = 'episode' AND playback_progress > 0;", (int(tmdb_id), int(season), int(episode)))
                    else:
                        c.execute("SELECT playback_progress, runtime, title FROM simplecache WHERE tmdb_id = ? AND item_type = 'movie' AND playback_progress > 0;", (int(tmdb_id),))
                    row = c.fetchone()
                    conn.close()
                    if row:
                        progress_pct, runtime_mins, title = row[0], row[1], row[2]
                        if progress_pct and runtime_mins and float(progress_pct) > 0.2:
                            pct = float(progress_pct)
                            if pct <= 1.0 and pct > 0: pct = pct * 100.0
                            tot_sec = float(runtime_mins) * 60.0
                            res_sec = (pct / 100.0) * tot_sec
                            if res_sec > 20 and (res_sec / tot_sec < 0.93):
                                return {'resume_time': res_sec, 'total_time': tot_sec, 'title': title, 'source': 'trakt'}
                except Exception: pass
        except Exception: pass

    return None

def save_bookmark(media_key, resume_time, total_time, title=""):
    if not media_key: return
    try:
        data = {}
        if os.path.exists(BOOKMARKS_FILE):
            try:
                with open(BOOKMARKS_FILE, 'r', encoding='utf-8') as f: data = json.load(f)
            except: data = {}
        
        if total_time > 0 and (resume_time / float(total_time) >= 0.92):
            if media_key in data: del data[media_key]
        elif resume_time > 30:
            data[media_key] = {
                'resume_time': float(resume_time),
                'total_time': float(total_time),
                'title': title,
                'updated': time.time()
            }
        elif resume_time <= 30 and media_key in data:
            del data[media_key]

        # Escritura atomica (temp + replace): si Kodi mata el script a mitad
        # (cierre con hilos vivos), el archivo jamas queda corrupto a medias.
        try:
            _tmp = BOOKMARKS_FILE + '.tmp'
            with open(_tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(_tmp, BOOKMARKS_FILE)
        except Exception:
            try:
                with open(BOOKMARKS_FILE, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
            except: pass
    except: pass

def load_continue_watching_data():
    if os.path.exists(CONTINUE_WATCHING_FILE):
        try:
            with open(CONTINUE_WATCHING_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_continue_watching_record(media_key, record_data):
    if not media_key or not isinstance(record_data, dict): return
    try:
        data = load_continue_watching_data()
        data[media_key] = record_data
        _tmp = CONTINUE_WATCHING_FILE + '.tmp'
        with open(_tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(_tmp, CONTINUE_WATCHING_FILE)
    except Exception as e:
        xbmc.log(f"Multi Bridge: save_continue_watching_record error: {e}", xbmc.LOGWARNING)

def remove_continue_watching_record(media_key):
    if not media_key: return
    try:
        data = load_continue_watching_data()
        if media_key in data:
            del data[media_key]
            _tmp = CONTINUE_WATCHING_FILE + '.tmp'
            with open(_tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(_tmp, CONTINUE_WATCHING_FILE)
    except Exception as e:
        xbmc.log(f"Multi Bridge: remove_continue_watching_record error: {e}", xbmc.LOGWARNING)

def clear_all_continue_watching():
    try:
        _tmp = CONTINUE_WATCHING_FILE + '.tmp'
        with open(_tmp, 'w', encoding='utf-8') as f:
            json.dump({}, f, indent=2, ensure_ascii=False)
        os.replace(_tmp, CONTINUE_WATCHING_FILE)
    except Exception as e:
        xbmc.log(f"Multi Bridge: clear_all_continue_watching error: {e}", xbmc.LOGWARNING)

_floating_dialog_active = False
_floating_dialog_lock = threading.Lock()
_playback_monitor_token = 0
_playback_monitor_lock = threading.Lock()
_current_playing_link_index = -1

def _get_global_monitor_token():
    try:
        return xbmcgui.Window(10000).getProperty('BridgeMulti.MonitorToken') or ''
    except Exception:
        return ''

def _set_global_monitor_token(token):
    try:
        xbmcgui.Window(10000).setProperty('BridgeMulti.MonitorToken', str(token or ''))
    except Exception:
        pass

def _is_dialog_active_global():
    try:
        return xbmcgui.Window(10000).getProperty('BridgeMulti.DialogActive') == 'true'
    except Exception:
        return False

def _set_dialog_active_global(active):
    try:
        xbmcgui.Window(10000).setProperty('BridgeMulti.DialogActive', 'true' if active else 'false')
    except Exception:
        pass

def start_playback_monitor(media_key, title_str="", seek_to_time=0, current_link_index=0, meta=None):
    if not media_key: return
    global _playback_monitor_token, _current_playing_link_index
    with _playback_monitor_lock:
        _playback_monitor_token += 1
        my_token = "%f_%d_%d" % (time.time(), _playback_monitor_token, random.randint(10000, 99999))
        _set_global_monitor_token(my_token)
        try:
            xbmcgui.Window(10000).setProperty('BridgeMulti.CurrentMediaKey', str(media_key or ''))
        except Exception:
            pass
        _current_playing_link_index = current_link_index

    # Limpiar cualquier estado residual de diálogo previo
    _set_dialog_active_global(False)

    def _monitor_loop():
        global _floating_dialog_active
        p = xbmc.Player()
        mon = xbmc.Monitor()
        xbmc.log("Multi Bridge: monitor de reproducción iniciado para %s (esperando inicio de vídeo)..." % (title_str or media_key), xbmc.LOGINFO)

        # Esperar hasta 180 segundos (3 minutos) a que inicie la reproducción
        # Esto es vital para torrents (Elementum, Quasar) que tardan en conectar con peers y descargar el pre-buffer
        video_started = False
        for _w in range(720):
            if mon.abortRequested() or _get_global_monitor_token() != my_token:
                xbmc.log("Multi Bridge: monitor descartado antes de iniciar (superado por nuevo enlace o cancelado)", xbmc.LOGINFO)
                return
            if p.isPlayingVideo():
                # Comprobar que la clave de medios coincide con la sesión actual
                cur_k = ''
                try: cur_k = xbmcgui.Window(10000).getProperty('BridgeMulti.CurrentMediaKey') or ''
                except Exception: pass
                if cur_k and cur_k != str(media_key):
                    xbmc.log("Multi Bridge: monitor descartado (reproducción activa no coincide con media_key: %s != %s)" % (cur_k, media_key), xbmc.LOGINFO)
                    return
                video_started = True
                break
            if mon.waitForAbort(0.25): return

        if not video_started or not p.isPlayingVideo() or _get_global_monitor_token() != my_token:
            xbmc.log("Multi Bridge: monitor de reproducción cancelado (tiempo agotado o superado)", xbmc.LOGINFO)
            return

        xbmc.log("Multi Bridge: monitor de reproducción ACTIVO (vídeo detectado en reproducción: %s)" % (title_str or media_key), xbmc.LOGINFO)

        # Registrar sesión activa en Continuar viendo con estado inicial 'playing'
        _m_dict = dict(meta or {})
        session_record = {
            'media_key': media_key,
            'status': 'playing',
            'interrupted': True,
            'tmdb': _m_dict.get('tmdb') or '',
            'imdb': _m_dict.get('imdb') or '',
            'tvdb': _m_dict.get('tvdb') or '',
            'trakt': _m_dict.get('trakt') or '',
            'is_series': bool(_m_dict.get('season') and _m_dict.get('episode')),
            'season': _m_dict.get('season'),
            'episode': _m_dict.get('episode'),
            'title': title_str or _m_dict.get('title') or '',
            'showname': _m_dict.get('showname') or '',
            'year': _m_dict.get('year') or _m_dict.get('showyear') or '',
            'plot': _m_dict.get('plot') or '',
            'poster': _m_dict.get('poster') or _m_dict.get('thumbnail') or '',
            'fanart': _m_dict.get('fanart') or '',
            'clearlogo': _m_dict.get('clearlogo') or '',
            'thumbnail': _m_dict.get('thumbnail') or '',
            'resume_time': float(seek_to_time or 0),
            'total_time': 0,
            'last_heartbeat': time.time()
        }
        save_continue_watching_record(media_key, session_record)

        if seek_to_time > 2:
            for _s in range(20):
                if mon.abortRequested() or _get_global_monitor_token() != my_token: return
                if not p.isPlayingVideo(): break
                try:
                    if p.getTime() > 0 or p.getTotalTime() > 0:
                        break
                except: pass
                if mon.waitForAbort(0.2): return
            try:
                xbmc.log("Multi Bridge: saltando a reanudar %.0fs (key=%s)" % (float(seek_to_time), media_key), xbmc.LOGINFO)
                p.seekTime(float(seek_to_time))
            except Exception as _ske:
                xbmc.log("Multi Bridge: error seekTime: %s" % str(_ske), xbmc.LOGINFO)

        last_saved_time = 0
        tot_time = 0
        last_pause_state = False
        pause_cooldown = time.time() + 2.5
        active_idx = current_link_index

        while p.isPlayingVideo() and not mon.abortRequested():
            if _get_global_monitor_token() != my_token:
                xbmc.log("Multi Bridge: monitor finalizado (un nuevo monitor tomó el control)", xbmc.LOGINFO)
                break
            try:
                cur_time = p.getTime()
                tot = p.getTotalTime()
                if tot > 0: tot_time = tot
                if abs(cur_time - last_saved_time) >= 5:
                    save_bookmark(media_key, cur_time, tot_time, title=title_str)
                    last_saved_time = cur_time

                    # Actualizar latido continuo en Continuar viendo
                    session_record['resume_time'] = float(cur_time)
                    session_record['total_time'] = float(tot_time)
                    session_record['last_heartbeat'] = time.time()
                    save_continue_watching_record(media_key, session_record)

                # Detección de pausa en vivo
                is_paused = bool(xbmc.getCondVisibility("Player.Paused"))
                pause_setting = str(_bridge_addon.getSetting('live_switch_on_pause') or '').strip().lower()

                if is_paused and not last_pause_state and pause_setting not in ('false', '2'):
                    now = time.time()
                    if now > pause_cooldown and cur_time >= 0.5 and not _floating_dialog_active and not _is_dialog_active_global():
                        xbmc.log("Multi Bridge: PAUSA DETECTADA en reproducción (cur_time=%.1fs, server_idx=%d)" % (cur_time, active_idx), xbmc.LOGINFO)
                        with _floating_dialog_lock:
                            _floating_dialog_active = True
                        _set_dialog_active_global(True)
                        try:
                            c_links, c_matched, c_meta, c_eng = _load_cached_links_for_dialog()
                            if c_links:
                                open_links = False
                                if pause_setting == '1':
                                    open_links = True
                                else:
                                    curr_srv = _get_link_server_name(c_links[active_idx]) if active_idx < len(c_links) else ""
                                    prompt_dlg = _PausePromptDialog(current_server_name=curr_srv, meta=c_meta)
                                    prompt_dlg.doModal()
                                    ans = prompt_dlg.selected
                                    del prompt_dlg
                                    if ans == 1 and p.isPlayingVideo():
                                        open_links = True
                                    else:
                                        if p.isPlayingVideo() and xbmc.getCondVisibility("Player.Paused"):
                                            try: p.pause()
                                            except: pass
                                        is_paused = False
                                        last_pause_state = False
                                        pause_cooldown = time.time() + 4.0

                                if open_links and p.isPlayingVideo():
                                    dlg = _FloatingLinksDialog(c_links, current_index=active_idx, meta=c_meta, engine=c_eng, is_playback=True)
                                    dlg.doModal()
                                    chosen_idx = dlg.selected
                                    del dlg
                                    if chosen_idx == 'other_engine':
                                        _other_engine = 'balandro' if c_eng == 'alfa' else 'alfa'
                                        xbmc.executebuiltin('RunPlugin(plugin://plugin.video.bridge.multi/?action=search_other_engine&engine=%s)' % _other_engine)
                                        return
                                    elif isinstance(chosen_idx, int) and chosen_idx >= 0:
                                        _switch_to_link(chosen_idx, c_links, c_meta, c_matched, engine=c_eng)
                                        return
                                    else:
                                        if p.isPlayingVideo() and xbmc.getCondVisibility("Player.Paused"):
                                            try: p.pause()
                                            except: pass
                                        is_paused = False
                                        last_pause_state = False
                                        pause_cooldown = time.time() + 4.0
                        except Exception as _de:
                            xbmc.log(f"Multi Bridge: floating dialog error: {_de}", xbmc.LOGINFO)
                        finally:
                            _set_dialog_active_global(False)
                            with _floating_dialog_lock:
                                _floating_dialog_active = False

                last_pause_state = is_paused

            except Exception:
                pass

            if mon.waitForAbort(0.25): break

        if _get_global_monitor_token() == my_token:
            _set_global_monitor_token('')
            try: xbmcgui.Window(10000).clearProperty('BridgeMulti.CurrentMediaKey')
            except Exception: pass

        if last_saved_time > 0 or tot_time > 0:
            save_bookmark(media_key, last_saved_time, tot_time, title=title_str)

        # Distinción exacta: Parada normal voluntaria vs Interrupción anormal (corte de luz / crash)
        normal_exit = not mon.abortRequested()
        video_finished = bool(tot_time > 0 and (last_saved_time / float(tot_time) >= 0.90))

        if normal_exit or video_finished:
            # El usuario pulsó Stop, volvió atrás o vio el final: parada limpia
            xbmc.log("Multi Bridge: parada normal voluntaria o finalizado. Limpiando registro de Continuar viendo.", xbmc.LOGINFO)
            remove_continue_watching_record(media_key)
        else:
            # Cierre forzado o interrupción inesperada de Kodi
            xbmc.log("Multi Bridge: parada abrupta/interrupción inesperada detectada. Preservando en Continuar viendo.", xbmc.LOGINFO)

    _th = threading.Thread(target=_monitor_loop, daemon=False)
    _th.name = "BridgeMultiPlaybackMonitor"
    _th.start()

alfa_icon = 'DefaultVideo.png'

def _safe_str(val):
    if val is None: return ""
    if isinstance(val, bytes):
        try: return val.decode('utf-8', errors='ignore')
        except: return str(val)
    return str(val)

def _safe_get_item_attr(item, attr_name, default=''):
    if not item: return default
    if hasattr(item, '__dict__') and attr_name in item.__dict__:
        val = item.__dict__[attr_name]
        if val is not None and val != '': return val
    if hasattr(item, '__dict__') and 'infoLabels' in item.__dict__:
        il = item.__dict__['infoLabels']
        if isinstance(il, dict) and attr_name in il:
            val = il[attr_name]
            if val is not None and val != '': return val
    return default

# ---------------------------------------------------------
# Dynamic Module Switcher for Clean Alfa / Balandro Execution
# ---------------------------------------------------------
_current_engine_env = None
_engine_lock = threading.RLock()

def _is_engine_installed(engine):
    """Comprueba si el motor (alfa o balandro) está instalado y disponible en Kodi."""
    addon_id = 'plugin.video.alfa' if engine == 'alfa' else 'plugin.video.balandro'
    addon_dir = alfa_path if engine == 'alfa' else balandro_path

    try:
        if xbmc.getCondVisibility('System.HasAddon("%s")' % addon_id):
            return True
    except Exception:
        pass

    try:
        _a = xbmcaddon.Addon(addon_id)
        if _a and _a.getAddonInfo('id'):
            return True
    except Exception:
        pass

    try:
        if os.path.isdir(addon_dir) and os.path.isdir(os.path.join(addon_dir, 'channels')):
            return True
    except Exception:
        pass

    return False

def _switch_engine_environment(target_engine):
    global _current_engine_env
    if _current_engine_env == target_engine:
        return
    with _engine_lock:
        if _current_engine_env == target_engine:
            return
        for m in list(sys.modules.keys()):
            if any(m == sm or m.startswith(sm + '.') for sm in ['platformcode', 'core', 'channels', 'servers', 'modules', 'lib']):
                del sys.modules[m]
        sys.path = [p for p in sys.path if p not in (alfa_path, _alfa_lib, balandro_path, _balandro_lib)]
        if target_engine == 'alfa':
            sys.argv[0] = 'plugin://plugin.video.alfa/'
            if alfa_path not in sys.path: sys.path.insert(0, alfa_path)
            if os.path.exists(_alfa_lib) and _alfa_lib not in sys.path: sys.path.append(_alfa_lib)
        else:
            sys.argv[0] = 'plugin://plugin.video.balandro/'
            if balandro_path not in sys.path: sys.path.insert(0, balandro_path)
            if os.path.exists(_balandro_lib) and _balandro_lib not in sys.path: sys.path.append(_balandro_lib)
        _current_engine_env = target_engine

_ALFA_MODULES_CACHE = None
_BALANDRO_MODULES_CACHE = None

def _get_alfa_modules():
    global _ALFA_MODULES_CACHE
    if _ALFA_MODULES_CACHE is not None and _current_engine_env == 'alfa':
        return _ALFA_MODULES_CACHE
    if not _is_engine_installed('alfa'):
        return None
    try:
        _switch_engine_environment('alfa')
        from platformcode import config, platformtools
        from core.item import Item, InfoLabels
        from core import servertools, httptools, scrapertools
        config.get_runtime_path = lambda: alfa_path
        try:
            from core import tmdb as _a_tmdb
            _a_tmdb.set_infoLabels = lambda source=None, *args, **kwargs: (source if isinstance(source, list) else [])
            _a_tmdb.set_infoLabels_itemlist = lambda source=None, *args, **kwargs: (source if isinstance(source, list) else [])
        except Exception: pass
        _ALFA_MODULES_CACHE = {
            'path': alfa_path, 'config': config, 'platformtools': platformtools,
            'Item': Item, 'InfoLabels': InfoLabels, 'servertools': servertools,
            'httptools': httptools, 'scrapertools': scrapertools
        }
        return _ALFA_MODULES_CACHE
    except Exception as e:
        xbmc.log("Multi Bridge: Error cargando Alfa: " + str(e), xbmc.LOGWARNING)
        return None

def _get_balandro_modules():
    global _BALANDRO_MODULES_CACHE
    if _BALANDRO_MODULES_CACHE is not None and _current_engine_env == 'balandro':
        return _BALANDRO_MODULES_CACHE
    if not _is_engine_installed('balandro'):
        return None
    try:
        _switch_engine_environment('balandro')
        from platformcode import config, platformtools
        from core.item import Item, InfoLabels
        from core import servertools, httptools, scrapertools
        config.get_runtime_path = lambda: balandro_path
        try:
            from core import tmdb as _b_tmdb
            _b_tmdb.set_infoLabels = lambda source=None, *args, **kwargs: (source if isinstance(source, list) else [])
            _b_tmdb.set_infoLabels_itemlist = lambda source=None, *args, **kwargs: (source if isinstance(source, list) else [])
            _b_tmdb.set_infoLabels_item = lambda *args, **kwargs: 0
        except Exception: pass

        orig_set_infolabels = platformtools.set_infolabels
        def _enhanced_balandro_set_infolabels(listitem, item, player=False):
            tmdb_id = str(_safe_get_item_attr(item, 'tmdb_id') or _safe_get_item_attr(item, 'tmdb') or _safe_get_item_attr(item, 'code') or '')
            imdb_id = str(_safe_get_item_attr(item, 'imdb_id') or _safe_get_item_attr(item, 'imdb') or _safe_get_item_attr(item, 'imdbnumber') or '')
            tvdb_id = str(_safe_get_item_attr(item, 'tvdb_id') or _safe_get_item_attr(item, 'tvdb') or '')
            trakt_id = str(_safe_get_item_attr(item, 'trakt_id') or _safe_get_item_attr(item, 'trakt') or '')

            title_str = str(_safe_get_item_attr(item, 'title') or _safe_get_item_attr(item, 'contentTitle') or '')
            showname_str = str(_safe_get_item_attr(item, 'tvshowtitle') or _safe_get_item_attr(item, 'contentSerieName') or _safe_get_item_attr(item, 'show') or '')
            season_num = _safe_get_item_attr(item, 'season', None)
            if season_num is None: season_num = _safe_get_item_attr(item, 'contentSeason', None)
            episode_num = _safe_get_item_attr(item, 'episode', None)
            if episode_num is None: episode_num = _safe_get_item_attr(item, 'contentEpisodeNumber', None)
            year_num = _safe_get_item_attr(item, 'year', None)
            plot_str = str(_safe_get_item_attr(item, 'plot') or _safe_get_item_attr(item, 'sinopsis') or '')
            tagline_str = str(_safe_get_item_attr(item, 'tagline') or _safe_get_item_attr(item, 'slogan') or '')

            try: orig_set_infolabels(listitem, item, player=player)
            except Exception: pass

            try:
                is_ep = bool(season_num is not None and episode_num is not None and str(season_num) != '' and str(episode_num) != '')
                unique_dict = {}
                if tmdb_id: unique_dict['tmdb'] = tmdb_id
                if imdb_id: unique_dict['imdb'] = imdb_id
                if tvdb_id: unique_dict['tvdb'] = tvdb_id
                if trakt_id: unique_dict['trakt'] = trakt_id

                if unique_dict:
                    try: listitem.setUniqueIDs(unique_dict, 'tmdb' if tmdb_id else 'imdb')
                    except: pass

                if tmdb_id: listitem.setProperty('tmdb_id', tmdb_id)
                if imdb_id: listitem.setProperty('imdb_id', imdb_id)
                if tvdb_id: listitem.setProperty('tvdb_id', tvdb_id)

                trakt_payload = {}
                if tmdb_id: trakt_payload['tmdb'] = int(tmdb_id) if tmdb_id.isdigit() else tmdb_id
                if imdb_id: trakt_payload['imdb'] = imdb_id
                if tvdb_id: trakt_payload['tvdb'] = int(tvdb_id) if tvdb_id.isdigit() else tvdb_id
                if trakt_payload:
                    listitem.setProperty('script.trakt.ids', json.dumps(trakt_payload))

                try:
                    vt = listitem.getVideoInfoTag()
                    if vt:
                        if unique_dict:
                            try: vt.setUniqueIDs(unique_dict, 'tmdb' if tmdb_id else ('imdb' if imdb_id else ''))
                            except: pass
                        if imdb_id:
                            try: vt.setIMDbNumber(imdb_id)
                            except: pass
                        if is_ep:
                            vt.setMediaType('episode')
                            if showname_str: vt.setTvShowTitle(showname_str)
                            if season_num: vt.setSeason(int(season_num))
                            if episode_num: vt.setEpisode(int(episode_num))
                            if title_str: vt.setTitle(title_str)
                        else:
                            vt.setMediaType('movie')
                            if title_str: vt.setTitle(title_str)
                        if year_num:
                            try: vt.setYear(int(year_num))
                            except: pass
                        if plot_str: vt.setPlot(plot_str)
                        if tagline_str:
                            try: vt.setTagLine(tagline_str)
                            except: pass
                except: pass
            except Exception: pass

        platformtools.set_infolabels = _enhanced_balandro_set_infolabels

        _BALANDRO_MODULES_CACHE = {
            'path': balandro_path, 'config': config, 'platformtools': platformtools,
            'Item': Item, 'InfoLabels': InfoLabels, 'servertools': servertools,
            'httptools': httptools, 'scrapertools': scrapertools
        }
        return _BALANDRO_MODULES_CACHE
    except Exception as e:
        xbmc.log("Multi Bridge: Error cargando Balandro: " + str(e), xbmc.LOGWARNING)
        return None

_dialog_silence_depth = 0
_dialog_silence_lock = threading.RLock()
_dialog_orig_pt = {}
_dialog_orig_tmdb = {}
_dialog_orig_modules = {}
_orig_dialog_class = None
_search_in_progress = False
_links_view_active = False

class _SilentDialog:
    def __init__(self, *args, **kwargs): pass
    def ok(self, *args, **kwargs): return True
    def yesno(self, *args, **kwargs): return True
    def select(self, *args, **kwargs): return -1
    def multiselect(self, *args, **kwargs): return []
    def notification(self, *args, **kwargs): return None
    def textviewer(self, *args, **kwargs): pass
    def input(self, *args, **kwargs): return ''
    def browse(self, *args, **kwargs): return ''
    def numeric(self, *args, **kwargs): return 0
    def contextmenu(self, *args, **kwargs): return -1

def _apply_silence_all():
    global _orig_dialog_class
    if hasattr(xbmcgui, 'Dialog') and xbmcgui.Dialog is not _SilentDialog:
        if _orig_dialog_class is None:
            _orig_dialog_class = xbmcgui.Dialog
        try:
            xbmcgui.Dialog = _SilentDialog
        except:
            pass

    for m_name, m_mod in list(sys.modules.items()):
        if not m_mod:
            continue
        if hasattr(m_mod, 'Dialog'):
            cur_d = getattr(m_mod, 'Dialog', None)
            if cur_d is not None and cur_d is not _SilentDialog:
                if m_name not in _dialog_orig_modules:
                    _dialog_orig_modules[m_name] = cur_d
                try:
                    setattr(m_mod, 'Dialog', _SilentDialog)
                except:
                    pass

        if 'platformtools' in m_name:
            if m_name not in _dialog_orig_pt:
                _dialog_orig_pt[m_name] = {
                    'dialog_ok': getattr(m_mod, 'dialog_ok', None),
                    'dialog_notification': getattr(m_mod, 'dialog_notification', None),
                    'dialog_yesno': getattr(m_mod, 'dialog_yesno', None),
                    'dialog_select': getattr(m_mod, 'dialog_select', None),
                    'dialog_multiselect': getattr(m_mod, 'dialog_multiselect', None),
                }
            m_mod.dialog_ok = lambda *args, **kwargs: None
            m_mod.dialog_notification = lambda *args, **kwargs: None
            m_mod.dialog_yesno = lambda *args, **kwargs: True
            m_mod.dialog_select = lambda *args, **kwargs: -1
            m_mod.dialog_multiselect = lambda *args, **kwargs: []

        if hasattr(m_mod, 'platformtools') and getattr(m_mod, 'platformtools', None):
            try:
                pt = getattr(m_mod, 'platformtools')
                pt_id = id(pt)
                if pt_id not in _dialog_orig_pt:
                    _dialog_orig_pt[pt_id] = {
                        'mod': pt,
                        'dialog_ok': getattr(pt, 'dialog_ok', None),
                        'dialog_notification': getattr(pt, 'dialog_notification', None),
                        'dialog_yesno': getattr(pt, 'dialog_yesno', None),
                        'dialog_select': getattr(pt, 'dialog_select', None),
                        'dialog_multiselect': getattr(pt, 'dialog_multiselect', None),
                    }
                pt.dialog_ok = lambda *args, **kwargs: None
                pt.dialog_notification = lambda *args, **kwargs: None
                pt.dialog_yesno = lambda *args, **kwargs: True
                pt.dialog_select = lambda *args, **kwargs: -1
                pt.dialog_multiselect = lambda *args, **kwargs: []
            except:
                pass

    # Silenciar también en cachés de módulos de motores si ya están cargadas
    for c_mods in (_ALFA_MODULES_CACHE, _BALANDRO_MODULES_CACHE):
        if c_mods and isinstance(c_mods, dict) and 'platformtools' in c_mods:
            try:
                pt = c_mods['platformtools']
                pt_id = id(pt)
                if pt_id not in _dialog_orig_pt:
                    _dialog_orig_pt[pt_id] = {
                        'mod': pt,
                        'dialog_ok': getattr(pt, 'dialog_ok', None),
                        'dialog_notification': getattr(pt, 'dialog_notification', None),
                        'dialog_yesno': getattr(pt, 'dialog_yesno', None),
                        'dialog_select': getattr(pt, 'dialog_select', None),
                        'dialog_multiselect': getattr(pt, 'dialog_multiselect', None),
                    }
                pt.dialog_ok = lambda *args, **kwargs: None
                pt.dialog_notification = lambda *args, **kwargs: None
                pt.dialog_yesno = lambda *args, **kwargs: True
                pt.dialog_select = lambda *args, **kwargs: -1
                pt.dialog_multiselect = lambda *args, **kwargs: []
            except:
                pass

        if 'core.tmdb' in m_name or m_name == 'tmdb' or (hasattr(m_mod, 'tmdb') and getattr(m_mod, 'tmdb', None)):
            t_targets = []
            if 'core.tmdb' in m_name or m_name == 'tmdb':
                t_targets.append((m_name, m_mod))
            if hasattr(m_mod, 'tmdb') and getattr(m_mod, 'tmdb', None):
                t_targets.append((m_name + '.tmdb', getattr(m_mod, 'tmdb')))
            for t_name, t_obj in t_targets:
                if t_name not in _dialog_orig_tmdb:
                    _dialog_orig_tmdb[t_name] = {
                        'obj': t_obj,
                        'set_infoLabels': getattr(t_obj, 'set_infoLabels', None),
                        'set_infoLabels_itemlist': getattr(t_obj, 'set_infoLabels_itemlist', None),
                        'set_infoLabels_item': getattr(t_obj, 'set_infoLabels_item', None),
                    }
                _dummy_sil = lambda source=None, *args, **kwargs: (source if isinstance(source, list) else [])
                try:
                    t_obj.set_infoLabels = _dummy_sil
                    t_obj.set_infoLabels_itemlist = _dummy_sil
                    if hasattr(t_obj, 'set_infoLabels_item'):
                        t_obj.set_infoLabels_item = lambda *args, **kwargs: 0
                except: pass

def _restore_silence_all():
    global _orig_dialog_class
    with _dialog_silence_lock:
        try:
            if hasattr(xbmcgui, 'Dialog'):
                target_cls = _orig_dialog_class or _KODI_ORIG_DIALOG
                if target_cls:
                    xbmcgui.Dialog = target_cls
        except Exception:
            pass
        _orig_dialog_class = None

        # Restaurar Dialog en todos los módulos de sys.modules afectados
        try:
            for m_name, orig_d in list(_dialog_orig_modules.items()):
                try:
                    if m_name in sys.modules and orig_d is not None:
                        setattr(sys.modules[m_name], 'Dialog', orig_d)
                except Exception:
                    pass
        except Exception:
            pass
        _dialog_orig_modules.clear()

        try:
            pt_items = list(_dialog_orig_pt.items())
        except Exception:
            pt_items = []
        for k, v in pt_items:
            try:
                if isinstance(k, str) and k in sys.modules:
                    m = sys.modules.get(k)
                    if m:
                        for fn, orig in list(v.items()):
                            if orig is not None:
                                try: setattr(m, fn, orig)
                                except Exception: pass
                elif isinstance(k, int) and 'mod' in v:
                    m = v.get('mod')
                    if m:
                        for fn in ('dialog_ok', 'dialog_notification', 'dialog_yesno', 'dialog_select', 'dialog_multiselect'):
                            orig = v.get(fn)
                            if orig is not None:
                                try: setattr(m, fn, orig)
                                except Exception: pass
            except Exception:
                pass
        _dialog_orig_pt.clear()

        try:
            tmdb_items = list(_dialog_orig_tmdb.items())
        except Exception:
            tmdb_items = []
        for k, v in tmdb_items:
            try:
                m = v.get('obj')
                if m is not None:
                    for fn in ('set_infoLabels', 'set_infoLabels_itemlist', 'set_infoLabels_item'):
                        orig = v.get(fn)
                        if orig is not None:
                            try: setattr(m, fn, orig)
                            except Exception: pass
                elif isinstance(k, str) and k in sys.modules:
                    m = sys.modules.get(k)
                    if m:
                        for fn, orig in list(v.items()):
                            if orig is not None:
                                try: setattr(m, fn, orig)
                                except Exception: pass
            except Exception:
                pass
        _dialog_orig_tmdb.clear()

def _restore_dialog_noblock():
    """Restaura xbmcgui.Dialog real SIN usar cerrojos globales."""
    try:
        target_cls = _orig_dialog_class or _KODI_ORIG_DIALOG
        if target_cls is not None and hasattr(xbmcgui, 'Dialog'):
            xbmcgui.Dialog = target_cls
            return True
    except Exception:
        pass
    return False

def _ask_resume_dialog(title_text, time_str):
    """Pregunta reanudar usando la clase Dialog REAL guardada, inmune al
    silenciamiento global que dejan workers de verificacion aun en curso
    (esos workers hacen que xbmcgui.Dialog siga siendo el falso silencioso,
    que contesta Si sin mostrar nada). Mismo texto y botones que siempre."""
    try:
        dlg_cls = _orig_dialog_class or _KODI_ORIG_DIALOG
        if dlg_cls is None or dlg_cls is _SilentDialog:
            dlg_cls = xbmcgui.Dialog
        return bool(dlg_cls().yesno(
            'Reanudar reproducción',
            '¿Reanudar desde [COLOR gold]%s[/COLOR]?' % time_str,
            nolabel='Desde el principio', yeslabel='Reanudar (%s)' % time_str))
    except Exception:
        return False

@contextmanager
def silenced_dialogs():
    global _dialog_silence_depth
    with _dialog_silence_lock:
        _apply_silence_all()
        _dialog_silence_depth += 1

    try:
        yield
    finally:
        _need_restore = False
        with _dialog_silence_lock:
            _dialog_silence_depth -= 1
            if _dialog_silence_depth <= 0:
                _dialog_silence_depth = 0
                if not (_search_in_progress or _links_view_active):
                    _need_restore = True
        if _need_restore:
            _restore_silence_all()

def _prepare_playable_link(link, engine='alfa'):
    """Prepara un enlace decodificando URLs protegidas (ej. data_url base64 en Cinecalidad)
    y ejecutando canal.play(item) si el canal implementa resolutor propio."""
    if not link:
        return None

    item = link.clone() if hasattr(link, 'clone') else link

    # 1. Base64 data_url decode si url esta vacia o 'None'
    data_url = _safe_str(getattr(item, 'data_url', '') or '')
    cur_url = _safe_str(getattr(item, 'url', '') or '')
    if (not cur_url or cur_url.lower() == 'none') and data_url:
        try:
            item.url = base64.b64decode(data_url).decode('utf-8')
        except Exception as e:
            xbmc.log(f"Multi Bridge: error decodificando data_url: {e}", xbmc.LOGINFO)

    # 2. Si el canal tiene metodo play(), ejecutarlo para resolver wrappers/protectores
    ch_name = _safe_str(getattr(item, 'channel', '') or '').strip()
    srv = _safe_str(getattr(item, 'server', '') or '').strip().lower()
    cur_u = _safe_str(getattr(item, 'url', '') or '').lower()
    needs_channel_play = bool(not srv or srv in ('', 'directo', 'local') or (ch_name and ch_name in cur_u))
    if ch_name and needs_channel_play:
        if engine == 'balandro':
            try:
                _switch_engine_environment('balandro')
                ch_mod = __import__('channels.' + ch_name, fromlist=[''])
                if hasattr(ch_mod, 'play'):
                    with silenced_dialogs():
                        p_res = ch_mod.play(item)
                        if p_res:
                            if isinstance(p_res, list) and len(p_res) > 0:
                                if isinstance(p_res[0], list):
                                    item.video_urls = p_res
                                elif hasattr(p_res[0], 'url') or hasattr(p_res[0], 'server'):
                                    item = p_res[0]
                            elif hasattr(p_res, 'url'):
                                item = p_res
                            elif isinstance(p_res, str) and p_res.startswith('http'):
                                item.url = p_res
            except Exception as e:
                xbmc.log(f"Multi Bridge: balandro canal {ch_name}.play error: {e}", xbmc.LOGINFO)
        else:  # alfa
            try:
                _switch_engine_environment('alfa')
                ch_mod = __import__('channels.' + ch_name, fromlist=[''])
                if hasattr(ch_mod, 'play'):
                    with silenced_dialogs():
                        p_res = ch_mod.play(item)
                        if p_res:
                            if isinstance(p_res, list) and len(p_res) > 0:
                                if isinstance(p_res[0], list):
                                    item.video_urls = p_res
                                elif hasattr(p_res[0], 'url') or hasattr(p_res[0], 'server'):
                                    item = p_res[0]
                            elif hasattr(p_res, 'url'):
                                item = p_res
                            elif isinstance(p_res, str) and p_res.startswith('http'):
                                item.url = p_res
            except Exception as e:
                xbmc.log(f"Multi Bridge: alfa canal {ch_name}.play error: {e}", xbmc.LOGINFO)

    # 3. Comprobacion secundaria de data_url si url sigue vacia
    if (not getattr(item, 'url', '') or str(getattr(item, 'url', '')).lower() == 'none') and getattr(item, 'data_url', ''):
        try:
            item.url = base64.b64decode(item.data_url).decode('utf-8')
        except Exception:
            pass

    return item

# ---------------------------------------------------------
# Request URL Parameter Parsing (Supports Nested URLs)
# ---------------------------------------------------------
def get_param(name):
    if len(sys.argv) > 2 and sys.argv[2]:
        params = sys.argv[2]
        if params.startswith('?'): params = params[1:]
        for pair in params.split('&'):
            if '=' in pair:
                k, v = pair.split('=', 1)
                if k == name and v:
                    val = uparse.unquote(v).strip()
                    if val.lower() in ('none', 'null', 'undefined', '_', ''):
                        return ''
                    return val
        if 'url=' in params:
            nested_part = params.split('url=', 1)[1]
            unq_nested = uparse.unquote(nested_part)
            if '?' in unq_nested:
                inner_q = unq_nested.split('?', 1)[1]
                for pair in inner_q.split('&'):
                    if '=' in pair:
                        k, v = pair.split('=', 1)
                        if k == name and v:
                            val = uparse.unquote(v).strip()
                            if val.lower() in ('none', 'null', 'undefined', '_', ''):
                                return ''
                            return val
    return ''

url = get_param('url')
action = get_param('action')
view = get_param('view')
engine_param = get_param('engine')
title = get_param('title')
year = get_param('year')
season = get_param('season')
episode = get_param('episode')
showname = get_param('showname')
showyear = get_param('showyear')
title_es = get_param('title_es')
title_lat = get_param('title_lat')
title_en = get_param('title_en')
title_orig = get_param('title_orig')
tmdb_id = get_param('tmdb')
imdb_id = get_param('imdb')
tvdb_id = get_param('tvdb')
trakt_id = get_param('trakt')
plot = get_param('plot')
plot_lat = get_param('plot_lat')
plot_es = get_param('plot_es')
director = get_param('director')
tagline = get_param('tagline')
tagline_lat = get_param('tagline_lat')
tagline_es = get_param('tagline_es')
poster = get_param('poster')
fanart = get_param('fanart')
thumbnail = get_param('thumbnail')
clearlogo = get_param('clearlogo')

def decode_base64_item(b64_str, ItemClass):
    try:
        decoded_bytes = base64.b64decode(b64_str)
        decoded_str = decoded_bytes.decode('utf-8', errors='ignore')
        data = json.loads(decoded_str)
        it = ItemClass()
        it.__dict__.update(data)
        for k, v in data.items():
            try: setattr(it, k, v)
            except: pass
        return it
    except Exception:
        try: return ItemClass().fromurl('plugin://plugin.video.alfa/?' + b64_str)
        except: return ItemClass()

# ---------------------------------------------------------
# Title Matching and Normalization
# ---------------------------------------------------------

# ── Leading-article stripping (The/El/La/etc.) ──────────────────────────────
_LEADING_ARTICLES = frozenset([
    'the', 'el', 'la', 'los', 'las', 'a', 'an', 'un', 'una',
    'le', 'les', 'der', 'die', 'das', 'de', 'het',
])

def _strip_leading_article(s):
    """Remove leading article from a *cleaned* title string for comparison.
    'the office' → 'office', 'la casa de papel' stays (only drops first word)."""
    words = s.split()
    if len(words) > 1 and words[0] in _LEADING_ARTICLES:
        return ' '.join(words[1:])
    return s

# ── Roman-numeral → Arabic normalization ────────────────────────────────────
_ROMAN_PATTERNS = [
    (re.compile(r'\bxx\b'),   '20'), (re.compile(r'\bxix\b'),  '19'),
    (re.compile(r'\bxviii\b'),'18'), (re.compile(r'\bxvii\b'), '17'),
    (re.compile(r'\bxvi\b'),  '16'), (re.compile(r'\bxv\b'),   '15'),
    (re.compile(r'\bxiv\b'),  '14'), (re.compile(r'\bxiii\b'), '13'),
    (re.compile(r'\bxii\b'),  '12'), (re.compile(r'\bxi\b'),   '11'),
    (re.compile(r'\bx\b'),    '10'), (re.compile(r'\bix\b'),    '9'),
    (re.compile(r'\bviii\b'),  '8'), (re.compile(r'\bvii\b'),   '7'),
    (re.compile(r'\bvi\b'),    '6'), (re.compile(r'\biv\b'),    '4'),
    (re.compile(r'\bv\b'),     '5'), (re.compile(r'\biii\b'),   '3'),
    (re.compile(r'\bii\b'),    '2'),
]

def _normalize_numerals(s):
    """Convert roman numerals to arabic digits: 'rocky ii' → 'rocky 2'.
    Only standalone words, not inside larger tokens."""
    if not s: return ''
    for pat, num in _ROMAN_PATTERNS:
        s = pat.sub(num, s)
    return s

_WORD_NUMBERS = [
    (re.compile(r'\bse7en\b', re.IGNORECASE), '7'),
    (re.compile(r'\bscre4m\b', re.IGNORECASE), 'scream 4'),
    (re.compile(r'\bfant4stic\b', re.IGNORECASE), 'fantastic 4'),
    (re.compile(r'\bzero\b|\bcero\b', re.IGNORECASE), '0'),
    (re.compile(r'\bone\b|\buno\b', re.IGNORECASE), '1'),
    (re.compile(r'\btwo\b|\bdos\b', re.IGNORECASE), '2'),
    (re.compile(r'\bthree\b|\btres\b', re.IGNORECASE), '3'),
    (re.compile(r'\bfour\b|\bcuatro\b', re.IGNORECASE), '4'),
    (re.compile(r'\bfive\b|\bcinco\b', re.IGNORECASE), '5'),
    (re.compile(r'\bsix\b|\bseis\b', re.IGNORECASE), '6'),
    (re.compile(r'\bseven\b|\bsiete\b', re.IGNORECASE), '7'),
    (re.compile(r'\beight\b|\bocho\b', re.IGNORECASE), '8'),
    (re.compile(r'\bnine\b|\bnueve\b', re.IGNORECASE), '9'),
    (re.compile(r'\bten\b|\bdiez\b', re.IGNORECASE), '10'),
    (re.compile(r'\beleven\b|\bonce\b', re.IGNORECASE), '11'),
    (re.compile(r'\btwelve\b|\bdoce\b', re.IGNORECASE), '12'),
    (re.compile(r'\bthirteen\b|\btrece\b', re.IGNORECASE), '13'),
    (re.compile(r'\bfourteen\b|\bcatorce\b', re.IGNORECASE), '14'),
    (re.compile(r'\bfifteen\b|\bquince\b', re.IGNORECASE), '15'),
    (re.compile(r'\bsixteen\b|\bdieciseis\b', re.IGNORECASE), '16'),
    (re.compile(r'\bseventeen\b|\bdiecisiete\b', re.IGNORECASE), '17'),
    (re.compile(r'\beighteen\b|\bdieciocho\b', re.IGNORECASE), '18'),
    (re.compile(r'\bnineteen\b|\bdiecinueve\b', re.IGNORECASE), '19'),
    (re.compile(r'\btwenty\b|\bveinte\b', re.IGNORECASE), '20'),
    (re.compile(r'\bthirty\b|\btreinta\b', re.IGNORECASE), '30'),
    (re.compile(r'\bforty\b|\bcuarenta\b', re.IGNORECASE), '40'),
    (re.compile(r'\bfifty\b|\bcincuenta\b', re.IGNORECASE), '50'),
    (re.compile(r'\bhundred\b|\bcien\b|\bciento\b', re.IGNORECASE), '100'),
    (re.compile(r'\bthousand\b|\bmil\b', re.IGNORECASE), '1000'),
]

def _normalize_number_words(s):
    if not s: return ''
    for pat, rep in _WORD_NUMBERS:
        s = pat.sub(rep, s)
    return s

_SPECIAL_CHAR_MAP = {
    'ß': 'ss', 'ẞ': 'ss',
    'æ': 'ae', 'Æ': 'ae',
    'œ': 'oe', 'Œ': 'oe',
    'ø': 'o',  'Ø': 'o',
    'ł': 'l',  'Ł': 'l',
    'đ': 'd',  'Đ': 'd',
    'ð': 'd',  'Ð': 'd',
    'þ': 'th', 'Þ': 'th',
    'ı': 'i',  'İ': 'i',
}

_ARTICLES_DEFINITE = frozenset([
    'the', 'el', 'la', 'los', 'las', 'le', 'les', 'der', 'die', 'das', 'het',
])
_ARTICLES_INDEFINITE = frozenset([
    'a', 'an', 'un', 'una', 'unos', 'unas', 'ein', 'eine',
])
_ALL_ARTICLES = _ARTICLES_DEFINITE | _ARTICLES_INDEFINITE

def _strip_all_articles(s):
    return ' '.join(w for w in s.split() if w not in _ALL_ARTICLES)

def _has_conflicting_articles(s1, s2):
    """Comprueba si dos títulos tienen artículos distintos en la misma posición.
    Permite omisiones puras (ej. '2001 una odisea' vs '2001 odisea', 'the office' vs 'office').
    Rechaza discrepancias de artículos opuestos (ej. 'una' vs 'la', 'the' vs 'a', 'un' vs 'el')."""
    tokens1 = s1.split()
    tokens2 = s2.split()
    core1 = [w for w in tokens1 if w not in _ALL_ARTICLES]
    core2 = [w for w in tokens2 if w not in _ALL_ARTICLES]
    if core1 != core2:
        return False

    def get_article_slots(tokens):
        slots = []
        cur_art = []
        for w in tokens:
            if w in _ALL_ARTICLES:
                cur_art.append(w)
            else:
                slots.append(tuple(cur_art))
                cur_art = []
        slots.append(tuple(cur_art))
        return slots

    slots1 = get_article_slots(tokens1)
    slots2 = get_article_slots(tokens2)
    if len(slots1) != len(slots2):
        return False
    for art1, art2 in zip(slots1, slots2):
        if art1 and art2:
            if art1 != art2:
                return True
    return False

def clean_title(title_str):
    """Normalize a title for fuzzy comparison.

    Handles: Kodi tags, brackets/parens, quality flags, & and + → and,
    Foreign ligatures/characters (ß, æ, ø, ł, etc.),
    Unicode accents (all scripts via NFD), possessives, roman numerals.
    Result is lowercase ASCII alphanumeric words joined by single spaces.
    """
    title_str = _safe_str(title_str)
    if not title_str: return ''
    # 1. Strip Kodi markup tags
    c = re.sub(r'\[/?(?:COLOR[^\]]*|B|I|UPPERCASE|LOWERCASE|LIGHT|CR)\]', '', title_str, flags=re.IGNORECASE)
    # 2. Strip content inside brackets / parens / braces (year, quality, etc.)
    c = re.sub(r'[\(\[\{][^\)\]\}]{0,80}[\)\]\}]', '', c)
    # 3. Strip quality / language / codec keywords
    c = re.sub(
        r'(?i)\b(4k|uhd|2160p|1080p|720p|480p|360p|'
        r'bluray|blu-ray|bdrip|brrip|hdrip|hdtv|dvdrip|dvdscr|'
        r'web-dl|webdl|webrip|web-rip|'
        r'x264|x265|h264|h265|hevc|avc|xvid|divx|'
        r'latino|castellano|espa[ñn]ol|subtitulado|dual|audio|'
        r'hdr|sdr|dts|aac|ac3|mp3|'
        r'extended|remastered|unrated|theatrical|directors.cut)\b',
        '', c)
    # 4. Normalize & and + to and
    c = re.sub(r'\s*[&+]\s*', ' and ', c)
    # 5. Map special ligatures / foreign letters that NFKD does not decompose
    for ch, repl in _SPECIAL_CHAR_MAP.items():
        if ch in c:
            c = c.replace(ch, repl)
    # 6. Unicode NFKD decomposition (handles European/Latin accents)
    try:
        import unicodedata as _ud
        nfkd = _ud.normalize('NFKD', c)
        c = ''.join(ch for ch in nfkd if not _ud.combining(ch))
    except Exception:
        pass
    c = c.lower()
    # 7. Possessives and apostrophes: "Pan's" → "pans", "it's" → "its"
    c = re.sub(r"[\u2019\u2018\u0027\u0060\u00b4]s\b", 's', c)
    c = re.sub(r"[\u2019\u2018\u0027\u0060\u00b4]", '', c)
    # 8. Keep only ASCII alphanumeric; everything else → space
    c = re.sub(r'[^a-z0-9]', ' ', c)
    return ' '.join(c.split())

# Unificacion de conjunciones entre idiomas (y/and/e/&). Ej. 'Tom y Jerry'
# casa con 'Tom & Jerry' porque clean_title ya mapea & -> and.
# Mecanismo GENERAL sin diccionarios: no hay lista de palabras que mantener.
# (Distinguir traducciones como Wolverine/Lobezno sin datos externos es
# imposible localmente; para eso esta el titulo original de TMDb.)
_TITLE_CONJUNCTIONS = frozenset(['y', 'and', 'e'])

def _alias_variants(names):
    """Genera variantes con conjunciones unificadas a 'and'.
    Solo ANIADE candidatos: jamas quita ni rechaza nada, asi que no puede
    romper matches que ya funcionaban."""
    out = []
    try:
        seen = set()
        for _n in (names or []):
            _s = _safe_str(_n).lower().strip()
            if _s:
                seen.add(_s)
        for name in (names or []):
            base = _safe_str(name)
            if not base:
                continue
            toks = clean_title(base).split()
            if not toks:
                continue
            core = [('and' if t in _TITLE_CONJUNCTIONS else t) for t in toks]
            c = ' '.join(core)
            if c and c not in seen:
                seen.add(c)
                out.append(c)
    except Exception:
        pass
    return out

def _extract_year_from_val(val):
    if not val: return None
    s = str(val).strip()
    m = re.search(r'\b(19\d\d|20\d\d)\b', s)
    if m:
        try:
            y = int(m.group(1))
            if 1900 <= y <= 2099:
                return y
        except:
            pass
    return None

def _extract_title_embedded_years(target_titles):
    """Finds 4-digit numbers that are part of target titles (e.g. 1917, 1984 in Wonder Woman 1984)."""
    embedded = set()
    for t in (target_titles or []):
        if t:
            for y in re.findall(r'\b(19\d\d|20\d\d)\b', str(t)):
                embedded.add(int(y))
    return embedded

def _get_item_year(item=None, title_str=None, embedded_title_years=None):
    """Extract a 4-digit year (1900-2099) as integer, or None, avoiding title embedded numbers."""
    embedded = embedded_title_years or set()
    # 1. From item.infoLabels
    if item:
        _il = getattr(item, 'infoLabels', {}) if hasattr(item, 'infoLabels') else {}
        if isinstance(_il, dict):
            for k in ('year', 'release_date', 'premiered', 'aired'):
                y = _extract_year_from_val(_il.get(k))
                if y and y not in embedded:
                    return y
        for attr in ('year', 'contentYear'):
            y = _extract_year_from_val(getattr(item, attr, None))
            if y and y not in embedded:
                return y
    # 2. From explicit year tags in title: (YYYY) or [YYYY]
    for t in (title_str, getattr(item, 'title', None) if item else None, getattr(item, 'contentTitle', None) if item else None):
        if t:
            m = re.search(r'[\(\[]\s*(19\d\d|20\d\d)\s*[\)\]]', str(t))
            if m:
                return int(m.group(1))
    # 3. From trailing year in title: ONLY if not an embedded title year
    for t in (title_str, getattr(item, 'title', None) if item else None, getattr(item, 'contentTitle', None) if item else None):
        if t:
            m = re.search(r'(?:\s*-\s*|\s+)(19\d\d|20\d\d)\s*$', str(t))
            if m:
                y = int(m.group(1))
                if y not in embedded:
                    return y
    # 4. From URL: ONLY if not an embedded title year
    if item:
        url = _safe_str(getattr(item, 'url', ''))
        if url:
            for m in re.finditer(r'[-_/(](19\d\d|20\d\d)(?:[-_/.)]|$)', url):
                y = int(m.group(1))
                if y not in embedded:
                    return y
    return None

def _get_item_tmdb(item):
    if not item: return None
    _il = getattr(item, 'infoLabels', {}) if hasattr(item, 'infoLabels') else {}
    if isinstance(_il, dict):
        for k in ('tmdb_id', 'tmdb'):
            val = _safe_str(_il.get(k))
            if val.isdigit() and int(val) > 0:
                return val
    for attr in ('tmdb_id', 'tmdb'):
        val = _safe_str(getattr(item, attr, ''))
        if val.isdigit() and int(val) > 0:
            return val
    url = _safe_str(getattr(item, 'url', ''))
    if url:
        m = re.search(r'[?&](?:tmdb_id|tmdb)=(\d+)', url)
        if m and int(m.group(1)) > 0:
            return m.group(1)
        ch = _safe_str(getattr(item, 'channel', '')).lower()
        if any(x in ch for x in ('poseidon', 'cuevana', 'pelisplus', 'estrenosgo', 'pelisforte')):
            m = re.search(r'/(?:pelicula|serie|series|tv|movie)/(\d{2,9})(?:/|[-_]|$)', url)
            if m and int(m.group(1)) > 0:
                return m.group(1)
        else:
            m = re.search(r'/(?:pelicula|serie|series|tv|movie)/(\d{2,9})/(?:[^/?#]+)', url)
            if m and int(m.group(1)) > 0:
                return m.group(1)
    return None

def _get_item_imdb(item):
    if not item: return None
    _il = getattr(item, 'infoLabels', {}) if hasattr(item, 'infoLabels') else {}
    if isinstance(_il, dict):
        for k in ('imdb_id', 'imdb', 'IMDBNumber'):
            val = _safe_str(_il.get(k)).lower()
            if val.startswith('tt'): return val
    for attr in ('imdb_id', 'imdb'):
        val = _safe_str(getattr(item, attr, '')).lower()
        if val.startswith('tt'): return val
    url = _safe_str(getattr(item, 'url', '')).lower()
    if url:
        m = re.search(r'(tt\d{6,10})', url)
        if m: return m.group(1)
    return None

def _clean_search_term(term):
    term = _safe_str(term)
    if not term: return ""
    term = re.sub(r'\s*\(\d{4}\)', '', term)
    term = re.sub(r'[\:\-\_]', ' ', term)
    term = re.sub(r'\s+', ' ', term)
    return term.strip().lower()

def get_core_title(t):
    t = _safe_str(t)
    if not t: return ''
    core = re.sub(r'\s*\(\d{4}\)', '', t)
    core = re.split(r'[:\-–—]', core)[0]
    return core.strip()

def _extract_title_number(title_str):
    title_str = _safe_str(title_str)
    if not title_str: return None
    s = _normalize_number_words(title_str.lower())
    m = re.search(r'\b(?:volumen|vol\.?|parte|part)\s*(\d+)\b', s)
    if m: return int(m.group(1))
    m = re.search(r'\b(?:volumen|vol\.?|parte|part)\s*(iv|v|vi|vii|viii|ix|x|iii|ii|i)\b', s)
    if m:
        romans = {'i': 1, 'ii': 2, 'iii': 3, 'iv': 4, 'v': 5, 'vi': 6, 'vii': 7, 'viii': 8, 'ix': 9, 'x': 10}
        return romans.get(m.group(1))
    m = re.search(r'\b(\d+)\b', s)
    if m: return int(m.group(1))
    m = re.search(r'\b(iv|v|vi|vii|viii|ix|x|iii|ii)\b', s)
    if m:
        romans = {'ii': 2, 'iii': 3, 'iv': 4, 'v': 5, 'vi': 6, 'vii': 7, 'viii': 8, 'ix': 9, 'x': 10}
        return romans.get(m.group(1))
    return None

def is_slug_or_raw_consistent(raw_title, target_title, target_year, item=None, embedded_years=None):
    raw_title = _safe_str(raw_title)
    target_title = _safe_str(target_title)
    if not raw_title or not target_title: return True
    raw_sub = re.sub(r'\s*\(\d{4}\)', '', raw_title)
    tgt_sub = re.sub(r'\s*\(\d{4}\)', '', target_title)
    raw_has_colon = ':' in raw_sub or ' - ' in raw_sub or '–' in raw_sub
    tgt_has_colon = ':' in tgt_sub or ' - ' in tgt_sub or '–' in tgt_sub
    if tgt_has_colon and not raw_has_colon:
        tgt_parts = re.split(r'[:\-–—]', tgt_sub)
        if len(tgt_parts) > 1:
            subtitle = clean_title(tgt_parts[1])
            if subtitle and len(subtitle) >= 3:
                raw_c = clean_title(raw_sub)
                if subtitle not in raw_c:
                    raw_y_match = _get_item_year(item, raw_title, embedded_title_years=embedded_years)
                    if raw_y_match and target_year:
                        if abs(int(raw_y_match) - int(target_year)) > 1: return False
    raw_num = _extract_title_number(raw_sub)
    tgt_num = _extract_title_number(tgt_sub)
    if raw_num is not None or tgt_num is not None:
        if raw_num != tgt_num: return False
    return True

def score_match(result_title, target_year, all_names, target_tmdb=None, item=None, target_imdb=None, is_series=None):
    """Calculates a match confidence score (integer > 0) or returns 0 if rejected.

    Higher score indicates greater precision and relevance.
    Scoring components:
      - TMDb match: +10000 (mismatch: REJECT)
      - IMDb match: +8000  (mismatch: REJECT)
      - Exact Year match: +3000
      - Off-by-1 Year: +2000
      - Year mismatch (>1): REJECT (0)
      - Exact Clean Title match: +1000
      - Compact de-spaced match (hyphens/dots/acronyms): +950
      - Number words & Roman numerals normalized match: +900
      - All articles stripped match: +880
      - Article/Numeral normalized match: +800
      - Subtitle prefix match: +800
      - Substring match: +300
      - Media type compatibility (movie vs show): +500
      - Title similarity is MANDATORY: with no title match the result is
        REJECTED (0), even with TMDb/IMDb/year (channel items inherit the
        search-item IDs via Item.clone, so matching IDs alone prove nothing).
    """
    result_title = _safe_str(result_title)
    if not result_title: return 0
    low_title = result_title.lower().strip()

    if any(p in low_title for p in ['siguiente', 'siguientes', 'next page', 'pagina siguiente', 'página siguiente', 'anterior', 'anteriores']) or low_title.startswith('>>') or low_title.startswith('<<') or low_title.endswith('>>') or low_title.endswith('<<'):
        return 0

    url_str = _safe_str(getattr(item, 'url', '')).lower() if item else ''
    content_type = _safe_str(getattr(item, 'contentType', '')).lower() if item else ''

    if is_series is False:
        # Searching for movie
        if content_type in ('tvshow', 'season', 'episode'):
            return 0
        if '/serie/' in url_str or '/series/' in url_str or '/temporada/' in url_str:
            return 0
        if re.search(r'\b(?:temporada\s*\d+|capitulo\s*\d+|season\s*\d+|episodio\s*\d+)\b', low_title):
            return 0
    elif is_series is True:
        # Searching for series
        if content_type == 'movie' and not getattr(item, 'contentSerieName', ''):
            return 0
        if '/pelicula/' in url_str and not ('/serie/' in url_str or '/temporada/' in url_str):
            return 0

    # 2. TMDb Check
    item_tmdb = _get_item_tmdb(item)
    tmdb_score = 0
    if target_tmdb and item_tmdb:
        if str(target_tmdb) == str(item_tmdb):
            tmdb_score = 10000
        else:
            # Explicit TMDb conflict!
            return 0

    # 3. IMDb Check
    item_imdb = _get_item_imdb(item)
    imdb_score = 0
    if target_imdb and item_imdb:
        if str(target_imdb).lower() == str(item_imdb).lower():
            imdb_score = 8000
        else:
            return 0

    # 4. Year Check (avoid treating title embedded numbers like 1917, 1984 as release years)
    embedded_years = _extract_title_embedded_years(all_names)
    item_year = _get_item_year(item, result_title, embedded_title_years=embedded_years)
    year_score = 0
    if item_year and target_year:
        try:
            y_diff = abs(int(item_year) - int(target_year))
            if y_diff == 0:
                year_score = 3000
            elif y_diff == 1:
                year_score = 2000
            else:
                # Strong rejection for year mismatch (e.g. 1984 vs 2026)
                return 0
        except:
            pass

    clean_res = clean_title(result_title)
    if not clean_res: return 0

    _cr_na       = _strip_leading_article(clean_res)
    _cr_nr       = _normalize_numerals(clean_res)
    _cr_nw       = _normalize_number_words(clean_res)
    _cr_nw_nr    = _normalize_numerals(_cr_nw)
    _cr_compact  = clean_res.replace(' ', '')
    _cr_nw_comp  = _cr_nw_nr.replace(' ', '')
    _cr_all_art  = _strip_all_articles(clean_res)
    _cr_art_comp = _cr_all_art.replace(' ', '')

    best_title_score = 0

    for name in (all_names or []):
        name = _safe_str(name)
        if not name: continue
        clean_target = clean_title(name)
        if not clean_target: continue

        if not is_slug_or_raw_consistent(result_title, name, target_year, item=item, embedded_years=embedded_years):
            continue

        _ct_na       = _strip_leading_article(clean_target)
        _ct_nr       = _normalize_numerals(clean_target)
        _ct_nw       = _normalize_number_words(clean_target)
        _ct_nw_nr    = _normalize_numerals(_ct_nw)
        _ct_compact  = clean_target.replace(' ', '')
        _ct_nw_comp  = _ct_nw_nr.replace(' ', '')
        _ct_all_art  = _strip_all_articles(clean_target)
        _ct_art_comp = _ct_all_art.replace(' ', '')

        # 1. Exact clean match
        if clean_res == clean_target:
            best_title_score = max(best_title_score, 1000)
            continue

        # 2. Compact match (handles hyphens, dots, spaces: Spider-Man/Spiderman, S.W.A.T./SWAT, Kick-Ass/Kickass, WALL·E/Wall-E)
        if _cr_compact == _ct_compact and len(_cr_compact) >= 3:
            best_title_score = max(best_title_score, 950)
            continue

        # 3. Number words / Roman numerals normalized (12 Monkeys/Twelve Monkeys, Ocean's 11/Ocean's Eleven, Rocky II/Rocky 2)
        if _cr_nw_nr == _ct_nw_nr or _cr_nr == _ct_nr:
            best_title_score = max(best_title_score, 900)
            continue

        # 4. All articles stripped match (e.g. 2001: Una odisea del espacio vs 2001: Odisea del espacio)
        if _cr_all_art and _ct_all_art and (_cr_all_art == _ct_all_art or (_cr_art_comp == _ct_art_comp and len(_cr_art_comp) >= 3)):
            if not _has_conflicting_articles(clean_res, clean_target):
                best_title_score = max(best_title_score, 880)
                continue

        # 5. Compact + Number words normalized
        if _cr_nw_comp == _ct_nw_comp and len(_cr_nw_comp) >= 3:
            best_title_score = max(best_title_score, 850)
            continue

        # 6. Article-stripped match
        if (_cr_na == _ct_na and _cr_na) or (_cr_na.replace(' ', '') == _ct_na.replace(' ', '') and len(_cr_na) >= 3):
            if not _has_conflicting_articles(clean_res, clean_target):
                best_title_score = max(best_title_score, 800)
                continue

        # 7. Subtitle split handling: "Supergirl: Woman of Tomorrow" vs "Supergirl", "Dr. Strangelove or:..." vs "Dr. Strangelove"
        if ':' in name or ' - ' in name or '–' in name:
            parts = re.split(r'[:\-–—]', name)
            if len(parts) > 1:
                prefix = clean_title(parts[0])
                prefix_comp = prefix.replace(' ', '')
                if (prefix and clean_res == prefix) or (prefix_comp and _cr_compact == prefix_comp):
                    if tmdb_score > 0:
                        best_title_score = max(best_title_score, 1000)
                    elif year_score > 0:
                        best_title_score = max(best_title_score, 800)
                    elif target_year is None:
                        best_title_score = max(best_title_score, 500)
                    continue

        if ':' in result_title or ' - ' in result_title or '–' in result_title:
            res_parts = re.split(r'[:\-–—]', result_title)
            if len(res_parts) > 1:
                res_prefix = clean_title(res_parts[0])
                res_pref_comp = res_prefix.replace(' ', '')
                if (res_prefix and res_prefix == clean_target) or (res_pref_comp and res_pref_comp == _ct_compact):
                    if tmdb_score > 0:
                        best_title_score = max(best_title_score, 1000)
                    elif year_score > 0:
                        best_title_score = max(best_title_score, 800)
                    elif target_year is None:
                        best_title_score = max(best_title_score, 500)
                    continue

        # 8. Substring match (palabra completa estricta; sin letras/palabras extra no autorizadas)
        for _rv, _tv in [(clean_res, clean_target), (_cr_na, _ct_na)]:
            if not _rv or not _tv or _rv == _tv: continue
            _sub_fwd = bool(re.search(r'\b' + re.escape(_tv) + r'\b', _rv))
            _sub_rev = bool(re.search(r'\b' + re.escape(_rv) + r'\b', _tv))
            if not _sub_fwd and not _sub_rev: continue

            _wc_res = len(_rv.split())
            _wc_tgt = len(_tv.split())
            _same_wc = _wc_res == _wc_tgt

            # Si el resultado tiene palabras extra respecto al objetivo (ej. 'Avatar Extended' vs 'Avatar')
            if _sub_fwd and not _same_wc:
                _pos = _rv.find(_tv)
                _extra = (_rv[:_pos].strip() + ' ' + _rv[_pos + len(_tv):].strip()).strip()
                if _extra and any(ch.isalpha() for ch in _extra):
                    continue

            # Si el objetivo tiene palabras extra respecto al resultado (ej. 'Heart of the Beast' vs 'Beast')
            # NUNCA permitir que una película distinta con un título más corto ('Beast') coincida con ('Heart of the Beast')
            if _sub_rev and not _same_wc:
                _pos = _tv.find(_rv)
                _extra = (_tv[:_pos].strip() + ' ' + _tv[_pos + len(_rv):].strip()).strip()
                if _extra and any(ch.isalpha() for ch in _extra):
                    continue

            if year_score > 0 or tmdb_score > 0 or _same_wc:
                best_title_score = max(best_title_score, 300)
                break

    # El titulo es obligatorio: los resultados de canal heredan los IDs del
    # item de busqueda via Item.clone() (ej. allcalidad clona it_search con
    # tmdb_id/imdb_id inyectados), asi que IDs coincidentes sin similitud de
    # titulo no prueban nada ('Pinky y Cerebro' llegaba a 21500 con el tmdb
    # heredado). Sin titulo -> RECHAZO aunque haya IDs/anio.
    if best_title_score == 0:
        return 0

    total_score = tmdb_score + imdb_score + year_score + best_title_score
    if is_series is False and ('/pelicula/' in url_str or content_type == 'movie'):
        total_score += 500
    elif is_series is True and ('/serie/' in url_str or content_type == 'tvshow'):
        total_score += 500

    return total_score

def match_title(result_title, target_year, all_names, target_tmdb=None, item=None, target_imdb=None, is_series=None):
    """Return True if result_title matches any name in all_names, considering year and IDs."""
    return score_match(result_title, target_year, all_names, target_tmdb=target_tmdb, item=item, target_imdb=target_imdb, is_series=is_series) > 0


# ---------------------------------------------------------
# Item Formatting & Quality / Server / Language Helpers
# ---------------------------------------------------------
def _is_torrent_link(link):
    if not link: return False

    # 1. Server check
    srv = _safe_str(getattr(link, 'server', '')).lower().strip()
    if srv:
        if srv in ('torrent', 'magnet', 'torrents', 'bittorrent', 'utorrent', 'elementum'):
            return True
        if 'torrent' in srv or 'magnet' in srv:
            return True

    # 2. URL check
    url_str = _safe_str(getattr(link, 'url', '')).lower().strip()
    if url_str:
        if url_str.startswith('magnet:') or 'magnet:?' in url_str or 'xt=urn:btih' in url_str or 'urn:btih:' in url_str:
            return True
        if '.torrent' in url_str:
            return True

    # 3. Action check
    act = _safe_str(getattr(link, 'action', '')).lower().strip()
    if act in ('play_torrent', 'torrent', 'download_torrent'):
        return True

    # 4. Extra / Stream type / Format / Type check
    extra = _safe_str(getattr(link, 'extra', '')).lower()
    if 'torrent' in extra:
        return True
    st_type = _safe_str(getattr(link, 'stream_type', '')).lower()
    if 'torrent' in st_type:
        return True
    l_type = _safe_str(getattr(link, 'type', '')).lower()
    if 'torrent' in l_type:
        return True

    # 5. Channel check (canales torrent conocidos en Balandro y Alfa)
    ch = _safe_str(getattr(link, 'channel', '')).lower().strip()
    if ch:
        if 'torrent' in ch or 'divx' in ch:
            return True
        torrent_channels = (
            'dontorrent', 'dontorrents', 'dontorrentsin', 'dontorrent21',
            'elitetorrent', 'elitetorrentnz', 'grantorrent', 'hacktorrent',
            'mejortorrent', 'mejortorrentapp', 'mejortorrentin', 'mitorrent',
            'pasateatorrent', 'pediatorrent', 'subtorrents', 'todotorrents',
            'torrentgalaxy', 'vivatorrents', 'divxtotal', 'divxatope',
            'tomadivx', 'eztv', 'yts', 'pelitorrent', 'yestorrent',
            'wolfmax4k', 'moviesdvdr', 'reinventorrent'
        )
        if ch in torrent_channels:
            return True

    # 6. video_urls check
    video_urls = getattr(link, 'video_urls', None)
    if video_urls and isinstance(video_urls, list):
        for v in video_urls:
            v_str = str(v).lower()
            if 'magnet:' in v_str or '.torrent' in v_str or '[torrent]' in v_str:
                return True

    # 7. other / title check
    other = _safe_str(getattr(link, 'other', '')).lower()
    if '[torrent]' in other or '(torrent)' in other or other.startswith('torrent'):
        return True

    return False


def _get_torrent_client():
    """Lee el ajuste 'cliente_torrent' de Balandro y devuelve (addon_id, url_template).

    url_template usa %s como placeholder para la URL/magnet codificada.
    Retorna (None, None) si no hay cliente configurado o no está instalado.
    """
    try:
        bal_addon = xbmcaddon.Addon('plugin.video.balandro')
        cliente = (bal_addon.getSetting('cliente_torrent') or '').strip().lower()
        if not cliente or cliente in ('ninguno', 'seleccionar', ''):
            return None, None

        # Leer torrent.json de Balandro para obtener la URL template
        bal_path = bal_addon.getAddonInfo('path')
        torrent_json_path = os.path.join(bal_path, 'servers', 'torrent.json')
        if not os.path.exists(torrent_json_path):
            return None, None

        import json as _json
        with open(torrent_json_path, 'r', encoding='utf-8') as _f:
            _data = _json.load(_f)

        for client in _data.get('clients', []):
            if client.get('name', '').lower() == cliente:
                addon_id = client.get('id', '')
                if addon_id and xbmc.getCondVisibility('System.HasAddon("%s")' % addon_id):
                    return addon_id, client.get('url', '')
                else:
                    xbmc.log('Multi Bridge: cliente torrent "%s" (%s) no instalado' % (cliente, addon_id), xbmc.LOGINFO)
                    return None, None

        xbmc.log('Multi Bridge: cliente torrent "%s" no encontrado en torrent.json' % cliente, xbmc.LOGINFO)
        return None, None

    except Exception as _e:
        xbmc.log('Multi Bridge: _get_torrent_client error: ' + str(_e), xbmc.LOGINFO)
        return None, None


# Segundos maximos esperando al play() del canal al resolver un torrent.
_TORRENT_PLAY_BUDGET = 25

def _resolve_torrent_url(link, engine='balandro'):
    """Resuelve la URL real (magnet/.torrent) de un enlace torrent.
    1) Decodifica data_url (cinecalidad* guardan ahi el magnet con url vacia).
    2) Si sigue sin ser magnet/.torrent, pide al play() del canal con un
       presupuesto de _TORRENT_PLAY_BUDGET segundos y dialogo de progreso
       (el play() del canal puede colgarse en red sin avisar; antes eso
       dejaba a Kodi en silencio).
    Devuelve (url, detalle_fallo). No lanza reproduccion."""
    fail_detail = ''
    try:
        prep = _prepare_playable_link(link, engine=engine)
    except Exception:
        prep = None
    torrent_url = _safe_str(getattr(prep, 'url', '') or '') if prep is not None else ''
    if not torrent_url:
        torrent_url = _safe_str(getattr(link, 'url', '') or '')
    if (not torrent_url or (not torrent_url.startswith('magnet:') and not torrent_url.endswith('.torrent'))) \
            and _safe_str(getattr(link, 'data_url', '') or ''):
        try:
            _switch_engine_environment(engine)
            _ch_dbg = _safe_str(getattr(link, 'channel', '') or '').strip()
            if _ch_dbg:
                _ch_mod = __import__('channels.' + _ch_dbg, fromlist=[''])
                if hasattr(_ch_mod, 'play'):
                    _play_holder = [None]
                    _play_err = [None]
                    def _do_play():
                        try:
                            with silenced_dialogs():
                                _play_holder[0] = _ch_mod.play(link.clone() if hasattr(link, 'clone') else link)
                        except Exception as _ex:
                            _play_err[0] = _ex
                    _pt = threading.Thread(target=_do_play, daemon=True)
                    _pt.start()
                    try:
                        _pdlg = xbmcgui.DialogProgressBG()
                        _pdlg.create('Multi Bridge', 'Resolviendo torrent (%s)...' % _ch_dbg)
                    except Exception:
                        _pdlg = None
                    _deadline = time.time() + _TORRENT_PLAY_BUDGET
                    try:
                        while _pt.is_alive() and time.time() < _deadline:
                            if xbmc.Monitor().abortRequested():
                                break
                            time.sleep(0.2)
                    except Exception:
                        pass
                    try:
                        if _pdlg:
                            _pdlg.close()
                    except Exception:
                        pass
                    if _pt.is_alive():
                        xbmc.log("Multi Bridge: torrent %s canal.play supero %ds, se abandona" % (_ch_dbg, _TORRENT_PLAY_BUDGET), xbmc.LOGWARNING)
                        fail_detail = 'El canal tardó demasiado en responder'
                        _p_res = None
                    elif _play_err[0] is not None:
                        xbmc.log("Multi Bridge: torrent canal.play error: %s" % _play_err[0], xbmc.LOGINFO)
                        _p_res = None
                    else:
                        _p_res = _play_holder[0]
                    if _p_res is not None:
                        if isinstance(_p_res, str) and _p_res.strip():
                            # El canal devuelve texto (ej. 'Tiene Acortador del enlace')
                            fail_detail = re.sub(r'\[/?COLOR[^\]]*\]|\[/?B\]|\[CR\]', '', _p_res).strip()
                        else:
                            _cand = None
                            if isinstance(_p_res, list) and _p_res:
                                _cand = _p_res[0]
                                if isinstance(_cand, list):
                                    _cand = _cand[0] if _cand else None
                            elif hasattr(_p_res, 'url'):
                                _cand = _p_res
                            if _cand is not None and _safe_str(getattr(_cand, 'url', '') or ''):
                                torrent_url = _safe_str(getattr(_cand, 'url', '') or '')
                                xbmc.log("Multi Bridge: torrent %s resuelto via canal.play (%s...)" % (_ch_dbg, torrent_url[:30]), xbmc.LOGINFO)
        except Exception as _e:
            xbmc.log("Multi Bridge: torrent canal.play error: %s" % _e, xbmc.LOGINFO)
    return torrent_url, fail_detail

def _play_torrent_link(torrent_url, matched_item=None, meta=None):
    """Reproduce un link torrent usando el cliente configurado en Balandro.

    Enriquece la URL con datos TMDb (tmdb_id, type, season, episode, show)
    para que Elementum/Quasar puedan identificar el contenido correctamente.

    Returns True si se lanzó la reproducción, False si no hay cliente configurado.
    """
    if not torrent_url:
        return False

    # Defensa: solo magnets o .torrent llegan al cliente (una pagina web o un
    # acortador darian "Invalid input" en Elementum).
    _tl = torrent_url.strip()
    if not (_tl.startswith('magnet:') or _tl.endswith('.torrent')):
        xbmc.log('Multi Bridge: _play_torrent_link rechaza URL no-torrent: %s...' % _tl[:80], xbmc.LOGWARNING)
        return False
    torrent_url = _tl

    _tor_id, _tor_tpl = _get_torrent_client()
    if not (_tor_id and _tor_tpl):
        xbmcgui.Dialog().notification(
            'Multi Bridge',
            'Configura un cliente torrent en Ajustes → Balandro → Torrents',
            '', 5000)
        return False

    # Elegir url o url_magnet según tipo de enlace
    _is_magnet = torrent_url.startswith('magnet:')

    # Si el template no acepta magnets y tenemos magnet, intentar url_magnet
    # (ya resuelto: _tor_tpl viene del json y tiene la URL correcta para el tipo)
    encoded = uparse.quote_plus(torrent_url)
    play_url = _tor_tpl % encoded

    if not meta:
        _cf = _read_search_cache_atomic()
        if _cf and isinstance(_cf, dict):
            meta = _cf.get('meta', {}) or {}

    # ── Enriquecimiento TMDb para Elementum / Quasar ───────────────────────
    _tor_name = _tor_id.rsplit('.', 1)[-1].lower()   # 'elementum', 'quasar', ...
    if _tor_name in ('elementum', 'quasar'):
        _tmdb   = ''
        _ctype  = 'movie'
        _season = ''
        _epnum  = ''
        _show   = ''

        # Fuente 1: matched_item (Item de Balandro)
        if matched_item:
            _info  = getattr(matched_item, 'infoLabels', {}) or {}
            _tmdb  = str(_info.get('tmdb_id', '') or '')
            _ctype = str(getattr(matched_item, 'contentType', '') or 'movie')
            _season= str(getattr(matched_item, 'contentSeason', '')
                         or _info.get('season', '') or '')
            _epnum = str(getattr(matched_item, 'contentEpisode', '')
                         or _info.get('episode', '') or '')
            _show  = str(getattr(matched_item, 'contentSerieName', '')
                         or _info.get('tvshowtitle', '') or '')

        # Fuente 2: meta dict (del handler principal)
        if not _tmdb and meta:
            _tmdb  = str(meta.get('tmdb', '') or '')
        if not _ctype or _ctype == 'movie':
            if meta and meta.get('season') and meta.get('episode'):
                _ctype = 'episode'
        if meta and not _season:
            _season = str(meta.get('season', '') or '')
        if meta and not _epnum:
            _epnum  = str(meta.get('episode', '') or '')
        if meta and not _show:
            _show   = str(meta.get('showname', '') or meta.get('title', '') or '')

        if _tmdb:
            if _ctype == 'episode' and _season and _epnum:
                # Elementum navigation getInfoLabels() expects show=<tmdb_id> when resolving episode infolabels:
                # url = "%s/show/%s/season/%s/episode/%s/infolabels" % (ELEMENTUMD_HOST, tmdb_id, query['season'][0], query['episode'][0])
                play_url += ('&episode=%s&library=&season=%s&show=%s&tmdb=%s&type=episode'
                             % (_epnum, _season, _tmdb, _tmdb))
                xbmc.log('Multi Bridge: torrent enriquecido [serie] tmdb=%s S%sE%s' % (
                    _tmdb, _season, _epnum), xbmc.LOGINFO)
            else:
                play_url += '&library=&tmdb=%s&type=movie' % _tmdb
                xbmc.log('Multi Bridge: torrent enriquecido [peli] tmdb=%s' % _tmdb, xbmc.LOGINFO)

    xbmc.log('Multi Bridge: torrent → %s' % _tor_id, xbmc.LOGINFO)
    sync_tmdbhelper_playerstring(meta)

    # Crear y enriquecer ListItem completo para que Kodi OSD y Trakt tengan los metadatos completos
    try:
        _t_label = (meta.get('title') or getattr(matched_item, 'title', '') or _show or 'Torrent') if meta or matched_item else 'Torrent'
        _li = xbmcgui.ListItem(label=_t_label)
        set_listitem_info(_li, meta=meta)
        xbmc.Player().play(play_url, _li)
    except Exception as _pe:
        xbmc.log('Multi Bridge: error reproduciendo torrent con ListItem: %s, usando fallback PlayMedia' % _pe, xbmc.LOGWARNING)
        xbmc.executebuiltin('PlayMedia(%s)' % play_url)
    return True



def _offer_elementum_search(meta):
    """Si elementum_fallback está activo, pregunta si buscar en Elementum.

    Se ejecuta después de que el handle del plugin ha sido descartado y TMDb Helper
    ha finalizado, evitando cualquier interferencia o espera de player.
    """
    has_elementum = bool(xbmc.getCondVisibility('System.HasAddon("plugin.video.elementum")'))
    fallback_enabled = (_bridge_addon.getSetting('elementum_fallback') == 'true')

    tmdb_id = str(meta.get('tmdb', '') or '')
    is_episode = bool(meta.get('season') and meta.get('episode'))
    title = str(meta.get('title') or meta.get('showname') or '')
    label = '[B]%s[/B]' % title if title else 'este contenido'

    if not fallback_enabled or not has_elementum or not tmdb_id:
        _show_bridge_notification('Multi Bridge', 'No se encontraron enlaces para %s' % (title or 'este contenido'), 3500)
        return False

    dlg = (_KODI_ORIG_DIALOG or xbmcgui.Dialog)()
    ans = dlg.yesno(
        'Multi Bridge — Sin enlaces',
        'No se encontraron enlaces para %s.\n¿Buscar en [B]Elementum[/B]?' % label,
        nolabel='No',
        yeslabel='Sí')

    if not ans:
        return False

    # URL de Elementum para buscar y mostrar la lista modal de torrents por TMDb ID
    if is_episode:
        _s = str(meta.get('season', '') or '')
        _e = str(meta.get('episode', '') or '')
        elem_url = 'plugin://plugin.video.elementum/show/%s/season/%s/episode/%s/links' % (tmdb_id, _s, _e)
    else:
        elem_url = 'plugin://plugin.video.elementum/movie/%s/links' % tmdb_id

    xbmc.log('Multi Bridge: lanzando Elementum via PlayMedia → %s' % elem_url, xbmc.LOGINFO)

    # PlayMedia asigna a Elementum un handle de reproductor real para que al elegir el torrent
    # Elementum pueda resolver e iniciar la reproducción en Kodi. Multi Bridge termina aquí su ejecución.
    sync_tmdbhelper_playerstring(meta)
    xbmc.executebuiltin('PlayMedia("%s")' % elem_url)
    return True






def _get_link_server_name(link):


    if not link: return 'Directo'
    srv = _safe_str(getattr(link, 'server', '')).strip()
    other = _safe_str(getattr(link, 'other', '')).strip()
    url_str = _safe_str(getattr(link, 'url', '')).strip().lower()

    clean_other = re.sub(r'[\(\)\[\]]', '', other).strip()
    clean_other = re.sub(r'\s+D$', '', clean_other, flags=re.IGNORECASE).strip()

    domain_map = [
        ('fastream', 'Fastream'), ('streamwish', 'Streamwish'), ('strwish', 'Streamwish'),
        ('swish', 'Streamwish'), ('audinifer', 'Streamwish'), ('hanerix', 'Streamwish'),
        ('vidhide', 'Vidhide'), ('vidhideplus', 'Vidhide'), ('vidhidepro', 'Vidhide'),
        ('voe', 'Voe'), ('voesx', 'Voe'), ('vidmoly', 'Vidmoly'), ('dood', 'Doodstream'),
        ('ds2play', 'Doodstream'), ('mixdrop', 'Mixdrop'), ('uqload', 'Uqload'),
        ('vudeo', 'Vudeo'), ('turbovid', 'Turboviplay'), ('emturbovid', 'Turboviplay'),
        ('turboviplay', 'Turboviplay'), ('filelions', 'Filelions'), ('lion', 'Filelions'),
        ('luluvdo', 'Lulustream'), ('lulustream', 'Lulustream'), ('waaw', 'Waaw'),
        ('netu', 'Waaw'), ('gamovideo', 'Gamovideo'), ('streamtape', 'Streamtape'),
        ('streamlare', 'Streamlare'), ('supervideo', 'Supervideo'), ('dropload', 'Dropload'),
        ('userload', 'Userload'), ('upstream', 'Upstream'), ('wolfstream', 'Wolfstream'),
        ('hexupload', 'Hexupload'), ('mega', 'Mega'), ('1fichier', '1fichier'),
        ('okru', 'Okru'), ('ok.ru', 'Okru'), ('kinoger', 'Kinoger')
    ]

    if not srv or srv.lower() in ('various', 'directo', 'none', ''):
        if clean_other and clean_other.lower() not in ('various', 'directo', 'none', ''):
            for d_key, d_name in domain_map:
                if d_key in clean_other.lower(): return d_name
            return clean_other.capitalize()
        for d_key, d_name in domain_map:
            if d_key in url_str: return d_name
        if _is_torrent_link(link): return 'Torrent'
        return 'Directo'

    for d_key, d_name in domain_map:
        if d_key == srv.lower() or d_key in srv.lower():
            return d_name

    if clean_other and clean_other.lower() not in ('various', 'directo', 'none', ''):
        for d_key, d_name in domain_map:
            if d_key in clean_other.lower(): return d_name

    return srv.capitalize()

def _format_language(link):
    lang = getattr(link, 'language', '')
    if isinstance(lang, list): lang = ' / '.join([_safe_str(l) for l in lang])
    lang = _safe_str(lang).strip()
    title_str = _safe_str(getattr(link, 'title', ''))
    combined = (lang + ' ' + title_str).lower()
    if 'lat' in combined or 'latino' in combined or 'dual' in combined: return 'LATINO'
    if 'cast' in combined or 'esp' in combined or 'castellano' in combined or 'español' in combined: return 'CASTELLANO'
    if 'vose' in combined or 'sub' in combined or 'vos' in combined: return 'VOSE'
    if 'eng' in combined or 'ingles' in combined: return 'INGLES'
    return 'LATINO' if not lang else lang.upper()

def _get_link_language_group(link):
    f_lang = _format_language(link)
    if 'LAT' in f_lang: return 'LAT'
    if 'CAST' in f_lang or 'ESP' in f_lang: return 'ESP'
    if 'VOSE' in f_lang or 'SUB' in f_lang: return 'VOSE'
    return 'OTHER'

def _has_4k_token(combined):
    """Detecta 4K real (4K/2160/UHD como palabra) sin picar con IDs
    aleatorios de embeds/magnets que contienen esas letras
    (ej. '...f4klam...' no es 4K)."""
    try:
        if re.search(r'\b4\s?K\b', combined): return True
        if re.search(r'\b2160P?\b', combined): return True
        if re.search(r'\bUHD\b', combined): return True
    except Exception:
        pass
    return False

def _format_quality(link):
    qual = _safe_str(getattr(link, 'quality', '')).strip()
    title_str = _safe_str(getattr(link, 'title', '')).strip()
    url_str = _safe_str(getattr(link, 'url', '')).strip()
    combined = (qual + ' ' + title_str + ' ' + url_str).upper()
    if _is_torrent_link(link):
        # Torrents: conservar fuente (WEB-DL vs WEBRip importa) + resolucion.
        # Ej. 'WEB-DL 1080p', 'Dual 1080p', 'WEBRip', 'Dual 720p'.
        parts = []
        _has_src = False
        if 'WEB-DL' in combined or 'WEBDL' in combined or 'WEB DL' in combined:
            parts.append('WEB-DL'); _has_src = True
        elif 'WEBRIP' in combined or 'WEB-RIP' in combined or 'WEB RIP' in combined:
            parts.append('WEBRip'); _has_src = True
        elif 'BLURAY' in combined or 'BLU-RAY' in combined or 'BRRIP' in combined or 'BDRIP' in combined:
            parts.append('BluRay'); _has_src = True
        elif 'DVDRIP' in combined or ('DVD' in combined and 'RIP' in combined):
            parts.append('DVDRip'); _has_src = True
        elif 'DVD' in combined:
            parts.append('DVD'); _has_src = True
        elif 'HDTV' in combined:
            parts.append('HDTV'); _has_src = True
        elif 'CAM' in combined or re.search(r'\bTS\b', combined) or 'TELESYNC' in combined:
            parts.append('CAM'); _has_src = True
        if 'DUAL' in combined:
            parts.append('Dual')
        if _has_4k_token(combined):
            parts.append('4K')
        elif '1080P' in combined or 'FULLHD' in combined or 'FHD' in combined or '1080' in combined:
            parts.append('1080p')
        elif '720P' in combined or 'HD' in combined:
            parts.append('720p')
        elif '480P' in combined or '480' in combined:
            parts.append('480p')
        elif not _has_src and ('RIP' in combined or 'DVD' in combined):
            parts.append('SD')
        if parts:
            return ' '.join(parts)
        return 'N/A'
    if _has_4k_token(combined): return '4K'
    if '1080P' in combined or 'FULLHD' in combined or 'FHD' in combined or '1080' in combined: return '1080p'
    if '720P' in combined or 'HD' in combined: return '720p'
    if 'RIP' in combined or 'DVD' in combined or 'WEBRIP' in combined or 'DVDRIP' in combined: return 'SD'
    if 'CAM' in combined or 'TS' in combined or 'TELESYNC' in combined: return 'CAM'
    return 'N/A'

def _get_link_quality_score(link):
    q = _format_quality(link)
    if '4K' in q or '2160' in q: return 500
    if '1080' in q: return 400
    if '720' in q: return 300
    if 'CAM' in q: return 100
    if q in ('SD', 'DVD', 'DVDRip') or 'RIP' in q or 'WEBRIP' in q.upper(): return 200
    return 50

def _format_channel(link):
    ch = _safe_str(getattr(link, 'channel', ''))
    if ch: return ch.capitalize()
    return 'General'

def _get_int_setting(key, default_val):
    try:
        val = _bridge_addon.getSetting(key)
        if val is None or val == '': return default_val
        return int(float(val))
    except Exception:
        return default_val

def _parse_csv_setting(val):
    if not val: return []
    return [_safe_str(s).strip().lower() for s in str(val).split(',') if s.strip()]

def _get_filter_prefs():
    def _bi(key, def_val):
        try:
            val = _bridge_addon.getSetting(key)
            if val is None or val == '': return def_val
            return int(float(val))
        except:
            return def_val

    def _bcsv(key, def_val):
        try:
            val = _bridge_addon.getSetting(key)
            if not val: return def_val
            return [s.strip().lower() for s in str(val).split(',') if s.strip()]
        except:
            return def_val

    lang_on = _bridge_addon.getSetting('filter_lang_enabled') != 'false'
    qual_on = _bridge_addon.getSetting('filter_quality_enabled') != 'false'
    srv_on  = _bridge_addon.getSetting('filter_servers_enabled') != 'false'

    return {
        'preferencia_idioma_lat': _bi('preferencia_idioma_lat', 1) if lang_on else 1,
        'preferencia_idioma_esp': _bi('preferencia_idioma_esp', 2) if lang_on else 1,
        'preferencia_idioma_vos': _bi('preferencia_idioma_vos', 3) if lang_on else 1,
        'servers_sort_quality': _bi('servers_sort_quality', 1) if qual_on else 0,
        'servers_preferred': _bcsv('servers_preferred', ['voe', 'doodstream', 'vidmoly']) if srv_on else [],
        'servers_unfavored': _bcsv('servers_unfavored', ['uqload', 'torrent']) if srv_on else [],
        'servers_discarded': _bcsv('servers_discarded', ['rapidgator', 'directo', 'waaw']) if srv_on else [],
    }

def _filter_and_sort_links(links):
    if not links: return []

    safe_links = []
    for l in links:
        try:
            _u = _safe_str(getattr(l, 'url', ''))
            if _u and len(_u) > 1500:
                continue
            safe_links.append(l)
        except:
            safe_links.append(l)

    b_prefs = _get_filter_prefs()

    # PASO 0: desempate por confianza del match (los anclados por TMDb/IMDb o
    # año primero dentro de igual calidad/servidor/idioma). Al ser el primer
    # sorted estable, solo ordena entre empatados: no quita ni agrega nada.
    try:
        safe_links = sorted(safe_links, key=lambda it: -int(getattr(it, 'bridge_score', 0) or 0))
    except: pass

    # PASO 1 de Balandro: filter_and_sort_by_quality
    # 0: Orden Web, 1: Calidad Alta (descendente), 2: Calidad Baja (ascendente)
    sort_q = b_prefs.get('servers_sort_quality', 1)
    if sort_q == 1:
        safe_links = sorted(safe_links, key=_get_link_quality_score, reverse=True)
    elif sort_q == 2:
        safe_links = sorted(safe_links, key=_get_link_quality_score)

    # PASO 2 de Balandro: filter_and_sort_by_server
    # 2a. Descartar servidores descartados por el usuario
    disc_list = b_prefs.get('servers_discarded', [])
    if disc_list:
        def _is_not_discarded(it):
            if _is_torrent_link(it) and 'torrent' not in disc_list:
                return True
            srv = (_get_link_server_name(it) or getattr(it, 'server', '') or '').lower()
            if not srv:
                return 'indeterminado' not in disc_list
            return srv not in disc_list
        safe_links = [it for it in safe_links if _is_not_discarded(it)]

    # 2b. Ordenar preferidos, normales (99) y última opción (999 - index)
    pref_list = b_prefs.get('servers_preferred', [])
    unfav_list = b_prefs.get('servers_unfavored', [])
    if pref_list or unfav_list:
        def numera_server(it):
            if _is_torrent_link(it):
                srv = 'torrent'
            else:
                srv = (_get_link_server_name(it) or getattr(it, 'server', '') or '').lower()
            if not srv:
                srv = 'indeterminado'
            if srv in pref_list:
                return pref_list.index(srv)
            elif srv in unfav_list:
                return 999 - unfav_list.index(srv)
            else:
                return 99
        safe_links = sorted(safe_links, key=numera_server)

    # PASO 3 de Balandro: filter_and_sort_by_language
    # 3a. Quitar idiomas con valor 0 (Descartar)
    # 3b. Ordenar según orden de preferencia (1: Primero, 2: Segundo, 3: Tercero)
    p_lat = b_prefs.get('preferencia_idioma_lat', 1)
    p_esp = b_prefs.get('preferencia_idioma_esp', 2)
    p_vos = b_prefs.get('preferencia_idioma_vos', 3)
    prefs_lang = {'Lat': p_lat, 'Esp': p_esp, 'VO': p_vos, '?': 4}

    def _get_lang_grp(it):
        l_str = (_safe_str(getattr(it, 'language', '')) + ' ' + _get_link_language_group(it)).upper()
        if 'LAT' in l_str: return 'Lat'
        if 'ESP' in l_str or 'CAST' in l_str: return 'Esp'
        if 'VOS' in l_str or 'VO' in l_str or 'SUB' in l_str or 'ENG' in l_str: return 'VO'
        return '?'

    # Descartar idiomas con valor 0 (Descartar)
    safe_links = [it for it in safe_links if _is_torrent_link(it) or prefs_lang.get(_get_lang_grp(it), 4) != 0]

    # Ordenar por preferencia de idioma
    def _lang_sort_key(it):
        if _is_torrent_link(it):
            grp = _get_lang_grp(it)
            return prefs_lang.get(grp, 4) if grp != '?' else 99
        return prefs_lang.get(_get_lang_grp(it), 4)

    safe_links = sorted(safe_links, key=_lang_sort_key)

    # PASO 4: Priorizar enlaces confirmados sobre enlaces debiles
    # Evita que un enlace debil/no confirmado (posible homonimo de otro anio)
    # se cuele al inicio de la lista o sea reproducido por autoplay por mejor calidad/servidor.
    try:
        safe_links = sorted(safe_links, key=lambda it: 1 if getattr(it, 'bridge_weak', False) else 0)
    except: pass

    return safe_links

# ---------------------------------------------------------
# Headless Link Verification System
# ---------------------------------------------------------
def _extract_valid_streams(video_urls):
    if not video_urls: return None
    res = []
    if isinstance(video_urls, list):
        for entry in video_urls:
            if isinstance(entry, list) and len(entry) >= 2:
                stream_url = _safe_str(entry[1])
                if stream_url.startswith('http') or stream_url.startswith('rtmp'):
                    res.append(entry)
            elif isinstance(entry, str) and (entry.startswith('http') or entry.startswith('rtmp')):
                res.append(['', entry])
    return res if res else None

def _link_cache_key(lnk):
    """Huella estable de un enlace para reutilizar su resolucion.
    Dos enlaces con la misma huella son intercambiables (mismos atributos),
    asi que resolver uno solo da el mismo resultado final que resolver ambos.
    Se excluye 'video_urls' (es el producto de la resolucion, no la entrada)."""
    try:
        d = dict(getattr(lnk, '__dict__', {}) or {})
        d.pop('video_urls', None)
        return json.dumps(d, sort_keys=True, default=str, ensure_ascii=False)
    except Exception:
        return '%s|%s|%s|%s' % (getattr(lnk, 'server', ''), getattr(lnk, 'url', ''),
                                getattr(lnk, 'title', ''), getattr(lnk, 'language', ''))

def _verify_links_headless(links, engine='alfa', p_dialog=None):
    if not links: return []

    # Separar enlaces torrent de enlaces directos.
    # Los enlaces torrent NUNCA se verifican (no son streaming directo ni usan hosters/resolvers HTTP).
    # Se conservan automáticamente en su posición original como disponibles.
    verified_results = {}
    direct_items_to_test = []

    for idx, lnk in enumerate(links):
        if _is_torrent_link(lnk):
            verified_results[idx] = lnk
        else:
            direct_items_to_test.append((idx, lnk))

    if not direct_items_to_test:
        xbmc.log("Multi Bridge: _verify_links_headless - todos los enlaces son torrents (%d), ninguno a verificar" % len(links), xbmc.LOGINFO)
        return [verified_results[idx] for idx in sorted(verified_results.keys())]

    mods = _get_alfa_modules() if engine == 'alfa' else _get_balandro_modules()
    if not mods: return links

    servertools = mods['servertools']
    total_to_check = len(direct_items_to_test)
    # Torrents ya verificados de entrada (se conservan sin comprobar)
    _n_torrents = len(links) - total_to_check
    checked_count = [0]
    finished_indices = set()
    lock = threading.Lock()

    max_workers = _get_int_setting('verify_threads_max', 8)
    verify_timeout = _get_int_setting('verify_timeout', 30)
    if max_workers < 1: max_workers = 1
    if verify_timeout < 3: verify_timeout = 3

    orig_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(verify_timeout)

    # Cache de resoluciones: los mismos enlaces (misma huella) se resuelven
    # una sola vez por red y el resto reutiliza el resultado. El clone() de
    # Balandro es deepcopy, asi que cada indice recibe un objeto independiente
    # con el mismo contenido -> el resultado final no cambia.
    prep_cache = {}
    _CACHE_MISS = object()

    def _resolve_one(lnk):
        test_item = lnk.clone() if hasattr(lnk, 'clone') else lnk
        video_urls = None
        puedes = False
        try:
            prep = _prepare_playable_link(test_item, engine=engine)
            if not prep: return None
            if _is_torrent_link(prep):
                return prep

            actual_srv = (getattr(prep, 'server', '') or _get_link_server_name(prep)).lower().strip()
            raw_srv = (getattr(prep, 'server', '') or '').lower().strip()
            url = getattr(prep, 'url', '')
            pwd = getattr(prep, 'password', '')

            if not url and not getattr(prep, 'video_urls', None):
                return None

            servers_to_try = []
            if actual_srv and actual_srv not in ('directo', 'none', ''): servers_to_try.append(actual_srv)
            if raw_srv and raw_srv not in servers_to_try and raw_srv not in ('directo', 'none', ''): servers_to_try.append(raw_srv)
            if 'various' not in servers_to_try: servers_to_try.append('various')

            if getattr(prep, 'video_urls', None):
                video_urls = prep.video_urls
                puedes = True
            else:
                with silenced_dialogs():
                    for srv_try in servers_to_try:
                        if engine == 'balandro':
                            try:
                                video_urls, puedes, _ = servertools.resolve_video_urls_for_playing(srv_try, url)
                                if puedes and video_urls: break
                            except Exception: pass
                        else:
                            try:
                                video_urls, puedes, _ = servertools.resolve_video_urls_for_playing(srv_try, url, pwd, False)
                                if puedes and video_urls: break
                            except Exception: pass

            if puedes and video_urls:
                valid_streams = _extract_valid_streams(video_urls)
                if valid_streams:
                    prep.video_urls = valid_streams
                    return prep
            return None
        except Exception:
            return None

    def _test_worker(orig_idx, lnk):
        # Doble salvaguarda: si es torrent, jamás verificar por red
        if _is_torrent_link(lnk):
            with lock:
                if orig_idx not in finished_indices:
                    finished_indices.add(orig_idx)
                    verified_results[orig_idx] = lnk
                checked_count[0] += 1
            return

        try:
            _ckey = _link_cache_key(lnk)
            with lock:
                _hit = prep_cache.get(_ckey, _CACHE_MISS)
            if _hit is _CACHE_MISS:
                _res = _resolve_one(lnk)
                with lock:
                    prep_cache[_ckey] = _res
            else:
                _res = _hit
            if _res is None:
                return
            _cp = _res.clone() if hasattr(_res, 'clone') else _res
            with lock:
                if orig_idx not in finished_indices:
                    finished_indices.add(orig_idx)
                    verified_results[orig_idx] = _cp
        except Exception: pass
        finally:
            with lock:
                if orig_idx not in finished_indices:
                    finished_indices.add(orig_idx)
                checked_count[0] += 1

    threads = []
    for orig_idx, lnk in direct_items_to_test:
        threads.append((orig_idx, threading.Thread(target=_test_worker, args=(orig_idx, lnk), daemon=True)))

    running = []
    i = 0
    try:
        while (i < len(threads) or running) and not xbmc.Monitor().abortRequested():
            if p_dialog and p_dialog.iscanceled(): break
            now = time.time()
            running = [(orig_idx, t, st) for (orig_idx, t, st) in running if t.is_alive() and (now - st < verify_timeout)]

            while i < len(threads) and len(running) < max_workers:
                orig_idx, t = threads[i]
                t.start()
                running.append((orig_idx, t, time.time()))
                i += 1

            with lock:
                done = checked_count[0]
                # Funcionales = resultados guardados menos los torrents de entrada
                funcionales = len(verified_results) - _n_torrents
            if p_dialog:
                pct = int((done / float(total_to_check)) * 100) if total_to_check else 100
                _l1 = 'Verificando %d enlaces' % len(links)
                _l2parts = []
                if funcionales > 0:
                    _l2parts.append('[COLOR lime]%d Funcionales[/COLOR]' % funcionales)
                if _n_torrents > 0:
                    _l2parts.append('[COLOR cyan]%d Torrents[/COLOR]' % _n_torrents)
                _l2 = ' · '.join(_l2parts)
                _l3 = '[COLOR grey]Cerrar para reproducir los enlaces[/COLOR]' if funcionales > 0 else ''
                p_dialog.update(pct, '%s\n%s\n%s' % (_l1, _l2, _l3))

            if not running and i >= len(threads): break
            time.sleep(0.1)
    finally:
        socket.setdefaulttimeout(orig_timeout)

    return [verified_results[orig_idx] for orig_idx in sorted(verified_results.keys()) if orig_idx in verified_results]

def _build_bridge_terms(target_title, alt_terms, all_names):
    candidates = []
    seen_raw = set()
    def add_cand(t):
        if not t:
            return
        t_str = _safe_str(t).strip()
        if not t_str:
            return
        low = t_str.lower()
        if low in seen_raw:
            return
        seen_raw.add(low)
        candidates.append(t_str)
    add_cand(target_title)
    if alt_terms:
        for t in alt_terms:
            add_cand(t)
    if all_names:
        for t in all_names:
            add_cand(t)
    terms = []
    seen_clean = set()
    for raw in candidates:
        cleaned = _clean_search_term(raw)
        if cleaned and cleaned.lower() not in seen_clean:
            seen_clean.add(cleaned.lower())
            terms.append(cleaned)
        core_raw = get_core_title(raw)
        if core_raw and core_raw != raw:
            core_clean = _clean_search_term(core_raw)
            if core_clean and core_clean.lower() not in seen_clean:
                seen_clean.add(core_clean.lower())
                terms.append(core_clean)
    return terms[:4]


_tmdb_translations_cache = {}

def _resolve_localized_metadata(tmdb_id=None, is_series=False, season=None, episode=None,
                                 plot_lat='', plot_es='', plot_param='',
                                 tagline_lat='', tagline_es='', tagline_param='',
                                 title_lat='', title_es='', title_param=''):
    """Resuelve de forma independiente la traducción del contenido usando TMDb API /translations.
    Prioridad:
      1. Español (México) (es-MX)
      2. Si no hay es-MX, usar Español (España) (es-ES) para sinopsis, slogan y título
      3. Si no hay es-MX ni es-ES (solo inglés u otro idioma), sinopsis = 'Sinopsis sin traducción'
    """
    tmdb_id_str = str(tmdb_id or '').strip()
    s_val = str(season or '').strip()
    e_val = str(episode or '').strip()
    is_ep = bool(is_series and s_val and e_val and s_val.isdigit() and e_val.isdigit())

    cache_key = f"{tmdb_id_str}_{s_val}_{e_val}" if is_ep else f"{tmdb_id_str}_{1 if is_series else 0}"

    cur_plot = str(plot_param or '').strip()
    cur_tagline = str(tagline_param or '').strip()
    cur_title = str(title_param or '').strip()

    if tmdb_id_str.isdigit() and cache_key in _tmdb_translations_cache:
        return dict(_tmdb_translations_cache[cache_key])

    tmdb_mx_plot = ''
    tmdb_mx_tagline = ''
    tmdb_mx_title = ''
    tmdb_es_plot = ''
    tmdb_es_tagline = ''
    tmdb_es_title = ''
    tmdb_oth_plot = ''
    tmdb_oth_tagline = ''
    tmdb_oth_title = ''
    has_tmdb_data = False

    if tmdb_id_str.isdigit():
        api_key = "a1ab8b8669da03637a4b98fa39c39228"
        import urllib.request as _ureq
        import ssl as _ssl
        import json as _json

        ctx = None
        try:
            ctx = _ssl._create_unverified_context()
        except Exception:
            pass

        urls_to_try = []
        if is_ep:
            urls_to_try.append(f"https://api.themoviedb.org/3/tv/{tmdb_id_str}/season/{s_val}/episode/{e_val}/translations?api_key={api_key}")
            urls_to_try.append(f"https://api.themoviedb.org/3/tv/{tmdb_id_str}/translations?api_key={api_key}")
        elif is_series:
            urls_to_try.append(f"https://api.themoviedb.org/3/tv/{tmdb_id_str}/translations?api_key={api_key}")
        else:
            urls_to_try.append(f"https://api.themoviedb.org/3/movie/{tmdb_id_str}/translations?api_key={api_key}")

        for u in urls_to_try:
            try:
                req = _ureq.Request(u, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
                kw = {'timeout': 3.5}
                if ctx:
                    kw['context'] = ctx
                with _ureq.urlopen(req, **kw) as resp:
                    raw = resp.read().decode('utf-8', errors='ignore')
                    data = _json.loads(raw)
                    translations = data.get('translations', [])
                    if translations:
                        has_tmdb_data = True
                        for t in translations:
                            iso_639 = (t.get('iso_639_1') or '').lower()
                            iso_3166 = (t.get('iso_3166_1') or '').upper()
                            d = t.get('data', {})
                            if iso_639 == 'es':
                                p = (d.get('overview') or '').strip()
                                tg = (d.get('tagline') or '').strip()
                                tt = (d.get('title') or d.get('name') or '').strip()
                                if iso_3166 == 'MX':
                                    if p and not tmdb_mx_plot: tmdb_mx_plot = p
                                    if tg and not tmdb_mx_tagline: tmdb_mx_tagline = tg
                                    if tt and not tmdb_mx_title: tmdb_mx_title = tt
                                elif iso_3166 == 'ES':
                                    if p and not tmdb_es_plot: tmdb_es_plot = p
                                    if tg and not tmdb_es_tagline: tmdb_es_tagline = tg
                                    if tt and not tmdb_es_title: tmdb_es_title = tt
                                else:
                                    if p and not tmdb_oth_plot: tmdb_oth_plot = p
                                    if tg and not tmdb_oth_tagline: tmdb_oth_tagline = tg
                                    if tt and not tmdb_oth_title: tmdb_oth_title = tt
                        # NOTA: no pre-poblar _tmdb_titles_cache aqui a proposito:
                        # solo tenemos titulos ES y _fetch_tmdb_titles necesita
                        # pedir el original en ingles; pre-poblarla la
                        # envenenaria y el fetch creeria que ya esta completa.
                    if tmdb_mx_plot or tmdb_es_plot:
                        break
            except Exception as e:
                pass

    # Resolver SINOPSIS (plot):
    if tmdb_mx_plot:
        final_plot = tmdb_mx_plot
    elif tmdb_es_plot:
        final_plot = tmdb_es_plot
    elif tmdb_oth_plot:
        final_plot = tmdb_oth_plot
    elif has_tmdb_data:
        final_plot = "Sinopsis sin traducción"
    elif plot_lat and plot_lat.strip():
        final_plot = plot_lat.strip()
    elif plot_es and plot_es.strip():
        final_plot = plot_es.strip()
    elif cur_plot:
        final_plot = cur_plot
    else:
        final_plot = "Sinopsis sin traducción"

    # Resolver SLOGAN (tagline):
    if tmdb_mx_tagline:
        final_tagline = tmdb_mx_tagline
    elif tmdb_es_tagline:
        final_tagline = tmdb_es_tagline
    elif tmdb_oth_tagline:
        final_tagline = tmdb_oth_tagline
    elif has_tmdb_data:
        final_tagline = ''
    elif tagline_lat and tagline_lat.strip():
        final_tagline = tagline_lat.strip()
    elif tagline_es and tagline_es.strip():
        final_tagline = tagline_es.strip()
    elif cur_tagline:
        final_tagline = cur_tagline
    else:
        final_tagline = ''

    # Resolver TÍTULO:
    final_title = cur_title
    if tmdb_mx_title:
        final_title = tmdb_mx_title
    elif tmdb_es_title:
        final_title = tmdb_es_title
    elif tmdb_oth_title:
        final_title = tmdb_oth_title
    elif title_lat and title_lat.strip():
        final_title = title_lat.strip()
    elif title_es and title_es.strip():
        final_title = title_es.strip()

    res = {
        'plot': final_plot,
        'tagline': final_tagline,
        'title': final_title,
        'has_spanish': bool(tmdb_mx_plot or tmdb_es_plot or tmdb_oth_plot)
    }
    if tmdb_id_str.isdigit():
        _tmdb_translations_cache[cache_key] = res

    try:
        xbmc.log(f"Multi Bridge: traducción resuelta (tmdb={tmdb_id_str}): plot={final_plot[:50]}... tagline={final_tagline} title={final_title}", xbmc.LOGINFO)
    except Exception:
        pass

    return res


_tmdb_titles_cache = {}
_tmdb_verify_cache = {}

def _notify_scan(channel_id):
    # Aviso desactivado por peticion del usuario: el chequeo sigue corriendo
    # en silencio (solo deja rastro en el log, sin ventanas emergentes).
    try:
        xbmc.log('Multi Bridge: comprobando año en web real (%s)...' % channel_id, xbmc.LOGINFO)
    except Exception:
        pass

def extract_year_from_url(url):
    """Extrae el año (1900-2039) del slug o ruta de una URL si viene embebido.
    0ms de latencia, evita peticiones de red innecesarias.
    Ejemplos: /pelicula/horas-desesperadas-2013 -> 2013
              /pelicula/37-horas-desesperadas-1990.html -> 1990
              /serie/the-boys-2019/ -> 2019
    """
    if not url:
        return None
    try:
        import re as _re_url
        m = _re_url.search(r'[-_/](19\d\d|20[0-3]\d)(?:[-_/]|\.html?|$)', str(url))
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None

_channel_cookie_jar = None
_channel_cookie_jar_time = 0

def _get_channel_cookie_opener():
    global _channel_cookie_jar, _channel_cookie_jar_time
    import time, http.cookiejar, urllib.request, ssl
    now = time.time()
    if _channel_cookie_jar is None or (now - _channel_cookie_jar_time) > 60:
        cj = http.cookiejar.MozillaCookieJar()
        for addon_id in ('plugin.video.balandro', 'plugin.video.alfa'):
            try:
                c_path = xbmcvfs.translatePath('special://userdata/addon_data/%s/cookies.dat' % addon_id)
                if os.path.exists(c_path):
                    cj.load(c_path, ignore_discard=True, ignore_expires=True)
            except Exception:
                pass
        _channel_cookie_jar = cj
        _channel_cookie_jar_time = now

    ctx = ssl._create_unverified_context() if hasattr(ssl, '_create_unverified_context') else None
    handlers = [urllib.request.HTTPCookieProcessor(_channel_cookie_jar)]
    if ctx:
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)

def _norm_person_name(s):
    if not s: return ''
    import unicodedata, re as _re
    s = unicodedata.normalize('NFKD', str(s)).encode('ASCII', 'ignore').decode('utf-8')
    s = _re.sub(r'[^a-zA-Z0-9\s]', '', s).lower()
    return ' '.join(s.split())

def _match_actors(web_set, tmdb_set):
    if not web_set or not tmdb_set:
        return False
    if web_set.intersection(tmdb_set):
        return True
    for w in web_set:
        w_parts = set(w.split())
        if len(w_parts) >= 2:
            for t in tmdb_set:
                t_parts = set(t.split())
                if len(t_parts) >= 2 and len(w_parts.intersection(t_parts)) >= 2:
                    return True
    return False

def _extract_web_actors(html):
    import re as _re
    actors = []
    # 1. Links de actores (ej: /stars/..., /actor/..., /actores/...)
    for a in _re.findall(r'<a[^>]+href=[\'"][^\'"]*(?:/stars?/|/actors?/)[^\'"]*[\'"][^>]*>([^<]+)</a>', html, _re.I):
        c = a.strip()
        if len(c) > 2 and c not in actors:
            actors.append(c)
    # 2. Bloque etiquetado de reparto (Actors, Actores, Reparto, Elenco, Cast)
    if not actors:
        lbl_block = _re.search(r'(?:<strong>|<span>|<p>|<div[^>]*>)\s*(?:Actors|Actores|Reparto|Cast|Elenco)\s*[:<](.*?)</(?:p|div|ul|li|section)>', html, _re.I | _re.DOTALL)
        if lbl_block:
            content = lbl_block.group(1)
            sub_as = _re.findall(r'<a[^>]*>([^<]+)</a>', content)
            if sub_as:
                for a in sub_as:
                    c = a.strip()
                    if len(c) > 2 and c not in actors:
                        actors.append(c)
            else:
                clean = _re.sub(r'<[^>]+>', ' ', content)
                for part in _re.split(r'[,;/•|]', clean):
                    p = part.strip()
                    if len(p) > 2 and len(p) < 40 and p not in actors:
                        actors.append(p)
    # 3. Microdata / schema.org itemprop="actor"
    if not actors:
        for a in _re.findall(r'itemprop=[\'"]actor[\'"].*?<span[^>]+itemprop=[\'"]name[\'"]>([^<]+)</span>', html, _re.I | _re.DOTALL):
            c = a.strip()
            if len(c) > 2 and c not in actors:
                actors.append(c)
    return actors

_tmdb_cast_cache = {}

def _fetch_tmdb_cast(tmdb_id, is_series=False):
    global _tmdb_cast_cache
    if not tmdb_id:
        return []
    tmdb_id_str = str(tmdb_id).strip()
    if not tmdb_id_str.isdigit():
        return []
    cache_key = f"{tmdb_id_str}_{1 if is_series else 0}"
    if cache_key in _tmdb_cast_cache:
        return _tmdb_cast_cache[cache_key]
    cast = []
    try:
        api_key = "a1ab8b8669da03637a4b98fa39c39228"
        tmdb_type = "tv" if is_series else "movie"
        import json as _json
        import urllib.request as _ureq
        import ssl
        ctx = ssl._create_unverified_context() if hasattr(ssl, '_create_unverified_context') else None
        url = f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id_str}/credits?api_key={api_key}"
        req = _ureq.Request(url)
        with _ureq.urlopen(req, timeout=4, context=ctx) as resp:
            data = _json.loads(resp.read().decode('utf-8', errors='ignore'))
            for c in data.get('cast', [])[:12]:
                name = (c.get('name') or c.get('original_name') or '').strip()
                if name and name not in cast:
                    cast.append(name)
    except Exception:
        pass
    if len(_tmdb_cast_cache) > 200:
        _tmdb_cast_cache.clear()
    _tmdb_cast_cache[cache_key] = cast
    return cast

def _detail_year_consistent(url, target_year, channel_id='', item=None, target_imdb=None, target_tmdb=None, is_series=False):
    # Comprueba si el candidato coincide con el año objetivo, IMDb ID o reparto.
    # True = mantener (coherente o no concluyente). False = descartar.
    # Ante cualquier duda o fallo devuelve True: nunca quita por error.
    # Si la web confirma positivamente el año objetivo, IMDb o actores, marca item._web_year_confirmed = True.
    year_t = None
    try:
        if target_year:
            year_t = int(str(target_year).strip())
            if year_t < 1900 or year_t > 2100:
                year_t = None
    except Exception:
        year_t = None

    if not year_t and not target_imdb and not target_tmdb:
        return True

    _u = _safe_str(url or '').strip()
    if not _u.startswith('http'):
        return True

    def _mark_confirmed():
        if item is not None:
            try:
                setattr(item, '_web_year_confirmed', True)
                if hasattr(item, 'infoLabels') and isinstance(getattr(item, 'infoLabels', None), dict):
                    if year_t:
                        item.infoLabels['year'] = year_t
                    if target_imdb:
                        item.infoLabels['imdb_id'] = str(target_imdb)
                    if target_tmdb:
                        item.infoLabels['tmdb_id'] = str(target_tmdb)
                        item.infoLabels['tmdb'] = str(target_tmdb)
                if year_t:
                    item.year = year_t
                if target_tmdb:
                    item.tmdb_id = str(target_tmdb)
            except Exception:
                pass

    # 0. Verificación ultrarrápida (0ms) en la propia URL si incluye el año en el slug
    if year_t:
        try:
            _url_year = extract_year_from_url(_u)
            if _url_year:
                if abs(_url_year - year_t) > 1:
                    try:
                        xbmc.log("Multi Bridge: %s '%s' descartado por año en URL (%d != %d)" % (channel_id, _u, _url_year, year_t), xbmc.LOGINFO)
                    except: pass
                    return False
                # Coincide con el año objetivo (tolerancia +/- 1)
                _mark_confirmed()
                return True
        except Exception:
            pass

    _notify_scan(channel_id or 'web')
    html = ''
    try:
        import urllib.request as _ureq
        opener = _get_channel_cookie_opener()
        req = _ureq.Request(_u, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.8010.12 Safari/537.36',
            'Referer': '/'.join(_u.split('/')[:3]) + '/'})
        with opener.open(req, timeout=6) as resp:
            # Si el servidor redirige a una URL genérica de login (/login), no es la ficha del contenido
            final_url = resp.geturl() or ''
            if final_url.rstrip('/').endswith('/login'):
                return True
            # 80KB es suficiente para capturar head, h1, metadatos y JSON-LD sin descargar megabytes
            html = resp.read(80000).decode('utf-8', errors='ignore')
    except Exception as _e:
        try:
            xbmc.log('Multi Bridge: page-check sin red (%s): %s' % (channel_id, _e), xbmc.LOGINFO)
        except: pass
        return True
    if not html or len(html) < 200:
        return True
    try:
        import re as _re2
        years_re = r'\b(19\d\d|20[0-3]\d)\b'

        # 0.5. Verificación por IMDb ID (100% inequívoca)
        if target_imdb:
            t_imdb = str(target_imdb).strip().lower()
            if t_imdb.startswith('tt'):
                page_imdbs = [m.lower() for m in _re2.findall(r'imdb\.com/title/(tt\d+)', html, _re2.I)]
                if page_imdbs:
                    if t_imdb in page_imdbs:
                        _mark_confirmed()
                        return True
                    else:
                        # Si la web muestra un IMDb explícito y no coincide con el buscado, descartar
                        try:
                            xbmc.log("Multi Bridge: %s '%s' descartado por IMDb diferente en web (%s != %s)" % (channel_id, _u, page_imdbs[0], t_imdb), xbmc.LOGINFO)
                        except: pass
                        return False

        if not year_t:
            # Si no hay año objetivo pero sí target_tmdb, verificar actores antes de salir
            if target_tmdb:
                try:
                    web_actors = _extract_web_actors(html)
                    if web_actors and len(web_actors) >= 2:
                        tmdb_cast = _fetch_tmdb_cast(target_tmdb, is_series=is_series)
                        if tmdb_cast and len(tmdb_cast) >= 2:
                            norm_web = {_norm_person_name(a) for a in web_actors if _norm_person_name(a)}
                            norm_tmdb = {_norm_person_name(a) for a in tmdb_cast if _norm_person_name(a)}
                            if _match_actors(norm_web, norm_tmdb):
                                _mark_confirmed()
                                try:
                                    xbmc.log("Multi Bridge: %s '%s' confirmado por coincidencia de actores" % (channel_id, _u), xbmc.LOGINFO)
                                except: pass
                                return True
                            elif len(web_actors) >= 3:
                                try:
                                    xbmc.log("Multi Bridge: %s '%s' descartado por reparto no coincidente" % (channel_id, _u), xbmc.LOGINFO)
                                except: pass
                                return False
                except Exception: pass
            return True

        # 0.8. Check específico de HDFull y similares (ej: /buscar/year/2013 o /year/2013)
        hdfull_years = [int(y) for y in _re2.findall(r'/(?:buscar/)?year/(\d{4})', html, _re2.I)]
        if hdfull_years:
            if any(abs(y - year_t) <= 1 for y in hdfull_years):
                _mark_confirmed()
                return True
            else:
                try:
                    xbmc.log("Multi Bridge: %s '%s' descartado por año en ficha HDFull (%d != %d)" % (channel_id, _u, hdfull_years[0], year_t), xbmc.LOGINFO)
                except: pass
                return False

        # 1. Encabezados h1, h2 y enlaces bookmark (lo más fiable: título principal de la ficha)
        # Portales como GnulaTv o temas de WordPress usan h2 con rel="bookmark" o título del post
        cand_title = ''
        if item is not None:
            cand_title = _safe_str(getattr(item, 'contentTitle', '') or getattr(item, 'title', '')).strip()
        cand_words = [w.lower() for w in _re2.findall(r'[a-zA-ZáéíóúÁÉÍÓÚñÑ]{4,}', cand_title)] if cand_title else []

        headings = []
        # Enlaces permalink del post con rel="bookmark" (ej. Gnula: <a rel="bookmark" title="Ver ... (2011) online">)
        headings.extend(_re2.findall(r'<a[^>]+rel=["\']bookmark["\'][^>]*>(.*?)</a>', html, flags=_re2.IGNORECASE | _re2.DOTALL))
        # Clases CSS explícitas de título del post/obra
        headings.extend(_re2.findall(r'<[^>]+class=["\'][^"\']*(?:entry-title|post-title|item-title|film-title|tit_peli)[^"\']*["\'][^>]*>(.*?)</', html, flags=_re2.IGNORECASE | _re2.DOTALL))
        # h1 (siempre)
        headings.extend(_re2.findall(r'<h1[^>]*>(.*?)</h1>', html, flags=_re2.IGNORECASE | _re2.DOTALL))
        # h2 (si coincide con palabras del título o términos de reproducción/post)
        h2s = _re2.findall(r'<h2[^>]*>(.*?)</h2>', html, flags=_re2.IGNORECASE | _re2.DOTALL)
        for _h in h2s:
            _htxt = _re2.sub(r'<[^>]+>', ' ', _h).strip()
            if cand_words and any(w in _htxt.lower() for w in cand_words):
                headings.append(_h)
            elif 'rel="bookmark"' in _h.lower() or 'online' in _htxt.lower() or 'ver ' in _htxt.lower():
                headings.append(_h)

        head_years = []
        for _h in headings:
            _t = _re2.sub(r'<[^>]+>', ' ', _h)
            hy = [int(y) for y in _re2.findall(years_re, _t)]
            if hy:
                head_years.extend(hy)

        if head_years:
            if any(abs(y - year_t) <= 1 for y in head_years):
                _mark_confirmed()
                return True
            try:
                xbmc.log("Multi Bridge: %s '%s' descartado por año en encabezado/título de la web (%s != %s)" % (channel_id, _u, head_years[0], year_t), xbmc.LOGINFO)
            except: pass
            return False

        # 2. Meta tags og:title / twitter:title
        meta_titles = _re2.findall(r'<meta[^>]+(?:property|name)=["\'](?:og:title|twitter:title)["\'][^>]+content=["\']([^"\']+)["\']', html, _re2.I)
        meta_titles += _re2.findall(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:title|twitter:title)["\']', html, _re2.I)
        all_my = []
        for mt in meta_titles:
            all_my.extend([int(y) for y in _re2.findall(years_re, mt)])
        if all_my:
            if any(abs(y - year_t) <= 1 for y in all_my):
                _mark_confirmed()
                return True
            return False

        # 3. Meta release_date estricto (ignora fechas de subida como uploadDate)
        meta_dates = _re2.findall(r'<meta[^>]+(?:property|name)=["\'](?:(?:video:)?release_date|datepublished)["\'][^>]+content=["\']([^"\']+)["\']', html, _re2.I)
        meta_dates += _re2.findall(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:(?:video:)?release_date|datepublished)["\']', html, _re2.I)
        all_md = []
        for md in meta_dates:
            all_md.extend([int(y) for y in _re2.findall(years_re, md)])
        if all_md:
            if any(abs(y - year_t) <= 1 for y in all_md):
                _mark_confirmed()
                return True
            return False

        # 4. Título de la pestaña (<title>)
        _mt = _re2.search(r'<title[^>]*>(.*?)</title>', html, flags=_re2.IGNORECASE | _re2.DOTALL)
        if _mt:
            _t = _re2.sub(r'<[^>]+>', ' ', _mt.group(1))
            ty = [int(y) for y in _re2.findall(years_re, _t)]
            if ty:
                if any(abs(y - year_t) <= 1 for y in ty):
                    _mark_confirmed()
                    return True
                return False

        # 5. JSON-LD datePublished / releaseDate
        j_dates = _re2.findall(r'["\'](?:datePublished|releaseDate)["\']\s*:\s*["\']?(19\d\d|20[0-3]\d)', html, _re2.IGNORECASE)
        if j_dates:
            jy = [int(y) for y in j_dates]
            if any(abs(y - year_t) <= 1 for y in jy):
                _mark_confirmed()
                return True
            return False

        # 6. Campos etiquetados explícitos (ej. >Año:</span> 2013 o <span>Estreno</span>: 2013 o <span>A&ntilde;o: </span>)
        lbl_m = _re2.search(r'>\s*(?:A(?:ñ|&ntilde;|&#241;)o|Estreno|Fecha\s+de\s+estreno|Year|Release(?:\s+date)?)\s*[:<](?:<[^>]+>|\s)*\b(19\d\d|20[0-3]\d)\b|>\s*(?:A(?:ñ|&ntilde;|&#241;)o|Estreno|Fecha\s+de\s+estreno|Year|Release(?:\s+date)?)\s*(?:<[^>]+>\s*)*:\s*(?:<[^>]+>\s*)*\b(19\d\d|20[0-3]\d)\b', html, _re2.IGNORECASE)
        if lbl_m:
            ly = int(lbl_m.group(1) or lbl_m.group(2))
            if abs(ly - year_t) <= 1:
                _mark_confirmed()
                return True
            return False

        # 6.5. Patrones de estreno o ficha en texto inline (ej. Ver película online [2011, Latino, HD])
        inline_m = _re2.search(r'(?:ver\s+pel[ií]cula|online|estreno|a[ñn]o)[^<]{0,50}(?:\[|\()\s*(19\d\d|20[0-3]\d)\s*(?:\]|\)|,)', html, _re2.IGNORECASE)
        if inline_m:
            iy = int(inline_m.group(1))
            if abs(iy - year_t) <= 1:
                _mark_confirmed()
                return True
            try:
                xbmc.log("Multi Bridge: %s '%s' descartado por año inline en web (%d != %d)" % (channel_id, _u, iy, year_t), xbmc.LOGINFO)
            except: pass
            return False

        # 7. Clase CSS meta estricta (<span class="year">2013</span>)
        cls_m = _re2.search(r'<[^>]+class=["\'][^"\']*\b(?:year|release)\b[^"\']*["\'][^>]*>\s*\b(19\d\d|20[0-3]\d)\b\s*<', html, _re2.IGNORECASE)
        if cls_m:
            cy = int(cls_m.group(1))
            if abs(cy - year_t) <= 1:
                _mark_confirmed()
                return True
            return False

        # 8. Formato de ficha AresHD y similares: >2026</span>...Año
        areshd_lbl = _re2.search(r'>\s*\b(19\d\d|20[0-3]\d)\b\s*</span[^>]*>\s*(?:<[^>]+>\s*)*A[ñn]o\b', html, _re2.IGNORECASE)
        if areshd_lbl:
            ay = int(areshd_lbl.group(1))
            if abs(ay - year_t) <= 1:
                _mark_confirmed()
                return True
            return False

        # 9. Verificación por Reparto / Actores (Nivel 4 de máxima precisión):
        # Si la web no tiene año en título/metadatos o no fue concluyente (ej. Homecine),
        # se extraen los actores y se comparan contra el reparto de TMDb.
        # Debe ejecutarse ANTES de escanear texto general para no dejarse engañar por
        # menús o pies de página (ej. 'Estrenos 2026' en el menú de navegación).
        if target_tmdb:
            try:
                web_actors = _extract_web_actors(html)
                if web_actors and len(web_actors) >= 2:
                    tmdb_cast = _fetch_tmdb_cast(target_tmdb, is_series=is_series)
                    if tmdb_cast and len(tmdb_cast) >= 2:
                        norm_web = {_norm_person_name(a) for a in web_actors if _norm_person_name(a)}
                        norm_tmdb = {_norm_person_name(a) for a in tmdb_cast if _norm_person_name(a)}
                        if _match_actors(norm_web, norm_tmdb):
                            _mark_confirmed()
                            try:
                                xbmc.log("Multi Bridge: %s '%s' confirmado por coincidencia de actores" % (channel_id, _u), xbmc.LOGINFO)
                            except: pass
                            return True
                        elif len(web_actors) >= 3:
                            try:
                                xbmc.log("Multi Bridge: %s '%s' descartado por reparto no coincidente (%s != %s)" % (channel_id, _u, web_actors[:3], tmdb_cast[:3]), xbmc.LOGINFO)
                            except: pass
                            return False
            except Exception as _ce:
                try:
                    xbmc.log("Multi Bridge: cast verify error (%s): %s" % (channel_id, _ce), xbmc.LOGINFO)
                except: pass

        # 10. Resto de la página (fallback de último recurso si no hubo actores):
        # Si sale un único año distinto repetido (>=3 veces), es otra obra.
        _all = [int(y) for y in _re2.findall(years_re, html)]
        if _all:
            _uniq = set(_all)
            if len(_uniq) == 1 and len(_all) >= 3 and abs(list(_uniq)[0] - year_t) > 1:
                return False

        return True
    except Exception:
        return True

def _verify_candidate_tmdb(title_check, target_year, target_tmdb, is_series=False):
    """Confirma contra la API de TMDb que un candidato debil (match solo por
    titulo, sin IDs ni anio) es realmente el contenido buscado.
    Devuelve True si el primer resultado de busqueda coincide con target_tmdb
    SIN forzar &year= (evita sesgo circular) y sin homonimos con distinto anio.
    Cache de sesion, 1 llamada como maximo por candidato, timeout 4s. Nunca lanza excepciones."""
    try:
        t = _safe_str(title_check).strip()
        if not t or not target_tmdb:
            return False
        key = ('tv' if is_series else 'movie', t.lower(), str(target_year or ''), str(target_tmdb))
        if key in _tmdb_verify_cache:
            return _tmdb_verify_cache[key]
        ok = False
        try:
            api_key = "a1ab8b8669da03637a4b98fa39c39228"
            kind = "tv" if is_series else "movie"
            import json as _json
            import urllib.request as _ureq
            q = uparse.quote(t)
            # Busqueda abierta sin forzar anio para no enmascarar homonimos de otros anios
            url = f"https://api.themoviedb.org/3/search/{kind}?api_key={api_key}&query={q}&language=es-MX&page=1&include_adult=false"
            with _ureq.urlopen(url, timeout=4) as resp:
                data = _json.loads(resp.read().decode('utf-8', errors='ignore'))
            results = data.get('results', []) if isinstance(data, dict) else []
            if results and isinstance(results[0], dict):
                top_id = str(results[0].get('id', ''))
                if top_id and top_id == str(target_tmdb):
                    # Comprobar si existen homonimos con el mismo titulo pero distinto año
                    homonyms = False
                    t_clean = t.lower()
                    target_y = int(str(target_year).strip()) if target_year and str(target_year).isdigit() else 0
                    for r in results[1:6]:
                        if not isinstance(r, dict): continue
                        rt = (r.get('title') or r.get('name') or '').strip().lower()
                        rot = (r.get('original_title') or r.get('original_name') or '').strip().lower()
                        ry_str = (r.get('release_date') or r.get('first_air_date') or '')[:4]
                        if (rt == t_clean or rot == t_clean) and ry_str.isdigit():
                            ry = int(ry_str)
                            if target_y and abs(ry - target_y) > 1:
                                homonyms = True
                                break
                    if not homonyms:
                        ok = True
        except Exception:
            ok = False
        try:
            if len(_tmdb_verify_cache) > 500:
                _tmdb_verify_cache.clear()
            _tmdb_verify_cache[key] = ok
        except Exception:
            pass
        return ok
    except Exception:
        return False

_tmdb_identity_cache = {}

_PAREN_TAG_RE = re.compile(r'(?i)^\s*(4k|uhd|2160p|1080p|720p|480p|360p|bluray|blu-ray|bdrip|brrip|hdrip|hdtv|dvdrip|dvdscr|web-?d[l1]|webrip|web-rip|x26[45]|h26[45]|hevc|avc|xvid|divx|latino|castellano|espa[ñn]ol|subtitulad[oa]s?|dual|audio|hdr|sdr|dts|aac|ac3|mp3|extended|remastered|unrated|theatrical|directors?\s*cut|3d|sbs|hsbs|hdts|hd-ts|cam|ts|vose?|repack|proper)\s*$')

def _stripped_paren_extra(raw_title):
    """Texto entre parentesis/corchetes que NO es anio ni etiqueta tecnica.
    Ej. '(Cuenta Atras)' -> 'Cuenta Atras'; '(1995)', '(Latino)' -> ''.
    El limpiador clean_title borra estos segmentos para puntuar, pero aqui se
    detecta si lo borrado era significativo (desambigua homonimos)."""
    try:
        segs = re.findall(r'[\(\[\{]([^\)\]\}]{1,80})[\)\]\}]', _safe_str(raw_title) or '')
        out = []
        for s in segs:
            t = _safe_str(s).strip()
            if not t:
                continue
            if re.search(r'(19\d\d|20\d\d)', t):
                continue
            if _PAREN_TAG_RE.match(t):
                continue
            out.append(t)
        return ' '.join(out)
    except Exception:
        return ''

def _tmdb_identity_conflict(raw_title, target_tmdb, target_year):
    """True si TMDb reconoce el titulo EXACTO como OTRA pelicula (!= target).
    Caza homonimos con parentesis distintivo: 'Horas Desesperadas (Cuenta
    Atras)' es Hours 2013, no Desperate Hours (tmdb 18612). Solo cuenta si el
    top-1 de TMDb casa exacto con el titulo Y su anio difiere del objetivo
    (misma pelicula = mismo anio). Fail-open: ante cualquier duda, False.
    1 llamada API como maximo por titulo (cache de sesion), timeout 4s."""
    try:
        t = _safe_str(raw_title).strip()
        if not t or not target_tmdb:
            return False
        key = ('movie', t.lower(), str(target_tmdb))
        if key in _tmdb_identity_cache:
            return _tmdb_identity_cache[key]
        conflict = False
        try:
            api_key = "a1ab8b8669da03637a4b98fa39c39228"
            import json as _json
            import urllib.request as _ureq
            q = uparse.quote(t)
            url = f"https://api.themoviedb.org/3/search/movie?api_key={api_key}&query={q}&language=es-MX&page=1&include_adult=false"
            with _ureq.urlopen(url, timeout=4) as resp:
                data = _json.loads(resp.read().decode('utf-8', errors='ignore'))
            results = data.get('results', []) if isinstance(data, dict) else []
            if results and isinstance(results[0], dict):
                top = results[0]
                top_id = str(top.get('id', ''))
                if top_id and top_id != str(target_tmdb):
                    for _k in ('title', 'original_title'):
                        _tt = _safe_str(top.get(_k) or '').strip()
                        if _tt and clean_title(_tt) == clean_title(t):
                            _ry = _safe_str(top.get('release_date') or '')[:4]
                            _ty = _safe_str(target_year or '').strip()
                            if _ry.isdigit() and _ty.isdigit():
                                if abs(int(_ry) - int(_ty)) > 1:
                                    conflict = True
                            else:
                                conflict = True
                            break
        except Exception:
            conflict = False
        try:
            if len(_tmdb_identity_cache) > 500:
                _tmdb_identity_cache.clear()
            _tmdb_identity_cache[key] = conflict
        except Exception:
            pass
        return conflict
    except Exception:
        return False

def _fetch_tmdb_titles(tmdb_id, is_series=False):
    # Cache + 3 idiomas en paralelo (es-MX, es-ES y EN siempre: el titulo
    # original ingles es el que casa con webs extranjeras) + 1 reintento por
    # idioma + timeout holgado (5s) para Celeron con red lenta.
    titles = []
    if not tmdb_id:
        return titles
    tmdb_id_str = str(tmdb_id).strip()
    if not tmdb_id_str.isdigit():
        return titles
    cache_key = f"{tmdb_id_str}_{1 if is_series else 0}"
    if cache_key in _tmdb_titles_cache:
        try:
            xbmc.log(f"Multi Bridge: tmdb titles cache-hit {cache_key}: {_tmdb_titles_cache[cache_key]}", xbmc.LOGINFO)
        except: pass
        return list(_tmdb_titles_cache[cache_key])
    try:
        api_key = "a1ab8b8669da03637a4b98fa39c39228"
        tmdb_type = "tv" if is_series else "movie"
        import json as _json
        import urllib.request as _ureq
        langs = ["es-MX", "es-ES", "en"]
        results = {}
        def _fetch_lang(lang):
            try:
                url = f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id_str}?api_key={api_key}&language={lang}"
                # Intento con urllib (5s) sin pasar por httptools para no bloquear lock; 1 reintento
                for _att in range(2):
                    try:
                        with _ureq.urlopen(url, timeout=5) as resp2:
                            raw = resp2.read().decode('utf-8', errors='ignore')
                            data = _json.loads(raw)
                            if data:
                                t = data.get('title') or data.get('name') or ""
                                o = data.get('original_title') or data.get('original_name') or ""
                                results[lang] = [x for x in [t, o] if x]
                                break
                    except Exception:
                        results[lang] = []
                        continue
            except Exception:
                results[lang] = []
        # Paralelo
        import threading
        threads = []
        for lang in langs:
            th = threading.Thread(target=_fetch_lang, args=(lang,), daemon=True)
            th.start()
            threads.append(th)
        for th in threads:
            th.join(timeout=5.5)
        for lang in langs:
            try:
                xbmc.log(f"Multi Bridge: tmdb titles {lang}: {results.get(lang, [])}", xbmc.LOGINFO)
            except: pass
            for cand in results.get(lang, []):
                if cand and cand not in titles:
                    titles.append(cand)
        # Cache
        uniq = []
        seen = set()
        for t in titles:
            if not t:
                continue
            low = t.lower().strip()
            if low in seen:
                continue
            seen.add(low)
            uniq.append(t)
        _tmdb_titles_cache[cache_key] = list(uniq)
        return uniq
    except Exception as e:
        try:
            xbmc.log(f"Multi Bridge: _fetch_tmdb_titles error: {e}", xbmc.LOGINFO)
        except:
            pass
        return []
        return []


def _enrich_with_tmdb(results, engine):
    # Desactivado intencionalmente: busquedas a ciegas en TMDb sobre resultados de canales
    # sin contexto de destino sobreescriben items con metadatos de peliculas equivocadas
    # (ej. 'El Origen' -> TMDb ID 8355 'Ice Age 3: El origen de los dinosaurios'),
    # generan alta latencia y contaminan la lista.
    # Los metadatos canonicos de TMDb Helper (meta) se aplican de forma fidedigna.
    return

# ---------------------------------------------------------
# ---------------------------------------------------------
# Search Implementation: Alfa Engine & Balandro Engine
# ---------------------------------------------------------
def _search_planb(target_title, target_year, is_series, s_num, e_num, all_names, target_tmdb=None, target_imdb=None):
    # PlanB: intentar con cada variante de titulo (por si vitaminar depende del titulo) y con tmdb/imdb
    try:
        mods = _get_alfa_modules()
        if not mods:
            return None, None
        Item = mods['Item']
        planb = None
        with _engine_lock:
            _switch_engine_environment('alfa')
            try:
                from lib import planb_py3 as planb
            except Exception:
                try:
                    from lib import planb
                except Exception:
                    planb = None
        if not planb:
            return None, None
        try:
            alt_for_planb = [t for t in (all_names or []) if t and t != target_title]
            terms_for_planb = _build_bridge_terms(target_title, alt_for_planb, all_names)
            if not terms_for_planb:
                terms_for_planb = [_clean_search_term(target_title) or target_title]
        except:
            terms_for_planb = [target_title]
        for try_title in terms_for_planb:
            it_vit = Item(
                channel='PlanB',
                action='vitaminar',
                contentType='episode' if is_series else 'movie',
                contentTitle=try_title,
                infoLabels={
                    'tmdb_id': str(target_tmdb or ''),
                    'imdb_id': str(target_imdb or ''),
                    'year': str(target_year or ''),
                    'title': try_title,
                    'mediatype': 'episode' if is_series else 'movie'
                }
            )
            if is_series:
                try:
                    it_vit.contentSeason = int(s_num or 1)
                except:
                    pass
                try:
                    it_vit.contentEpisodeNumber = int(e_num or 1)
                except:
                    pass
                try:
                    it_vit.infoLabels['season'] = int(s_num or 1)
                except:
                    pass
                try:
                    it_vit.infoLabels['episode'] = int(e_num or 1)
                except:
                    pass
                it_vit.infoLabels['tvshowtitle'] = try_title
            raw_links = []
            with silenced_dialogs():
                if hasattr(planb, 'vitaminar'):
                    try:
                        raw_links = planb.vitaminar(it_vit) or []
                    except Exception as e:
                        xbmc.log("Multi Bridge: PlanB vitaminar error title '%s': %s" % (try_title, str(e)), xbmc.LOGINFO)
                if not raw_links and hasattr(planb, 'findvideos'):
                    try:
                        raw_links = planb.findvideos(it_vit) or []
                    except Exception as e:
                        xbmc.log("Multi Bridge: PlanB findvideos error title '%s': %s" % (try_title, str(e)), xbmc.LOGINFO)
            if raw_links and isinstance(raw_links, list):
                valid_links = []
                for l in raw_links:
                    if getattr(l, 'action', '') == 'play' or getattr(l, 'server', '') or getattr(l, 'url', ''):
                        l.channel = 'PlanB'
                        l.is_vitamina = True
                        l.bridge_engine = 'alfa'
                        valid_links.append(l)
                if valid_links:
                    xbmc.log("Multi Bridge: PlanB encontro %d enlaces para '%s' (try_title='%s')" % (len(valid_links), target_title, try_title), xbmc.LOGINFO)
                    return it_vit, valid_links
            xbmc.log("Multi Bridge: PlanB sin enlaces para try_title='%s'" % try_title, xbmc.LOGINFO)
    except Exception as e:
        xbmc.log("Multi Bridge: PlanB search error: " + str(e), xbmc.LOGWARNING)
        import traceback
        xbmc.log(traceback.format_exc(), xbmc.LOGINFO)
    return None, None

def _search_channel_alfa(channel_id, target_title, target_year, is_series, s_num, e_num, all_names, base_item=None, alt_terms=None, target_tmdb=None, target_imdb=None):
    if str(channel_id).lower() in ('planb', 'plan_b'):
        return _search_planb(target_title, target_year, is_series, s_num, e_num, all_names, target_tmdb=target_tmdb, target_imdb=target_imdb)
    mods = _get_alfa_modules()
    if not mods:
        return None, None
    Item = mods['Item']
    if is_series:
        cleaned_target = _clean_search_term(target_title)
        terms_to_try = [cleaned_target] if cleaned_target else []
    else:
        terms_to_try = _build_bridge_terms(target_title, alt_terms, all_names)
    if not terms_to_try:
        cleaned = _clean_search_term(target_title)
        if cleaned:
            terms_to_try = [cleaned]
        else:
            return None, None
    xbmc.log("Multi Bridge: %s terms_to_try=%s" % (channel_id, terms_to_try), xbmc.LOGINFO)
    canal = None
    with _engine_lock:
        try:
            _switch_engine_environment('alfa')
            canal = __import__('channels.' + channel_id, fromlist=[''])
            try:
                if hasattr(canal, 'canonical') and isinstance(canal.canonical, dict):
                    canal.canonical['global_search_active'] = True
            except:
                pass
            if hasattr(canal, 'platformtools') and getattr(canal, 'platformtools', None):
                try:
                    canal.platformtools.dialog_yesno = lambda *args, **kwargs: True
                    canal.platformtools.dialog_ok = lambda *args, **kwargs: None
                    canal.platformtools.dialog_select = lambda *args, **kwargs: -1
                    canal.platformtools.dialog_multiselect = lambda *args, **kwargs: []
                    canal.platformtools.dialog_notification = lambda *args, **kwargs: None
                except:
                    pass
            if hasattr(canal, 'tmdb') and getattr(canal, 'tmdb', None):
                try:
                    canal.tmdb.set_infoLabels = lambda source=None, *args, **kwargs: (source if isinstance(source, list) else [])
                    canal.tmdb.set_infoLabels_itemlist = lambda source=None, *args, **kwargs: (source if isinstance(source, list) else [])
                    canal.tmdb.set_infoLabels_item = lambda *args, **kwargs: 0
                except:
                    pass
            if hasattr(canal, 'httptools') and getattr(canal, 'httptools', None):
                try:
                    _ht = canal.httptools
                    _orig_dp_proxy = getattr(_ht, 'downloadpage_proxy', None)
                    if _orig_dp_proxy:
                        def _fast_dp_proxy(c_name, url, *args, **kwargs):
                            kwargs['timeout'] = min(3, kwargs.get('timeout', 3) or 3)
                            try:
                                r = _ht.downloadpage(url, *args, **kwargs)
                                if r and getattr(r, 'sucess', False) and len(getattr(r, 'data', '') or '') > 200:
                                    return r
                            except: pass
                            return type('Resp', (), {'sucess': False, 'code': 500, 'data': '', 'headers': {}})()
                        _ht.downloadpage_proxy = _fast_dp_proxy
                except:
                    pass
            if hasattr(canal, 'read_api') and callable(getattr(canal, 'read_api', None)):
                try:
                    if not getattr(canal.read_api, '_bridge_hooked', False):
                        _orig_ra = canal.read_api
                        def _hooked_ra(*args, **kwargs):
                            data = _orig_ra(*args, **kwargs)
                            try:
                                if isinstance(data, dict):
                                    candidates_lists = []
                                    d_data = data.get('data') if isinstance(data.get('data'), dict) else data
                                    if isinstance(d_data, dict):
                                        for k in ('posts', 'searchFilm', 'items', 'films', 'movies', 'results'):
                                            v = d_data.get(k)
                                            if isinstance(v, list): candidates_lists.append(v)
                                        pag = d_data.get('paginationFilm')
                                        if isinstance(pag, dict) and isinstance(pag.get('items'), list):
                                            candidates_lists.append(pag['items'])

                                    if not hasattr(canal, '_bridge_post_metadata'):
                                        canal._bridge_post_metadata = {}

                                    for cl in candidates_lists:
                                        for p in cl:
                                            if isinstance(p, dict):
                                                pid = p.get('_id') or p.get('id')
                                                rd = str(p.get('release_date') or p.get('first_air_date') or p.get('premiered') or '').strip()
                                                t_str = str(p.get('title') or p.get('name') or p.get('name_es') or '').strip()
                                                orig_t = str(p.get('original_title') or p.get('original_name') or '').strip()
                                                slug_str = str(p.get('slug') or '').strip()
                                                y_found = None
                                                if rd and len(rd) >= 4 and rd[:4].isdigit():
                                                    y_found = int(rd[:4])
                                                elif t_str:
                                                    m_y = re.search(r'\b(19\d\d|20[0-3]\d)\b', t_str)
                                                    if m_y:
                                                        y_found = int(m_y.group(1))
                                                if not y_found and slug_str:
                                                    m_s = re.search(r'\b(19\d\d|20[0-3]\d)\b', slug_str)
                                                    if m_s:
                                                        y_found = int(m_s.group(1))
                                                if pid:
                                                    entry = {
                                                        'year': y_found,
                                                        'title': t_str,
                                                        'original_title': orig_t,
                                                        'release_date': rd
                                                    }
                                                    canal._bridge_post_metadata[str(pid)] = entry
                                                    try: canal._bridge_post_metadata[int(pid)] = entry
                                                    except: pass
                            except Exception:
                                pass
                            return data
                        _hooked_ra._bridge_hooked = True
                        canal.read_api = _hooked_ra
                except Exception:
                    pass
        except Exception as e:
            xbmc.log("Multi Bridge: %s import error: %s" % (channel_id, str(e)), xbmc.LOGINFO)
            return None, None

    search_actions = []
    if not base_item and hasattr(canal, 'mainlist'):
        try:
            with silenced_dialogs():
                mainlist_items = canal.mainlist(Item(channel=channel_id))
                search_actions = [elem for elem in (mainlist_items or []) if getattr(elem, 'action', '') == 'search']
        except Exception as e:
            xbmc.log("Multi Bridge: %s mainlist error: %s" % (channel_id, str(e)), xbmc.LOGINFO)
            search_actions = []

    for cur_term in terms_to_try:
        if xbmc.Monitor().abortRequested():
            break
        results = []
        with silenced_dialogs():
            if search_actions:
                for s_act in search_actions:
                    try:
                        res = canal.search(s_act, cur_term)
                        if isinstance(res, list) and res:
                            results.extend(res)
                    except TypeError:
                        try:
                            res = canal.search(s_act, cur_term, 'tvshow' if is_series else 'movie')
                            if isinstance(res, list) and res:
                                results.extend(res)
                        except Exception as e2:
                            xbmc.log("Multi Bridge: %s search(s_act) TypeError fallback failed: %s" % (channel_id, str(e2)), xbmc.LOGINFO)
                    except Exception as e:
                        xbmc.log("Multi Bridge: %s search(s_act) error term '%s': %s" % (channel_id, cur_term, str(e)), xbmc.LOGINFO)
            else:
                try:
                    if base_item and hasattr(base_item, 'clone'):
                        try:
                            it_search = base_item.clone()
                        except:
                            it_search = Item(channel=channel_id)
                    else:
                        it_search = Item(channel=channel_id)
                    it_search.channel = channel_id
                    it_search.buscando = cur_term
                    it_search.text = cur_term
                    it_search.terms = cur_term
                    it_search.term = cur_term
                    it_search.c_type = 'search'
                    it_search.search_type = 'tvshow' if is_series else 'movie'
                    if not hasattr(it_search, 'infoLabels') or not isinstance(getattr(it_search, 'infoLabels', None), dict):
                        it_search.infoLabels = {}
                    if target_tmdb:
                        it_search.infoLabels['tmdb_id'] = str(target_tmdb)
                    if target_imdb:
                        it_search.infoLabels['imdb_id'] = str(target_imdb)
                    if target_year:
                        it_search.infoLabels['year'] = str(target_year)
                    if is_series:
                        it_search.contentSerieName = cur_term
                        it_search.contentType = 'tvshow'
                    else:
                        it_search.contentTitle = cur_term
                        it_search.contentType = 'movie'
                    ch_host = None
                    try:
                        if hasattr(canal, 'canonical') and isinstance(canal.canonical, dict):
                            alt = canal.canonical.get('host_alt', [])
                            if alt and isinstance(alt, list) and alt and alt[0].startswith('http'):
                                ch_host = alt[0]
                        if not ch_host and hasattr(canal, 'host') and getattr(canal, 'host', '') and str(canal.host).startswith('http'):
                            ch_host = str(canal.host)
                    except:
                        pass
                    if not getattr(it_search, 'url', '') or not str(it_search.url).startswith('http'):
                        if ch_host:
                            it_search.url = ch_host
                    if hasattr(canal, 'search'):
                        try:
                            res = canal.search(it_search, cur_term)
                            if isinstance(res, list) and res:
                                results.extend(res)
                        except TypeError:
                            try:
                                res = canal.search(it_search, cur_term, 'tvshow' if is_series else 'movie')
                                if isinstance(res, list) and res:
                                    results.extend(res)
                            except Exception as e2:
                                xbmc.log("Multi Bridge: %s fallback TypeError2: %s" % (channel_id, str(e2)), xbmc.LOGINFO)
                        except Exception as e:
                            xbmc.log("Multi Bridge: %s fallback search error: %s" % (channel_id, str(e)), xbmc.LOGINFO)
                    elif hasattr(canal, 'list_all'):
                        try:
                            res = canal.list_all(it_search)
                            if isinstance(res, list) and res:
                                results.extend(res)
                        except Exception as e:
                            xbmc.log("Multi Bridge: %s list_all fallback error: %s" % (channel_id, str(e)), xbmc.LOGINFO)
                except Exception as e:
                    xbmc.log("Multi Bridge: %s fallback construction error: %s" % (channel_id, str(e)), xbmc.LOGINFO)
        if not results:
            xbmc.log("Multi Bridge: %s sin resultados para '%s'" % (channel_id, cur_term), xbmc.LOGINFO)
            continue
        xbmc.log("Multi Bridge: %s obtuvo %d resultados para '%s'" % (channel_id, len(results), cur_term), xbmc.LOGINFO)
        if results:
            for it in results:
                try:
                    # 1. Rescate por metadatos de API capturados
                    if hasattr(canal, '_bridge_post_metadata'):
                        pid = getattr(it, '_id', None)
                        if pid is None: pid = getattr(it, 'id', None)
                        if pid is not None and pid in canal._bridge_post_metadata:
                            p_meta = canal._bridge_post_metadata[pid]
                            y_val = p_meta.get('year')
                            if y_val:
                                if not hasattr(it, 'infoLabels') or not isinstance(getattr(it, 'infoLabels', None), dict):
                                    it.infoLabels = {}
                                it.infoLabels['year'] = y_val
                                if p_meta.get('release_date'):
                                    it.infoLabels['release_date'] = p_meta.get('release_date')
                                it.year = y_val
                                if target_year and str(y_val) == str(target_year).strip():
                                    setattr(it, '_web_year_confirmed', True)
                                xbmc.log("Multi Bridge: %s rescatado año %s para '%s' de metadatos API" % (channel_id, y_val, getattr(it, 'title', '')), xbmc.LOGINFO)
                            if p_meta.get('title') and not getattr(it, 'contentTitle', None):
                                it.contentTitle = p_meta.get('title')

                    # 2. Rescate universal para cualquier canal presente o futuro:
                    # Si el item no tiene año en infoLabels/year pero su título o contenido incluye (YYYY) o [YYYY]
                    it_y = None
                    if hasattr(it, 'infoLabels') and isinstance(getattr(it, 'infoLabels', None), dict):
                        it_y = it.infoLabels.get('year')
                    if not it_y:
                        it_y = getattr(it, 'year', None)
                    if not it_y:
                        t_check = getattr(it, 'title', '') or getattr(it, 'contentTitle', '') or ''
                        m_y = re.search(r'[\(\[]\s*(19\d\d|20[0-3]\d)\s*[\)\]]', str(t_check))
                        if m_y:
                            y_parsed = int(m_y.group(1))
                            if not hasattr(it, 'infoLabels') or not isinstance(getattr(it, 'infoLabels', None), dict):
                                it.infoLabels = {}
                            it.infoLabels['year'] = y_parsed
                            it.year = y_parsed
                            if target_year and y_parsed == int(str(target_year).strip()):
                                setattr(it, '_web_year_confirmed', True)
                except Exception:
                    pass
        _enrich_with_tmdb(results, 'alfa')
        candidates = []
        for it in results:
            title_check = getattr(it, 'title', '') or getattr(it, 'contentTitle', '') or getattr(it, 'contentSerieName', '')
            if not title_check:
                continue
            # Filtrar items de paginacion / siguiente pagina que contaminan resultados (ej. CineCalidad, PelisPedia, HDFull)
            _low_title = _safe_str(title_check).lower().strip()
            if any(p in _low_title for p in ['siguiente', 'siguientes', 'next page', 'pagina siguiente', 'página siguiente', 'anterior', 'anteriores']) or _low_title.startswith('>>') or _low_title.startswith('<<') or _low_title.endswith('>>') or _low_title.endswith('<<'):
                xbmc.log(f"Multi Bridge: {channel_id} skip pagination '{title_check}'", xbmc.LOGINFO)
                continue
            score = score_match(title_check, target_year, all_names, target_tmdb=target_tmdb, item=it, target_imdb=target_imdb, is_series=is_series)
            if score <= 0:
                continue
            candidates.append((score, it, title_check))

        candidates.sort(key=lambda c: c[0], reverse=True)

        for score, it, title_check in candidates:
            # Confianza del match (pelis y series): sin TMDb/IMDb ni año no se puede
            # distinguir homonimos (ej. Horas desesperadas 1990 vs 2013, o series homónimas).
            try:
                _w_weak = (not _get_item_tmdb(it) and not _get_item_imdb(it)
                           and not _get_item_year(it, title_check))
            except Exception:
                _w_weak = False
            xbmc.log("Multi Bridge: %s MATCH (score=%d) '%s' para term '%s' (target_year=%s tmdb=%s)%s" % (channel_id, score, title_check, cur_term, target_year, target_tmdb, ' [match debil: sin anio ni ID]' if _w_weak else ''), xbmc.LOGINFO)
            # Chequeo de pagina real: si el match es debil y conocemos el año,
            # se verifica el año que muestra la web (con aviso). Si la web
            # muestra otro año, se salta este candidato (sin tocar el canal).
            if _w_weak and (target_year or target_imdb or target_tmdb):
                try:
                    if not _detail_year_consistent(getattr(it, 'url', ''), target_year, channel_id, item=it, target_imdb=target_imdb, target_tmdb=target_tmdb, is_series=is_series):
                        xbmc.log("Multi Bridge: %s '%s' descartado: la web muestra otro año, ID o reparto" % (channel_id, title_check), xbmc.LOGINFO)
                        continue
                    if getattr(it, '_web_year_confirmed', False):
                        _w_weak = False
                        if target_tmdb:
                            try:
                                if not hasattr(it, 'infoLabels') or not isinstance(getattr(it, 'infoLabels', None), dict):
                                    it.infoLabels = {}
                                it.infoLabels['tmdb_id'] = str(target_tmdb)
                                it.infoLabels['tmdb'] = str(target_tmdb)
                                it.tmdb_id = str(target_tmdb)
                            except: pass
                        xbmc.log("Multi Bridge [Alfa]: %s confirmado en web (%s), match verificado (no débil)" % (channel_id, target_year or target_imdb or target_tmdb), xbmc.LOGINFO)
                except Exception:
                    pass
            # Conflicto de identidad TMDb (solo Alfa): aunque el match sea
            # FUERTE por IDs, si el titulo traia parentesis con texto
            # significativo ('(Cuenta Atras)') que el limpiador borra, TMDb
            # puede reconocer ese titulo exacto como OTRA pelicula
            # (Hours 2013 vs tmdb 18612). En ese caso se rechaza.
            try:
                if not is_series and target_tmdb:
                    _extra_txt = _stripped_paren_extra(title_check)
                    if _extra_txt and _tmdb_identity_conflict(title_check, target_tmdb, target_year):
                        xbmc.log("Multi Bridge: %s '%s' descartado: TMDb lo identifica como otra pelicula (parentesis '%s')" % (channel_id, title_check, _extra_txt), xbmc.LOGINFO)
                        continue
            except: pass
            if is_series and hasattr(canal, 'episodios'):
                try:
                    if target_tmdb:
                        if not hasattr(it, 'infoLabels') or not isinstance(getattr(it, 'infoLabels', None), dict):
                            it.infoLabels = {}
                        it.infoLabels['tmdb_id'] = str(target_tmdb)
                        it.infoLabels['tvdb_id'] = str(target_tmdb)
                        it.tvdb_id = str(target_tmdb)
                    it.perpage = 500
                    it.page = 0
                    with silenced_dialogs():
                        ep_list = canal.episodios(it)
                    if not ep_list:
                        xbmc.log("Multi Bridge: %s episodios vacio para '%s'" % (channel_id, title_check), xbmc.LOGINFO)
                    for ep in (ep_list or []):
                        ep_season = int(getattr(ep, 'contentSeason', 0) or getattr(ep, 'infoLabels', {}).get('season', 0) or getattr(ep, 'season', 0) or 0)
                        ep_episode = int(getattr(ep, 'contentEpisodeNumber', 0) or getattr(ep, 'infoLabels', {}).get('episode', 0) or getattr(ep, 'episode', 0) or 0)
                        if ep_season == int(s_num or 1) and ep_episode == int(e_num or 1):
                            links = None
                            try:
                                with silenced_dialogs():
                                    links = canal.findvideos(ep) if hasattr(canal, 'findvideos') else None
                            except Exception as e:
                                xbmc.log("Multi Bridge: %s findvideos(ep) error: %s" % (channel_id, str(e)), xbmc.LOGINFO)
                            if links and isinstance(links, list) and len(links) > 0:
                                valid = [l for l in links if getattr(l, 'url', '') or getattr(l, 'server', '') or getattr(l, 'action', '') == 'play']
                                if valid:
                                    for l in valid:
                                        l.channel = channel_id
                                        l.bridge_engine = 'alfa'
                                        try:
                                            l.bridge_score = int(score)
                                            l.bridge_weak = bool(_w_weak)
                                        except: pass
                                    xbmc.log("Multi Bridge: %s OK serie %dx%d %d enlaces" % (channel_id, ep_season, ep_episode, len(valid)), xbmc.LOGINFO)
                                    return ep, valid
                                else:
                                    xbmc.log("Multi Bridge: %s findvideos(ep) sin enlaces validos" % channel_id, xbmc.LOGINFO)
                            else:
                                xbmc.log("Multi Bridge: %s findvideos(ep) vacio" % channel_id, xbmc.LOGINFO)
                except Exception as e:
                    xbmc.log("Multi Bridge: %s episodios exception: %s" % (channel_id, str(e)), xbmc.LOGINFO)
                    import traceback
                    xbmc.log(traceback.format_exc(), xbmc.LOGINFO)
            elif hasattr(canal, 'findvideos'):
                try:
                    with silenced_dialogs():
                        links = canal.findvideos(it)
                    if links and isinstance(links, list) and len(links) > 0:
                        valid = [l for l in links if getattr(l, 'url', '') or getattr(l, 'server', '') or getattr(l, 'action', '') == 'play']
                        if valid:
                            # Inyeccion TMDb en vivo: si el match es debil, se
                            # confirma contra la API y se inyecta el tmdb
                            # correcto al item (solo memoria, sin tocar disco).
                            _w_conf = bool(getattr(it, '_web_year_confirmed', False))
                            if _w_weak and not _w_conf:
                                try:
                                    _w_conf = _verify_candidate_tmdb(title_check, target_year, target_tmdb, is_series=False)
                                    if _w_conf:
                                        try:
                                            if not hasattr(it, 'infoLabels') or not isinstance(getattr(it, 'infoLabels', None), dict):
                                                it.infoLabels = {}
                                            it.infoLabels['tmdb_id'] = str(target_tmdb)
                                            it.infoLabels['tmdb'] = str(target_tmdb)
                                            it.tmdb_id = str(target_tmdb)
                                        except: pass
                                        xbmc.log("Multi Bridge: %s TMDb confirmado (%s), inyectado" % (channel_id, target_tmdb), xbmc.LOGINFO)
                                except: pass
                            for l in valid:
                                l.channel = channel_id
                                l.bridge_engine = 'alfa'
                                try:
                                    l.bridge_score = int(score)
                                    l.bridge_weak = bool(_w_weak and not _w_conf)
                                except: pass
                            xbmc.log("Multi Bridge: %s OK peli %d enlaces para '%s'" % (channel_id, len(valid), title_check), xbmc.LOGINFO)
                            return it, valid
                        else:
                            xbmc.log("Multi Bridge: %s findvideos sin enlaces validos para '%s'" % (channel_id, title_check), xbmc.LOGINFO)
                    else:
                        xbmc.log("Multi Bridge: %s findvideos vacio para '%s'" % (channel_id, title_check), xbmc.LOGINFO)
                except Exception as e:
                    xbmc.log("Multi Bridge: %s findvideos error: %s" % (channel_id, str(e)), xbmc.LOGINFO)
                    import traceback
                    xbmc.log(traceback.format_exc(), xbmc.LOGINFO)
        if is_series and candidates:
            break
    return None, None

def _search_channel_balandro(channel_id, target_title, target_year, is_series, s_num, e_num, all_names, base_item=None, alt_terms=None, target_tmdb=None, target_imdb=None):
    mods = _get_balandro_modules()
    if not mods:
        return None, None
    Item = mods['Item']
    if is_series:
        cleaned_target = _clean_search_term(target_title)
        terms_to_try = [cleaned_target] if cleaned_target else []
    else:
        terms_to_try = _build_bridge_terms(target_title, alt_terms, all_names)
    if not terms_to_try:
        cleaned = _clean_search_term(target_title)
        if cleaned:
            terms_to_try = [cleaned]
        else:
            return None, None
    xbmc.log("Multi Bridge [Balandro]: %s terms_to_try=%s" % (channel_id, terms_to_try), xbmc.LOGINFO)
    canal = None
    with _engine_lock:
        try:
            _switch_engine_environment('balandro')
            canal = __import__('channels.' + channel_id, fromlist=[''])
            try:
                if hasattr(canal, 'canonical') and isinstance(canal.canonical, dict):
                    canal.canonical['global_search_active'] = True
            except:
                pass
            if hasattr(canal, 'platformtools') and getattr(canal, 'platformtools', None):
                try:
                    canal.platformtools.dialog_yesno = lambda *args, **kwargs: True
                    canal.platformtools.dialog_ok = lambda *args, **kwargs: None
                    canal.platformtools.dialog_select = lambda *args, **kwargs: -1
                    canal.platformtools.dialog_multiselect = lambda *args, **kwargs: []
                    canal.platformtools.dialog_notification = lambda *args, **kwargs: None
                except:
                    pass
            if hasattr(canal, 'tmdb') and getattr(canal, 'tmdb', None):
                try:
                    canal.tmdb.set_infoLabels = lambda source=None, *args, **kwargs: (source if isinstance(source, list) else [])
                    canal.tmdb.set_infoLabels_itemlist = lambda source=None, *args, **kwargs: (source if isinstance(source, list) else [])
                    canal.tmdb.set_infoLabels_item = lambda *args, **kwargs: 0
                except:
                    pass
            if hasattr(canal, 'httptools') and getattr(canal, 'httptools', None):
                try:
                    _ht = canal.httptools
                    if getattr(getattr(_ht, 'downloadpage_proxy', None), '_bridge_fast', False) is not True:
                        def _fast_dp_proxy(c_name, url, *args, **kwargs):
                            kwargs['timeout'] = min(5, kwargs.get('timeout', 5) or 5)
                            try:
                                r = _ht.downloadpage(url, *args, **kwargs)
                                if r and getattr(r, 'sucess', False) and len(getattr(r, 'data', '') or '') > 200:
                                    return r
                            except: pass
                            return type('Resp', (), {'sucess': False, 'code': 500, 'data': '', 'headers': {}})()
                        _fast_dp_proxy._bridge_fast = True
                        _ht.downloadpage_proxy = _fast_dp_proxy
                except:
                    pass
            # Parche en runtime (solo memoria, sin tocar disco externo):
            # pelispanda.episodios() usa un regex con multiples ".*?" + DOTALL
            # sobre el JSON completo de la serie (~58KB) que provoca backtracking
            # catastrofico (100-150s reteniendo el GIL = freeze total de Kodi).
            # Se sustituye la extraccion por json.loads (0.003s) replicando su
            # logica posterior (paginacion, titulos, idiomas). Si el JSON falla,
            # se delega a la funcion original del canal.
            if str(channel_id).lower() == 'pelispanda' and hasattr(canal, 'episodios'):
                try:
                    if getattr(canal.episodios, '_bridge_json_safe', False) is not True:
                        _orig_panda_episodios = canal.episodios
                        def _pelispanda_episodios_safe(item, *args, **kwargs):
                            try:
                                import json as _pjs
                                import re as _pre
                                if not getattr(item, 'page', None): item.page = 0
                                if not getattr(item, 'perpage', None): item.perpage = 50
                                _data = canal.do_downloadpage(item.url)
                                _data = _pre.sub(r'\n|\r|\t|\s{2}|&nbsp;', '', _data)
                                _matches = []
                                _obj = _pjs.loads(_data)
                                _dls = _obj.get('downloads', []) if isinstance(_obj, dict) else []
                                _season = str(getattr(item, 'contentSeason', ''))
                                for _d in (_dls or []):
                                    if not isinstance(_d, dict): continue
                                    if str(_d.get('season', '')) != _season: continue
                                    _matches.append((str(_d.get('episode', '')), str(_d.get('quality', '')),
                                                     str(_d.get('size', '')), str(_d.get('download_link', '')),
                                                     str(_d.get('language', ''))))
                            except Exception:
                                try:
                                    return _orig_panda_episodios(item, *args, **kwargs)
                                except Exception as _e2:
                                    xbmc.log("Multi Bridge [Balandro]: pelispanda episodios fallback error: %s" % _e2, xbmc.LOGINFO)
                                    return []
                            try:
                                if item.page == 0 and item.perpage == 50:
                                    item.perpage = len(_matches) or 50
                                _itemlist = []
                                for _epis, _qlty, _size, _link, _lang in _matches[item.page * item.perpage:]:
                                    _lang = _lang.replace('\\/', '/')
                                    if 'Latino/Ingles' in _lang: _lang = 'Lat'
                                    elif 'Castellano/Ingles' in _lang: _lang = 'Esp'
                                    elif 'Latino/Japones' in _lang: _lang = 'Vos'
                                    elif 'Castellano' in _lang: _lang = 'Esp'
                                    elif 'Latino' in _lang: _lang = 'Lat'
                                    elif 'Subtitulado' in _lang: _lang = 'Vose'
                                    elif 'Version Original' in _lang: _lang = 'VO'
                                    _link = _link.replace('\\/', '/')
                                    _titulo = str(item.contentSeason) + 'x' + str(_epis) + ' ' + str(getattr(item, 'contentSerieName', '')).replace('&#038;', '&').replace('&#8217;', "'")
                                    try:
                                        _itemlist.append(item.clone(action='findvideos', url=_link, title=_titulo, language=_lang, quality=_qlty, size=_size,
                                                                    contentType='episode', contentSeason=item.contentSeason, contentEpisodeNumber=_epis))
                                    except Exception:
                                        break
                                    if len(_itemlist) >= item.perpage:
                                        break
                                return _itemlist
                            except Exception as _e3:
                                xbmc.log("Multi Bridge [Balandro]: pelispanda episodios safe error: %s" % _e3, xbmc.LOGINFO)
                                return []
                        _pelispanda_episodios_safe._bridge_json_safe = True
                        canal.episodios = _pelispanda_episodios_safe
                except:
                    pass
            # Canales solo-peliculas (sin temporadas ni episodios) no pueden
            # resolver episodios de series: se omiten con log explicito en vez
            # de buscar para nada (ej. repelishd).
            if is_series and not hasattr(canal, 'temporadas') and not hasattr(canal, 'episodios'):
                xbmc.log("Multi Bridge [Balandro]: %s sin soporte para series (solo peliculas), omitido" % channel_id, xbmc.LOGINFO)
                return None, None
        except Exception as e:
            xbmc.log("Multi Bridge [Balandro]: %s import error: %s" % (channel_id, str(e)), xbmc.LOGINFO)
            return None, None

    search_actions = []
    if not base_item and hasattr(canal, 'mainlist'):
        try:
            with silenced_dialogs():
                mainlist_items = canal.mainlist(Item(channel=channel_id))
                search_actions = [elem for elem in (mainlist_items or []) if getattr(elem, 'action', '') == 'search']
        except Exception as e:
            xbmc.log("Multi Bridge [Balandro]: %s mainlist error: %s" % (channel_id, str(e)), xbmc.LOGINFO)
            search_actions = []

    for cur_term in terms_to_try:
        if xbmc.Monitor().abortRequested():
            break
        results = []
        with silenced_dialogs():
            if search_actions:
                for s_act in search_actions:
                    try:
                        res = canal.search(s_act, cur_term)
                        if isinstance(res, list) and res:
                            results.extend(res)
                    except TypeError:
                        try:
                            res = canal.search(s_act, cur_term, 'tvshow' if is_series else 'movie')
                            if isinstance(res, list) and res:
                                results.extend(res)
                        except Exception as e2:
                            xbmc.log("Multi Bridge [Balandro]: %s search TypeError fallback: %s" % (channel_id, str(e2)), xbmc.LOGINFO)
                    except Exception as e:
                        xbmc.log("Multi Bridge [Balandro]: %s search error: %s" % (channel_id, str(e)), xbmc.LOGINFO)
            else:
                try:
                    if base_item and hasattr(base_item, 'clone'):
                        try:
                            it_search = base_item.clone()
                        except:
                            it_search = Item(channel=channel_id)
                    else:
                        it_search = Item(channel=channel_id)
                    it_search.channel = channel_id
                    it_search.buscando = cur_term
                    it_search.text = cur_term
                    it_search.terms = cur_term
                    it_search.term = cur_term
                    it_search.c_type = 'search'
                    it_search.search_type = 'tvshow' if is_series else 'movie'
                    if not hasattr(it_search, 'infoLabels') or not isinstance(getattr(it_search, 'infoLabels', None), dict):
                        it_search.infoLabels = {}
                    if target_tmdb:
                        it_search.infoLabels['tmdb_id'] = str(target_tmdb)
                    if target_imdb:
                        it_search.infoLabels['imdb_id'] = str(target_imdb)
                    if target_year:
                        it_search.infoLabels['year'] = str(target_year)
                    if is_series:
                        it_search.contentSerieName = cur_term
                        it_search.contentType = 'tvshow'
                    else:
                        it_search.contentTitle = cur_term
                        it_search.contentType = 'movie'
                    ch_host = None
                    try:
                        if hasattr(canal, 'host') and getattr(canal, 'host', '') and str(canal.host).startswith('http'):
                            ch_host = str(canal.host)
                    except:
                        pass
                    if not getattr(it_search, 'url', '') or not str(it_search.url).startswith('http'):
                        if ch_host:
                            it_search.url = ch_host
                    if hasattr(canal, 'search'):
                        try:
                            res = canal.search(it_search, cur_term)
                            if isinstance(res, list) and res:
                                results.extend(res)
                        except TypeError:
                            try:
                                res = canal.search(it_search, cur_term, 'tvshow' if is_series else 'movie')
                                if isinstance(res, list) and res:
                                    results.extend(res)
                            except Exception as e2:
                                xbmc.log("Multi Bridge [Balandro]: %s fallback TypeError2: %s" % (channel_id, str(e2)), xbmc.LOGINFO)
                        except Exception as e:
                            xbmc.log("Multi Bridge [Balandro]: %s fallback error: %s" % (channel_id, str(e)), xbmc.LOGINFO)
                    elif hasattr(canal, 'list_all'):
                        try:
                            res = canal.list_all(it_search)
                            if isinstance(res, list) and res:
                                results.extend(res)
                        except Exception as e:
                            xbmc.log("Multi Bridge [Balandro]: %s list_all error: %s" % (channel_id, str(e)), xbmc.LOGINFO)
                except Exception as e:
                    xbmc.log("Multi Bridge [Balandro]: %s fallback construction error: %s" % (channel_id, str(e)), xbmc.LOGINFO)
        if not results:
            xbmc.log("Multi Bridge [Balandro]: %s sin resultados para '%s'" % (channel_id, cur_term), xbmc.LOGINFO)
            continue
        xbmc.log("Multi Bridge [Balandro]: %s obtuvo %d resultados para '%s'" % (channel_id, len(results), cur_term), xbmc.LOGINFO)
        _debug_nomatch_count = 0
        _enrich_with_tmdb(results, 'balandro')
        candidates = []
        for it in results:
            title_check = getattr(it, 'title', '') or getattr(it, 'contentTitle', '') or getattr(it, 'contentSerieName', '')
            if not title_check:
                continue
            # Filtrar items de paginacion / siguiente pagina que contaminan resultados (ej. CineCalidad, PelisPedia, HDFull)
            _low_title = _safe_str(title_check).lower().strip()
            if any(p in _low_title for p in ['siguiente', 'siguientes', 'next page', 'pagina siguiente', 'página siguiente', 'anterior', 'anteriores']) or _low_title.startswith('>>') or _low_title.startswith('<<') or _low_title.endswith('>>') or _low_title.endswith('<<'):
                xbmc.log(f"Multi Bridge: {channel_id} skip pagination '{title_check}'", xbmc.LOGINFO)
                continue
            score = score_match(title_check, target_year, all_names, target_tmdb=target_tmdb, item=it, target_imdb=target_imdb, is_series=is_series)
            if score <= 0:
                if _debug_nomatch_count < 5:
                    _item_yr = _get_item_year(it, title_check)
                    _item_tmdb = _get_item_tmdb(it)
                    xbmc.log("Multi Bridge [Balandro]: %s NO match '%s' yr=%s tmdb=%s" % (channel_id, title_check, _item_yr, _item_tmdb), xbmc.LOGINFO)
                    _debug_nomatch_count += 1
                continue
            candidates.append((score, it, title_check))

        candidates.sort(key=lambda c: c[0], reverse=True)

        for score, it, title_check in candidates:
            try:
                _w_weak = (not _get_item_tmdb(it) and not _get_item_imdb(it)
                           and not _get_item_year(it, title_check))
            except Exception:
                _w_weak = False
            xbmc.log("Multi Bridge [Balandro]: %s MATCH (score=%d) '%s' para term '%s'%s" % (channel_id, score, title_check, cur_term, ' [match debil: sin anio ni ID]' if _w_weak else ''), xbmc.LOGINFO)
            # Chequeo de pagina real: si el match es debil y conocemos el año,
            # se verifica el año que muestra la web (con aviso). Si la web
            # muestra otro año, se salta este candidato (sin tocar el canal).
            if _w_weak and (target_year or target_imdb or target_tmdb):
                try:
                    if not _detail_year_consistent(getattr(it, 'url', ''), target_year, channel_id, item=it, target_imdb=target_imdb, target_tmdb=target_tmdb, is_series=is_series):
                        xbmc.log("Multi Bridge [Balandro]: %s '%s' descartado: la web muestra otro año, ID o reparto" % (channel_id, title_check), xbmc.LOGINFO)
                        continue
                    if getattr(it, '_web_year_confirmed', False):
                        _w_weak = False
                        if target_tmdb:
                            try:
                                if not hasattr(it, 'infoLabels') or not isinstance(getattr(it, 'infoLabels', None), dict):
                                    it.infoLabels = {}
                                it.infoLabels['tmdb_id'] = str(target_tmdb)
                                it.infoLabels['tmdb'] = str(target_tmdb)
                                it.tmdb_id = str(target_tmdb)
                            except: pass
                        xbmc.log("Multi Bridge [Balandro]: %s confirmado en web (%s), match verificado (no débil)" % (channel_id, target_year or target_imdb or target_tmdb), xbmc.LOGINFO)
                except Exception:
                    pass
            if is_series:
                _target_s = int(s_num or 1)
                _target_e = int(e_num or 1)
                _found_ep = False

                def _ep_nums(ep):
                    """Extrae (season, episode) de un item episodio de Balandro."""
                    s = int(getattr(ep, 'contentSeason', 0) or
                            getattr(ep, 'infoLabels', {}).get('season', 0) or 0)
                    e = int(getattr(ep, 'contentEpisodeNumber', 0) or
                            getattr(ep, 'infoLabels', {}).get('episode', 0) or 0)
                    return s, e

                def _try_ep_list(ep_list):
                    """Busca TODOS los items del episodio correcto (distintos idiomas) y devuelve todos sus links."""
                    _all_valid = []
                    _any_ep = None
                    for ep in (ep_list or []):
                        ep_s, ep_e = _ep_nums(ep)
                        if ep_s == _target_s and ep_e == _target_e:
                            _lk = None
                            try:
                                with silenced_dialogs():
                                    _lk = canal.findvideos(ep) if hasattr(canal, 'findvideos') else None
                            except Exception as _fe:
                                xbmc.log('Multi Bridge [Balandro]: %s findvideos error: %s' % (channel_id, _fe), xbmc.LOGINFO)
                            if _lk and isinstance(_lk, list):
                                valid = [l for l in _lk if getattr(l, 'url', '') or getattr(l, 'server', '') or getattr(l, 'action', '') == 'play']
                                if valid:
                                    for l in valid:
                                        l.channel = channel_id
                                        l.bridge_engine = 'balandro'
                                        try:
                                            l.bridge_score = int(score)
                                            l.bridge_weak = bool(_w_weak)
                                        except: pass
                                    _all_valid.extend(valid)
                                    if _any_ep is None:
                                        _any_ep = ep
                    if _all_valid:
                        xbmc.log('Multi Bridge [Balandro]: %s OK serie %sx%s %d enlaces' % (channel_id, _target_s, _target_e, len(_all_valid)), xbmc.LOGINFO)
                        return _any_ep, _all_valid
                    return None, None

                # Patrón estándar Balandro: temporadas() → episodios(season_item)
                if hasattr(canal, 'temporadas') and hasattr(canal, 'episodios'):
                    try:
                        xbmc.log('Multi Bridge [Balandro]: %s show_url=%s' % (channel_id, getattr(it, 'url', '?')[:80]), xbmc.LOGINFO)
                        with silenced_dialogs():
                            seasons = canal.temporadas(it)

                        xbmc.log('Multi Bridge [Balandro]: %s temporadas() -> %d items' % (channel_id, len(seasons) if seasons else 0), xbmc.LOGINFO)
                        if not seasons:
                            pass
                        else:
                            # Detectar si temporadas() ya devolvio episodios directamente
                            # (ocurre en series de 1 sola temporada en hdfull y otros canales)
                            _first = seasons[0]
                            _is_ep_list = (
                                int(getattr(_first, 'contentEpisodeNumber', 0) or 0) > 0 or
                                getattr(_first, 'contentType', '') == 'episode' or
                                getattr(_first, 'action', '') not in ('episodios', 'temporadas', '')
                            )

                            if _is_ep_list:
                                # temporadas() devolvio episodios directamente (1 temporada)
                                xbmc.log('Multi Bridge [Balandro]: %s temporadas devolvio %d eps directo' % (channel_id, len(seasons)), xbmc.LOGINFO)
                                _rep_ep, _rep_lk = _try_ep_list(seasons)
                                if _rep_lk:
                                    return _rep_ep, _rep_lk
                                _found_ep = True
                            else:
                                # Lista de temporadas — buscar la correcta
                                sea_item = None
                                for sea in seasons:
                                    sea_num = int(getattr(sea, 'contentSeason', 0) or
                                                  getattr(sea, 'infoLabels', {}).get('season', 0) or 0)
                                    if sea_num == _target_s:
                                        sea_item = sea
                                        break
                                if sea_item is None:
                                    sea_item = seasons[0]  # fallback a primera temporada
                                xbmc.log('Multi Bridge [Balandro]: %s usando temporada %s' % (channel_id, getattr(sea_item, 'contentSeason', '?')), xbmc.LOGINFO)
                                if target_tmdb:
                                    sea_item.tmdb_id = str(target_tmdb)
                                    if not hasattr(sea_item, 'infoLabels') or not isinstance(sea_item.infoLabels, dict):
                                        sea_item.infoLabels = {}
                                    sea_item.infoLabels['tmdb_id'] = str(target_tmdb)
                                    sea_item.infoLabels['tvdb_id'] = str(target_tmdb)
                                    sea_item.tvdb_id = str(target_tmdb)
                                sea_item.perpage = 500
                                sea_item.page = 0
                                with silenced_dialogs():
                                    ep_list = canal.episodios(sea_item)
                                _rep_ep, _rep_lk = _try_ep_list(ep_list)
                                if _rep_lk:
                                    return _rep_ep, _rep_lk
                                if ep_list:
                                    _found_ep = True
                    except Exception as e:
                        xbmc.log('Multi Bridge [Balandro]: %s temporadas/episodios error: %s' % (channel_id, str(e)), xbmc.LOGINFO)

                # Fallback: intentar episodios() directo en el show (algunos canales lo soportan)
                if not _found_ep and hasattr(canal, 'episodios'):
                    try:
                        _it_fallback = it.clone() if hasattr(it, 'clone') else it
                        _it_fallback.contentSeason = _target_s  # inyectar temporada para canales como homecine
                        if target_tmdb:
                            _it_fallback.tmdb_id = str(target_tmdb)
                            if not hasattr(_it_fallback, 'infoLabels') or not isinstance(_it_fallback.infoLabels, dict):
                                _it_fallback.infoLabels = {}
                            _it_fallback.infoLabels['tmdb_id'] = str(target_tmdb)
                            _it_fallback.infoLabels['tvdb_id'] = str(target_tmdb)
                            _it_fallback.tvdb_id = str(target_tmdb)
                        _it_fallback.perpage = 500
                        _it_fallback.page = 0
                        with silenced_dialogs():
                            ep_list = canal.episodios(_it_fallback)
                        xbmc.log('Multi Bridge [Balandro]: %s episodios fallback(s=%s) -> %d items' % (channel_id, _target_s, len(ep_list) if ep_list else 0), xbmc.LOGINFO)
                        _rep_ep, _rep_lk = _try_ep_list(ep_list)
                        if _rep_lk:
                            return _rep_ep, _rep_lk
                    except Exception as e:
                        xbmc.log('Multi Bridge [Balandro]: %s episodios directo error: %s' % (channel_id, str(e)), xbmc.LOGINFO)

            elif hasattr(canal, 'findvideos'):
                try:
                    with silenced_dialogs():
                        links = canal.findvideos(it)
                    if links and isinstance(links, list) and len(links) > 0:
                        valid = [l for l in links if getattr(l, 'url', '') or getattr(l, 'server', '') or getattr(l, 'action', '') == 'play']
                        if valid:
                            # Inyeccion TMDb en vivo: si el match es debil, se
                            # confirma contra la API y se inyecta el tmdb
                            # correcto al item (solo memoria, sin tocar disco).
                            _w_conf = bool(getattr(it, '_web_year_confirmed', False))
                            if _w_weak and not _w_conf:
                                try:
                                    _w_conf = _verify_candidate_tmdb(title_check, target_year, target_tmdb, is_series=False)
                                    if _w_conf:
                                        try:
                                            if not hasattr(it, 'infoLabels') or not isinstance(getattr(it, 'infoLabels', None), dict):
                                                it.infoLabels = {}
                                            it.infoLabels['tmdb_id'] = str(target_tmdb)
                                            it.infoLabels['tmdb'] = str(target_tmdb)
                                            it.tmdb_id = str(target_tmdb)
                                        except: pass
                                        xbmc.log("Multi Bridge [Balandro]: %s TMDb confirmado (%s), inyectado" % (channel_id, target_tmdb), xbmc.LOGINFO)
                                except: pass
                            for l in valid:
                                l.channel = channel_id
                                l.bridge_engine = 'balandro'
                                try:
                                    l.bridge_score = int(score)
                                    l.bridge_weak = bool(_w_weak and not _w_conf)
                                except: pass
                            return it, valid
                except Exception as e:
                    xbmc.log("Multi Bridge [Balandro]: %s findvideos error: %s" % (channel_id, str(e)), xbmc.LOGINFO)
        if is_series and candidates:
            break
    return None, None

def _search_on_player(player_data_or_file, engine, is_series, target_title, target_year, s_num, e_num, all_names, alt_terms=None, target_tmdb=None, target_imdb=None):
    try:
        if isinstance(player_data_or_file, dict):
            data = player_data_or_file
            target_channel = data.get('channel') or data.get('id') or ''
            p_engine = data.get('engine', engine)
        else:
            player_file = str(player_data_or_file)
            fpath = os.path.join(TMDB_PLAYERS_PATH, player_file)
            if os.path.exists(fpath):
                with open(fpath, 'r', encoding='utf-8') as f: data = json.load(f)
            else:
                ch_clean = player_file.lower().replace('.json', '')
                prefix = 'alfa-' if engine == 'alfa' else 'balandro-'
                if ch_clean.startswith(prefix): ch_clean = ch_clean[len(prefix):]
                ch_clean = re.sub(r'-(series|movies?)$', '', ch_clean).strip()
                ch_map = load_channels_data().get('channels', {}).get(engine, {})
                data = ch_map.get(ch_clean, {})
            target_channel = data.get('channel', '')
            p_engine = 'alfa' if player_file.lower().startswith('alfa-') else ('balandro' if player_file.lower().startswith('balandro-') else engine)

        key = 'play_episode' if is_series else 'play_movie'
        url_list = data.get(key, [])
        url_str = ''
        if isinstance(url_list, list) and url_list:
            url_str = url_list[0]
        elif isinstance(url_list, str):
            url_str = url_list

        mods = _get_alfa_modules() if p_engine == 'alfa' else _get_balandro_modules()
        if not mods: return None, None
        ItemClass = mods['Item']
        search_item = None

        if url_str:
            if 'plugin://plugin.video.alfa/?' in url_str:
                raw_b64 = url_str.split('plugin://plugin.video.alfa/?')[1].split('&')[0]
                raw_b64 = uparse.unquote(raw_b64)
                search_item = decode_base64_item(raw_b64, ItemClass)
                if not target_channel:
                    target_channel = getattr(search_item, 'channel', '')
            elif 'plugin://plugin.video.balandro/?' in url_str:
                raw_b64 = url_str.split('plugin://plugin.video.balandro/?')[1].split('&')[0]
                raw_b64 = uparse.unquote(raw_b64)
                search_item = decode_base64_item(raw_b64, ItemClass)
                if not target_channel:
                    target_channel = getattr(search_item, 'channel', '')

        if not target_channel:
            target_channel = str(data.get('id', '')).lower()

        if not target_channel or target_channel == 'search': return None, None

        if p_engine == 'alfa':
            res_it, res_links = _search_channel_alfa(target_channel, target_title, target_year, is_series, s_num, e_num, all_names, base_item=search_item, alt_terms=alt_terms, target_tmdb=target_tmdb, target_imdb=target_imdb)
            if res_links:
                for l in res_links: l.bridge_engine = 'alfa'
            return res_it, res_links
        else:
            res_it, res_links = _search_channel_balandro(target_channel, target_title, target_year, is_series, s_num, e_num, all_names, base_item=search_item, alt_terms=alt_terms, target_tmdb=target_tmdb, target_imdb=target_imdb)
            if res_links:
                for l in res_links: l.bridge_engine = 'balandro'
            return res_it, res_links
    except Exception as e:
        xbmc.log("Multi Bridge: error en _search_on_player: %s" % e, xbmc.LOGWARNING)
        return None, None

# ---------------------------------------------------------
# Master Parallel Search Execution
# ---------------------------------------------------------
def _run_parallel_search_impl(engine='alfa'):
    if not _is_engine_installed(engine):
        xbmc.log("Multi Bridge: El motor %s no está instalado. Cancelando búsqueda." % engine, xbmc.LOGWARNING)
        xbmcgui.Dialog().notification('Multi Bridge', 'El addon %s no está instalado' % engine.capitalize(), '', 3000)
        return [], None

    p_title = title or get_param('title') or get_param('title_es') or get_param('title_lat') or get_param('title_orig') or get_param('title_en') or ''
    p_showname = showname or get_param('showname') or ''
    p_year = year or get_param('year') or ''
    p_showyear = showyear or get_param('showyear') or ''
    p_season = season or get_param('season') or ''
    p_episode = episode or get_param('episode') or ''
    p_tmdb = tmdb_id or get_param('tmdb') or ''
    p_imdb = imdb_id or get_param('imdb') or ''

    is_series = bool(p_season and p_episode)
    if is_series and engine == 'alfa':
        xbmc.log("Multi Bridge: Alfa solo soporta películas. Cambiando motor a Balandro para series.", xbmc.LOGINFO)
        engine = 'balandro'
        if not _is_engine_installed('balandro'):
            xbmcgui.Dialog().ok('Multi Bridge', 'Para reproducir series se requiere tener instalado el addon Balandro.')
            return [], None

    if is_series:
        target_title = (p_showname or p_title or '').strip()
        target_year = p_showyear or p_year
        all_names = [t for t in [target_title, p_showname] if t]
        alt_terms = [t for t in all_names if t != target_title]
    else:
        target_title = (p_title or '').strip()
        target_year = p_year
        all_names = [t for t in [target_title, title_es or get_param('title_es'), title_lat or get_param('title_lat'), title_en or get_param('title_en'), title_orig or get_param('title_orig')] if t]
        alt_terms = [t for t in [title_es or get_param('title_es'), title_lat or get_param('title_lat'), title_orig or get_param('title_orig'), title_en or get_param('title_en')] if t and t != target_title]
    target_tmdb = p_tmdb
    target_imdb = p_imdb

    ch_data = load_channels_data()
    engine_channels = ch_data.get('channels', {}).get(engine, {})

    enabled_players = []
    for ch_id in sorted(engine_channels.keys(), key=lambda k: engine_channels[k].get('name', k).lower()):
        ch_info = engine_channels[ch_id]
        if is_series:
            if not ch_info.get('series'): continue
            if ch_info.get('series_disabled'): continue
        else:
            if not ch_info.get('movies'): continue
            if ch_info.get('movie_disabled'): continue
        enabled_players.append(ch_info)

    if not enabled_players: return [], None

    # PlanB (vitaminar) es el mas lento y valioso: arrancarlo primero en Alfa.
    if engine == 'alfa':
        try:
            _pb = [ch for ch in enabled_players if 'planb' in str(ch.get('channel', '')).lower() or 'planb' in str(ch.get('id', '')).lower()]
            _rest = [ch for ch in enabled_players if 'planb' not in str(ch.get('channel', '')).lower() and 'planb' not in str(ch.get('id', '')).lower()]
            if _pb:
                enabled_players = _pb + _rest
                xbmc.log('Multi Bridge: PlanB priorizado al inicio de la cola', xbmc.LOGINFO)
        except: pass

    # Respetar el ajuste del usuario (antes se recortaba a 18s aunque el
    # ajuste permite hasta 120s): en PCs lentos los canales necesitan mas.
    # Alfa necesita mas margen (vitaminar de PlanB tarda ~45s): suelo de 60s
    # solo en Alfa (Balandro queda con el ajuste del usuario tal cual).
    timeout_secs = min(120, max(10, _get_int_setting('search_timeout', 40)))
    if engine == 'alfa':
        timeout_secs = min(120, max(60, timeout_secs))
    if is_series:
        timeout_secs = min(122, timeout_secs + 2)
        xbmc.log('Multi Bridge: modo serie, timeout ajustado a %ds' % timeout_secs, xbmc.LOGINFO)
    max_search_workers = min(35, max(1, _get_int_setting('search_threads_max', 6)))

    total_channels = len(enabled_players)
    engine_name = 'Alfa' if engine == 'alfa' else 'Balandro'
    p_dialog = xbmcgui.DialogProgress()
    p_dialog.create('Multi Bridge (%s)' % engine_name, 'Preparando busqueda en %d canales...' % total_channels)
    # Complementar titulos solo si faltan variantes en películas (en series el nombre ya está resuelto)
    try:
        _distinct = len(set([_safe_str(x).lower().strip() for x in all_names if x]))
        if target_tmdb and not is_series and _distinct < 3:
            p_dialog.update(5, 'Obteniendo titulos TMDB...')
            fetched = _fetch_tmdb_titles(target_tmdb, is_series)
            xbmc.log(f"Multi Bridge: fetched TMDB titles for {target_tmdb}: {fetched}", xbmc.LOGINFO)
            for ft in fetched:
                if ft and ft not in all_names:
                    all_names.append(ft)
                if ft and ft != target_title and ft not in alt_terms:
                    alt_terms.append(ft)
            xbmc.log(f"Multi Bridge: run_parallel_search target_title='{target_title}' all_names={all_names} alt_terms={alt_terms}", xbmc.LOGINFO)
        else:
            xbmc.log(f"Multi Bridge: run_parallel_search (sin fetch) target_title='{target_title}' all_names={all_names} alt_terms={alt_terms}", xbmc.LOGINFO)
    except Exception as e:
        xbmc.log(f"Multi Bridge: fetch titles error: {e}", xbmc.LOGINFO)
    # Variantes con alias de traduccion (wolverine/lobezno...) para cazar
    # matches entre idiomas que el puntuador no puede unir solo.
    try:
        for _av in _alias_variants(all_names):
            if _av not in all_names:
                all_names.append(_av)
            if _av != target_title and _av not in alt_terms:
                alt_terms.append(_av)
    except Exception:
        pass
    p_dialog.update(0, 'Buscando en tus %d canales de %s...' % (total_channels, engine_name))

    # Pre-importar canales secuencialmente para evitar contencion de lock al inicio (acelera arranque)
    try:
        p_dialog.update(2, 'Preparando canales...')
        for ch_info in enabled_players:
            try:
                _ch = str(ch_info.get('channel') or ch_info.get('id') or '').strip().lower()
                if not _ch or _ch == 'search':
                    continue
                with _engine_lock:
                    _switch_engine_environment(engine)
                    __import__('channels.' + _ch, fromlist=[''])
            except:
                pass
            if xbmc.Monitor().abortRequested() or p_dialog.iscanceled():
                break
        xbmc.log(f"Multi Bridge: pre-import completado para {len(enabled_players)} canales", xbmc.LOGINFO)
    except Exception as e:
        xbmc.log(f"Multi Bridge: pre-import error: {e}", xbmc.LOGINFO)

    results_dict = {}
    threads = []
    def _worker(ch_info):
        try:
            ch_key = ch_info.get('id') or ch_info.get('channel')
            it, links = _search_on_player(ch_info, engine, is_series, target_title, target_year, p_season, p_episode, all_names, alt_terms=alt_terms, target_tmdb=target_tmdb, target_imdb=target_imdb)
            if it and links: results_dict[ch_key] = (it, links)
        except Exception: pass

    for ch_info in enabled_players:
        threads.append(threading.Thread(target=_worker, args=(ch_info,), daemon=True))

    started_indices = set()
    completed_indices = set()
    finished_channels = []

    def clean_name(ch_info):
        if isinstance(ch_info, dict):
            return ch_info.get('name') or ch_info.get('channel') or ch_info.get('id') or ''
        n = str(ch_info).replace('.json', '')
        n = re.sub(r'^(Alfa|Balandro)-', '', n, flags=re.IGNORECASE)
        return re.sub(r'-(Series|Movies?)$', '', n, flags=re.IGNORECASE).strip()

    start_time = time.time()
    thread_start_times = {}
    if engine == 'alfa':
        channel_timeout = 25 if is_series else 20
    else:
        channel_timeout = 10 if is_series else 8
    i = 0
    while not xbmc.Monitor().abortRequested():
        now = time.time()
        active_cnt = len([idx for idx in started_indices if idx not in completed_indices and threads[idx].is_alive()])
        while i < len(threads) and active_cnt < max_search_workers:
            threads[i].start()
            started_indices.add(i)
            thread_start_times[i] = now
            active_cnt += 1
            i += 1

        for idx, ch_info in enumerate(enabled_players):
            if idx in started_indices and idx not in completed_indices:
                st = thread_start_times.get(idx, start_time)
                is_alive = threads[idx].is_alive()
                ch_key = ch_info.get('id') or ch_info.get('channel') if isinstance(ch_info, dict) else ch_info
                if engine == 'alfa':
                    if not is_alive:
                        completed_indices.add(idx)
                        found = ch_key in results_dict
                        finished_channels.append((clean_name(ch_info), found))
                elif not is_alive or (now - st > channel_timeout):
                    completed_indices.add(idx)
                    found = ch_key in results_dict
                    finished_channels.append((clean_name(ch_info), found))

        completed = len(completed_indices)
        pct = int((completed / float(total_channels)) * 100) if total_channels else 100
        lines = []
        recent = finished_channels[-4:]
        for ch_name, found in recent:
            if found: lines.append('[COLOR lime][OK] %s[/COLOR]' % ch_name)
            else: lines.append('[COLOR grey][--] %s[/COLOR]' % ch_name)
        while len(lines) < 4: lines.append('')

        active_names = [clean_name(enabled_players[idx]) for idx in started_indices if idx not in completed_indices and threads[idx].is_alive()][:3]
        active_str = ', '.join(active_names) if active_names else 'Finalizando...'
        p_dialog.update(pct, '%s (%d/%d canales)\n%s\n%s\nBuscando: %s' % (target_title, completed, total_channels, lines[0], lines[1], active_str))

        elapsed = now - start_time
        if completed >= total_channels or elapsed > timeout_secs:
            break

        if p_dialog.iscanceled():
            if elapsed > 4 or len(results_dict) > 0:
                break
        time.sleep(0.1)

    try:
        p_dialog.close()
    except: pass
    try:
        xbmc.sleep(200)
    except:
        time.sleep(0.2)

    if not results_dict:
        _grace_start = time.time()
        while time.time() - _grace_start < 2.0:
            if results_dict or xbmc.Monitor().abortRequested():
                break
            time.sleep(0.1)

    all_links = []
    matched_item = None
    for ch_info in enabled_players:
        ch_key = ch_info.get('id') or ch_info.get('channel') if isinstance(ch_info, dict) else ch_info
        if ch_key in results_dict:
            it, links = results_dict[ch_key]
            if not matched_item and it: matched_item = it
            for lnk in links: all_links.append(lnk)

    # Foto para el recolector tardio: que canales entraron y donde quedaron
    # los resultados de los hilos que sigan vivos tras el cierre.
    try:
        global _LAST_SEARCH_TOKEN, _LAST_MERGED_PFS, _LAST_RESULTS_DICT, _LAST_SEARCH_THREADS
        _LAST_MERGED_PFS = set(
            (ch.get('id') or ch.get('channel') if isinstance(ch, dict) else ch)
            for ch in enabled_players
            if (ch.get('id') or ch.get('channel') if isinstance(ch, dict) else ch) in results_dict
        )
        _LAST_RESULTS_DICT = results_dict
        _LAST_SEARCH_TOKEN = (str(engine), str(target_tmdb or ''), str(p_season or ''), str(p_episode or ''), time.time())
        _LAST_SEARCH_THREADS = [
            ((ch.get('id') or ch.get('channel') if isinstance(ch, dict) else ch), threads[idx])
            for idx, ch in enumerate(enabled_players)
            if idx in started_indices and threads[idx].is_alive()
        ]
    except: pass

    all_links = _filter_and_sort_links(all_links)
    return all_links, matched_item

def _append_late_links(engine, target_tmdb, season, episode, new_links, channel_name=''):
    """Anexa enlaces tardios a la cache (RAM + fichero) sin reordenar lo ya
    mostrado, y refresca el contenedor solo si nuestra lista sigue abierta.
    Devuelve el nº anexado (0 si la busqueda ya fue superada por otra)."""
    added = 0
    try:
        if not new_links:
            return 0
        tmdb_s = _norm_id_str(target_tmdb)
        sea_s = _norm_id_str(season)
        epi_s = _norm_id_str(episode)

        ram = _RAM_SEARCH_CACHE
        if ram:
            r_tmdb = _norm_id_str(ram.get('tmdb'))
            if tmdb_s and r_tmdb and r_tmdb != tmdb_s:
                return 0
            if sea_s and _norm_id_str(ram.get('season')) and _norm_id_str(ram.get('season')) != sea_s:
                return 0
            if epi_s and _norm_id_str(ram.get('episode')) and _norm_id_str(ram.get('episode')) != epi_s:
                return 0

        _sc = _read_search_cache_atomic()
        if not _sc or not isinstance(_sc, dict):
            return 0

        _meta = _sc.get('meta', {}) or {}
        sc_tmdb = _norm_id_str(_meta.get('tmdb'))
        if tmdb_s and sc_tmdb and sc_tmdb != tmdb_s:
            return 0
        if sea_s and _norm_id_str(_meta.get('season')) and _norm_id_str(_meta.get('season')) != sea_s:
            return 0
        if epi_s and _norm_id_str(_meta.get('episode')) and _norm_id_str(_meta.get('episode')) != epi_s:
            return 0

        _cur = _sc.get('links', []) or []
        _have_urls = set()
        for _d in _cur:
            try:
                if isinstance(_d, dict):
                    u = str(_d.get('url') or '')
                    if u: _have_urls.add(u)
            except: pass

        _ser = []
        fresh_raw = []
        for _l in new_links:
            try:
                u = str(getattr(_l, 'url', '') or '')
                if u and u not in _have_urls:
                    _have_urls.add(u)
                    _d = _serialize_item(_l)
                    if _d:
                        _ser.append(_d)
                        fresh_raw.append(_l)
            except: pass

        if not _ser:
            return 0

        _sc['links'] = _cur + _ser
        if not _save_search_cache_atomic(_sc):
            return 0

        added = len(_ser)

        try:
            if ram and 'links' in ram:
                ram['links'].extend(fresh_raw)
                ram['time'] = time.time()
        except: pass

        if added:
            try:
                xbmc.log("Multi Bridge: recolector tardio +%d enlaces de %s (total %d)" % (added, channel_name or '?', len(_sc['links'])), xbmc.LOGINFO)
            except: pass

            # Mostrar siempre la notificación toast al usuario en cualquier dispositivo y pantalla
            try:
                _show_bridge_notification('Multi Bridge', '+%d enlaces de %s' % (added, channel_name or 'canal'), 3500)
            except: pass

            # Refrescar el contenedor si la lista de enlaces está abierta
            try:
                _folder = xbmc.getInfoLabel('Container.FolderPath') or ''
                if 'plugin.video.bridge.multi' in _folder and 'list_links' in _folder:
                    xbmc.executebuiltin('Container.Refresh')
            except: pass
    except Exception as _e:
        try: xbmc.log("Multi Bridge: _append_late_links error: %s" % _e, xbmc.LOGWARNING)
        except: pass
    return added

def _late_collect_worker(engine, target_tmdb, season, episode, token, deadline=120):
    """Espera hilos rezagados tras el cierre y anexa sus enlaces a la lista
    abierta. Se aborta si otra busqueda toma el relevo o al expirar deadline."""
    try:
        mon = xbmc.Monitor()
        t0 = time.time()
        done_pfs = set()
        time.sleep(2.5)
        while time.time() - t0 < deadline and not mon.abortRequested():
            try:
                if _LAST_SEARCH_TOKEN != token:
                    return
                avail_batch = []
                for pf, res in list(_LAST_RESULTS_DICT.items()):
                    if pf in _LAST_MERGED_PFS or pf in done_pfs:
                        continue
                    try:
                        it, links = res
                    except: continue
                    if links:
                        try:
                            _ch = re.sub(r'^(Alfa|Balandro)-', '', str(pf).replace('.json', ''), flags=re.IGNORECASE)
                        except: _ch = str(pf)
                        avail_batch.append((pf, links, _ch))
                    else:
                        done_pfs.add(pf)

                if avail_batch:
                    all_batch_links = []
                    channel_names = []
                    for pf, links, ch_name in avail_batch:
                        all_batch_links.extend(links)
                        if ch_name not in channel_names:
                            channel_names.append(ch_name)
                        done_pfs.add(pf)

                    if all_batch_links:
                        display_ch = ', '.join(channel_names[:3])
                        if len(channel_names) > 3:
                            display_ch += ' +%d más' % (len(channel_names) - 3)
                        _append_late_links(engine, target_tmdb, season, episode, all_batch_links, display_ch)

                # Terminar si no quedan hilos vivos pendientes
                try:
                    alive = False
                    for _pf, _th in list(_LAST_SEARCH_THREADS):
                        try:
                            if _pf not in _LAST_MERGED_PFS and _pf not in done_pfs and _th.is_alive():
                                alive = True
                                break
                        except: pass
                    if not alive:
                        return
                except: return
            except: pass
            try: time.sleep(1.2)
            except: return
    except: pass

_LAST_SEARCH_THREADS = []

def _spawn_late_collector(engine, target_tmdb, season, episode):
    """Arranca el recolector tardio si quedaron hilos vivos tras el cierre."""
    try:
        token = _LAST_SEARCH_TOKEN
        if not token:
            return
        alive = False
        try:
            for _pf, _th in list(_LAST_SEARCH_THREADS):
                try:
                    if _pf not in _LAST_MERGED_PFS and _th.is_alive():
                        alive = True
                        break
                except: pass
        except: pass
        if not alive:
            return
        th = threading.Thread(target=_late_collect_worker, args=(engine, target_tmdb, season, episode, token), daemon=True)
        th.start()
        try:
            xbmc.log("Multi Bridge: recolector tardio activado (hilos rezagados en curso)", xbmc.LOGINFO)
        except: pass
    except: pass

def run_parallel_search(engine='alfa'):
    global _search_in_progress
    _search_in_progress = True
    _apply_silence_all()
    try:
        return _run_parallel_search_impl(engine=engine)
    finally:
        _search_in_progress = False

# ---------------------------------------------------------
# Serialization and UI List Directory Display
# ---------------------------------------------------------
def _serialize_item(it):
    if not it: return None
    d = {}
    dict_to_use = it.__dict__ if hasattr(it, '__dict__') else {}
    for k in dict_to_use:
        try:
            val = dict_to_use[k]
            if isinstance(val, (str, int, float, bool, list, dict, tuple)) or val is None: d[k] = val
        except: pass
    return d

def _deserialize_item(d, engine='alfa'):
    if not d: return None
    mods = _get_alfa_modules() if engine == 'alfa' else _get_balandro_modules()
    if not mods: return None
    Item = mods['Item']
    InfoLabels = mods['InfoLabels']
    it = Item()
    it.__dict__.update(d)
    for k, v in d.items():
        try: setattr(it, k, v)
        except: pass
    if 'infoLabels' in it.__dict__ and not isinstance(it.__dict__['infoLabels'], InfoLabels):
        it.__dict__['infoLabels'] = InfoLabels(it.__dict__['infoLabels'])
    return it

def _deserialize_item_fast(Item, InfoLabels, d):
    """Igual que _deserialize_item pero con las clases ya resueltas: evita
    recargar los modulos del motor una vez por enlace al pintar la lista."""
    if not d or not Item: return None
    it = Item()
    it.__dict__.update(d)
    if 'infoLabels' in it.__dict__ and InfoLabels and not isinstance(it.__dict__['infoLabels'], InfoLabels):
        try: it.__dict__['infoLabels'] = InfoLabels(it.__dict__['infoLabels'])
        except Exception: pass
    return it

def _enrich_link_metadata(link, meta, matched_item=None):
    if not meta: meta = {}
    t_id = meta.get('tmdb') or _safe_get_item_attr(matched_item, 'tmdb_id') or ''
    i_id = meta.get('imdb') or _safe_get_item_attr(matched_item, 'imdb_id') or ''
    tv_id = meta.get('tvdb') or _safe_get_item_attr(matched_item, 'tvdb_id') or ''
    tr_id = meta.get('trakt') or _safe_get_item_attr(matched_item, 'trakt_id') or ''
    is_series = bool(meta.get('season') and meta.get('episode'))
    title_val = meta.get('title') or _safe_get_item_attr(matched_item, 'contentTitle') or _safe_get_item_attr(matched_item, 'title') or ''
    showname_val = meta.get('showname') or _safe_get_item_attr(matched_item, 'contentSerieName') or _safe_get_item_attr(matched_item, 'show') or ''
    year_val = meta.get('showyear' if is_series else 'year') or _safe_get_item_attr(matched_item, 'year') or ''
    plot_val = meta.get('plot') or _safe_get_item_attr(matched_item, 'plot') or ''
    tagline_val = meta.get('tagline') or _safe_get_item_attr(matched_item, 'tagline') or ''
    poster_val = meta.get('poster') or meta.get('thumbnail') or _safe_get_item_attr(matched_item, 'thumbnail') or ''
    fanart_val = meta.get('fanart') or _safe_get_item_attr(matched_item, 'fanart') or ''

    targets = []
    if link: targets.append(link)
    if matched_item and hasattr(matched_item, '__dict__'): targets.append(matched_item)
    if not targets: return

    for target in targets:
        if not hasattr(target, 'infoLabels') or not isinstance(getattr(target, 'infoLabels', None), dict):
            target.infoLabels = {}
        if is_series:
            target.contentType = 'episode'
            target.contentSerieName = showname_val
            target.show = showname_val
            try: target.contentSeason = int(meta.get('season'))
            except: pass
            try: target.contentEpisodeNumber = int(meta.get('episode'))
            except: pass
            target.contentTitle = title_val or showname_val
            target.infoLabels['mediatype'] = 'episode'
            target.infoLabels['tvshowtitle'] = showname_val
            try: target.infoLabels['season'] = int(meta.get('season'))
            except: pass
            try: target.infoLabels['episode'] = int(meta.get('episode'))
            except: pass
            target.infoLabels['title'] = '%s %sx%02d' % (showname_val, str(meta.get('season')), int(meta.get('episode')))
        else:
            target.contentType = 'movie'
            target.contentTitle = title_val
            target.infoLabels['mediatype'] = 'movie'
            target.infoLabels['title'] = title_val
        if t_id: target.infoLabels['tmdb_id'] = str(t_id); target.infoLabels['tmdb'] = str(t_id); target.infoLabels['code'] = str(t_id); target.tmdb_id = str(t_id)
        if i_id: target.infoLabels['imdb_id'] = str(i_id); target.infoLabels['imdbnumber'] = str(i_id); target.imdb_id = str(i_id)
        if tv_id: target.infoLabels['tvdb_id'] = str(tv_id); target.infoLabels['tvdb'] = str(tv_id); target.tvdb_id = str(tv_id)
        if tr_id: target.infoLabels['trakt_id'] = str(tr_id); target.infoLabels['trakt'] = str(tr_id); target.trakt_id = str(tr_id)
        if year_val:
            try: target.infoLabels['year'] = int(year_val); target.year = int(year_val)
            except: pass
        if plot_val: target.infoLabels['plot'] = plot_val; target.plot = plot_val
        if tagline_val: target.infoLabels['tagline'] = tagline_val; target.tagline = tagline_val
        if poster_val:
            target.thumbnail = poster_val
            target.infoLabels['poster'] = poster_val
            target.infoLabels['thumbnail'] = poster_val
        if fanart_val:
            target.fanart = fanart_val
            target.infoLabels['fanart'] = fanart_val

def sync_tmdbhelper_playerstring(meta=None):
    """Sincroniza la propiedad de ventana TMDbHelper.PlayerInfoString para que
    el servicio de TMDb Helper y el scrobbler de Trakt reconozcan la reproducción,
    incluso cuando el reproductor en TMDb Helper tiene 'is_resolvable': 'false'."""
    if not meta:
        meta = {}
    t_id = str(meta.get('tmdb') or '')
    i_id = str(meta.get('imdb') or '')
    tv_id = str(meta.get('tvdb') or '')
    s_val = meta.get('season')
    e_val = meta.get('episode')
    is_series = bool(s_val and e_val)

    if not t_id and not i_id:
        return

    try:
        try: _int_tmdb = int(t_id)
        except: _int_tmdb = t_id

        p_dict = {
            'tmdb_type': 'episode' if is_series else 'movie',
            'tmdb_id': _int_tmdb if _int_tmdb else t_id,
        }
        if i_id:
            p_dict['imdb_id'] = i_id
        if tv_id:
            try: p_dict['tvdb_id'] = int(tv_id)
            except: p_dict['tvdb_id'] = tv_id
        if is_series:
            try: p_dict['season'] = int(s_val)
            except: p_dict['season'] = str(s_val)
            try: p_dict['episode'] = int(e_val)
            except: p_dict['episode'] = str(e_val)

        p_str = json.dumps(p_dict)
        xbmcgui.Window(10000).setProperty('TMDbHelper.PlayerInfoString', p_str)
        xbmc.log(f"Multi Bridge: TMDbHelper.PlayerInfoString sincronizado -> {p_str}", xbmc.LOGINFO)
    except Exception as ex:
        xbmc.log(f"Multi Bridge: error sincronizando TMDbHelper.PlayerInfoString: {ex}", xbmc.LOGINFO)

def set_listitem_info(listitem, info=None, meta=None, skip_art=False):
    if info is None: info = {}
    if meta is None: meta = {}

    tmdb_id = str(info.get('tmdb_id') or info.get('tmdb') or meta.get('tmdb') or '')
    imdb_id = str(info.get('imdb_id') or info.get('imdb') or meta.get('imdb') or '')
    tvdb_id = str(info.get('tvdb_id') or info.get('tvdb') or meta.get('tvdb') or '')
    trakt_id = str(info.get('trakt_id') or info.get('trakt') or meta.get('trakt') or '')

    s_val = info.get('season') or meta.get('season')
    e_val = info.get('episode') or meta.get('episode')
    is_series = bool(s_val and e_val)

    title_val = str(info.get('title') or meta.get('title') or '')
    showname_val = str(info.get('tvshowtitle') or meta.get('showname') or '')
    year_val = info.get('year') or meta.get('showyear' if is_series else 'year') or meta.get('year')
    plot_val = str(info.get('plot') or meta.get('plot') or '')
    tagline_val = str(info.get('tagline') or meta.get('tagline') or '')
    poster_val = str(info.get('poster') or info.get('thumbnail') or meta.get('poster') or meta.get('thumbnail') or '')
    fanart_val = str(info.get('fanart') or meta.get('fanart') or '')

    unique_dict = {}
    if tmdb_id:
        unique_dict['tmdb'] = str(tmdb_id)
        if is_series:
            unique_dict['tvshow.tmdb'] = str(tmdb_id)
    if imdb_id:
        unique_dict['imdb'] = str(imdb_id)
        if is_series:
            unique_dict['tvshow.imdb'] = str(imdb_id)
    if tvdb_id:
        unique_dict['tvdb'] = str(tvdb_id)
        if is_series:
            unique_dict['tvshow.tvdb'] = str(tvdb_id)
    if trakt_id:
        unique_dict['trakt'] = str(trakt_id)

    if tmdb_id: listitem.setProperty('tmdb_id', str(tmdb_id))
    if imdb_id: listitem.setProperty('imdb_id', str(imdb_id))
    if tvdb_id: listitem.setProperty('tvdb_id', str(tvdb_id))
    listitem.setProperty('tmdb_type', 'episode' if is_series else 'movie')

    trakt_payload = {}
    if tmdb_id: trakt_payload['tmdb'] = int(tmdb_id) if str(tmdb_id).isdigit() else tmdb_id
    if imdb_id: trakt_payload['imdb'] = imdb_id
    if tvdb_id: trakt_payload['tvdb'] = int(tvdb_id) if str(tvdb_id).isdigit() else tvdb_id
    if trakt_payload:
        listitem.setProperty('script.trakt.ids', json.dumps(trakt_payload))

    art_dict = {}
    if not skip_art:
        if poster_val:
            art_dict['poster'] = poster_val
            art_dict['thumb'] = poster_val
            art_dict['icon'] = poster_val
        if fanart_val:
            art_dict['fanart'] = fanart_val
    if art_dict:
        try: listitem.setArt(art_dict)
        except: pass

    media_type = info.get('mediatype') or ('episode' if is_series else 'movie')
    try:
        vt = listitem.getVideoInfoTag()
        if vt:
            if unique_dict:
                try: vt.setUniqueIDs(unique_dict, 'tmdb' if tmdb_id else ('imdb' if imdb_id else ''))
                except: pass
            if imdb_id:
                try: vt.setIMDbNumber(imdb_id)
                except: pass
            vt.setMediaType(media_type)
            if is_series and media_type == 'episode':
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
            if plot_val: vt.setPlot(plot_val)
            if tagline_val:
                try: vt.setTagLine(tagline_val)
                except: pass
            if year_val:
                try: vt.setYear(int(year_val))
                except: pass
            return
    except: pass

    if unique_dict:
        try: listitem.setUniqueIDs(unique_dict, 'tmdb' if tmdb_id else 'imdb')
        except: pass
    legacy_info = dict(info)
    for bad_key in ('trakt', 'trakt_id', 'poster', 'tmdb', 'tvshow.tmdb', 'tvshow.imdb', 'tvshow.tvdb'):
        legacy_info.pop(bad_key, None)
    if 'mediatype' not in legacy_info:
        legacy_info['mediatype'] = media_type
    if title_val and 'title' not in legacy_info:
        legacy_info['title'] = title_val
    if is_series and media_type == 'episode' and showname_val and 'tvshowtitle' not in legacy_info:
        legacy_info['tvshowtitle'] = showname_val
    if s_val and 'season' not in legacy_info:
        try: legacy_info['season'] = int(s_val)
        except: pass
    if e_val and 'episode' not in legacy_info:
        try: legacy_info['episode'] = int(e_val)
        except: pass
    if year_val and 'year' not in legacy_info:
        try: legacy_info['year'] = int(year_val)
        except: pass
    if plot_val and 'plot' not in legacy_info:
        legacy_info['plot'] = plot_val
    if tagline_val and 'tagline' not in legacy_info:
        legacy_info['tagline'] = tagline_val
    try: listitem.setInfo('video', legacy_info)
    except: pass

VIEW_MODE_FILE = os.path.join(BRIDGE_DATA_PATH, 'saved_view_mode.json')

def _get_saved_view_mode():
    """Obtiene el ID de vista guardado por el usuario o detectado desde la base de datos de Kodi."""
    # 1. Archivo persistente del addon
    try:
        if os.path.exists(VIEW_MODE_FILE):
            with open(VIEW_MODE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                vid = int(data.get('view_id', 0))
                if 45 <= vid <= 650:
                    return vid
    except Exception:
        pass

    # 2. Base de datos ViewModes de Kodi (recuperar la última vista que usó el usuario)
    try:
        import sqlite3
        db_dir = xbmcvfs.translatePath('special://userdata/Database/')
        if os.path.exists(db_dir):
            db_files = sorted([f for f in os.listdir(db_dir) if f.startswith('ViewModes') and f.endswith('.db')], reverse=True)
            if db_files:
                db_path = os.path.join(db_dir, db_files[0])
                conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=1.0)
                cur = conn.cursor()
                cur.execute("SELECT viewMode FROM view WHERE path LIKE 'plugin://plugin.video.bridge.multi/?view=list_links%' ORDER BY idView DESC LIMIT 10")
                rows = cur.fetchall()
                conn.close()
                for r in rows:
                    if r and r[0]:
                        vid = r[0] & 0xFFFF
                        if 45 <= vid <= 650 and vid != 50:
                            _save_view_mode(vid)
                            return vid
                if rows and rows[0] and rows[0][0]:
                    vid = rows[0][0] & 0xFFFF
                    if 45 <= vid <= 650:
                        return vid
    except Exception as e:
        xbmc.log(f"Multi Bridge: _get_saved_view_mode db error: {e}", xbmc.LOGDEBUG)

    # 3. Default según el skin activo si nada se ha guardado
    skin_id = ''
    try: skin_id = str(xbmc.getSkinDir() or '').lower()
    except: pass
    return 50

def _save_view_mode(view_id):
    """Guarda la vista elegida por el usuario para todas las futuras búsquedas."""
    try:
        if not os.path.exists(BRIDGE_DATA_PATH):
            os.makedirs(BRIDGE_DATA_PATH)
        with open(VIEW_MODE_FILE, 'w', encoding='utf-8') as f:
            json.dump({'view_id': int(view_id)}, f)
        xbmc.log(f"Multi Bridge: Vista persistente guardada -> {view_id}", xbmc.LOGINFO)
    except Exception as e:
        xbmc.log(f"Multi Bridge: _save_view_mode error: {e}", xbmc.LOGWARNING)

def _in_own_list():
    """True solo si la ventana activa es nuestra lista de enlaces.
    Evita aplicar nuestra vista a otros contenedores (ej. TMDb Helper)."""
    try:
        if not xbmc.getCondVisibility("Window.IsVisible(10025)"):
            return False
        folder = xbmc.getInfoLabel("Container.FolderPath") or ''
        return "plugin.video.bridge.multi" in folder and "view=list_links" in folder
    except Exception:
        return False

def _apply_saved_view_mode():
    """Aplica la vista guardada por el usuario en la lista de resultados."""
    view_id = _get_saved_view_mode()
    if not view_id:
        return

    def _apply_delayed():
        _mon2 = xbmc.Monitor()
        for delay in (50, 150, 300, 600, 1000):
            xbmc.sleep(delay)
            if _mon2.abortRequested():
                return
            try:
                # Si el usuario ya salio de nuestra lista (ej. volvio a
                # TMDb Helper), no tocar la vista del otro contenedor.
                if not _in_own_list():
                    break
                xbmc.executebuiltin("Container.SetViewMode(%d)" % view_id)
            except Exception:
                pass

    try:
        # Solo aplicar de inmediato si ya estamos en nuestra lista (ej.
        # refresco); si no, el hilo retardado lo hara al aparecer.
        if _in_own_list():
            try:
                xbmc.executebuiltin("Container.SetViewMode(%d)" % view_id)
            except Exception:
                pass
        t = threading.Thread(target=_apply_delayed)
        t.daemon = True
        t.start()
    except Exception:
        pass

def _start_view_mode_monitor():
    """Hilo en segundo plano que detecta en tiempo real si el usuario cambia de vista para recordarla siempre."""
    def _monitor():
        mon = xbmc.Monitor()
        # Esperar a que la ventana de enlaces cargue en pantalla
        for _ in range(15):
            if mon.abortRequested():
                return
            xbmc.sleep(200)
            if xbmc.getCondVisibility("Window.IsVisible(10025)"):
                break

        current_saved = _get_saved_view_mode()

        # Monitorear activamente mientras el usuario navegue en la ventana de enlaces (hasta 3 minutos)
        for _ in range(360):
            if mon.abortRequested():
                return
            xbmc.sleep(500)
            try:
                if not xbmc.getCondVisibility("Window.IsVisible(10025)"):
                    break
                folder = xbmc.getInfoLabel("Container.FolderPath")
                if "plugin.video.bridge.multi" not in folder or "view=list_links" not in folder:
                    break
                focus_id = xbmcgui.Window(10025).getFocusId()
                if 45 <= focus_id <= 650 and focus_id != current_saved:
                    current_saved = focus_id
                    _save_view_mode(focus_id)
            except Exception:
                pass

        # Respaldo al salir: revisar si Kodi guardó un cambio en su base de datos ViewModes
        try:
            import sqlite3
            db_dir = xbmcvfs.translatePath('special://userdata/Database/')
            if os.path.exists(db_dir):
                db_files = sorted([f for f in os.listdir(db_dir) if f.startswith('ViewModes') and f.endswith('.db')], reverse=True)
                if db_files:
                    db_path = os.path.join(db_dir, db_files[0])
                    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=1.0)
                    cur = conn.cursor()
                    cur.execute("SELECT viewMode FROM view WHERE path LIKE 'plugin://plugin.video.bridge.multi/?view=list_links%' ORDER BY idView DESC LIMIT 1")
                    row = cur.fetchone()
                    conn.close()
                    if row and row[0]:
                        vid = row[0] & 0xFFFF
                        if 45 <= vid <= 650 and vid != current_saved:
                            _save_view_mode(vid)
        except Exception:
            pass

    try:
        t = threading.Thread(target=_monitor)
        t.daemon = True
        t.start()
    except Exception:
        pass

_clearlogo_cache = {}

def _get_clearlogo(tmdb_id, is_series=False):
    """Obtiene de forma instantánea el clearlogo de la base de datos local de TMDb Helper o caché en RAM."""
    if not tmdb_id:
        return ''
    _id_str = str(tmdb_id).strip()
    if not _id_str.isdigit():
        return ''

    _cache_key = f"{'tv' if is_series else 'movie'}.{_id_str}"
    if _cache_key in _clearlogo_cache:
        return _clearlogo_cache[_cache_key]

    logo_url = ''
    # 1. Búsqueda instantánea en SQLite local de TMDb Helper (ItemDetails.db)
    try:
        tmdb_base = xbmcvfs.translatePath('special://userdata/addon_data/plugin.video.themoviedb.helper/')
        db_files = []
        if os.path.exists(tmdb_base):
            for root, _, files in os.walk(tmdb_base):
                for fn in files:
                    if fn.lower() == 'itemdetails.db':
                        db_files.append(os.path.join(root, fn))

        pref_id = f"tv.{_id_str}" if is_series else f"movie.{_id_str}"
        alt_id  = f"movie.{_id_str}" if is_series else f"tv.{_id_str}"

        for db_path in db_files:
            try:
                conn = sqlite3.connect(db_path, timeout=1.0)
                cur = conn.cursor()
                cur.execute(
                    "SELECT icon, iso_language FROM art WHERE type = 'logos' AND (parent_id = ? OR parent_id = ?) "
                    "ORDER BY (CASE WHEN parent_id = ? THEN 1 ELSE 0 END) DESC, "
                    "(CASE WHEN iso_language = 'es' THEN 2 WHEN iso_language = 'en' THEN 1 ELSE 0 END) DESC, "
                    "rating DESC, votes DESC LIMIT 1;",
                    (pref_id, alt_id, pref_id)
                )
                row = cur.fetchone()
                conn.close()
                if row and row[0]:
                    icon = row[0].strip()
                    if icon.startswith('/'):
                        logo_url = f"https://image.tmdb.org/t/p/original{icon}"
                    elif icon.startswith('http'):
                        logo_url = icon
                    break
            except Exception:
                pass
    except Exception as e:
        xbmc.log(f"Multi Bridge: _get_clearlogo db error: {e}", xbmc.LOGDEBUG)

    # 2. Fallback opcional a API TMDb si la base local aún no lo tiene
    if not logo_url:
        try:
            import urllib.request, ssl
            media_endpoint = 'tv' if is_series else 'movie'
            api_url = f"https://api.themoviedb.org/3/{media_endpoint}/{_id_str}/images?api_key=a07324c669cac4d96789197134ce272b"
            req = urllib.request.Request(api_url, headers={'User-Agent': 'Mozilla/5.0'})
            ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(req, timeout=1.5, context=ctx) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                logos = data.get('logos', [])
                if logos:
                    best_fp = None
                    for lg in logos:
                        if lg.get('iso_639_1') == 'es':
                            best_fp = lg.get('file_path'); break
                    if not best_fp:
                        for lg in logos:
                            if lg.get('iso_639_1') == 'en':
                                best_fp = lg.get('file_path'); break
                    if not best_fp and logos:
                        best_fp = logos[0].get('file_path')
                    if best_fp:
                        logo_url = f"https://image.tmdb.org/t/p/original{best_fp}"
        except Exception:
            pass

    _clearlogo_cache[_cache_key] = logo_url
    return logo_url

def show_links_as_directory():
    global _search_in_progress, _links_view_active
    _search_in_progress = False
    _links_view_active = True
    _apply_silence_all()
    xbmc.log("Multi Bridge: show_links_as_directory llamado", xbmc.LOGINFO)
    links = []
    matched_item = None
    meta = {}
    engine = 'alfa'

    _req_tmdb = get_param('tmdb') or tmdb_id or ''
    _req_s = get_param('season') or season
    _req_e = get_param('episode') or episode

    # 1. Acceso instantáneo a caché en RAM (0ms disk lag)
    ram_hit = _get_ram_search_cache(_req_tmdb, _req_s, _req_e)
    if ram_hit and ram_hit.get('links'):
        links = list(ram_hit.get('links', []))
        matched_item = ram_hit.get('matched_item')
        meta = dict(ram_hit.get('meta', {}))
        engine = ram_hit.get('engine', 'alfa') or 'alfa'
        xbmc.log("Multi Bridge: show_links_as_directory usando RAM cache directa (0ms disk, %d enlaces)" % len(links), xbmc.LOGINFO)
    else:
        # Fallback a disco con lectura atómica y reintentos automáticos
        cache = _read_search_cache_atomic(max_attempts=6, delay=0.05)
        if cache and isinstance(cache, dict):
            try:
                engine = cache.get('engine', 'alfa') or 'alfa'
                _raw_links = cache.get('links', []) or []
                _d_mods = _get_alfa_modules() if engine == 'alfa' else _get_balandro_modules()
                if _d_mods:
                    _d_Item, _d_Info = _d_mods['Item'], _d_mods['InfoLabels']
                    links = [_deserialize_item_fast(_d_Item, _d_Info, lnk) for lnk in _raw_links]
                    matched_item = _deserialize_item_fast(_d_Item, _d_Info, cache.get('item'))
                else:
                    links = [_deserialize_item(lnk, engine) for lnk in _raw_links]
                    matched_item = _deserialize_item(cache.get('item'), engine)
                links = [l for l in links if l is not None]
                meta = cache.get('meta', {}) or {}
                if links:
                    _store_ram_search_cache(links, matched_item, meta, engine)
                    xbmc.log("Multi Bridge: show_links_as_directory cargo %d enlaces desde cache de disco" % len(links), xbmc.LOGINFO)
            except Exception as e:
                xbmc.log("Multi Bridge: error deserializando cache de disco: %s" % e, xbmc.LOGWARNING)

    # Rescate de emergencia desde RAM cache si no coincidieron estrictamente los parametros
    if not links and _RAM_SEARCH_CACHE and _RAM_SEARCH_CACHE.get('links'):
        links = list(_RAM_SEARCH_CACHE.get('links', []))
        matched_item = _RAM_SEARCH_CACHE.get('matched_item')
        meta = dict(_RAM_SEARCH_CACHE.get('meta', {}))
        engine = _RAM_SEARCH_CACHE.get('engine', 'alfa') or 'alfa'
        xbmc.log("Multi Bridge: show_links_as_directory rescate desde RAM cache global (%d enlaces)" % len(links), xbmc.LOGINFO)

    if not links:
        xbmcgui.Dialog().notification('Multi Bridge', 'No hay enlaces en caché', '', 3000)
        xbmcplugin.endOfDirectory(handle, succeeded=False)
        return

    _max_list = _get_int_setting('max_links_list', 40)
    if _max_list < 5: _max_list = 5
    _total_links = len(links)
    _has_more = _total_links > _max_list
    _remaining_links = _total_links - _max_list if _has_more else 0
    if _has_more:
        xbmc.log(f"Multi Bridge: mostrando primeros {_max_list} de {_total_links} enlaces nativos ({_remaining_links} restantes en ventana flotante)", xbmc.LOGINFO)
        links = links[:_max_list]

    # 2. Plantilla compartida de metadatos y arte (calculada 1 sola vez fuera del bucle)
    s_int = int(meta.get('season')) if meta.get('season') and str(meta.get('season')).isdigit() else None
    e_int = int(meta.get('episode')) if meta.get('episode') and str(meta.get('episode')).isdigit() else None
    is_s = bool(s_int is not None and e_int is not None)
    media_type = 'episode' if is_s else 'movie'

    poster_val = meta.get('poster') or meta.get('thumbnail') or getattr(matched_item, 'thumbnail', '') or alfa_icon
    fanart_val = meta.get('fanart') or getattr(matched_item, 'fanart', '') or ''
    thumb_val = meta.get('thumbnail') or meta.get('poster') or getattr(matched_item, 'thumbnail', '') or alfa_icon
    if len(_safe_str(thumb_val)) > 1000: thumb_val = alfa_icon
    if len(_safe_str(poster_val)) > 1000: poster_val = alfa_icon
    if len(_safe_str(fanart_val)) > 1000: fanart_val = ''

    tmdb_val = meta.get('tmdb') or getattr(matched_item, 'tmdb', '') or _req_tmdb or ''
    imdb_val = meta.get('imdb') or getattr(matched_item, 'imdb', '') or ''
    tvdb_val = meta.get('tvdb') or getattr(matched_item, 'tvdb', '') or ''
    showname_val = _safe_str(meta.get('showname') or getattr(matched_item, 'showname', '') or '')
    year_val = meta.get('year') or meta.get('showyear') or getattr(matched_item, 'year', None)
    try: year_int = int(year_val) if year_val else None
    except: year_int = None

    duration_val = meta.get('duration') or meta.get('runtime') or getattr(matched_item, 'duration', 0)
    try: duration_int = int(duration_val) if duration_val else None
    except: duration_int = None

    clearlogo_val = meta.get('clearlogo') or get_param('clearlogo') or getattr(matched_item, 'clearlogo', '') or ''
    if clearlogo_val in ('_', 'None', 'none') or len(_safe_str(clearlogo_val)) > 1000:
        clearlogo_val = ''
    if not clearlogo_val and tmdb_val:
        clearlogo_val = _get_clearlogo(tmdb_val, is_s)

    base_art = {'thumb': thumb_val, 'icon': thumb_val, 'poster': poster_val, 'fanart': fanart_val}
    if clearlogo_val:
        base_art['clearlogo'] = clearlogo_val
        base_art['logo'] = clearlogo_val
        if is_s:
            base_art['tvshow.clearlogo'] = clearlogo_val
            base_art['tvshow.logo'] = clearlogo_val

    plot_text = _safe_str(meta.get('plot') or getattr(matched_item, 'plot', '') or '')[:2000]
    tagline_text = _safe_str(meta.get('tagline') or '')

    main_title = _safe_str(meta.get('title') or meta.get('title_es') or meta.get('title_lat') or getattr(matched_item, 'title', '') or getattr(matched_item, 'contentTitle', '') or '')
    header_title = showname_val if (is_s and showname_val) else main_title

    base_unique = {}
    if tmdb_val:
        base_unique['tmdb'] = str(tmdb_val)
        if is_s: base_unique['tvshow.tmdb'] = str(tmdb_val)
    if imdb_val:
        base_unique['imdb'] = str(imdb_val)
        if is_s: base_unique['tvshow.imdb'] = str(imdb_val)
    if tvdb_val:
        base_unique['tvdb'] = str(tvdb_val)
        if is_s: base_unique['tvshow.tvdb'] = str(tvdb_val)

    # Marcador de reanudación
    media_key_main = _get_media_key(meta, matched_item)
    saved_bm = get_bookmark(media_key_main, tmdb_id=meta.get('tmdb'), is_series=is_s, season=meta.get('season'), episode=meta.get('episode'))
    bm_str = ''
    if saved_bm:
        r_sec = float(saved_bm.get('resume_time', 0))
        m_mins = int(r_sec // 60); m_secs = int(r_sec % 60); m_hrs = m_mins // 60; m_mins = m_mins % 60
        bm_str = (' [COLOR orange][Reanudar: %d:%02d:%02d][/COLOR]' % (m_hrs, m_mins, m_secs)) if m_hrs else (' [COLOR orange][Reanudar: %d:%02d][/COLOR]' % (m_mins, m_secs))

    # Menú contextual pre-armado
    _other_engine = 'balandro' if engine == 'alfa' else 'alfa'
    _other_name   = 'Balandro' if engine == 'alfa' else 'Alfa'
    _rs_url  = 'plugin://plugin.video.bridge.multi/?action=search_other_engine&engine=%s' % _other_engine
    _v_url   = 'plugin://plugin.video.bridge.multi/?action=verify_links'
    verified_only = meta.get('verified_only', False)

    base_ctx_items = []
    if not (is_s and _other_engine == 'alfa'):
        base_ctx_items.append(('[COLOR orange]Buscar con %s[/COLOR]' % _other_name, 'RunPlugin(%s)' % _rs_url))
    if not verified_only:
        base_ctx_items.append(('[COLOR deepskyblue]Verificar disponibilidad[/COLOR]', 'RunPlugin(%s)' % _v_url))

    # 3. Construcción ultra-rápida de ListItems
    _dir_listing = []
    for idx, lnk in enumerate(links):
        try:
            _url = _safe_str(getattr(lnk, 'url', ''))
            if len(_url) > 1500:
                continue
            srv = _get_link_server_name(lnk)
            lang = _format_language(lnk)
            qual = _format_quality(lnk)
            ch = _format_channel(lnk)
            _weak_str = ' [COLOR orange][Dudoso][/COLOR]' if getattr(lnk, 'bridge_weak', False) else ''

            is_tor = (srv.strip().lower() == 'torrent' or _is_torrent_link(lnk))
            if is_tor:
                srv_tag = '[B][COLOR cyan]TORRENT[/COLOR][/B]'
                if srv.strip().lower() not in ('torrent', 'magnet', 'directo', 'unknown', ''):
                    srv_tag += '  [B][COLOR deepskyblue]%s[/COLOR][/B]' % srv
            else:
                srv_tag = '[B][COLOR deepskyblue]%s[/COLOR][/B]' % (srv or 'Directo')

            mid_parts = []
            if lang: mid_parts.append('[COLOR lime]%s[/COLOR]' % lang)
            if qual and qual != 'N/A': mid_parts.append('[COLOR gold]%s[/COLOR]' % qual)
            mid = ' | '.join(mid_parts)
            if mid:
                lbl = '%s | %s | [COLOR grey](%s)[/COLOR]%s%s' % (srv_tag, mid, ch, _weak_str, bm_str)
            else:
                lbl = '%s | [COLOR grey](%s)[/COLOR]%s%s' % (srv_tag, ch, _weak_str, bm_str)

            play_url = 'plugin://plugin.video.bridge.multi/?action=play_single_link&index=%d&engine=%s' % (idx, engine)
            li = xbmcgui.ListItem(label=lbl)
            li.setPath(play_url)
            li.setArt(base_art)
            li.setLabel(lbl)
            li.setLabel2(ch)
            li.setProperty('title', lbl)
            li.setProperty('IsPlayable', 'false')
            if clearlogo_val:
                li.setProperty('clearlogo', clearlogo_val)
                li.setProperty('Art(clearlogo)', clearlogo_val)
                li.setProperty('logo', clearlogo_val)
                li.setProperty('Art(logo)', clearlogo_val)
                if is_s:
                    li.setProperty('tvshow.clearlogo', clearlogo_val)
                    li.setProperty('Art(tvshow.clearlogo)', clearlogo_val)
            if header_title:
                li.setProperty('TvShowTitle', header_title)
                li.setProperty('tvshowtitle', header_title)
            if not is_s:
                li.setProperty('DBTYPE', 'movie')
            else:
                li.setProperty('DBTYPE', 'episode')
            if base_ctx_items:
                li.addContextMenuItems(base_ctx_items)

            try:
                vt = li.getVideoInfoTag()
                if vt:
                    vt.setTitle(lbl)
                    vt.setMediaType(media_type)
                    if plot_text: vt.setPlot(plot_text)
                    if year_int: vt.setYear(year_int)
                    if duration_int: vt.setDuration(duration_int)
                    if tagline_text: vt.setTagLine(tagline_text)
                    if is_s:
                        if showname_val: vt.setTvShowTitle(showname_val)
                        if s_int is not None: vt.setSeason(s_int)
                        if e_int is not None: vt.setEpisode(e_int)
                    else:
                        if header_title: vt.setTvShowTitle(header_title)
                    if base_unique:
                        vt.setUniqueIDs(base_unique, 'tmdb' if tmdb_val else 'imdb')
            except Exception:
                pass

            _dir_listing.append((play_url, li, False))
        except Exception:
            continue

    if clearlogo_val:
        try:
            xbmcgui.Window(10000).setProperty('TMDbHelper.ListItem.ClearLogo', clearlogo_val)
            xbmcgui.Window(10000).setProperty('TMDbHelper.ListItem.CropImage', clearlogo_val)
        except Exception:
            pass

    # Si hay mas enlaces que el limite max_links_list, anadir elemento para abrir en la ventana personalizada
    if _has_more:
        _floating_url = 'plugin://plugin.video.bridge.multi/?action=floating_links&offset=%d' % _max_list
        _li_more = xbmcgui.ListItem(label='[COLOR deepskyblue][B]Siguientes enlaces (%d restantes) >> [COLOR gold](Ver en ventana)[/COLOR][/B][/COLOR]' % _remaining_links)
        _li_more.setProperty('IsPlayable', 'false')
        try: _li_more.setArt(base_art)
        except: pass
        _dir_listing.append((_floating_url, _li_more, False))

    if _dir_listing:
        try:
            xbmcplugin.addDirectoryItems(handle, _dir_listing)
        except Exception:
            for _u, _li, _f in _dir_listing:
                try: xbmcplugin.addDirectoryItem(handle, _u, _li, _f)
                except: pass

    xbmcplugin.addSortMethod(handle, xbmcplugin.SORT_METHOD_NONE)
    if header_title:
        try: xbmcplugin.setPluginCategory(handle, header_title)
        except Exception: pass
    xbmcplugin.setContent(handle, 'episodes')
    _apply_saved_view_mode()
    xbmcplugin.endOfDirectory(handle, succeeded=True, updateListing=False, cacheToDisc=False)
    _apply_saved_view_mode()
    _start_view_mode_monitor()

# ---------------------------------------------------------
# Playback Handler (Native Delegates for Alfa & Balandro)
# ---------------------------------------------------------

def _resolve_link_to_listitem(link, engine, matched_item=None):
    """Resolve *link* to a playable ListItem by intercepting the engine's setResolvedUrl.

    Does NOT consume the real Kodi plugin handle — the interception captures the
    ListItem (with the direct stream URL) and returns it to the caller.

    Returns (True, listitem) on success, (False, None) on failure.
    """
    if not link: return False, None

    prep = _prepare_playable_link(link, engine=engine)
    if not prep:
        return False, None

    if not getattr(prep, 'url', '') and not getattr(prep, 'video_urls', None):
        return False, None

    _resolved_li = [None]
    _result       = [False]

    import xbmcplugin as _xp
    _orig_sru = getattr(_xp, 'setResolvedUrl', None)

    def _capture(h, succeeded, listitem):
        if succeeded and listitem:
            _resolved_li[0] = listitem
            _result[0]      = True
        else:
            if not _result[0]:
                _result[0] = False

    def _cap_ok(*args, **kwargs):
        return True

    def _cap_notif(*args, **kwargs):
        return True

    def _cap_sel(heading="", options=None, *args, **kwargs):
        if options and isinstance(options, (list, tuple)):
            return len(options) - 1
        return 0

    try:    _xp.setResolvedUrl = _capture
    except: pass

    try:
        if engine == 'balandro':
            mods = _get_balandro_modules()
            if not mods: return False, None
            pt = mods['platformtools']
            parent = matched_item or prep
            _orig_ok = getattr(pt, 'dialog_ok', None)
            _orig_notif = getattr(pt, 'dialog_notification', None)
            _orig_sel = getattr(pt, 'dialog_select', None)
            try:
                pt.dialog_ok = _cap_ok
                pt.dialog_notification = _cap_notif
                pt.dialog_select = _cap_sel
                sys.argv[1] = '1'
                res = pt.play_video(prep, parent, autoplay=False)
                if res is True and _resolved_li[0] is not None:
                    _result[0] = True
            finally:
                if _orig_ok is not None:
                    try: pt.dialog_ok = _orig_ok
                    except: pass
                if _orig_notif is not None:
                    try: pt.dialog_notification = _orig_notif
                    except: pass
                if _orig_sel is not None:
                    try: pt.dialog_select = _orig_sel
                    except: pass
        else:  # alfa
            mods = _get_alfa_modules()
            if not mods: return False, None
            pt = mods['platformtools']
            _orig_ok = getattr(pt, 'dialog_ok', None)
            _orig_notif = getattr(pt, 'dialog_notification', None)
            _orig_sel = getattr(pt, 'dialog_select', None)
            try:
                pt.dialog_ok = _cap_ok
                pt.dialog_notification = _cap_notif
                pt.dialog_select = _cap_sel
                sys.argv[1] = '1'
                pt.play_video(prep, autoplay=True)
            finally:
                if _orig_ok is not None:
                    try: pt.dialog_ok = _orig_ok
                    except: pass
                if _orig_notif is not None:
                    try: pt.dialog_notification = _orig_notif
                    except: pass
                if _orig_sel is not None:
                    try: pt.dialog_select = _orig_sel
                    except: pass
    except Exception as _ex:
        xbmc.log('Multi Bridge _resolve_link error: ' + str(_ex), xbmc.LOGINFO)
        _result[0] = False
    finally:
        if _orig_sru is not None:
            try:    _xp.setResolvedUrl = _orig_sru
            except: pass

    if _result[0] and _resolved_li[0] is not None:
        return True, _resolved_li[0]
    return False, None


def _autoplay_with_fallback(links, handle, engine, matched_item=None,
                            monitor_secs=180, max_attempts=999, tmdb_for_list=None,
                            meta=None, seek_to_time=0, resume_media_key='',
                            resume_title=''):
    """Try non-torrent links in order until one verifiably plays.

    While autoplay runs, list_links is opened in the background (if tmdb_for_list
    is provided) so the user can choose a different link (e.g. a torrent) after
    stopping the current playback.

    Torrents are filtered out of the autoplay queue — they only appear in list_links.
    The Kodi plugin handle is dismissed with setResolvedUrl(False) at the start.
    Actual playback uses xbmc.Player().play() independently of the handle.
    """
    if not meta:
        _cf = _read_search_cache_atomic()
        if _cf and isinstance(_cf, dict):
            meta = _cf.get('meta', {}) or {}

    if not links:
        _absorb = xbmcgui.ListItem()
        xbmcplugin.setResolvedUrl(handle, False, _absorb)
        return False

    # ── Filter torrents ────────────────────────────────────────────────────
    normal_links = [l for l in links if not _is_torrent_link(l)]
    if not normal_links:
        _absorb = xbmcgui.ListItem()
        xbmcplugin.setResolvedUrl(handle, False, _absorb)
        return False

    # ── Dismiss handle early (suppress Kodi error dialog via guardian) ─────
    _guard_stop = [False]
    def _suppress():
        for _ in range(30):
            if _guard_stop[0]: break
            try:
                xbmc.executebuiltin('Dialog.Close(okdialog,true)')
                xbmc.executebuiltin('Dialog.Close(error,true)')
            except: pass
            time.sleep(0.1)
    _gt = threading.Thread(target=_suppress, daemon=True)
    _gt.start()

    _absorb = xbmcgui.ListItem()
    try:    xbmcplugin.setResolvedUrl(handle, False, _absorb)
    except: pass
    xbmc.sleep(300)
    _guard_stop[0] = True
    try: _gt.join(timeout=0.5)
    except: pass

    # ── Abrir list_links en segundo plano ──────────────────────────────────
    # Se carga detrás del reproductor; cuando el usuario detiene el video
    # list_links se abrirá en el momento correcto: cuando el usuario diga "No"
    # o cuando se agoten todos los enlaces (ver más abajo).

    player    = xbmc.Player()
    total_att = min(len(normal_links), max_attempts)
    _cancelled_by_user = False
    _monitor_started = False
    _want_resume = (seek_to_time or 0) > 10
    # Tiempo por servidor leido del ajuste (antes fijo 12s ignorando ajustes)
    _resolve_timeout = _get_int_setting('autoplay_resolve_timeout', 12)
    if _resolve_timeout < 5: _resolve_timeout = 5
    if _resolve_timeout > 120: _resolve_timeout = 120
    _resolve_steps = int(_resolve_timeout * 10)



    # Mapa de idioma interno → nombre legible para el usuario
    _LANG_NAMES = {
        'lat': 'Latino', 'latino': 'Latino',
        'esp': 'Castellano', 'castellano': 'Castellano', 'es': 'Castellano',
        'vose': 'VOSE', 'sub': 'VOSE', 'subtitulado': 'VOSE',
        'eng': 'Inglés', 'en': 'Inglés',
        'por': 'Portugués', 'pt': 'Portugués',
    }

    for idx, link in enumerate(normal_links[:total_att]):
        server   = _safe_str(getattr(link, 'server',   '') or '').capitalize() or 'servidor'
        lang_raw = _safe_str(getattr(link, 'language', '') or '').strip()
        lang_lbl = _LANG_NAMES.get(lang_raw.lower(), lang_raw) if lang_raw else ''
        qual     = _safe_str(getattr(link, 'quality',  '') or '').strip()
        ch       = _safe_str(getattr(link, 'channel',  '') or '')

        # Etiqueta: "Voe [Latino] · HD"
        _lang_part = (' [%s]' % lang_lbl) if lang_lbl else ''
        _qual_part = (' · %s' % qual) if qual else ''
        _label = '%s%s%s' % (server, _lang_part, _qual_part)

        xbmc.log('Multi Bridge autoplay [%d/%d] → %s %s (%s)' % (
            idx+1, total_att, server, lang_lbl, ch), xbmc.LOGINFO)


        # ── Step 1: Resolve ────────────────────────────────────────────────
        _prog = xbmcgui.DialogProgress()
        _prog.create('Multi Bridge',
                     '[%d/%d] Conectando a [B]%s[/B]...\n[COLOR grey]Espere por favor (0.0s / %ds)[/COLOR]' % (idx+1, total_att, _label, _resolve_timeout))
        _prog.update(10)

        _res_done  = [None]
        _res_li    = [None]
        def _do_resolve(lnk=link):
            ok, li = _resolve_link_to_listitem(lnk, engine, matched_item)
            _res_done[0] = ok
            _res_li[0]   = li

        _rt = threading.Thread(target=_do_resolve, daemon=True)
        _rt.start()

        cancelled = False
        for _s in range(_resolve_steps):
            if _prog.iscanceled():
                cancelled = True
                break
            if _res_done[0] is not None:
                break
            xbmc.sleep(100)
            _prog.update(min(10 + int(_s * 70.0 / _resolve_steps), 80),
                         '[%d/%d] Conectando a [B]%s[/B]...\n[COLOR grey]Espere por favor (%.1fs / %ds)[/COLOR]' % (idx+1, total_att, _label, (_s + 1) * 0.1, _resolve_timeout))

        _rt.join(timeout=1)

        if cancelled:
            try: _prog.close()
            except: pass
            xbmc.log('Multi Bridge autoplay: usuario canceló resolución', xbmc.LOGINFO)
            _cancelled_by_user = True
            break

        if not _res_done[0] or _res_li[0] is None:
            try: _prog.close()
            except: pass
            xbmc.log('Multi Bridge autoplay [%d/%d] %s no resolvió, siguiente...' % (
                idx+1, total_att, server), xbmc.LOGINFO)
            continue

        # Extract media URL
        _media_url = ''
        try:    _media_url = _res_li[0].getPath()
        except: pass
        if not _media_url:
            try: _prog.close()
            except: pass
            xbmc.log('Multi Bridge autoplay [%d/%d] %s URL vacía, siguiente...' % (
                idx+1, total_att, server), xbmc.LOGINFO)
            continue

        # ── Step 2: Start playback ─────────────────────────────────────────
        _prog.update(90, '[%d/%d] Iniciando [B]%s[/B]...' % (idx+1, total_att, _label))
        try:
            sync_tmdbhelper_playerstring(meta)
            if _res_li[0] is not None:
                set_listitem_info(_res_li[0], meta=meta)
            player.play(_media_url, _res_li[0])
        except Exception as _pe:
            try: _prog.close()
            except: pass
            xbmc.log('Multi Bridge autoplay Player.play error: ' + str(_pe), xbmc.LOGINFO)
            continue

        try: _prog.close()
        except: pass

        # ── Step 3: Monitor 3 min reales de reloj ────────────────────────────
        # Esperar hasta 15 s a que el reproductor arranque.
        # Si el usuario para el reproductor durante este tiempo, se detecta
        # como parada manual y se muestra el diálogo de confirmación.
        started = False
        user_stopped_early = False
        was_playing_briefly = False  # el reproductor llegó a iniciar brevemente
        for _ in range(150):
            if xbmc.Monitor().abortRequested():
                user_stopped_early = True
                break
            if player.isPlaying():
                started = True
                was_playing_briefly = True
                break
            # Detectar si el reproductor empezó y el usuario lo paró
            # (isPlayingVideo puede estar en False aunque player.isPlaying() falló)
            xbmc.sleep(100)

        if user_stopped_early:
            _cancelled_by_user = True
            break

        if not started:
            xbmc.log('Multi Bridge autoplay [%d/%d] %s nunca arrancó / usuario paró en arranque (15s)' % (
                idx+1, total_att, server), xbmc.LOGINFO)
            try: player.stop()
            except: pass
            try: xbmc.PlayList(xbmc.PLAYLIST_VIDEO).clear()
            except: pass
            xbmc.sleep(400)

            _restore_dialog_noblock()

            remaining = total_att - (idx + 1)
            _srv_lang = server + ((' [%s]' % lang_lbl) if lang_lbl else '')
            if remaining > 0:
                try:
                    ans = _KODI_ORIG_DIALOG().yesno(
                        'Multi Bridge — Autoplay',
                        '[B]%s[/B] no pudo reproducirse o fue detenido.\n¿Intentar con el siguiente enlace?' % _srv_lang,
                        nolabel='No, abrir lista',
                        yeslabel='Sí, siguiente')
                except Exception as _ye:
                    xbmc.log('Multi Bridge autoplay not started yesno error: ' + str(_ye), xbmc.LOGINFO)
                    ans = False
                xbmc.log('Multi Bridge autoplay: respuesta de usuario en arranque = %s' % str(ans), xbmc.LOGINFO)
                if not ans:
                    _cancelled_by_user = True
                    break
            continue

        xbmc.log('Multi Bridge autoplay [%d/%d] %s arrancó, monitoreando 3 min reales...' % (
            idx+1, total_att, server), xbmc.LOGINFO)

        # Arrancar el monitor de bookmarks + reanudar AL INICIO de cada
        # reproduccion (no al final): asi el salto a reanudar ocurre a los
        # segundos y los bookmarks se guardan desde el principio.
        # Cada intento usa el punto guardado MAS RECIENTE (releido aqui):
        # el 1.º el que elegiste en el dialogo, los siguientes el que se
        # guardo mientras veias el anterior. (El monitor anterior termina
        # solo al detenerse su video.)
        try:
            _rk = resume_media_key or _get_media_key(meta, matched_item)
            _rt = resume_title or str((meta or {}).get('title') or (meta or {}).get('showname') or '')
            if _monitor_started and _want_resume:
                _sk = 0
                try:
                    _is_s2 = bool((meta or {}).get('season') and (meta or {}).get('episode'))
                    _bm2 = get_bookmark(_rk, tmdb_id=(meta or {}).get('tmdb'), is_series=_is_s2,
                                        season=(meta or {}).get('season'), episode=(meta or {}).get('episode'))
                    if _bm2 and float(_bm2.get('resume_time', 0)) > 10:
                        _sk = float(_bm2.get('resume_time', 0))
                except: pass
            elif not _monitor_started:
                _sk = seek_to_time
            else:
                _sk = 0
            start_playback_monitor(_rk, title_str=_rt, seek_to_time=_sk, current_link_index=idx, meta=meta)
        except Exception as _se:
            xbmc.log('Multi Bridge autoplay monitor inicio error: %s' % _se, xbmc.LOGINFO)
        _monitor_started = True

        mon_start       = time.time()
        user_declined   = False   # usuario dijo "No": parar y volver
        link_failed     = False   # usuario dijo "Si": continuar con siguiente
        _ver_enlaces    = False   # boton personalizado: abrir lista de enlaces

        while time.time() - mon_start < monitor_secs:
            if xbmc.Monitor().abortRequested():
                user_declined = True
                break

            xbmc.sleep(1000)   # verificar cada segundo (tiempo real)

            if not player.isPlaying():
                elapsed = time.time() - mon_start
                xbmc.log('Multi Bridge autoplay [%d/%d] %s se detuvo a %.1fs reales' % (
                    idx+1, total_att, server, elapsed), xbmc.LOGINFO)

                # Asegurar dialogo real SIN cerrojos (nunca puede bloquearse aqui)
                xbmc.log('Multi Bridge autoplay: restaurando dialogo real sin cerrojos...', xbmc.LOGINFO)
                _restore_dialog_noblock()

                # Esperar a que la ventana de vídeo de Kodi termine de cerrarse
                xbmc.sleep(600)

                remaining = total_att - (idx + 1)
                _srv_lang = server + ((' [%s]' % lang_lbl) if lang_lbl else '')
                if remaining > 0:
                    xbmc.log('Multi Bridge autoplay: mostrando ventana al usuario (%s)...' % _srv_lang, xbmc.LOGINFO)
                    # Ventana personalizada (Si/No/Ver enlaces). Devuelve
                    # 0/1/2 o -1; ante cualquier fallo, volver sin lista.
                    try:
                        ans = _show_autoplay_stop_dialog(_srv_lang)
                    except Exception as _ye:
                        xbmc.log('Multi Bridge autoplay stop dialog error: ' + str(_ye), xbmc.LOGINFO)
                        ans = 1

                    xbmc.log('Multi Bridge autoplay: respuesta de usuario sobre siguiente enlace = %s' % str(ans), xbmc.LOGINFO)
                    if ans == 0:
                        link_failed = True   # Si → continuar con siguiente
                    elif ans == 2:
                        _ver_enlaces = True  # Ver enlaces → abrir lista
                    else:
                        user_declined = True  # No o cerrar → parar y volver
                else:
                    _ver_enlaces = True  # ultimo enlace: ir a la lista
                break   # salir del while

        if user_declined:
            _cancelled_by_user = True
            try: player.stop()
            except: pass
            try: xbmc.PlayList(xbmc.PLAYLIST_VIDEO).clear()
            except: pass
            xbmc.log('Multi Bridge autoplay: usuario detuvo → volver', xbmc.LOGINFO)
            return False

        if _ver_enlaces:
            # Abrir list_links para que el usuario elija enlace manualmente
            if tmdb_for_list:
                _ts = int(time.time())
                _lu = ('plugin://plugin.video.bridge.multi/?view=list_links'
                       '&tmdb=%s&t=%s' % (str(tmdb_for_list), _ts))
                if meta and meta.get('season') and meta.get('episode'):
                    _lu += '&season=%s&episode=%s' % (meta['season'], meta['episode'])
                xbmc.log('Multi Bridge autoplay: ver enlaces → abriendo list_links', xbmc.LOGINFO)
                xbmc.executebuiltin('Dialog.Close(all,true)')
                xbmc.sleep(200)
                _open_links_view(_lu)
            return False

        if link_failed:
            try: player.stop()
            except: pass
            try: xbmc.PlayList(xbmc.PLAYLIST_VIDEO).clear()
            except: pass
            for _ in range(30):
                try:
                    if not player.isPlaying():
                        break
                except: break
                xbmc.sleep(100)
            xbmc.sleep(600)
            continue   # → siguiente enlace

        # ── 60 s reales transcurridos con reproducción activa → ÉXITO ────────
        xbmc.log('Multi Bridge autoplay [%d/%d] %s verificado OK tras 3 min reales' % (
            idx+1, total_att, server), xbmc.LOGINFO)
        return True


    # ── Todos los intentos agotados ────────────────────────────────────────
    try: player.stop()
    except: pass
    try: xbmc.PlayList(xbmc.PLAYLIST_VIDEO).clear()
    except: pass

    if not player.isPlaying():
        if tmdb_for_list:
            _ts = int(time.time())
            _lu = ('plugin://plugin.video.bridge.multi/?view=list_links'
                   '&tmdb=%s&t=%s' % (str(tmdb_for_list), _ts))
            if meta and meta.get('season') and meta.get('episode'):
                _lu += '&season=%s&episode=%s' % (meta['season'], meta['episode'])
            if _cancelled_by_user:
                xbmc.log('Multi Bridge autoplay: cancelado por usuario → abriendo list_links', xbmc.LOGINFO)
            else:
                xbmc.log('Multi Bridge autoplay: enlaces agotados → abriendo list_links', xbmc.LOGINFO)
            xbmc.executebuiltin('Dialog.Close(all,true)')
            xbmc.sleep(200)
            _open_links_view(_lu)
        else:
            xbmcgui.Dialog().notification(
                'Multi Bridge',
                'Ningún enlace pudo reproducirse (%d intentos)' % total_att,
                '', 5000)
    return False



def _autoplay_link(link, handle, engine, matched_item=None):
    """Resolve *link* and tell Kodi to play it via setResolvedUrl(handle, True, ...).

    This is the CORRECT approach for autoplay: we hold the Kodi plugin handle open,
    resolve the server URL in the background, then hand the resolved ListItem back to
    Kodi.  No setResolvedUrl(False) / xbmc.Player().play() hack needed.

    Returns True if playback was handed off to Kodi, False on failure.
    """
    if not link: return False

    # ── Torrent → cliente configurado en Balandro (con enriquecimiento TMDb) ─
    if _is_torrent_link(link):
        _absorb = xbmcgui.ListItem()
        torrent_url, _fail_detail = _resolve_torrent_url(link, engine=engine)
        _tl = (torrent_url or '').strip()
        if not (_tl.startswith('magnet:') or _tl.endswith('.torrent')):
            xbmc.log('Multi Bridge autoplay: torrent no reproducible (%s)' % (_fail_detail or 'sin URL'), xbmc.LOGINFO)
            xbmcplugin.setResolvedUrl(handle, False, _absorb)
            return False
        xbmcplugin.setResolvedUrl(handle, False, _absorb)
        return _play_torrent_link(_tl, matched_item=matched_item, meta=getattr(link, 'meta', None))


    server_name = (_safe_str(getattr(link, 'server', '') or '').strip().capitalize()
                   or 'servidor')

    # ── Balandro engine ────────────────────────────────────────────────────
    if engine == 'balandro':
        mods = _get_balandro_modules()
        if not mods:
            _absorb = xbmcgui.ListItem()
            xbmcplugin.setResolvedUrl(handle, False, _absorb)
            return False

        platformtools = mods['platformtools']
        parent = matched_item if matched_item else link

        _prog = xbmcgui.DialogProgress()
        _prog.create('Multi Bridge', 'Reproduciendo via [B]%s[/B]...' % server_name)
        _prog.update(5)

        _resolved_li = [None]
        _result      = [None]   # True=ok, False=fail, 'cancel'=user cancelled

        def _do_resolve():
            import xbmcplugin as _xp
            _orig = getattr(_xp, 'setResolvedUrl', None)

            def _capture(h, succeeded, listitem):
                if succeeded:
                    _resolved_li[0] = listitem
                    _result[0] = True
                else:
                    _result[0] = False

            try:    _xp.setResolvedUrl = _capture
            except: pass

            try:
                sys.argv[1] = str(handle)
                # autoplay=True → skip Balandro's internal player-selector dialog
                platformtools.play_video(link, parent, autoplay=True)
                if _result[0] is None:
                    _result[0] = False
            except Exception as _ex:
                xbmc.log('Multi Bridge _autoplay_link Balandro error: ' + str(_ex), xbmc.LOGINFO)
                _result[0] = False
            finally:
                if _orig is not None:
                    try:    _xp.setResolvedUrl = _orig
                    except: pass

        _t = threading.Thread(target=_do_resolve, daemon=True)
        _t.start()

        # Wait for resolution with animated progress (max 30 s)
        for _step in range(300):
            if _prog.iscanceled():
                _result[0] = 'cancel'
                break
            if _result[0] is not None:
                break
            xbmc.sleep(100)
            _prog.update(min(5 + _step * 3, 90),
                         'Reproduciendo via [B]%s[/B]...' % server_name)

        try:    _prog.close()
        except: pass
        _t.join(timeout=2)

        if _result[0] is True and _resolved_li[0] is not None:
            xbmc.log('Multi Bridge _autoplay_link: setResolvedUrl True via %s' % server_name,
                     xbmc.LOGINFO)
            xbmcplugin.setResolvedUrl(handle, True, _resolved_li[0])
            return True
        else:
            _absorb = xbmcgui.ListItem()
            xbmcplugin.setResolvedUrl(handle, False, _absorb)
            if _result[0] != 'cancel':
                xbmcgui.Dialog().notification(
                    'Multi Bridge',
                    '[B]%s[/B] no pudo reproducirse' % server_name,
                    '', 4000)
            return False

    # ── Alfa engine ────────────────────────────────────────────────────────
    else:
        mods = _get_alfa_modules()
        if not mods:
            _absorb = xbmcgui.ListItem()
            xbmcplugin.setResolvedUrl(handle, False, _absorb)
            return False
        platformtools = mods['platformtools']
        try:
            sys.argv[1] = str(handle)
            # Alfa's play_video calls setResolvedUrl(handle, True, ...) internally
            platformtools.play_video(link)
            return True
        except Exception as _e:
            _absorb = xbmcgui.ListItem()
            xbmcplugin.setResolvedUrl(handle, False, _absorb)
            xbmcgui.Dialog().notification(
                'Multi Bridge', 'Error al reproducir: ' + str(_e)[:60], '', 3500)
            return False


def _clean_dialog_text(text):
    """Limpia etiquetas Kodi y HTML para mostrar texto legible en diálogos."""
    if not text:
        return ""
    t = str(text).replace('<br/>', '\n').replace('<br>', '\n')
    t = re.sub(r'\[/?COLOR.*?\]', '', t)
    t = re.sub(r'\[/?B\]', '', t)
    t = re.sub(r'\[/?I\]', '', t)
    t = re.sub(r'<[^>]+>', '', t)
    return t.strip()


def _play_link_safely(link, engine='alfa', matched_item=None, meta=None):

    if not link: return False

    if not meta:
        _cf = _read_search_cache_atomic()
        if _cf and isinstance(_cf, dict):
            meta = _cf.get('meta', {}) or {}

    # 1. Enlace Torrent → cliente configurado en Balandro (con enriquecimiento TMDb)
    if _is_torrent_link(link):
        _ch_dbg = _safe_str(getattr(link, 'channel', '') or '')
        torrent_url, _fail_detail = _resolve_torrent_url(link, engine=engine)
        _tl = (torrent_url or '').strip()
        if not (_tl.startswith('magnet:') or _tl.endswith('.torrent')):
            msg = _fail_detail or 'Enlace torrent sin URL reproducible'
            xbmc.log("Multi Bridge: torrent %s no reproducible: %s" % (_ch_dbg, msg), xbmc.LOGWARNING)
            xbmcgui.Dialog().notification('Multi Bridge', msg, '', 4000)
            return False
        return _play_torrent_link(_tl, matched_item=matched_item, meta=meta)

    # 2. Preparar enlace (resolver canal.play y data_url)
    prepared = _prepare_playable_link(link, engine=engine)
    if not prepared:
        xbmcgui.Dialog().notification('Multi Bridge', 'No se pudo preparar el enlace', '', 3000)
        return False

    server_name = (_safe_str(getattr(prepared, 'server', '') or getattr(link, 'server', '') or '').strip().capitalize()
                   or 'Servidor')

    has_url = bool(getattr(prepared, 'url', None))
    has_video_urls = bool(getattr(prepared, 'video_urls', None))

    if not has_url and not has_video_urls:
        xbmc.log(f"Multi Bridge: enlace sin URL para {server_name}", xbmc.LOGINFO)
        xbmcgui.Dialog().notification('Multi Bridge', f'{server_name} — No se encontró URL reproducible', '', 3500)
        return False

    # 3. Reproductor según el motor
    # ── Balandro: ventana de progreso de conexión, timeout (15s) y aviso de fallo ──
    if engine == 'balandro':
        mods = _get_balandro_modules()
        if not mods: return False

        platformtools = mods['platformtools']
        parent = matched_item if matched_item else prepared

        # Ventana de progreso visual exclusiva para Balandro
        prog = xbmcgui.DialogProgress()
        prog.create('Multi Bridge', 'Conectando a [B]%s[/B]...' % server_name)
        prog.update(5, 'Conectando a [B]%s[/B]...' % server_name)

        import xbmcplugin as _xp
        _resolved_li = [None]
        _result = [None]   # True=éxito, False=fallo, 'cancel'=cancelado por usuario
        _fail_reason = ['']

        def _cap_sru(h, succeeded, listitem):
            if succeeded and listitem:
                _resolved_li[0] = listitem
                _result[0] = True
            else:
                if _result[0] is None:
                    _result[0] = False

        def _cap_ok(heading="", message="", *args):
            txt = str(message or heading or '')
            if txt and not _fail_reason[0]:
                _fail_reason[0] = txt
            return True

        def _cap_notif(heading="", message="", *args, **kwargs):
            txt = str(message or heading or '')
            if txt and not _fail_reason[0]:
                _fail_reason[0] = txt
            return True

        def _cap_sel(heading="", options=None, *args, **kwargs):
            if options and isinstance(options, (list, tuple)):
                return len(options) - 1
            return 0

        def _do_resolve():
            _orig_sru = getattr(_xp, 'setResolvedUrl', None)
            _orig_ok = getattr(platformtools, 'dialog_ok', None)
            _orig_notif = getattr(platformtools, 'dialog_notification', None)
            _orig_sel = getattr(platformtools, 'dialog_select', None)

            try:
                _xp.setResolvedUrl = _cap_sru
                platformtools.dialog_ok = _cap_ok
                platformtools.dialog_notification = _cap_notif
                platformtools.dialog_select = _cap_sel
                sys.argv[1] = str(handle)
                xbmc.log(f"Multi Bridge: conectando a {server_name} en Balandro (url={str(getattr(prepared, 'url', ''))[:60]})", xbmc.LOGINFO)
                res = platformtools.play_video(prepared, parent, autoplay=False)
                if res is True and _resolved_li[0] is not None:
                    _result[0] = True
                elif res is False:
                    _result[0] = False
                elif _result[0] is None:
                    _result[0] = False
            except Exception as _ex:
                xbmc.log(f"Multi Bridge: error en play_video Balandro: {_ex}", xbmc.LOGINFO)
                if not _fail_reason[0]:
                    _fail_reason[0] = str(_ex)
                _result[0] = False
            finally:
                if _orig_sru is not None:
                    try: _xp.setResolvedUrl = _orig_sru
                    except: pass
                if _orig_ok is not None:
                    try: platformtools.dialog_ok = _orig_ok
                    except: pass
                if _orig_notif is not None:
                    try: platformtools.dialog_notification = _orig_notif
                    except: pass
                if _orig_sel is not None:
                    try: platformtools.dialog_select = _orig_sel
                    except: pass

        _t = threading.Thread(target=_do_resolve, daemon=True)
        _t.start()

        _timeout_steps = 150  # 15s
        for _step in range(_timeout_steps):
            if prog.iscanceled():
                _result[0] = 'cancel'
                break
            if _result[0] is not None:
                break
            xbmc.sleep(100)
            _elapsed = (_step + 1) * 0.1
            _pct = min(15 + int((_step / float(_timeout_steps)) * 80), 95)
            prog.update(
                _pct,
                'Conectando a [B]%s[/B]...\n[COLOR grey]Espere por favor (%.1fs / 15s)[/COLOR]' % (server_name, _elapsed)
            )

        try:
            prog.close()
        except:
            pass
        _t.join(timeout=1.0)

        if _result[0] == 'cancel':
            xbmc.log(f"Multi Bridge: conexión a {server_name} cancelada", xbmc.LOGINFO)
            return False

        if _result[0] is True and _resolved_li[0] is not None:
            media_url = ''
            try: media_url = _resolved_li[0].getPath()
            except: pass
            if media_url:
                xbmc.log(f"Multi Bridge: reproduciendo {server_name} con éxito (url={media_url[:60]})", xbmc.LOGINFO)
                sync_tmdbhelper_playerstring(meta)
                if _resolved_li[0] is not None:
                    set_listitem_info(_resolved_li[0], meta=meta)
                xbmc.Player().play(media_url, _resolved_li[0])
                return True
            else:
                xbmc.log(f"Multi Bridge: {server_name} resolvió pero no proporcionó la ruta final del vídeo", xbmc.LOGWARNING)
                dlg = (_KODI_ORIG_DIALOG or xbmcgui.Dialog)()
                dlg.ok(
                    'Multi Bridge — Error de Reproducción',
                    f'El servidor [B][COLOR gold]{server_name}[/COLOR][/B] resolvió pero no proporcionó la ruta final del vídeo.'
                )
                return False

        if _result[0] is None:
            xbmc.log(f"Multi Bridge: timeout conectando a {server_name} tras 15s", xbmc.LOGINFO)
            dlg = (_KODI_ORIG_DIALOG or xbmcgui.Dialog)()
            dlg.ok(
                'Multi Bridge — Tiempo de Espera Agotado',
                f'El servidor [B][COLOR gold]{server_name}[/COLOR][/B] no respondió a tiempo (15 segundos).\n\n'
                f'[COLOR red][B]Causa:[/B][/COLOR] Tiempo de espera agotado.\n'
                f'El servidor puede estar caído, saturado o bloqueando la conexión.'
            )
            return False

        raw_reason = _fail_reason[0].strip()
        clean_reason = _clean_dialog_text(raw_reason)
        if not clean_reason:
            clean_reason = 'El vídeo ya no existe, el enlace está caído o el servidor no responde.'

        xbmc.log(f"Multi Bridge: fallo reproduciendo {server_name}: {clean_reason}", xbmc.LOGINFO)
        dlg = (_KODI_ORIG_DIALOG or xbmcgui.Dialog)()
        dlg.ok(
            'Multi Bridge — Fallo del Servidor',
            f'No se pudo reproducir en el servidor [B][COLOR gold]{server_name}[/COLOR][/B].\n\n'
            f'[COLOR red][B]Motivo:[/B][/COLOR] {clean_reason}'
        )
        return False

    # ── Alfa: reproducción 100% nativa sin ventanas personalizadas ────────
    # Alfa gestiona su propia ventana nativa de progreso "Conectando con [Servidor]..."
    # y sus propios diálogos de fallo de forma independiente.
    else:
        mods = _get_alfa_modules()
        if not mods: return False
        platformtools = mods['platformtools']
        try:
            sys.argv[1] = str(handle)
            _enrich_link_metadata(prepared, meta, matched_item)
            sync_tmdbhelper_playerstring(meta)

            # Limpiar video_urls para que Alfa resuelva de forma fresca con su ventana nativa de progreso
            prepared.video_urls = []

            xbmc.log(f"Multi Bridge: reproduciendo {server_name} en Alfa (url={str(getattr(prepared, 'url', ''))[:60]})", xbmc.LOGINFO)

            orig_select = getattr(platformtools, 'dialog_select', None)
            def _smart_alfa_select(heading="", options=None, *args, **kwargs):
                if options and isinstance(options, (list, tuple)):
                    # Contar opciones de vídeo ("Ver el vídeo ...")
                    video_opts = []
                    for i, opt in enumerate(options):
                        opt_str = str(opt).lower()
                        if 'ver el video' in opt_str or 'ver el vídeo' in opt_str or 'reproducir' in opt_str:
                            video_opts.append(i)

                    # Si hay varias resoluciones, mostrar la ventana nativa de Alfa
                    if len(video_opts) > 1:
                        if orig_select:
                            return orig_select(heading, options, *args, **kwargs)
                        return (_KODI_ORIG_DIALOG or xbmcgui.Dialog)().select(heading, options)

                    # Si solo hay una resolución, iniciar directamente
                    if len(video_opts) == 1:
                        return video_opts[0]
                    return 0

                if orig_select:
                    return orig_select(heading, options, *args, **kwargs)
                return 0

            try:
                platformtools.dialog_select = _smart_alfa_select
                platformtools.play_video(prepared, force_direct=True)
            finally:
                if orig_select:
                    platformtools.dialog_select = orig_select

            pl = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
            if pl.size() > 0 or xbmc.Player().isPlaying() or xbmc.getCondVisibility("Player.HasMedia"):
                return True

            for _ in range(20):
                if xbmc.Player().isPlaying() or xbmc.getCondVisibility("Player.HasMedia") or pl.size() > 0:
                    return True
                xbmc.sleep(100)

            return True
        except Exception as e:
            xbmc.log(f"Multi Bridge: error en play_video Alfa: {e}", xbmc.LOGINFO)
            _show_bridge_notification('Multi Bridge', f'{server_name} — Error: {str(e)[:60]}', 3500)
            return False

def verify_and_filter_links():
    links = []
    matched_item = None
    meta = {}
    engine = 'alfa'
    cache = _read_search_cache_atomic()
    if cache and isinstance(cache, dict):
        try:
            engine = cache.get('engine', 'alfa') or 'alfa'
            links = [_deserialize_item(lnk, engine) for lnk in cache.get('links', [])]
            links = [l for l in links if l is not None]
            matched_item = _deserialize_item(cache.get('item'), engine)
            meta = cache.get('meta', {}) or {}
        except: pass

    if not links: return
    p_dialog = xbmcgui.DialogProgress()
    p_dialog.create('Multi Bridge', 'Verificando disponibilidad de enlaces...')
    verified_links = _verify_links_headless(links, engine=engine, p_dialog=p_dialog)
    try: p_dialog.close()
    except: pass

    if not verified_links:
        xbmcgui.Dialog().ok('Multi Bridge', 'No se encontraron enlaces funcionales.')
        return

    verified_links = _filter_and_sort_links(verified_links)
    meta['verified_only'] = True
    try:
        _store_ram_search_cache(verified_links, matched_item, meta, engine)
        _save_search_cache_atomic({
            'item': _serialize_item(matched_item),
            'links': [_serialize_item(l) for l in verified_links],
            'meta': meta,
            'engine': engine
        })
    except: pass
    xbmc.executebuiltin('Container.Refresh')

# ---------------------------------------------------------
# Player Manager & Migration / Cloud Update Tools
# ---------------------------------------------------------
_migration_checked_flag = False

def check_and_run_migration():
    global _migration_checked_flag
    if _migration_checked_flag:
        return
    try:
        if xbmcgui.Window(10000).getProperty('MultiBridge_MigrationDone') == 'true':
            _migration_checked_flag = True
            return
    except Exception:
        pass
    if not os.path.exists(TMDB_PLAYERS_PATH):
        try: os.makedirs(TMDB_PLAYERS_PATH)
        except: pass

    # 1. Asegurar el player maestro (1) Multi Bridge en TMDb Helper
    master_file = os.path.join(TMDB_PLAYERS_PATH, '(1)MultiBusqueda.json')
    master_movie_url = 'executebuiltin://RunPlugin("plugin://plugin.video.bridge.multi/?action=play&title={es-MX_title}&year={year}&title_es={es-ES_title}&title_lat={es-MX_title}&tmdb={tmdb}&imdb={imdb}&tvdb={tvdb}&trakt={trakt}&plot={plot}&plot_lat={es-MX_plot}&plot_es={es-ES_plot}&tagline={tagline}&tagline_lat={es-MX_tagline}&tagline_es={es-ES_tagline}&director={director}&title_en={en_title}&title_orig={originaltitle}&poster={poster}&fanart={fanart}&thumbnail={thumbnail}&clearlogo={clearlogo}")'
    master_ep_url = 'executebuiltin://RunPlugin("plugin://plugin.video.bridge.multi/?action=play&title={es-MX_showname}&season={season}&episode={episode}&showname={showname}&showyear={showyear}&title_es={es-ES_showname}&title_lat={es-MX_showname}&tmdb={tmdb}&imdb={imdb}&tvdb={tvdb}&trakt={trakt}&plot={plot}&plot_lat={es-MX_plot}&plot_es={es-ES_plot}&tagline={tagline}&tagline_lat={es-MX_tagline}&tagline_es={es-ES_tagline}&director={director}&title_en={en_showname}&title_orig={original_name}&poster={poster}&fanart={fanart}&thumbnail={thumbnail}&clearlogo={clearlogo}")'

    master_data = {
        "name": "(1) Multi Bridge",
        "plugin": "plugin.video.bridge.multi",
        "priority": 100,
        "is_resolvable": "false",
        "assert": {
            "play_movie": ["title", "year"],
            "play_episode": ["showname", "season", "episode"]
        },
        "play_movie": master_movie_url,
        "play_episode": master_ep_url,
        "is_folder": "false"
    }

    need_write_master = True
    if os.path.exists(master_file):
        try:
            with open(master_file, 'r', encoding='utf-8') as f:
                cur_master = json.load(f)
            if cur_master == master_data:
                need_write_master = False
        except Exception:
            pass

    if need_write_master:
        try:
            with open(master_file, 'w', encoding='utf-8') as f:
                json.dump(master_data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            xbmc.log("Multi Bridge: error guardando master player: %s" % e, xbmc.LOGWARNING)

    # 2. Asegurar que channels.json existe internamente en Multi Bridge (userdata)
    if not os.path.exists(CHANNELS_JSON_FILE):
        channels_data = {'version': 1, 'channels': {'alfa': {}, 'balandro': {}}}
        # Si existen players antiguos en TMDB_PLAYERS_PATH, migrarlos a channels.json
        for fname in os.listdir(TMDB_PLAYERS_PATH):
            if not fname.endswith('.json') or fname.startswith('(1)'): continue
            fpath = os.path.join(TMDB_PLAYERS_PATH, fname)
            try:
                with open(fpath, 'r', encoding='utf-8') as f: p_data = json.load(f)
                engine = 'alfa' if fname.lower().startswith('alfa-') else 'balandro'
                clean = fname[:-5]
                prefix = 'Alfa-' if engine == 'alfa' else 'Balandro-'
                if clean.lower().startswith(prefix.lower()):
                    clean = clean[len(prefix):]
                is_series = False
                if clean.lower().endswith('-series'):
                    clean = re.sub(r'-series$', '', clean, flags=re.IGNORECASE)
                    is_series = True
                elif 'play_episode' in p_data and 'play_movie' not in p_data:
                    is_series = True
                ch_id = clean.lower().strip()

                pm = p_data.get('play_movie') or p_data.get('play_episode')
                url = pm[0] if isinstance(pm, list) and pm else (pm if isinstance(pm, str) else '')
                embedded_ch = ''
                if 'url=plugin://' in url:
                    sub = url.split('url=plugin://')[1].split('&')[0]
                    if '?' in sub:
                        q = sub.split('?', 1)[1]
                        try:
                            dec = json.loads(urllib.parse.unquote(base64.b64decode(urllib.parse.unquote(q)).decode('utf-8')))
                            embedded_ch = dec.get('channel')
                        except: pass
                elif 'channel=' in url:
                    m = re.search(r'channel=([^&]+)', url)
                    if m: embedded_ch = m.group(1)

                actual_ch = embedded_ch.lower().strip() if embedded_ch else ch_id
                entry = channels_data['channels'][engine].setdefault(ch_id, {
                    'id': ch_id,
                    'name': clean.strip(),
                    'channel': actual_ch,
                    'engine': engine,
                    'movies': False,
                    'series': False,
                    'movie_disabled': False,
                    'series_disabled': False,
                    'play_movie': None,
                    'play_episode': None
                })
                dis = str(p_data.get('disabled', '')).lower() in ('true', '1') or p_data.get('disabled') is True
                if is_series or 'play_episode' in p_data:
                    entry['series'] = True
                    entry['series_disabled'] = dis
                    if 'play_episode' in p_data: entry['play_episode'] = p_data['play_episode']
                if not is_series or 'play_movie' in p_data:
                    entry['movies'] = True
                    entry['movie_disabled'] = dis
                    if 'play_movie' in p_data: entry['play_movie'] = p_data['play_movie']
            except Exception: pass
        save_channels_data(channels_data)

    # 3. Limpieza de TMDb Helper: eliminar todos los players individuales
    # TMDb Helper unicamente debe tener (1)MultiBusqueda.json
    try:
        for fname in os.listdir(TMDB_PLAYERS_PATH):
            if not fname.endswith('.json') or fname.startswith('(1)MultiBusqueda'):
                continue
            fn_low = fname.lower()
            if fn_low.startswith('alfa-') or fn_low.startswith('balandro-') or fn_low.startswith('(1)alfa') or fn_low.startswith('(1)balandro'):
                try:
                    fpath = os.path.join(TMDB_PLAYERS_PATH, fname)
                    if os.path.exists(fpath):
                        os.remove(fpath)
                except Exception: pass
    except Exception as e:
        xbmc.log("Multi Bridge: error limpiando players de TMDb Helper: %s" % e, xbmc.LOGWARNING)
    _migration_checked_flag = True
    try:
        xbmcgui.Window(10000).setProperty('MultiBridge_MigrationDone', 'true')
    except Exception:
        pass

def _channel_display_name(base, channel):
    """Nombre visible del canal (ej. 'Gnula'). Lee su descriptor si existe,
    si no usa el id tal cual."""
    try:
        with open(os.path.join(base, 'channels', channel + '.json'), 'r', encoding='utf-8') as f:
            name = json.load(f).get('name', '')
        if name:
            return str(name)
    except Exception:
        pass
    return channel

def check_orphan_players():
    """Arranque de Kodi: muestra una ventana SOLO si algun canal apunta a un
    descriptor que no existe en Balandro/Alfa. Si todos existen, no muestra nada.
    Se puede desactivar con el ajuste startup_orphan_check."""
    try:
        try:
            if _bridge_addon.getSetting('startup_orphan_check') != 'true':
                return
        except Exception:
            pass

        ch_data = load_channels_data()
        channels_dict = ch_data.get('channels', {})
        missing = []

        for engine in ('alfa', 'balandro'):
            # Si el addon no está instalado en Kodi, no comprobar si sus canales existen
            if not _is_engine_installed(engine):
                xbmc.log('Multi Bridge: %s no está instalado, omitiendo comprobación de canales.' % engine.capitalize(), xbmc.LOGINFO)
                continue

            base = balandro_path if engine == 'balandro' else alfa_path
            channels_dir = os.path.join(base, 'channels')
            if not os.path.isdir(channels_dir):
                continue

            try:
                avail = [x.lower() for x in os.listdir(channels_dir)]
            except Exception:
                avail = []

            for ch_id, ch_info in channels_dict.get(engine, {}).items():
                if ch_info.get('movie_disabled') and ch_info.get('series_disabled'):
                    continue
                actual_ch = str(ch_info.get('channel') or ch_id).strip().lower()
                exists = (actual_ch + '.py') in avail
                if not exists and engine == 'alfa' and actual_ch in ('planb', 'plan_b'):
                    try:
                        libfiles = [x.lower() for x in os.listdir(os.path.join(base, 'lib'))]
                    except Exception:
                        libfiles = []
                    exists = any(x.startswith('planb') and x.endswith('.py') for x in libfiles)
                if not exists:
                    missing.append((ch_info.get('name', ch_id), actual_ch, engine))

        if not missing:
            xbmc.log('Multi Bridge: todos los canales configurados existen en Balandro/Alfa', xbmc.LOGINFO)
            return
        xbmc.log('Multi Bridge: canales no encontrados: %s' % ['%s (canal "%s" no existe en %s)' % t for t in missing], xbmc.LOGWARNING)
        try:
            mon = xbmc.Monitor()
            if not mon.waitForAbort(8):
                msg_lines = []
                for name, channel, engine in missing[:12]:
                    msg_lines.append('%s: no existe en %s' % (name, engine))
                if len(missing) > 12:
                    msg_lines.append('... y %d mas' % (len(missing) - 12))
                engines = sorted(set(e for _, _, e in missing))
                if len(engines) == 1:
                    url = 'plugin://plugin.video.bridge.multi/?view=list_players&engine=%s' % engines[0]
                else:
                    url = 'plugin://plugin.video.bridge.multi/?view=home'
                go = xbmcgui.Dialog().yesno('Multi Bridge: canal no existente',
                                            'Estos canales configurados no existen en sus respectivos addons:\n%s' % '\n'.join(msg_lines),
                                            nolabel='Cerrar', yeslabel='Ir al gestor')
                if go:
                    xbmc.executebuiltin('ActivateWindow(videos,"%s",return)' % url)
        except Exception as e:
            xbmc.log('Multi Bridge: no se pudo mostrar aviso de no existentes: %s' % e, xbmc.LOGINFO)
    except Exception as e:
        xbmc.log('Multi Bridge: check_orphan_players error: %s' % e, xbmc.LOGWARNING)

# ---------------------------------------------------------
# Player Management Menus
# ---------------------------------------------------------
def _format_regional_datetime(dt):
    """Formatea fecha y hora respetando estrictamente la configuración regional de Kodi
    (fecha corta y reloj de 12h AM/PM o 24h).
    """
    if not dt:
        return ''

    # 1. Formato de fecha según región de Kodi
    date_str = ''
    try:
        dfmt = xbmc.getRegion('dateshort')
        if dfmt:
            if '%' in dfmt:
                date_str = dt.strftime(dfmt)
            else:
                conv = dfmt.replace('YYYY', '%Y').replace('yyyy', '%Y').replace('MM', '%m').replace('mm', '%m').replace('DD', '%d').replace('dd', '%d')
                date_str = dt.strftime(conv)
    except Exception:
        pass
    if not date_str:
        date_str = dt.strftime('%d/%m/%Y')

    # 2. Formato de hora según región de Kodi (12h AM/PM o 24h)
    time_str = ''
    try:
        tfmt = xbmc.getRegion('time')
        if tfmt:
            if '%' in tfmt:
                clean_tf = tfmt.replace(':%S', '').replace(':%s', '').replace('%H%H', '%H')
                time_str = dt.strftime(clean_tf).strip()
            else:
                conv = tfmt.replace(':ss', '').replace(':SS', '').replace('ss', '').replace('SS', '')
                conv = conv.replace('HH', '%H').replace('hh', '%I').replace('h', '%I').replace('mm', '%M').replace('xx', '%p').replace('XX', '%p')
                time_str = dt.strftime(conv).strip()
    except Exception:
        pass

    if not time_str:
        try:
            tf = str(xbmc.getRegion('timeformat')).lower()
            if '12' in tf:
                time_str = dt.strftime('%I:%M %p').lstrip('0')
            elif '24' in tf:
                time_str = dt.strftime('%H:%M')
        except Exception:
            pass

    if not time_str:
        time_str = dt.strftime('%H:%M')

    return '%s a las %s' % (date_str, time_str)

def _get_last_cloud_import_datetime():
    """Devuelve (datetime_obj, total_count) de la última importación desde la nube.
    Verifica primero 'last_sync' dentro de channels.json y como respaldo en CLOUD_SYNC_FILE.
    Nunca usa mtime de archivo porque se actualiza en cada inicio o cambio de ajustes.
    """
    ch_data = load_channels_data()
    tot_channels = len(ch_data.get('channels', {}).get('alfa', {})) + len(ch_data.get('channels', {}).get('balandro', {}))

    # 1. Prioridad: campo persistente 'last_sync' dentro de channels.json
    sync_data = ch_data.get('last_sync')
    if isinstance(sync_data, dict):
        ts = sync_data.get('timestamp')
        tot = sync_data.get('total', 0)
        if ts:
            try:
                return datetime.datetime.fromtimestamp(ts), (tot or tot_channels)
            except Exception:
                pass

    # 2. Respaldo: archivo cloud_sync_info.json
    if os.path.exists(CLOUD_SYNC_FILE):
        try:
            with open(CLOUD_SYNC_FILE, 'r', encoding='utf-8') as f:
                d = json.load(f)
            ts = d.get('timestamp')
            tot = d.get('total', 0)
            if ts:
                return datetime.datetime.fromtimestamp(ts), (tot or tot_channels)
        except Exception:
            pass

    return None, tot_channels

def show_continue_watching():
    if handle < 0: return
    cw_data = load_continue_watching_data()
    valid_items = []
    for mk, rec in (cw_data or {}).items():
        if isinstance(rec, dict) and rec.get('interrupted'):
            valid_items.append((mk, rec))

    # Ordenar cronológicamente descendente por el último latido (los más recientes primero)
    valid_items.sort(key=lambda x: float(x[1].get('last_heartbeat') or 0), reverse=True)

    if not valid_items:
        xbmcplugin.setContent(handle, 'videos')
        xbmcplugin.endOfDirectory(handle, succeeded=True)
        return

    for mk, rec in valid_items:
        is_series = bool(rec.get('is_series') or (rec.get('season') and rec.get('episode')))
        tmdb_id_val = str(rec.get('tmdb') or '')
        imdb_id_val = str(rec.get('imdb') or '')
        season_val = rec.get('season')
        episode_val = rec.get('episode')
        title_val = rec.get('title') or ''
        showname_val = rec.get('showname') or ''
        year_val = str(rec.get('year') or '')
        resume_t = float(rec.get('resume_time') or 0)
        total_t = float(rec.get('total_time') or 0)

        mins = int(resume_t // 60); secs = int(resume_t % 60); hrs = mins // 60; mins = mins % 60
        time_str = ('%d:%02d:%02d' % (hrs, mins, secs)) if hrs else ('%d:%02d' % (mins, secs))

        if is_series:
            s_num = int(season_val) if (season_val is not None and str(season_val).isdigit()) else 1
            e_num = int(episode_val) if (episode_val is not None and str(episode_val).isdigit()) else 1
            show_name = showname_val or title_val
            ep_name = title_val if (title_val and title_val != show_name) else ''
            if ep_name:
                item_label = f"{show_name} - {s_num}x{e_num:02d} - {ep_name}"
            else:
                item_label = f"{show_name} - {s_num}x{e_num:02d}"

            target_url = 'plugin://plugin.video.themoviedb.helper/?info=play&tmdb_type=tv'
            if tmdb_id_val:
                target_url += f'&tmdb_id={tmdb_id_val}'
            elif imdb_id_val:
                target_url += f'&imdb_id={imdb_id_val}'
            else:
                target_url += f'&query={uparse.quote_plus(show_name)}'
            target_url += f'&season={s_num}&episode={e_num}'
        else:
            item_label = title_val or 'Película'
            if year_val:
                item_label += f' ({year_val})'

            target_url = 'plugin://plugin.video.themoviedb.helper/?info=play&tmdb_type=movie'
            if tmdb_id_val:
                target_url += f'&tmdb_id={tmdb_id_val}'
            elif imdb_id_val:
                target_url += f'&imdb_id={imdb_id_val}'
            else:
                target_url += f'&query={uparse.quote_plus(title_val)}'

        li = xbmcgui.ListItem(label=item_label)

        poster_art = rec.get('poster') or rec.get('thumbnail') or 'DefaultVideo.png'
        fanart_art = rec.get('fanart') or ''
        clearlogo_art = rec.get('clearlogo') or ''
        art_dict = {
            'poster': poster_art,
            'thumb': poster_art,
            'icon': poster_art,
            'fanart': fanart_art,
            'clearlogo': clearlogo_art,
            'landscape': fanart_art or poster_art,
            'banner': fanart_art or poster_art
        }
        li.setArt(art_dict)

        display_plot = f"[COLOR gold][B]▶ Interrumpido en {time_str}[/B][/COLOR]"

        try:
            vt = li.getVideoInfoTag()
            if vt:
                vt.setMediaType('episode' if is_series else 'movie')
                vt.setTitle(title_val or item_label)
                if is_series:
                    if showname_val: vt.setTvShowTitle(showname_val)
                    vt.setSeason(int(season_val) if (season_val is not None and str(season_val).isdigit()) else 1)
                    vt.setEpisode(int(episode_val) if (episode_val is not None and str(episode_val).isdigit()) else 1)
                if year_val and str(year_val).isdigit():
                    vt.setYear(int(year_val))
                vt.setPlot(display_plot)
                if resume_t > 0 and total_t > 0:
                    vt.setResumePoint(resume_t, total_t)
                uids = {}
                if tmdb_id_val: uids['tmdb'] = tmdb_id_val
                if imdb_id_val: uids['imdb'] = imdb_id_val
                if rec.get('tvdb'): uids['tvdb'] = str(rec['tvdb'])
                if rec.get('trakt'): uids['trakt'] = str(rec['trakt'])
                if uids:
                    vt.setUniqueIDs(uids)
        except Exception:
            pass

        info_video = {
            'title': title_val or item_label,
            'plot': display_plot,
            'mediatype': 'episode' if is_series else 'movie'
        }
        if is_series and showname_val:
            info_video['tvshowtitle'] = showname_val
        if is_series and season_val is not None:
            try: info_video['season'] = int(season_val)
            except: pass
        if is_series and episode_val is not None:
            try: info_video['episode'] = int(episode_val)
            except: pass
        if year_val and str(year_val).isdigit():
            try: info_video['year'] = int(year_val)
            except: pass
        if total_t > 0:
            info_video['duration'] = int(total_t)
        try:
            li.setInfo('video', info_video)
        except Exception:
            pass

        if total_t > 0 and resume_t > 0:
            pct = int((resume_t / total_t) * 100.0)
            li.setProperty('PercentPlayed', str(pct))
            li.setProperty('ResumeTime', str(resume_t))
            li.setProperty('TotalTime', str(total_t))

        rm_url = 'plugin://plugin.video.bridge.multi/?action=remove_continue_watching&media_key=%s' % uparse.quote_plus(mk)
        cl_url = 'plugin://plugin.video.bridge.multi/?action=clear_all_continue_watching'
        li.addContextMenuItems([
            ('[COLOR red]Quitar de Continuar viendo[/COLOR]', 'RunPlugin(%s)' % rm_url),
            ('[COLOR orange]Limpiar todo Continuar viendo[/COLOR]', 'RunPlugin(%s)' % cl_url),
        ])

        xbmcplugin.addDirectoryItem(handle, target_url, li, isFolder=False)

    xbmcplugin.setContent(handle, 'videos')
    xbmcplugin.endOfDirectory(handle, succeeded=True)

def show_player_manager_home():
    if handle < 0: return
    alfa_icon = os.path.join(alfa_path, 'resources', 'icon.png')
    if not os.path.exists(alfa_icon): alfa_icon = 'DefaultFolder.png'

    balandro_icon = os.path.join(balandro_path, 'icon.png')
    if not os.path.exists(balandro_icon): balandro_icon = 'DefaultFolder.png'

    last_dt, tot_channels = _get_last_cloud_import_datetime()
    if last_dt:
        dt_formatted = _format_regional_datetime(last_dt)
        cloud_plot = (
            'Descargar y sincronizar todos los canales de Alfa y Balandro desde GitHub.\n\n'
            '[COLOR lime][B]Última importación:[/B][/COLOR] %s' % dt_formatted
        )
        if tot_channels > 0:
            cloud_plot += '\n[COLOR grey]Canales configurados: %d[/COLOR]' % tot_channels
    else:
        cloud_plot = (
            'Descargar y sincronizar todos los canales de Alfa y Balandro desde GitHub.\n\n'
            '[COLOR orange][B]Última importación:[/B] Aún no se ha realizado ninguna importación.[/COLOR]'
        )

    cw_data = load_continue_watching_data()
    cw_count = len(cw_data) if isinstance(cw_data, dict) else 0
    cw_badge = (' (%d)' % cw_count) if cw_count > 0 else ''
    cw_plot = (
        'Películas y episodios cuya reproducción fue interrumpida abruptamente (corte de luz, congelamiento o cierre de Kodi).\n\n'
        '[COLOR gold]Al seleccionarlo, TMDb Helper buscará de nuevo en los canales y continuará desde el punto exacto guardado.[/COLOR]'
    )

    alfa_ok = _is_engine_installed('alfa')
    bal_ok = _is_engine_installed('balandro')
    alfa_title_str = 'Gestor de Canales de Alfa (Películas)' if alfa_ok else 'Gestor de Canales de Alfa [COLOR red](No instalado)[/COLOR]'
    bal_title_str = 'Gestor de Canales de Balandro' if bal_ok else 'Gestor de Canales de Balandro [COLOR red](No instalado)[/COLOR]'

    items = [
        ('Continuar', 'Continuar viendo%s' % cw_badge, 'plugin://plugin.video.bridge.multi/?view=continue_watching', 'DefaultInProgressShows.png', True,
         cw_plot),
        ('Alfa', alfa_title_str, 'plugin://plugin.video.bridge.multi/?view=list_players&engine=alfa', alfa_icon, True,
         'Administrar, activar o desactivar canales de Alfa para búsquedas en Multi Bridge.'),
        ('Balandro', bal_title_str, 'plugin://plugin.video.bridge.multi/?view=list_players&engine=balandro', balandro_icon, True,
         'Administrar, activar o desactivar canales de Balandro (Películas y Series).'),
        ('Crear', 'Añadir Canal (Asistente)', 'plugin://plugin.video.bridge.multi/?view=create_player', 'DefaultAddSource.png', False,
         'Asistente guiado paso a paso para añadir nuevos canales disponibles a Multi Bridge.'),
        ('Nube', 'Importar / Actualizar Canales desde la Nube', 'plugin://plugin.video.bridge.multi/?view=update_cloud', 'DefaultNetwork.png', False,
         cloud_plot),
        ('Ajustes', 'Ajustes del Addon', 'plugin://plugin.video.bridge.multi/?view=settings', 'DefaultAddonProgram.png', False,
         'Configurar opciones del addon, orden de servidores, calidades, idiomas y repositorio de GitHub.'),
    ]
    for tag, title_str, item_url, icon, is_f, plot_str in items:
        li = xbmcgui.ListItem(label='[B][COLOR deepskyblue][%s][/COLOR] [COLOR white]%s[/COLOR][/B]' % (tag, title_str))
        li.setArt({'thumb': icon, 'icon': icon, 'poster': icon, 'banner': icon})
        try:
            vt = li.getVideoInfoTag()
            if vt:
                vt.setTitle(title_str)
                vt.setPlot(plot_str)
                vt.setMediaType('video')
        except Exception:
            pass
        try:
            li.setInfo('video', {'title': title_str, 'plot': plot_str})
        except Exception:
            pass
        xbmcplugin.addDirectoryItem(handle, item_url, li, isFolder=is_f)
    xbmcplugin.setContent(handle, 'videos')
    xbmcplugin.endOfDirectory(handle)

def _get_channel_group_data(engine='alfa'):
    ch_data = load_channels_data()
    return ch_data.get('channels', {}).get(engine, {})

def _get_channel_icon_and_info(channel_id, engine='alfa'):
    """Obtiene el icono original y la información de un canal directamente desde la ruta de su addon
    (Alfa o Balandro), sin descargar ni almacenar ninguna imagen en Multi Bridge.
    Soporta URLs remotas y rutas locales de forma nativa.
    """
    addon_path = alfa_path if engine == 'alfa' else balandro_path
    ch_dir = os.path.join(addon_path, 'channels')

    ch_json = os.path.join(ch_dir, channel_id + '.json')
    if not os.path.exists(ch_json) and os.path.exists(ch_dir):
        for f in os.listdir(ch_dir):
            if f.lower() == (channel_id + '.json').lower():
                ch_json = os.path.join(ch_dir, f)
                break

    # Si no existe por nombre directo, buscar por coincidencia parcial en channels
    if not os.path.exists(ch_json) and os.path.exists(ch_dir):
        for f in os.listdir(ch_dir):
            if f.endswith('.json') and (f.lower().startswith(channel_id.lower()) or channel_id.lower().startswith(f[:-5].lower())):
                ch_json = os.path.join(ch_dir, f)
                break

    icon = None
    title = ''
    plot = ''

    if os.path.exists(ch_json):
        try:
            with open(ch_json, 'r', encoding='utf-8', errors='ignore') as f:
                ch_data = json.load(f)
            title = ch_data.get('name') or ''
            plot = ch_data.get('notes') or ''

            thumb = ch_data.get('thumbnail') or ch_data.get('banner') or ''
            if thumb:
                if thumb.startswith('http://') or thumb.startswith('https://'):
                    icon = thumb
                else:
                    local_thumb = os.path.join(addon_path, 'resources', 'media', 'channels', 'thumb', thumb)
                    if os.path.exists(local_thumb):
                        icon = local_thumb
                    else:
                        local_thumb2 = os.path.join(addon_path, 'resources', 'media', 'channels', thumb)
                        if os.path.exists(local_thumb2):
                            icon = local_thumb2

            if not plot:
                langs = [str(l).upper() for l in ch_data.get('language', [])]
                cats = [str(c).capitalize() for c in ch_data.get('categories', [])]
                parts = []
                if langs: parts.append('Idiomas: ' + ', '.join(langs))
                if cats: parts.append('Categorías: ' + ', '.join(cats))
                if parts: plot = ' | '.join(parts)
        except:
            pass

    if not icon:
        for ext in ('.jpg', '.png', '.jpeg'):
            cand_thumb = os.path.join(addon_path, 'resources', 'media', 'channels', 'thumb', channel_id + ext)
            if os.path.exists(cand_thumb):
                icon = cand_thumb
                break

    if not icon:
        for cand_icon in (os.path.join(addon_path, 'resources', 'icon.png'), os.path.join(addon_path, 'icon.png')):
            if os.path.exists(cand_icon):
                icon = cand_icon
                break

    if not icon:
        icon = 'DefaultFolder.png'

    return icon, title, plot

def show_players_list(engine='alfa'):
    if handle < 0: return
    channels = _get_channel_group_data(engine=engine)
    if not channels:
        xbmcplugin.endOfDirectory(handle); return

    for norm_key in sorted(channels.keys(), key=lambda x: channels[x].get('name', x).lower()):
        info = channels[norm_key]
        name = info.get('name', norm_key)
        has_movie = bool(info.get('movies'))
        has_series = bool(info.get('series'))
        has_both = has_movie and has_series
        m_dis = bool(info.get('movie_disabled'))
        s_dis = bool(info.get('series_disabled'))

        if has_both:
            if not m_dis and not s_dis:
                status_icon = '[COLOR lime]●[/COLOR]'
                type_str = '[COLOR deepskyblue]Películas[/COLOR] + [COLOR gold]Series[/COLOR]'
            elif m_dis and s_dis:
                status_icon = '[COLOR red]●[/COLOR]'
                type_str = '[COLOR gray]Desactivado[/COLOR]'
            elif not m_dis and s_dis:
                status_icon = '[COLOR orange]●[/COLOR]'
                type_str = '[COLOR deepskyblue]Películas[/COLOR] [COLOR gray]| Series Desactivadas[/COLOR]'
            else:
                status_icon = '[COLOR orange]●[/COLOR]'
                type_str = '[COLOR gold]Series[/COLOR] [COLOR gray]| Películas Desactivadas[/COLOR]'
        elif has_movie:
            if not m_dis:
                status_icon = '[COLOR lime]●[/COLOR]'
                type_str = '[COLOR deepskyblue]Solo Películas[/COLOR]'
            else:
                status_icon = '[COLOR red]●[/COLOR]'
                type_str = '[COLOR gray]Desactivado (Películas)[/COLOR]'
        else:
            if not s_dis:
                status_icon = '[COLOR lime]●[/COLOR]'
                type_str = '[COLOR gold]Solo Series[/COLOR]'
            else:
                status_icon = '[COLOR red]●[/COLOR]'
                type_str = '[COLOR gray]Desactivado (Series)[/COLOR]'

        lbl = '%s  [B]%s[/B]   [%s]' % (status_icon, name, type_str)
        opt_url = 'plugin://plugin.video.bridge.multi/?view=player_options&channel=%s&engine=%s' % (norm_key, engine)
        li = xbmcgui.ListItem(label=lbl)

        actual_ch = str(info.get('channel') or norm_key).strip().lower()
        icon, ch_title, ch_plot = _get_channel_icon_and_info(actual_ch, engine=engine)

        li.setArt({
            'thumb': icon,
            'icon': icon,
            'poster': icon,
            'banner': icon
        })

        display_title = ch_title if ch_title else name
        desc_plot = ch_plot if ch_plot else ('Canal de %s' % engine.capitalize())

        try:
            vt = li.getVideoInfoTag()
            if vt:
                vt.setTitle(display_title)
                vt.setPlot(desc_plot)
                vt.setMediaType('video')
        except Exception:
            pass
        try:
            li.setInfo('video', {'title': display_title, 'plot': desc_plot})
        except Exception:
            pass

        xbmcplugin.addDirectoryItem(handle, opt_url, li, isFolder=False)

    xbmcplugin.setContent(handle, 'videos')
    xbmcplugin.endOfDirectory(handle)

def show_player_options(channel_key, engine='alfa'):
    if not channel_key: return
    dialog = xbmcgui.Dialog()

    ch_data = load_channels_data()
    engine_channels = ch_data.setdefault('channels', {}).setdefault(engine, {})

    clean_key = str(channel_key).lower().replace('.json', '')
    prefix = 'alfa-' if engine == 'alfa' else 'balandro-'
    if clean_key.startswith(prefix):
        clean_key = clean_key[len(prefix):]
    clean_key = re.sub(r'-series$', '', clean_key, flags=re.IGNORECASE).strip()

    info = engine_channels.get(clean_key)
    if not info:
        for k, v in engine_channels.items():
            if str(v.get('name', '')).lower() == clean_key or str(v.get('channel', '')).lower() == clean_key:
                clean_key = k
                info = v
                break

    if not info:
        return

    name = info.get('name', clean_key)
    has_movie = bool(info.get('movies'))
    has_series = bool(info.get('series'))
    has_both = has_movie and has_series
    m_dis = bool(info.get('movie_disabled'))
    s_dis = bool(info.get('series_disabled'))

    if has_both:
        opt_movie = ('[COLOR red]Desactivar[/COLOR] Películas' if not m_dis else '[COLOR lime]Activar[/COLOR] Películas')
        opt_series = ('[COLOR red]Desactivar[/COLOR] Series' if not s_dis else '[COLOR lime]Activar[/COLOR] Series')
        both_active = (not m_dis and not s_dis)
        opt_all = ('[COLOR red]Desactivar Todo[/COLOR] (Películas y Series)' if both_active else '[COLOR lime]Activar Todo[/COLOR] (Películas y Series)')
        opt_delete = '[COLOR red]Eliminar Canal[/COLOR]'
        opt_back = 'Volver'

        options = [opt_movie, opt_series, opt_all, opt_delete, opt_back]
        sel = dialog.select('Canal: %s' % name, options)

        if sel == 0:
            info['movie_disabled'] = not m_dis
            save_channels_data(ch_data)
            dialog.notification('Multi Bridge', 'Películas de %s %s' % (name, 'desactivadas' if not m_dis else 'activadas'), '', 2000)
            xbmc.executebuiltin('Container.Refresh')
        elif sel == 1:
            info['series_disabled'] = not s_dis
            save_channels_data(ch_data)
            dialog.notification('Multi Bridge', 'Series de %s %s' % (name, 'desactivadas' if not s_dis else 'activadas'), '', 2000)
            xbmc.executebuiltin('Container.Refresh')
        elif sel == 2:
            new_val = both_active
            info['movie_disabled'] = new_val
            info['series_disabled'] = new_val
            save_channels_data(ch_data)
            dialog.notification('Multi Bridge', '%s %s' % (name, 'desactivado' if new_val else 'activado'), '', 2000)
            xbmc.executebuiltin('Container.Refresh')
        elif sel == 3:
            if dialog.yesno('Eliminar Canal', '¿Seguro que deseas eliminar el canal %s?' % name):
                if clean_key in engine_channels:
                    del engine_channels[clean_key]
                save_channels_data(ch_data)
                dialog.notification('Multi Bridge', '%s eliminado' % name, '', 2000)
                xbmc.executebuiltin('Container.Refresh')

    else:
        target_type = 'Películas' if has_movie else 'Series'
        is_dis = m_dis if has_movie else s_dis

        opt_toggle = ('[COLOR red]Desactivar[/COLOR] %s' % target_type if not is_dis else '[COLOR lime]Activar[/COLOR] %s' % target_type)
        opt_delete = '[COLOR red]Eliminar Canal[/COLOR]'
        opt_back = 'Volver'

        options = [opt_toggle, opt_delete, opt_back]
        sel = dialog.select('Canal: %s (%s)' % (name, target_type), options)

        if sel == 0:
            if has_movie:
                info['movie_disabled'] = not m_dis
            else:
                info['series_disabled'] = not s_dis
            save_channels_data(ch_data)
            dialog.notification('Multi Bridge', '%s (%s) %s' % (name, target_type, 'desactivado' if not is_dis else 'activado'), '', 2000)
            xbmc.executebuiltin('Container.Refresh')
        elif sel == 1:
            if dialog.yesno('Eliminar Canal', '¿Seguro que deseas eliminar el canal %s?' % name):
                if clean_key in engine_channels:
                    del engine_channels[clean_key]
                save_channels_data(ch_data)
                dialog.notification('Multi Bridge', '%s eliminado' % name, '', 2000)
                xbmc.executebuiltin('Container.Refresh')

def _detect_channel_capabilities(engine, channel_id, mods_path):
    ch_json = os.path.join(mods_path, 'channels', channel_id + '.json')
    supports_movies = True
    supports_series = False
    if os.path.exists(ch_json):
        try:
            with open(ch_json, 'r', encoding='utf-8') as f:
                d = json.load(f)
            cats = [str(c).lower() for c in d.get('categories', [])]
            stypes = [str(s).lower() for s in d.get('search_types', [])]
            if cats or stypes:
                supports_movies = ('movie' in cats or 'movie' in stypes or 'all' in stypes)
                supports_series = ('tvshow' in cats or 'tvshow' in stypes or 'series' in cats)
        except: pass
    if not supports_series:
        ch_py = os.path.join(mods_path, 'channels', channel_id + '.py')
        if os.path.exists(ch_py):
            try:
                with open(ch_py, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                supports_series = bool('episodios' in content or 'temporadas' in content or 'search_type == "tvshow"' in content)
            except: pass
    return supports_movies, supports_series

def _add_channel_to_json(engine, channel_id, is_series=False, also_movies=False):
    ch_data = load_channels_data()
    engine_channels = ch_data.setdefault('channels', {}).setdefault(engine, {})
    ch_clean = channel_id.lower().strip()

    addon_path = alfa_path if engine == 'alfa' else balandro_path
    display_name = _channel_display_name(addon_path, ch_clean)
    if not display_name or display_name == ch_clean:
        display_name = channel_id.capitalize()

    if ch_clean not in engine_channels:
        engine_channels[ch_clean] = {
            'id': ch_clean,
            'name': display_name,
            'channel': ch_clean,
            'engine': engine,
            'movies': False,
            'series': False,
            'movie_disabled': False,
            'series_disabled': False,
            'play_movie': None,
            'play_episode': None
        }

    entry = engine_channels[ch_clean]
    if also_movies:
        entry['movies'] = True
        entry['movie_disabled'] = False
        entry['series'] = True
        entry['series_disabled'] = False
    elif is_series:
        entry['series'] = True
        entry['series_disabled'] = False
    else:
        entry['movies'] = True
        entry['movie_disabled'] = False

    save_channels_data(ch_data)
    return display_name

def create_player_wizard():
    alfa_ok = _is_engine_installed('alfa')
    bal_ok = _is_engine_installed('balandro')

    if not alfa_ok and not bal_ok:
        xbmcgui.Dialog().ok('Multi Bridge', 'No se encontró ni Alfa ni Balandro instalado.')
        return

    dialog = xbmcgui.Dialog()
    if alfa_ok and not bal_ok:
        engine = 'alfa'
    elif bal_ok and not alfa_ok:
        engine = 'balandro'
    else:
        engine_choice = dialog.select('¿Para qué addon deseas añadir el canal?', ['Alfa (Solo Películas)', 'Balandro'])
        if engine_choice < 0: return
        engine = 'alfa' if engine_choice == 0 else 'balandro'

    mods = _get_alfa_modules() if engine == 'alfa' else _get_balandro_modules()
    if not mods:
        dialog.ok('Multi Bridge', 'El addon %s no está instalado o disponible.' % engine.capitalize())
        return

    channels_dir = os.path.join(mods['path'], 'channels')
    if not os.path.exists(channels_dir): return
    available = []
    for f in sorted(os.listdir(channels_dir)):
        if f.endswith('.py') and not f.startswith('__'):
            ch_id = f.replace('.py', '')
            available.append(ch_id)

    sel_ch = dialog.select('Selecciona Canal de %s' % engine.capitalize(), [c.capitalize() for c in available])
    if sel_ch < 0: return
    chosen_id = available[sel_ch]
    ch_clean = chosen_id.capitalize()

    supports_movies, supports_series = _detect_channel_capabilities(engine, chosen_id, mods['path'])

    created_name = ''
    if engine == 'balandro' and supports_series:
        if supports_movies:
            opts = [
                'Habilitar Ambos (Películas y Series)',
                'Solo Películas',
                'Solo Series',
                'Cancelar'
            ]
            choice = dialog.select('El canal %s también soporta Series' % ch_clean, opts)
            if choice == 0:
                created_name = _add_channel_to_json(engine, chosen_id, is_series=True, also_movies=True)
            elif choice == 1:
                created_name = _add_channel_to_json(engine, chosen_id, is_series=False)
            elif choice == 2:
                created_name = _add_channel_to_json(engine, chosen_id, is_series=True)
            else:
                return
        else:
            created_name = _add_channel_to_json(engine, chosen_id, is_series=True)
    else:
        created_name = _add_channel_to_json(engine, chosen_id, is_series=False)

    if created_name:
        dialog.ok('Multi Bridge', '¡Canal %s añadido con éxito a Multi Bridge!' % created_name)

def update_players_from_cloud():
    """Descarga e importa el archivo channels.json desde el repositorio de GitHub (01xKeven/Players-Multi),
    realiza un merge inteligente respetando canales desactivados por el usuario, y asegura que TMDb Helper
    contenga únicamente (1)MultiBusqueda.json.
    """
    dialog = xbmcgui.Dialog()
    repo_url = _bridge_addon.getSetting('cloud_repo_url') if _bridge_addon else ''
    if not repo_url or not repo_url.strip():
        repo_url = 'https://github.com/01xKeven/Players-Multi'

    confirm = dialog.yesno(
        'Multi Bridge — Nube',
        '¿Deseas descargar y actualizar los canales desde la Nube (GitHub)?\n\n'
        '[COLOR deepskyblue]Repositorio:[/COLOR] %s\n'
        '[COLOR gold]Aviso:[/COLOR] Se actualizará la lista de canales interna (channels.json).' % repo_url
    )
    if not confirm:
        return

    p_dialog = xbmcgui.DialogProgress()
    p_dialog.create('Multi Bridge — Nube', 'Conectando con GitHub...')
    p_dialog.update(10, 'Descargando channels.json desde GitHub...')

    clean_repo = repo_url.strip().rstrip('/')
    if clean_repo.startswith('https://github.com/'):
        clean_repo = clean_repo[len('https://github.com/'):]
    elif clean_repo.startswith('http://github.com/'):
        clean_repo = clean_repo[len('http://github.com/'):]

    token = _bridge_addon.getSetting('github_token').strip() if _bridge_addon else ''
    headers = {'User-Agent': 'Mozilla/5.0'}
    if token:
        headers['Authorization'] = 'token %s' % token

    raw_candidates = [
        'https://raw.githubusercontent.com/%s/master/channels.json' % clean_repo,
        'https://raw.githubusercontent.com/%s/main/channels.json' % clean_repo,
    ]

    remote_json_bytes = None
    download_error = ''

    for u in raw_candidates:
        if p_dialog.iscanceled():
            p_dialog.close()
            return
        try:
            xbmc.log('Multi Bridge Nube: intentando descargar ' + u, xbmc.LOGINFO)
            req = urllib.request.Request(u, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                if resp.status == 200:
                    remote_json_bytes = resp.read()
                    break
        except Exception as e:
            download_error = str(e)
            xbmc.log('Multi Bridge Nube: error descargando ' + u + ': ' + str(e), xbmc.LOGWARNING)

    # Fallback to ZIP if raw fails
    if not remote_json_bytes:
        import zipfile, io
        zip_candidates = [
            'https://github.com/%s/archive/refs/heads/master.zip' % clean_repo,
            'https://github.com/%s/archive/refs/heads/main.zip' % clean_repo,
            'https://codeload.github.com/%s/zip/refs/heads/master' % clean_repo,
        ]
        for u in zip_candidates:
            if p_dialog.iscanceled():
                p_dialog.close()
                return
            try:
                xbmc.log('Multi Bridge Nube: intentando descargar zip ' + u, xbmc.LOGINFO)
                req = urllib.request.Request(u, headers=headers)
                with urllib.request.urlopen(req, timeout=20) as resp:
                    if resp.status == 200:
                        zf = zipfile.ZipFile(io.BytesIO(resp.read()))
                        for zi in zf.infolist():
                            if zi.filename.endswith('channels.json'):
                                remote_json_bytes = zf.read(zi.filename)
                                break
                        if remote_json_bytes:
                            break
            except Exception as e:
                download_error = str(e)
                xbmc.log('Multi Bridge Nube: error en fallback zip: ' + str(e), xbmc.LOGWARNING)

    if not remote_json_bytes:
        try: p_dialog.close()
        except: pass
        dialog.ok(
            'Multi Bridge — Error en Nube',
            'No se pudo descargar channels.json desde GitHub.\n\n'
            '[COLOR red]Detalle:[/COLOR] %s\n'
            'Verifica tu conexión o el repositorio: %s' % (download_error or 'Descarga fallida', repo_url)
        )
        return

    p_dialog.update(60, 'Procesando y sincronizando canales...')

    try:
        remote_data = json.loads(remote_json_bytes.decode('utf-8'))
    except Exception as e:
        try: p_dialog.close()
        except: pass
        dialog.ok('Multi Bridge — Error', 'El archivo channels.json remoto no es JSON válido: ' + str(e))
        return

    local_data = load_channels_data()
    local_channels = local_data.setdefault('channels', {})
    remote_channels = remote_data.get('channels', {})

    alfa_count = 0
    balandro_count = 0

    for eng in ('alfa', 'balandro'):
        rem_eng = remote_channels.get(eng, {})
        loc_eng = local_channels.setdefault(eng, {})
        for ch_id, r_info in rem_eng.items():
            if ch_id in loc_eng:
                # Conservar preferencias locales de desactivado
                r_info['movie_disabled'] = loc_eng[ch_id].get('movie_disabled', False)
                r_info['series_disabled'] = loc_eng[ch_id].get('series_disabled', False)
            loc_eng[ch_id] = r_info
            if eng == 'alfa':
                alfa_count += 1
            else:
                balandro_count += 1

    total_count = alfa_count + balandro_count
    sync_payload = {
        'timestamp': int(datetime.datetime.now().timestamp()),
        'iso': datetime.datetime.now().isoformat(),
        'total_alfa': alfa_count,
        'total_balandro': balandro_count,
        'total': total_count
    }
    local_data['last_sync'] = sync_payload
    save_channels_data(local_data)

    try:
        with open(CLOUD_SYNC_FILE, 'w', encoding='utf-8') as _cf:
            json.dump(sync_payload, _cf, indent=4)
    except Exception as e:
        xbmc.log('Multi Bridge Nube: error guardando sync_info: ' + str(e), xbmc.LOGWARNING)

    p_dialog.update(90, 'Verificando y limpiando carpeta de TMDb Helper...')
    try:
        check_and_run_migration()
    except Exception:
        pass

    try: p_dialog.close()
    except: pass

    dialog.ok(
        'Multi Bridge — Nube',
        '¡Sincronización de la Nube completada!\n\n'
        '• [B][COLOR deepskyblue]Canales Alfa:[/COLOR][/B] %d sincronizados\n'
        '• [B][COLOR gold]Canales Balandro:[/COLOR][/B] %d sincronizados\n'
        '• [B][COLOR lime]Total actualizados:[/COLOR][/B] %d canales\n\n'
        'Los canales internos de Multi Bridge han sido actualizados con éxito y TMDb Helper se mantiene limpio.' % (alfa_count, balandro_count, total_count)
    )
    try:
        xbmc.executebuiltin('Container.Refresh')
    except Exception:
        pass

# ---------------------------------------------------------
# Custom Engine Picker Dialog (ventana personalizada)
# ---------------------------------------------------------
class _EnginePickerDialog(xbmcgui.WindowDialog):
    ACTION_MOVE_LEFT   = 1
    ACTION_MOVE_RIGHT  = 2
    ACTION_SELECT_ITEM = 7
    ACTION_PREVIOUS_MENU = 10
    ACTION_NAV_BACK    = 92
    ACTION_MOUSE_MOVE  = 107

    def __init__(self, alfa_icon_path, balandro_icon_path):
        super().__init__()
        self.selected = -1          # -1 = cancelado
        self.current  = 0           # 0=Alfa 1=Balandro
        self._alfa_icon  = alfa_icon_path
        self._bal_icon   = balandro_icon_path
        self._alfa_hls   = []
        self._bal_hls    = []
        self._alfa_img_id = -1
        self._bal_img_id  = -1
        try:
            self._build()
        except Exception as e:
            xbmc.log('BridgeMulti EnginePickerDialog build error: ' + str(e), xbmc.LOGWARNING)

    @staticmethod
    def _white():
        # Textura blanca integrada del skin Estuary de Kodi (no requiere archivo propio)
        import xbmcvfs
        candidates = [
            'special://xbmc/addons/skin.estuary/media/white-diffuse.png',
            'special://xbmc/addons/skin.estuary/media/Colours/white.png',
            'special://xbmcbin/addons/skin.estuary/media/white-diffuse.png',
        ]
        for c in candidates:
            if xbmcvfs.exists(c):
                return c
        # Fallback: crear un PNG blanco 1x1 en memoria temporal
        import tempfile, struct, zlib
        _png = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
                b'\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xff\xff'
                b'\x3f\x00\x05\xfe\x02\xfe\xdc\xccY\xe7\x00\x00\x00\x00IEND\xaeB`\x82')
        _tmp = os.path.join(tempfile.gettempdir(), 'bm_white.png')
        try:
            if not os.path.exists(_tmp):
                with open(_tmp, 'wb') as _f: _f.write(_png)
        except: pass
        return _tmp


    def _build(self):
        W = self.getWidth()
        H = self.getHeight()

        # ----- dimensiones del dialogo -----
        dw, dh = 660, 340
        dx = (W - dw) // 2
        dy = (H - dh) // 2
        border = 4
        white  = self._white()

        # Borde dorado exterior
        self.addControl(xbmcgui.ControlImage(
            dx, dy, dw, dh, white, colorDiffuse='0xFFCFA82C'))

        # Fondo oscuro interior (negro puro)
        self.addControl(xbmcgui.ControlImage(
            dx+border, dy+border, dw-border*2, dh-border*2,
            white, colorDiffuse='0xFF000000'))

        # Titulo
        self.addControl(xbmcgui.ControlLabel(
            dx, dy+14, dw, 36,
            '[B]¿Con qué addon deseas buscar?[/B]',
            font='font13', textColor='0xFFCFA82C', alignment=6))

        # Línea separadora dorada
        self.addControl(xbmcgui.ControlImage(
            dx+30, dy+56, dw-60, 2, white, colorDiffuse='0x55CFA82C'))

        # ----- opciones: iconos centrados horizontalmente -----
        icon_sz  = 148
        gap      = 100
        total_w  = icon_sz*2 + gap
        opt1_x   = dx + (dw - total_w) // 2
        opt2_x   = opt1_x + icon_sz + gap
        icon_y   = dy + 76

        # --- Alfa ---
        alfa_img = xbmcgui.ControlImage(opt1_x, icon_y, icon_sz, icon_sz, self._alfa_icon)
        self.addControl(alfa_img)
        self._alfa_img_id = alfa_img.getId()

        self.addControl(xbmcgui.ControlLabel(
            opt1_x, icon_y+icon_sz+6, icon_sz, 32,
            '[B]Alfa[/B]', font='font13',
            textColor='0xFFFFFFFF', alignment=6))

        # --- Balandro ---
        bal_img = xbmcgui.ControlImage(opt2_x, icon_y, icon_sz, icon_sz, self._bal_icon)
        self.addControl(bal_img)
        self._bal_img_id = bal_img.getId()

        self.addControl(xbmcgui.ControlLabel(
            opt2_x, icon_y+icon_sz+6, icon_sz, 32,
            '[B]Balandro[/B]', font='font13',
            textColor='0xFFFFFFFF', alignment=6))

        # ----- highlight dorado (4 lados como borde) -----
        hl_mg  = 8
        hl_sz  = icon_sz + hl_mg*2
        thick  = 3
        GOLD   = '0xFFCFA82C'

        def make_hl(ox, oy):
            return [
                xbmcgui.ControlImage(ox-hl_mg,           oy-hl_mg,           hl_sz,  thick, white, colorDiffuse=GOLD),
                xbmcgui.ControlImage(ox-hl_mg,           oy+icon_sz+hl_mg-thick, hl_sz,  thick, white, colorDiffuse=GOLD),
                xbmcgui.ControlImage(ox-hl_mg,           oy-hl_mg,           thick, hl_sz, white, colorDiffuse=GOLD),
                xbmcgui.ControlImage(ox+icon_sz+hl_mg-thick, oy-hl_mg,       thick, hl_sz, white, colorDiffuse=GOLD),
            ]

        self._alfa_hls = make_hl(opt1_x, icon_y)
        self._bal_hls  = make_hl(opt2_x, icon_y)

        for c in self._alfa_hls + self._bal_hls:
            self.addControl(c)

        # Hint
        self.addControl(xbmcgui.ControlLabel(
            dx, dy+dh-34, dw, 28,
            '[COLOR=888888][← →] Navegar    [OK] Confirmar    [Atrás] Cancelar[/COLOR]',
            font='font12', textColor='0xFF888888', alignment=6))

        self._update_highlight()

    def _update_highlight(self):
        for c in self._alfa_hls:
            c.setVisible(self.current == 0)
        for c in self._bal_hls:
            c.setVisible(self.current == 1)

    def onAction(self, action):
        aid = action.getId()
        if aid in (self.ACTION_PREVIOUS_MENU, self.ACTION_NAV_BACK):
            self.selected = -1
            self.close()
        elif aid == self.ACTION_MOVE_LEFT:
            self.current = 0
            self._update_highlight()
        elif aid == self.ACTION_MOVE_RIGHT:
            self.current = 1
            self._update_highlight()
        elif aid == self.ACTION_SELECT_ITEM:
            self.selected = self.current
            self.close()

    def onClick(self, control_id):
        if control_id == self._alfa_img_id:
            self.selected = 0
            self.close()
        elif control_id == self._bal_img_id:
            self.selected = 1
            self.close()


def _show_engine_picker():
    """Muestra la ventana personalizada de seleccion de motor.
    Devuelve 'alfa', 'balandro' o None si se cancela."""
    alfa_ok = _is_engine_installed('alfa')
    bal_ok  = _is_engine_installed('balandro')

    if not alfa_ok and not bal_ok:
        xbmcgui.Dialog().ok('Multi Bridge', 'No se encontró ni Alfa ni Balandro instalados.\nInstala al menos uno de ellos para buscar contenido.')
        return None

    if alfa_ok and not bal_ok:
        return 'alfa'

    if bal_ok and not alfa_ok:
        return 'balandro'

    try:
        _ch_info = load_channels_data().get('channels', {})
        _a_has = any(c.get('movies') and not c.get('movie_disabled') for c in _ch_info.get('alfa', {}).values())
        _b_has = any(c.get('movies') and not c.get('movie_disabled') for c in _ch_info.get('balandro', {}).values())
        if _a_has and not _b_has:
            return 'alfa'
        if _b_has and not _a_has:
            return 'balandro'
    except Exception:
        pass

    try:
        alfa_addon = xbmcaddon.Addon('plugin.video.alfa')
        alfa_icon  = os.path.join(alfa_addon.getAddonInfo('path'), 'resources', 'icon.png')
        if not os.path.exists(alfa_icon):
            alfa_icon = alfa_addon.getAddonInfo('icon') or ''
    except Exception:
        alfa_icon = ''

    try:
        bal_addon  = xbmcaddon.Addon('plugin.video.balandro')
        bal_icon   = os.path.join(bal_addon.getAddonInfo('path'), 'icon.png')
        if not os.path.exists(bal_icon):
            bal_icon = bal_addon.getAddonInfo('icon') or ''
    except Exception:
        bal_icon = ''

    try:
        dlg = _EnginePickerDialog(alfa_icon, bal_icon)
        dlg.doModal()
        sel = dlg.selected
        del dlg
    except Exception as e:
        xbmc.log('BridgeMulti _show_engine_picker error: ' + str(e), xbmc.LOGWARNING)
        # Fallback al dialogo nativo de Kodi si algo falla
        fb = xbmcgui.Dialog().select(
            '¿Dónde deseas buscar?',
            ['[B][COLOR gold]Alfa[/COLOR][/B]', '[B][COLOR deepskyblue]Balandro[/COLOR][/B]'])
        return ('alfa' if fb == 0 else 'balandro') if fb >= 0 else None

    if sel < 0:
        return None
    return 'alfa' if sel == 0 else 'balandro'


class _AutoplayStopDialog(xbmcgui.WindowDialog):
    """Ventana compacta estilo Kodi (titulo + mensaje + fila de 3 botones)
    con el dorado del selector de motor. Devuelve 0=Si, 1=No, 2=Ver enlaces,
    -1 si se cierra con Atras."""
    ACTION_MOVE_LEFT   = 1
    ACTION_MOVE_RIGHT  = 2
    ACTION_MOVE_UP     = 3
    ACTION_MOVE_DOWN   = 4
    ACTION_SELECT_ITEM = 7
    ACTION_PREVIOUS_MENU = 10
    ACTION_STOP          = 13
    ACTION_NAV_BACK      = 92
    ACTION_MOUSE_LEFT_CLICK = 100
    ACTION_MOUSE_DOUBLE_CLICK = 103

    _LABELS = ('Sí', 'No', 'Ver enlaces')

    def __init__(self, server_label):
        super().__init__()
        self.selected = -1
        self.current  = 0
        self._server_label = server_label or ''
        self._btn_ids = []
        self._highlights = []
        try:
            self._build()
        except Exception as e:
            xbmc.log('BridgeMulti AutoplayStopDialog build error: ' + str(e), xbmc.LOGWARNING)

    def addControl(self, *args, **kwargs):
        # Entrada estilo cartoon: pop rapido con rebote (pasa de 100 y se
        # asienta = efecto "pum"). Animacion nativa de Kodi; DEBE ir DESPUES
        # de addControl o Kodi la ignora. Si falla, el dialogo sale normal.
        res = super().addControl(*args, **kwargs)
        try:
            ctrl = args[0] if args else None
            if ctrl is not None:
                try:
                    ctrl.setAnimations([('WindowOpen', 'effect=zoom start=50,50 end=100,100 center=auto time=220 tween=back easing=out')])
                except Exception:
                    pass
        except Exception:
            pass
        return res

    _WHITE_PNG_CACHE = None

    @staticmethod
    def _bg_texture():
        if _AutoplayStopDialog._WHITE_PNG_CACHE and os.path.exists(_AutoplayStopDialog._WHITE_PNG_CACHE):
            return _AutoplayStopDialog._WHITE_PNG_CACHE
        try:
            d = BRIDGE_DATA_PATH
            if not os.path.exists(d):
                try: os.makedirs(d)
                except: pass
            _tmp = os.path.join(d, 'bm_white.png')
            if not os.path.exists(_tmp):
                _png = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
                        b'\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xff\xff'
                        b'\x3f\x00\x05\xfe\x02\xfe\xdc\xccY\xe7\x00\x00\x00\x00IEND\xaeB`\x82')
                with open(_tmp, 'wb') as _f: _f.write(_png)
            if os.path.exists(_tmp):
                _AutoplayStopDialog._WHITE_PNG_CACHE = _tmp
                return _tmp
            xbmc.log('BridgeMulti AutoplayStopDialog: sin textura bg', xbmc.LOGWARNING)
        except Exception as e:
            xbmc.log('BridgeMulti AutoplayStopDialog bg error: ' + str(e), xbmc.LOGWARNING)
        return ''

    def _build(self):
        W = self.getWidth()
        H = self.getHeight()

        # ----- dialogo compacto -----
        dw, dh = 620, 250
        dx = (W - dw) // 2
        dy = (H - dh) // 2
        border = 4
        GOLD = '0xFFCFA82C'
        white = self._bg_texture()
        xbmc.log('BridgeMulti AutoplayStopDialog: bg=%s' % ('ok' if white else 'FALLO'), xbmc.LOGINFO)

        if white:
            # Borde dorado exterior
            self.addControl(xbmcgui.ControlImage(
                dx, dy, dw, dh, white, colorDiffuse='0xFFCFA82C'))
            # Fondo oscuro interior
            self.addControl(xbmcgui.ControlImage(
                dx+border, dy+border, dw-border*2, dh-border*2,
                white, colorDiffuse='0xFF000000'))

        # Titulo
        self.addControl(xbmcgui.ControlLabel(
            dx, dy+10, dw, 32,
            '[B]Multi Bridge — Autoplay[/B]',
            font='font13', textColor=GOLD, alignment=6))

        # Linea separadora dorada
        if white:
            self.addControl(xbmcgui.ControlImage(
                dx+30, dy+48, dw-60, 2, white, colorDiffuse='0x55CFA82C'))

        # Mensaje (2 lineas)
        self.addControl(xbmcgui.ControlLabel(
            dx+20, dy+60, dw-40, 30,
            '[B]%s[/B] se detuvo.' % self._server_label,
            font='font13', textColor='0xFFFFFFFF', alignment=6))
        self.addControl(xbmcgui.ControlLabel(
            dx+20, dy+92, dw-40, 30,
            '¿Reproducir siguiente enlace disponible?',
            font='font13', textColor='0xFFFFFFFF', alignment=6))

        # ----- fila de 3 botones -----
        widths  = (110, 110, 190)
        gap     = 24
        btn_h   = 44
        total_w = widths[0] + widths[1] + widths[2] + gap*2
        btn_y   = dy + dh - 44 - btn_h
        bx = dx + (dw - total_w) // 2

        for i, (label, bw) in enumerate(zip(self._LABELS, widths)):
            btn = xbmcgui.ControlButton(
                bx, btn_y, bw, btn_h, label, '', '',
                font='font13', textColor='0xFFFFFFFF', focusedColor=GOLD,
                alignment=6)
            self.addControl(btn)
            self._btn_ids.append(btn.getId())
            # Borde dorado tenue permanente + resaltado brillante con foco
            hl = []
            if white:
                m, t = 5, 2
                DIM = '0xFF8A6D1F'
                for _bx, _by, _bw, _bh in (
                        (bx-m, btn_y-m, bw+m*2, t),
                        (bx-m, btn_y+btn_h+m-t, bw+m*2, t),
                        (bx-m, btn_y-m, t, btn_h+m*2),
                        (bx+bw+m-t, btn_y-m, t, btn_h+m*2)):
                    _base = xbmcgui.ControlImage(_bx, _by, _bw, _bh, white, colorDiffuse=DIM)
                    self.addControl(_base)
                m, t = 5, 5
                hl = [
                    xbmcgui.ControlImage(bx-m, btn_y-m, bw+m*2, btn_h+m*2, white, colorDiffuse='0x44CFA82C'),
                    xbmcgui.ControlImage(bx-m, btn_y-m, bw+m*2, t, white, colorDiffuse=GOLD),
                    xbmcgui.ControlImage(bx-m, btn_y+btn_h+m-t, bw+m*2, t, white, colorDiffuse=GOLD),
                    xbmcgui.ControlImage(bx-m, btn_y-m, t, btn_h+m*2, white, colorDiffuse=GOLD),
                    xbmcgui.ControlImage(bx+bw+m-t, btn_y-m, t, btn_h+m*2, white, colorDiffuse=GOLD),
                ]
                for c in hl:
                    self.addControl(c)
            self._highlights.append(hl)
            bx += bw + gap

        self._update_highlight()
        try:
            self.setFocus(self.getControl(self._btn_ids[0]))
        except Exception:
            pass

    def _update_highlight(self):
        for i, hl in enumerate(self._highlights):
            for c in hl:
                try: c.setVisible(i == self.current)
                except: pass

    def _move(self, delta):
        self.current = (self.current + delta) % 3
        self._update_highlight()
        try:
            self.setFocus(self.getControl(self._btn_ids[self.current]))
        except Exception:
            pass

    def onAction(self, action):
        aid = action.getId()
        xbmc.log('BridgeMulti AutoplayStopDialog accion=%s' % aid, xbmc.LOGINFO)
        if aid in (self.ACTION_PREVIOUS_MENU, self.ACTION_NAV_BACK, self.ACTION_STOP, 13):
            self.selected = -1
            self.close()
        elif aid in (self.ACTION_MOVE_LEFT, self.ACTION_MOVE_UP):
            self._move(-1)
        elif aid in (self.ACTION_MOVE_RIGHT, self.ACTION_MOVE_DOWN):
            self._move(1)
        elif aid == self.ACTION_SELECT_ITEM:
            self.selected = self.current
            self.close()
        elif aid in (self.ACTION_MOUSE_LEFT_CLICK, self.ACTION_MOUSE_DOUBLE_CLICK):
            # En Kodi el clic de raton/tactil llega como accion (onClick no se
            # dispara en esta ventana): se elige el boton que tiene el foco,
            # que Kodi pone bajo el cursor al hacer clic.
            try:
                xbmc.sleep(120)
            except: pass
            try:
                fid = self.getFocusId()
            except Exception:
                fid = -1
            for i, bid in enumerate(self._btn_ids):
                if fid == bid:
                    self.selected = i
                    self.close()
                    return

    def onClick(self, control_id):
        xbmc.log('BridgeMulti AutoplayStopDialog click=%s' % control_id, xbmc.LOGINFO)
        # Acepta clics en botones Y en sus resaltados (las imagenes doradas
        # quedan encima y en tactil el toque cae sobre ellas, no el boton).
        for i, bid in enumerate(self._btn_ids):
            if control_id == bid:
                self.selected = i
                self.close()
                return
        for i, hl in enumerate(self._highlights):
            for c in hl:
                try:
                    if control_id == c.getId():
                        self.selected = i
                        self.close()
                        return
                except: pass


def _show_autoplay_stop_dialog(server_label):
    """Muestra la ventana compacta (Si/No/Ver enlaces). Devuelve 0/1/2 o -1."""
    try:
        dlg = _AutoplayStopDialog(server_label)
        dlg.doModal()
        sel = dlg.selected
        del dlg
        if sel is None:
            return -1
        return sel
    except Exception as e:
        xbmc.log('BridgeMulti _show_autoplay_stop_dialog error, fallback a select: ' + str(e), xbmc.LOGWARNING)
        try:
            return xbmcgui.Dialog().select(
                'Multi Bridge — Autoplay: [B]%s[/B] se detuvo' % (server_label or ''),
                ['Sí, siguiente enlace', 'No, volver', 'Ver enlaces'])
        except Exception:
            return -1


# ---------------------------------------------------------
# Custom Pause Prompt Dialog & Floating Links Dialog
# ---------------------------------------------------------

class _PausePromptDialog(xbmcgui.WindowDialog):
    ACTION_MOVE_LEFT   = 1
    ACTION_MOVE_RIGHT  = 2
    ACTION_SELECT_ITEM = 7
    ACTION_PREVIOUS_MENU = 10
    ACTION_PAUSE       = 12
    ACTION_STOP        = 13
    ACTION_PLAY        = 68
    ACTION_PLAYER_PLAY = 79
    ACTION_NAV_BACK    = 92
    ACTION_MOUSE_LEFT_CLICK = 100
    ACTION_MOUSE_MOVE       = 107
    ACTION_BACKSPACE   = 110

    def __init__(self, current_server_name="", meta=None):
        super().__init__()
        self._is_closed = False
        self.selected = -1  # 0=No (continuar), 1=Abrir enlaces, -1=Cerrar/Atrás
        self.current = 0    # 0=No (continuar) por defecto a la izquierda
        self.server_name = current_server_name or "Actual"
        self.meta = meta or {}
        self._btn_ids = []
        self._highlights = []
        try:
            self._build()
        except Exception as e:
            xbmc.log('BridgeMulti PausePromptDialog build error: ' + str(e), xbmc.LOGWARNING)
        self._start_playback_watchdog()

    def _start_playback_watchdog(self):
        def _watch():
            p = xbmc.Player()
            mon = xbmc.Monitor()
            while not getattr(self, '_is_closed', False) and not mon.abortRequested():
                if not p.isPlayingVideo():
                    xbmc.sleep(150)
                    if not p.isPlayingVideo() and not getattr(self, '_is_closed', False):
                        self.selected = -1
                        try:
                            self.close()
                        except Exception:
                            pass
                        break
                if mon.waitForAbort(0.2):
                    break
        t = threading.Thread(target=_watch, daemon=True)
        t.name = "PausePromptWatchdog"
        t.start()

    def _white(self):
        return _AutoplayStopDialog._bg_texture() or _EnginePickerDialog._white()

    def _build(self):
        W = self.getWidth() or 1920
        H = self.getHeight() or 1080
        dw, dh = 580, 240
        dx = (W - dw) // 2
        dy = (H - dh) // 2
        border = 3
        GOLD = '0xFFCFA82C'
        white = self._white()

        # Borde dorado exterior
        self.addControl(xbmcgui.ControlImage(dx, dy, dw, dh, white, colorDiffuse=GOLD))
        # Fondo oscuro interior
        self.addControl(xbmcgui.ControlImage(dx+border, dy+border, dw-border*2, dh-border*2, white, colorDiffuse='0xF20A0A0A'))

        # Título
        self.addControl(xbmcgui.ControlLabel(
            dx+24, dy+16, dw-48, 30,
            '[B][COLOR #CFA82C]MULTI BRIDGE[/COLOR][/B]  [COLOR #E0E0E0]—  Reproducción Pausada[/COLOR]',
            font='font13'))

        # Separador dorado
        self.addControl(xbmcgui.ControlImage(dx+20, dy+50, dw-40, 2, white, colorDiffuse='0x55CFA82C'))

        # Pregunta principal
        self.addControl(xbmcgui.ControlLabel(
            dx+24, dy+66, dw-48, 28,
            '[B][COLOR #FFFFFF]¿Deseas cambiar de enlace o servidor?[/COLOR][/B]',
            font='font13', alignment=6))

        # Detalle de servidor en reproducción
        srv_name_clean = self.server_name.strip()
        if srv_name_clean.lower() in ('torrent', 'magnet'):
            srv_str = '[COLOR #888888]En reproducción:[/COLOR] [B][COLOR cyan]TORRENT[/COLOR][/B]'
        else:
            srv_str = '[COLOR #888888]En reproducción:[/COLOR] [B][COLOR #CFA82C]%s[/COLOR][/B]' % srv_name_clean
        self.addControl(xbmcgui.ControlLabel(
            dx+24, dy+100, dw-48, 24, srv_str, font='font12', alignment=6))

        # Dos botones: [ No, continuar ]  [ Abrir enlaces ]
        bw, bh = 220, 42
        gap = 24
        total_w = bw * 2 + gap
        bx = dx + (dw - total_w) // 2
        by = dy + dh - 64

        labels = ('No, continuar', 'Abrir enlaces')
        for i, lbl in enumerate(labels):
            btn_x = bx + i * (bw + gap)
            btn = xbmcgui.ControlButton(
                btn_x, by, bw, bh, lbl, '', '',
                font='font13', textColor='0xFFFFFFFF', focusedColor=GOLD, alignment=6)
            self.addControl(btn)
            self._btn_ids.append(btn.getId())

            m, t = 4, 2
            # Marco dorado tenue permanente
            for _bx, _by, _bw, _bh in (
                    (btn_x-m, by-m, bw+m*2, t),
                    (btn_x-m, by+bh+m-t, bw+m*2, t),
                    (btn_x-m, by-m, t, bh+m*2),
                    (btn_x+bw+m-t, by-m, t, bh+m*2)):
                self.addControl(xbmcgui.ControlImage(_bx, _by, _bw, _bh, white, colorDiffuse='0xFF66551A'))

            # Resaltado brillante cuando tiene foco
            hl = [
                xbmcgui.ControlImage(btn_x-m, by-m, bw+m*2, bh+m*2, white, colorDiffuse='0x33CFA82C'),
                xbmcgui.ControlImage(btn_x-m, by-m, bw+m*2, t+1, white, colorDiffuse=GOLD),
                xbmcgui.ControlImage(btn_x-m, by+bh+m-t-1, bw+m*2, t+1, white, colorDiffuse=GOLD),
                xbmcgui.ControlImage(btn_x-m, by-m, t+1, bh+m*2, white, colorDiffuse=GOLD),
                xbmcgui.ControlImage(btn_x+bw+m-t-1, by-m, t+1, bh+m*2, white, colorDiffuse=GOLD),
            ]
            for c in hl: self.addControl(c)
            self._highlights.append(hl)

        # Ayuda en pie de página
        self.addControl(xbmcgui.ControlLabel(
            dx, dy+dh-22, dw, 18,
            '[COLOR=666666][← →] Elegir    [OK] Confirmar    [Atrás] Continuar[/COLOR]',
            font='font11', alignment=6))

        self._update_highlight()
        try:
            self.setFocus(self.getControl(self._btn_ids[0]))
        except Exception:
            pass

    def _update_highlight(self):
        for i, hl in enumerate(self._highlights):
            for c in hl:
                try: c.setVisible(i == self.current)
                except: pass

    def onAction(self, action):
        aid = action.getId()

        # Detección de movimiento de cursor (Hover)
        try:
            fid = self.getFocusId()
            if fid in self._btn_ids:
                idx = self._btn_ids.index(fid)
                if idx != self.current:
                    self.current = idx
                    self._update_highlight()
        except Exception:
            pass

        if aid in (self.ACTION_PREVIOUS_MENU, self.ACTION_NAV_BACK, self.ACTION_BACKSPACE, self.ACTION_PAUSE, self.ACTION_PLAYER_PLAY, self.ACTION_PLAY, self.ACTION_STOP, 13):
            if aid in (self.ACTION_STOP, 13):
                try:
                    if xbmc.Player().isPlaying():
                        xbmc.Player().stop()
                except Exception:
                    pass
            self.selected = -1
            self.close()
        elif aid == self.ACTION_MOVE_LEFT:
            self.current = 0
            self._update_highlight()
            try: self.setFocus(self.getControl(self._btn_ids[0]))
            except: pass
        elif aid == self.ACTION_MOVE_RIGHT:
            self.current = 1
            self._update_highlight()
            try: self.setFocus(self.getControl(self._btn_ids[1]))
            except: pass
        elif aid == self.ACTION_SELECT_ITEM:
            self.selected = self.current
            self.close()
        elif aid in (self.ACTION_MOUSE_LEFT_CLICK, 100, 103):
            try: fid = self.getFocusId()
            except: fid = -1
            if fid in self._btn_ids:
                self.selected = self._btn_ids.index(fid)
            else:
                self.selected = self.current
            self.close()

    def onClick(self, control_id):
        if control_id in self._btn_ids:
            self.selected = self._btn_ids.index(control_id)
        else:
            self.selected = self.current
        self.close()

    def close(self):
        if getattr(self, '_is_closed', False):
            return
        self._is_closed = True
        try:
            super().close()
        except Exception:
            pass


class _FloatingLinksDialog(xbmcgui.WindowDialog):
    ACTION_MOVE_LEFT          = 1
    ACTION_MOVE_RIGHT         = 2
    ACTION_MOVE_UP            = 3
    ACTION_MOVE_DOWN          = 4
    ACTION_PAGE_UP            = 5
    ACTION_PAGE_DOWN          = 6
    ACTION_SELECT_ITEM        = 7
    ACTION_PREVIOUS_MENU      = 10
    ACTION_SHOW_INFO          = 11
    ACTION_PAUSE              = 12
    ACTION_STOP               = 13
    ACTION_PLAY               = 68
    ACTION_PLAYER_PLAY        = 79
    ACTION_NAV_BACK           = 92
    ACTION_MOUSE_LEFT_CLICK   = 100
    ACTION_MOUSE_DOUBLE_CLICK = 103
    ACTION_MOUSE_WHEEL_UP     = 104
    ACTION_MOUSE_WHEEL_DOWN   = 105
    ACTION_MOUSE_MOVE         = 107
    ACTION_BACKSPACE          = 110
    ACTION_CONTEXT_MENU       = 117

    def __init__(self, links, current_index=0, meta=None, engine='alfa', is_playback=False, failed_links=None, initial_tab=None, offset=0):
        super().__init__()
        self._is_closed = False
        self.all_links = list(links or [])
        self.meta = meta or {}
        self.engine = engine or 'alfa'
        self.is_playback = is_playback
        self.selected = -1
        self.failed_links = set(failed_links or [])
        self.offset_count = offset

        # Separar en dos pestañas: 0 = Servidores Directos, 1 = Torrents
        self.direct_links = []
        self.torrent_links = []
        for l in self.all_links:
            srv = _get_link_server_name(l).strip().lower()
            if srv == 'torrent' or _is_torrent_link(l):
                self.torrent_links.append(l)
            else:
                self.direct_links.append(l)

        # Enlace en reproducción (si aplica)
        self.playing_item = None
        if self.is_playback and 0 <= current_index < len(self.all_links):
            self.playing_item = self.all_links[current_index]

        # Determinar pestaña activa inicial
        if initial_tab is not None and initial_tab in (0, 1):
            self.active_tab = initial_tab
        elif self.is_playback and self.playing_item is not None:
            if self.playing_item in self.torrent_links:
                self.active_tab = 1
            else:
                self.active_tab = 0
        elif len(self.direct_links) == 0 and len(self.torrent_links) > 0:
            self.active_tab = 1
        else:
            self.active_tab = 0

        # Posición del cursor y desplazamiento independientes por pestaña
        self.tab_current = {0: 0, 1: 0}
        self.tab_offset = {0: 0, 1: 0}

        # Enfocar enlace inicial en la pestaña activa
        if isinstance(current_index, int) and 0 <= current_index < len(self.all_links):
            target_link = self.all_links[current_index]
            if target_link in self.direct_links:
                d_idx = self.direct_links.index(target_link)
                self.tab_current[0] = d_idx
                if d_idx >= 7:
                    self.tab_offset[0] = min(d_idx - 7 + 1, max(0, len(self.direct_links) - 7))
            if target_link in self.torrent_links:
                t_idx = self.torrent_links.index(target_link)
                self.tab_current[1] = t_idx
                if t_idx >= 7:
                    self.tab_offset[1] = min(t_idx - 7 + 1, max(0, len(self.torrent_links) - 7))
        elif self.is_playback and self.playing_item is not None:
            if self.playing_item in self.direct_links:
                d_idx = self.direct_links.index(self.playing_item)
                self.tab_current[0] = d_idx
                if d_idx >= 7:
                    self.tab_offset[0] = min(d_idx - 7 + 1, max(0, len(self.direct_links) - 7))
            if self.playing_item in self.torrent_links:
                t_idx = self.torrent_links.index(self.playing_item)
                self.tab_current[1] = t_idx
                if t_idx >= 7:
                    self.tab_offset[1] = min(t_idx - 7 + 1, max(0, len(self.torrent_links) - 7))

        self.visible_rows = 7
        self._row_bgs = []
        self._row_btns = []
        self._row_btn_ids = []
        self._row_badges = []
        self._row_details = []
        self._row_channels = []
        self._hl_images = []
        self._counter_label = None
        self._empty_label = None
        self._scroll_controls = []
        self._up_btn = None
        self._down_btn = None
        self._scroll_btn_bgs = {}
        self._row_start_y = 0
        self._row_h = 48
        self._row_gap = 6
        self._rx = 0
        self._rw = 0
        try:
            self._build()
        except Exception as e:
            xbmc.log('BridgeMulti FloatingLinksDialog build error: ' + str(e), xbmc.LOGWARNING)
        if self.is_playback:
            self._start_playback_watchdog()

    def _start_playback_watchdog(self):
        def _watch():
            p = xbmc.Player()
            mon = xbmc.Monitor()
            while not getattr(self, '_is_closed', False) and not mon.abortRequested():
                if not p.isPlayingVideo():
                    xbmc.sleep(150)
                    if not p.isPlayingVideo() and not getattr(self, '_is_closed', False):
                        self.selected = -1
                        try:
                            self.close()
                        except Exception:
                            pass
                        break
                if mon.waitForAbort(0.2):
                    break
        t = threading.Thread(target=_watch, daemon=True)
        t.name = "FloatingLinksWatchdog"
        t.start()

    @property
    def active_links(self):
        return self.direct_links if self.active_tab == 0 else self.torrent_links

    @property
    def current(self):
        return self.tab_current.get(self.active_tab, 0)

    @current.setter
    def current(self, val):
        self.tab_current[self.active_tab] = val

    @property
    def offset(self):
        return self.tab_offset.get(self.active_tab, 0)

    @offset.setter
    def offset(self, val):
        self.tab_offset[self.active_tab] = val

    def addControl(self, *args, **kwargs):
        res = super().addControl(*args, **kwargs)
        try:
            ctrl = args[0] if args else None
            if ctrl is not None:
                try:
                    ctrl.setAnimations([
                        ('WindowOpen', 'effect=zoom start=85,85 end=100,100 center=auto time=180 tween=back easing=out'),
                        ('WindowClose', 'effect=fade start=100 end=0 time=100')
                    ])
                except Exception: pass
        except Exception: pass
        return res

    def _white(self):
        return _AutoplayStopDialog._bg_texture() or _EnginePickerDialog._white()

    def _format_link(self, lnk, is_playing=False, is_failed=False):
        srv = _get_link_server_name(lnk)
        lang = _format_language(lnk)
        qual = _format_quality(lnk)
        ch = _format_channel(lnk)
        weak = getattr(lnk, 'bridge_weak', False)
        is_tor = (srv.lower() == 'torrent' or _is_torrent_link(lnk))

        if is_failed:
            if is_tor:
                srv_clean = srv if srv.strip().lower() not in ('torrent', 'magnet', 'directo', 'unknown', '') else ''
                badge = ('[COLOR #888888]TORRENT[/COLOR]  [COLOR #777777]%s[/COLOR]' % srv_clean) if srv_clean else '[COLOR #888888]TORRENT[/COLOR]'
            else:
                badge = '[COLOR #777777]%s[/COLOR]' % (srv or 'Directo')

            mid_parts = []
            if lang: mid_parts.append('[COLOR #666666]%s[/COLOR]' % lang)
            if qual and qual != 'N/A': mid_parts.append('[COLOR #666666]%s[/COLOR]' % qual)
            mid = ' | '.join(mid_parts)

            right_parts = ['[B][COLOR red][FALLIDO][/COLOR][/B]']
            if ch: right_parts.append('[COLOR #555555](%s)[/COLOR]' % ch)
            right = '  '.join(right_parts)
            return badge, mid, right

        if is_tor:
            # Eliminar duplicado "TORRENT Torrent": si el servidor es "torrent" o vacío, sólo mostrar "TORRENT"
            if srv and srv.strip().lower() not in ('torrent', 'magnet', 'directo', 'unknown', ''):
                badge = '[B][COLOR cyan]TORRENT[/COLOR][/B]  [B][COLOR deepskyblue]%s[/COLOR][/B]' % srv
            else:
                badge = '[B][COLOR cyan]TORRENT[/COLOR][/B]'
        else:
            badge = '[B][COLOR deepskyblue]%s[/COLOR][/B]' % (srv or 'Directo')

        mid_parts = []
        if lang: mid_parts.append('[COLOR lime]%s[/COLOR]' % lang)
        if qual and qual != 'N/A': mid_parts.append('[COLOR gold]%s[/COLOR]' % qual)
        if weak: mid_parts.append('[COLOR orange][Dudoso][/COLOR]')
        mid = ' | '.join(mid_parts)

        right_parts = []
        if is_playing:
            # Texto verde sin icono con glifo roto
            right_parts.append('[B][COLOR lime]EN REPRODUCCIÓN[/COLOR][/B]')
        if ch:
            right_parts.append('[COLOR #888888](%s)[/COLOR]' % ch)
        right = '  '.join(right_parts)

        return badge, mid, right

    def _build(self):
        W = self.getWidth() or 1920
        H = self.getHeight() or 1080
        dw, dh = 980, 580
        dx = (W - dw) // 2
        dy = (H - dh) // 2
        white = self._white()
        GOLD = '0xFFCFA82C'
        BORDER = 3

        # Borde dorado exterior
        self.addControl(xbmcgui.ControlImage(dx, dy, dw, dh, white, colorDiffuse=GOLD))
        # Fondo oscuro interior translúcido
        self.addControl(xbmcgui.ControlImage(dx+BORDER, dy+BORDER, dw-BORDER*2, dh-BORDER*2, white, colorDiffuse='0xF20A0A0A'))

        # Título cabecera
        if self.is_playback:
            sub_head = '¿Deseas cambiar de servidor?'
        elif getattr(self, 'offset_count', 0) > 0:
            sub_head = 'Enlaces Adicionales (%d)' % len(self.all_links)
        else:
            sub_head = 'Servidores Disponibles'
        self.addControl(xbmcgui.ControlLabel(
            dx+28, dy+16, 620, 28,
            '[B][COLOR #CFA82C]MULTI BRIDGE[/COLOR][/B]  [COLOR #E0E0E0]—  %s[/COLOR]' % sub_head,
            font='font13'))

        # Línea separadora dorada superior
        self.addControl(xbmcgui.ControlImage(dx+20, dy+48, dw-40, 2, white, colorDiffuse='0x55CFA82C'))

        # Barra de Pestañas (Tabs)
        tab_y = dy + 58
        tab_h = 34
        tab0_w = 200
        tab0_x = dx + 28
        tab1_w = 200
        tab1_x = tab0_x + tab0_w + 12
        t = 2

        # Pestaña 0: Enlaces Directos
        self._tab0_bg = xbmcgui.ControlImage(tab0_x, tab_y, tab0_w, tab_h, white, colorDiffuse='0x44CFA82C')
        self.addControl(self._tab0_bg)
        self._tab0_borders = [
            xbmcgui.ControlImage(tab0_x, tab_y, tab0_w, t, white, colorDiffuse=GOLD),
            xbmcgui.ControlImage(tab0_x, tab_y + tab_h - t, tab0_w, t, white, colorDiffuse=GOLD),
            xbmcgui.ControlImage(tab0_x, tab_y, t, tab_h, white, colorDiffuse=GOLD),
            xbmcgui.ControlImage(tab0_x + tab0_w - t, tab_y, t, tab_h, white, colorDiffuse=GOLD),
        ]
        for b in self._tab0_borders: self.addControl(b)
        self._tab0_btn = xbmcgui.ControlButton(tab0_x, tab_y, tab0_w, tab_h, '', '', '', font='font12', alignment=6)
        self.addControl(self._tab0_btn)
        self._tab0_lbl = xbmcgui.ControlLabel(tab0_x, tab_y + 4, tab0_w, 24, '', font='font12', alignment=6)
        self.addControl(self._tab0_lbl)

        # Pestaña 1: Torrents
        self._tab1_bg = xbmcgui.ControlImage(tab1_x, tab_y, tab1_w, tab_h, white, colorDiffuse='0x15FFFFFF')
        self.addControl(self._tab1_bg)
        self._tab1_borders = [
            xbmcgui.ControlImage(tab1_x, tab_y, tab1_w, t, white, colorDiffuse='0x33CFA82C'),
            xbmcgui.ControlImage(tab1_x, tab_y + tab_h - t, tab1_w, t, white, colorDiffuse='0x33CFA82C'),
            xbmcgui.ControlImage(tab1_x, tab_y, t, tab_h, white, colorDiffuse='0x33CFA82C'),
            xbmcgui.ControlImage(tab1_x + tab1_w - t, tab_y, t, tab_h, white, colorDiffuse='0x33CFA82C'),
        ]
        for b in self._tab1_borders: self.addControl(b)
        self._tab1_btn = xbmcgui.ControlButton(tab1_x, tab_y, tab1_w, tab_h, '', '', '', font='font12', alignment=6)
        self.addControl(self._tab1_btn)
        self._tab1_lbl = xbmcgui.ControlLabel(tab1_x, tab_y + 4, tab1_w, 24, '', font='font12', alignment=6)
        self.addControl(self._tab1_lbl)

        # Botón de scroll arriba (único botón de navegación en la parte superior)
        btn_w, btn_h = 88, 34
        up_top_x = dx + dw - btn_w - 24
        up_top_y = tab_y

        # Botón superior ▲ con marco dorado y fondo táctil destacado
        up_top_bg = xbmcgui.ControlImage(up_top_x, up_top_y, btn_w, btn_h, white, colorDiffuse='0x33CFA82C')
        self.addControl(up_top_bg)
        self._scroll_controls.append(up_top_bg)
        for _bx, _by, _bw, _bh in (
                (up_top_x, up_top_y, btn_w, t),
                (up_top_x, up_top_y + btn_h - t, btn_w, t),
                (up_top_x, up_top_y, t, btn_h),
                (up_top_x + btn_w - t, up_top_y, t, btn_h)):
            _b = xbmcgui.ControlImage(_bx, _by, _bw, _bh, white, colorDiffuse=GOLD)
            self.addControl(_b)
            self._scroll_controls.append(_b)

        self._up_btn = xbmcgui.ControlButton(
            up_top_x, up_top_y, btn_w, btn_h, '[B]▲[/B]', '', '',
            font='font20_title', textColor=GOLD, focusedColor='0xFFFFFFFF', alignment=6)
        self.addControl(self._up_btn)
        self._scroll_controls.append(self._up_btn)
        self._scroll_btn_bgs[self._up_btn.getId()] = up_top_bg

        # Contador a la izquierda del botón superior
        self._counter_label = xbmcgui.ControlLabel(
            dx + dw - btn_w - 24 - 12 - 200, tab_y + 4, 200, 26, '', font='font13', alignment=1)
        self.addControl(self._counter_label)

        # Filas de la lista (espaciado de 14px idéntico arriba y abajo)
        self._rx = dx + 20
        self._rw = dw - 40
        self._row_start_y = dy + 106

        for i in range(self.visible_rows):
            ry = self._row_start_y + i * (self._row_h + self._row_gap)
            bg = xbmcgui.ControlImage(self._rx, ry, self._rw, self._row_h, white, colorDiffuse='0x18FFFFFF')
            self.addControl(bg)
            self._row_bgs.append(bg)

            badge_lbl = xbmcgui.ControlLabel(self._rx + 16, ry + 9, 220, 30, '', font='font13')
            self.addControl(badge_lbl)
            self._row_badges.append(badge_lbl)

            details_lbl = xbmcgui.ControlLabel(self._rx + 240, ry + 9, 360, 30, '', font='font13')
            self.addControl(details_lbl)
            self._row_details.append(details_lbl)

            ch_lbl = xbmcgui.ControlLabel(self._rx + 610, ry + 9, self._rw - 626, 30, '', font='font13', alignment=1)
            self.addControl(ch_lbl)
            self._row_channels.append(ch_lbl)

            # Botón interactivo transparente sobre toda la fila para soporte táctil, clic y hover con cursor
            btn = xbmcgui.ControlButton(
                self._rx, ry, self._rw, self._row_h, '', '', '',
                textColor='0x00000000', focusedColor='0x00000000')
            self.addControl(btn)
            self._row_btns.append(btn)
            self._row_btn_ids.append(btn.getId())

        # Mensaje si la pestaña está vacía
        self._empty_label = xbmcgui.ControlLabel(
            self._rx, self._row_start_y + 130, self._rw, 36,
            '[COLOR #888888]No se encontraron enlaces en esta categoría[/COLOR]',
            font='font13', alignment=6)
        self.addControl(self._empty_label)

        # Marco dorado de resaltado activo (4 bordes)
        th = 2
        self._hl_images = [
            xbmcgui.ControlImage(self._rx, self._row_start_y, self._rw, th, white, colorDiffuse=GOLD),
            xbmcgui.ControlImage(self._rx, self._row_start_y + self._row_h - th, self._rw, th, white, colorDiffuse=GOLD),
            xbmcgui.ControlImage(self._rx, self._row_start_y, th, self._row_h, white, colorDiffuse=GOLD),
            xbmcgui.ControlImage(self._rx + self._rw - th, self._row_start_y, th, self._row_h, white, colorDiffuse=GOLD),
        ]
        for hl in self._hl_images:
            self.addControl(hl)

        # Botón inferior ▼ con el mismo espaciado de 14px respecto a la última fila
        nav_y = dy + 492
        down_x = dx + dw - btn_w - 24

        down_bg = xbmcgui.ControlImage(down_x, nav_y, btn_w, btn_h, white, colorDiffuse='0x33CFA82C')
        self.addControl(down_bg)
        self._scroll_controls.append(down_bg)
        for _bx, _by, _bw, _bh in (
                (down_x, nav_y, btn_w, t),
                (down_x, nav_y + btn_h - t, btn_w, t),
                (down_x, nav_y, t, btn_h),
                (down_x + btn_w - t, nav_y, t, btn_h)):
            _b = xbmcgui.ControlImage(_bx, _by, _bw, _bh, white, colorDiffuse=GOLD)
            self.addControl(_b)
            self._scroll_controls.append(_b)

        self._down_btn = xbmcgui.ControlButton(
            down_x, nav_y, btn_w, btn_h, '[B]▼[/B]', '', '',
            font='font20_title', textColor=GOLD, focusedColor='0xFFFFFFFF', alignment=6)
        self.addControl(self._down_btn)
        self._scroll_controls.append(self._down_btn)
        self._scroll_btn_bgs[self._down_btn.getId()] = down_bg

        # Título de la película o serie/episodio en la barra inferior izquierda (idéntico en gris)
        s_val = self.meta.get('season')
        e_val = self.meta.get('episode')
        year_val = self.meta.get('year') or self.meta.get('showyear')
        show_name = _safe_str(self.meta.get('showname') or self.meta.get('tvshowtitle') or '')
        ep_title = _safe_str(self.meta.get('title') or 'Vídeo')
        if s_val and e_val:
            bottom_media_title = (show_name or ep_title)
            if year_val: bottom_media_title += ' (%s)' % year_val
            bottom_media_title += ' [COLOR gold]T%sxE%s[/COLOR]' % (s_val, e_val)
        else:
            bottom_media_title = ep_title
            if year_val: bottom_media_title += ' (%s)' % year_val

        self._bottom_media_label = xbmcgui.ControlLabel(
            dx + 28, nav_y + 6, dw - btn_w - 76, 22,
            bottom_media_title, font='font12', textColor='0xFFAAAAAA')
        self.addControl(self._bottom_media_label)

        # Separador inferior
        self.addControl(xbmcgui.ControlImage(dx+20, dy+538, dw-40, 1, white, colorDiffuse='0x44CFA82C'))

        # Ayuda en pie de página
        other_name = 'Balandro' if self.engine == 'alfa' else 'Alfa'
        if self.is_playback:
            hint_txt = '[COLOR=888888][← →] Pestaña    [↑ ↓] Navegar    [OK / Clic] Servidor    [I] %s    [Atrás] Continuar[/COLOR]' % other_name
        else:
            hint_txt = '[COLOR=888888][← →] Pestaña    [↑ ↓] Navegar    [OK / Clic] Reproducir    [I] %s    [Atrás] Salir[/COLOR]' % other_name
        self.addControl(xbmcgui.ControlLabel(
            dx+20, dy+546, dw-40, 22, hint_txt, font='font12', alignment=6))

        self._update_tab_headers()
        self._update_view()

    def _switch_tab(self, new_tab):
        if new_tab == self.active_tab:
            return
        self.active_tab = new_tab
        self._update_tab_headers()
        self._update_view()

    def _update_tab_headers(self):
        GOLD = '0xFFCFA82C'
        DIM_BORDER = '0x33CFA82C'
        BG_ACTIVE = '0x44CFA82C'
        BG_INACTIVE = '0x15FFFFFF'

        d_count = len(self.direct_links)
        t_count = len(self.torrent_links)

        if self.active_tab == 0:
            self._tab0_bg.setColorDiffuse(BG_ACTIVE)
            for b in self._tab0_borders: b.setColorDiffuse(GOLD)
            self._tab0_lbl.setLabel('[B][COLOR #CFA82C]DIRECTOS (%d)[/COLOR][/B]' % d_count)

            self._tab1_bg.setColorDiffuse(BG_INACTIVE)
            for b in self._tab1_borders: b.setColorDiffuse(DIM_BORDER)
            self._tab1_lbl.setLabel('[COLOR #888888]TORRENTS (%d)[/COLOR]' % t_count)
        else:
            self._tab0_bg.setColorDiffuse(BG_INACTIVE)
            for b in self._tab0_borders: b.setColorDiffuse(DIM_BORDER)
            self._tab0_lbl.setLabel('[COLOR #888888]DIRECTOS (%d)[/COLOR]' % d_count)

            self._tab1_bg.setColorDiffuse(BG_ACTIVE)
            for b in self._tab1_borders: b.setColorDiffuse(GOLD)
            self._tab1_lbl.setLabel('[B][COLOR #CFA82C]TORRENTS (%d)[/COLOR][/B]' % t_count)

    def _move(self, delta):
        links = self.active_links
        if not links: return
        n = len(links)
        new_idx = self.current + delta
        if new_idx >= n:
            if delta == 1:
                new_idx = 0
            else:
                new_idx = min(new_idx, n - 1) if self.current < n - 1 else 0
        elif new_idx < 0:
            if delta == -1:
                new_idx = n - 1
            else:
                new_idx = 0 if self.current > 0 else n - 1
        self.current = new_idx
        self._update_view()

    def _update_highlight_and_bgs(self):
        links = self.active_links
        n = len(links)
        if n == 0:
            for hl in self._hl_images: hl.setVisible(False)
            return

        slot = self.current - self.offset
        if 0 <= slot < self.visible_rows:
            active_y = self._row_start_y + slot * (self._row_h + self._row_gap)
            th = 2
            self._hl_images[0].setPosition(self._rx, active_y)
            self._hl_images[1].setPosition(self._rx, active_y + self._row_h - th)
            self._hl_images[2].setPosition(self._rx, active_y)
            self._hl_images[3].setPosition(self._rx + self._rw - th, active_y)
            for hl in self._hl_images: hl.setVisible(True)

            for i in range(self.visible_rows):
                item_idx = self.offset + i
                if item_idx < n:
                    lnk = links[item_idx]
                    is_failed = (lnk in self.failed_links)
                    if i == slot:
                        bg_c = '0x44AA3333' if is_failed else '0x33CFA82C'
                    else:
                        bg_c = '0x20551111' if is_failed else '0x18FFFFFF'
                    self._row_bgs[i].setColorDiffuse(bg_c)

            self._counter_label.setLabel('[B][COLOR #CFA82C]%d[/COLOR] [COLOR #888888]/ %d[/COLOR][/B]' % (self.current + 1, n))

    def _update_view(self):
        links = self.active_links
        n = len(links)
        if n == 0:
            self._counter_label.setLabel('[COLOR #888888]0 / 0[/COLOR]')
            self._empty_label.setVisible(True)
            for c in self._scroll_controls:
                try: c.setVisible(False)
                except: pass
            for hl in self._hl_images: hl.setVisible(False)
            for i in range(self.visible_rows):
                self._row_badges[i].setVisible(False)
                self._row_details[i].setVisible(False)
                self._row_channels[i].setVisible(False)
                self._row_bgs[i].setVisible(False)
                if i < len(self._row_btns): self._row_btns[i].setVisible(False)
            return

        self._empty_label.setVisible(False)

        if self.current < self.offset:
            self.offset = self.current
        elif self.current >= self.offset + self.visible_rows:
            self.offset = self.current - self.visible_rows + 1

        self._counter_label.setLabel('[B][COLOR #CFA82C]%d[/COLOR] [COLOR #888888]/ %d[/COLOR][/B]' % (self.current + 1, n))

        # Visibilidad de controles de desplazamiento táctil: visibles si hay más enlaces que filas en pantalla
        has_scroll = (n > self.visible_rows)
        for c in self._scroll_controls:
            try: c.setVisible(has_scroll)
            except: pass

        slot = self.current - self.offset
        for i in range(self.visible_rows):
            item_idx = self.offset + i
            if item_idx < n:
                lnk = links[item_idx]
                is_curr_playing = bool(self.is_playback and self.playing_item is not None and lnk is self.playing_item)
                is_failed = (lnk in self.failed_links)
                badge, mid, right = self._format_link(lnk, is_playing=is_curr_playing, is_failed=is_failed)
                self._row_badges[i].setLabel(badge)
                self._row_details[i].setLabel(mid)
                self._row_channels[i].setLabel(right)
                self._row_badges[i].setVisible(True)
                self._row_details[i].setVisible(True)
                self._row_channels[i].setVisible(True)
                self._row_bgs[i].setVisible(True)
                if i == slot:
                    bg_c = '0x44AA3333' if is_failed else '0x33CFA82C'
                else:
                    bg_c = '0x20551111' if is_failed else '0x18FFFFFF'
                self._row_bgs[i].setColorDiffuse(bg_c)
                if i < len(self._row_btns): self._row_btns[i].setVisible(True)
            else:
                self._row_badges[i].setVisible(False)
                self._row_details[i].setVisible(False)
                self._row_channels[i].setVisible(False)
                self._row_bgs[i].setVisible(False)
                if i < len(self._row_btns): self._row_btns[i].setVisible(False)

        active_y = self._row_start_y + slot * (self._row_h + self._row_gap)
        th = 2
        self._hl_images[0].setPosition(self._rx, active_y)
        self._hl_images[1].setPosition(self._rx, active_y + self._row_h - th)
        self._hl_images[2].setPosition(self._rx, active_y)
        self._hl_images[3].setPosition(self._rx + self._rw - th, active_y)
        for hl in self._hl_images: hl.setVisible(True)

        # Sincronizar foco nativo en el botón interactivo de la fila activa
        if hasattr(self, '_row_btns') and 0 <= slot < len(self._row_btns) and slot < n - self.offset:
            try:
                self.setFocus(self._row_btns[slot])
            except Exception:
                pass

    def onAction(self, action):
        aid = action.getId()

        # Detección instantánea de cursor sobre un enlace o botón (Hover / Focus)
        try:
            fid = self.getFocusId()
            if hasattr(self, '_row_btn_ids') and fid in self._row_btn_ids:
                row_idx = self._row_btn_ids.index(fid)
                hovered_idx = self.offset + row_idx
                if hovered_idx < len(self.active_links) and hovered_idx != self.current:
                    self.current = hovered_idx
                    self._update_highlight_and_bgs()

            if hasattr(self, '_scroll_btn_bgs'):
                for bid, bg_c in self._scroll_btn_bgs.items():
                    try:
                        bg_c.setColorDiffuse('0x77CFA82C' if bid == fid else '0x33CFA82C')
                    except Exception:
                        pass
        except Exception:
            pass

        if aid in (self.ACTION_PREVIOUS_MENU, self.ACTION_NAV_BACK, self.ACTION_BACKSPACE, self.ACTION_PAUSE, self.ACTION_PLAYER_PLAY, self.ACTION_PLAY, self.ACTION_STOP, 13):
            if aid in (self.ACTION_STOP, 13) and self.is_playback:
                try:
                    if xbmc.Player().isPlaying():
                        xbmc.Player().stop()
                except Exception:
                    pass
            self.selected = -1
            self.close()
        elif aid == self.ACTION_MOVE_LEFT:
            self._switch_tab(0)
        elif aid == self.ACTION_MOVE_RIGHT:
            self._switch_tab(1)
        elif aid == self.ACTION_MOVE_UP:
            self._move(-1)
        elif aid == self.ACTION_MOVE_DOWN:
            self._move(1)
        elif aid == self.ACTION_PAGE_UP:
            self._move(-self.visible_rows)
        elif aid == self.ACTION_PAGE_DOWN:
            self._move(self.visible_rows)
        elif aid == self.ACTION_SELECT_ITEM:
            if self.active_links and 0 <= self.current < len(self.active_links):
                chosen = self.active_links[self.current]
                try:
                    self.selected = self.all_links.index(chosen)
                except ValueError:
                    self.selected = 0
            else:
                self.selected = -1
            self.close()
        elif aid in (self.ACTION_SHOW_INFO, 11, self.ACTION_CONTEXT_MENU, 117):
            other_eng_name = 'Balandro' if self.engine == 'alfa' else 'Alfa'
            try:
                ans = _KODI_ORIG_DIALOG().yesno(
                    'Multi Bridge',
                    '¿Deseas buscar enlaces en [B]%s[/B]?' % other_eng_name,
                    nolabel='Cancelar',
                    yeslabel='Buscar en %s' % other_eng_name
                )
                if ans:
                    self.selected = 'other_engine'
                    self.close()
            except Exception:
                pass
        elif aid in (self.ACTION_MOUSE_WHEEL_UP, 104):
            self._move(-1)
        elif aid in (self.ACTION_MOUSE_WHEEL_DOWN, 105):
            self._move(1)
        elif aid in (self.ACTION_MOUSE_LEFT_CLICK, self.ACTION_MOUSE_DOUBLE_CLICK, 100, 103):
            try: fid = self.getFocusId()
            except: fid = -1
            if hasattr(self, '_tab0_btn') and fid == self._tab0_btn.getId():
                self._switch_tab(0); return
            if hasattr(self, '_tab1_btn') and fid == self._tab1_btn.getId():
                self._switch_tab(1); return
            if hasattr(self, '_up_btn') and fid == self._up_btn.getId():
                self._move(-1); return
            if hasattr(self, '_down_btn') and fid == self._down_btn.getId():
                self._move(1); return
            if hasattr(self, '_row_btn_ids') and fid in self._row_btn_ids:
                row_idx = self._row_btn_ids.index(fid)
                item_idx = self.offset + row_idx
                if item_idx < len(self.active_links):
                    chosen = self.active_links[item_idx]
                    try: self.selected = self.all_links.index(chosen)
                    except ValueError: self.selected = 0
                    self.close()
                    return
            if self.active_links and 0 <= self.current < len(self.active_links):
                chosen = self.active_links[self.current]
                try: self.selected = self.all_links.index(chosen)
                except ValueError: self.selected = 0
            else:
                self.selected = -1
            self.close()

    def onClick(self, control_id):
        if hasattr(self, '_tab0_btn') and control_id == self._tab0_btn.getId():
            self._switch_tab(0)
            return
        if hasattr(self, '_tab1_btn') and control_id == self._tab1_btn.getId():
            self._switch_tab(1)
            return
        if hasattr(self, '_up_btn') and control_id == self._up_btn.getId():
            self._move(-1)
            return
        if hasattr(self, '_down_btn') and control_id == self._down_btn.getId():
            self._move(1)
            return
        if hasattr(self, '_row_btn_ids') and control_id in self._row_btn_ids:
            row_idx = self._row_btn_ids.index(control_id)
            item_idx = self.offset + row_idx
            if item_idx < len(self.active_links):
                chosen = self.active_links[item_idx]
                try:
                    self.selected = self.all_links.index(chosen)
                except ValueError:
                    self.selected = 0
                self.close()
                return
        if self.active_links and 0 <= self.current < len(self.active_links):
            chosen = self.active_links[self.current]
            try:
                self.selected = self.all_links.index(chosen)
            except ValueError:
                self.selected = 0
        else:
            self.selected = -1
        self.close()

    def close(self):
        if getattr(self, '_is_closed', False):
            return
        self._is_closed = True
        try:
            super().close()
        except Exception:
            pass


def _load_cached_links_for_dialog(offset=0):
    # 1. RAM Cache instantáneo (0 ms, sin acceso a disco ni deserialización)
    try:
        ram = _get_ram_search_cache()
        if ram and ram.get('links'):
            c_links = list(ram['links'])
            c_matched = ram.get('matched_item')
            c_meta = ram.get('meta', {}) or {}
            c_engine = ram.get('engine', 'alfa') or 'alfa'
            valid_links = [l for l in c_links if len(_safe_str(getattr(l, 'url', ''))) <= 1500]
            if offset > 0 and offset < len(valid_links):
                valid_links = valid_links[offset:]
            xbmc.log("Multi Bridge: _load_cached_links_for_dialog RAM HIT -> %d enlaces (offset=%d)" % (len(valid_links), offset), xbmc.LOGINFO)
            return valid_links, c_matched, c_meta, c_engine
    except Exception as _re:
        xbmc.log("Multi Bridge: _load_cached_links_for_dialog ram check error: %s" % _re, xbmc.LOGDEBUG)

    # 2. Fallback a archivo de caché en disco
    cache = _read_search_cache_atomic()
    if not cache or not isinstance(cache, dict):
        return [], None, {}, 'alfa'
    try:
        c_engine = cache.get('engine', 'alfa') or 'alfa'
        c_meta = cache.get('meta', {}) or {}

        raw_links = cache.get('links', []) or []
        raw_valid = [l for l in raw_links if len(_safe_str((l or {}).get('url', ''))) <= 1500]
        if offset > 0 and offset < len(raw_valid):
            raw_valid = raw_valid[offset:]

        _d_mods = _get_alfa_modules() if c_engine == 'alfa' else _get_balandro_modules()
        if _d_mods:
            _Item, _Info = _d_mods['Item'], _d_mods['InfoLabels']
            c_links = [_deserialize_item_fast(_Item, _Info, l) for l in raw_valid]
            c_matched = _deserialize_item_fast(_Item, _Info, cache.get('item'))
        else:
            c_links = [_deserialize_item(l, c_engine) for l in raw_valid]
            c_matched = _deserialize_item(cache.get('item'), c_engine)

        return c_links, c_matched, c_meta, c_engine
    except Exception as e:
        xbmc.log('Multi Bridge: _load_cached_links_for_dialog error: ' + str(e), xbmc.LOGINFO)
        return [], None, {}, 'alfa'


def _switch_to_link(chosen_idx, links, meta, matched_item=None, engine='alfa', force_cur_time=0.0):
    if not links or chosen_idx < 0 or chosen_idx >= len(links):
        return False
    chosen = links[chosen_idx]

    p = xbmc.Player()
    cur_time = 0.0
    total_time = 0.0
    try:
        if p.isPlayingVideo():
            cur_time = float(p.getTime())
            total_time = float(p.getTotalTime())
    except Exception:
        pass
    if cur_time <= 0.0 and force_cur_time > 0.0:
        cur_time = float(force_cur_time)

    srv_name = _get_link_server_name(chosen)
    xbmc.log("Multi Bridge: cambio en caliente a enlace %d/%d (%s) en %.1fs" % (
        chosen_idx + 1, len(links), srv_name, cur_time), xbmc.LOGINFO)

    media_key = _get_media_key(meta, chosen)
    if cur_time > 2:
        try:
            save_bookmark(media_key, cur_time, total_time, title=meta.get('title', ''))
        except Exception:
            pass

    try:
        if p.isPlaying():
            p.stop()
    except Exception:
        pass

    xbmc.sleep(300)

    # Preservar metadatos completos para TMDb Helper, Trakt y Kodi OSD
    _enrich_link_metadata(chosen, meta, matched_item)
    sync_tmdbhelper_playerstring(meta)

    try:
        xbmcgui.Dialog().notification('Multi Bridge', 'Cambiando a [B]%s[/B]...' % srv_name, '', 2500)
    except Exception:
        pass

    eng = getattr(chosen, 'bridge_engine', '') or engine or 'alfa'
    played = _play_link_safely(chosen, engine=eng, matched_item=matched_item, meta=meta)
    if played:
        start_playback_monitor(media_key, title_str=meta.get('title', ''), seek_to_time=cur_time, current_link_index=chosen_idx, meta=meta)
        return True
    return False


def _play_link_from_dialog(chosen_idx, links, meta, matched_item=None, engine='alfa', global_idx=None):
    if not links or chosen_idx < 0 or chosen_idx >= len(links):
        return False
    chosen = links[chosen_idx]
    _enrich_link_metadata(chosen, meta, matched_item)
    sync_tmdbhelper_playerstring(meta)

    media_key = _get_media_key(meta, chosen)
    is_s = bool(meta.get('season') and meta.get('episode'))
    bm = get_bookmark(media_key, tmdb_id=meta.get('tmdb'), is_series=is_s, season=meta.get('season'), episode=meta.get('episode'))
    seek_to_time = 0
    if bm:
        r_time = float(bm.get('resume_time', 0))
        if r_time > 10:
            mins = int(r_time // 60); secs = int(r_time % 60); hrs = mins // 60; mins = mins % 60
            time_str = ('%d:%02d:%02d' % (hrs, mins, secs)) if hrs else ('%d:%02d' % (mins, secs))
            dialog = (_KODI_ORIG_DIALOG or xbmcgui.Dialog)()
            _rsel = dialog.select(
                'Reanudar reproducción',
                ['Reanudar desde %s' % time_str, 'Desde el principio'])
            if _rsel == 0:
                seek_to_time = r_time
            elif _rsel is None or _rsel < 0:
                return 'cancel'

    eng = getattr(chosen, 'bridge_engine', '') or engine or 'alfa'
    played = _play_link_safely(chosen, engine=eng, matched_item=matched_item, meta=meta)
    if played:
        mon_idx = global_idx if global_idx is not None else chosen_idx
        start_playback_monitor(media_key, title_str=meta.get('title', ''), seek_to_time=seek_to_time, current_link_index=mon_idx, meta=meta)
        return True
    return False


def _verify_playback_started(is_torrent=False, timeout=None):
    p = xbmc.Player()
    mon = xbmc.Monitor()
    if timeout is None:
        timeout = 35.0 if is_torrent else 22.0
    start_t = time.time()

    while (time.time() - start_t) < timeout:
        if mon.abortRequested():
            return False

        # Vídeo ya en reproducción real activa: éxito inmediato
        if p.isPlayingVideo():
            return True

        # Rastrear indicadores de que Kodi está activamente ocupado o cargando el stream
        is_buffering = bool(xbmc.getCondVisibility("Player.Buffering"))
        is_busy = bool(xbmc.getCondVisibility(
            "Window.IsVisible(busydialog) | Window.IsVisible(busydialognocancel) | Window.IsVisible(progressdialog)"
        ))
        has_media = bool(xbmc.getCondVisibility("Player.HasMedia") or xbmc.getCondVisibility("Player.HasAudio") or xbmc.getCondVisibility("Player.HasVideo"))
        is_playing = bool(p.isPlaying())
        try:
            has_playlist = (xbmc.PlayList(xbmc.PLAYLIST_VIDEO).size() > 0)
        except Exception:
            has_playlist = False

        # Si Kodi está en búfer o mostrando la rueda de carga/progreso, continuar esperando
        if is_buffering or is_busy:
            if mon.waitForAbort(0.25):
                return False
            continue

        # Solo abortar anticipadamente si ya pasó un tiempo mínimo prudencial (10s en directo, 15s en torrent)
        # y Kodi ya no tiene nada en cola, ni en reproducción, ni ocupado
        elapsed = time.time() - start_t
        min_wait = 15.0 if is_torrent else 10.0
        if elapsed > min_wait:
            if not is_buffering and not is_busy and not has_media and not is_playing and not has_playlist:
                xbmc.log("Multi Bridge: _verify_playback_started detecta fin de intento tras %.1fs (sin actividad ni medio)" % elapsed, xbmc.LOGINFO)
                return False

        if mon.waitForAbort(0.25):
            return False

    return bool(p.isPlayingVideo())


def show_floating_links_dialog(offset=0):
    global _search_in_progress, _links_view_active
    _search_in_progress = False
    _links_view_active = True
    _apply_silence_all()
    if _is_dialog_active_global():
        xbmc.log("Multi Bridge: show_floating_links_dialog omitido porque ya hay un diálogo activo", xbmc.LOGINFO)
        return False
    _set_dialog_active_global(True)
    try:
        return _show_floating_links_dialog_impl(offset=offset)
    finally:
        _set_dialog_active_global(False)
        _links_view_active = False
        _restore_silence_all()

def _show_floating_links_dialog_impl(offset=0):
    p = xbmc.Player()
    is_playing = False
    cur_playback_time = 0.0
    try:
        is_playing = p.isPlayingVideo()
        if is_playing:
            cur_playback_time = float(p.getTime())
    except Exception:
        pass

    if is_playing and not xbmc.getCondVisibility("Player.Paused"):
        try: p.pause()
        except: pass

    c_links, c_matched, c_meta, c_eng = _load_cached_links_for_dialog(offset=offset)
    if not c_links:
        _show_bridge_notification('Multi Bridge', 'No hay enlaces disponibles', 3000)
        return False

    failed_links = set()
    failed_indices = set()
    last_tab = None
    next_suggested_idx = _current_playing_link_index if (is_playing and 0 <= _current_playing_link_index < len(c_links)) else 0
    was_playback = is_playing

    # Ajuste on_link_fail: 0 = Reabrir ventana con enlace marcado (Recomendado), 1 = Desactivado
    on_fail_setting = str(_bridge_addon.getSetting('on_link_fail') or '0').strip()

    while True:
        # Si todos los enlaces disponibles han fallado, notificar y salir
        if len(failed_indices) >= len(c_links) and len(c_links) > 0:
            _show_bridge_notification('Multi Bridge', 'Todos los enlaces probados han fallado', 3500)
            if was_playback and xbmc.getCondVisibility("Player.Paused") and p.isPlayingVideo():
                try: p.pause()
                except: pass
            return False

        dlg = _FloatingLinksDialog(
            c_links,
            current_index=next_suggested_idx,
            meta=c_meta,
            engine=c_eng,
            is_playback=was_playback,
            failed_links=failed_links,
            initial_tab=last_tab,
            offset=offset
        )
        dlg.doModal()
        chosen_idx = dlg.selected
        last_tab = dlg.active_tab
        del dlg

        if chosen_idx == 'other_engine':
            _other_engine = 'balandro' if c_eng == 'alfa' else 'alfa'
            xbmc.executebuiltin('RunPlugin(plugin://plugin.video.bridge.multi/?action=search_other_engine&engine=%s)' % _other_engine)
            return True

        if not (isinstance(chosen_idx, int) and chosen_idx >= 0):
            # El usuario canceló o presionó Atrás
            if was_playback and xbmc.getCondVisibility("Player.Paused") and p.isPlayingVideo():
                try: p.pause()
                except: pass
            return False

        # Intentar reproducir el enlace seleccionado
        chosen_link = c_links[chosen_idx]
        srv_name = _get_link_server_name(chosen_link) or 'Servidor'
        is_tor = (srv_name.lower() == 'torrent' or _is_torrent_link(chosen_link))

        if was_playback:
            played = _switch_to_link(chosen_idx, c_links, c_meta, c_matched, engine=c_eng, force_cur_time=cur_playback_time)
        else:
            played = _play_link_from_dialog(chosen_idx, c_links, c_meta, c_matched, engine=c_eng, global_idx=(chosen_idx + offset))

        if played == 'cancel':
            continue

        playback_ok = False
        if played:
            playback_ok = _verify_playback_started(is_torrent=is_tor)

        if playback_ok:
            return True

        if on_fail_setting == '1':
            return False

        # Enlace fallido: registrar y reabrir
        failed_links.add(chosen_link)
        failed_indices.add(chosen_idx)

        xbmc.log("Multi Bridge: enlace %d (%s) falló al reproducir. Reabriendo ventana..." % (chosen_idx + 1, srv_name), xbmc.LOGWARNING)
        _show_bridge_notification('Multi Bridge', 'Fallo en [B]%s[/B]. Reabriendo lista...' % srv_name, 3000)

        # Buscar el siguiente enlace no fallido en la pestaña activa
        active_list = []
        for l in c_links:
            _s = _get_link_server_name(l).strip().lower()
            _t = (_s == 'torrent' or _is_torrent_link(l))
            if (last_tab == 1 and _t) or (last_tab != 1 and not _t):
                active_list.append(l)

        next_suggested_idx = chosen_idx + 1
        if chosen_link in active_list:
            cur_tab_pos = active_list.index(chosen_link)
            found_next = None
            for off in range(1, len(active_list)):
                cand = active_list[(cur_tab_pos + off) % len(active_list)]
                if cand not in failed_links:
                    found_next = c_links.index(cand)
                    break
            if found_next is not None:
                next_suggested_idx = found_next
            elif next_suggested_idx >= len(c_links):
                next_suggested_idx = 0
        elif next_suggested_idx >= len(c_links):
            next_suggested_idx = 0

        xbmc.sleep(300)


def _open_links_view(win_url=""):
    global _search_in_progress, _links_view_active
    _search_in_progress = False
    _links_view_active = True
    _apply_silence_all()
    disp_mode = str(_bridge_addon.getSetting('links_display_mode') or '').strip().lower()
    if disp_mode in ('1', 'native'):
        if not win_url:
            _ts = int(time.time())
            win_url = 'plugin://plugin.video.bridge.multi/?view=list_links&t=%s' % _ts
        xbmc.log("Multi Bridge: abriendo list_links via ActivateWindow -> " + win_url, xbmc.LOGINFO)
        xbmc.executebuiltin('ActivateWindow(10025, "%s", return)' % win_url)
    else:
        show_floating_links_dialog()


# ---------------------------------------------------------
# Main Execution Entry Point
# ---------------------------------------------------------
def main():
    global plot, plot_lat, plot_es, tagline, tagline_lat, tagline_es
    global title, title_es, title_lat, title_en, title_orig
    global showname, year, showyear, season, episode
    global tmdb_id, imdb_id, tvdb_id, trakt_id
    global poster, fanart, thumbnail, director, clearlogo

    if action not in ('switch_source', 'floating_links', 'play_single_link'):
        check_and_run_migration()

    if clearlogo in ('_', 'None', 'none'):
        clearlogo = ''

    if action in ('switch_source', 'floating_links'):
        try:
            xbmc.executebuiltin('Dialog.Close(busydialog, true)')
            xbmc.executebuiltin('Dialog.Close(busydialognocancel, true)')
        except Exception:
            pass
        _offset_str = get_param('offset')
        try: _offset = int(_offset_str) if _offset_str else 0
        except: _offset = 0
        show_floating_links_dialog(offset=_offset)
        return

    if action == 'remove_continue_watching':
        mk = get_param('media_key')
        if mk:
            remove_continue_watching_record(mk)
            xbmcgui.Dialog().notification('Multi Bridge', 'Elemento quitado', '', 2500)
            xbmc.executebuiltin('Container.Refresh')
        return

    if action == 'clear_all_continue_watching':
        if xbmcgui.Dialog().yesno('Continuar viendo', '¿Seguro que deseas vaciar toda la lista de Continuar viendo?'):
            clear_all_continue_watching()
            xbmcgui.Dialog().notification('Multi Bridge', 'Lista vaciada', '', 2500)
            xbmc.executebuiltin('Container.Refresh')
        return

    if not action and not url:
        if not view or view == 'home': show_player_manager_home()
        elif view == 'continue_watching': show_continue_watching()
        elif view == 'list_players': show_players_list(engine_param or 'alfa')
        elif view in ('player_options', 'channel_options'): show_player_options(get_param('channel') or get_param('player'), engine_param or 'alfa')
        elif view == 'create_player': create_player_wizard()
        elif view == 'update_cloud': update_players_from_cloud()
        elif view == 'settings': _bridge_addon.openSettings(); (show_player_manager_home() if handle >= 0 else None)
        elif view == 'list_links':
            disp_mode = str(_bridge_addon.getSetting('links_display_mode') or '').strip().lower()
            if disp_mode in ('1', 'native'):
                show_links_as_directory()
            else:
                show_floating_links_dialog()
                if handle >= 0:
                    try: xbmcplugin.endOfDirectory(handle, succeeded=True, updateListing=False, cacheToDisc=False)
                    except: pass
        else: show_player_manager_home()
        return

    if action == 'search_other_engine':

        _se_engine = get_param('engine') or 'alfa'

        # Cargar el cache e inyectar metadata
        _se_meta = {}
        _se_item = None
        try:
            _sc = _read_search_cache_atomic()
            if _sc and isinstance(_sc, dict):
                _se_meta = _sc.get('meta', {}) or {}
                _se_item = _deserialize_item(_sc.get('item'), _se_engine)
        except: pass

        _se_is_series = bool(season or get_param('season') or (_se_meta.get('season') and _se_meta.get('episode')))
        if _se_is_series and _se_engine == 'alfa':
            xbmcgui.Dialog().notification('Multi Bridge', 'Alfa solo está disponible para películas', '', 3500)
            xbmcplugin.endOfDirectory(handle, succeeded=False)
            return

        def _g(k): return _se_meta.get(k) or ''

        title      = _g('title')      or title
        title_es   = _g('title_es')
        title_lat  = _g('title_lat')
        title_en   = _g('title_en')
        title_orig = _g('title_orig')
        showname   = _g('showname')   or showname
        year       = _g('year')       or year
        showyear   = _g('showyear')   or showyear
        season     = _g('season')     or season
        episode    = _g('episode')    or episode
        tmdb_id    = _g('tmdb')       or tmdb_id
        imdb_id    = _g('imdb')       or imdb_id
        tvdb_id    = _g('tvdb')       or tvdb_id
        trakt_id   = _g('trakt')      or trakt_id

        xbmc.log('Multi Bridge: search_other_engine=%s title=%s tmdb=%s' % (_se_engine, title, tmdb_id), xbmc.LOGINFO)

        _se_links, _se_matched = run_parallel_search(engine=_se_engine)

        _se_verified = False
        _auto_v = _bridge_addon.getSetting('auto_verify_links') == 'true'
        if _se_links and _auto_v:
            _pv = xbmcgui.DialogProgress()
            _pv.create('Multi Bridge', 'Comprobando disponibilidad...')
            _vl = _verify_links_headless(_se_links, engine=_se_engine, p_dialog=_pv)
            try: _pv.close()
            except: pass
            if _vl:
                _se_links  = _filter_and_sort_links(_vl)
                _se_verified = True

        _se_meta2 = dict(_se_meta)
        _se_meta2['engine'] = _se_engine
        _se_meta2['verified_only'] = _se_verified

        if _se_matched:
            _enrich_link_metadata(None, _se_meta2, _se_matched)
        try:
            _store_ram_search_cache(_se_links or [], _se_matched, _se_meta2, _se_engine)
            _save_search_cache_atomic({
                'item': _serialize_item(_se_matched) if _se_matched else None,
                'links': [_serialize_item(l) for l in (_se_links or [])],
                'meta': _se_meta2,
                'engine': _se_engine
            })
        except: pass
        try:
            _spawn_late_collector(_se_engine, _se_meta2.get('tmdb'), _se_meta2.get('season'), _se_meta2.get('episode'))
        except: pass

        if not _se_links:
            _eng_name = 'Alfa' if _se_engine == 'alfa' else 'Balandro'
            xbmcgui.Dialog().notification('Multi Bridge', 'No se encontraron enlaces en %s' % _eng_name, '', 4000)
            xbmcplugin.endOfDirectory(handle, succeeded=False)
            return

        _ts2 = int(time.time())
        _win_url2 = 'plugin://plugin.video.bridge.multi/?view=list_links&tmdb=%s&t=%s' % (str(tmdb_id or _g('tmdb') or ''), _ts2)
        if _se_meta2.get('season') and _se_meta2.get('episode'):
            _win_url2 += '&season=%s&episode=%s' % (_se_meta2['season'], _se_meta2['episode'])
        _open_links_view(_win_url2)
        xbmcplugin.endOfDirectory(handle, succeeded=True, updateListing=False, cacheToDisc=False)
        return

    if action == 'verify_links':
        verify_and_filter_links()
        return

    if action == 'play_single_link':
        idx_str = get_param('index')
        eng = engine_param or 'alfa'
        try: idx = int(idx_str)
        except: idx = 0
        links = []
        matched_item = None
        meta = {}
        cache = _read_search_cache_atomic()
        if cache and isinstance(cache, dict):
            try:
                engine = cache.get('engine', 'alfa')
                # Clases resueltas una sola vez (no una por enlace)
                _d_mods = _get_alfa_modules() if engine == 'alfa' else _get_balandro_modules()
                if _d_mods:
                    _d_Item, _d_Info = _d_mods['Item'], _d_mods['InfoLabels']
                    links = [_deserialize_item_fast(_d_Item, _d_Info, lnk) for lnk in cache.get('links', [])]
                    matched_item = _deserialize_item_fast(_d_Item, _d_Info, cache.get('item'))
                else:
                    links = [_deserialize_item(lnk, engine) for lnk in cache.get('links', [])]
                    matched_item = _deserialize_item(cache.get('item'), engine)
                links = [l for l in links if l is not None]
                meta = cache.get('meta', {})
            except: pass

        if links and 0 <= idx < len(links):
            chosen = links[idx]
            try:
                xbmc.log("Multi Bridge: play click idx=%d/%d canal=%s server=%s url=%s data_url=%s" % (
                    idx, len(links), _safe_str(getattr(chosen, 'channel', '')),
                    _safe_str(getattr(chosen, 'server', '')),
                    _safe_str(getattr(chosen, 'url', '') or '')[:60],
                    'si' if _safe_str(getattr(chosen, 'data_url', '') or '') else 'no'), xbmc.LOGINFO)
            except: pass
            _enrich_link_metadata(chosen, meta, matched_item)
            media_key = _get_media_key(meta, chosen)
            is_s = bool(meta.get('season') and meta.get('episode'))
            bm = get_bookmark(media_key, tmdb_id=meta.get('tmdb'), is_series=is_s, season=meta.get('season'), episode=meta.get('episode'))
            seek_to_time = 0
            if bm:
                r_time = float(bm.get('resume_time', 0))
                mins = int(r_time // 60); secs = int(r_time % 60); hrs = mins // 60; mins = mins % 60
                time_str = ('%d:%02d:%02d' % (hrs, mins, secs)) if hrs else ('%d:%02d' % (mins, secs))
                dialog = (_KODI_ORIG_DIALOG or xbmcgui.Dialog)()
                t_title = meta.get('title') or getattr(matched_item, 'title', '') or 'este vídeo'
                # select() en vez de yesno: asi Atras (-1) se distingue de No
                # y cancela sin reproducir.
                _rsel = dialog.select(
                    'Reanudar reproducción',
                    ['Reanudar desde %s' % time_str, 'Desde el principio'])
                if _rsel == 0:
                    seek_to_time = r_time
                elif _rsel is None or _rsel < 0:
                    return

            played = _play_link_safely(chosen, engine=eng, matched_item=matched_item, meta=meta)
            if played:
                start_playback_monitor(media_key, title_str=meta.get('title', ''), seek_to_time=seek_to_time, current_link_index=idx, meta=meta)
        else:
            _show_bridge_notification('Multi Bridge', 'Enlace no disponible', 3000)
        return

    if action == 'play':
        # Cerrar inmediatamente el diálogo "Elige Acción" de TMDb Helper que queda abierto
        # mientras nuestro plugin corre. Lo hacemos al inicio antes de cualquier búsqueda.
        try:
            xbmc.executebuiltin('Dialog.Close(10101,true)')
        except: pass
        try:
            if xbmc.Player().isPlaying(): xbmc.Player().stop()
        except: pass

        _s_check = season if season is not None and str(season).strip() != '' else get_param('season')
        _e_check = episode if episode is not None and str(episode).strip() != '' else get_param('episode')
        _is_s_play = bool(_s_check is not None and str(_s_check).strip() != '' and _e_check is not None and str(_e_check).strip() != '')

        # Comprobar si hay canales configurados al invocar desde TMDb Helper
        ch_data = load_channels_data()
        _all_alfa = ch_data.get('channels', {}).get('alfa', {})
        _all_bal = ch_data.get('channels', {}).get('balandro', {})
        if not _all_alfa and not _all_bal:
            opt = (_KODI_ORIG_DIALOG or xbmcgui.Dialog)().yesno(
                'Multi Bridge — Sin Canales',
                'No hay ningún canal configurado en Multi Bridge.\n\n'
                'Para buscar y reproducir contenido debes importar los canales:\n'
                '1. Abre el add-on [B]Multi Bridge[/B]\n'
                '2. Entra en [B]Importar / Actualizar Canales desde la Nube[/B]\n'
                '3. Confirma la descarga desde GitHub\n\n'
                '¿Deseas descargar e importar los canales ahora mismo desde la Nube?'
            )
            if opt:
                try:
                    update_players_from_cloud()
                except Exception as _e:
                    xbmc.log("Multi Bridge: error al importar canales desde aviso: %s" % _e, xbmc.LOGWARNING)
                ch_data = load_channels_data()
                _all_alfa = ch_data.get('channels', {}).get('alfa', {})
                _all_bal = ch_data.get('channels', {}).get('balandro', {})
            if not _all_alfa and not _all_bal:
                return

        if _is_s_play:
            _bal_series = [c for c in _all_bal.values() if c.get('series') and not c.get('series_disabled')]
            if not _bal_series:
                opt_s = (_KODI_ORIG_DIALOG or xbmcgui.Dialog)().yesno(
                    'Multi Bridge — Sin Canales de Series',
                    'No hay ningún canal de series activo en Balandro.\n\n'
                    '¿Deseas importar los canales de la Nube ahora mismo?'
                )
                if opt_s:
                    try:
                        update_players_from_cloud()
                    except Exception as _e:
                        xbmc.log("Multi Bridge: error importando canales: %s" % _e, xbmc.LOGWARNING)
                    ch_data = load_channels_data()
                    _all_bal = ch_data.get('channels', {}).get('balandro', {})
                    _bal_series = [c for c in _all_bal.values() if c.get('series') and not c.get('series_disabled')]
                if not _bal_series:
                    (_KODI_ORIG_DIALOG or xbmcgui.Dialog)().ok(
                        'Multi Bridge — Sin Canales de Series',
                        'Para ver series:\n1. Ve a [B]Multi Bridge > Gestor de Canales de Balandro[/B] y activa canales de series, o\n2. Usa [B]Importar / Actualizar Canales desde la Nube[/B].'
                    )
                    return
        else:
            _alfa_movies = [c for c in _all_alfa.values() if c.get('movies') and not c.get('movie_disabled')]
            _bal_movies = [c for c in _all_bal.values() if c.get('movies') and not c.get('movie_disabled')]
            if not _alfa_movies and not _bal_movies:
                opt_m = (_KODI_ORIG_DIALOG or xbmcgui.Dialog)().yesno(
                    'Multi Bridge — Sin Canales de Películas',
                    'No hay ningún canal de películas activo en Multi Bridge.\n\n'
                    '¿Deseas importar los canales de la Nube ahora mismo?'
                )
                if opt_m:
                    try:
                        update_players_from_cloud()
                    except Exception as _e:
                        xbmc.log("Multi Bridge: error importando canales: %s" % _e, xbmc.LOGWARNING)
                    ch_data = load_channels_data()
                    _all_alfa = ch_data.get('channels', {}).get('alfa', {})
                    _all_bal = ch_data.get('channels', {}).get('balandro', {})
                    _alfa_movies = [c for c in _all_alfa.values() if c.get('movies') and not c.get('movie_disabled')]
                    _bal_movies = [c for c in _all_bal.values() if c.get('movies') and not c.get('movie_disabled')]
                if not _alfa_movies and not _bal_movies:
                    (_KODI_ORIG_DIALOG or xbmcgui.Dialog)().ok(
                        'Multi Bridge — Sin Canales de Películas',
                        'Para ver películas:\n1. Ve a [B]Multi Bridge > Gestor de Canales[/B] y activa algún canal, o\n2. Usa [B]Importar / Actualizar Canales desde la Nube[/B].'
                    )
                    return
        _res_meta = _resolve_localized_metadata(
            tmdb_id=tmdb_id or get_param('tmdb'),
            is_series=_is_s_play,
            season=season or get_param('season'),
            episode=episode or get_param('episode'),
            plot_lat=plot_lat or get_param('plot_lat'),
            plot_es=plot_es or get_param('plot_es'),
            plot_param=plot or get_param('plot'),
            tagline_lat=tagline_lat or get_param('tagline_lat'),
            tagline_es=tagline_es or get_param('tagline_es'),
            tagline_param=tagline or get_param('tagline'),
            title_lat=title_lat or get_param('title_lat'),
            title_es=title_es or get_param('title_es'),
            title_param=title or get_param('title') or showname or get_param('showname')
        )
        if _res_meta.get('plot'):
            plot = _res_meta['plot']
        if _res_meta.get('tagline') is not None:
            tagline = _res_meta['tagline']
        if _res_meta.get('title') and _res_meta.get('has_spanish'):
            title = _res_meta['title']

        _init_meta = {
            'tmdb': tmdb_id or get_param('tmdb'),
            'imdb': imdb_id or get_param('imdb'),
            'tvdb': tvdb_id or get_param('tvdb'),
            'trakt': trakt_id or get_param('trakt'),
            'title': title or get_param('title') or get_param('title_es') or get_param('title_lat') or ((showname if (season and episode) else '') or ''),
            'year': (showyear or get_param('showyear')) if (season and episode) else (year or get_param('year')),
            'season': season or get_param('season'),
            'episode': episode or get_param('episode'),
            'showname': showname or get_param('showname'),
            'showyear': showyear or get_param('showyear'),
            'plot': plot,
            'tagline': tagline,
            'poster': poster or get_param('poster'),
            'fanart': fanart or get_param('fanart'),
            'thumbnail': thumbnail or get_param('thumbnail'),
            'clearlogo': (clearlogo if clearlogo and clearlogo not in ('_', 'None') else '') or (get_param('clearlogo') if get_param('clearlogo') not in ('_', 'None', None) else '') or _get_clearlogo(tmdb_id or get_param('tmdb'), bool((season or get_param('season')) and (episode or get_param('episode'))))
        }
        sync_tmdbhelper_playerstring(_init_meta)

        links = []
        matched_item = None
        meta = dict(_init_meta)

        # Reutilizar cache reciente (<90s) SOLO si corresponde EXACTAMENTE al mismo contenido
        # (misma película o mismo episodio de serie con idéntica temporada y número de episodio)
        _use_cache = False
        _cached_payload = None
        try:
            if os.path.exists(SEARCH_CACHE_FILE):
                import time as _time
                _mtime = os.path.getmtime(SEARCH_CACHE_FILE)
                _age = _time.time() - _mtime
                if _age < 90:
                    _c = _read_search_cache_atomic()
                    if _c and isinstance(_c, dict):
                        _c_meta = _c.get('meta', {}) or {}
                        _c_links = _c.get('links', [])
                        _cached_engine = _c.get('engine', 'alfa') or 'alfa'

                    if _c_links and _is_same_media_item(_c_meta, _init_meta):
                        _cur_def_eng = _get_int_setting('default_engine', 0)
                        if _is_s_play:
                            # Para series, Balandro es el único motor. Si la cache es de Balandro, es válida
                            if _cached_engine == 'balandro':
                                _use_cache = True
                                _cached_payload = _c
                                xbmc.log(f"Multi Bridge: cache reciente {_age:.1f}s para episodio serie ({_get_media_key(_init_meta)}), reutilizando sin nueva búsqueda", xbmc.LOGINFO)
                            else:
                                _use_cache = False
                        elif _cur_def_eng == 1 and _cached_engine != 'alfa':
                            _use_cache = False
                        elif _cur_def_eng == 2 and _cached_engine != 'balandro':
                            _use_cache = False
                        elif _cur_def_eng == 0:
                            _use_cache = False
                        else:
                            _use_cache = True
                            _cached_payload = _c
                            xbmc.log(f"Multi Bridge: cache reciente {_age:.1f}s para {_get_media_key(_init_meta)}, reutilizando sin nueva búsqueda", xbmc.LOGINFO)
                    elif _c_links:
                        xbmc.log(f"Multi Bridge: cache descartada (pertenece a otro contenido: cache={_get_media_key(_c_meta)} vs actual={_get_media_key(_init_meta)}), buscando fresco", xbmc.LOGINFO)
        except Exception as _ce:
            xbmc.log(f"Multi Bridge: cache reciente error: {_ce}", xbmc.LOGINFO)
            _use_cache = False

        if _use_cache and _cached_payload:
            # Reutilizar cache reciente — respetar ajuste autoplay
            try:
                _c = _cached_payload
                _cached_engine  = _c.get('engine', 'alfa') or 'alfa'
                _cached_links   = [_deserialize_item(l, _cached_engine) for l in _c.get('links', [])]
                _cached_item    = _deserialize_item(_c.get('item'), _cached_engine)
                _cached_meta    = _c.get('meta', {})
                _cached_tmdb_v  = str(_c.get('meta', {}).get('tmdb') or '')
                _autoplay_cache = _bridge_addon.getSetting('autoplay_enabled') == 'true'

                if _autoplay_cache and _cached_links:
                    xbmc.log('Multi Bridge: cache reciente → autoplay con %d enlaces' % len(_cached_links), xbmc.LOGINFO)
                    _enrich_link_metadata(_cached_links[0], _cached_meta, _cached_item)
                    _mk = _get_media_key(_cached_meta, _cached_links[0])
                    _is_s = bool(_cached_meta.get('season') and _cached_meta.get('episode'))
                    _bm = get_bookmark(_mk, tmdb_id=_cached_meta.get('tmdb'), is_series=_is_s,
                                       season=_cached_meta.get('season'), episode=_cached_meta.get('episode'))
                    _seek = 0
                    if _bm:
                        _rt2 = float(_bm.get('resume_time', 0))
                        _m2 = int(_rt2 // 60); _s2 = int(_rt2 % 60); _h2 = _m2 // 60; _m2 = _m2 % 60
                        _ts2 = ('%d:%02d:%02d' % (_h2, _m2, _s2)) if _h2 else ('%d:%02d' % (_m2, _s2))
                        _tt2 = _cached_meta.get('title') or ''
                        if _ask_resume_dialog(_tt2, _ts2):
                            _seek = _rt2
                    _played = _autoplay_with_fallback(
                        _cached_links, handle,
                        engine=_cached_engine, matched_item=_cached_item,
                        monitor_secs=180, max_attempts=999,
                        tmdb_for_list=_cached_tmdb_v,
                        meta=_cached_meta, seek_to_time=_seek,
                        resume_media_key=_mk,
                        resume_title=_cached_meta.get('title', ''))
                    return

                else:
                    # Autoplay desactivado → abrir lista de enlaces
                    xbmc.log('Multi Bridge: cache reciente → abriendo list_links', xbmc.LOGINFO)
                    _ts3 = int(time.time())
                    _win_url = 'plugin://plugin.video.bridge.multi/?view=list_links&tmdb=%s&t=%s' % (_cached_tmdb_v, _ts3)
                    if _cached_meta.get('season') and _cached_meta.get('episode'):
                        _win_url += '&season=%s&episode=%s' % (_cached_meta['season'], _cached_meta['episode'])
                    _open_links_view(_win_url)
                    return
            except Exception as _ce:
                xbmc.log('Multi Bridge: cache reciente error: ' + str(_ce), xbmc.LOGINFO)
                pass  # Si falla, continua con búsqueda normal

        if True:  # forzar fresco, no usar cache (si no es caso de doble busqueda)
            eng = 'alfa'
            def_engine = _get_int_setting('default_engine', 0)
            alfa_ok = _is_engine_installed('alfa')
            bal_ok  = _is_engine_installed('balandro')

            if _is_s_play:
                # Las series son exclusivas de Balandro (Alfa es solo para películas)
                if not bal_ok:
                    xbmcgui.Dialog().ok('Multi Bridge', 'Para reproducir series se requiere tener instalado el addon Balandro.')
                    return
                eng = 'balandro'
                xbmc.log("Multi Bridge: Contenido es serie/episodio y Alfa solo soporta películas. Usando Balandro automáticamente.", xbmc.LOGINFO)
            elif def_engine == 1:
                if alfa_ok:
                    eng = 'alfa'
                elif bal_ok:
                    xbmc.log("Multi Bridge: Motor Alfa no está instalado. Usando Balandro automáticamente.", xbmc.LOGINFO)
                    eng = 'balandro'
                else:
                    xbmcgui.Dialog().ok('Multi Bridge', 'El addon Alfa no está instalado.')
                    return
            elif def_engine == 2:
                if bal_ok:
                    eng = 'balandro'
                elif alfa_ok:
                    xbmc.log("Multi Bridge: Motor Balandro no está instalado. Usando Alfa automáticamente.", xbmc.LOGINFO)
                    eng = 'alfa'
                else:
                    xbmcgui.Dialog().ok('Multi Bridge', 'El addon Balandro no está instalado.')
                    return
            else:
                _picked = _show_engine_picker()
                if _picked is None:
                    return
                eng = _picked

            links, matched_item = run_parallel_search(engine=eng)
            verified_flag = False
            _auto_verify = _bridge_addon.getSetting('auto_verify_links') == 'true'
            xbmc.log(f"Multi Bridge: auto_verify_links={_auto_verify} engine={eng} links={len(links) if links else 0}", xbmc.LOGINFO)
            if links and _auto_verify:
                p_diag = xbmcgui.DialogProgress()
                p_diag.create('Multi Bridge', 'Comprobando disponibilidad de servidores...')
                v_links = _verify_links_headless(links, engine=eng, p_dialog=p_diag)
                try: p_diag.close()
                except: pass
                if v_links:
                    links = _filter_and_sort_links(v_links)
                    verified_flag = True

            meta = {
                'tmdb': tmdb_id or get_param('tmdb'), 'imdb': imdb_id or get_param('imdb'), 'tvdb': tvdb_id or get_param('tvdb'), 'trakt': trakt_id or get_param('trakt'),
                'title': title or get_param('title') or get_param('title_es') or get_param('title_lat') or ((showname if (season and episode) else '') or ''),
                'year': (showyear or get_param('showyear')) if (season and episode) else (year or get_param('year')),
                'season': season or get_param('season'), 'episode': episode or get_param('episode'), 'showname': showname or get_param('showname'), 'showyear': showyear or get_param('showyear'),
                'plot': plot or get_param('plot'), 'director': director or get_param('director'), 'tagline': tagline or get_param('tagline'),
                'poster': poster or get_param('poster'), 'fanart': fanart or get_param('fanart'), 'thumbnail': thumbnail or get_param('thumbnail'),
                'clearlogo': (clearlogo if clearlogo and clearlogo not in ('_', 'None') else '') or (get_param('clearlogo') if get_param('clearlogo') not in ('_', 'None', None) else '') or _get_clearlogo(tmdb_id or get_param('tmdb'), bool((season or get_param('season')) and (episode or get_param('episode')))),
                'verified_only': verified_flag, 'engine': eng
            }
            if matched_item:
                _enrich_link_metadata(None, meta, matched_item)
            try:
                _store_ram_search_cache(links or [], matched_item, meta, eng)
                _save_search_cache_atomic({
                    'item': _serialize_item(matched_item) if matched_item else None,
                    'links': [_serialize_item(l) for l in (links or [])],
                    'meta': meta,
                    'engine': eng
                })
            except: pass
            try:
                _spawn_late_collector(eng, meta.get('tmdb'), meta.get('season'), meta.get('episode'))
            except: pass

        if not links:
            _offer_elementum_search(meta)
            return


        autoplay = _bridge_addon.getSetting('autoplay_enabled') == 'true'
        if autoplay:
            _enrich_link_metadata(links[0], meta, matched_item)
            media_key = _get_media_key(meta, links[0])
            is_s = bool(meta.get('season') and meta.get('episode'))
            bm = get_bookmark(media_key, tmdb_id=meta.get('tmdb'), is_series=is_s,
                              season=meta.get('season'), episode=meta.get('episode'))
            seek_to_time = 0
            if bm:
                r_time = float(bm.get('resume_time', 0))
                mins = int(r_time // 60); secs = int(r_time % 60)
                hrs = mins // 60; mins = mins % 60
                time_str = ('%d:%02d:%02d' % (hrs, mins, secs)) if hrs else ('%d:%02d' % (mins, secs))
                t_title = meta.get('title') or getattr(matched_item, 'title', '') or 'este vídeo'
                res = _ask_resume_dialog(t_title, time_str)
                if res:
                    seek_to_time = r_time

            xbmc.log('Multi Bridge: autoplay con fallback, %d enlaces disponibles, motor=%s' % (
                len(links), meta.get('engine', 'alfa')), xbmc.LOGINFO)

            # Intenta TODOS los enlaces no-torrent con monitoreo de 3 min cada uno
            played = _autoplay_with_fallback(
                links, handle,
                engine       = meta.get('engine', 'alfa'),
                matched_item = matched_item,
                monitor_secs = 180,
                max_attempts = 999,
                tmdb_for_list= str(tmdb_id or meta.get('tmdb') or ''),
                meta         = meta, seek_to_time=seek_to_time,
                resume_media_key=media_key,
                resume_title=meta.get('title', ''))
            return

        else:
            # Autoplay desactivado → abrir directamente la lista de enlaces
            _cur_tmdb = str(tmdb_id or get_param('tmdb') or meta.get('tmdb') or '')
            _ts = int(time.time())
            _win_url = 'plugin://plugin.video.bridge.multi/?view=list_links&tmdb=%s&t=%s' % (_cur_tmdb, _ts)
            if meta.get('season') and meta.get('episode'):
                _win_url += '&season=%s&episode=%s' % (meta['season'], meta['episode'])
            _open_links_view(_win_url)
            return

    if url:
        search_item = None
        if 'action=search' in url:
            _is_s_url = bool((season or get_param('season')) and (episode or get_param('episode')))
            if _is_s_url:
                eng = 'balandro'
                xbmc.log("Multi Bridge: Búsqueda para serie en URL -> Balandro asignado automáticamente", xbmc.LOGINFO)
            else:
                eng = 'alfa'
                def_engine = _get_int_setting('default_engine', 0)
                if def_engine == 0:
                    _picked = _show_engine_picker()
                    if _picked is None:
                        xbmcplugin.endOfDirectory(handle, succeeded=False)
                        return
                    eng = _picked
                elif def_engine == 1: eng = 'alfa'
                elif def_engine == 2: eng = 'balandro'

            links, matched_item = run_parallel_search(engine=eng)
        else:
            is_alfa = 'alfa' in url.lower()
            is_series = bool((season or get_param('season')) and (episode or get_param('episode')))
            if is_alfa and is_series:
                xbmcgui.Dialog().notification('Multi Bridge', 'Alfa solo soporta películas', '', 3000)
                xbmcplugin.endOfDirectory(handle, succeeded=False)
                return

            eng = 'alfa' if is_alfa else 'balandro'
            mods = _get_alfa_modules() if is_alfa else _get_balandro_modules()
            ItemClass = mods['Item'] if mods else object
            raw_b64 = url.split('plugin://plugin.video.alfa/?')[-1].split('plugin://plugin.video.balandro/?')[-1].split('&')[0]
            raw_b64 = uparse.unquote(raw_b64)
            search_item = decode_base64_item(raw_b64, ItemClass)
            target_channel = getattr(search_item, 'channel', '')
            target_title = (showname or get_param('showname') if is_series else (title or get_param('title'))) or get_param('title_es') or get_param('title_lat') or get_param('title_orig') or get_param('title_en') or ''
            target_year = (showyear or get_param('showyear')) if is_series else (year or get_param('year'))
            target_tmdb = tmdb_id or get_param('tmdb')
            target_imdb = imdb_id or get_param('imdb')
            _res_meta_url = _resolve_localized_metadata(
                tmdb_id=target_tmdb,
                is_series=is_series,
                season=season or get_param('season'),
                episode=episode or get_param('episode'),
                plot_lat=plot_lat or get_param('plot_lat'),
                plot_es=plot_es or get_param('plot_es'),
                plot_param=plot or get_param('plot'),
                tagline_lat=tagline_lat or get_param('tagline_lat'),
                tagline_es=tagline_es or get_param('tagline_es'),
                tagline_param=tagline or get_param('tagline'),
                title_lat=title_lat or get_param('title_lat'),
                title_es=title_es or get_param('title_es'),
                title_param=target_title
            )
            if _res_meta_url.get('plot'):
                plot = _res_meta_url['plot']
            if _res_meta_url.get('tagline') is not None:
                tagline = _res_meta_url['tagline']
            if _res_meta_url.get('title') and _res_meta_url.get('has_spanish'):
                if not title_es:
                    title_es = _res_meta_url['title']
            all_names = [t for t in [target_title, title or get_param('title'), title_es or get_param('title_es'), title_lat or get_param('title_lat'), title_en or get_param('title_en'), title_orig or get_param('title_orig'), showname or get_param('showname')] if t]
            alt_terms = [t for t in [title_es or get_param('title_es'), title_lat or get_param('title_lat'), title_orig or get_param('title_orig'), title_en or get_param('title_en'), showname or get_param('showname'), title or get_param('title')] if t and t != target_title]
            if target_channel and target_channel != 'search':
                p_dialog = xbmcgui.DialogProgress()
                p_dialog.create('Multi Bridge', 'Buscando en %s...' % target_channel.capitalize())
                if is_alfa:
                    matched_item, links = _search_channel_alfa(target_channel, target_title, target_year, is_series, season or get_param('season'), episode or get_param('episode'), all_names, base_item=search_item, alt_terms=alt_terms, target_tmdb=target_tmdb, target_imdb=target_imdb)
                else:
                    matched_item, links = _search_channel_balandro(target_channel, target_title, target_year, is_series, season or get_param('season'), episode or get_param('episode'), all_names, base_item=search_item, alt_terms=alt_terms, target_tmdb=target_tmdb, target_imdb=target_imdb)
                p_dialog.close()
            else:
                links, matched_item = run_parallel_search(engine=eng)

        if links and matched_item:
            verified_flag = False
            _auto_verify2 = _bridge_addon.getSetting('auto_verify_links') == 'true'
            xbmc.log(f"Multi Bridge: auto_verify_links={_auto_verify2} engine={eng} links={len(links) if links else 0}", xbmc.LOGINFO)
            if _auto_verify2:
                p_diag = xbmcgui.DialogProgress()
                p_diag.create('Multi Bridge', 'Comprobando disponibilidad de servidores...')
                v_links = _verify_links_headless(links, engine=eng, p_dialog=p_diag)
                try: p_diag.close()
                except: pass
                if v_links:
                    links = v_links
                    verified_flag = True

            links = _filter_and_sort_links(links)
            meta = {
                'tmdb': tmdb_id or get_param('tmdb'), 'imdb': imdb_id or get_param('imdb'), 'tvdb': tvdb_id or get_param('tvdb'), 'trakt': trakt_id or get_param('trakt'),
                'title': title or get_param('title') or get_param('title_es') or get_param('title_lat') or ((showname if (season and episode) else '') or ''),
                'year': (showyear or get_param('showyear')) if (season and episode) else (year or get_param('year')),
                'season': season or get_param('season'), 'episode': episode or get_param('episode'), 'showname': showname or get_param('showname'), 'showyear': showyear or get_param('showyear'),
                'plot': plot or get_param('plot'), 'director': director or get_param('director'), 'tagline': tagline or get_param('tagline'),
                'poster': poster or get_param('poster'), 'fanart': fanart or get_param('fanart'), 'thumbnail': thumbnail or get_param('thumbnail'),
                'clearlogo': (clearlogo if clearlogo and clearlogo not in ('_', 'None') else '') or (get_param('clearlogo') if get_param('clearlogo') not in ('_', 'None', None) else '') or _get_clearlogo(tmdb_id or get_param('tmdb'), bool((season or get_param('season')) and (episode or get_param('episode')))),
                'verified_only': verified_flag, 'engine': eng
            }
            if matched_item:
                _enrich_link_metadata(None, meta, matched_item)
            try:
                _store_ram_search_cache(links or [], matched_item, meta, eng)
                _save_search_cache_atomic({
                    'item': _serialize_item(matched_item),
                    'links': [_serialize_item(l) for l in (links or [])],
                    'meta': meta,
                    'engine': eng
                })
            except Exception: pass
            try:
                _spawn_late_collector(eng, meta.get('tmdb'), meta.get('season'), meta.get('episode'))
            except: pass

            autoplay = _bridge_addon.getSetting('autoplay_enabled') == 'true'
            if autoplay:
                _enrich_link_metadata(links[0], meta, matched_item)
                media_key = _get_media_key(meta, links[0])
                is_s = bool(meta.get('season') and meta.get('episode'))
                bm = get_bookmark(media_key, tmdb_id=meta.get('tmdb'), is_series=is_s,
                                  season=meta.get('season'), episode=meta.get('episode'))
                seek_to_time = 0
                if bm:
                    r_time = float(bm.get('resume_time', 0))
                    mins = int(r_time // 60); secs = int(r_time % 60)
                    hrs = mins // 60; mins = mins % 60
                    time_str = ('%d:%02d:%02d' % (hrs, mins, secs)) if hrs else ('%d:%02d' % (mins, secs))
                    t_title = meta.get('title') or getattr(matched_item, 'title', '') or 'este vídeo'
                    res = _ask_resume_dialog(t_title, time_str)
                    if res:
                        seek_to_time = r_time

                played = _autoplay_with_fallback(
                    links, handle,
                    engine       = meta.get('engine', 'alfa'),
                    matched_item = matched_item,
                    monitor_secs = 180,
                    max_attempts = 999,
                    tmdb_for_list= str(tmdb_id or meta.get('tmdb') or ''),
                    meta         = meta, seek_to_time=seek_to_time,
                    resume_media_key=media_key,
                    resume_title=meta.get('title', ''))
            else:
                _cur_tmdb = str(tmdb_id or get_param('tmdb') or meta.get('tmdb') or '')
                _ts = int(time.time())
                _win_url = 'plugin://plugin.video.bridge.multi/?view=list_links&tmdb=%s&t=%s' % (_cur_tmdb, _ts)
                if meta.get('season') and meta.get('episode'):
                    _win_url += '&season=%s&episode=%s' % (meta['season'], meta['episode'])
                _open_links_view(_win_url)
            return
        else:
            _meta_fallback = {
                'tmdb':    tmdb_id or get_param('tmdb'),
                'title':   title or get_param('title') or get_param('title_es') or get_param('title_lat') or showname or get_param('showname'),
                'season':  season or get_param('season'),
                'episode': episode or get_param('episode'),
                'showname': showname or get_param('showname'),
            }
            _offer_elementum_search(_meta_fallback)
        return




if __name__ == '__main__':
    main()
