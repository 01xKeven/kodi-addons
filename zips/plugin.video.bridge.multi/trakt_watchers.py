# -*- coding: utf-8 -*-
"""
Multi Bridge - Módulo de Espectadores en Vivo de Trakt.tv (Trickplay / Social Overlay)
Diseño de cápsula flotante estilo Apple (Dynamic Island / Frosted Glass):
- Soporte de posición configurable en los ajustes del addon:
  * Abajo (por defecto): Encima de la barra de navegación / seekbar (Y = 870)
  * Arriba: Centro superior (Y = 32) estilo Dynamic Island
- Centrado horizontal exacto (lejos del reloj gigante de Kodi)
- Alineación óptica milimétrica: punto rojo, LIVE, divisor, contador y avatares perfectamente en línea
- Avatares circulares limpios (36x36) con precarga antes del render para animación conjunta
- Insignia "+N" circular perfectamente legible y centrada con ancho holgado (sin '...')
- Ancho generoso en LIVE (70px) y contador (220px) para evitar el marquee auto-scroll de Kodi
- Animación suave con curva de desaceleración (ease-out) y salida rápida sincronizada
- Inyectado en Window(12005) durante la reproducción para no bloquear el OSD ni la tecla Atrás
"""

import os
import sys
import time
import math
import json
import io
import threading
import urllib.request
import urllib.error
import warnings

warnings.filterwarnings('ignore', category=UserWarning)

import xbmc
import xbmcgui
import xbmcaddon
import xbmcvfs

# Inyectar soporte nativo WebP para Pillow desde resources/lib
try:
    import PIL
    _addon_base = xbmcvfs.translatePath('special://home/addons/plugin.video.bridge.multi/')
    _lib_pil = os.path.join(_addon_base, 'resources', 'lib', 'PIL')
    if os.path.exists(_lib_pil) and _lib_pil not in getattr(PIL, '__path__', []):
        PIL.__path__.append(_lib_pil)
    import PIL.WebPImagePlugin
    from PIL import Image, ImageDraw, ImageFilter
    HAS_PIL = True
except Exception:
    try:
        from PIL import Image, ImageDraw, ImageFilter
        HAS_PIL = True
    except Exception:
        HAS_PIL = False

TRAKT_CLIENT_ID = '9e65c7bb4a5b786a5b5df3e7371c3bcdfa00e26a49078ea20288d616fe83d84d'

_ID_CACHE = {}
_WATCHERS_CACHE = {}

# Gestor global del HUD
_HUD_LOCK = threading.RLock()
_CURRENT_HUD = None
_LAST_OSD_STATE = False
_OSD_COOLDOWN = 0
_TIMED_OVERLAY_ACTIVE = False


def _get_addon():
    try:
        return xbmcaddon.Addon('plugin.video.bridge.multi')
    except Exception:
        return None


def _get_addon_path():
    a = _get_addon()
    if a:
        return a.getAddonInfo('path')
    return xbmcvfs.translatePath('special://home/addons/plugin.video.bridge.multi/')


def _get_overlay_is_top():
    """Lee el ajuste de posición configurado por el usuario (0: Abajo, 1: Arriba)"""
    try:
        a = _get_addon()
        if a:
            pos = a.getSetting('trakt_live_overlay_position')
            return pos == '1' or 'arriba' in str(pos).lower()
    except Exception:
        pass
    return False


def _get_screen_dimensions(window=None):
    """
    Obtiene las dimensiones reales del lienzo gráfico de Kodi de forma 100% confiable
    probando sucesivamente la ventana activa, getScreenWidth/Height y System.ScreenWidth/Height.
    """
    w, h = 0, 0
    if window is not None:
        try:
            w = window.getWidth()
            h = window.getHeight()
        except Exception:
            pass

    if not w or not h or w <= 0 or h <= 0:
        try:
            w = xbmcgui.getScreenWidth()
            h = xbmcgui.getScreenHeight()
        except Exception:
            pass

    if not w or not h or w <= 0 or h <= 0:
        try:
            sw_str = xbmc.getInfoLabel('System.ScreenWidth')
            sh_str = xbmc.getInfoLabel('System.ScreenHeight')
            if sw_str and sh_str:
                w = int(sw_str)
                h = int(sh_str)
        except Exception:
            pass

    if not w or w <= 0:
        w = 1920
    if not h or h <= 0:
        h = 1080

    return w, h


def _get_media_asset(filename):
    """Devuelve la ruta absoluta de un archivo gráfico de la skin del addon"""
    p = os.path.join(_get_addon_path(), 'resources', 'skins', 'Default', 'media', filename)
    if os.path.exists(p):
        return p
    # Respaldo en resources/media
    p2 = os.path.join(_get_addon_path(), 'resources', 'media', filename)
    if os.path.exists(p2):
        return p2
    return filename


def _get_avatar_cache_dir():
    base = xbmcvfs.translatePath('special://profile/addon_data/plugin.video.bridge.multi/avatars/')
    if not os.path.exists(base):
        try:
            os.makedirs(base)
        except Exception:
            pass
    return base


def _download_and_crop_avatar(avatar_url, username):
    """Descarga el avatar de Trakt y lo recorta en un círculo anti-aliasing de 36x36 con máscara estricta"""
    if not avatar_url:
        return _get_media_asset('avatar_default_36.png')

    cdir = _get_avatar_cache_dir()
    safe_name = "".join([c for c in username if c.isalnum() or c in ('_', '-')]) or 'trakt_user'
    out_file = os.path.join(cdir, "%s_36.png" % safe_name)

    if os.path.exists(out_file) and os.path.getsize(out_file) > 200:
        return out_file

    try:
        req = urllib.request.Request(
            avatar_url,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        )
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            raw = resp.read()

        if HAS_PIL:
            try:
                img = Image.open(io.BytesIO(raw)).convert('RGBA')
                w, h = img.size
                min_d = min(w, h)
                left = (w - min_d) // 2
                top = (h - min_d) // 2
                img = img.crop((left, top, left + min_d, top + min_d))
                img = img.resize((144, 144), Image.Resampling.LANCZOS)

                # Máscara circular anti-aliasing estricta que corta cualquier desborde exterior
                mask = Image.new('L', (144, 144), 0)
                draw_m = ImageDraw.Draw(mask)
                draw_m.ellipse((0, 0, 143, 143), fill=255)
                img.putalpha(mask)

                # Borde perimetral circular fino y sutil
                draw_b = ImageDraw.Draw(img)
                draw_b.ellipse((0, 0, 143, 143), outline=(255, 255, 255, 38), width=5)

                # Guardar a 72x72 para alta fidelidad tanto en 720p, 1080p como en 4K
                img = img.resize((72, 72), Image.Resampling.LANCZOS)
                img.save(out_file, format='PNG')
                return out_file
            except Exception:
                pass

        ext = '.webp' if raw.startswith(b'RIFF') else '.jpg'
        raw_file = os.path.join(cdir, f"{safe_name}{ext}")
        with open(raw_file, 'wb') as f:
            f.write(raw)
        return raw_file
    except Exception:
        return _get_media_asset('avatar_default_36.png')


def _generate_badge_asset(diff, badge_size=38):
    """Genera una textura circular de cristal con '+N' perfectamente centrado y renderizado por PIL"""
    fname = f'avatar_more_{badge_size}_{diff}.png'
    cdir = _get_avatar_cache_dir()
    fpath = os.path.join(cdir, fname)
    if os.path.exists(fpath) and os.path.getsize(fpath) > 200:
        return fpath

    if not HAS_PIL:
        return _get_media_asset('avatar_more_36.png')

    try:
        scale = 3
        sz = badge_size * scale
        img = Image.new('RGBA', (sz, sz), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        # Fondo circular carmesí obsidiana
        d.ellipse([0, 0, sz - 1, sz - 1], fill=(42, 16, 22, 235))
        # Anillo de acento rojo brillante
        d.ellipse([int(2 * scale), int(2 * scale), sz - int(2 * scale) - 1, sz - int(2 * scale) - 1],
                  outline=(237, 40, 50, 210), width=int(1.5 * scale))
        # Borde exterior oscuro
        d.ellipse([0, 0, sz - 1, sz - 1], outline=(15, 16, 21, 255), width=int(2 * scale))

        # Texto centrado con tamaño dinámico proporcional a badge_size para cualquier resolución
        txt = f'+{diff}'
        f_ratio = 0.35 if len(txt) <= 3 else (0.29 if len(txt) == 4 else 0.25)
        f_size = max(7.0, badge_size * f_ratio)
        try:
            from PIL import ImageFont
            font = ImageFont.truetype('arial.ttf', int(f_size * scale))
        except Exception:
            font = ImageFont.load_default()

        d.text((sz // 2, sz // 2), txt, font=font, fill=(255, 255, 255, 255), anchor='mm')
        res = img.resize((badge_size, badge_size), Image.Resampling.LANCZOS)
        res.save(fpath, format='PNG')
        return fpath
    except Exception:
        return _get_media_asset('avatar_more_36.png')


def _get_apple_pill_texture(w, h):
    """Genera y cachea una cápsula de cristal obsidiana con bordes redondeados y relieve Apple"""
    stock = _get_media_asset(f'apple_pill_{w}_{h}.png')
    if os.path.exists(stock):
        return stock

    cdir = _get_avatar_cache_dir()
    cache_file = os.path.join(cdir, f'apple_pill_{w}_{h}.png')
    if os.path.exists(cache_file):
        return cache_file

    if not HAS_PIL:
        stock_fb = _get_media_asset('apple_pill_500_50.png')
        if os.path.exists(stock_fb):
            return stock_fb
        return _get_media_asset('white.png')

    try:
        scale = 3
        sw, sh = w * scale, h * scale
        r = (h // 2) * scale

        img = Image.new('RGBA', (sw, sh), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Cristal obsidiana de alta profundidad
        draw.rounded_rectangle([0, 0, sw - 1, sh - 1], radius=r, fill=(15, 16, 21, 238))

        # Perímetro de cristal reflectante Apple
        draw.rounded_rectangle([scale, scale, sw - scale - 1, sh - scale - 1], radius=r - scale, outline=(255, 255, 255, 42), width=int(1.5 * scale))

        # Trazo sutil carmesí Trakt a lo largo del perfil
        accent = Image.new('RGBA', (sw, sh), (0, 0, 0, 0))
        a_draw = ImageDraw.Draw(accent)
        a_draw.rounded_rectangle([0, 0, sw - 1, sh - 1], radius=r, outline=(237, 28, 36, 65), width=int(1.5 * scale))
        img.alpha_composite(accent)

        final_img = img.resize((w, h), Image.Resampling.LANCZOS)
        final_img.save(cache_file, format='PNG')
        return cache_file
    except Exception:
        stock_fb = _get_media_asset('apple_pill_500_50.png')
        if os.path.exists(stock_fb):
            return stock_fb
        return _get_media_asset('white.png')


def _get_headers():
    return {
        'Content-Type': 'application/json',
        'trakt-api-version': '2',
        'trakt-api-key': TRAKT_CLIENT_ID,
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }


def resolve_trakt_id(tmdb_id, is_series=False):
    """Resuelve el ID de Trakt a partir del TMDb ID mediante /search/tmdb/"""
    if not tmdb_id:
        return None
    key = ('show' if is_series else 'movie', str(tmdb_id))
    if key in _ID_CACHE:
        return _ID_CACHE[key]

    mtype = 'show' if is_series else 'movie'
    url = 'https://api.trakt.tv/search/tmdb/%s?type=%s' % (tmdb_id, mtype)
    try:
        req = urllib.request.Request(url, headers=_get_headers())
        with urllib.request.urlopen(req, timeout=3.5) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            if data and isinstance(data, list):
                obj = data[0].get(mtype) or {}
                ids = obj.get('ids') or {}
                tid = ids.get('trakt') or ids.get('imdb') or ids.get('slug')
                if tid:
                    _ID_CACHE[key] = str(tid)
                    return str(tid)
    except Exception as e:
        xbmc.log("Multi Bridge Trakt resolve_trakt_id error: " + str(e), xbmc.LOGINFO)
    return None


def fetch_live_watchers(meta, timeout=3.5, force=False):
    """Consulta a Trakt.tv qué usuarios están viendo la película o episodio ahora"""
    if not isinstance(meta, dict):
        return {'count': 0, 'is_series': False, 'users': []}

    s_val = meta.get('season')
    e_val = meta.get('episode')
    is_series = bool(s_val and e_val)

    imdb_id = str(meta.get('imdb') or '').strip()
    trakt_id = str(meta.get('trakt') or '').strip()
    tmdb_id = str(meta.get('tmdb') or '').strip()

    target_id = None
    if trakt_id:
        target_id = trakt_id
    elif imdb_id and imdb_id.startswith('tt'):
        target_id = imdb_id
    elif tmdb_id:
        target_id = resolve_trakt_id(tmdb_id, is_series=is_series)

    if not target_id:
        return {
            'count': 0,
            'is_series': is_series,
            'title': meta.get('title') or meta.get('showname') or '',
            'season': s_val,
            'episode': e_val,
            'users': []
        }

    cache_key = ("%s_S%sE%s" % (target_id, s_val, e_val)) if is_series else str(target_id)
    cached = _WATCHERS_CACHE.get(cache_key)
    if not force and cached and (time.time() - cached.get('ts', 0)) < 12:
        return cached.get('data')

    if is_series:
        try:
            s_num = int(s_val)
            e_num = int(e_val)
        except Exception:
            s_num, e_num = s_val, e_val
        url = 'https://api.trakt.tv/shows/%s/seasons/%s/episodes/%s/watching?extended=full' % (target_id, s_num, e_num)
    else:
        url = 'https://api.trakt.tv/movies/%s/watching?extended=full' % target_id

    data = []
    try:
        req = urllib.request.Request(url, headers=_get_headers())
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            if not isinstance(data, list):
                data = []
    except Exception as e:
        xbmc.log("Multi Bridge Trakt fetch_live_watchers error: " + str(e), xbmc.LOGINFO)
        data = []

    users = []
    for item in data:
        u = item.get('user') if (isinstance(item, dict) and 'user' in item) else item
        if not isinstance(u, dict):
            continue
        uname = str(u.get('username') or '').strip()
        name = str(u.get('name') or uname or '').strip()
        imgs = u.get('images') or {}
        avatar_obj = imgs.get('avatar') or {}
        avatar_url = avatar_obj.get('full') if isinstance(avatar_obj, dict) else ''
        loc = str(u.get('location') or '').strip()
        vip = bool(u.get('vip') or u.get('vip_ep'))

        users.append({
            'username': uname,
            'name': name,
            'avatar': avatar_url or '',
            'location': loc,
            'vip': vip
        })

    result = {
        'count': len(users),
        'is_series': is_series,
        'title': meta.get('title') or meta.get('showname') or '',
        'season': s_val,
        'episode': e_val,
        'users': users
    }
    _WATCHERS_CACHE[cache_key] = {'ts': time.time(), 'data': result}
    return result


class AppleControlAnim(object):
    """Control animable con elevación/descenso suave y fundido de opacidad coordinado"""
    def __init__(self, ctrl, base_x, base_y, rgb='FFFFFF', is_label=False, max_alpha=0xFF, is_top=False, label_pattern=None, slide_dist=35):
        self.ctrl = ctrl
        self.base_x = base_x
        self.base_y = base_y
        self.rgb = rgb
        self.is_label = is_label
        self.max_alpha = max_alpha
        self.is_top = is_top
        self.label_pattern = label_pattern
        self.slide_dist = slide_dist

    def apply(self, p):
        p = max(0.0, min(1.0, p))
        alpha = int(round(p * self.max_alpha))
        # Desplazamiento vertical suave responsivo
        dy = int(round((1.0 - p) * (-self.slide_dist if self.is_top else self.slide_dist)))
        try:
            self.ctrl.setPosition(self.base_x, self.base_y + dy)
            if not self.is_label:
                if self.rgb is not None:
                    self.ctrl.setColorDiffuse(f"{alpha:02X}{self.rgb}")
            else:
                if self.label_pattern:
                    self.ctrl.setLabel(self.label_pattern(alpha))
        except Exception:
            pass


class TraktLiveHUD(object):
    """
    HUD Cápsula Apple Floating (Dynamic Island)
    - Totalmente adaptativo y responsivo a cualquier resolución de pantalla (720p, 1080p, 2K, 4K)
    - Posición seleccionable: Abajo (encima del seekbar) o Arriba (Dynamic Island)
    - Centrado horizontal exacto respecto al ancho real del lienzo gráfico
    - Alineación óptica milimétrica con soporte para pantallas táctiles y televisores
    - Avatares circulares limpios e insignia +N con precarga total para animación conjunta
    - Textos con anchos generosos que eliminan totalmente '...' y el auto-scroll
    - Entrada suave con curva cúbica desacelerada (ease-out ~280ms) y salida simultánea (~160ms)
    - Inyectado en Window(12005) para respetar el OSD y la navegación de Kodi
    """
    HEIGHT = 50

    def __init__(self, is_preview=False):
        self.is_preview = is_preview
        self.controls = []
        self.anims = []
        self.lock = threading.RLock()
        self.closed = False
        self.dialog_canvas = None
        self.is_top = _get_overlay_is_top()

        p = xbmc.Player()
        if not is_preview and p.isPlayingVideo():
            self.window = xbmcgui.Window(12005)
        else:
            self.dialog_canvas = xbmcgui.WindowDialog()
            self.window = self.dialog_canvas

        # Detección dinámica y robusta de la resolución activa del canvas de Kodi
        self.win_w, self.win_h = _get_screen_dimensions(self.window)
        # Factor de escala proporcional respecto a la resolución base de diseño (1920x1080)
        self.scale = max(0.5, min(3.0, self.win_h / 1080.0))

        if self.is_top:
            self.y_pos = max(10, int(round(32 * self.scale)))
        else:
            # En 1080p: 1080 - 210 = 870 px. En 720p: 720 - 140 = 580 px.
            self.y_pos = self.win_h - int(round(210 * self.scale))

        self.ctrl_num = None
        self.ctrl_badge = None
        self.avatar_files = []
        self.badge_size = max(20, int(round(38 * self.scale)))
        self.current_count = 0

    def _animate(self, forward=True):
        if forward:
            # Entrada: Curva de desaceleración suave (cubic ease-out) en 12 pasos (~280ms)
            steps = [1.0 - (1.0 - (i / 12.0))**3 for i in range(13)]
            delay = 0.022
        else:
            # Salida: Curva de aceleración rápida (ease-in) y simultánea en 8 pasos (~160ms)
            steps = [(1.0 - (i / 8.0))**2 for i in range(9)]
            delay = 0.018

        for p in steps:
            if self.closed and forward:
                break
            for a in self.anims:
                a.apply(p)
            time.sleep(delay)

    def _pulse_loop(self, ctrl_dot):
        """Animación de palpitar (pulsing / breathing) continua para el punto rojo estilo Live"""
        start_t = time.time()
        while not self.closed:
            elapsed = time.time() - start_t
            # Ciclo de respiración rítmico de ~1.3s: modula la opacidad entre 0x40 (resplandor suave) y 0xFF (brillo máximo)
            pulse = 0.5 + 0.5 * math.sin(elapsed * 4.8)
            alpha = int(round(64 + 191 * pulse))
            try:
                ctrl_dot.setColorDiffuse(f"{alpha:02X}FFFFFF")
            except Exception:
                pass
            time.sleep(0.04)

    def _live_watchers_poller(self, meta):
        """Monitorea Trakt cada 10-12s mientras el OSD está abierto y actualiza el número en vivo si cambia"""
        while not self.closed:
            for _ in range(22):
                if self.closed:
                    return
                time.sleep(0.5)

            if self.closed:
                return

            try:
                new_data = fetch_live_watchers(meta, force=True)
                if not new_data or self.closed:
                    continue

                new_count = new_data.get('count', 0)
                if new_count != self.current_count:
                    self.update_live_count(new_data)
            except Exception:
                pass

    def update_live_count(self, new_data):
        """Actualiza instantáneamente en pantalla el número y la insignia +N si la API reporta un cambio"""
        with self.lock:
            if self.closed:
                return
            new_count = new_data.get('count', 0)
            self.current_count = new_count
            num_str = f"{new_count:,}" if new_count >= 1000 else str(new_count)
            try:
                if self.ctrl_num:
                    self.ctrl_num.setLabel(f"[COLOR FFFFFFFF][B]{num_str}[/B][/COLOR]")
            except Exception:
                pass

            # Si hay insignia de más espectadores, actualizar el número +diff
            num_avatars = len(self.avatar_files)
            if new_count > num_avatars:
                new_diff = new_count - num_avatars
                try:
                    if self.ctrl_badge:
                        new_badge_img = _generate_badge_asset(new_diff, badge_size=self.badge_size)
                        self.ctrl_badge.setImage(new_badge_img)
                except Exception:
                    pass

    def build_and_show(self, data, hold_seconds=6, is_osd_mode=False, meta=None):
        with self.lock:
            if self.closed:
                return

            count = data.get('count', 0)
            users = data.get('users', [])

            scale = self.scale
            h = max(26, int(round(self.HEIGHT * scale)))

            # Configuración independiente para el número y para el texto "viendo ahora"
            num_str = f"{count:,}" if count >= 1000 else str(count)
            if count < 10:
                base_num_w = 16
            elif count < 100:
                base_num_w = 30
            elif count < 1000:
                base_num_w = 44
            else:
                base_num_w = 66
            actual_num_w = int(round(base_num_w * scale))

            tail_str = "espectadores" if count == 0 else "viendo ahora"
            base_tail_w = 156 if count == 0 else 153
            tail_actual_w = int(round(base_tail_w * scale))

            # Bounding boxes amplias con margen generoso para que Kodi NUNCA trunque con puntos suspensivos (...)
            num_box_w = max(actual_num_w + int(round(25 * scale)), int(round(85 * scale)))
            tail_box_w = max(tail_actual_w + int(round(50 * scale)), int(round(220 * scale)))
            live_box_w = int(round(75 * scale))
            label_h = max(18, int(round(26 * scale)))

            badge_size = self.badge_size
            if count > 4:
                num_avatars = min(len(users), 3)
                diff = max(0, count - num_avatars)
                has_more = (diff > 0)
            else:
                num_avatars = min(len(users), count)
                diff = 0
                has_more = False

            live_pattern = lambda a: f"[COLOR {a:02X}FF3B30][B]LIVE[/B][/COLOR]"
            num_pattern = lambda a: f"[COLOR {a:02X}FFFFFF][B]{num_str}[/B][/COLOR]"
            tail_pattern = lambda a: f"[COLOR {int(a*0.7):02X}B0B0B8]{tail_str}[/COLOR]"

            # Posicionamiento horizontal responsivo milimétrico y compacto estilo Apple
            dot_offset = int(round(16 * scale))
            dot_size = max(8, int(round(12 * scale)))
            live_offset = dot_offset + dot_size + int(round(6 * scale))
            live_w_base = int(round(56 * scale))
            div_offset = live_offset + live_w_base + int(round(8 * scale))
            div_w = max(1, int(round(1 * scale)))
            div_h = max(10, int(round(18 * scale)))
            num_offset = div_offset + div_w + int(round(10 * scale))
            tail_offset = num_offset + actual_num_w + int(round(8 * scale))

            # Separación calibrada con los 9 px exactos (escalados) respecto al texto
            avatar_gap_to_text = max(4, int(round(9 * scale)))
            avatar_start_offset = tail_offset + tail_actual_w + avatar_gap_to_text

            avatar_size = max(20, int(round(36 * scale)))
            avatar_gap = max(3, int(round(6 * scale)))
            avatar_step = avatar_size + avatar_gap

            if num_avatars > 0:
                avatars_total_w = (num_avatars - 1) * avatar_step + avatar_size
                if has_more:
                    avatars_total_w += avatar_step - avatar_size + badge_size
                w = avatar_start_offset + avatars_total_w + max(6, int(round(10 * scale)))
            else:
                w = avatar_start_offset + max(6, int(round(10 * scale)))

            # Centrado horizontal exacto respecto al ancho real del lienzo gráfico
            x = (self.win_w - w) // 2
            y = self.y_pos
            slide_dist = max(18, int(round(35 * scale)))
            init_offset = -slide_dist if self.is_top else slide_dist

            # PASO CRÍTICO: Descargar y recortar TODOS los avatares e insignia ANTES de pintar en Kodi
            avatar_files = []
            for idx in range(num_avatars):
                u = users[idx] if idx < len(users) else {}
                av_local = _download_and_crop_avatar(u.get('avatar'), u.get('username') or f'usr_{idx}')
                avatar_files.append(av_local)

            badge_img = _generate_badge_asset(diff, badge_size=badge_size) if has_more else None

            # 1. Fondo de cápsula Apple (Glassmorphism con bordes redondeados)
            pill_img = _get_apple_pill_texture(w, h)
            ctrl_bg = xbmcgui.ControlImage(x, y + init_offset, w, h, pill_img)
            ctrl_bg.setColorDiffuse("00FFFFFF")
            self.window.addControl(ctrl_bg)
            self.controls.append(ctrl_bg)
            self.anims.append(AppleControlAnim(ctrl_bg, x, y, rgb='FFFFFF', max_alpha=0xFF, is_top=self.is_top, slide_dist=slide_dist))

            # 2. Punto rojo brillante estilo Live elevado
            dot_img = _get_media_asset('apple_live_dot.png')
            dot_y = y + int(round(18 * scale))
            ctrl_dot = xbmcgui.ControlImage(x + dot_offset, dot_y + init_offset, dot_size, dot_size, dot_img)
            ctrl_dot.setColorDiffuse("00FFFFFF")
            self.window.addControl(ctrl_dot)
            self.controls.append(ctrl_dot)
            self.anims.append(AppleControlAnim(ctrl_dot, x + dot_offset, dot_y, rgb='FFFFFF', max_alpha=0xFF, is_top=self.is_top, slide_dist=slide_dist))

            # 3. Etiqueta "LIVE" en el mismo píxel vertical que el número
            live_y = y + int(round(6 * scale))
            ctrl_live = xbmcgui.ControlLabel(x + live_offset, live_y + init_offset, live_box_w, label_h, live_pattern(0),
                                            font='font12')
            self.window.addControl(ctrl_live)
            self.controls.append(ctrl_live)
            self.anims.append(AppleControlAnim(ctrl_live, x + live_offset, live_y, is_label=True, is_top=self.is_top,
                                              label_pattern=live_pattern, slide_dist=slide_dist))

            # 4. Divisor vertical sutil de cristal
            white_tex = _get_media_asset('white.png')
            div_y = y + int(round(16 * scale))
            ctrl_div = xbmcgui.ControlImage(x + div_offset, div_y + init_offset, div_w, div_h, white_tex)
            ctrl_div.setColorDiffuse("00FFFFFF")
            self.window.addControl(ctrl_div)
            self.controls.append(ctrl_div)
            self.anims.append(AppleControlAnim(ctrl_div, x + div_offset, div_y, rgb='FFFFFF', max_alpha=0x35, is_top=self.is_top, slide_dist=slide_dist))

            # 5a. Número de espectadores
            num_y = y + int(round(6 * scale))
            ctrl_num = xbmcgui.ControlLabel(x + num_offset, num_y + init_offset, num_box_w, label_h, num_pattern(0),
                                            font='font12')
            self.window.addControl(ctrl_num)
            self.controls.append(ctrl_num)
            self.anims.append(AppleControlAnim(ctrl_num, x + num_offset, num_y, is_label=True, is_top=self.is_top,
                                               label_pattern=num_pattern, slide_dist=slide_dist))

            # 5b. Texto "viendo ahora"
            tail_y = y + int(round(4 * scale))
            ctrl_tail = xbmcgui.ControlLabel(x + tail_offset, tail_y + init_offset, tail_box_w, label_h, tail_pattern(0),
                                             font='font12')
            self.window.addControl(ctrl_tail)
            self.controls.append(ctrl_tail)
            self.anims.append(AppleControlAnim(ctrl_tail, x + tail_offset, tail_y, is_label=True, is_top=self.is_top,
                                               label_pattern=tail_pattern, slide_dist=slide_dist))

            # 6. Avatares circulares
            cur_av_x = x + avatar_start_offset
            av_y = y + int(round(7 * scale))

            for av_local in avatar_files:
                ctrl_av = xbmcgui.ControlImage(cur_av_x, av_y + init_offset, avatar_size, avatar_size, av_local)
                ctrl_av.setColorDiffuse("00FFFFFF")
                self.window.addControl(ctrl_av)
                self.controls.append(ctrl_av)
                self.anims.append(AppleControlAnim(ctrl_av, cur_av_x, av_y, rgb='FFFFFF', max_alpha=0xFF, is_top=self.is_top, slide_dist=slide_dist))
                cur_av_x += avatar_step

            # 7. Insignia "+N" circular
            if has_more and badge_img:
                badge_y = y + int(round(6 * scale))
                ctrl_badge = xbmcgui.ControlImage(cur_av_x, badge_y + init_offset, badge_size, badge_size, badge_img)
                ctrl_badge.setColorDiffuse("00FFFFFF")
                self.window.addControl(ctrl_badge)
                self.controls.append(ctrl_badge)
                self.anims.append(AppleControlAnim(ctrl_badge, cur_av_x, badge_y, rgb='FFFFFF', max_alpha=0xFF, is_top=self.is_top, slide_dist=slide_dist))
            else:
                ctrl_badge = None

            # Referencias para actualización dinámica en vivo
            self.ctrl_num = ctrl_num
            self.ctrl_badge = ctrl_badge
            self.avatar_files = avatar_files
            self.badge_size = badge_size
            self.current_count = count
            self.meta = meta

            # Mostrar si es lienzo WindowDialog en menús
            if self.dialog_canvas is not None:
                try:
                    self.dialog_canvas.show()
                except Exception:
                    pass

        # Disparar animación suave de elevación con desaceleración
        self._animate(forward=True)

        # Iniciar animación continua de palpitar (breathing/pulse) estilo Live en el punto rojo
        t_pulse = threading.Thread(target=self._pulse_loop, args=(ctrl_dot,), daemon=True)
        t_pulse.name = "TraktDotPulse"
        t_pulse.start()

        # Si el OSD está abierto, iniciar monitor en vivo para actualizar el número si cambia en la API
        if is_osd_mode and meta:
            t_poller = threading.Thread(target=self._live_watchers_poller, args=(meta,), daemon=True)
            t_poller.name = "TraktLivePoller"
            t_poller.start()

        # Si es modo sincronizado con el OSD, se mantiene abierto hasta que el OSD cierre
        if is_osd_mode:
            return

        # Si es modo temporizado al inicio de reproducción o preview
        p = xbmc.Player()
        mon = xbmc.Monitor()
        start_t = time.time()
        while time.time() - start_t < hold_seconds:
            if self.closed or mon.abortRequested():
                break
            if not self.is_preview and not p.isPlayingVideo():
                break
            time.sleep(0.05)

        self.close()

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            try:
                self._animate(forward=False)
            except Exception:
                pass
            for ctrl in self.controls:
                try:
                    self.window.removeControl(ctrl)
                except Exception:
                    pass
            self.controls = []
            self.anims = []
            if self.dialog_canvas is not None:
                try:
                    self.dialog_canvas.close()
                except Exception:
                    pass
                self.dialog_canvas = None


def show_trakt_overlay_timed(meta, duration=6, monitor_token=None):
    """Muestra el HUD compacto temporizado al iniciar la reproducción"""
    global _CURRENT_HUD, _TIMED_OVERLAY_ACTIVE

    try:
        addon = _get_addon()
        if addon and addon.getSetting('trakt_live_watchers_overlay') == 'false':
            return
        show_when_zero = (addon.getSetting('trakt_live_show_when_zero') == 'true') if addon else False
    except Exception:
        show_when_zero = False

    data = fetch_live_watchers(meta)
    if not data:
        return

    count = data.get('count', 0)
    if count == 0 and not show_when_zero:
        return

    # Esperar hasta 2.5s a que el vídeo esté en reproducción activa
    p = xbmc.Player()
    for _ in range(25):
        if not p.isPlayingVideo():
            return
        if p.getTime() > 0.05:
            break
        time.sleep(0.1)

    _TIMED_OVERLAY_ACTIVE = True
    try:
        with _HUD_LOCK:
            if _CURRENT_HUD is not None:
                _CURRENT_HUD.close()
                _CURRENT_HUD = None

            hud = TraktLiveHUD(is_preview=False)
            _CURRENT_HUD = hud

        hud.build_and_show(data, hold_seconds=duration, is_osd_mode=False, meta=meta)
    finally:
        _TIMED_OVERLAY_ACTIVE = False


def check_osd_watchers_trigger(meta):
    """
    Supervisa la apertura y cierre del OSD/SeekBar en tiempo real.
    Abre o cierra el HUD en perfecta sincronización con el OSD.
    """
    global _LAST_OSD_STATE, _CURRENT_HUD, _OSD_COOLDOWN, _TIMED_OVERLAY_ACTIVE

    # Si el overlay inicial temporizado está activo, no interferir con el OSD
    if _TIMED_OVERLAY_ACTIVE:
        return

    try:
        addon = _get_addon()
        if addon and addon.getSetting('trakt_live_watchers_overlay') == 'false':
            return
        if addon and addon.getSetting('trakt_live_show_on_osd') == 'false':
            return
        show_when_zero = (addon.getSetting('trakt_live_show_when_zero') == 'true') if addon else False
    except Exception:
        return

    is_osd_visible = bool(xbmc.getCondVisibility(
        "Window.IsVisible(videoosd) | Window.IsVisible(osdvideo) | "
        "Window.IsVisible(seekbar) | Window.IsVisible(10115) | "
        "Window.IsVisible(10120) | Player.Paused"
    ))

    now = time.time()

    # 1. El OSD se abrió
    if is_osd_visible and not _LAST_OSD_STATE:
        _LAST_OSD_STATE = True
        if now > _OSD_COOLDOWN:
            _OSD_COOLDOWN = now + 2.5

            def _show_async():
                global _CURRENT_HUD
                data = fetch_live_watchers(meta)
                if not data:
                    return
                if data.get('count', 0) == 0 and not show_when_zero:
                    return

                # Asegurarse de que el OSD SIGUE abierto antes de pintar
                still_open = bool(xbmc.getCondVisibility(
                    "Window.IsVisible(videoosd) | Window.IsVisible(osdvideo) | "
                    "Window.IsVisible(seekbar) | Window.IsVisible(10115) | "
                    "Window.IsVisible(10120) | Player.Paused"
                ))
                if not still_open:
                    return

                with _HUD_LOCK:
                    if _CURRENT_HUD is not None:
                        _CURRENT_HUD.close()
                        _CURRENT_HUD = None
                    hud = TraktLiveHUD(is_preview=False)
                    _CURRENT_HUD = hud

                hud.build_and_show(data, hold_seconds=0, is_osd_mode=True, meta=meta)

            t = threading.Thread(target=_show_async, daemon=True)
            t.name = "TraktOSDShow"
            t.start()

    # 2. El OSD se cerró (por timer de Kodi o porque el usuario pulsó Atrás / Back)
    elif not is_osd_visible and _LAST_OSD_STATE:
        _LAST_OSD_STATE = False
        with _HUD_LOCK:
            if _CURRENT_HUD is not None:
                _CURRENT_HUD.close()
                _CURRENT_HUD = None


def close_trakt_overlay():
    """Cierra de inmediato el HUD y limpia los controles"""
    global _CURRENT_HUD, _LAST_OSD_STATE, _TIMED_OVERLAY_ACTIVE
    _TIMED_OVERLAY_ACTIVE = False
    _LAST_OSD_STATE = False
    with _HUD_LOCK:
        if _CURRENT_HUD is not None:
            _CURRENT_HUD.close()
            _CURRENT_HUD = None


def preview_trakt_live():
    """Reproduce directamente el vídeo de prueba local y lanza el HUD de Trakt en vivo"""
    video_path = _get_media_asset('sample_test.mp4')
    if not os.path.exists(video_path):
        video_path = xbmcvfs.translatePath('special://home/addons/plugin.video.themoviedb.helper/resources/dummy.mp4')
    if not os.path.exists(video_path):
        video_path = xbmcvfs.translatePath('special://home/addons/plugin.video.balandro/resources/subtitle.mp4')

    meta = {
        'tmdb': 1263337,
        'title': 'Heart of the Beast',
        'year': 2026,
        'plot': 'Vídeo de prueba para previsualizar el overlay compacto de Trakt Live con OSD y navegación.',
        'poster': 'https://image.tmdb.org/t/p/w500/8b8R8l88Qje9dn9OE8PY05Nxl1X.jpg'
    }

    li = xbmcgui.ListItem('Prueba Trakt Live')
    li.setPath(video_path)
    try:
        vt = li.getVideoInfoTag()
        if vt:
            vt.setTitle('Heart of the Beast')
            vt.setMediaType('movie')
    except Exception:
        pass

    p = xbmc.Player()
    p.play(video_path, li)

    def _run_demo_monitor():
        for _ in range(30):
            if p.isPlayingVideo():
                break
            time.sleep(0.1)

        if not p.isPlayingVideo():
            return

        # Lanzar overlay inicial durante 8 segundos
        show_trakt_overlay_timed(meta, duration=8)

        # Monitorear OSD mientras siga reproduciéndose el clip
        mon = xbmc.Monitor()
        while p.isPlayingVideo() and not mon.abortRequested():
            check_osd_watchers_trigger(meta)
            time.sleep(0.2)

        close_trakt_overlay()

    t = threading.Thread(target=_run_demo_monitor, daemon=True)
    t.name = "TraktDemoMonitor"
    t.start()
