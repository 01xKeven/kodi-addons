# -*- coding: utf-8 -*-
import sys
import os
import re
import json
import base64
import threading
import time
import socket
socket.setdefaulttimeout(30)

# Store original handle and argv before modifying anything
handle = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1] else -1
original_argv = list(sys.argv)

# Mock sys.argv[0] to make Balandro think it is running as itself
sys.argv[0] = 'plugin://plugin.video.balandro/'

import xbmc
import xbmcaddon
import xbmcvfs
import xbmcplugin
import xbmcgui

# Window property usada para comunicar la decision de resume entre procesos
_RESUME_FLAG_PROP = 'BalandroBridgeUserWantsResume'
_RESUME_FLAG_TS_PROP = 'BalandroBridgeResumeTimestamp'

# Add Balandro paths to import modules
balandro_addon = xbmcaddon.Addon('plugin.video.balandro')
balandro_path = xbmcvfs.translatePath(balandro_addon.getAddonInfo('Path'))
sys.path.insert(0, balandro_path)
sys.path.insert(1, os.path.join(balandro_path, 'lib'))

from platformcode import config, platformtools
from core.item import Item, InfoLabels
from core import servertools

# Variables globales para controlar la interceptación del Autoplay
_autoplay_in_progress = False
_autoplay_dialog = None
# Contador de veces que dialog_select es llamado por el mismo enlace durante autoplay
_autoplay_dialog_select_count = 0
# Flag: tras elegir servidor en el selector manual, auto-elegir calidad máxima en diálogos secundarios
_after_link_selection = False

# Monkeypatch httptools.downloadpage para abortar descargas si el usuario cancela autoplay
try:
    from core import httptools
    _orig_downloadpage = httptools.downloadpage
    def patched_downloadpage(*args, **kwargs):
        global _autoplay_dialog, _autoplay_in_progress
        if _autoplay_dialog and _autoplay_dialog.iscanceled():
            xbmc.log("Balandro Bridge Multi: downloadpage abortado por cancelación de autoplay", xbmc.LOGINFO)
            from core.httptools import Response
            return Response(data="")
        if _autoplay_in_progress:
            kwargs['timeout'] = 4
        return _orig_downloadpage(*args, **kwargs)
    httptools.downloadpage = patched_downloadpage
    xbmc.log("Balandro Bridge Multi: Parche de httptools.downloadpage aplicado correctamente", xbmc.LOGINFO)
except Exception as e_http:
    xbmc.log("Balandro Bridge Multi: FALLO al parchear httptools - " + str(e_http), xbmc.LOGERROR)

# Monkeypatch dialog_select to clean heading and prevent None returns
_orig_dialog_select = platformtools.dialog_select
def patched_dialog_select(heading, _list, autoclose=0, preselect=-1, useDetails=False):
    xbmc.log("Balandro Bridge Multi: patched_dialog_select llamado con heading: %s" % heading, xbmc.LOGINFO)
    global _autoplay_in_progress, _autoplay_dialog, _autoplay_dialog_select_count, _after_link_selection

    # --- Modo AUTOPLAY ---
    if _autoplay_dialog and _autoplay_dialog.iscanceled():
        xbmc.log("Balandro Bridge Multi: dialog_select abortado por cancelación de autoplay", xbmc.LOGINFO)
        return -1
    if _autoplay_in_progress:
        _autoplay_dialog_select_count += 1
        if _autoplay_dialog_select_count > 1:
            if "disponibles en" in heading:
                # Es un reintento del selector de mirrors: el primer mirror falló → abortar este enlace
                xbmc.log("Balandro Bridge Multi: Autoplay - reintento de mirror #%d. Abortando enlace." % _autoplay_dialog_select_count, xbmc.LOGINFO)
                return -1
            else:
                # Es un selector de calidad interno del servidor.
                # Elegir la ÚLTIMA opción = mayor calidad (len(_list)-1 es dinámico, funciona con cualquier cantidad)
                best_idx = len(_list) - 1 if _list else 0
                try:
                    opts = [str(getattr(o, 'label', o)) for o in _list]
                except:
                    opts = [str(o) for o in _list]
                xbmc.log("Balandro Bridge Multi: Autoplay - selector de calidad. Opciones: %s → eligiendo índice %d ('%s')" % (opts, best_idx, opts[best_idx] if opts else ''), xbmc.LOGINFO)
                return best_idx


        xbmc.log("Balandro Bridge Multi: Autoplay activo. Auto-seleccionando opción 0 para: %s" % heading, xbmc.LOGINFO)
        return 0

    # --- Modo MANUAL (sin cambios, tal como estaba) ---
    if "disponibles en" in heading:
        heading = "[COLOR fuchsia]Enlaces[/COLOR] disponibles"
    res = _orig_dialog_select(heading, _list, autoclose, preselect, useDetails)
    xbmc.log("Balandro Bridge Multi: patched_dialog_select retorno original: %s" % str(res), xbmc.LOGINFO)
    if res is None or res < 0:
        return -1
    return res


def apply_monkeypatch():
    # Overwrite dialog_select unconditionally on all loaded modules in sys.modules to fix duplicate/late imports
    import sys
    for name, module in list(sys.modules.items()):
        if module and hasattr(module, '__dict__'):
            if 'dialog_select' in module.__dict__:
                try:
                    module.__dict__['dialog_select'] = patched_dialog_select
                    xbmc.log("Balandro Bridge Multi: Monkeypatched dialog_select in module: %s" % name, xbmc.LOGINFO)
                except Exception as e:
                    pass

apply_monkeypatch()

# Monkeypatch filter_and_sort_by_quality to avoid TypeError when quality_num is '' or str
_orig_filter_quality = servertools.filter_and_sort_by_quality
def patched_filter_quality(itemlist):
    def safe_quality_num(it):
        try: return int(it.quality_num)
        except: return 0
    servers_sort_quality = config.get_setting('servers_sort_quality', default=0)
    if servers_sort_quality == 1:
        return sorted(itemlist, key=safe_quality_num, reverse=True)
    elif servers_sort_quality == 2:
        return sorted(itemlist, key=safe_quality_num)
    return itemlist
servertools.filter_and_sort_by_quality = patched_filter_quality

# Monkeypatch GibberishAES y decrypters para corregir descifrado
try:
    from lib import decrypters
    from lib.pyberishaes import GibberishAES

    # --- Parche 1: block2s seguro (evita exception con padding invalido) ---
    _orig_block2s = GibberishAES.block2s
    def patched_block2s(self, block, lastBlock):
        try:
            return _orig_block2s(self, block, lastBlock)
        except Exception as ex:
            xbmc.log('Balandro Bridge Multi: block2s error (padding invalido): ' + str(ex), xbmc.LOGWARNING)
            # Intentar sin padding: devolver todos los bytes como string
            try:
                s = ''
                for b in block:
                    if 32 <= b < 127:
                        s += chr(b)
                return s.rstrip('\x00')
            except:
                return ''
    GibberishAES.block2s = patched_block2s

    # --- Parche 2: rawDecrypt con indice correcto (fix NameError en 1 bloque) ---
    def patched_rawDecrypt(self, cryptArr, key, iv, binary=None):
        key = self.expandKey(key)
        numBlocks = len(cryptArr) // 16
        if numBlocks == 0:
            xbmc.log('Balandro Bridge Multi: rawDecrypt - cryptArr vacio o muy corto', xbmc.LOGWARNING)
            return ''
        cipherBlocks = []
        plainBlocks = []
        string = ''

        for i in range(numBlocks):
            cipherBlocks.append(cryptArr[i * 16 : (i + 1) * 16])

        for i in reversed(range(len(cipherBlocks))):
            plainBlocks.append(self.decryptBlock(cipherBlocks[i], key))
            if i == 0:
                reversePlainBlocks = [self.xorBlocks(plainBlocks[-1], iv)]
                reversePlainBlocks.extend(plainBlocks)
                plainBlocks = reversePlainBlocks[:]
            else:
                reversePlainBlocks = [self.xorBlocks(plainBlocks[-1], cipherBlocks[i - 1])]
                reversePlainBlocks.extend(plainBlocks)
                plainBlocks = reversePlainBlocks[:]

        for i in range(numBlocks - 1):
            string += self.block2s(plainBlocks[i], False)

        # FIX: usar numBlocks-1 en lugar de i+1 (que lanza NameError cuando numBlocks==1)
        string += self.block2s(plainBlocks[numBlocks - 1], True)
        return string

    GibberishAES.rawDecrypt = patched_rawDecrypt

    # --- Parche 3: dec() con logging completo ---
    _orig_dec = GibberishAES.dec
    def patched_dec(self, string, pass_, binary=None):
        try:
            result = _orig_dec(self, string, pass_, binary)
            xbmc.log('Balandro Bridge Multi: dec() resultado len=' + str(len(result)) +
                     ' starts_http=' + str(result.startswith('http')) +
                     ' pass_=' + str(pass_)[:30], xbmc.LOGINFO)
            return result
        except Exception as ex:
            xbmc.log('Balandro Bridge Multi: dec() EXCEPCION: ' + str(ex) +
                     ' pass_=' + str(pass_)[:30], xbmc.LOGERROR)
            return ''
    GibberishAES.dec = patched_dec

    # --- Parche 4: decode_decipher con AES-CBC raw (fallback de Cryptodome) ---
    def patched_decode_decipher(_cryto, e_bytes):
        try:
            encrypt = base64.b64decode(_cryto)
            _len = e_bytes.encode('utf-8') if not isinstance(e_bytes, bytes) else e_bytes
            if len(_len) not in (16, 24, 32):
                _len = (_len + b'\x00' * 32)[:32]

            iv_bytes = list(encrypt[:16])
            ciphertext = list(encrypt[16:])
            key_list = list(_len)

            aes = GibberishAES()
            aes.Nk = len(key_list) // 4
            aes.Nr = {4: 10, 6: 12, 8: 14}.get(aes.Nk, 14)

            result = aes.rawDecrypt(ciphertext, key_list, iv_bytes)
            xbmc.log('Balandro Bridge Multi: decode_decipher() resultado len=' + str(len(result)) +
                     ' starts_http=' + str(result.startswith('http') if result else False) +
                     ' key_len=' + str(len(key_list)), xbmc.LOGINFO)
            return result
        except Exception as ex:
            xbmc.log('Balandro Bridge Multi: decode_decipher() EXCEPCION: ' + str(ex), xbmc.LOGERROR)
            return ''

    decrypters.decode_decipher = patched_decode_decipher

    xbmc.log('Balandro Bridge Multi: Parches de descifrado aplicados correctamente', xbmc.LOGINFO)

except Exception as e:
    import traceback
    xbmc.log('Balandro Bridge Multi: FALLO al aplicar parches de descifrado: ' + traceback.format_exc(), xbmc.LOGERROR)


# --- Parche TMDB: corregir &year= a &primary_release_year= en búsquedas de películas ---
try:
    from core.tmdb import Tmdb
    _orig_get_json = Tmdb.get_json
    @staticmethod
    def patched_get_json(url):
        if '/search/movie' in url and '&year=' in url:
            url = url.replace('&year=', '&primary_release_year=')
        return _orig_get_json(url)
    Tmdb.get_json = patched_get_json
    xbmc.log('Balandro Bridge Multi: Parche de TMDB (primary_release_year) aplicado correctamente', xbmc.LOGINFO)
except Exception as e:
    import traceback
    xbmc.log('Balandro Bridge Multi: FALLO al aplicar parche de TMDB: ' + traceback.format_exc(), xbmc.LOGERROR)


# --- Parche set_infoLabels: heredar año objetivo si el canal no lo raspó ---
import threading
_bridge_tls = threading.local()  # thread-local: disable_year_filter para items sin año propio
_bridge_target_tmdb_id = None    # global: accesible desde los sub-hilos de set_infoLabels de Balandro

try:
    from core import tmdb
    _orig_set_infoLabels = tmdb.set_infoLabels
    def patched_set_infoLabels(source, seekTmdb=True, idioma_busqueda=tmdb.tmdb_lang):
        global year, title, title_es, title_lat, title_en, title_orig, showname, showyear
        # Separar items con año propio del canal vs sin año.
        # Para los SIN año (year='-'): llamar a TMDB sin filtro de año para que haga
        # su búsqueda natural. Si TMDB encuentra la película correcta por título puro,
        # el ID coincidirá con nuestro target. Si no (otro film con mismo título),
        # el ID no coincidirá → rechazo correcto.
        # Ej: Supergirl → TMDB sin filtro año → encuentra Supergirl 1984 → ID≠2026 → rechaza ✅
        # Ej: Toy Story 5 → TMDB sin filtro año → encuentra Toy Story 5 2026 → ID=target → acepta ✅
        items_list = source if isinstance(source, list) else [source]
        uncertain_ids = set()
        certain_items = []
        uncertain_items = []
        for item in items_list:
            if hasattr(item, 'infoLabels'):
                raw_year = item.infoLabels.get('year', '')
                if not raw_year or str(raw_year).strip() in ('-', '?', '0', '', 'None'):
                    # Intentar extraer año de la URL del item si es incierto
                    if getattr(item, 'url', ''):
                        import re
                        m = re.search(r'[-_](19\d{2}|20\d{2})(?:[-_/]|$|\.html?)', item.url)
                        if m:
                            extracted_year = m.group(1)
                            try:
                                import datetime
                                yr_val = int(extracted_year)
                                # Validar que sea un año coherente
                                if 1900 <= yr_val <= datetime.date.today().year + 2:
                                    # Evitar falsos positivos si el año extraído forma parte del título original/objetivo
                                    target_titles_lower = [t.lower() for t in [title, title_es, title_lat, title_en, title_orig, showname] if t]
                                    is_part_of_title = False
                                    for t in target_titles_lower:
                                        if extracted_year in t:
                                            is_part_of_title = True
                                            break
                                    if not is_part_of_title:
                                        item.infoLabels['year'] = extracted_year
                                        raw_year = extracted_year
                                        xbmc.log("Balandro Bridge Multi: Año '%s' extraído de la URL '%s' para canal '%s'" % (extracted_year, item.url, getattr(item, 'channel', '?')), xbmc.LOGINFO)
                            except:
                                pass

                if not raw_year or str(raw_year).strip() in ('-', '?', '0', '', 'None'):
                    uncertain_ids.add(id(item))
                    uncertain_items.append(item)
                else:
                    certain_items.append(item)
            else:
                certain_items.append(item)

        # Procesar items con año propio normalmente (con filtro de año activo)
        if certain_items:
            src = certain_items if isinstance(source, list) else certain_items[0] if certain_items else source
            _orig_set_infoLabels(src, seekTmdb, idioma_busqueda)

        # Procesar items SIN año propio: deshabilitar filtro de año en TMDB temporalmente
        import datetime as _dt
        _today = _dt.date.today()
        for item in uncertain_items:
            try:
                _bridge_tls.disable_year_filter = True
                _orig_set_infoLabels([item], seekTmdb, idioma_busqueda)
            finally:
                _bridge_tls.disable_year_filter = False
            item.infoLabels['_bridge_year_uncertain'] = True

        # Aplicar bloqueo de pre-estrenos o estrenos muy recientes (menos de 10 días) en base a la fecha de estreno en TMDB
        # Esto evita reproducir enlaces de películas antiguas (fake) en fichas de películas no estrenadas o recién estrenadas en cines.
        for item in items_list:
            if not hasattr(item, 'infoLabels'):
                continue
            release_date_str = ''
            try:
                release_date_str = str(item.infoLabels.get('release_date', '') or item.infoLabels.get('premiered', '') or item.infoLabels.get('aired', '') or '')
            except: pass
            if release_date_str and '/' in release_date_str:
                try:
                    parts = release_date_str.split('/')
                    film_date = _dt.date(int(parts[2]), int(parts[1]), int(parts[0]))
                    # SÓLO si el año es incierto en el canal
                    is_uncertain = item.infoLabels.get('_bridge_year_uncertain', False)
                    if is_uncertain and film_date > _today - _dt.timedelta(days=15):
                        for id_key in ('tmdb_id', 'tmdb', 'imdb_id', 'IMDBNumber', 'code'):
                            item.infoLabels.pop(id_key, None)
                        item.infoLabels['_bridge_future_release'] = True
                        xbmc.log("Balandro Bridge Multi: '%s' — estreno muy reciente/futuro (%s) con año incierto → IDs eliminados para evitar fake" % (getattr(item, 'title', '?'), release_date_str), xbmc.LOGINFO)
                    else:
                        xbmc.log("Balandro Bridge Multi: '%s' — estreno pasado o con año propio (%s) → IDs válidos" % (getattr(item, 'title', '?'), release_date_str), xbmc.LOGINFO)
                except:
                    pass

        # Para items con año propio: corregir si TMDB puso un año muy diferente al objetivo
        target_year = year or showyear
        if target_year:
            try:
                target_year_int = int(target_year)
            except:
                target_year_int = 0
            target_titles = [t.lower().strip() for t in [title, title_es, title_lat, title_en, title_orig, showname] if t]
            for item in certain_items:
                if not hasattr(item, 'infoLabels'):
                    continue
                item_year_raw = item.infoLabels.get('year', '')
                try:
                    item_year_int = int(item_year_raw) if item_year_raw and item_year_raw not in ('-', '?', '') else 0
                except:
                    item_year_int = 0
                item_title = getattr(item, 'title', '').strip().lower()
                if item_title in target_titles and target_year_int and item_year_int and abs(item_year_int - target_year_int) > 1:
                    item.infoLabels['year'] = str(target_year_int)
                    xbmc.log("Balandro Bridge Multi: Corrigiendo año TMDB incorrecto en '%s': %s → %s" % (item.title, item_year_raw, target_year_int), xbmc.LOGINFO)
        return None
    tmdb.set_infoLabels = patched_set_infoLabels
    xbmc.log('Balandro Bridge Multi: Parche de set_infoLabels (búsqueda natural para items sin año) aplicado', xbmc.LOGINFO)
except Exception as e:
    import traceback
    xbmc.log('Balandro Bridge Multi: FALLO al aplicar parche de set_infoLabels: ' + traceback.format_exc(), xbmc.LOGERROR)


# --- Parche TMDB Search Exact Match: evitar que index 0 sobrescriba con títulos incorrectos ---
# También prioriza el resultado con año correcto cuando hay múltiples títulos iguales
# Si disable_year_filter está activo (item sin año propio), NO aplicar filtro de año
try:
    from core.tmdb import Tmdb, ResultDictDefault
    _orig_search = Tmdb._Tmdb__search
    def patched_search(self, index_results=0, page=1):
        ret = _orig_search(self, index_results, page)
        if ret and getattr(self, 'results', None):
            # Prioridad máxima: si el ID de TMDB objetivo del bridge está en los resultados de la búsqueda de TMDB,
            # forzar la elección de ese ID (independientemente del año u otros factores de ordenamiento).
            # SÓLO si el canal especificó un año propio (disable_year_filter es False) para evitar falsos positivos con clásicos.
            target_id = getattr(_bridge_tls, 'target_tmdb_id', None) or _bridge_target_tmdb_id
            disable_yr = getattr(_bridge_tls, 'disable_year_filter', False)
            if target_id and not disable_yr:
                try:
                    for idx, r in enumerate(self.results):
                        if str(r.get('id', '')) == str(target_id):
                            self.result = ResultDictDefault(self.results[idx])
                            res_name = self.result.get('title') or self.result.get('name') or '?'
                            xbmc.log("Balandro Bridge Multi: Forzado match de TMDB al ID objetivo '%s' -> indice %d: '%s'" % (str(target_id), idx, res_name), xbmc.LOGINFO)
                            return ret
                except:
                    pass

            query = self.busqueda_texto.strip().lower()
            # Si estamos procesando un item sin año propio, NO usar filtro de año
            # → TMDB elige el match más natural/popular para ese título
            disable_yr = getattr(_bridge_tls, 'disable_year_filter', False)
            global year, showyear
            target_year = None if disable_yr else (year or showyear)
            # Recopilar todos los índices con título exacto (apoya tanto películas con 'title' como series con 'name')
            exact_matches = []
            for idx, r in enumerate(self.results):
                r_title = (r.get('title') or r.get('name') or '').strip().lower()
                r_orig_title = (r.get('original_title') or r.get('original_name') or '').strip().lower()
                if r_title == query or r_orig_title == query:
                    exact_matches.append(idx)
            if exact_matches:
                best_idx = exact_matches[0]  # Por defecto el primero exacto
                # Si estamos procesando un item sin año propio y hay homónimos en TMDB:
                # Evitar emparejarlo con estrenos muy recientes (el año actual o el año siguiente/anterior)
                # si existen versiones clásicas/antiguas con el mismo nombre.
                if disable_yr and len(exact_matches) > 1:
                    import datetime as _dt
                    current_yr = _dt.date.today().year
                    non_recent_matches = []
                    for idx in exact_matches:
                        r = self.results[idx]
                        r_date = r.get('release_date', '') or r.get('first_air_date', '') or ''
                        if r_date and len(r_date) >= 4:
                            try:
                                r_yr = int(r_date[:4])
                                if r_yr < current_yr - 1:
                                    non_recent_matches.append((idx, r_yr))
                            except:
                                pass
                    if non_recent_matches:
                        best_idx = non_recent_matches[0][0]
                        res_name = self.results[best_idx].get('title') or self.results[best_idx].get('name') or '?'
                        xbmc.log("Balandro Bridge Multi: Evitando match de estreno reciente/futuro para '%s' sin año. Elegido clásico indice %d: '%s'" % (self.busqueda_texto, best_idx, res_name), xbmc.LOGINFO)

                # Si tenemos año objetivo (y no está deshabilitado), priorizar el que coincide
                if target_year:
                    try:
                        t_yr = int(target_year)
                        for idx in exact_matches:
                            r = self.results[idx]
                            r_date = r.get('release_date', '') or r.get('first_air_date', '') or ''
                            if r_date and len(r_date) >= 4:
                                r_yr = int(r_date[:4])
                                if r_yr == t_yr or abs(r_yr - t_yr) <= 1:
                                    best_idx = idx
                                    break
                    except:
                        pass
                if best_idx != 0:
                    self.result = ResultDictDefault(self.results[best_idx])
                    res_name = self.result.get('title') or self.result.get('name') or '?'
                    xbmc.log("Balandro Bridge Multi: Corregido match de TMDB para '%s' -> indice %d: '%s'" % (self.busqueda_texto, best_idx, res_name), xbmc.LOGINFO)
        return ret
    Tmdb._Tmdb__search = patched_search
    xbmc.log('Balandro Bridge Multi: Parche de TMDB (exact title match + year priority) aplicado correctamente', xbmc.LOGINFO)
except Exception as e:
    import traceback
    xbmc.log('Balandro Bridge Multi: FALLO al aplicar parche de TMDB exact title match: ' + traceback.format_exc(), xbmc.LOGERROR)




# --- Parche 5: Import hook para envolver play() de cada canal con logging ---
import importlib
import importlib.abc
import importlib.machinery

class ChannelPlayLogger(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Intercepta la importacion de canales de Balandro y envuelve su play()"""

    def find_module(self, fullname, path=None):
        if fullname.startswith('channels.') or fullname.startswith('modules.'):
            return self
        return None

    def load_module(self, fullname):
        if fullname in sys.modules:
            mod = sys.modules[fullname]
        else:
            # Carga normal
            try:
                sys.meta_path = [f for f in sys.meta_path if not isinstance(f, ChannelPlayLogger)]
                mod = importlib.import_module(fullname)
                sys.meta_path.insert(0, self)
            except Exception:
                sys.meta_path.insert(0, self)
                raise
        # Envolver play() si existe
        if hasattr(mod, 'play') and not getattr(mod, '_bridge_play_wrapped', False):
            _orig_play = mod.play
            def _logged_play(item, _ch=fullname, _orig=_orig_play):
                ch_short = _ch.split('.')[-1]
                try:
                    result = _orig(item)
                    if isinstance(result, str) and ('Descifrar' in result or 'No Soportado' in result):
                        xbmc.log('Bridge: CANAL [' + ch_short + '] -> suprimiendo error: ' + result[:60], xbmc.LOGWARNING)
                        # Retornamos lista vacia en vez del string de error
                        # Esto hace que Balandro marque el item como erroneo sin mostrar popup
                        return []
                    elif isinstance(result, list):
                        xbmc.log('Bridge: CANAL [' + ch_short + '] -> OK ' + str(len(result)) + ' links', xbmc.LOGINFO)
                    return result
                except Exception as ex:
                    xbmc.log('Bridge: CANAL [' + ch_short + '] -> EXCEPCION: ' + str(ex), xbmc.LOGERROR)
                    return []
            mod.play = _logged_play
            mod._bridge_play_wrapped = True
        return mod

_channel_logger = ChannelPlayLogger()
sys.meta_path.insert(0, _channel_logger)
xbmc.log('Balandro Bridge Multi: ChannelPlayLogger instalado en sys.meta_path', xbmc.LOGINFO)


# Monkeypatch setResolvedUrl
_original_setResolvedUrl = xbmcplugin.setResolvedUrl
_meta_ctx = {}
_resume_time = 0.0


def _get_local_resume_seconds(tmdb_id, season, episode):
    """
    Lee el progreso de reproduccion desde la base de datos local de TMDb Helper
    (tabla simplecache), que almacena el playback_progress (%) sincronizado con Trakt.
    Retorna los segundos de progreso, o 0.0 si no hay datos.
    """
    try:
        import sqlite3 as _sqlite3
        db_path = xbmcvfs.translatePath(
            'special://profile/addon_data/plugin.video.themoviedb.helper/database_07/ItemDetails.db'
        )
        tmdb_id_int = int(tmdb_id)
        is_episode = False
        try:
            if season is not None and str(season).strip() != '' and episode is not None and str(episode).strip() != '':
                season_val = int(float(season))
                episode_val = int(float(episode))
                is_episode = True
        except ValueError:
            pass

        if is_episode:
            row_id = 'tv.%d.%d.%d' % (tmdb_id_int, season_val, episode_val)
        else:
            row_id = 'movie.%d' % tmdb_id_int

        xbmc.log('Bridge Resume: buscando en simplecache id=' + row_id, xbmc.LOGINFO)
        con = _sqlite3.connect(db_path)
        cur = con.cursor()
        cur.execute(
            'SELECT playback_progress, runtime FROM simplecache WHERE id=?',
            (row_id,)
        )
        row = cur.fetchone()
        con.close()

        if row and row[0] and float(row[0]) > 0:
            progress_pct = float(row[0])   # porcentaje 0-100
            runtime_min  = float(row[1] or 0)
            if runtime_min <= 0:
                runtime_min = 45.0 if is_episode else 90.0
            pos_secs = (progress_pct / 100.0) * runtime_min * 60.0
            xbmc.log('Bridge Resume: encontrado progress=%.2f%% runtime=%.0fmin -> %.1f segundos' % (progress_pct, runtime_min, pos_secs), xbmc.LOGINFO)
            return pos_secs
        else:
            xbmc.log('Bridge Resume: sin progreso para ' + row_id, xbmc.LOGINFO)
    except Exception as ex:
        xbmc.log('Bridge Resume: error leyendo simplecache: ' + str(ex), xbmc.LOGERROR)
    return 0.0


def patched_setResolvedUrl(handle, succeeded, listitem):
    global _resume_time
    xbmc.log("Bridge setResolvedUrl called: handle=" + str(handle) + ", succeeded=" + str(succeeded) + ", path=" + str(listitem.getPath()), xbmc.LOGINFO)
    xbmc.log("Bridge Play: _meta_ctx = " + str(_meta_ctx), xbmc.LOGINFO)
    if str(handle) == '-1' or handle == -1:
        if succeeded:
            try:
                # --- Verificar y preguntar al usuario si quiere reanudar ---
                # Buscamos el progreso en el DB local. Si hay progreso, preguntamos.
                _resume_time = 0.0
                try:
                    _saved_ctx = _load_last_player_context()
                    ctx_tmdb    = _meta_ctx.get('tmdb')    or (_saved_ctx.get('tmdb')    if _saved_ctx else '')
                    ctx_season  = _meta_ctx.get('season')  or (_saved_ctx.get('season')  if _saved_ctx else '')
                    ctx_episode = _meta_ctx.get('episode') or (_saved_ctx.get('episode') if _saved_ctx else '')
                    if ctx_tmdb:
                        pos = _get_local_resume_seconds(ctx_tmdb, ctx_season, ctx_episode)
                        if pos > 30.0:
                            # Formatear el tiempo para el dialogo en formato H:MM:SS o M:SS
                            hours = int(pos) // 3600
                            mins = (int(pos) % 3600) // 60
                            secs = int(pos) % 60
                            if hours > 0:
                                time_str = '%d:%02d:%02d' % (hours, mins, secs)
                            else:
                                time_str = '%d:%02d' % (mins, secs)

                            # Preguntar al usuario usando dialog.select para enfocar Reanudar por defecto
                            # y poder detectar cancelacion (Atras/Esc) que retorna -1 para abortar la reproduccion.
                            dialog = xbmcgui.Dialog()
                            options = [
                                'Reanudar desde %s' % time_str,
                                'Desde el principio'
                            ]
                            res = dialog.select('Reanudar reproducción', options, preselect=0)
                            if res == 0:
                                _resume_time = pos
                                xbmc.log('Bridge Play: Usuario eligio REANUDAR desde %.1fs' % pos, xbmc.LOGINFO)
                            elif res == 1:
                                xbmc.log('Bridge Play: Usuario eligio DESDE EL PRINCIPIO', xbmc.LOGINFO)
                            else:
                                xbmc.log('Bridge Play: Usuario cancelo el dialogo (res=%d). Abortando reproduccion.' % res, xbmc.LOGINFO)
                                _clear_resume_state()
                                return
                        else:
                            xbmc.log('Bridge Play: Sin progreso suficiente (pos=%.1fs)' % pos, xbmc.LOGINFO)
                except Exception as e_res:
                    xbmc.log('Bridge Play: Error en logica de resume: ' + str(e_res), xbmc.LOGERROR)

                # --- Enriquecer listitem con metadatos ---
                try:
                    ctx_tmdb   = _meta_ctx.get('tmdb')
                    ctx_imdb   = _meta_ctx.get('imdb')
                    ctx_tvdb   = _meta_ctx.get('tvdb')
                    ctx_trakt  = _meta_ctx.get('trakt')
                    ctx_season  = _meta_ctx.get('season')
                    ctx_episode = _meta_ctx.get('episode')
                    ctx_title   = _meta_ctx.get('title')
                    ctx_year    = _meta_ctx.get('year')
                    ctx_showname = _meta_ctx.get('showname')
                    ctx_showyear = _meta_ctx.get('showyear')
                    ctx_plot    = _meta_ctx.get('plot')
                    ctx_tagline = _meta_ctx.get('tagline')
                    ctx_director = _meta_ctx.get('director')

                    if not ctx_tagline or ctx_tagline == '_':
                        ctx_tagline = _get_tagline_from_db(ctx_tmdb, is_episode=bool(ctx_season and ctx_episode))
                        _meta_ctx['tagline'] = ctx_tagline

                    if not ctx_director or ctx_director == '_':
                        ctx_director = _get_director_from_db(ctx_tmdb, is_episode=bool(ctx_season and ctx_episode))
                        _meta_ctx['director'] = ctx_director

                    unique_ids = {}
                    if ctx_tmdb:  unique_ids['tmdb']  = str(ctx_tmdb)
                    if ctx_imdb:  unique_ids['imdb']  = str(ctx_imdb)
                    if ctx_tvdb:  unique_ids['tvdb']  = str(ctx_tvdb)
                    if ctx_trakt: unique_ids['trakt'] = str(ctx_trakt)
                    if unique_ids:
                        default_id = 'tmdb' if 'tmdb' in unique_ids else ('imdb' if 'imdb' in unique_ids else '')
                        _set_unique_ids(listitem, unique_ids, default_id)

                    info = {}
                    if ctx_season and ctx_episode:
                        info['mediatype'] = 'episode'
                        info['title']     = ctx_showname if ctx_showname else (ctx_title or '')
                        try: info['season']  = int(ctx_season)
                        except: pass
                        try: info['episode'] = int(ctx_episode)
                        except: pass
                        try:
                            if ctx_showyear: info['year'] = int(ctx_showyear)
                        except: pass
                        if ctx_showname: info['tvshowtitle'] = ctx_showname
                    elif ctx_title:
                        info['mediatype'] = 'movie'
                        info['title']     = ctx_title or ''
                        try:
                            if ctx_year: info['year'] = int(ctx_year)
                        except: pass
                    if ctx_plot:
                        info['plot'] = ctx_plot
                    if ctx_tagline:
                        info['tagline'] = ctx_tagline
                    if ctx_director:
                        info['director'] = ctx_director
                    if info:
                        set_listitem_info(listitem, info)
                    # Forzar el label en español para que el OSD del player lo muestre correctamente
                    if ctx_title:
                        try: listitem.setLabel(ctx_title)
                        except: pass
                except Exception as e_meta:
                    xbmc.log("Balandro Bridge Multi: error enriqueciendo listitem en handle=-1: " + str(e_meta), xbmc.LOGERROR)

                # --- Reproducir y hacer seek robusto ---
                path = listitem.getPath()
                kodi_player = xbmc.Player()
                kodi_player.play(path, listitem)
                if _resume_time and _resume_time > 0.0:
                    seek_val = _resume_time
                    _resume_time = 0.0

                    def wait_and_seek(p_obj, s_val):
                        # Esperar hasta que el player empiece (hasta 120s)
                        deadline = 2400
                        for _ in range(deadline):
                            time.sleep(0.05)
                            if p_obj.isPlaying():
                                break
                        else:
                            xbmc.log("Bridge Play: Timeout esperando playback para seek", xbmc.LOGWARNING)
                            return
                        # Reintentar el seek hasta que getTotalTime() sea valido
                        for attempt in range(10):
                            time.sleep(1.5)
                            try:
                                if not p_obj.isPlaying():
                                    xbmc.log("Bridge Play: Player ya no activo en intento %d" % (attempt+1), xbmc.LOGWARNING)
                                    break
                                total = p_obj.getTotalTime()
                                current = p_obj.getTime()
                                xbmc.log("Bridge Play: Seek intento %d current=%.1f total=%.1f target=%.1f" % (attempt+1, current, total, s_val), xbmc.LOGINFO)
                                if total > 10.0 and s_val < total:
                                    p_obj.seekTime(s_val)
                                    xbmc.log("Bridge Play: seekTime(%.1f) ejecutado" % s_val, xbmc.LOGINFO)
                                    time.sleep(1.5)
                                    new_pos = p_obj.getTime()
                                    xbmc.log("Bridge Play: posicion tras seek = %.1f" % new_pos, xbmc.LOGINFO)
                                    if new_pos >= s_val - 15.0:
                                        xbmc.log("Bridge Play: Seek exitoso!", xbmc.LOGINFO)
                                        break
                            except Exception as se:
                                xbmc.log("Bridge Play: Error en seek intento %d: %s" % (attempt+1, str(se)), xbmc.LOGWARNING)

                    threading.Thread(target=wait_and_seek, args=(kodi_player, seek_val), daemon=True).start()
            except Exception as e:
                xbmc.log("Balandro Bridge Multi: error in patched setResolvedUrl: " + str(e), xbmc.LOGERROR)
        return

    if succeeded:
        try:
            ctx_tmdb   = _meta_ctx.get('tmdb')
            ctx_imdb   = _meta_ctx.get('imdb')
            ctx_tvdb   = _meta_ctx.get('tvdb')
            ctx_trakt  = _meta_ctx.get('trakt')
            ctx_season  = _meta_ctx.get('season')
            ctx_episode = _meta_ctx.get('episode')
            ctx_title   = _meta_ctx.get('title')
            ctx_year    = _meta_ctx.get('year')
            ctx_showname = _meta_ctx.get('showname')
            ctx_showyear = _meta_ctx.get('showyear')
            ctx_plot    = _meta_ctx.get('plot')
            ctx_tagline = _meta_ctx.get('tagline')
            ctx_director = _meta_ctx.get('director')

            if not ctx_plot or ctx_plot == '_':
                ctx_plot = _get_plot_from_db(ctx_tmdb, is_episode=bool(ctx_season and ctx_episode))
                _meta_ctx['plot'] = ctx_plot

            if not ctx_tagline or ctx_tagline == '_':
                ctx_tagline = _get_tagline_from_db(ctx_tmdb, is_episode=bool(ctx_season and ctx_episode))
                _meta_ctx['tagline'] = ctx_tagline

            if not ctx_director or ctx_director == '_':
                ctx_director = _get_director_from_db(ctx_tmdb, is_episode=bool(ctx_season and ctx_episode))
                _meta_ctx['director'] = ctx_director

            unique_ids = {}
            if ctx_tmdb:  unique_ids['tmdb']  = str(ctx_tmdb)
            if ctx_imdb:  unique_ids['imdb']  = str(ctx_imdb)
            if ctx_tvdb:  unique_ids['tvdb']  = str(ctx_tvdb)
            if ctx_trakt: unique_ids['trakt'] = str(ctx_trakt)
            if unique_ids:
                default_id = 'tmdb' if 'tmdb' in unique_ids else ('imdb' if 'imdb' in unique_ids else '')
                _set_unique_ids(listitem, unique_ids, default_id)

            info = {}
            if ctx_season and ctx_episode:
                info['mediatype'] = 'episode'
                info['title']     = ctx_showname if ctx_showname else (ctx_title or '')
                try: info['season']  = int(ctx_season)
                except: pass
                try: info['episode'] = int(ctx_episode)
                except: pass
                try:
                    if ctx_showyear: info['year'] = int(ctx_showyear)
                except: pass
                if ctx_showname: info['tvshowtitle'] = ctx_showname
            elif ctx_title:
                info['mediatype'] = 'movie'
                info['title']     = ctx_title or ''
                try:
                    if ctx_year: info['year'] = int(ctx_year)
                except: pass
            if ctx_plot:
                info['plot'] = ctx_plot
            if ctx_tagline:
                info['tagline'] = ctx_tagline
            if ctx_director:
                info['director'] = ctx_director
            if info:
                set_listitem_info(listitem, info)
            # Forzar el label en español para que el OSD del player lo muestre correctamente
            if ctx_title:
                try: listitem.setLabel(ctx_title)
                except: pass
        except Exception as e:
            xbmc.log("Balandro Bridge Multi: error enriqueciendo resolved listitem: " + str(e), xbmc.LOGERROR)

    _original_setResolvedUrl(handle, succeeded, listitem)

    if not succeeded:
        def _close_error_dialog():
            for _ in range(20):
                time.sleep(0.05)
                xbmc.executebuiltin('Dialog.Close(okdialog,true)')
                xbmc.executebuiltin('Dialog.Close(error,true)')
                xbmc.executebuiltin('Dialog.Close(notification,true)')
        threading.Thread(target=_close_error_dialog, daemon=True).start()

xbmcplugin.setResolvedUrl = patched_setResolvedUrl

# Parse query arguments from original argv[2]
if sys.version_info[0] >= 3:
    from urllib.parse import parse_qs, unquote, quote
else:
    from urlparse import parse_qs
    from urllib import unquote, quote

params = parse_qs(original_argv[2].lstrip('?')) if len(original_argv) > 2 else {}
xbmc.log("Bridge Multi INITIAL PARAMS: %s" % str(params), xbmc.LOGINFO)

def get_param(name):
    val = params.get(name)
    return val[0] if val else None

def _get_title_from_db(tmdb_id, is_episode=False):
    if not tmdb_id:
        return ""
    try:
        db_dir = xbmcvfs.translatePath('special://userdata/addon_data/plugin.video.themoviedb.helper/')
        db_path = None
        if os.path.exists(db_dir):
            for item in os.listdir(db_dir):
                if item.startswith('database_') and os.path.isdir(os.path.join(db_dir, item)):
                    path = os.path.join(db_dir, item, 'ItemDetails.db')
                    if os.path.exists(path):
                        db_path = path
                        break
        if not db_path:
            db_path = os.path.join(db_dir, 'database_07', 'ItemDetails.db')
            if not os.path.exists(db_path):
                return ""
        
        import sqlite3
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        parent_id = ("tv." if is_episode else "movie.") + str(tmdb_id)
        cursor.execute("SELECT title, iso_language FROM translation WHERE parent_id = ? AND title IS NOT NULL AND title != '' AND title != '_';", (parent_id,))
        rows = cursor.fetchall()
        conn.close()
        
        if not rows:
            return ""
        
        # Primero intentamos Latino
        for t, lang in rows:
            if lang in ('es_MX', 'es'):
                return t
        # Luego Castellano
        for t, lang in rows:
            if lang in ('es_ES',):
                return t
        return ""
    except Exception as e:
        xbmc.log("Balandro Bridge Multi: Error fetching title from db: " + str(e), xbmc.LOGWARNING)
    return ""

def _get_original_title_from_db(tmdb_id, is_episode=False):
    """Obtiene el título en inglés desde la tabla translation de TMDB Helper.
    NO usa originaltitle porque puede estar en chino, árabe, etc."""
    if not tmdb_id:
        return ""
    try:
        db_dir = xbmcvfs.translatePath('special://userdata/addon_data/plugin.video.themoviedb.helper/')
        db_path = None
        if os.path.exists(db_dir):
            for item in os.listdir(db_dir):
                if item.startswith('database_') and os.path.isdir(os.path.join(db_dir, item)):
                    path = os.path.join(db_dir, item, 'ItemDetails.db')
                    if os.path.exists(path):
                        db_path = path
                        break
        if not db_path:
            db_path = os.path.join(db_dir, 'database_07', 'ItemDetails.db')
            if not os.path.exists(db_path):
                return ""

        import sqlite3
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # 1. Buscar el título en inglés en la tabla translation
        parent_id = ("tv." if is_episode else "movie.") + str(tmdb_id)
        cursor.execute(
            "SELECT title FROM translation WHERE parent_id = ? AND iso_language = 'en' AND title IS NOT NULL AND title != '' AND title != '_';",
            (parent_id,)
        )
        row = cursor.fetchone()
        if row and row[0]:
            conn.close()
            return row[0]

        # 2. Fallback: leer originaltitle de la tabla principal
        #    pero solo si es texto latino (no chino, árabe, japonés, etc.)
        table = 'tvshow' if is_episode else 'movie'
        cursor.execute(
            "SELECT originaltitle FROM %s WHERE tmdb_id = ? AND originaltitle IS NOT NULL AND originaltitle != '' AND originaltitle != '_';" % table,
            (int(tmdb_id),)
        )
        row2 = cursor.fetchone()
        conn.close()
        if row2 and row2[0]:
            orig = row2[0]
            # Verificar que sea texto latino (ASCII básico + caracteres latinos extendidos)
            latin_chars = sum(1 for c in orig if ord(c) < 0x0500)
            if latin_chars > len(orig) * 0.8:  # >80% caracteres latinos
                return orig

        # 3. Fallback final: consultar la API de TMDB directamente
        api_en = _get_english_title_from_api(tmdb_id, is_episode)
        if api_en:
            return api_en
        return ""
    except Exception as e:
        xbmc.log("Balandro Bridge Multi: Error fetching original title from db: " + str(e), xbmc.LOGWARNING)
    return ""



def _get_spanish_title_from_api(tmdb_id, is_episode=False):
    if not tmdb_id:
        return None
    try:
        try:
            import urllib.request as urllib_request
        except ImportError:
            import urllib2 as urllib_request
        import json
        
        # TMDB Helper's hardcoded API key
        api_key = 'a07324c669cac4d96789197134ce272b'
        media_type = 'tv' if is_episode else 'movie'
        url = 'https://api.themoviedb.org/3/%s/%s/translations?api_key=%s' % (media_type, tmdb_id, api_key)
        req = urllib_request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        response = urllib_request.urlopen(req, timeout=3)
        try:
            data = json.loads(response.read().decode('utf-8'))
            translations = data.get('translations', [])
            
            # Buscar Latino
            for trans in translations:
                if trans.get('iso_639_1') == 'es' and trans.get('iso_3166_1') == 'MX':
                    title_val = trans.get('data', {}).get('title') or trans.get('data', {}).get('name')
                    if title_val and title_val != '_':
                        return title_val
            # Buscar Castellano
            for trans in translations:
                if trans.get('iso_639_1') == 'es':
                    title_val = trans.get('data', {}).get('title') or trans.get('data', {}).get('name')
                    if title_val and title_val != '_':
                        return title_val
        finally:
            response.close()
    except Exception as e:
        xbmc.log("Balandro Bridge Multi: Error fetching title from TMDB API: " + str(e), xbmc.LOGWARNING)
    return None

def _get_all_spanish_titles(tmdb_id, is_episode=False):
    """Obtiene todos los títulos en español (Latino, España, etc.) desde la DB local y la API de TMDB."""
    if not tmdb_id:
        return []
    titles = []
    
    # 1. Intentar desde la base de datos local
    try:
        db_dir = xbmcvfs.translatePath('special://userdata/addon_data/plugin.video.themoviedb.helper/')
        db_path = None
        if os.path.exists(db_dir):
            for item in os.listdir(db_dir):
                if item.startswith('database_') and os.path.isdir(os.path.join(db_dir, item)):
                    path = os.path.join(db_dir, item, 'ItemDetails.db')
                    if os.path.exists(path):
                        db_path = path
                        break
        if not db_path:
            db_path = os.path.join(db_dir, 'database_07', 'ItemDetails.db')
        
        if os.path.exists(db_path):
            import sqlite3
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            parent_id = ("tv." if is_episode else "movie.") + str(tmdb_id)
            cursor.execute("SELECT title, iso_language FROM translation WHERE parent_id = ? AND title IS NOT NULL AND title != '' AND title != '_';", (parent_id,))
            rows = cursor.fetchall()
            conn.close()
            
            if rows:
                for t, lang in rows:
                    if lang.startswith('es'):
                        if t and t not in titles:
                            titles.append(t)
    except Exception as e:
        xbmc.log("Balandro Bridge Multi: Error fetching all titles from db: " + str(e), xbmc.LOGWARNING)
        
    # 2. Intentar desde la API de TMDB
    try:
        try:
            import urllib.request as urllib_request
        except ImportError:
            import urllib2 as urllib_request
        import json
        
        api_key = 'a07324c669cac4d96789197134ce272b'
        media_type = 'tv' if is_episode else 'movie'
        url = 'https://api.themoviedb.org/3/%s/%s/translations?api_key=%s' % (media_type, tmdb_id, api_key)
        req = urllib_request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        response = urllib_request.urlopen(req, timeout=3)
        try:
            data = json.loads(response.read().decode('utf-8'))
            translations = data.get('translations', [])
            for trans in translations:
                if trans.get('iso_639_1') == 'es':
                    title_val = trans.get('data', {}).get('title') or trans.get('data', {}).get('name')
                    if title_val and title_val != '_' and title_val not in titles:
                        titles.append(title_val)
        finally:
            response.close()
    except Exception as e:
        xbmc.log("Balandro Bridge Multi: Error fetching all titles from API: " + str(e), xbmc.LOGWARNING)
        
    return titles

def _get_english_title_from_api(tmdb_id, is_episode=False):
    """Obtiene el título en inglés desde la API de TMDB (para películas sin traducciones en DB local)."""
    if not tmdb_id:
        return None
    try:
        try:
            import urllib.request as urllib_request
        except ImportError:
            import urllib2 as urllib_request
        import json

        api_key = 'a07324c669cac4d96789197134ce272b'
        media_type = 'tv' if is_episode else 'movie'
        # Primero intentar el endpoint de detalles que devuelve el título en inglés directamente
        url = 'https://api.themoviedb.org/3/%s/%s?api_key=%s&language=en-US' % (media_type, tmdb_id, api_key)
        req = urllib_request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        response = urllib_request.urlopen(req, timeout=4)
        try:
            data = json.loads(response.read().decode('utf-8'))
            # Para películas: 'title' es el título en inglés cuando language=en-US
            title_val = data.get('title') or data.get('name')
            if title_val and title_val != '_':
                # Verificar que sea texto latino (no chino, árabe, etc.)
                latin_chars = sum(1 for ch in title_val if ord(ch) < 0x0500)
                if latin_chars > len(title_val) * 0.8:
                    xbmc.log("Balandro Bridge Multi: Titulo ingles via API TMDB: '%s'" % title_val, xbmc.LOGINFO)
                    return title_val
        finally:
            response.close()
    except Exception as e:
        xbmc.log("Balandro Bridge Multi: Error fetching English title from TMDB API: " + str(e), xbmc.LOGWARNING)
    return None

def _resolve_missing_spanish_titles(tmdb_id, is_episode=False):
    """Intenta obtener un título en español primero desde la DB local y luego desde la API de TMDB."""
    # 1. Intentar desde la base de datos local (más rápido, sin red)
    db_title = _get_title_from_db(tmdb_id, is_episode)
    if db_title and db_title != '_':
        return db_title
    # 2. Fallback: consultar la API de TMDB
    api_title = _get_spanish_title_from_api(tmdb_id, is_episode)
    if api_title and api_title != '_':
        return api_title
    return None


def _get_tagline_from_db(tmdb_id, is_episode=False):
    if not tmdb_id:
        return ""
    try:
        db_dir = xbmcvfs.translatePath('special://userdata/addon_data/plugin.video.themoviedb.helper/')
        db_path = None
        if os.path.exists(db_dir):
            for item in os.listdir(db_dir):
                if item.startswith('database_') and os.path.isdir(os.path.join(db_dir, item)):
                    path = os.path.join(db_dir, item, 'ItemDetails.db')
                    if os.path.exists(path):
                        db_path = path
                        break
        if not db_path:
            db_path = os.path.join(db_dir, 'database_07', 'ItemDetails.db')
            if not os.path.exists(db_path):
                return ""
        
        import sqlite3
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        tagline = ""
        # 1. Consultar tabla principal correspondiente
        if is_episode:
            # Los episodios no suelen tener tagline, pero buscamos en tvshow para el show padre
            cursor.execute("SELECT tagline FROM tvshow WHERE tmdb_id = ? AND tagline IS NOT NULL AND tagline != '' AND tagline != '_';", (int(tmdb_id),))
            row = cursor.fetchone()
            if row:
                tagline = row[0]
        else:
            cursor.execute("SELECT tagline FROM movie WHERE tmdb_id = ? AND tagline IS NOT NULL AND tagline != '' AND tagline != '_';", (int(tmdb_id),))
            row = cursor.fetchone()
            if row:
                tagline = row[0]
                
        # 2. Fallback a la tabla translation si la principal no tiene el dato
        if not tagline:
            parent_id = ("tv." if is_episode else "movie.") + str(tmdb_id)
            cursor.execute("SELECT tagline, iso_language FROM translation WHERE parent_id = ? AND tagline IS NOT NULL AND tagline != '' AND tagline != '_';", (parent_id,))
            rows = cursor.fetchall()
            if rows:
                # Priorizar variantes de español (con guion o guion bajo)
                for tag, lang in rows:
                    lang_clean = lang.replace('_', '-').lower() if lang else ''
                    if lang_clean in ('es', 'es-es', 'es-mx', 'es-ar', 'es-co', 'es-cl', 'es-pe'):
                        tagline = tag
                        break
                if not tagline:
                    tagline = rows[0][0] # Fallback primer idioma encontrado
                    
        conn.close()
        return tagline if tagline else ""
    except Exception as e:
        xbmc.log("Balandro Bridge Multi: Error fetching tagline from db: " + str(e), xbmc.LOGWARNING)
    return ""

def _get_plot_from_db(tmdb_id, is_episode=False):
    if not tmdb_id:
        return ""
    try:
        db_dir = xbmcvfs.translatePath('special://userdata/addon_data/plugin.video.themoviedb.helper/')
        db_path = None
        if os.path.exists(db_dir):
            for item in os.listdir(db_dir):
                if item.startswith('database_') and os.path.isdir(os.path.join(db_dir, item)):
                    path = os.path.join(db_dir, item, 'ItemDetails.db')
                    if os.path.exists(path):
                        db_path = path
                        break
        if not db_path:
            db_path = os.path.join(db_dir, 'database_07', 'ItemDetails.db')
            if not os.path.exists(db_path):
                return ""
        
        import sqlite3
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        plot = ""
        # 1. Consultar tabla principal correspondiente
        if is_episode:
            # Para episodios, intentamos obtener el plot del episodio concreto relacionando con tvshow_id
            # El parámetro tmdb_id que nos llega para episodio es el del show de TV
            # Como a veces no tenemos la temporada/episodio en esta firma, consultamos el plot de la serie tvshow
            # Pero si hay season y episode definidos en el contexto global, podemos intentar buscarlos
            global season, episode
            try:
                s_num = int(season) if 'season' in globals() and season else None
                e_num = int(episode) if 'episode' in globals() and episode else None
            except:
                s_num, e_num = None, None
                
            if s_num is not None and e_num is not None:
                cursor.execute("""
                    SELECT e.plot FROM episode e 
                    JOIN tvshow t ON e.tvshow_id = t.id 
                    WHERE t.tmdb_id = ? AND e.episode = ? AND e.year IS NOT NULL
                """, (int(tmdb_id), e_num)) # intentamos buscar el episodio
                row = cursor.fetchone()
                if row:
                    plot = row[0]
            
            if not plot:
                cursor.execute("SELECT plot FROM tvshow WHERE tmdb_id = ? AND plot IS NOT NULL AND plot != '' AND plot != '_';", (int(tmdb_id),))
                row = cursor.fetchone()
                if row:
                    plot = row[0]
        else:
            cursor.execute("SELECT plot FROM movie WHERE tmdb_id = ? AND plot IS NOT NULL AND plot != '' AND plot != '_';", (int(tmdb_id),))
            row = cursor.fetchone()
            if row:
                plot = row[0]
                
        # 2. Fallback a la tabla translation
        if not plot:
            parent_id = ("tv." if is_episode else "movie.") + str(tmdb_id)
            cursor.execute("SELECT plot, iso_language FROM translation WHERE parent_id = ? AND plot IS NOT NULL AND plot != '' AND plot != '_';", (parent_id,))
            rows = cursor.fetchall()
            if rows:
                for plt, lang in rows:
                    lang_clean = lang.replace('_', '-').lower() if lang else ''
                    if lang_clean in ('es', 'es-es', 'es-mx', 'es-ar', 'es-co', 'es-cl', 'es-pe'):
                        plot = plt
                        break
                if not plot:
                    plot = rows[0][0]
                    
        conn.close()
        return plot if plot else ""
    except Exception as e:
        xbmc.log("Balandro Bridge Multi: Error fetching plot from db: " + str(e), xbmc.LOGWARNING)
    return ""

def _get_director_from_db(tmdb_id, is_episode=False):
    if not tmdb_id:
        return ""
    try:
        db_dir = xbmcvfs.translatePath('special://userdata/addon_data/plugin.video.themoviedb.helper/')
        db_path = None
        if os.path.exists(db_dir):
            for item in os.listdir(db_dir):
                if item.startswith('database_') and os.path.isdir(os.path.join(db_dir, item)):
                    path = os.path.join(db_dir, item, 'ItemDetails.db')
                    if os.path.exists(path):
                        db_path = path
                        break
        if not db_path:
            db_path = os.path.join(db_dir, 'database_07', 'ItemDetails.db')
            if not os.path.exists(db_path):
                return ""
        
        import sqlite3
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        parent_id = ("tv." if is_episode else "movie.") + str(tmdb_id)
        cursor.execute("""
            SELECT p.name 
            FROM crewmember c
            JOIN person p ON c.tmdb_id = p.tmdb_id
            WHERE c.parent_id = ? AND c.role = 'Director';
        """, (parent_id,))
        rows = cursor.fetchall()
        conn.close()
        
        if not rows:
            return ""
        
        directors = [r[0] for r in rows if r[0]]
        return " / ".join(directors)
    except Exception as e:
        xbmc.log("Balandro Bridge Multi: Error fetching director from db: " + str(e), xbmc.LOGWARNING)
    return ""

action = get_param('action')
view = get_param('view')
url = get_param('url')
title = get_param('title')
year = get_param('year')
season = get_param('season')
episode = get_param('episode')
showname = get_param('showname')
showyear = get_param('showyear')

# Normalize: TMDB Helper serializa Python None como la cadena literal "None"
_NULL_STRINGS = {'None', 'none', 'null', 'NULL', '_'}
def _clean(v):
    if not v or v in _NULL_STRINGS:
        return ''
    return v

# Alternativos e IDs
title_es = _clean(get_param('title_es'))
title_lat = _clean(get_param('title_lat'))
title_en = _clean(get_param('title_en'))
title_orig = _clean(get_param('title_orig'))
tmdb_id  = get_param('tmdb')
imdb_id  = get_param('imdb')
tvdb_id  = get_param('tvdb')
trakt_id = get_param('trakt')

# Fix: detectar título corrupto (ej: "????" cuando comillas tipográficas rompen la URL)
# Si el título principal contiene solo signos de interrogación, lo descartamos
def _is_corrupt_title(t):
    if not t:
        return False
    clean_t = t.replace('?', '').replace(' ', '').strip()
    return len(clean_t) == 0  # Solo tenía '?' y espacios

if title and _is_corrupt_title(title):
    xbmc.log("Balandro Bridge Multi: Titulo principal corrupto '%s', descartando" % title, xbmc.LOGWARNING)
    title = ''

# Fix: enriquecer title_en y title_orig desde la DB local si llegan vacios
# (Los parámetros title_en y title_orig son eliminados del JSON del player para evitar
# truncamiento por '&' en el nombre, por lo que siempre llegan vacíos aquí)
if tmdb_id and (not title_en or not title_orig):
    is_ep_tmp = bool(season and episode)
    db_orig = _get_original_title_from_db(tmdb_id, is_ep_tmp)
    if db_orig:
        if not title_orig:
            title_orig = db_orig
        if not title_en:
            title_en = db_orig
        xbmc.log("Balandro Bridge Multi: Titulo original recuperado de DB: '%s'" % db_orig, xbmc.LOGINFO)

plot_gen = _clean(get_param('plot'))
plot_lat = _clean(get_param('plot_lat'))
plot_es  = _clean(get_param('plot_es'))
tagline_gen = _clean(get_param('tagline'))
tagline_lat = _clean(get_param('tagline_lat'))
tagline_es  = _clean(get_param('tagline_es'))
director = _clean(get_param('director'))

# Si no tenemos ningún título en español (o es idéntico al inglés/original) pero sí tenemos el ID de TMDB, intentamos resolverlo
if tmdb_id and (not title_es or title_es == '_' or title_es == title_en or title_es == title_orig or not title_lat or title_lat == '_' or title_lat == title_en or title_lat == title_orig):
    is_ep = bool(season and episode)
    sp_title = _resolve_missing_spanish_titles(tmdb_id, is_ep)
    if sp_title:
        _eng_candidates = set(t for t in [title_en, title_orig, title] if t and t != '_')
        if sp_title not in _eng_candidates:
            title_es = sp_title
            title_lat = sp_title

is_ep = bool(season and episode)

# Intentar resolver la sinopsis y el eslogan en español directamente desde la DB local de forma prioritaria
# para evitar la pérdida de comas y signos de puntuación causada por el parseo de parámetros URL de Kodi.
if tmdb_id:
    db_plot = _get_plot_from_db(tmdb_id, is_ep)
    if db_plot:
        plot_lat = db_plot
        plot_es = db_plot

    db_tagline = _get_tagline_from_db(tmdb_id, is_ep)
    if db_tagline:
        tagline_lat = db_tagline
        tagline_es = db_tagline

# Si director está vacío pero tenemos tmdb_id, intentamos resolver el director desde la DB local
if tmdb_id and not director:
    db_director = _get_director_from_db(tmdb_id, is_ep)
    if db_director:
        director = db_director

# Fallback de idioma para Título (Latino -> Castellano -> original/genérico)
best_title = title_lat
# Si el título latino es idéntico al inglés, al original o al título genérico del canal (posiblemente en inglés),
# Y el castellano es diferente, preferimos castellano
_english_candidates = set(t for t in [title_en, title_orig, title] if t and t != '_')
if best_title and _english_candidates and best_title in _english_candidates:
    if title_es and title_es != '_' and title_es not in _english_candidates:
        best_title = title_es
    elif not title_es or title_es == '_':
        # No hay castellano tampoco; intentar resolver dinámicamente
        pass

if not best_title or best_title == '_':
    best_title = title_es
if not best_title or best_title == '_' or best_title in _english_candidates:
    # Si solo tenemos el título en inglés, intentamos resolver el castellano dinámicamente
    if tmdb_id and (not title_es or title_es == '_'):
        is_ep = bool(season and episode)
        sp_title2 = _resolve_missing_spanish_titles(tmdb_id, is_ep)
        if sp_title2:
            best_title = sp_title2
if not best_title or best_title == '_':
    best_title = title

# Fallback de idioma para Plot/Sinopsis (Latino -> Castellano -> vacío, NO inglés)
best_plot = plot_lat
# Si la sinopsis latina es idéntica a la genérica (posiblemente en inglés), preferimos la castellana si existe
if best_plot and plot_gen and best_plot == plot_gen:
    if plot_es and plot_es != plot_gen:
        best_plot = plot_es

if not best_plot:
    best_plot = plot_es
# Sin sinopsis en español: dejamos vacío (no mandamos el texto en inglés)
if not best_plot:
    best_plot = ''

# Fallback de idioma para Eslogan (Latino -> Castellano -> vacío, NO inglés)
best_tagline = tagline_lat
# Si el eslogan latino es idéntico al genérico (posiblemente en inglés), preferimos el castellano si existe
if best_tagline and tagline_gen and best_tagline == tagline_gen:
    if tagline_es and tagline_es != tagline_gen:
        best_tagline = tagline_es

if not best_tagline:
    best_tagline = tagline_es
# Sin eslogan en español: dejamos vacío (no mandamos el texto en inglés)
if not best_tagline:
    best_tagline = ''

_meta_ctx.update({
    'tmdb':     tmdb_id  or '',
    'imdb':     imdb_id  or '',
    'tvdb':     tvdb_id  or '',
    'trakt':    trakt_id or '',
    'title':    best_title or '',
    'year':     year     or '',
    'season':   season   or '',
    'episode':  episode  or '',
    'showname': showname or '',
    'showyear': showyear or '',
    'plot':     best_plot or '',
    'tagline':  best_tagline or '',
    'director': director or '',
})
xbmc.log(
    "Balandro Bridge Multi: META resuelto -> title=%r | plot=%r | tagline=%r | director=%r" % (
        best_title, best_plot[:80] if best_plot else '', best_tagline, director
    ), xbmc.LOGINFO
)

def _inject_meta_into_items(links, matched_item):
    try:
        title_val = _meta_ctx.get('title')
        plot_val = _meta_ctx.get('plot')
        tagline_val = _meta_ctx.get('tagline')
        director_val = _meta_ctx.get('director')
        
        if matched_item:
            if not hasattr(matched_item, 'infoLabels') or matched_item.infoLabels is None:
                matched_item.infoLabels = {}
            if title_val:
                matched_item.title = title_val
                matched_item.infoLabels['title'] = title_val
            if plot_val:
                matched_item.plot = plot_val
                matched_item.infoLabels['plot'] = plot_val
            if tagline_val:
                matched_item.infoLabels['tagline'] = tagline_val
            if director_val:
                matched_item.infoLabels['director'] = director_val
                
        if links:
            for lnk in links:
                if not hasattr(lnk, 'infoLabels') or lnk.infoLabels is None:
                    lnk.infoLabels = {}
                if title_val:
                    # Guardamos el título para reproducción
                    lnk.infoLabels['title'] = title_val
                if plot_val:
                    lnk.plot = plot_val
                    lnk.infoLabels['plot'] = plot_val
                if tagline_val:
                    lnk.infoLabels['tagline'] = tagline_val
                if director_val:
                    lnk.infoLabels['director'] = director_val
    except Exception as e:
        xbmc.log("Balandro Bridge Multi: Error injecting metadata into items: " + str(e), xbmc.LOGWARNING)

TMDB_PLAYERS_PATH = xbmcvfs.translatePath(
    'special://userdata/addon_data/plugin.video.themoviedb.helper/players'
)

def _migrate_players_add_plot():
    try:
        if not os.path.exists(TMDB_PLAYERS_PATH):
            return
        for fname in os.listdir(TMDB_PLAYERS_PATH):
            if not (fname.endswith('.json') or fname.endswith('.json.disabled')):
                continue
            fpath = os.path.join(TMDB_PLAYERS_PATH, fname)
            try:
                modified = False
                with open(fpath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                for key in ['play_movie', 'play_episode']:
                    if key in data and isinstance(data[key], list) and len(data[key]) > 0:
                        url_str = data[key][0]
                        if isinstance(url_str, str) and 'plugin://plugin.video.balandro.bridge.multi/' in url_str:
                            if 'plot=' not in url_str:
                                url_str += '&plot={plot}'
                                modified = True
                            if 'plot_lat=' not in url_str:
                                url_str += '&plot_lat={es-MX_plot}&plot_es={es-ES_plot}'
                                modified = True
                            if 'tagline=' not in url_str:
                                url_str += '&tagline={tagline}'
                                modified = True
                            if 'tagline_lat=' not in url_str:
                                url_str += '&tagline_lat={es-MX_tagline}&tagline_es={es-ES_tagline}'
                                modified = True
                            if 'director=' not in url_str:
                                url_str += '&director={director}'
                                modified = True
                            # Eliminar title_en y title_orig que causan truncamiento con '&' en titulos
                            # como "Thelma & Louise". TMDb Helper no URL-encoda estos valores al sustituirlos.
                            if '&title_en={en_title}' in url_str:
                                url_str = url_str.replace('&title_en={en_title}', '')
                                modified = True
                            if '&title_orig={originaltitle}' in url_str:
                                url_str = url_str.replace('&title_orig={originaltitle}', '')
                                modified = True
                            if '&title_en={en_showname}' in url_str:
                                url_str = url_str.replace('&title_en={en_showname}', '')
                                modified = True
                            if '&title_orig={original_name}' in url_str:
                                url_str = url_str.replace('&title_orig={original_name}', '')
                                modified = True
                            if modified:
                                data[key][0] = url_str
                        
                        # Actualizar matcher para incluir .+ como comodin (asegura compatibilidad con titulos resueltos)
                        if len(data[key]) > 1 and isinstance(data[key][1], dict):
                            matcher = data[key][1]
                            if 'title' in matcher and isinstance(matcher['title'], str):
                                if '|.+)' not in matcher['title'] and matcher['title'].endswith(')'):
                                    matcher['title'] = matcher['title'][:-1] + '|.+)'
                                    data[key][1] = matcher
                                    modified = True
                
                if modified:
                    with open(fpath, 'w', encoding='utf-8') as f:
                        json.dump(data, f, indent=4, ensure_ascii=False)
                    xbmc.log("Balandro Bridge Multi: Migrated player %s to include plot/tagline params" % fname, xbmc.LOGINFO)
            except Exception as e_mig:
                xbmc.log("Balandro Bridge Multi: Error migrating player %s: %s" % (fname, str(e_mig)), xbmc.LOGWARNING)
    except Exception as e:
        xbmc.log("Balandro Bridge Multi: Player migration error: " + str(e), xbmc.LOGWARNING)

_migrate_players_add_plot()



def _ensure_balandro_multi_player():
    """Crea automáticamente el player (1)BalandroMulti.json en TMDB Helper si no existe."""
    try:
        if not os.path.exists(TMDB_PLAYERS_PATH):
            os.makedirs(TMDB_PLAYERS_PATH)
        player_file = os.path.join(TMDB_PLAYERS_PATH, '(1)BalandroMulti.json')
        if os.path.exists(player_file):
            return  # Ya existe, nada que hacer
        player_data = {
            "name": "(1)Balandro Multi-Búsqueda",
            "plugin": "plugin.video.balandro.bridge.multi",
            "priority": 100,
            "is_resolvable": "true",
            "assert": {
                "play_movie": ["title", "year"],
                "play_episode": ["showname", "season", "episode"]
            },
            "play_movie": [
                # NOTA: NO incluir {originaltitle} ni {en_title} en la URL porque pueden contener
                # el caracter '&' (ej: "Thelma & Louise") que truncaria el valor del parametro.
                # En su lugar usamos solo titulos en español y el tmdb_id para resolver el titulo completo.
                "plugin://plugin.video.balandro.bridge.multi/?url=plugin://plugin.video.balandro/?action=search"
                "&title={es-MX_title}&year={year}"
                "&title_es={es-ES_title}&title_lat={es-MX_title}"
                "&tmdb={tmdb}&imdb={imdb}&tvdb={tvdb}&trakt={trakt}"
                "&plot={plot}&plot_lat={es-MX_plot}&plot_es={es-ES_plot}"
                "&tagline={tagline}&tagline_lat={es-MX_tagline}&tagline_es={es-ES_tagline}"
                "&director={director}",
                {
                    "title": "(?i)^({es-ES_title}|{es-MX_title}|{en_title}|{originaltitle}|.+)",
                    "year": "{year}"
                }
            ],
            "play_episode": [
                "plugin://plugin.video.balandro.bridge.multi/?url=plugin://plugin.video.balandro/?action=search"
                "&title={es-MX_showname}&season={season}&episode={episode}"
                "&showname={showname}&showyear={showyear}"
                "&title_es={es-ES_showname}&title_lat={es-MX_showname}"
                "&tmdb={tmdb}&imdb={imdb}&tvdb={tvdb}&trakt={trakt}"
                "&plot={plot}&plot_lat={es-MX_plot}&plot_es={es-ES_plot}"
                "&tagline={tagline}&tagline_lat={es-MX_tagline}&tagline_es={es-ES_tagline}"
                "&director={director}",
                {
                    "title": "(?i)^({es-ES_showname}|{es-MX_showname}|{en_showname}|{original_name}|.+)",
                    "season": "{season}",
                    "episode": "{episode}"
                }
            ]
        }
        with open(player_file, 'w', encoding='utf-8') as f:
            json.dump(player_data, f, indent=4, ensure_ascii=False)
        xbmc.log("Balandro Bridge Multi: Player (1)BalandroMulti.json creado automaticamente.", xbmc.LOGINFO)
    except Exception as e:
        xbmc.log("Balandro Bridge Multi: Error al crear (1)BalandroMulti.json: " + str(e), xbmc.LOGWARNING)

_ensure_balandro_multi_player()


try:
    _profile_dir = xbmcvfs.translatePath('special://profile/addon_data/plugin.video.balandro.bridge.multi')
except AttributeError:
    _profile_dir = xbmc.translatePath('special://profile/addon_data/plugin.video.balandro.bridge.multi')

if not os.path.exists(_profile_dir):
    try: os.makedirs(_profile_dir)
    except: pass

LAST_PLAYER_TMPFILE = os.path.join(_profile_dir, 'balandro_bridge_last_player.json')
SEARCH_CACHE_FILE = os.path.join(_profile_dir, 'search_cache.json')

def set_listitem_info(listitem, info):
    try:
        if hasattr(listitem, 'getVideoInfoTag'):
            info_tag = listitem.getVideoInfoTag()
            if 'mediatype' in info: info_tag.setMediaType(info['mediatype'])
            if 'title' in info: info_tag.setTitle(info['title'])
            if 'tvshowtitle' in info:
                try: info_tag.setTvShowTitle(info['tvshowtitle'])
                except: pass
            if 'season' in info:
                try: info_tag.setSeason(int(info['season']))
                except: pass
            if 'episode' in info:
                try: info_tag.setEpisode(int(info['episode']))
                except: pass
            if 'year' in info:
                try: info_tag.setYear(int(info['year']))
                except: pass
            if 'plot' in info: info_tag.setPlot(info['plot'])
            if 'tagline' in info:
                try: info_tag.setTagline(info['tagline'])
                except: pass
            if 'director' in info:
                try:
                    if hasattr(info_tag, 'setDirectors'):
                        dirs = [d.strip() for d in info['director'].split('/') if d.strip()]
                        info_tag.setDirectors(dirs)
                    elif hasattr(info_tag, 'setDirector'):
                        info_tag.setDirector(info['director'])
                except: pass
    except Exception as e:
        xbmc.log("Balandro Bridge Multi Debug: error setting video info tag: " + str(e), xbmc.LOGERROR)
    try:
        listitem.setInfo('video', info)
    except Exception as e:
        xbmc.log("Balandro Bridge Multi Debug: error setting video info: " + str(e), xbmc.LOGERROR)

def _set_unique_ids(listitem, unique_ids, default_id=''):
    if not unique_ids: return
    try:
        if hasattr(listitem, 'getVideoInfoTag'):
            listitem.getVideoInfoTag().setUniqueIDs(unique_ids, default_id)
            return
    except: pass
    try: listitem.setUniqueIDs(unique_ids, default_id)
    except: pass

def _clear_resume_state():
    try:
        state_file = os.path.join(_profile_dir, 'resume_state.json')
        if os.path.exists(state_file): os.remove(state_file)
    except: pass

def _save_last_player_context(player_file, title, year, season, episode,
                              showname, showyear, title_es, title_lat,
                              title_en, title_orig,
                              p_tmdb='', p_imdb='', p_tvdb='', p_trakt='', p_plot='', p_tagline='', p_director=''):
    try:
        data = {
            'player_file': player_file,
            'title':       title      or '',
            'year':        year       or '',
            'season':      season     or '',
            'episode':     episode    or '',
            'showname':    showname   or '',
            'showyear':    showyear   or '',
            'title_es':    title_es   or '',
            'title_lat':   title_lat  or '',
            'title_en':    title_en   or '',
            'title_orig':  title_orig or '',
            'tmdb':        p_tmdb     or '',
            'imdb':        p_imdb     or '',
            'tvdb':        p_tvdb     or '',
            'trakt':       p_trakt    or '',
            'plot':        p_plot     or '',
            'tagline':     p_tagline  or '',
            'director':    p_director or '',
        }
        with open(LAST_PLAYER_TMPFILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
    except: pass


def _load_last_player_context():
    try:
        if os.path.exists(LAST_PLAYER_TMPFILE):
            with open(LAST_PLAYER_TMPFILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except: pass
    return None

def _find_player_file_for_channel(channel_name, is_episode):
    key = 'play_episode' if is_episode else 'play_movie'
    try:
        for fname in sorted(os.listdir(TMDB_PLAYERS_PATH)):
            if not fname.endswith('.json') or fname.endswith('.disabled'):
                continue
            fpath = os.path.join(TMDB_PLAYERS_PATH, fname)
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                url_list = data.get(key, [])
                if not url_list or not isinstance(url_list, list):
                    continue
                url_str = unquote(url_list[0])
                if 'plugin://plugin.video.balandro/?' in url_str:
                    raw_b64 = url_str.split('plugin://plugin.video.balandro/?')[1].split('&')[0].split('%')[0]
                    try:
                        decoded = base64.b64decode(raw_b64).decode('utf-8')
                        if '"channel": "' + channel_name.lower() + '"' in decoded.lower():
                            return fname
                    except Exception as e:
                        xbmc.log("Balandro Bridge Multi Debug: _find_player_file_for_channel dec error: " + str(e), xbmc.LOGERROR)
            except Exception:
                continue
    except Exception:
        pass
    return None

def _get_fallback_player_file(player_file, is_episode):
    key = 'play_episode' if is_episode else 'play_movie'
    fpath = os.path.join(TMDB_PLAYERS_PATH, player_file)
    try:
        with open(fpath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        fb = data.get('fallback', {})
        fb_val = fb.get(key, '').strip()
        if fb_val:
            return fb_val.split()[0]
    except Exception:
        pass
    return None

def _player_display_name(player_file):
    name = player_file.replace('.json', '').replace('.disabled', '')
    m = re.match(r'^\(\d+\)(.+)', name)
    name = m.group(1) if m else name
    name = re.sub(r'-(Series|Movies?)$', '', name, flags=re.IGNORECASE)
    return name

# extract_langs_info eliminado — el bridge busca dinámicamente en todos los idiomas

def _load_tmdb_players():
    players = []
    try:
        def _num_key(fn):
            m = re.match(r'^\((\d+)\)', fn)
            return int(m.group(1)) if m else 9999

        for fname in sorted(os.listdir(TMDB_PLAYERS_PATH), key=_num_key):
            if not (fname.endswith('.json') or fname.endswith('.json.disabled')):
                continue
            if fname.startswith('(1)BalandroMulti'):
                continue
            fpath = os.path.join(TMDB_PLAYERS_PATH, fname)
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                has_movie   = 'play_movie'   in data
                has_episode = 'play_episode' in data
                if not has_movie and not has_episode:
                    continue
                players.append({
                    'filename': fname,
                    'name':     data.get('name', fname.replace('.json', '').replace('.disabled', '')),
                    'plugin':   data.get('plugin', ''),
                    'priority': data.get('priority', 0),
                    'has_movie':   has_movie,
                    'has_episode': has_episode,
                    'fallback':    data.get('fallback', {}),
                    'disabled':    fname.endswith('.disabled'),
                })
            except Exception:
                continue
    except Exception:
        pass
    return players

def _player_type_tag(p):
    tags = []
    if p['disabled']:
        tags.append('[COLOR red]DESACTIVADO[/COLOR]')
    if p['has_movie']:
        tags.append('[COLOR deepskyblue]Pelicula[/COLOR]')
    if p['has_episode']:
        tags.append('[COLOR gold]Serie[/COLOR]')
    return '  '.join(tags)

def _add_player_item(p, pos=None):
    label = p['name']
    if pos is not None:
        label = '[COLOR grey]{}.[/COLOR] {}'.format(pos, label)
    tag   = _player_type_tag(p)
    if tag:
        label += '   ' + tag

    li = xbmcgui.ListItem(label=label)
    li.setProperty('IsPlayable', 'false')

    lines = []
    if p['disabled']:
        lines.append('[COLOR red]Este player está inactivo y será ignorado en las búsquedas.[/COLOR]')
    fb = p.get('fallback', {})
    if fb:
        fb_cleaned = []
        for val in fb.values():
            clean_val = re.sub(r'\.json.*', '', str(val))
            fb_cleaned.append(clean_val)
        fb_str = ', '.join(fb_cleaned)
        lines.append('Fallback: ' + fb_str)
    set_listitem_info(li, {'plot': '\n'.join(lines), 'title': p['name']})

    raw = p['filename'].replace('.json', '').replace('.disabled', '')
    raw = re.sub(r'^\(\d+\)', '', raw)
    raw = re.sub(r'-(Series|Movies?)$', '', raw, flags=re.IGNORECASE)
    ch_id = raw.lower()
    thumb_dir = os.path.join(balandro_path, 'resources', 'media', 'channels', 'thumb')
    thumb = ''
    for ext in ('jpg', 'png'):
        candidate = os.path.join(thumb_dir, ch_id + '.' + ext)
        if os.path.exists(candidate):
            thumb = candidate
            break
    if not thumb:
        thumb = os.path.join(balandro_path, 'icon.png')
    li.setArt({'thumb': thumb, 'icon': thumb})

    item_url = 'plugin://plugin.video.balandro.bridge.multi/?view=player_options&player=' + quote(p['filename'])
    xbmcplugin.addDirectoryItem(handle, item_url, li, False)

def show_player_options(player_filename):
    dialog = xbmcgui.Dialog()
    is_disabled = player_filename.endswith('.disabled')
    status_label = 'Activar Player' if is_disabled else 'Desactivar Player'
    options = [status_label, 'Mover Posición', 'Eliminar Player']
    sel = dialog.select('Opciones de Player: ' + player_filename, options)
    if sel == 0:
        _toggle_player_status(player_filename)
    elif sel == 1:
        _move_player_position(player_filename)
    elif sel == 2:
        if dialog.yesno('Eliminar Player', '¿Estas seguro de que deseas eliminar permanentemente este player y reconfigurar la cadena de fallbacks?'):
            _delete_player(player_filename)
    xbmcplugin.endOfDirectory(handle, succeeded=True)

def _move_player_position(player_filename):
    dialog = xbmcgui.Dialog()
    players = _load_tmdb_players()
    
    target_player = None
    for p in players:
        if p['filename'] == player_filename:
            target_player = p
            break
            
    if not target_player:
        dialog.ok('Balandro Bridge Multi', 'No se encontro el player especificado.')
        return

    is_movie = target_player['has_movie']
    cat_players = [p for p in players if (is_movie and p['has_movie']) or (not is_movie and p['has_episode'])]
    
    def _num_key(p_obj):
        m = re.match(r'^\((\d+)\)', p_obj['filename'])
        return int(m.group(1)) if m else 9999
    cat_players.sort(key=_num_key)
    
    current_index = -1
    for idx, p in enumerate(cat_players):
        if p['filename'] == player_filename:
            current_index = idx
            break
            
    if current_index == -1:
        dialog.ok('Balandro Bridge Multi', 'Error al localizar la posicion del player.')
        return
        
    options = []
    for idx, p in enumerate(cat_players):
        name_clean = p['name']
        if idx == current_index:
            options.append('[COLOR gold]Posicion {} -> {} (Actual)[/COLOR]'.format(idx + 1, name_clean))
        else:
            options.append('Posicion {} -> {}'.format(idx + 1, name_clean))
            
    sel = dialog.select('Mover a la posicion:', options)
    if sel < 0 or sel == current_index:
        return
        
    moved_player = cat_players.pop(current_index)
    cat_players.insert(sel, moved_player)
    
    new_index = 1
    for p in cat_players:
        old_fn = p['filename']
        ch_name_part = re.sub(r'^\(\d+\)', '', old_fn.replace('.disabled', ''))
        new_fn = '({}){}'.format(new_index, ch_name_part)
        if old_fn.endswith('.disabled'):
            new_fn += '.disabled'
        
        ch_internal_part = re.sub(r'^\(\d+\)', '', p['name'])
        new_name = '({}){}'.format(new_index, ch_internal_part)
        
        p_path_old = os.path.join(TMDB_PLAYERS_PATH, old_fn)
        p_path_new = os.path.join(TMDB_PLAYERS_PATH, new_fn)
        
        try:
            with open(p_path_old, 'r', encoding='utf-8') as f:
                p_data = json.load(f)
            
            p_data['name'] = new_name
            
            with open(p_path_new, 'w', encoding='utf-8') as f:
                json.dump(p_data, f, indent=4, ensure_ascii=False)
            
            if old_fn != new_fn and os.path.exists(p_path_old):
                os.remove(p_path_old)
        except Exception as e:
            dialog.ok('Balandro Bridge Multi', 'Error al renombrar player: {}'.format(str(e)))
            return
            
        new_index += 1

    _rebuild_fallbacks(is_movie)
    dialog.ok('Balandro Bridge Multi', 'Player movido y reordenado correctamente.')
    xbmc.executebuiltin("Container.Refresh")

def _rebuild_fallbacks(is_movie):
    all_players = _load_tmdb_players()
    cat_players = [p for p in all_players if (is_movie and p['has_movie']) or (not is_movie and p['has_episode'])]
    
    def _num_key(p_obj):
        m = re.match(r'^\((\d+)\)', p_obj['filename'])
        return int(m.group(1)) if m else 9999
    cat_players.sort(key=_num_key)
    
    active_cat_players = [p for p in cat_players if not p['disabled']]
    key = "play_movie" if is_movie else "play_episode"
    
    for i, p in enumerate(cat_players):
        p_path = os.path.join(TMDB_PLAYERS_PATH, p['filename'])
        try:
            with open(p_path, 'r', encoding='utf-8') as f:
                p_data = json.load(f)
                
            if p['disabled']:
                if "fallback" in p_data:
                    del p_data["fallback"]
            else:
                next_active = None
                p_index_in_active = active_cat_players.index(p) if p in active_cat_players else -1
                if p_index_in_active != -1 and p_index_in_active < len(active_cat_players) - 1:
                    next_active = active_cat_players[p_index_in_active + 1]
                    
                if next_active:
                    if "fallback" not in p_data:
                        p_data["fallback"] = {}
                    p_data["fallback"][key] = "{} {}".format(next_active['filename'], key)
                else:
                    if "fallback" in p_data:
                        if key in p_data["fallback"]:
                            del p_data["fallback"][key]
                        if not p_data["fallback"]:
                            del p_data["fallback"]
                            
            with open(p_path, 'w', encoding='utf-8') as f:
                json.dump(p_data, f, indent=4, ensure_ascii=False)
        except Exception:
            pass

def _toggle_player_status(player_filename):
    dialog = xbmcgui.Dialog()
    old_path = os.path.join(TMDB_PLAYERS_PATH, player_filename)
    
    if not os.path.exists(old_path):
        dialog.ok('Balandro Bridge Multi', 'No se encuentra el archivo del player.')
        return
        
    is_disabled = player_filename.endswith('.disabled')
    new_filename = player_filename.replace('.disabled', '') if is_disabled else player_filename + '.disabled'
    new_path = os.path.join(TMDB_PLAYERS_PATH, new_filename)
    
    try:
        os.rename(old_path, new_path)
        is_movie = "-Series" not in new_filename
        _rebuild_fallbacks(is_movie)
        
        status_txt = 'activado' if is_disabled else 'desactivado'
        dialog.ok('Balandro Bridge Multi', 'Player {} con éxito.'.format(status_txt))
        xbmc.executebuiltin("Container.Refresh")
    except Exception as e:
        dialog.ok('Balandro Bridge Multi', 'Error al cambiar estado del player: ' + str(e))

def _delete_player(player_filename):
    dialog = xbmcgui.Dialog()
    players = _load_tmdb_players()
    
    target_player = None
    for p in players:
        if p['filename'] == player_filename:
            target_player = p
            break
            
    if not target_player:
        dialog.ok('Balandro Bridge Multi', 'No se encontro el player especificado.')
        return

    is_movie = target_player['has_movie']
    dest_path = os.path.join(TMDB_PLAYERS_PATH, player_filename)
    try:
        if os.path.exists(dest_path):
            os.remove(dest_path)
    except Exception as e:
        dialog.ok('Balandro Bridge Multi', 'Error al eliminar el archivo: ' + str(e))
        return

    remaining_players = _load_tmdb_players()
    cat_players = [p for p in remaining_players if (is_movie and p['has_movie']) or (not is_movie and p['has_episode'])]

    def _num_key(p_obj):
        m = re.match(r'^\((\d+)\)', p_obj['filename'])
        return int(m.group(1)) if m else 9999
    cat_players.sort(key=_num_key)

    new_index = 1
    for p in cat_players:
        old_fn = p['filename']
        ch_name_part = re.sub(r'^\(\d+\)', '', old_fn.replace('.disabled', ''))
        new_fn = '({}){}'.format(new_index, ch_name_part)
        if old_fn.endswith('.disabled'):
            new_fn += '.disabled'
        
        ch_internal_part = re.sub(r'^\(\d+\)', '', p['name'])
        new_name = '({}){}'.format(new_index, ch_internal_part)

        p_path_old = os.path.join(TMDB_PLAYERS_PATH, old_fn)
        p_path_new = os.path.join(TMDB_PLAYERS_PATH, new_fn)

        try:
            with open(p_path_old, 'r', encoding='utf-8') as f:
                p_data = json.load(f)
            
            p_data['name'] = new_name
            
            with open(p_path_new, 'w', encoding='utf-8') as f:
                json.dump(p_data, f, indent=4, ensure_ascii=False)
            
            if old_fn != new_fn and os.path.exists(p_path_old):
                os.remove(p_path_old)
        except Exception as e:
            dialog.ok('Balandro Bridge Multi', 'Error al renombrar player: ' + str(e))
            return
            
        new_index += 1

    _rebuild_fallbacks(is_movie)
    dialog.ok('Balandro Bridge Multi', 'Player eliminado y reordenado correctamente.')
    xbmc.executebuiltin("Container.Refresh")

def check_and_run_migration():
    legacy_players = []
    try:
        if not os.path.exists(TMDB_PLAYERS_PATH):
            return
        for fname in os.listdir(TMDB_PLAYERS_PATH):
            if not (fname.endswith('.json') or fname.endswith('.json.disabled')):
                continue
            fpath = os.path.join(TMDB_PLAYERS_PATH, fname)
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if data.get('plugin') == 'plugin.video.balandro.bridge':
                    legacy_players.append((fpath, data))
            except:
                pass
    except:
        pass

    if not legacy_players:
        return

    dialog = xbmcgui.Dialog()
    msg = (
        "Se han detectado %d reproductores antiguos de Balandro Bridge.\n\n"
        "¿Deseas migrarlos automáticamente para que funcionen con Balandro Bridge Multi?"
    ) % len(legacy_players)
    
    if dialog.yesno("Balandro Bridge Multi - Migración", msg, yeslabel="Sí", nolabel="No"):
        p_dialog = xbmcgui.DialogProgress()
        p_dialog.create('Migrando reproductores', 'Iniciando migración...')
        
        migrated_count = 0
        total = len(legacy_players)
        
        for idx, (fpath, data) in enumerate(legacy_players):
            if p_dialog.iscanceled():
                break
            p_dialog.update(int((idx / float(total)) * 100), 'Migrando: %s' % os.path.basename(fpath))
            
            try:
                data['plugin'] = 'plugin.video.balandro.bridge.multi'
                
                if 'play_movie' in data and isinstance(data['play_movie'], list) and len(data['play_movie']) > 0:
                    url = data['play_movie'][0]
                    if 'plugin://plugin.video.balandro.bridge/' in url:
                        data['play_movie'][0] = url.replace('plugin://plugin.video.balandro.bridge/', 'plugin://plugin.video.balandro.bridge.multi/')
                
                if 'play_episode' in data and isinstance(data['play_episode'], list) and len(data['play_episode']) > 0:
                    url = data['play_episode'][0]
                    if 'plugin://plugin.video.balandro.bridge/' in url:
                        data['play_episode'][0] = url.replace('plugin://plugin.video.balandro.bridge/', 'plugin://plugin.video.balandro.bridge.multi/')
                
                with open(fpath, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=4, ensure_ascii=False)
                migrated_count += 1
            except Exception as e:
                xbmc.log("Balandro Bridge Multi MIGRATION ERROR on %s: %s" % (fpath, str(e)), xbmc.LOGERROR)
                
        p_dialog.close()
        dialog.ok("Balandro Bridge Multi - Migración", "Se han migrado %d reproductores con éxito." % migrated_count)

def show_player_manager_home():
    check_and_run_migration()
    balandro_icon = os.path.join(balandro_path, 'icon.png')

    categories = [
        ('create', '[B]Crear nuevo Player[/B]',
         'Configura y agrega un nuevo Player para TMDB Helper',
         balandro_icon, False),
        ('movie',  '[B]Players de Peliculas[/B]',
         'Players que soportan reproduccion de peliculas',
         balandro_icon, True),
        ('tvshow', '[B]Players de Series[/B]',
         'Players que soportan reproduccion de episodios de series',
         balandro_icon, True),
    ]

    for cat_id, cat_label, cat_desc, icon, is_folder in categories:
        li = xbmcgui.ListItem(label=cat_label)
        li.setArt({'thumb': icon, 'icon': icon})
        set_listitem_info(li, {'plot': cat_desc, 'title': cat_label})
        li.setProperty('IsPlayable', 'false')
        cat_url = 'plugin://plugin.video.balandro.bridge.multi/?view=' + cat_id
        xbmcplugin.addDirectoryItem(handle, cat_url, li, is_folder)

    xbmcplugin.setPluginCategory(handle, 'Balandro Bridge Multi - Player Manager')
    xbmcplugin.setContent(handle, 'files')
    xbmcplugin.endOfDirectory(handle, succeeded=True)

def _get_balandro_channels_for_movies():
    ch_path = os.path.join(balandro_path, 'channels')
    channels = []
    try:
        for fname in sorted(os.listdir(ch_path)):
            if not fname.endswith('.json') or fname.startswith('__'):
                continue
            ch_id = fname[:-5]
            json_path = os.path.join(ch_path, fname)
            py_path = os.path.join(ch_path, ch_id + '.py')
            if not os.path.exists(py_path):
                continue
            try:
                with open(json_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if not data.get('active', False) or not data.get('searchable', False):
                    continue
                stypes = data.get('search_types', [])
                if 'movie' in stypes or 'all' in stypes:
                    channels.append({
                        'id': ch_id,
                        'name': data.get('name', ch_id),
                        'plot': data.get('notes', ''),
                        'thumbnail': data.get('thumbnail', '')
                    })
            except Exception:
                continue
    except Exception:
        pass
    return channels

def _get_balandro_channels_for_series():
    ch_path = os.path.join(balandro_path, 'channels')
    channels = []
    try:
        for fname in sorted(os.listdir(ch_path)):
            if not fname.endswith('.json') or fname.startswith('__'):
                continue
            ch_id = fname[:-5]
            json_path = os.path.join(ch_path, fname)
            py_path = os.path.join(ch_path, ch_id + '.py')
            if not os.path.exists(py_path):
                continue
            try:
                with open(json_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if not data.get('active', False) or not data.get('searchable', False):
                    continue
                stypes = data.get('search_types', [])
                if 'tvshow' in stypes or 'all' in stypes:
                    channels.append({
                        'id': ch_id,
                        'name': data.get('name', ch_id),
                        'plot': data.get('notes', ''),
                        'thumbnail': data.get('thumbnail', '')
                    })
            except Exception:
                continue
    except Exception:
        pass
    return channels

def show_creation_menu():
    dialog = xbmcgui.Dialog()
    options = ['Crear player para Pelicula', 'Crear player para Serie']
    sel = dialog.select('Tipo de Player a Crear', options)
    if sel == 0:
        _create_movie_player()
    elif sel == 1:
        _create_series_player()
    xbmcplugin.endOfDirectory(handle, succeeded=True)

def _create_series_player():
    dialog = xbmcgui.Dialog()
    channels = _get_balandro_channels_for_series()
    if not channels:
        dialog.ok('Balandro Bridge Multi', 'No se encontraron canales de Balandro aptos para Series.')
        return

    ch_names = [ch['name'] for ch in channels]
    sel_ch_idx = dialog.select('Selecciona Canal de Balandro', ch_names)
    if sel_ch_idx < 0:
        return
    ch = channels[sel_ch_idx]

    channel_id = ch['id']
    try:
        tipo_channel = 'channels.' if os.path.exists(os.path.join(balandro_path, 'channels', channel_id + '.py')) else 'modules.'
        canal = __import__(tipo_channel + channel_id, fromlist=[''])
    except Exception as e:
        dialog.ok('Balandro Bridge Multi', 'Error al cargar el canal: ' + str(e))
        return

    current_item = Item(channel=channel_id)
    if hasattr(canal, 'mainlist_series'):
        current_item.action = 'mainlist_series'
    elif hasattr(canal, 'mainlist'):
        current_item.action = 'mainlist'
    else:
        dialog.ok('Balandro Bridge Multi', 'El canal no tiene un punto de entrada estandar (mainlist).')
        return

    selected_search_item = None

    while True:
        try:
            func = getattr(canal, current_item.action)
            with silenced_dialogs():
                items = func(current_item)
        except Exception as e:
            dialog.ok('Balandro Bridge Multi', 'Error al leer el menu del canal: ' + str(e))
            return

        if not items or not isinstance(items, list):
            dialog.ok('Balandro Bridge Multi', 'El menu del canal esta vacio o no es valido.')
            return

        valid_items = []
        for it in items:
            if not it.action:
                continue
            if it.action in ('acciones', 'configurar_proxies'):
                continue
            valid_items.append(it)

        if not valid_items:
            dialog.ok('Balandro Bridge Multi', 'No se encontraron opciones navegables en este nivel.')
            return

        options_labels = []
        for it in valid_items:
            clean_label = re.sub(r'\[/?COLOR.*?\]|\[/?B\]', '', it.title)
            if it.action == 'search':
                clean_label = '[COLOR gold][BUSQUEDA] [/COLOR]' + clean_label
            options_labels.append(clean_label)

        sel = dialog.select('Navegar en ' + ch['name'] + ' (Elige el boton de Buscar)', options_labels)
        if sel < 0:
            return

        chosen = valid_items[sel]
        if chosen.action == 'search':
            selected_search_item = chosen
            break
        else:
            current_item = chosen

    if not selected_search_item:
        return

    showname_placeholder = '{showname}'
    match_regex = '(?i)^({es-ES_showname}|{es-MX_showname}|{en_showname}|{original_name}|.+)'

    existing_players = _load_tmdb_players()
    series_players = [p for p in existing_players if p['has_episode']]
    
    fallback_file = ''

    max_num = 0
    for p in series_players:
        m = re.match(r'^\((\d+)\)', p['filename'])
        if m:
            max_num = max(max_num, int(m.group(1)))
    new_num = max_num + 1
    new_priority = 200

    player_name = '({}){}-Series'.format(new_num, ch['name'])
    new_filename = '({}){}-Series.json'.format(new_num, ch['id'].capitalize())

    inner_dict = {
        "action": selected_search_item.action,
        "category": selected_search_item.category if hasattr(selected_search_item, 'category') else ch['name'],
        "channel": selected_search_item.channel,
        "extra": selected_search_item.extra if hasattr(selected_search_item, 'extra') else "tvshows",
        "fanart": selected_search_item.fanart if hasattr(selected_search_item, 'fanart') else "",
        "infoLabels": {},
        "plot": selected_search_item.plot if hasattr(selected_search_item, 'plot') else ch['plot'],
        "search_type": selected_search_item.search_type if hasattr(selected_search_item, 'search_type') else "tvshow",
        "thumbnail": selected_search_item.thumbnail if hasattr(selected_search_item, 'thumbnail') else "",
        "title": selected_search_item.title
    }

    try:
        inner_json = json.dumps(inner_dict, separators=(',', ':'))
        b64_str = base64.b64encode(inner_json.encode('utf-8')).decode('utf-8')
    except Exception as e:
        dialog.ok('Balandro Bridge Multi', 'Error al procesar base64: ' + str(e))
        return

    play_url = 'plugin://plugin.video.balandro.bridge.multi/?url=plugin://plugin.video.balandro/?' + b64_str
    play_url += '&showname=' + showname_placeholder + '&showyear={showyear}&season={season}&episode={episode}'
    # NOTA: NO incluir title_en ni title_orig — el '&' en titulos originales trunca el parametro
    play_url += '&title_es={es-ES_showname}&title_lat={es-MX_showname}'
    play_url += '&tmdb={tmdb}&imdb={imdb}&tvdb={tvdb}&trakt={trakt}'
    play_url += '&plot={plot}&plot_lat={es-MX_plot}&plot_es={es-ES_plot}'
    play_url += '&tagline={tagline}&tagline_lat={es-MX_tagline}&tagline_es={es-ES_tagline}'
    play_url += '&director={director}'

    player_data = {
        "name": player_name,
        "plugin": "plugin.video.balandro.bridge.multi",
        "priority": new_priority,
        "is_resolvable": "true",
        "assert": {
            "play_episode": ["showname", "season", "episode"]
        },
        "play_episode": [
            play_url,
            {
                "title": match_regex
            }
        ],
        "is_folder": "false"
    }

    if fallback_file:
        player_data["fallback"] = {
            "play_episode": "{} play_episode".format(fallback_file)
        }

    dest_path = os.path.join(TMDB_PLAYERS_PATH, new_filename)
    try:
        with open(dest_path, 'w', encoding='utf-8') as f:
            json.dump(player_data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        dialog.ok('Balandro Bridge Multi', 'Error al escribir el archivo: ' + str(e))
        return

    linked = False
    if series_players:
        last_prev_player = None
        highest_prev_num = -1
        for p in series_players:
            m = re.match(r'^\((\d+)\)', p['filename'])
            if m:
                num = int(m.group(1))
                if num < new_num and num > highest_prev_num:
                    highest_prev_num = num
                    last_prev_player = p

        if last_prev_player:
            prev_path = os.path.join(TMDB_PLAYERS_PATH, last_prev_player['filename'])
            try:
                with open(prev_path, 'r', encoding='utf-8') as f:
                    prev_data = json.load(f)
                
                if "fallback" not in prev_data:
                    prev_data["fallback"] = {}
                
                prev_data["fallback"]["play_episode"] = "{} play_episode".format(new_filename)
                
                with open(prev_path, 'w', encoding='utf-8') as f:
                    json.dump(prev_data, f, indent=4, ensure_ascii=False)
                linked = True
            except Exception:
                pass

    msg = 'Player de Serie "{}" creado con exito.'.format(player_name)
    if linked:
        msg += '\nEncadenado desde el player de serie anterior.'
    dialog.ok('Balandro Bridge Multi', msg)

def _create_movie_player():
    dialog = xbmcgui.Dialog()
    channels = _get_balandro_channels_for_movies()
    if not channels:
        dialog.ok('Balandro Bridge Multi', 'No se encontraron canales de Balandro aptos para Peliculas.')
        return

    ch_names = [ch['name'] for ch in channels]
    sel_ch_idx = dialog.select('Selecciona Canal de Balandro', ch_names)
    if sel_ch_idx < 0:
        return
    ch = channels[sel_ch_idx]

    channel_id = ch['id']
    try:
        tipo_channel = 'channels.' if os.path.exists(os.path.join(balandro_path, 'channels', channel_id + '.py')) else 'modules.'
        canal = __import__(tipo_channel + channel_id, fromlist=[''])
    except Exception as e:
        dialog.ok('Balandro Bridge Multi', 'Error al cargar el canal: ' + str(e))
        return

    current_item = Item(channel=channel_id)
    if hasattr(canal, 'mainlist_pelis'):
        current_item.action = 'mainlist_pelis'
    elif hasattr(canal, 'mainlist'):
        current_item.action = 'mainlist'
    else:
        dialog.ok('Balandro Bridge Multi', 'El canal no tiene un punto de entrada estandar (mainlist).')
        return

    selected_search_item = None

    while True:
        try:
            func = getattr(canal, current_item.action)
            with silenced_dialogs():
                items = func(current_item)
        except Exception as e:
            dialog.ok('Balandro Bridge Multi', 'Error al leer el menu del canal: ' + str(e))
            return

        if not items or not isinstance(items, list):
            dialog.ok('Balandro Bridge Multi', 'El menu del canal esta vacio o no es valido.')
            return

        valid_items = []
        for it in items:
            if not it.action:
                continue
            if it.action in ('acciones', 'configurar_proxies'):
                continue
            valid_items.append(it)

        if not valid_items:
            dialog.ok('Balandro Bridge Multi', 'No se encontraron opciones navegables en este nivel.')
            return

        options_labels = []
        for it in valid_items:
            clean_label = re.sub(r'\[/?COLOR.*?\]|\[/?B\]', '', it.title)
            if it.action == 'search':
                clean_label = '[COLOR chartreuse][BUSQUEDA] [/COLOR]' + clean_label
            options_labels.append(clean_label)

        sel = dialog.select('Navegar en ' + ch['name'] + ' (Elige el boton de Buscar)', options_labels)
        if sel < 0:
            return

        chosen = valid_items[sel]
        if chosen.action == 'search':
            selected_search_item = chosen
            break
        else:
            current_item = chosen

    if not selected_search_item:
        return

    # El bridge busca automáticamente en Castellano -> Latino -> Inglés/Original
    # No es necesario preguntar idioma de búsqueda ni filtro
    match_regex = '(?i)^({es-ES_title}|{es-MX_title}|{en_title}|{originaltitle}|.+)'

    use_year = True
    
    existing_players = _load_tmdb_players()
    movie_players = [p for p in existing_players if p['has_movie']]
    
    fallback_file = ''

    max_num = 0
    for p in movie_players:
        m = re.match(r'^\((\d+)\)', p['filename'])
        if m:
            max_num = max(max_num, int(m.group(1)))
    new_num = max_num + 1
    new_priority = 200

    player_name = '({}){}'.format(new_num, ch['name'])
    new_filename = '({}){}.json'.format(new_num, ch['id'].capitalize())

    inner_dict = {
        "action": selected_search_item.action,
        "category": selected_search_item.category if hasattr(selected_search_item, 'category') else ch['name'],
        "channel": selected_search_item.channel,
        "extra": selected_search_item.extra if hasattr(selected_search_item, 'extra') else "movies",
        "fanart": selected_search_item.fanart if hasattr(selected_search_item, 'fanart') else "",
        "infoLabels": {},
        "plot": selected_search_item.plot if hasattr(selected_search_item, 'plot') else ch['plot'],
        "search_type": selected_search_item.search_type if hasattr(selected_search_item, 'search_type') else "movie",
        "thumbnail": selected_search_item.thumbnail if hasattr(selected_search_item, 'thumbnail') else "",
        "title": selected_search_item.title
    }

    try:
        inner_json = json.dumps(inner_dict, separators=(',', ':'))
        b64_str = base64.b64encode(inner_json.encode('utf-8')).decode('utf-8')
    except Exception as e:
        dialog.ok('Balandro Bridge Multi', 'Error al procesar base64: ' + str(e))
        return

    play_url = 'plugin://plugin.video.balandro.bridge.multi/?url=plugin://plugin.video.balandro/?' + b64_str
    play_url += '&title={es-MX_title}'
    # NOTA: NO incluir title_en ni title_orig — el '&' en titulos originales trunca el parametro
    play_url += '&title_es={es-ES_title}&title_lat={es-MX_title}'
    play_url += '&tmdb={tmdb}&imdb={imdb}&tvdb={tvdb}&trakt={trakt}'
    play_url += '&plot={plot}&plot_lat={es-MX_plot}&plot_es={es-ES_plot}'
    play_url += '&tagline={tagline}&tagline_lat={es-MX_tagline}&tagline_es={es-ES_tagline}'
    play_url += '&director={director}'
    if use_year:
        play_url += '&year={year}'

    player_data = {
        "name": player_name,
        "plugin": "plugin.video.balandro.bridge.multi",
        "priority": new_priority,
        "is_resolvable": "true",
        "assert": {
            "play_movie": ["title"]
        },
        "play_movie": [
            play_url,
            {
                "title": match_regex
            }
        ],
        "is_folder": "false"
    }

    if use_year:
        player_data["assert"]["play_movie"].append("year")
        player_data["play_movie"][1]["year"] = "{year}"

    if fallback_file:
        player_data["fallback"] = {
            "play_movie": "{} play_movie".format(fallback_file)
        }

    dest_path = os.path.join(TMDB_PLAYERS_PATH, new_filename)
    try:
        with open(dest_path, 'w', encoding='utf-8') as f:
            json.dump(player_data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        dialog.ok('Balandro Bridge Multi', 'Error al escribir el archivo: ' + str(e))
        return

    linked = False
    if movie_players:
        last_prev_player = None
        highest_prev_num = -1
        for p in movie_players:
            m = re.match(r'^\((\d+)\)', p['filename'])
            if m:
                num = int(m.group(1))
                if num < new_num and num > highest_prev_num:
                    highest_prev_num = num
                    last_prev_player = p

        if last_prev_player:
            prev_path = os.path.join(TMDB_PLAYERS_PATH, last_prev_player['filename'])
            try:
                with open(prev_path, 'r', encoding='utf-8') as f:
                    prev_data = json.load(f)
                
                if "fallback" not in prev_data:
                    prev_data["fallback"] = {}
                
                prev_data["fallback"]["play_movie"] = "{} play_movie".format(new_filename)
                
                with open(prev_path, 'w', encoding='utf-8') as f:
                    json.dump(prev_data, f, indent=4, ensure_ascii=False)
                linked = True
            except Exception:
                pass

    msg = 'Player "{}" creado con exito.'.format(player_name)
    if linked:
        msg += '\nEncadenado desde el player anterior.'
    dialog.ok('Balandro Bridge Multi', msg)

def verify_all_players_channels(category):
    dialog = xbmcgui.Dialog()
    p_dialog = xbmcgui.DialogProgress()
    p_dialog.create('Verificando canales', 'Cargando players...')
    
    all_players = _load_tmdb_players()
    if category == 'movie':
        players = [p for p in all_players if p['has_movie']]
    elif category == 'tvshow':
        players = [p for p in all_players if p['has_episode']]
    else:
        players = all_players
        
    if not players:
        p_dialog.close()
        dialog.ok('Balandro Bridge Multi', 'No hay players para verificar.')
        return
        
    invalids = []
    total = len(players)
    
    for idx, p in enumerate(players):
        if p_dialog.iscanceled():
            break
        p_dialog.update(int((idx / float(total)) * 100), 'Verificando: {}'.format(p['name']))
        
        fpath = os.path.join(TMDB_PLAYERS_PATH, p['filename'])
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            key = 'play_episode' if p['has_episode'] else 'play_movie'
            url_list = data.get(key, [])
            if not url_list or not isinstance(url_list, list):
                invalids.append((p['name'], 'El archivo del reproductor está dañado o no es válido'))
                continue
                
            url_str = unquote(url_list[0])
            if 'plugin://plugin.video.balandro/?' not in url_str:
                invalids.append((p['name'], 'El archivo del reproductor está dañado o no es válido'))
                continue
                
            raw_b64 = url_str.split('plugin://plugin.video.balandro/?')[1].split('&')[0]
            raw_b64 = unquote(raw_b64).strip()
            
            missing_padding = len(raw_b64) % 4
            if missing_padding:
                raw_b64 += '=' * (4 - missing_padding)
                
            try:
                decoded = base64.b64decode(raw_b64).decode('utf-8', errors='replace')
                inner = json.loads(decoded)
            except Exception:
                invalids.append((p['name'], 'El archivo del reproductor está dañado o no es válido'))
                continue
                
            ch = inner.get('channel')
            if not ch:
                invalids.append((p['name'], 'El archivo del reproductor está dañado o no es válido'))
                continue
                
            channel_file = os.path.join(balandro_path, 'channels', ch + '.py')
            module_file = os.path.join(balandro_path, 'modules', ch + '.py')
            
            json_channel_file = os.path.join(balandro_path, 'channels', ch + '.json')
            json_module_file = os.path.join(balandro_path, 'modules', ch + '.json')
            
            py_exists = os.path.exists(channel_file) or os.path.exists(module_file)
            json_exists = os.path.exists(json_channel_file) or os.path.exists(json_module_file)
            
            if not py_exists:
                if json_exists:
                    invalids.append((p['name'], 'Este canal aparece en Balandro pero esta cerrado'))
                else:
                    invalids.append((p['name'], 'Este canal no existe en Balandro'))
        except Exception:
            invalids.append((p['name'], 'El archivo del reproductor está dañado o no es válido'))
            
    p_dialog.close()
    
    if invalids:
        report = []
        for name, reason in invalids:
            report.append('[COLOR red]• {}[/COLOR]: {}'.format(name, reason))
        dialog.select('Canales NO Encontrados o Rotos ({})'.format(len(invalids)), report)
    else:
        dialog.ok('Balandro Bridge Multi', '¡Todos los reproductores vinculan a canales de Balandro activos!')

def show_channel_list(filter_category=None):
    all_players = _load_tmdb_players()

    if filter_category == 'movie':
        players = [p for p in all_players if p['has_movie']]
    elif filter_category == 'tvshow':
        players = [p for p in all_players if p['has_episode']]
    else:
        players = all_players

    cat_titles = {
        'all':    'Todos los players',
        'movie':  'Players de Peliculas',
        'tvshow': 'Players de Series',
    }
    cat_title = cat_titles.get(filter_category or 'all', 'Players')

    if not players:
        dialog = xbmcgui.Dialog()
        dialog.ok('Balandro Bridge Multi', 'No se encontraron players para esta categoria.')
        xbmcplugin.endOfDirectory(handle, succeeded=True)
        return

    summary_li = xbmcgui.ListItem(label='[COLOR grey]-- {} player(s) encontrado(s) --[/COLOR]'.format(len(players)))
    summary_li.setProperty('IsPlayable', 'false')
    xbmcplugin.addDirectoryItem(handle, '', summary_li, False)

    verify_url = 'plugin://plugin.video.balandro.bridge.multi/?action=verify_channels&category={}'.format(filter_category or 'all')
    verify_li = xbmcgui.ListItem(
        label='[COLOR green][B]Verificar existencia de canales en Balandro[/B][/COLOR]',
        label2='Comprueba si los canales reales siguen existiendo'
    )
    verify_li.setProperty('IsPlayable', 'false')
    verify_li.setArt({'thumb': os.path.join(balandro_path, 'icon.png'), 'icon': os.path.join(balandro_path, 'icon.png')})
    xbmcplugin.addDirectoryItem(handle, verify_url, verify_li, False)

    def _sort_key(p):
        m = re.match(r'^\((\d+)\)', p['filename'])
        num = int(m.group(1)) if m else 9999
        return (num, p['filename'])
    players.sort(key=_sort_key)

    for pos, p in enumerate(players, 1):
        _add_player_item(p)

    xbmcplugin.setPluginCategory(handle, 'Balandro Bridge Multi - ' + cat_title)
    xbmcplugin.setContent(handle, 'files')
    xbmcplugin.endOfDirectory(handle, succeeded=True)

def _offer_continue_search(ctx):
    return False

    while True:
        next_player = _get_fallback_player_file(current_player, is_ep)
        if not next_player:
            return False

        next_name = _player_display_name(next_player)
        if not os.path.exists(os.path.join(TMDB_PLAYERS_PATH, next_player)):
            return False

        ans = dialog.yesno(
            'Balandro Bridge Multi',
            'Ningun enlace funciono en [B]{}[/B].\n\n'
            '[COLOR chartreuse]Seguir buscando más enlaces?[/COLOR]'.format(
                _player_display_name(current_player)
            )
        )
        if not ans:
            return False

        matched = None
        links = None
        temp_player = next_player

        while temp_player:
            temp_name = _player_display_name(temp_player)
            dialog.notification(
                'Balandro Bridge Multi',
                'Buscando en [COLOR chartreuse]{}[/COLOR]...'.format(temp_name),
                xbmcgui.NOTIFICATION_INFO, 2500, False
            )

            matched, links = _search_on_player(
                temp_player,
                ctx.get('title', ''),    ctx.get('year', ''),
                ctx.get('season', ''),   ctx.get('episode', ''),
                ctx.get('showname', ''), ctx.get('showyear', ''),
                ctx.get('title_es', ''), ctx.get('title_lat', ''),
                ctx.get('title_en', ''), ctx.get('title_orig', '')
            )

            if matched and links:
                next_player = temp_player
                break
            else:
                temp_player = _get_fallback_player_file(temp_player, is_ep)
                if temp_player and not os.path.exists(os.path.join(TMDB_PLAYERS_PATH, temp_player)):
                    temp_player = None

        if matched and links:
            ctx['player_file'] = next_player
            _save_last_player_context(
                next_player,
                ctx.get('title', ''),    ctx.get('year', ''),
                ctx.get('season', ''),   ctx.get('episode', ''),
                ctx.get('showname', ''), ctx.get('showyear', ''),
                ctx.get('title_es', ''), ctx.get('title_lat', ''),
                ctx.get('title_en', ''), ctx.get('title_orig', ''),
                ctx.get('tmdb', ''),     ctx.get('imdb', ''),
                ctx.get('tvdb', ''),     ctx.get('trakt', ''),
                ctx.get('plot', ''),     ctx.get('tagline', ''),
                ctx.get('director', '')
            )

            _meta_ctx.update({
                'tmdb':     ctx.get('tmdb', ''),
                'imdb':     ctx.get('imdb', ''),
                'tvdb':     ctx.get('tvdb', ''),
                'trakt':    ctx.get('trakt', ''),
                'title':    ctx.get('title', ''),
                'year':     ctx.get('year', ''),
                'season':   ctx.get('season', ''),
                'episode':  ctx.get('episode', ''),
                'showname': ctx.get('showname', ''),
                'showyear': ctx.get('showyear', ''),
            })

            is_torrent = False
            if links:
                for lnk in links:
                    server = getattr(lnk, 'server', '').lower() if hasattr(lnk, 'server') else ''
                    url_lnk = getattr(lnk, 'url', '').lower() if hasattr(lnk, 'url') else ''
                    if 'torrent' in server or 'torrent' in url_lnk or 'magnet:' in url_lnk or 'elementum' in url_lnk or 'elementum' in server:
                        is_torrent = True
                        break

            _orig_pf = platformtools.play_fake
            active_handle = '-1'

            started = False
            while True:
                play_fake_intercepted = [False]
                def patched_play_fake(resuelto=False):
                    if not resuelto:
                        play_fake_intercepted[0] = True
                    else:
                        _orig_pf(resuelto)

                platformtools.play_fake = patched_play_fake

                try:
                    sys.argv[1] = active_handle
                    _inject_meta_into_items(links, matched)
                    platformtools.play_from_itemlist(links, matched)
                finally:
                    platformtools.play_fake = _orig_pf

                started = False
                if not play_fake_intercepted[0]:
                    max_seconds = 90 if is_torrent else 8
                    max_loops = int(max_seconds * 2)
                    progress_seen = False
                    progress_inactive_count = 0
                    p_dialog = None

                    try:
                        for _ in range(max_loops):
                            xbmc.sleep(500)
                            if xbmc.Player().isPlaying():
                                started = True
                                break

                            if is_torrent:
                                progress_active = False
                                if not p_dialog:
                                    progress_active = (
                                        xbmc.getCondVisibility('Window.IsActive(progressdialog)') or
                                        xbmc.getCondVisibility('Window.IsActive(10101)') or
                                        xbmc.getCondVisibility('Window.IsActive(10151)') or
                                        xbmc.getCondVisibility('Window.IsVisible(progressdialog)')
                                    )
                                if progress_active:
                                    progress_seen = True
                                    progress_inactive_count = 0
                                elif progress_seen:
                                    if p_dialog is None:
                                        p_dialog = True
                                        xbmcgui.Dialog().notification('Balandro Bridge Multi', 'Cargando enlaces...', xbmcgui.NOTIFICATION_INFO, 2500)
                                    progress_inactive_count += 1
                                    if progress_inactive_count >= 6:
                                        break
                    finally:
                        pass

                if started:
                    load_error_detected = False
                    played_secs = 0.0
                    for _mon in range(240):
                        xbmc.sleep(500)
                        if xbmc.Player().isPlaying():
                            try:
                                played_secs = xbmc.Player().getTime()
                            except Exception:
                                pass
                            continue
                        has_kodi_error = (
                            xbmc.getCondVisibility('Window.IsActive(okdialog)') or
                            xbmc.getCondVisibility('Window.IsActive(error)') or
                            xbmc.getCondVisibility('Window.IsVisible(okdialog)')
                        )
                        if has_kodi_error or played_secs < 3.0:
                            load_error_detected = True
                        break

                    if not load_error_detected:
                        return True

                    xbmc.executebuiltin('Dialog.Close(okdialog,true)')
                    xbmc.executebuiltin('Dialog.Close(error,true)')
                    xbmcgui.Dialog().notification(
                        'Balandro Bridge Multi',
                        '[COLOR gold]Error de carga[/COLOR] — Elige otro servidor',
                        xbmcgui.NOTIFICATION_WARNING, 3500, False
                    )
                    xbmc.sleep(400)
                    continue

                if play_fake_intercepted[0]:
                    break

                if is_torrent:
                    continue
                else:
                    break

            current_player = next_player
        else:
            dialog.notification(
                'Balandro Bridge Multi',
                'No se encontraron enlaces en ningun canal restante.',
                xbmcgui.NOTIFICATION_INFO, 3000, False
            )
            return False

def _load_last_player_context():
    try:
        if os.path.exists(LAST_PLAYER_TMPFILE):
            with open(LAST_PLAYER_TMPFILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except: pass
    return None

class silenced_dialogs:
    _noop = staticmethod(lambda *a, **kw: None)
    _functions = ['dialog_ok', 'dialog_notification', 'dialog_yesno', 'dialog_select', 'dialog_progress', 'dialog_progress_bg', 'dialog_input']
    def __enter__(self):
        self._originals = {}
        for name in self._functions:
            if hasattr(platformtools, name):
                self._originals[name] = getattr(platformtools, name)
                setattr(platformtools, name, self._noop)
        return self
    def __exit__(self, *_):
        for name, orig in self._originals.items():
            setattr(platformtools, name, orig)

def clean_title(title_str):
    if not title_str: return ""
    import unicodedata
    title_str = re.sub(r'\[COLOR [^\]]*\]', '', title_str.replace('[/COLOR]', ''))
    title_str = unicodedata.normalize('NFKD', title_str)
    title_str = title_str.encode('ascii', 'ignore').decode('utf-8')
    title_str = title_str.lower()
    title_str = re.sub(r'[^a-z0-9 ]', '', title_str)
    return re.sub(r'\s+', ' ', title_str).strip()

def match_title(result_title, search_title, all_titles=None):
    clean_r = clean_title(result_title)
    clean_s = clean_title(search_title)
    if clean_r == clean_s: return True
    clean_r_noyear = re.sub(r'\b(19|20)\d{2}\b', '', clean_r).strip()
    clean_r_noyear = re.sub(r'\s+', ' ', clean_r_noyear)
    if clean_r_noyear == clean_s: return True
    tags = ['1080p', '720p', '4k', '2k', 'hd', 'rip', 'brrip', 'dvdrip', 'bluray', 'webrip', 'castellano', 'latino', 'subtitulado', 'dual', 'mp3', 'aac', 'x264', 'h264', 'x265', 'h265']
    pat = r'\b(' + '|'.join(tags) + r')\b'
    clean_r_notags = re.sub(pat, '', clean_r_noyear).strip()
    clean_r_notags = re.sub(r'\s+', ' ', clean_r_notags)
    if clean_r_notags == clean_s: return True

    titles_to_check = all_titles if all_titles else [search_title]
    valid_words = set()
    for t in titles_to_check:
        if t:
            valid_words.update(clean_title(t).split())

    allowed_extra = {'pelicula', 'completa', 'movie', 'full', 'the', 'y', 'and', 'o', 'or'}
    valid_words.update(allowed_extra)

    result_words = clean_r_notags.split()
    if result_words and all(w in valid_words for w in result_words):
        real_search_words = set(clean_s.split()) - allowed_extra
        if real_search_words and all(w in result_words for w in real_search_words):
            return True

    if len(clean_s) <= 4: return False
    if clean_s in clean_r_notags:
        ratio = len(clean_s) / float(len(clean_r_notags))
        if ratio >= 0.6: return True
    if clean_r_notags in clean_s:
        ratio = len(clean_r_notags) / float(len(clean_s))
        r_wc = len(clean_r_notags.split())
        s_wc = len(clean_s.split())
        if ratio >= 0.6 and r_wc >= 0.8 * s_wc:
            return True
    return False

def _precheck_links(it):
    try:
        ch_tmp = it.channel
        act_tmp = it.action
        p_tmp = os.path.join(config.get_runtime_path(), 'channels', ch_tmp + '.py')
        t_tmp = 'channels.' if os.path.exists(p_tmp) else 'modules.'
        c_tmp = __import__(t_tmp + ch_tmp, fromlist=[''])
        lnk = getattr(c_tmp, act_tmp)(it)
        return lnk if lnk and isinstance(lnk, list) and len(lnk) > 0 else None
    except Exception as e:
        xbmc.log("Balandro Bridge Multi PRECHECK ERROR: " + str(e), xbmc.LOGERROR)
        return None

def run_action_silent(item):
    channel_name = item.channel
    action_name = item.action
    path = os.path.join(config.get_runtime_path(), 'channels', channel_name + ".py")
    tipo_channel = 'channels.' if os.path.exists(path) else 'modules.'
    canal = __import__(tipo_channel + channel_name, fromlist=[''])
    func = getattr(canal, action_name)
    with silenced_dialogs():
        try: return func(item)
        except: return None

def find_episode(item, season, episode):
    try: season_num = int(season)
    except: season_num = 1
    try: episode_num = int(episode)
    except: episode_num = 1

    res = run_action_silent(item)
    if not res or not isinstance(res, list): return None

    for it in res:
        it_season = None
        if hasattr(it, 'contentSeason') and it.contentSeason is not None:
            try: it_season = int(it.contentSeason)
            except: pass
        if it_season is None: it_season = getattr(it, 'infoLabels', {}).get('season')

        it_episode = None
        if hasattr(it, 'contentEpisodeNumber') and it.contentEpisodeNumber is not None:
            try: it_episode = int(it.contentEpisodeNumber)
            except: pass
        if it_episode is None: it_episode = getattr(it, 'infoLabels', {}).get('episode')

        if it_episode == episode_num:
            if season_num and it_season and it_season != season_num: continue
            return it

        title_clean = clean_title(it.title)
        if f'{season_num}x{episode_num:02d}' in title_clean or f'{season_num}x{episode_num}' in title_clean: return it
        if f's{season_num:02d}e{episode_num:02d}' in title_clean or f's{season_num}e{episode_num}' in title_clean: return it

    matched_sea = None
    for it in res:
        it_season = None
        if hasattr(it, 'contentSeason') and it.contentSeason is not None:
            try: it_season = int(it.contentSeason)
            except: pass
        if it_season is None: it_season = getattr(it, 'infoLabels', {}).get('season')

        if it_season == season_num:
            matched_sea = it
            break

        title_clean = clean_title(it.title)
        if (f'temporada {season_num}' in title_clean or f't{season_num}' in title_clean
                or f' {season_num}' in title_clean or title_clean == str(season_num)):
            matched_sea = it
            break

    if matched_sea:
        res_episodes = run_action_silent(matched_sea)
        if res_episodes and isinstance(res_episodes, list):
            for it in res_episodes:
                it_episode = None
                if hasattr(it, 'contentEpisodeNumber') and it.contentEpisodeNumber is not None:
                    try: it_episode = int(it.contentEpisodeNumber)
                    except: pass
                if it_episode is None: it_episode = getattr(it, 'infoLabels', {}).get('episode')

                if it_episode == episode_num: return it
                title_clean = clean_title(it.title)
                if f'x{episode_num:02d}' in title_clean or f'x{episode_num}' in title_clean: return it
                if f'e{episode_num:02d}' in title_clean or f'e{episode_num}' in title_clean: return it
                if (f'episodio {episode_num}' in title_clean
                        or f'capitulo {episode_num}' in title_clean
                        or title_clean == str(episode_num)): return it
    return None

def _is_year_compatible(item_year, target_year):
    if not target_year:
        return True
    y_str = str(item_year or '').strip()
    if not y_str or y_str in ('-', '?', '0', 'None'):
        return True
    try:
        y_int = int(y_str)
        t_int = int(target_year)
        return abs(y_int - t_int) <= 1
    except:
        return str(target_year) in y_str

def _search_on_player(player_file, title, year, season, episode,
                       showname, showyear, title_es, title_lat, title_en, title_orig):
    global tmdb_id, imdb_id, tvdb_id, trakt_id
    
    # Inicializar thread-local para forzar el match del ID de TMDB objetivo en búsquedas homónimas
    _bridge_tls.target_tmdb_id = tmdb_id
    _bridge_tls.target_imdb_id = imdb_id
    _bridge_tls.target_year = year
    _bridge_tls.target_title = title

    is_ep = bool(season and episode)
    key = 'play_episode' if is_ep else 'play_movie'

    fpath = os.path.join(TMDB_PLAYERS_PATH, player_file)
    try:
        with open(fpath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        url_list = data.get(key, [])
        if not url_list or not isinstance(url_list, list): return None, None
        url_str = unquote(url_list[0])
        if 'plugin://plugin.video.balandro/?' not in url_str: return None, None
        raw_b64 = url_str.split('plugin://plugin.video.balandro/?')[1].split('&')[0]
        raw_b64 = unquote(raw_b64)
        plugin_url = 'plugin://plugin.video.balandro/?' + raw_b64
    except Exception as e:
        xbmc.log("Balandro Bridge Multi: error leyendo JSON del player: " + str(e), xbmc.LOGERROR)
        return None, None

    try:
        search_item = Item().fromurl(plugin_url)
        ch = search_item.channel
        tipo = 'channels.' if os.path.exists(
            os.path.join(config.get_runtime_path(), 'channels', ch + '.py')
        ) else 'modules.'
        canal = __import__(tipo + ch, fromlist=[''])

        matched_item = None
        links = None
        with silenced_dialogs():
            if is_ep:
                search_term = title_es or title_lat or showname or title
                all_names = [t for t in [search_term, title_es, title_lat, title_en, title_orig] if t]
                if tmdb_id:
                    for _extra_t in _get_all_spanish_titles(tmdb_id, True):
                        if _extra_t not in all_names:
                            all_names.append(_extra_t)
                search_item.buscando = search_term
                results = canal.search(search_item, search_term)
                matched_show = None
                if results and isinstance(results, list):
                    if showyear:
                        for it in results:
                            it_url = getattr(it, 'url', '')
                            if str(showyear) in it_url and any(match_title(it.title, t, all_names) for t in all_names):
                                matched_show = it
                                xbmc.log("Balandro Bridge Multi: Direct match by URL year '%s' -> '%s' (url: %s)" % (showyear, getattr(it, 'title', ''), it_url), xbmc.LOGINFO)
                                break
                    if not matched_show and tmdb_id:
                        tmdb_matches = [it for it in results if str(getattr(it, 'infoLabels', {}).get('tmdb_id', '') or getattr(it, 'infoLabels', {}).get('tmdb', '') or '') == str(tmdb_id)]
                        if tmdb_matches:
                            matched_show = tmdb_matches[0]
                            if len(tmdb_matches) > 1 and showyear:
                                for it in tmdb_matches:
                                    it_url = getattr(it, 'url', '')
                                    it_yr = str(getattr(it, 'infoLabels', {}).get('year', ''))
                                    if str(showyear) in it_url or str(showyear) in it_yr:
                                        matched_show = it
                                        break
                            xbmc.log("Balandro Bridge Multi: Target TMDB ID '%s' matched directly in show search: '%s' (url: %s)" % (tmdb_id, getattr(matched_show, 'title', ''), getattr(matched_show, 'url', '')), xbmc.LOGINFO)
                    if not matched_show:
                        for it in results:
                            if any(match_title(it.title, t, all_names) for t in all_names):
                                it_year = str(getattr(it, 'infoLabels', {}).get('year', ''))
                                if _is_year_compatible(it_year, showyear):
                                    matched_show = it
                                    break
                    if not matched_show:
                        fallback_terms = []
                        for t in all_names:
                            if t and t != search_term and t not in fallback_terms:
                                fallback_terms.append(t)
                        for term in fallback_terms:
                            if not matched_show:
                                try:
                                    search_item.buscando = term
                                    fallback_results = canal.search(search_item, term)
                                    if fallback_results and isinstance(fallback_results, list):
                                        for it in fallback_results:
                                            if any(match_title(it.title, t, all_names) for t in all_names):
                                                it_year = str(getattr(it, 'infoLabels', {}).get('year', ''))
                                                if _is_year_compatible(it_year, showyear):
                                                    matched_show = it
                                                    break
                                except:
                                    pass
                    if not matched_show:
                        next_page_item = None
                        for it in results:
                            raw_title_lower = it.title.lower()
                            if 'siguiente' in raw_title_lower or 'next' in raw_title_lower:
                                next_page_item = it
                                break
                        if next_page_item:
                            next_results = run_action_silent(next_page_item)
                            if next_results and isinstance(next_results, list):
                                for it in next_results:
                                    if any(match_title(it.title, t, all_names) for t in all_names):
                                        it_year = str(getattr(it, 'infoLabels', {}).get('year', ''))
                                        if _is_year_compatible(it_year, showyear):
                                            matched_show = it
                                            break
                if matched_show:
                    matched_ep = find_episode(matched_show, season, episode)
                    if matched_ep:
                        lnk = _precheck_links(matched_ep)
                        if lnk:
                            matched_item = matched_ep
                            links = lnk
            else:
                # Elegir el mejor término de búsqueda inicial
                # Para películas cuyo título en español no coincide con el del canal,
                # preferimos buscar por título original para evitar fallbacks que
                # causan rate limiting (e.g. PlusHD almacena "Her" no "Ella")

                # Fix: si title es corrupto (ej: "????"), descartarlo localmente para esta búsqueda
                _local_title = title
                if _local_title and all(c in '? ' for c in _local_title):
                    _local_title = ''

                # Fix: enriquecer title_en/title_orig si llegan vacíos consultando la DB
                _local_title_en = title_en
                _local_title_orig = title_orig
                if tmdb_id and (not _local_title_en or not _local_title_orig):
                    try:
                        _fetched_orig = _get_original_title_from_db(tmdb_id, False)
                        if _fetched_orig:
                            if not _local_title_orig:
                                _local_title_orig = _fetched_orig
                            if not _local_title_en:
                                _local_title_en = _fetched_orig
                    except Exception:
                        pass

                _primary_es = title_es or title_lat or _local_title
                _primary_orig = _local_title_orig or _local_title_en or _local_title
                # Si el título original es diferente al español Y es corto/puro, buscar en dos pasos
                # pero primero usar el título más corto (suele ser el original en inglés)
                if _primary_orig and _primary_es and _primary_orig.lower() != _primary_es.lower():
                     # Usar el título más corto como primer intento (menos palabras -> búsqueda más precisa)
                     if len(_primary_orig) <= len(_primary_es):
                         search_term = _primary_orig
                     else:
                         search_term = _primary_es
                else:
                    search_term = _primary_es
                all_titles = [t for t in [_local_title, title_es, title_lat, _local_title_en, _local_title_orig] if t]
                if tmdb_id:
                    for _extra_t in _get_all_spanish_titles(tmdb_id, False):
                        if _extra_t not in all_titles:
                            all_titles.append(_extra_t)
                search_item.buscando = search_term
                xbmc.log("Bridge SEARCH [%s]: buscando '%s' (year=%s) [VARS: title=%r, title_es=%r, title_lat=%r, title_orig=%r, title_en=%r]" % (
                    player_file, search_term, year, _local_title, title_es, title_lat, _local_title_orig, _local_title_en), xbmc.LOGINFO)
                results = canal.search(search_item, search_term)

                n_results = len(results) if results and isinstance(results, list) else 0
                xbmc.log("Bridge SEARCH [%s]: obtuvo %d resultados" % (player_file, n_results), xbmc.LOGINFO)


                def _get_match_level(it_title, it_il):
                    # Rechazo inmediato si el film aún no fue estrenado según TMDB
                    # (fecha de estreno futura + canal no proveyó año propio)
                    if hasattr(it_il, 'get'):
                        try:
                            if it_il.get('_bridge_future_release'):
                                return 0  # No estrenado — ningún canal puede tenerlo
                        except: pass

                    # 0b. Rechazo por año explícito en el propio título del item.
                    #     Ej: "He-Man Y Los Amos Del Universo (1987)" con target=2026
                    #     → el título mismo revela el año real, ignorar cualquier ID match.
                    _tgt_yr = 0
                    try: _tgt_yr = int(year or 0)
                    except: pass
                    if _tgt_yr:
                        import re as _re_ml
                        _title_yr = _re_ml.search(r'\((\d{4})\)\s*$', it_title.strip())
                        if _title_yr:
                            _yr = int(_title_yr.group(1))
                            if _yr != _tgt_yr:
                                return 0  # El título grita otro año — rechazado

                    # 1. Check ID match first if available
                    has_id_info = False
                    id_matched = False
                    
                    if tmdb_id:
                        it_tmdb = ''
                        if hasattr(it_il, 'get'):
                            try: it_tmdb = str(it_il.get('tmdb_id') or it_il.get('tmdb') or '')
                            except: pass
                        else:
                            try: it_tmdb = str(it_il['tmdb_id'] or it_il['tmdb'])
                            except: pass
                        if it_tmdb and it_tmdb != '0' and it_tmdb.lower() != 'none' and it_tmdb != '':
                            has_id_info = True
                            if it_tmdb == str(tmdb_id):
                                id_matched = True
                                
                    if not id_matched and imdb_id:
                        it_imdb = ''
                        if hasattr(it_il, 'get'):
                            try: it_imdb = str(it_il.get('imdb_id') or it_il.get('imdb') or it_il.get('IMDBNumber') or it_il.get('code') or '')
                            except: pass
                        else:
                            try: it_imdb = str(it_il['imdb_id'] or it_il['imdb'] or it_il['IMDBNumber'] or it_il['code'])
                            except: pass
                        if it_imdb and it_imdb != '0' and it_imdb.lower() != 'none' and it_imdb != '':
                            def normalize_imdb(val):
                                if not val: return ''
                                val = str(val).strip().lower()
                                if val.startswith('tt'):
                                    val = val[2:]
                                return val.lstrip('0')
                            
                            norm_it = normalize_imdb(it_imdb)
                            norm_tgt = normalize_imdb(imdb_id)
                            if norm_it and norm_tgt:
                                has_id_info = True
                                if norm_it == norm_tgt:
                                    id_matched = True
                                    
                    if has_id_info:
                        if id_matched:
                            # El ID coincide. ¿Cómo se obtuvo?
                            # - _bridge_year_uncertain=True → TMDB buscó por título puro (sin filtro de año)
                            #   Si el ID coincide así → match natural y confiable → nivel 3
                            # - _bridge_year_uncertain=False → canal confirmó año → nivel 3
                            # En ambos casos si el ID coincide → nivel 3 (100% seguro)
                            return 3
                        else:
                            return 0  # Tiene ID pero es diferente: diferente película/serie

                    matched_any = False
                    matched_single_word = False
                    for t in all_titles:
                        if t and match_title(it_title, t, all_titles):
                            matched_any = True
                            clean_t = clean_title(t)
                            if len(clean_t.split()) <= 1:
                                matched_single_word = True

                    if not matched_any:
                        return 0 # No coincide el título
                    
                    if not year:
                        return 2 # Coincide título, no se busca año específico -> perfecto
                    
                    try:
                        target_year = int(year)
                    except:
                        return 2 # Año buscado inválido -> tratar como no especificado
                    
                    import re as _re_yr
                    
                    # 1. Buscar año en infoLabels PRIMERO (más fiable)
                    raw_year = ''
                    if hasattr(it_il, 'get'):
                        try: raw_year = it_il.get('year', '')
                        except: pass
                    else:
                        try: raw_year = it_il['year']
                        except: pass
                    
                    if raw_year:
                        m = _re_yr.search(r'\b(19\d\d|20\d\d)\b', str(raw_year))
                        if m:
                            found_year = int(m.group(1))
                            if found_year == target_year:
                                return 3  # Exacto
                            elif abs(found_year - target_year) <= 1:
                                if matched_single_word:
                                    return 0  # Títulos de una sola palabra requieren coincidencia exacta de año
                                return 2  # +-1 año
                            else:
                                return 0  # Año muy diferente
                    
                    # 2. Buscar año en el título, pero SOLO si no forma parte del nombre buscado.
                    # Ejemplo: "Blade Runner 2049" -> 2049 aparece en todos los títulos buscados,
                    # por lo que NO es el año de estreno sino parte del nombre.
                    m = _re_yr.search(r'\b(19\d\d|20\d\d)\b', it_title)
                    if m:
                        found_year = int(m.group(1))
                        # ¿Ese número aparece también en alguno de los títulos que buscamos?
                        # Si sí, es parte del nombre (ej: "2049", "1917", "2001") -> ignorarlo
                        yr_str = m.group(1)
                        is_part_of_name = any(yr_str in t for t in all_titles if t)
                        if not is_part_of_name:
                            if found_year == target_year:
                                return 3  # Exacto
                            elif abs(found_year - target_year) <= 1:
                                if matched_single_word:
                                    return 0  # Títulos de una sola palabra requieren coincidencia exacta de año
                                return 2  # +-1 año
                            else:
                                return 0  # Año muy diferente
                    
                    # Sin información de año fiable en metadatos ni título
                    return 1 # Fallback (coincide título pero no tiene año verificable)

                if results and isinstance(results, list):
                    # LOG DEBUG: mostrar todos los títulos devueltos
                    for _it_dbg in results:
                        _il_dbg = getattr(_it_dbg, "infoLabels", {}) or {}
                        _yr_dbg = _il_dbg.get("year", "?") if hasattr(_il_dbg, "get") else "?"
                        xbmc.log("Bridge DBG [%s]: resultado titulo='%s' year=%s" % (player_file, _it_dbg.title, str(_yr_dbg)), xbmc.LOGINFO)
                    # Pase 1: Buscar coincidencia perfecta (nivel >= 2)
                    for it in results:
                        it_il = getattr(it, 'infoLabels', {}) or {}
                        level = _get_match_level(it.title, it_il)
                        if level >= 2:
                            lnk = _precheck_links(it)
                            if lnk:
                                matched_item = it
                                links = lnk
                                xbmc.log("Bridge SEARCH [%s]: ENCONTRADO nivel%d '%s'" % (player_file, level, it.title), xbmc.LOGINFO)
                                break
                        elif level == 0:
                            if any(match_title(it.title, t, all_titles) for t in all_titles):
                                _it_il_dbg = getattr(it, "infoLabels", {}) or {}
                                _yr_dbg = _it_il_dbg.get("year", "N/A") if hasattr(_it_il_dbg, "get") else "N/A"
                                xbmc.log("Bridge SEARCH [%s]: descartado '%s' por anio (buscado=%s, canal_year=%s)" % (
                                    player_file, it.title, year, str(_yr_dbg)), xbmc.LOGINFO)

                    # Pase 2: Buscar coincidencia fallback (nivel 1) si no hubo perfecta
                    if not matched_item or not links:
                        for it in results:
                            it_il = getattr(it, 'infoLabels', {}) or {}
                            level = _get_match_level(it.title, it_il)
                            if level == 1:
                                lnk = _precheck_links(it)
                                if lnk:
                                    matched_item = it
                                    links = lnk
                                    xbmc.log("Bridge SEARCH [%s]: ENCONTRADO fallback '%s'" % (player_file, it.title), xbmc.LOGINFO)
                                    break

                    # Pase 2.5: Título exacto aunque el año del canal esté mal (metadatos erróneos del canal)
                    # SOLO para canales que SÍ proveyeron un año propio pero incorrecto.
                    # NO rescata si: (a) canal no tenía año (_bridge_year_uncertain), 
                    #                (b) hay ID de TMDB/IMDB que identifica una película DIFERENTE
                    if not matched_item or not links:
                        title_only_candidates = []
                        for it in results:
                            if any(match_title(it.title, t, all_titles) for t in all_titles if t):
                                it_il = getattr(it, 'infoLabels', {}) or {}

                                # (a) Saltar si el canal no tenía año propio o el film aún no fue estrenado
                                year_uncertain = False
                                future_release = False
                                try:
                                    year_uncertain = bool(it_il.get('_bridge_year_uncertain') if hasattr(it_il, 'get') else False)
                                    future_release = bool(it_il.get('_bridge_future_release') if hasattr(it_il, 'get') else False)
                                except: pass
                                if year_uncertain or future_release:
                                    continue

                                # (b) Saltar si hay un ID que identifica una película diferente
                                has_id_conflict = False
                                try:
                                    it_tmdb = str(it_il.get('tmdb_id') or it_il.get('tmdb') or '') if hasattr(it_il, 'get') else ''
                                    if it_tmdb and it_tmdb not in ('0', '', 'None') and tmdb_id:
                                        if it_tmdb != str(tmdb_id):
                                            has_id_conflict = True
                                    if not has_id_conflict:
                                        it_imdb = str(it_il.get('imdb_id') or it_il.get('imdb') or it_il.get('IMDBNumber') or '') if hasattr(it_il, 'get') else ''
                                        if it_imdb and it_imdb not in ('0', '', 'None') and imdb_id:
                                            def _norm(v):
                                                v = str(v).strip().lower()
                                                if v.startswith('tt'): v = v[2:]
                                                return v.lstrip('0')
                                            if _norm(it_imdb) and _norm(imdb_id) and _norm(it_imdb) != _norm(imdb_id):
                                                has_id_conflict = True
                                except: pass
                                if has_id_conflict:
                                    xbmc.log("Bridge SEARCH [%s]: Pase 2.5 OMITIDO '%s' — ID indica película diferente" % (player_file, it.title), xbmc.LOGINFO)
                                    continue

                                # (c) Saltar si el TÍTULO del item contiene un año explícito que
                                #     contradice el año objetivo. Ej: "He-Man Y Los Amos Del Universo (1987)"
                                #     con target=2026 → el título mismo revela que es el film de 1987.
                                _p25_tgt_yr = 0
                                try: _p25_tgt_yr = int(year or 0)
                                except: pass
                                if _p25_tgt_yr:
                                    import re as _re
                                    title_year_match = _re.search(r'\((\d{4})\)\s*$', it.title.strip())
                                    if title_year_match:
                                        title_year = int(title_year_match.group(1))
                                        if title_year != _p25_tgt_yr:
                                            xbmc.log("Bridge SEARCH [%s]: Pase 2.5 OMITIDO '%s' — título contiene año %d ≠ objetivo %d" % (player_file, it.title, title_year, _p25_tgt_yr), xbmc.LOGINFO)
                                            continue

                                level = _get_match_level(it.title, it_il)
                                if level == 0:  # Rechazado solo por año incorrecto del canal
                                    lnk = _precheck_links(it)
                                    if lnk:
                                        title_only_candidates.append((it, lnk))
                        if len(title_only_candidates) == 1:
                            matched_item, links = title_only_candidates[0]
                            xbmc.log("Bridge SEARCH [%s]: ENCONTRADO por título (año canal incorrecto) '%s'" % (player_file, matched_item.title), xbmc.LOGINFO)
                        elif len(title_only_candidates) > 1:
                            xbmc.log("Bridge SEARCH [%s]: %d candidatos con título coincidente pero años incorrectos → ambiguo, se omite" % (player_file, len(title_only_candidates)), xbmc.LOGINFO)

                # Pase 3: Intentar buscar con títulos alternativos (ej: original/inglés)
                if not matched_item or not links:
                    fallback_terms = []
                    for t in all_titles:
                        if t and t != search_term and t not in fallback_terms:
                            fallback_terms.append(t)
                    for term in fallback_terms:
                        if not matched_item or not links:
                            try:
                                # Pequeño delay para evitar rate limiting (Cloudflare/PlusHD)
                                import time as _time
                                if ch == 'plushd':
                                    _time.sleep(3.0)
                                else:
                                    _time.sleep(0.2)
                                xbmc.log("Bridge SEARCH [%s]: Fallback buscando '%s'" % (player_file, term), xbmc.LOGINFO)
                                search_item.buscando = term
                                fallback_results = canal.search(search_item, term)
                                if fallback_results and isinstance(fallback_results, list):
                                    for it in fallback_results:
                                        it_il = getattr(it, 'infoLabels', {}) or {}
                                        _yr_fb = it_il.get('year', '?') if hasattr(it_il, 'get') else '?'
                                        xbmc.log("Bridge DBG fallback [%s]: titulo='%s' year=%s" % (player_file, it.title, str(_yr_fb)), xbmc.LOGINFO)
                                    for it in fallback_results:
                                        it_il = getattr(it, 'infoLabels', {}) or {}
                                        level = _get_match_level(it.title, it_il)
                                        if level >= 2:
                                            lnk = _precheck_links(it)
                                            if lnk:
                                                matched_item = it
                                                links = lnk
                                                xbmc.log("Bridge SEARCH [%s]: ENCONTRADO perfecto en fallback '%s'" % (player_file, it.title), xbmc.LOGINFO)
                                                break
                                            else:
                                                xbmc.log("Bridge DBG fallback [%s]: '%s' nivel=%d pero sin links" % (player_file, it.title, level), xbmc.LOGINFO)
                                        else:
                                            xbmc.log("Bridge DBG fallback [%s]: '%s' descartado nivel=%d" % (player_file, it.title, level), xbmc.LOGINFO)
                                    if not matched_item or not links:
                                        for it in fallback_results:
                                            it_il = getattr(it, 'infoLabels', {}) or {}
                                            level = _get_match_level(it.title, it_il)
                                            if level == 1:
                                                lnk = _precheck_links(it)
                                                if lnk:
                                                    matched_item = it
                                                    links = lnk
                                                    xbmc.log("Bridge SEARCH [%s]: ENCONTRADO fallback en fallback '%s'" % (player_file, it.title), xbmc.LOGINFO)
                                                    break

                                    # Pase especial: búsqueda con título en inglés/original devolvió
                                    # exactamente 1 resultado con año exacto pero título diferente.
                                    # Ej: SoloLatino tiene "The Shadow's Edge" como "Escuadrón letal".
                                    # SOLO se activa si el term fue el título inglés/original,
                                    # para evitar falsos positivos con búsquedas en español.
                                    if not matched_item or not links:
                                        _is_en_term = bool(
                                            (_local_title_en and term == _local_title_en) or
                                            (_local_title_orig and term == _local_title_orig)
                                        )
                                        if _is_en_term and len(fallback_results) == 1 and year:
                                            it = fallback_results[0]
                                            it_il = getattr(it, 'infoLabels', {}) or {}
                                            try:
                                                canal_yr = int(str(it_il.get('year', '') if hasattr(it_il, 'get') else '').strip())
                                                target_yr = int(year)
                                                if abs(canal_yr - target_yr) <= 1:
                                                    lnk = _precheck_links(it)
                                                    if lnk:
                                                        matched_item = it
                                                        links = lnk
                                                        xbmc.log("Bridge SEARCH [%s]: ENCONTRADO por titulo-ingles resultado-unico '%s' (canal=%d, buscado=%d)" % (
                                                            player_file, it.title, canal_yr, target_yr), xbmc.LOGINFO)
                                            except:
                                                pass

                                else:
                                    xbmc.log("Bridge DBG fallback [%s]: busqueda '%s' devolvio 0 resultados" % (player_file, term), xbmc.LOGINFO)
                            except Exception as ex_fb:
                                import traceback as _tb
                                xbmc.log("Bridge DBG fallback [%s]: excepcion buscando '%s': %s\n%s" % (player_file, term, str(ex_fb), _tb.format_exc()), xbmc.LOGERROR)

                if not matched_item or not links:
                    next_page_item = None
                    if results and isinstance(results, list):
                        for it in results:
                            raw_title_lower = it.title.lower()
                            if 'siguiente' in raw_title_lower or 'next' in raw_title_lower:
                                next_page_item = it
                                break
                    if next_page_item:
                        next_results = run_action_silent(next_page_item)
                        if next_results and isinstance(next_results, list):
                            # Pase 1 en página 2
                            for it in next_results:
                                it_il = getattr(it, 'infoLabels', {}) or {}
                                level = _get_match_level(it.title, it_il)
                                if level == 2:
                                    lnk = _precheck_links(it)
                                    if lnk:
                                        matched_item = it
                                        links = lnk
                                        xbmc.log("Bridge SEARCH [%s]: ENCONTRADO perfecto (pag2) '%s'" % (player_file, it.title), xbmc.LOGINFO)
                                        break
                            # Pase 2 en página 2
                            if not matched_item or not links:
                                for it in next_results:
                                    it_il = getattr(it, 'infoLabels', {}) or {}
                                    level = _get_match_level(it.title, it_il)
                                    if level == 1:
                                        lnk = _precheck_links(it)
                                        if lnk:
                                            matched_item = it
                                            links = lnk
                                            xbmc.log("Bridge SEARCH [%s]: ENCONTRADO fallback (pag2) '%s'" % (player_file, it.title), xbmc.LOGINFO)
                                            break

        if not matched_item or not links:
            xbmc.log("Bridge SEARCH [%s]: sin resultado final" % player_file, xbmc.LOGINFO)
            return None, None
        return matched_item, links
    except Exception as e:
        xbmc.log("Balandro Bridge Multi SEARCH ERROR in %s: %s" % (player_file, str(e)), xbmc.LOGERROR)
        pass

    return None, None

def _detect_language(title_str):
    title_lower = title_str.lower()
    if 'latino' in title_lower or 'es-mx' in title_lower or 'lat' in title_lower: return 'Latino'
    if 'castellano' in title_lower or 'es-es' in title_lower or 'esp' in title_lower: return 'Castellano'
    if 'vose' in title_lower or 'subt' in title_lower: return 'VOSE'
    if 'ingles' in title_lower or 'eng' in title_lower or 'english' in title_lower: return 'Ingles'
    return 'Ninguno'

def _detect_quality(title_str):
    title_lower = title_str.lower()
    if '1080' in title_lower: return '1080p'
    if '720' in title_lower: return '720p'
    if '4k' in title_lower or '2160' in title_lower or 'uhd' in title_lower: return '4k'
    if 'sd' in title_lower or 'dvd' in title_lower or 'screener' in title_lower or 'cam' in title_lower: return 'SD'
    return 'Ninguno'

def _serialize_item(it):
    if not it: return None
    d = {}
    dict_to_use = it.__dict__ if hasattr(it, '__dict__') else {}
    for k in dict_to_use:
        try:
            val = dict_to_use[k]
            is_str = isinstance(val, str) or (sys.version_info[0] < 3 and isinstance(val, unicode))
            if is_str or isinstance(val, (int, float, bool, list, dict, tuple)) or val is None:
                d[k] = val
        except Exception as e:
            xbmc.log("Balandro Bridge Multi serialization warning for key %s: %s" % (k, str(e)), xbmc.LOGWARNING)
    return d

def _deserialize_item(d):
    if not d: return None
    it = Item()
    for k, v in d.items():
        setattr(it, k, v)
    if 'infoLabels' in it.__dict__ and not isinstance(it.__dict__['infoLabels'], InfoLabels):
        it.__dict__['infoLabels'] = InfoLabels(it.__dict__['infoLabels'])
    return it

def main():
    # -----------------------------------------------------------
    # PLAYER MANAGER: Opened without action/url → show UI
    # -----------------------------------------------------------
    if not action and not url:
        if not view or view == 'home':
            show_player_manager_home()
        elif view in ('all', 'movie', 'tvshow'):
            show_channel_list(filter_category=view)
        elif view == 'create':
            show_creation_menu()
        elif view == 'player_options':
            player_file = get_param('player')
            show_player_options(player_file)
        else:
            show_player_manager_home()
        return

    # -----------------------------------------------------------
    # action=absorb: llamada interna para liberar el handle original
    # -----------------------------------------------------------
    if action == 'absorb':
        _original_setResolvedUrl(handle, False, xbmcgui.ListItem())
        return

    # -----------------------------------------------------------
    # action=select_and_play: muestra el diálogo de selección de servidor
    # y reproduce. Se invoca via RunScript para evitar bloquear el hilo
    # del plugin original (lo que causaba freeze en Arctic Fuse).
    # -----------------------------------------------------------
    if action == 'select_and_play':
        xbmc.log("Bridge select_and_play: iniciando en hilo limpio", xbmc.LOGINFO)
        try:
            if xbmc.Player().isPlaying():
                xbmc.Player().stop()
        except: pass

        # Leer caché de búsqueda
        links = []
        matched_item = None
        if os.path.exists(SEARCH_CACHE_FILE):
            try:
                with open(SEARCH_CACHE_FILE, 'r', encoding='utf-8') as f:
                    cache = json.load(f)
                matched_item = _deserialize_item(cache.get('item'))
                links = [_deserialize_item(lnk) for lnk in cache.get('links', [])]
                xbmc.log("Bridge select_and_play: %d enlaces en caché" % len(links), xbmc.LOGINFO)
            except Exception as e:
                xbmc.log("Bridge select_and_play: Error leyendo caché - " + str(e), xbmc.LOGERROR)

        if not links or not matched_item:
            xbmc.log("Bridge select_and_play: caché vacío, sin enlaces", xbmc.LOGWARNING)
            xbmcgui.Dialog().notification(
                'Balandro Bridge Multi',
                'Sin enlaces disponibles',
                xbmcgui.NOTIFICATION_WARNING, 3000
            )
            return

        # Restaurar meta contexto
        _saved_ctx = _load_last_player_context()
        if _saved_ctx:
            _meta_ctx.update({
                'tmdb':     _saved_ctx.get('tmdb', ''),
                'imdb':     _saved_ctx.get('imdb', ''),
                'tvdb':     _saved_ctx.get('tvdb', ''),
                'trakt':    _saved_ctx.get('trakt', ''),
                'title':    _saved_ctx.get('title', ''),
                'year':     _saved_ctx.get('year', ''),
                'season':   _saved_ctx.get('season', ''),
                'episode':  _saved_ctx.get('episode', ''),
                'showname': _saved_ctx.get('showname', ''),
                'showyear': _saved_ctx.get('showyear', ''),
                'plot':     _saved_ctx.get('plot', ''),
                'tagline':  _saved_ctx.get('tagline', ''),
                'director': _saved_ctx.get('director', ''),
            })

        # Mostrar diálogo y reproducir — en hilo limpio sin herencia de sub-hilos de búsqueda
        _inject_meta_into_items(links, matched_item)
        autoplay_enabled = xbmcaddon.Addon('plugin.video.balandro.bridge.multi').getSetting('autoplay_enabled') != 'false'
        if autoplay_enabled:
            _direct_autoplay(links, matched_item, handle)
        else:
            xbmc.log("Balandro Bridge Multi: Autoplay desactivado. Mostrando selector manual.", xbmc.LOGINFO)
            sys.argv[1] = handle
            apply_monkeypatch()
            platformtools.play_from_itemlist(links, matched_item)
        try:
            xbmcplugin.endOfDirectory(handle, succeeded=False)
        except Exception:
            pass
        return


    # -----------------------------------------------------------
    # action=verify_channels: verifica canales
    # -----------------------------------------------------------
    if action == 'verify_channels':
        category = get_param('category')
        verify_all_players_channels(category)
        xbmcplugin.endOfDirectory(handle, succeeded=False)
        return

    # -----------------------------------------------------------
    # action=play: reproduccion
    # -----------------------------------------------------------
    if action == 'play':
        # Detener inmediatamente el reproductor para evitar la pantalla negra/dummy de 1 segundo
        try:
            if xbmc.Player().isPlaying():
                xbmc.Player().stop()
        except:
            pass

        # --- CASO A: Reproducción de canal único ---

        if url:
            matched_item = Item().fromurl(url)

            # Guardar estado de reanudación
            try:
                _saved_ctx = _load_last_player_context()
                resume_title = 'Contenido'
                resume_url = 'plugin://plugin.video.balandro.bridge.multi/' + sys.argv[2]
                if _saved_ctx:
                    resume_title = _saved_ctx.get('title') or _saved_ctx.get('showname') or 'Contenido'
                    tmdb = _saved_ctx.get('tmdb')
                    player = _saved_ctx.get('player_file')
                    if tmdb and player:
                        resume_season = _saved_ctx.get('season')
                        resume_episode = _saved_ctx.get('episode')
                        if resume_season and resume_episode:
                            resume_url = 'plugin://plugin.video.themoviedb.helper/?info=play&tmdb_id=%s&tmdb_type=tv&season=%s&episode=%s&player=%s' % (tmdb, resume_season, resume_episode, quote(player))
                        else:
                            resume_url = 'plugin://plugin.video.themoviedb.helper/?info=play&tmdb_id=%s&tmdb_type=movie&player=%s' % (tmdb, quote(player))
                    
                state_data = {
                    'url': resume_url,
                    'title': resume_title
                }
                state_file = os.path.join(_profile_dir, 'resume_state.json')
                with open(state_file, 'w', encoding='utf-8') as f:
                    json.dump(state_data, f, ensure_ascii=False)
            except Exception as e:
                xbmc.log('Balandro Bridge Multi: Error guardando estado de reanudacion - ' + str(e), xbmc.LOGERROR)

            # Restaurar metadatos
            _saved_ctx = _load_last_player_context()
            if _saved_ctx:
                _meta_ctx.update({
                    'tmdb':     _saved_ctx.get('tmdb', ''),
                    'imdb':     _saved_ctx.get('imdb', ''),
                    'tvdb':     _saved_ctx.get('tvdb', ''),
                    'trakt':    _saved_ctx.get('trakt', ''),
                    'title':    _saved_ctx.get('title', ''),
                    'year':     _saved_ctx.get('year', ''),
                    'season':   _saved_ctx.get('season', ''),
                    'episode':  _saved_ctx.get('episode', ''),
                    'showname': _saved_ctx.get('showname', ''),
                    'showyear': _saved_ctx.get('showyear', ''),
                    'plot':     _saved_ctx.get('plot', ''),
                    'tagline':  _saved_ctx.get('tagline', ''),
                    'director': _saved_ctx.get('director', ''),
                })

            channel_name = matched_item.channel
            action_name = matched_item.action
            path = os.path.join(config.get_runtime_path(), 'channels', channel_name + ".py")
            tipo_channel = 'channels.' if os.path.exists(path) else 'modules.'
            canal = __import__(tipo_channel + channel_name, fromlist=[''])
            func = getattr(canal, action_name)

            try:
                links = func(matched_item)
            except Exception:
                links = None

            if not links or not isinstance(links, list):
                _original_setResolvedUrl(handle, False, xbmcgui.ListItem())
                _clear_resume_state()
                return

            _guardian_stop = [False]
            def _error_guardian():
                import time
                while not _guardian_stop[0]:
                    time.sleep(0.05)
                    if not xbmc.Player().isPlaying():
                        xbmc.executebuiltin('Dialog.Close(okdialog,true)')
                        xbmc.executebuiltin('Dialog.Close(error,true)')
            import threading
            _guardian_thread = threading.Thread(target=_error_guardian, daemon=True)
            _guardian_thread.start()

            # Pre-matador: cierra el dialogo de error antes de que llegue a mostrarse
            # El absorb genera un setResolvedUrl(False) que Kodi intenta mostrar como error
            def _pre_kill_error_dialog():
                import time
                # Mantenemos vivo el hilo hasta 5 minutos mientras el usuario elige enlace
                for _ in range(30000):
                    time.sleep(0.01)
                    if _guardian_stop[0]:
                        break
                    xbmc.executebuiltin('Dialog.Close(okdialog,true)')
                    xbmc.executebuiltin('Dialog.Close(error,true)')
                    xbmc.executebuiltin('Dialog.Close(notification,true)')
                    if xbmc.Player().isPlaying():
                        break
            threading.Thread(target=_pre_kill_error_dialog, daemon=True).start()

            _orig_play_fake = platformtools.play_fake
            _absorb_item = xbmcgui.ListItem()
            _original_setResolvedUrl(handle, False, _absorb_item)
            active_handle = '-1'
            _pre_kill_stop = [False]  # flag compartido para detener el hilo matador

            def _pre_kill_error_dialog():
                import time
                for _ in range(30000):
                    if _guardian_stop[0] or _pre_kill_stop[0]:
                        break
                    xbmc.executebuiltin('Dialog.Close(okdialog,true)')
                    xbmc.executebuiltin('Dialog.Close(error,true)')
                    if xbmc.Player().isPlaying():
                        break
                    time.sleep(0.01)
            threading.Thread(target=_pre_kill_error_dialog, daemon=True).start()

            is_torrent = False
            if links:
                for lnk in links:
                    server = getattr(lnk, 'server', '').lower() if hasattr(lnk, 'server') else ''
                    url_lnk = getattr(lnk, 'url', '').lower() if hasattr(lnk, 'url') else ''
                    if 'torrent' in server or 'torrent' in url_lnk or 'magnet:' in url_lnk or 'elementum' in url_lnk or 'elementum' in server:
                        is_torrent = True
                        break

            started = False
            while True:
                play_fake_intercepted = [False]
                def patched_play_fake(resuelto=False):
                    if not resuelto:
                        play_fake_intercepted[0] = True
                    else:
                        _orig_play_fake(resuelto)

                platformtools.play_fake = patched_play_fake

                try:
                    sys.argv[1] = active_handle
                    apply_monkeypatch()
                    _inject_meta_into_items(links, matched_item)
                    platformtools.play_from_itemlist(links, matched_item)
                finally:
                    platformtools.play_fake = _orig_play_fake

                started = False
                if not play_fake_intercepted[0]:
                    max_seconds = 90 if is_torrent else 8
                    max_loops = int(max_seconds * 2)
                    progress_seen = False
                    progress_inactive_count = 0
                    p_dialog = None

                    try:
                        for _ in range(max_loops):
                            xbmc.sleep(500)
                            if xbmc.Player().isPlaying():
                                started = True
                                break

                            if is_torrent:
                                progress_active = False
                                if not p_dialog:
                                    progress_active = (
                                        xbmc.getCondVisibility('Window.IsActive(progressdialog)') or
                                        xbmc.getCondVisibility('Window.IsActive(10101)') or
                                        xbmc.getCondVisibility('Window.IsActive(10151)') or
                                        xbmc.getCondVisibility('Window.IsVisible(progressdialog)')
                                    )
                                if progress_active:
                                    progress_seen = True
                                    progress_inactive_count = 0
                                elif progress_seen:
                                    if p_dialog is None:
                                        p_dialog = True
                                        xbmcgui.Dialog().notification('Balandro Bridge Multi', 'Cargando enlaces...', xbmcgui.NOTIFICATION_INFO, 2500)
                                    progress_inactive_count += 1
                                    if progress_inactive_count >= 6:
                                        break
                    finally:
                        pass
                if started:
                    load_error_detected = False
                    played_secs = 0.0
                    for _mon in range(240):
                        xbmc.sleep(500)
                        if xbmc.Player().isPlaying():
                            try:
                                played_secs = xbmc.Player().getTime()
                            except Exception:
                                pass
                            continue
                        has_kodi_error = (
                            xbmc.getCondVisibility('Window.IsActive(okdialog)') or
                            xbmc.getCondVisibility('Window.IsActive(error)') or
                            xbmc.getCondVisibility('Window.IsVisible(okdialog)') or
                            xbmc.getCondVisibility('Window.IsActive(notification)')
                        )
                        if has_kodi_error or played_secs < 3.0:
                            load_error_detected = True
                        break

                    if not load_error_detected:
                        _guardian_stop[0] = True
                        return

                    xbmc.executebuiltin('Dialog.Close(okdialog,true)')
                    xbmc.executebuiltin('Dialog.Close(error,true)')
                    xbmcgui.Dialog().notification(
                        'Balandro Bridge Multi',
                        '[COLOR gold]Error de carga[/COLOR] — Elige otro servidor',
                        xbmcgui.NOTIFICATION_WARNING, 3500, False
                    )
                    xbmc.sleep(400)
                    continue

                if play_fake_intercepted[0]:
                    _guardian_stop[0] = True   # INMEDIATO: detener guardian que llama Dialog.Close cada 50ms
                    _pre_kill_stop[0] = True   # Detener hilo matador inmediatamente
                    break

                if is_torrent:
                    continue
                else:
                    break

            _guardian_stop[0] = True
            _pre_kill_stop[0] = True

            ctx = _load_last_player_context()
            played_fallback = False
            if ctx and ctx.get('player_file'):
                played_fallback = _offer_continue_search(ctx)

            if not played_fallback:
                _clear_resume_state()
            return

        # --- CASO B: Reproducción de Búsqueda Paralela (desde caché) ---
        else:
            _saved_ctx = _load_last_player_context()
            if _saved_ctx:
                _meta_ctx.update({
                    'tmdb':     _saved_ctx.get('tmdb', ''),
                    'imdb':     _saved_ctx.get('imdb', ''),
                    'tvdb':     _saved_ctx.get('tvdb', ''),
                    'trakt':    _saved_ctx.get('trakt', ''),
                    'title':    _saved_ctx.get('title', ''),
                    'year':     _saved_ctx.get('year', ''),
                    'season':   _saved_ctx.get('season', ''),
                    'episode':  _saved_ctx.get('episode', ''),
                    'showname': _saved_ctx.get('showname', ''),
                    'showyear': _saved_ctx.get('showyear', ''),
                    'plot':     _saved_ctx.get('plot', ''),
                })

            links = []
            matched_item = None

            if os.path.exists(SEARCH_CACHE_FILE):
                try:
                    with open(SEARCH_CACHE_FILE, 'r', encoding='utf-8') as f:
                        cache = json.load(f)
                    matched_item = _deserialize_item(cache.get('item'))
                    links = [_deserialize_item(lnk) for lnk in cache.get('links', [])]
                except Exception as e:
                    xbmc.log("Balandro Bridge Multi: Error al leer cache - " + str(e), xbmc.LOGERROR)

            if not links or not matched_item:
                xbmc.log("Balandro Bridge Multi: Cache vacio, iniciando busqueda paralela en action=play...", xbmc.LOGINFO)
                links, matched_item = run_parallel_search()
                if links and matched_item:
                    try:
                        with open(SEARCH_CACHE_FILE, 'w', encoding='utf-8') as f:
                            cache_data = {
                                'item': _serialize_item(matched_item),
                                'links': [_serialize_item(lnk) for lnk in links]
                            }
                            json.dump(cache_data, f, ensure_ascii=False)
                        xbmc.log("Balandro Bridge Multi: Guardado cache de busqueda con %d enlaces" % len(links), xbmc.LOGINFO)
                    except Exception as e:
                        xbmc.log("Balandro Bridge Multi: Error al guardar cache - " + str(e), xbmc.LOGERROR)

            if not links or not matched_item:
                # Mostrar diálogo de aviso al usuario y preguntar si desea buscar en Elementum (si la opción está activa)
                _no_result_title = (_meta_ctx.get('title') or _meta_ctx.get('showname') or title or showname or 'este contenido')
                _no_result_season = _meta_ctx.get('season') or season
                _no_result_episode = _meta_ctx.get('episode') or episode
                
                # Leer ajuste de Elementum
                addon_obj = xbmcaddon.Addon('plugin.video.balandro.bridge.multi')
                elementum_enabled = addon_obj.getSetting('elementum_fallback') == 'true'
                
                if elementum_enabled:
                    if _no_result_season and _no_result_episode:
                        _no_result_msg = u'No se encontraron enlaces disponibles para:\n[B]%s[/B] — T%02dE%02d\n\n¿Deseas buscar en Elementum?' % (
                            _no_result_title, int(_no_result_season), int(_no_result_episode))
                    else:
                        _no_result_msg = u'No se encontraron enlaces disponibles para:\n[B]%s[/B]\n\n¿Deseas buscar en Elementum?' % _no_result_title
                    
                    if xbmcgui.Dialog().yesno('Balandro Bridge Multi', _no_result_msg, yeslabel='Sí', nolabel='No'):
                        _t_id = _meta_ctx.get('tmdb') or tmdb_id
                        if _no_result_season and _no_result_episode:
                            elementum_url = 'plugin://plugin.video.elementum/show/%s/season/%s/episode/%s/links' % (
                                str(_t_id), str(_no_result_season), str(_no_result_episode)
                            )
                        else:
                            elementum_url = 'plugin://plugin.video.elementum/movie/%s/links' % str(_t_id)
                        
                        xbmc.log("Balandro Bridge Multi: Iniciando hilo de reproducción retrasada para Elementum: %s" % elementum_url, xbmc.LOGINFO)
                        
                        # Hilo para cerrar los popups de error de Kodi silenciosamente
                        def close_error_popups():
                            for _ in range(150):  # 3 segundos (150 * 20ms)
                                xbmc.executebuiltin('Dialog.Close(okdialog,true)')
                                xbmc.executebuiltin('Dialog.Close(error,true)')
                                xbmc.sleep(20)
                        
                        # Hilo para reproducir en Elementum con retraso
                        def delayed_play():
                            xbmc.sleep(1500)  # Esperar 1.5 segundos para que Kodi limpie el estado del reproductor abortado
                            xbmc.log("Balandro Bridge Multi: Ejecutando PlayMedia para Elementum", xbmc.LOGINFO)
                            xbmc.executebuiltin('PlayMedia("%s")' % elementum_url)
                        
                        import threading
                        threading.Thread(target=close_error_popups, daemon=True).start()
                        threading.Thread(target=delayed_play, daemon=True).start()
                else:
                    if _no_result_season and _no_result_episode:
                        _no_result_msg = u'No se encontraron enlaces disponibles para:\n[B]%s[/B] — T%02dE%02d' % (
                            _no_result_title, int(_no_result_season), int(_no_result_episode))
                    else:
                        _no_result_msg = u'No se encontraron enlaces disponibles para:\n[B]%s[/B]' % _no_result_title
                    xbmcgui.Dialog().ok('Balandro Bridge Multi', _no_result_msg)
                
                _original_setResolvedUrl(handle, False, xbmcgui.ListItem())
                _clear_resume_state()
                return

            try:
                resume_title = _meta_ctx.get('title') or _meta_ctx.get('showname') or 'Contenido'
                tmdb = _meta_ctx.get('tmdb')
                player = 'BalandroMulti'
                resume_url = 'plugin://plugin.video.balandro.bridge.multi/?action=play'
                if tmdb:
                    resume_season = _meta_ctx.get('season')
                    resume_episode = _meta_ctx.get('episode')
                    if resume_season and resume_episode:
                        resume_url = 'plugin://plugin.video.themoviedb.helper/?info=play&tmdb_id=%s&tmdb_type=tv&season=%s&episode=%s&player=%s' % (tmdb, resume_season, resume_episode, quote(player))
                    else:
                        resume_url = 'plugin://plugin.video.themoviedb.helper/?info=play&tmdb_id=%s&tmdb_type=movie&player=%s' % (tmdb, quote(player))
                
                state_data = {
                    'url': resume_url,
                    'title': resume_title
                }
                state_file = os.path.join(_profile_dir, 'resume_state.json')
                with open(state_file, 'w', encoding='utf-8') as f:
                    json.dump(state_data, f, ensure_ascii=False)
            except Exception as e:
                xbmc.log('Balandro Bridge Multi: Error guardando estado de reanudacion - ' + str(e), xbmc.LOGERROR)

            # Lanzar el diálogo de selección en una invocación separada via RunPlugin
            # para que ESTE plugin retorne INMEDIATAMENTE y libere el hilo de Kodi.
            # Esto evita el freeze de Arctic Fuse causado por hilos no-daemon de Balandro.
            xbmc.log("Bridge CASO B: lanzando select_and_play via RunPlugin", xbmc.LOGINFO)

            # Limpiar la playlist de video de Kodi de antemano.
            # Esto previene que el Playlist Player de Kodi intente procesar o reproducir
            # este item tras resolverlo, evitando de raíz cualquier diálogo de error nativo.
            try:
                xbmc.PlayList(xbmc.PLAYLIST_VIDEO).clear()
            except Exception as e:
                xbmc.log("Bridge CASO B: error limpiando playlist - " + str(e), xbmc.LOGDEBUG)

            import threading as _threading_cB
            _cB_stop = [False]
            def _pre_kill_dialog_cB():
                import time as _t
                deadline = _t.time() + 3.0
                while not _cB_stop[0] and _t.time() < deadline:
                    xbmc.executebuiltin('Dialog.Close(okdialog,true)')
                    xbmc.executebuiltin('Dialog.Close(error,true)')
                    _t.sleep(0.05)
            _threading_cB.Thread(target=_pre_kill_dialog_cB, daemon=True).start()

            dummy_item = xbmcgui.ListItem()
            dummy_item.setPath("")
            dummy_item.setProperty('IsPlayable', 'false')
            _original_setResolvedUrl(handle, True, dummy_item)
            # Pequeña pausa para que Kodi procese el setResolvedUrl antes del RunPlugin
            xbmc.sleep(300)
            xbmc.executebuiltin('RunPlugin(plugin://plugin.video.balandro.bridge.multi/?action=select_and_play)')
            _cB_stop[0] = True
            # NO borrar resume state aquí - select_and_play lo necesita
            return

    # -----------------------------------------------------------
    # Búsqueda inicial (TMDB Helper)
    # -----------------------------------------------------------
    if not url:
        xbmcplugin.endOfDirectory(handle, succeeded=False)
        return

    search_item = Item().fromurl(url)

    # --- CASO A: Búsqueda de Canal Único ---
    if search_item.channel:
        tipo_channel = 'channels.' if os.path.exists(
            os.path.join(config.get_runtime_path(), 'channels', search_item.channel + ".py")
        ) else 'modules.'
        canal = __import__(tipo_channel + search_item.channel, fromlist=[''])

        dialog = xbmcgui.Dialog()
        channel_name_colored = "[COLOR chartreuse]" + search_item.channel.capitalize() + "[/COLOR]"
        dialog.notification("Balandro Bridge Multi", "Buscando en " + channel_name_colored + "...", xbmcgui.NOTIFICATION_INFO, 3000, False)

        matched_item = None
        found_links = [None]

        with silenced_dialogs():
            try:
                if season and episode:
                    search_term = title_es or title_lat or showname or title
                    all_shownames = [t for t in [search_term, title_es, title_lat, title_en, title_orig] if t]
                    if tmdb_id:
                        for _extra_t in _get_all_spanish_titles(tmdb_id, True):
                            if _extra_t not in all_shownames:
                                all_shownames.append(_extra_t)
                    search_item.buscando = search_term
                    results = canal.search(search_item, search_term)

                    matched_show = None
                    if results and isinstance(results, list):
                        if showyear:
                            for it in results:
                                it_url = getattr(it, 'url', '')
                                if str(showyear) in it_url and any(match_title(it.title, t, all_shownames) for t in all_shownames):
                                    matched_show = it
                                    xbmc.log("Balandro Bridge Multi: Direct match by URL year '%s' -> '%s' (url: %s)" % (showyear, getattr(it, 'title', ''), it_url), xbmc.LOGINFO)
                                    break
                        if not matched_show and tmdb_id:
                            tmdb_matches = [it for it in results if str(getattr(it, 'infoLabels', {}).get('tmdb_id', '') or getattr(it, 'infoLabels', {}).get('tmdb', '') or '') == str(tmdb_id)]
                            if tmdb_matches:
                                matched_show = tmdb_matches[0]
                                if len(tmdb_matches) > 1 and showyear:
                                    for it in tmdb_matches:
                                        it_url = getattr(it, 'url', '')
                                        it_yr = str(getattr(it, 'infoLabels', {}).get('year', ''))
                                        if str(showyear) in it_url or str(showyear) in it_yr:
                                            matched_show = it
                                            break
                                xbmc.log("Balandro Bridge Multi: Target TMDB ID '%s' matched directly in show search: '%s' (url: %s)" % (tmdb_id, getattr(matched_show, 'title', ''), getattr(matched_show, 'url', '')), xbmc.LOGINFO)
                        if not matched_show:
                            for it in results:
                                if any(match_title(it.title, t, all_shownames) for t in all_shownames):
                                    it_year = str(getattr(it, 'infoLabels', {}).get('year', ''))
                                    if _is_year_compatible(it_year, showyear):
                                        matched_show = it
                                        break
                        if not matched_show:
                            fallback_terms = []
                            for t in all_shownames:
                                if t and t != search_term and t not in fallback_terms:
                                    fallback_terms.append(t)
                            for term in fallback_terms:
                                if not matched_show:
                                    try:
                                        search_item.buscando = term
                                        fallback_results = canal.search(search_item, term)
                                        if fallback_results and isinstance(fallback_results, list):
                                            for it in fallback_results:
                                                if any(match_title(it.title, t, all_shownames) for t in all_shownames):
                                                    it_year = str(getattr(it, 'infoLabels', {}).get('year', ''))
                                                    if _is_year_compatible(it_year, showyear):
                                                        matched_show = it
                                                        break
                                    except:
                                        pass
                        if not matched_show:
                            next_page_item = None
                            for it in results:
                                raw_title_lower = it.title.lower()
                                if 'siguiente' in raw_title_lower or 'next' in raw_title_lower:
                                    next_page_item = it
                                    break
                            if next_page_item:
                                next_results = run_action_silent(next_page_item)
                                if next_results and isinstance(next_results, list):
                                    for it in next_results:
                                        if any(match_title(it.title, t, all_shownames) for t in all_shownames):
                                            it_year = str(getattr(it, 'infoLabels', {}).get('year', ''))
                                            if _is_year_compatible(it_year, showyear):
                                                matched_show = it
                                                break
                    if matched_show:
                        matched_ep = find_episode(matched_show, season, episode)
                        if matched_ep:
                            lnk = _precheck_links(matched_ep)
                            if lnk:
                                matched_item = matched_ep
                                found_links[0] = lnk
                else:
                    search_term = title_es or title_lat or title
                    all_titles = [t for t in [title, title_es, title_lat, title_en, title_orig] if t]
                    if tmdb_id:
                        for _extra_t in _get_all_spanish_titles(tmdb_id, False):
                            if _extra_t not in all_titles:
                                all_titles.append(_extra_t)
                    search_item.buscando = search_term
                    results = canal.search(search_item, search_term)

                    if results and isinstance(results, list):
                        for it in results:
                            if any(match_title(it.title, t, all_titles) for t in all_titles):
                                if year and str(year) not in str(getattr(it, 'infoLabels', {}).get('year', '')):
                                    continue
                                lnk = _precheck_links(it)
                                if lnk:
                                    matched_item = it
                                    found_links[0] = lnk
                                    break
                        if not matched_item or not found_links[0]:
                            fallback_terms = []
                            for t in all_titles:
                                if t and t != search_term and t not in fallback_terms:
                                    fallback_terms.append(t)
                            for term in fallback_terms:
                                if not matched_item or not found_links[0]:
                                    try:
                                        search_item.buscando = term
                                        fallback_results = canal.search(search_item, term)
                                        if fallback_results and isinstance(fallback_results, list):
                                            for it in fallback_results:
                                                if any(match_title(it.title, t, all_titles) for t in all_titles):
                                                    if year and str(year) not in str(getattr(it, 'infoLabels', {}).get('year', '')):
                                                        continue
                                                    lnk = _precheck_links(it)
                                                    if lnk:
                                                        matched_item = it
                                                        found_links[0] = lnk
                                                        break
                                    except:
                                        pass
                        if not matched_item or not found_links[0]:
                            next_page_item = None
                            for it in results:
                                raw_title_lower = it.title.lower()
                                if 'siguiente' in raw_title_lower or 'next' in raw_title_lower:
                                    next_page_item = it
                                    break
                            if next_page_item:
                                next_results = run_action_silent(next_page_item)
                                if next_results and isinstance(next_results, list):
                                    for it in next_results:
                                        if any(match_title(it.title, t, all_titles) for t in all_titles):
                                            if year and str(year) not in str(getattr(it, 'infoLabels', {}).get('year', '')):
                                                continue
                                            lnk = _precheck_links(it)
                                            if lnk:
                                                matched_item = it
                                                found_links[0] = lnk
                                                break
            except Exception as e:
                xbmc.log("Balandro Bridge Multi SEARCH ERROR: " + str(e), xbmc.LOGERROR)

        has_links = matched_item is not None and found_links[0] is not None
        if has_links:
            is_ep_mode = bool(season and episode)
            player_file_found = _find_player_file_for_channel(search_item.channel, is_ep_mode)
            if player_file_found:
                _save_last_player_context(
                    player_file_found, title, year, season, episode,
                    showname, showyear, title_es, title_lat, title_en, title_orig,
                    tmdb_id, imdb_id, tvdb_id, trakt_id,
                    getattr(matched_item, 'infoLabels', {}).get('plot', '') or getattr(matched_item, 'plot', ''),
                    getattr(matched_item, 'infoLabels', {}).get('tagline', '') or getattr(matched_item, 'tagline', ''),
                    getattr(matched_item, 'infoLabels', {}).get('director', '') or getattr(matched_item, 'director', '')
                )

            play_url = 'plugin://plugin.video.balandro.bridge.multi/?action=play&url=' + quote(matched_item.tourl())
            listitem = xbmcgui.ListItem(label=(_meta_ctx.get('title') or matched_item.title))
            listitem.setProperty('IsPlayable', 'true')

            info = {}
            if season and episode:
                info['mediatype'] = 'episode'
                info['title'] = _meta_ctx.get('title') or showname or title
                try: info['season'] = int(season)
                except: pass
                try: info['episode'] = int(episode)
                except: pass
                try:
                    if showyear: info['year'] = int(showyear)
                    elif getattr(matched_item, 'infoLabels', {}).get('year'):
                        info['year'] = int(getattr(matched_item, 'infoLabels', {}).get('year'))
                except: pass
                if showname: info['tvshowtitle'] = showname
                info['plot'] = _meta_ctx.get('plot') or ''
                if _meta_ctx.get('tagline'): info['tagline'] = _meta_ctx.get('tagline')
                if _meta_ctx.get('director'): info['director'] = _meta_ctx.get('director')
            else:
                info['mediatype'] = 'movie'
                info['title'] = _meta_ctx.get('title') or title or matched_item.title
                try:
                    if year: info['year'] = int(year)
                    elif getattr(matched_item, 'infoLabels', {}).get('year'):
                        info['year'] = int(getattr(matched_item, 'infoLabels', {}).get('year'))
                except: pass
                info['plot'] = _meta_ctx.get('plot') or ''
                if _meta_ctx.get('tagline'): info['tagline'] = _meta_ctx.get('tagline')
                if _meta_ctx.get('director'): info['director'] = _meta_ctx.get('director')

            set_listitem_info(listitem, info)

            unique_ids = {}
            if tmdb_id:  unique_ids['tmdb']  = str(tmdb_id)
            if imdb_id:  unique_ids['imdb']  = str(imdb_id)
            if tvdb_id:  unique_ids['tvdb']  = str(tvdb_id)
            if trakt_id: unique_ids['trakt'] = str(trakt_id)
            if not unique_ids:
                il = matched_item.infoLabels if hasattr(matched_item, 'infoLabels') else {}
                raw_imdb = il.get('imdb_id') or il.get('imdb') or ''
                raw_tmdb = il.get('tmdb')    or ''
                raw_tvdb = il.get('tvdb')    or ''
                if raw_imdb: unique_ids['imdb'] = str(raw_imdb)
                if raw_tmdb: unique_ids['tmdb'] = str(raw_tmdb)
                if raw_tvdb: unique_ids['tvdb'] = str(raw_tvdb)
            if unique_ids:
                default_id = 'tmdb' if 'tmdb' in unique_ids else ('imdb' if 'imdb' in unique_ids else '')
                _set_unique_ids(listitem, unique_ids, default_id)

            try:
                thumb = matched_item.thumbnail or ''
                fanart = matched_item.fanart or ''
                if thumb or fanart:
                    art = {}
                    if thumb: art['thumb'] = thumb; art['icon'] = thumb
                    if fanart: art['fanart'] = fanart
                    listitem.setArt(art)
            except: pass

            xbmcplugin.addDirectoryItem(handle, play_url, listitem, isFolder=False)
            xbmcplugin.endOfDirectory(handle, succeeded=True)
        else:
            xbmcplugin.endOfDirectory(handle, succeeded=False)

    # --- CASO B: Búsqueda Paralela (todos los canales) ---
    else:
        if os.path.exists(SEARCH_CACHE_FILE):
            try: os.remove(SEARCH_CACHE_FILE)
            except: pass

        _save_last_player_context(
            'BalandroMulti', title, year, season, episode,
            showname, showyear, title_es, title_lat, title_en, title_orig,
            tmdb_id, imdb_id, tvdb_id, trakt_id,
            best_plot or '',
            best_tagline or '',
            director or ''
        )

        play_url = 'plugin://plugin.video.balandro.bridge.multi/?action=play'
        # Usar best_title (resuelto desde TMDB DB) en lugar del parámetro 'title' crudo
        # ya que el '&' en títulos originales como 'Thelma & Louise' trunca el valor del parámetro URL
        _display_title = _meta_ctx.get('title') or best_title or title or showname or 'Balandro Multi'
        listitem = xbmcgui.ListItem(label=_display_title)
        listitem.setPath(play_url)
        listitem.setProperty('IsPlayable', 'true')

        info = {}
        if season and episode:
            info['mediatype'] = 'episode'
            info['title'] = _meta_ctx.get('title') or showname or title
            try: info['season'] = int(season)
            except: pass
            try: info['episode'] = int(episode)
            except: pass
            try:
                if showyear: info['year'] = int(showyear)
            except: pass
            if showname: info['tvshowtitle'] = showname
        else:
            info['mediatype'] = 'movie'
            info['title'] = _meta_ctx.get('title') or title
            try:
                if year: info['year'] = int(year)
            except: pass

        set_listitem_info(listitem, info)

        unique_ids = {}
        if tmdb_id:  unique_ids['tmdb']  = str(tmdb_id)
        if imdb_id:  unique_ids['imdb']  = str(imdb_id)
        if tvdb_id:  unique_ids['tvdb']  = str(tvdb_id)
        if trakt_id: unique_ids['trakt'] = str(trakt_id)
        if unique_ids:
            default_id = 'tmdb' if 'tmdb' in unique_ids else ('imdb' if 'imdb' in unique_ids else '')
            _set_unique_ids(listitem, unique_ids, default_id)

        xbmcplugin.addDirectoryItem(handle, play_url, listitem, isFolder=False)
        xbmcplugin.endOfDirectory(handle, succeeded=True)

def _direct_autoplay(links, matched_item, handle):
    """Muestra una ventana de progreso para autoplay secuencial, permitiendo cancelación manual."""
    import threading
    
    global _autoplay_in_progress, _autoplay_dialog, _autoplay_dialog_select_count
    _autoplay_in_progress = True
    _autoplay_dialog_select_count = 0
    
    # Los links ya vienen pre-ordenados por get_link_score desde run_parallel_search.
    # NO re-ordenar aquí para respetar ese orden.

    # Separar descarga directa y torrents. El autoplay solo se intentará para descarga directa.
    autoplay_links = []
    for lnk in links:
        server = getattr(lnk, 'server', '').lower() if hasattr(lnk, 'server') else ''
        url_lnk = getattr(lnk, 'url', '').lower() if hasattr(lnk, 'url') else ''
        is_torrent = 'torrent' in server or 'torrent' in url_lnk or 'magnet:' in url_lnk or 'elementum' in url_lnk or 'elementum' in server
        if not is_torrent:
            autoplay_links.append(lnk)
            
    xbmc.log("Balandro Bridge Multi: Iniciando Autoplay Secuencial para %d enlaces de descarga directa (de %d totales)" % (len(autoplay_links), len(links)), xbmc.LOGINFO)
    
    _guardian_stop = [False]
    _pre_kill_stop = [False]  # flag compartido para detener ambos hilos
    
    def _error_guardian():
        import time
        while not _guardian_stop[0] and not _pre_kill_stop[0]:
            time.sleep(0.05)
            if xbmc.Player().isPlaying():
                continue
            if not _guardian_stop[0] and not _pre_kill_stop[0]:
                xbmc.executebuiltin('Dialog.Close(okdialog,true)')
                xbmc.executebuiltin('Dialog.Close(error,true)')

    _guardian_thread = threading.Thread(target=_error_guardian, daemon=True)
    _guardian_thread.start()

    # Pre-matador: cierra el dialogo de error antes de que llegue a mostrarse
    def _pre_kill_error_dialog():
        import time
        for _ in range(30000):
            if _guardian_stop[0] or _pre_kill_stop[0]:
                break
            xbmc.executebuiltin('Dialog.Close(okdialog,true)')
            xbmc.executebuiltin('Dialog.Close(error,true)')
            if xbmc.Player().isPlaying():
                break
            time.sleep(0.01)

    threading.Thread(target=_pre_kill_error_dialog, daemon=True).start()

    _orig_play_fake = platformtools.play_fake
    active_handle = '-1'
    
    p_dialog = xbmcgui.DialogProgress()
    _autoplay_dialog = p_dialog
    p_dialog.create("Autoplay - Balandro Multi", "Inicializando Autoplay...")
    
    autoplay_succeeded = False
    canceled_by_user = False
    
    total_links = len(autoplay_links)
    for idx, lnk in enumerate(autoplay_links):
        # Resetear el contador de dialog_select para cada nuevo enlace
        _autoplay_dialog_select_count = 0
        if p_dialog.iscanceled():
            xbmc.log("Balandro Bridge Multi Autoplay: Cancelado por el usuario", xbmc.LOGINFO)
            canceled_by_user = True
            break
            
        server = getattr(lnk, 'server', '').lower() if hasattr(lnk, 'server') else ''
        url_lnk = getattr(lnk, 'url', '').lower() if hasattr(lnk, 'url') else ''
        is_torrent = 'torrent' in server or 'torrent' in url_lnk or 'magnet:' in url_lnk or 'elementum' in url_lnk or 'elementum' in server
        
        # Obtener información amigable
        lnk_title = getattr(lnk, 'title', '')
        lnk_server = getattr(lnk, 'server', 'Servidor').capitalize()
        if lnk_server.lower() in ('various', 'directo'):
            other_val = getattr(lnk, 'other', '')
            if other_val:
                lnk_server = other_val.capitalize()
                
        lnk_lang = getattr(lnk, 'language', '')
        if not lnk_lang or lnk_lang == 'Ninguno':
            lnk_lang = _detect_language(lnk_title)
            
        lnk_qual = getattr(lnk, 'quality', '')
        if not lnk_qual or lnk_qual == 'Ninguno':
            lnk_qual = _detect_quality(lnk_title)
        
        lang_map = {
            "Latino": "Lat", "Lat": "Lat", "lat": "Lat", 
            "Castellano": "Cast", "Esp": "Cast", "esp": "Cast", 
            "VOSE": "Vose", "Vose": "Vose", "vose": "Vose", 
            "Ingles": "Eng", "English": "Eng", "Eng": "Eng"
        }
        lang_abbrev = lang_map.get(lnk_lang, lnk_lang)
        
        parts = [lnk_server]
        if lang_abbrev and lang_abbrev != "Ninguno" and lang_abbrev != "":
            parts.append(lang_abbrev)
        if lnk_qual and lnk_qual != "Ninguno" and lnk_qual != "":
            parts.append(lnk_qual)
            
        friendly_name = " ".join(parts)
        percent = int(((idx) / float(total_links)) * 100)

        msg_lines = [
            "Autoplay en curso...",
            "[COLOR gold]%s[/COLOR]" % friendly_name,
            "[COLOR silver]%d de %d enlaces[/COLOR]" % (idx + 1, total_links)
        ]
        p_dialog.update(percent, "\n".join(msg_lines))
        
        play_fake_intercepted = [False]
        def patched_play_fake(resuelto=False):
            if not resuelto:
                play_fake_intercepted[0] = True
            else:
                _orig_play_fake(resuelto)

        platformtools.play_fake = patched_play_fake
        try:
            sys.argv[1] = active_handle
            apply_monkeypatch()
            _inject_meta_into_items([lnk], matched_item)
            platformtools.play_from_itemlist([lnk], matched_item)
        except Exception as e_play:
            xbmc.log("Balandro Bridge Multi Autoplay Playback Error: " + str(e_play), xbmc.LOGERROR)
            play_fake_intercepted[0] = True
        finally:
            platformtools.play_fake = _orig_play_fake

        started = False
        if not play_fake_intercepted[0]:
            max_seconds = 90 if is_torrent else 8
            max_loops = int(max_seconds * 2)
            progress_seen = False
            progress_inactive_count = 0
            
            for _ in range(max_loops):
                xbmc.sleep(500)
                if p_dialog.iscanceled():
                    canceled_by_user = True
                    break
                if xbmc.Player().isPlaying():
                    started = True
                    break
                if is_torrent:
                    progress_active = (
                        xbmc.getCondVisibility('Window.IsActive(progressdialog)') or
                        xbmc.getCondVisibility('Window.IsActive(10101)') or
                        xbmc.getCondVisibility('Window.IsActive(10151)') or
                        xbmc.getCondVisibility('Window.IsVisible(progressdialog)')
                    )
                    if progress_active:
                        progress_seen = True
                        progress_inactive_count = 0
                    elif progress_seen:
                        progress_inactive_count += 1
                        if progress_inactive_count >= 6:
                            break
                            
        if canceled_by_user or p_dialog.iscanceled():
            canceled_by_user = True
            break
            
        if started:
            p_dialog.close()  # Cerrar la ventana inmediatamente para liberar la pantalla
            load_error_detected = False
            played_secs = 0.0

            # Esperar hasta que el video supere los 60s de reproducción confirmada (primer minuto).
            # Si el player se detiene solo (error o corte de streaming) antes de 60s → salta al siguiente servidor.
            # Si está buffereando pero sigue activo → paciencia, no interrumpir.
            # Damos un margen de hasta 180s de tiempo real (por si hay buffering lento) para alcanzar los 60s de reproducción.
            for _mon in range(180):  # 180 × 1000ms = 180s máximo de tiempo real
                xbmc.sleep(1000)
                if canceled_by_user or p_dialog.iscanceled():
                    canceled_by_user = True
                    break
                if not xbmc.Player().isPlaying():
                    # El reproductor se detuvo antes del primer minuto → error de carga / corte
                    if played_secs < 60.0:
                        load_error_detected = True
                    break
                try: played_secs = xbmc.Player().getTime()
                except: pass
                if played_secs >= 60.0:
                    break  # Primer minuto superado con éxito

            if canceled_by_user:
                break

            # Si transcurrieron los 180s y el video no superó los 60s de reproducción → fallo de carga
            if not load_error_detected and played_secs < 60.0:
                load_error_detected = True

            if not load_error_detected and xbmc.Player().isPlaying() and played_secs >= 60.0:
                xbmc.log("Balandro Bridge Multi Autoplay: Reproducción exitosa en %s (primer minuto superado)" % friendly_name, xbmc.LOGINFO)
                autoplay_succeeded = True
                break

        # Si no se logró reproducir con éxito, forzar la detención del reproductor
        if not autoplay_succeeded:
            try: xbmc.Player().stop()
            except: pass

            # Si falló la carga del enlace, reabrir el diálogo de progreso para el siguiente intento
            p_dialog = xbmcgui.DialogProgress()
            _autoplay_dialog = p_dialog
            p_dialog.create("Autoplay - Balandro Multi", "Reintentando Autoplay...")
            # Limpiar diálogos de error de Kodi silenciosamente y continuar
            xbmc.executebuiltin('Dialog.Close(okdialog,true)')
            xbmc.executebuiltin('Dialog.Close(error,true)')
            xbmc.sleep(400)
            
    p_dialog.close()
    _autoplay_dialog = None
    _autoplay_in_progress = False
    _guardian_stop[0] = True
    _pre_kill_stop[0] = True
    
    # Si falló el autoplay o fue cancelado por el usuario, mostrar el diálogo de selección manual original
    if not autoplay_succeeded:
        xbmc.log("Balandro Bridge Multi: Autoplay no completado (cancelado o fallido). Mostrando selección manual...", xbmc.LOGINFO)
        _orig_dialog_select = platformtools.dialog_select
        
        def patched_dialog_select(heading, _list, autoclose=0, preselect=-1, useDetails=False):
            res = _orig_dialog_select(heading, _list, autoclose=autoclose, preselect=preselect, useDetails=useDetails)
            if res is None or res < 0:
                return -1
            return res
            
        platformtools.dialog_select = patched_dialog_select
        try:
            sys.argv[1] = active_handle
            apply_monkeypatch()
            _inject_meta_into_items(links, matched_item)
            platformtools.play_from_itemlist(links, matched_item)
        finally:
            platformtools.dialog_select = _orig_dialog_select
            
        # Si el usuario canceló el selector manual también, cerrar el handle de reproducción de Kodi
        try:
            _original_setResolvedUrl(handle, False, xbmcgui.ListItem())
        except Exception:
            pass

    _clear_resume_state()

def run_parallel_search():
    global title, year, season, episode, showname, showyear, title_es, title_lat, title_en, title_orig, tmdb_id
    global _bridge_target_tmdb_id
    if not title and not showname:
        ctx = _load_last_player_context()
        if ctx:
            title = ctx.get('title') or title
            year = ctx.get('year') or year
            season = ctx.get('season') or season
            episode = ctx.get('episode') or episode
            showname = ctx.get('showname') or showname
            showyear = ctx.get('showyear') or showyear
            title_es = ctx.get('title_es') or title_es
            title_lat = ctx.get('title_lat') or title_lat
            title_en = ctx.get('title_en') or title_en
            title_orig = ctx.get('title_orig') or title_orig
            if not tmdb_id:
                tmdb_id = ctx.get('tmdb') or tmdb_id

    # Guardar en variable global para que los sub-hilos de set_infoLabels puedan acceder
    _bridge_target_tmdb_id = tmdb_id

    # Si no tenemos título en español pero sí tmdb_id, intentar resolverlo ahora
    _eng_cands = set(t for t in [title_en, title_orig, title] if t and t != '_')
    if tmdb_id and (not title_es or not title_lat or title_es in _eng_cands or title_lat in _eng_cands):
        is_ep_ps = bool(season and episode)
        _sp = _resolve_missing_spanish_titles(tmdb_id, is_ep_ps)
        if _sp and _sp not in _eng_cands:
            xbmc.log("Balandro Bridge Multi: Titulo espanol resuelto via API para busqueda: '%s'" % _sp, xbmc.LOGINFO)
            if not title_es or title_es in _eng_cands:
                title_es = _sp
            if not title_lat or title_lat in _eng_cands:
                title_lat = _sp

    # Limpiar title corrupto (????): ocurre cuando comillas tipográficas en el título
    # original (ej: "The Shadow's Edge") rompen la codificación URL de Kodi
    if title and all(c in '? ' for c in title):
        xbmc.log("Balandro Bridge Multi: [run_parallel] Titulo corrupto '%s', descartando" % title, xbmc.LOGWARNING)
        title = ''

    # Enriquecer title_en / title_orig desde la DB si llegan vacíos
    # (el JSON del player los omite para evitar truncamiento por '&')
    if tmdb_id and (not title_en or not title_orig):
        is_ep_orig = bool(season and episode)
        _db_orig = _get_original_title_from_db(tmdb_id, is_ep_orig)
        if _db_orig:
            if not title_orig:
                title_orig = _db_orig
            if not title_en:
                title_en = _db_orig
            xbmc.log("Balandro Bridge Multi: [run_parallel] Titulo ingles recuperado de DB: '%s'" % _db_orig, xbmc.LOGINFO)

    is_episode = bool(season and episode)
    enabled_players = []
    try:
        for fname in os.listdir(TMDB_PLAYERS_PATH):
            if not fname.endswith('.json') or fname.endswith('.disabled'): continue
            if fname == '(1)BalandroMulti.json': continue
            
            fpath = os.path.join(TMDB_PLAYERS_PATH, fname)
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if data.get('plugin') in ('plugin.video.balandro.bridge', 'plugin.video.balandro.bridge.multi'):
                    # Filtrar por tipo de contenido
                    if is_episode:
                        if 'play_episode' not in data:
                            continue
                    else:
                        if 'play_movie' not in data:
                            continue
                    enabled_players.append(fname)
            except: pass
    except Exception as e:
        xbmc.log("Balandro Bridge Multi: Error al leer players - " + str(e), xbmc.LOGERROR)

    xbmc.log("Balandro Bridge Multi: Busqueda paralela con %d canales: %s" % (len(enabled_players), str(enabled_players)), xbmc.LOGINFO)
    if not enabled_players:
        xbmc.log("Balandro Bridge Multi: No se encontraron players habilitados.", xbmc.LOGWARNING)
        return [], None

    addon = xbmcaddon.Addon('plugin.video.balandro.bridge.multi')
    timeout = int(addon.getSetting('search_timeout') or 40)
    if timeout < 10:
        timeout = 10

    results_dict = {}
    threads = []
    
    def thread_target(p_file):
        try:
            res = _search_on_player(
                p_file, title, year, season, episode,
                showname, showyear, title_es, title_lat, title_en, title_orig
            )
            if res and res[0] and res[1]:
                results_dict[p_file] = res
        except Exception as e_th:
            import traceback as _tb_th
            xbmc.log("Balandro Bridge Multi thread_target [%s] EXCEPTION: %s\n%s" % (p_file, str(e_th), _tb_th.format_exc()), xbmc.LOGERROR)

    for pf in enabled_players:
        t = threading.Thread(target=thread_target, args=(pf,), daemon=True)
        threads.append(t)
        t.start()

    p_dialog = xbmcgui.DialogProgress()
    p_dialog.create("Buscando enlaces en los canales", "Iniciando búsqueda...")

    start_time = time.time()
    total_channels = len(enabled_players)

    finished_channels = []
    reported_done = set()

    def clean_name(pf):
        n = pf.replace('.json', '').replace('.disabled', '')
        m = re.match(r'^\(\d+\)(.+)', n)
        n = m.group(1) if m else n
        return re.sub(r'-(Series|Movies?)$', '', n, flags=re.IGNORECASE).strip()

    dialog_closed = False
    while not xbmc.Monitor().abortRequested():
        alive_threads = [t for t in threads if t.is_alive()]
        completed = total_channels - len(alive_threads)
        percent = int((completed / float(total_channels)) * 100)

        for idx, pf in enumerate(enabled_players):
            if pf not in reported_done and not threads[idx].is_alive():
                reported_done.add(pf)
                found = pf in results_dict
                finished_channels.append((clean_name(pf), found))

        lines = []
        for (cname, found) in finished_channels[-3:]:
            icon = "[COLOR chartreuse][B]OK[/B][/COLOR]" if found else "[COLOR gray]--[/COLOR]"
            lines.append("[%s] %s" % (icon, cname))

        active_count = len(alive_threads)
        if active_count > 0:
            lines.append("[COLOR gold]Buscando en %d canal(es) mas...[/COLOR]" % active_count)

        msg = "\n".join(lines) if lines else "Iniciando..."
        header = "Completados: [COLOR chartreuse]%d[/COLOR] / %d  (%d%%)" % (completed, total_channels, percent)

        if not dialog_closed:
            try:
                if p_dialog.iscanceled():
                    dialog_closed = True
                else:
                    p_dialog.update(percent, header + "\n" + msg)
            except:
                dialog_closed = True

        if completed == total_channels:
            break
        if time.time() - start_time >= timeout:
            xbmc.log("Balandro Bridge Multi: Búsqueda paralela excedió el tiempo límite.", xbmc.LOGINFO)
            break
        xbmc.sleep(120)

    p_dialog.close()

    consolidated_links = []
    base_item = None

    for pf, (matched_item, links) in results_dict.items():
        if not base_item: base_item = matched_item
        
        chan_label = pf.replace('.json', '').replace('.disabled', '')
        m = re.match(r'^\(\d+\)(.+)', chan_label)
        chan_label = m.group(1) if m else chan_label
        chan_label = re.sub(r'-(Series|Movies?)$', '', chan_label, flags=re.IGNORECASE)

        for lnk in links:
            new_lnk = lnk.clone()
            orig_title = getattr(lnk, 'title', 'Enlace')
            new_lnk.title = "[COLOR deepskyblue][%s][/COLOR] %s" % (chan_label, orig_title)
            consolidated_links.append(new_lnk)

    # Ordenar usando exactamente los mismos filtros que Balandro aplica en play_from_itemlist:
    # filter_and_sort_by_quality → filter_and_sort_by_server → filter_and_sort_by_language
    # Así el orden del autoplay siempre coincide con el selector manual de Balandro.
    try:
        consolidated_links = servertools.filter_and_sort_by_quality(consolidated_links)
        consolidated_links = servertools.filter_and_sort_by_server(consolidated_links)
        consolidated_links = servertools.filter_and_sort_by_language(consolidated_links)
        xbmc.log("Balandro Bridge Multi: Links ordenados con filtros de Balandro. Total: %d" % len(consolidated_links), xbmc.LOGINFO)
    except Exception as e_sort:
        xbmc.log("Balandro Bridge Multi: Error al ordenar con filtros de Balandro - " + str(e_sort), xbmc.LOGWARNING)

    return consolidated_links, base_item

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        import traceback
        xbmc.log('Balandro Bridge Multi ERROR global: ' + traceback.format_exc(), xbmc.LOGERROR)
        try: xbmcplugin.endOfDirectory(handle, succeeded=False)
        except: pass
