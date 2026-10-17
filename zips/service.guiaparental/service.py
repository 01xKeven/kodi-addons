import sys
import os
import time
import threading
import xbmc
import xbmcgui
import xbmcaddon

# Configuración y rutas del addon
ADDON_ID = 'service.guiaparental'

# La carpeta del script ya está en sys.path (Kodi la añade), pero nos aseguramos
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.append(_HERE)

# main.py crea el objeto del addon de forma tolerante a que Kodi lo esté
# actualizando en ese momento (ver create_addon en main.py).
import main

ADDON = main.ADDON
ADDON_PATH = ADDON.getAddonInfo('path')
ADDON_NAME = ADDON.getAddonInfo('name')


def log(msg, level=xbmc.LOGINFO):
    """Función para escribir en el log de Kodi."""
    xbmc.log(f"[{ADDON_NAME} Service] {msg}", level=level)


def get_bool(setting_id, default=True):
    """Lee un ajuste booleano del addon de forma segura."""
    return main.get_bool_setting(setting_id, default)


def get_int(setting_id, default=0):
    """Lee un ajuste entero del addon de forma segura."""
    try:
        return int(ADDON.getSetting(setting_id) or default)
    except Exception:
        return default


def log_active_settings():
    """Registra en el log los ajustes activos (útil para diagnosticar problemas)."""
    try:
        duration = get_int('overlay_duration', 8)
        api_key = (ADDON.getSetting('tmdb_api_key') or '').strip()
        log(
            "Ajustes activos: "
            f"mostrar HUD={get_bool('show_overlay', True)}, "
            f"posición={get_int('overlay_position', 0)}, "
            f"diseño={get_int('overlay_layout', 0)}, "
            f"animación={get_int('overlay_animation', 0)}, "
            f"retardo={get_int('overlay_delay', 1)} s, "
            f"duración visible={duration} s, "
            f"valoración={get_bool('show_rating', False)}, "
            f"clasificación={get_bool('show_certificate', False)}, "
            f"post-créditos={get_bool('show_post_credits', False)}, "
            f"global={get_bool('show_overall', False)}, "
            f"datos extra={get_bool('show_extra_meta', False)}, "
            f"ID IMDb={get_bool('show_imdb_id', False)}, "
            f"cuenta atrás={get_bool('show_countdown', False)}, "
            f"clave TMDb={'sí' if api_key else 'no'}"
        )
        if duration <= 0:
            log(
                "AVISO: «Duración visible» está en 0 s, así que el HUD se cerrará "
                "en cuanto aparezca. Ponlo entre 1 y 30 s en los ajustes del addon "
                "(pestaña General → Duración visible).",
                level=xbmc.LOGWARNING,
            )
    except Exception as e:
        log(f"No se pudieron leer los ajustes activos: {e}", level=xbmc.LOGWARNING)


# ---------------------------------------------------------------------------
# Paleta de colores (RRGGBB; el canal alfa se añade al renderizar)
# ---------------------------------------------------------------------------
C_WHITE    = 'FFFFFF'
C_SOFT     = 'D8D8D8'
C_MUTED    = '9E9E9E'
C_MILD     = '2ECC71'
C_MODERATE = 'F39C12'
C_SEVERE   = 'E74C3C'
C_RATING   = 'FFD24A'
C_RATED    = '4AC1FF'
C_STINGER  = '00E5FF'

# Texturas del addon
_TEX_DIR   = os.path.join(ADDON_PATH, 'resources', 'skins', 'Default')
TEX_WHITE  = os.path.join(_TEX_DIR, 'media', 'white.png')
TEX_BAR    = os.path.join(_TEX_DIR, '720p', 'rounded_bar.png')

# Pasos de alpha para las animaciones (00 → FF)
_FADE_IN  = [0x00, 0x20, 0x45, 0x70, 0xA0, 0xCF, 0xFF]
_FADE_OUT = [0xFF, 0xCF, 0xA0, 0x70, 0x45, 0x20, 0x00]
_STEP_S   = 0.030   # segundos por paso (~30 ms)
_CLASSIC_STEP_S = 0.035   # el ritmo exacto del addon original
_CLASSIC_MAX_S  = 2.0     # tope de duración de la cascada clásica completa

# Diseños disponibles
LAYOUT_SIDEBAR = 0   # barra vertical + filas con punto
LAYOUT_MINIMAL = 1   # solo texto, sin adornos
LAYOUT_COUNT   = 2

# Animaciones disponibles
ANIM_CLASSIC = 0   # la del addon original: la barra y luego fila a fila
ANIM_FADE    = 1   # fundido (todos los controles a la vez)
ANIM_STAGGER = 2   # escalonada (control a control)
ANIM_SLIDE   = 3   # deslizar desde la izquierda
ANIM_SLIDE_V = 4   # deslizar en vertical
ANIM_NONE    = 5   # sin animación
ANIM_COUNT   = 6

# Nombres (para el log)
_LAYOUT_KEYS = {
    LAYOUT_SIDEBAR: 'layout_sidebar',
    LAYOUT_MINIMAL: 'layout_minimal',
}
_ANIM_KEYS = {
    ANIM_CLASSIC: 'anim_classic',
    ANIM_FADE:    'anim_fade',
    ANIM_STAGGER: 'anim_stagger',
    ANIM_SLIDE:   'anim_slide',
    ANIM_SLIDE_V: 'anim_slide_v',
    ANIM_NONE:    'anim_none',
}


class Row(object):
    """Una fila lógica del overlay."""

    def __init__(self, kind, name, value, color):
        self.kind  = kind       # 'meta' | 'cat' | 'stinger'
        self.name  = name
        self.value = value
        self.color = color      # RRGGBB


class ControlAnim(object):
    """Envuelve un control y aplica progreso (0..1) a su alpha/posición."""

    def __init__(self, ctrl, anim, base_x, base_y, max_alpha=0xFF, rgb=None, text_fn=None):
        self.ctrl      = ctrl
        self.anim      = anim
        self.base_x    = base_x
        self.base_y    = base_y
        self.max_alpha = max_alpha
        self.rgb       = rgb
        self.text_fn   = text_fn

    def apply(self, p):
        p = 0.0 if p < 0 else (1.0 if p > 1 else p)
        alpha = int(round(p * self.max_alpha))
        try:
            if self.rgb is not None:
                self.ctrl.setColorDiffuse(f"{alpha:02X}{self.rgb}")
            if self.text_fn is not None:
                self.ctrl.setLabel(self.text_fn(alpha))

            if self.anim == ANIM_SLIDE:
                self.ctrl.setPosition(self.base_x - int((1 - p) * 60), self.base_y)
            elif self.anim == ANIM_SLIDE_V:
                dy = -36 if self.base_y < 540 else 36
                self.ctrl.setPosition(self.base_x, self.base_y + int((1 - p) * dy))
            else:
                self.ctrl.setPosition(self.base_x, self.base_y)
        except Exception:
            pass


class ParentalGuideOverlay(object):
    """HUD no intrusivo inyectado en la ventana de reproducción, con animaciones.

    Seguro para multihilo: todas las operaciones sobre controles están protegidas
    por self.lock y el cierre es idempotente, evitando el crash al detener la
    reproducción mientras el overlay está en pantalla.
    """

    def __init__(self, window=None, layout=None, animation=None):
        self.window        = window if window is not None else xbmcgui.Window(12005)
        self.layout        = layout
        self.animation     = animation
        self.anim          = animation if animation is not None else 0
        self.controls      = []
        self.anims         = []    # todos los controles animables (lista plana)
        self.anim_groups   = []    # los mismos, agrupados por fila (para la clásica)
        self.block_rect    = None
        self.progress_ctrl = None
        self.progress_w    = 0
        self.lock          = threading.RLock()
        self.closed        = False

    # ------------------------------------------------------------------
    # Datos
    # ------------------------------------------------------------------
    def _sev_color(self, sev_class, sev_text):
        try:
            if sev_text == main.get_string('no_data'):
                return C_MUTED
        except Exception:
            pass
        sc = (sev_class or '').lower()
        if 'severe' in sc:
            return C_SEVERE
        if 'moderate' in sc:
            return C_MODERATE
        if 'mild' in sc:
            return C_MILD
        return C_WHITE

    @staticmethod
    def _sev_rank(sev_class):
        sc = (sev_class or '').lower()
        if 'severe' in sc:
            return 3
        if 'moderate' in sc:
            return 2
        if 'mild' in sc:
            return 1
        return 0

    def _overall(self, ratings):
        """Nivel general = peor severidad encontrada en la guía."""
        worst = 0
        for item in (ratings or []):
            worst = max(worst, self._sev_rank(item.get('severity_class')))
        return {
            3: (main.get_string('severe'), C_SEVERE),
            2: (main.get_string('moderate'), C_MODERATE),
            1: (main.get_string('mild'), C_MILD),
            0: (main.get_string('none'), C_WHITE),
        }[worst]

    def _build_groups(self, ratings, stinger_info, meta, imdb_id=None):
        """Devuelve una lista de grupos; cada grupo es una lista de Row."""
        meta = meta or {}
        groups = []

        # --- Grupo 1: valoración, clasificación, nivel general ---
        meta_rows = []
        if get_bool('show_rating', True) and meta.get('rating'):
            val = str(meta['rating'])
            if meta.get('votes'):
                val += f" / {main.format_votes(meta['votes'])} {main.get_string('votes')}"
            meta_rows.append(Row('meta', main.get_string('rating'), val, C_RATING))
        if get_bool('show_certificate', True) and meta.get('certificate'):
            meta_rows.append(Row('meta', main.get_string('rated'), str(meta['certificate']), C_RATED))
        if get_bool('show_overall', True) and ratings:
            text, color = self._overall(ratings)
            meta_rows.append(Row('meta', main.get_string('overall'), text, color))
        if meta_rows:
            groups.append(meta_rows)

        # --- Grupo 2: ficha técnica (año, duración, géneros) e ID de IMDb ---
        extra_rows = []
        if get_bool('show_extra_meta', True):
            if meta.get('year'):
                extra_rows.append(Row('meta', main.get_string('year'), str(meta['year']), C_SOFT))
            runtime = main.format_runtime(meta.get('runtime'))
            if runtime:
                extra_rows.append(Row('meta', main.get_string('runtime'), runtime, C_SOFT))
            if meta.get('genres'):
                extra_rows.append(Row('meta', main.get_string('genres'),
                                      ", ".join(main.translate_genres(meta['genres'])[:3]), C_SOFT))
        if get_bool('show_imdb_id', False) and imdb_id:
            extra_rows.append(Row('meta', main.get_string('imdb_id'), imdb_id, C_MUTED))
        if extra_rows:
            groups.append(extra_rows)

        # --- Grupo 3: categorías de la guía parental ---
        cat_rows = []
        for item in (ratings or []):
            name = item.get('category', '')
            sev  = item.get('severity_text', main.get_string('no_data'))
            cat_rows.append(Row('cat', name, sev,
                                self._sev_color(item.get('severity_class', ''), sev)))
        if cat_rows:
            groups.append(cat_rows)

        # --- Grupo 4: escenas post-créditos ---
        if get_bool('show_post_credits', True) and stinger_info and stinger_info.get('stinger_text'):
            color = str(stinger_info.get('stinger_color') or ('FF' + C_STINGER))[-6:]
            groups.append([Row('stinger', main.get_string('post_credits'),
                               str(stinger_info['stinger_text']), color)])
        return groups

    @staticmethod
    def _flatten(groups, with_separators=True):
        rows = []
        for gi, group in enumerate(groups):
            if gi > 0 and with_separators:
                rows.append('sep')
            rows.extend(group)
        return rows

    # ------------------------------------------------------------------
    # Primitivas de dibujo (registran además su animación)
    # ------------------------------------------------------------------
    def _begin_group(self):
        """Inicia un grupo de animación (una fila). Los controles que se añadan
        a continuación se animarán juntos en la animación clásica."""
        self.anim_groups.append([])

    def _group(self):
        if not self.anim_groups:
            self._begin_group()
        return self.anim_groups[-1]

    def _add_image(self, x, y, w, h, texture, rgb, max_alpha=0xFF):
        ctrl = xbmcgui.ControlImage(-4000, y, w, h, texture)
        self.window.addControl(ctrl)
        self.controls.append(ctrl)
        anim = ControlAnim(ctrl, self.anim, x, y, max_alpha=max_alpha, rgb=rgb)
        self.anims.append(anim)
        self._group().append(anim)
        anim.apply(0.0)
        return ctrl

    def _add_label(self, x, y, w, h, text_fn, font='font12', alignment=0):
        ctrl = xbmcgui.ControlLabel(x, y, w, h, text_fn(0), font=font,
                                    textColor='0x00000000', alignment=alignment)
        self.window.addControl(ctrl)
        self.controls.append(ctrl)
        anim = ControlAnim(ctrl, self.anim, x, y, text_fn=text_fn)
        self.anims.append(anim)
        self._group().append(anim)
        anim.apply(0.0)
        return ctrl

    # ------------------------------------------------------------------
    # Animaciones
    # ------------------------------------------------------------------
    def _animate(self, forward, should_abort=None):
        """Anima los controles.

        La entrada respeta el estilo elegido. La salida es siempre un fundido
        rápido en paralelo: evita que el overlay se quede en pantalla varios
        segundos mientras el reproductor ya se está cerrando.
        """
        groups = [g for g in self.anim_groups if g]
        flat = [a for g in groups for a in g]
        if not flat:
            return

        if self.anim == ANIM_NONE:
            for a in flat:
                a.apply(1.0 if forward else 0.0)
            return

        # Fundido: todos los controles a la vez.
        # Las salidas de estilos no clásicos también son un fundido (rápido).
        if self.anim == ANIM_FADE or ((not forward) and self.anim != ANIM_CLASSIC):
            seq = _FADE_IN if forward else _FADE_OUT
            for value in seq:
                if should_abort and should_abort():
                    return
                for a in flat:
                    a.apply(value / 255.0)
                time.sleep(_STEP_S)
            return

        # Clásica: como el addon original.
        #  - entrada: la barra primero y luego cada fila completa
        #  - salida: la última fila primero (orden inverso) y la barra al final
        if self.anim == ANIM_CLASSIC:
            # Ritmo del addon original (0,035 s por paso), con un tope de duración
            # para que la cascada no se alargue cuando hay muchas filas activas.
            step = min(_CLASSIC_STEP_S,
                       _CLASSIC_MAX_S / float(max(len(groups) * len(_FADE_IN), 1)))
            seq = _FADE_IN if forward else _FADE_OUT
            ordered = groups if forward else list(reversed(groups))
            for group in ordered:
                for value in seq:
                    if should_abort and should_abort():
                        return
                    for a in group:
                        a.apply(value / 255.0)
                    time.sleep(step)
            return

        # Escalonada y deslizamientos: control a control
        for a in flat:
            for value in _FADE_IN:
                if should_abort and should_abort():
                    return
                a.apply(value / 255.0)
                time.sleep(_STEP_S)

    def _set_progress(self, fraction):
        ctrl = self.progress_ctrl
        if not ctrl or self.closed:
            return
        try:
            ctrl.setWidth(max(1, int(self.progress_w * fraction)))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Diseño 0 · Barra lateral
    # ------------------------------------------------------------------
    def _render_sidebar(self, groups, bottom):
        line_h, sep_h = 34, 11
        rows = self._flatten(groups)
        n_lab = sum(1 for r in rows if r != 'sep')
        n_sep = sum(1 for r in rows if r == 'sep')
        block_h = n_lab * line_h + n_sep * sep_h + 12

        x_bar, x_lbl, x_sep, w_lbl = 50, 72, 72, 430
        y0 = (1080 - 60 - block_h) if bottom else 56
        self.block_rect = (x_lbl, y0, w_lbl, block_h)

        # La barra lateral se anima como primer grupo (igual que el original)
        self._begin_group()
        self._add_image(x_bar, y0 + 4, 4, max(block_h - 8, 20), TEX_BAR, C_WHITE)

        cur = y0
        pending = False
        for row in rows:
            if pending:
                # El separador anterior comparte grupo con esta fila
                pending = False
            else:
                self._begin_group()
            if row == 'sep':
                self._add_image(x_sep, cur + sep_h // 2, 250, 1, TEX_WHITE, C_WHITE, max_alpha=0x45)
                cur += sep_h
                pending = True
                continue

            def txt(a, rr=row):
                return (f"[B][COLOR={a:02X}FFFFFF]{rr.name}[/COLOR][/B]"
                        f"[COLOR={a:02X}888888]   ·   [/COLOR]"
                        f"[COLOR={a:02X}{rr.color}]{rr.value}[/COLOR]")

            self._add_label(x_lbl, cur, w_lbl, 30, txt)
            cur += line_h

    # ------------------------------------------------------------------
    # Diseño 1 · Minimalista
    # ------------------------------------------------------------------
    def _render_minimal(self, groups, bottom):
        line_h, gap_h = 30, 7
        rows = self._flatten(groups)
        n_lab = sum(1 for r in rows if r != 'sep')
        n_sep = sum(1 for r in rows if r == 'sep')
        block_h = n_lab * line_h + n_sep * gap_h + 4
        y0 = (1080 - 54 - block_h) if bottom else 58
        self.block_rect = (58, y0, 520, block_h)

        cur = y0
        for row in rows:
            self._begin_group()
            if row == 'sep':
                cur += gap_h
                continue

            def txt(a, rr=row):
                return (f"[COLOR={a:02X}{C_MUTED}]{rr.name}[/COLOR]"
                        f"    [COLOR={a:02X}{rr.color}]{rr.value}[/COLOR]")

            self._add_label(58, cur, 860, 28, txt)
            cur += line_h

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------
    def show_guide(self, ratings, stinger_info=None, meta=None, hold_seconds=8,
                   should_abort=None, imdb_id=None):
        """Construye, anima y mantiene el overlay en pantalla hold_seconds segundos."""
        with self.lock:
            if self.closed:
                return
            try:
                groups = self._build_groups(ratings, stinger_info, meta, imdb_id)
            except Exception as e:
                log(f"Error construyendo el HUD: {e}", level=xbmc.LOGWARNING)
                return
            if not groups:
                return

            layout = self.layout
            if layout is None:
                layout = max(0, min(LAYOUT_COUNT - 1, get_int('overlay_layout', 0)))
            anim = self.animation
            if anim is None:
                anim = max(0, min(ANIM_COUNT - 1, get_int('overlay_animation', 0)))
            self.anim = anim
            bottom = get_int('overlay_position', 0) == 1

            try:
                layout_name = main.get_string(_LAYOUT_KEYS.get(layout, 'layout_sidebar'))
            except Exception:
                layout_name = str(layout)
            try:
                anim_name = main.get_string(_ANIM_KEYS.get(anim, 'anim_fade'))
            except Exception:
                anim_name = str(anim)
            log(f"Construyendo HUD (diseño «{layout_name}», animación «{anim_name}»)...")
            self.controls      = []
            self.anims         = []
            self.anim_groups   = []
            self.block_rect    = None
            self.progress_ctrl = None
            self.progress_w    = 0

            try:
                if layout == LAYOUT_MINIMAL:
                    self._render_minimal(groups, bottom)
                else:
                    self._render_sidebar(groups, bottom)
            except Exception as e:
                log(f"Error dibujando el HUD: {e}", level=xbmc.LOGWARNING)

            # Barra de duración (cuenta atrás) al pie del bloque
            if get_bool('show_countdown', True) and self.block_rect:
                self._begin_group()
                bx, by, bw, bh = self.block_rect
                self.progress_w = bw
                self.progress_ctrl = self._add_image(bx, by + bh + 7, bw, 3,
                                                     TEX_WHITE, C_WHITE, max_alpha=0x9A)
                self.progress_ctrl.setWidth(bw)

            # Animación de entrada
            t_anim = time.time()
            self._animate(True, should_abort)

            if self.closed or (should_abort and should_abort()):
                return
            log(f"HUD visible tras la animación (%.2f s)" % (time.time() - t_anim))

            # Mantener en pantalla con cuenta atrás (basada en tiempo real)
            total = float(max(0, hold_seconds))
            start = time.time()
            if self.progress_ctrl:
                self._set_progress(1.0)
            while total > 0:
                if self.closed or (should_abort and should_abort()):
                    return
                elapsed = time.time() - start
                if elapsed >= total:
                    break
                if self.progress_ctrl:
                    self._set_progress(1.0 - (elapsed / total))
                time.sleep(0.1)
            if self.progress_ctrl:
                self._set_progress(0.0)

    def close(self, fast=False):
        """Cierra el overlay. Idempotente y seguro entre hilos.

        fast=True evita la animación de salida (cierre inmediato): se usa al
        detener la reproducción para minimizar la carrera con la ventana.
        """
        with self.lock:
            if self.closed:
                return
            self.closed = True
            log("Cerrando HUD...")

            if not fast:
                try:
                    self._animate(False)
                except Exception as e:
                    log(f"Error en animación de salida: {e}", level=xbmc.LOGWARNING)

            for ctrl in self.controls:
                try:
                    self.window.removeControl(ctrl)
                except Exception:
                    pass

            self.controls      = []
            self.anims         = []
            self.anim_groups   = []
            self.progress_ctrl = None


class ParentalPlayer(xbmc.Player):
    """Reproductor personalizado para detectar e interactuar con la reproducción de videos."""

    def __init__(self):
        super().__init__()
        self.active_overlay  = None
        self.playback_thread = None
        self.stop_event      = threading.Event()
        self.state_lock      = threading.Lock()   # protege active_overlay / thread_active
        self.thread_active   = False

    # ------------------------------------------------------------------
    # Eventos del reproductor
    # ------------------------------------------------------------------
    def onPlayBackStarted(self):
        log("Evento de reproducción iniciado (onPlayBackStarted) recibido.")
        self._trigger_start("onPlayBackStarted")

    def onAVStarted(self):
        log("Evento de audio/video iniciado (onAVStarted) recibido.")
        self._trigger_start("onAVStarted")

    def onPlayBackStopped(self):
        log("Reproducción detenida por el usuario.")
        self._stop_processing()

    def onPlayBackEnded(self):
        log("Reproducción finalizada.")
        self._stop_processing()

    def _stop_processing(self):
        # Señalizar parada y cerrar el overlay de forma inmediata (sin animación)
        # para evitar manipular la ventana de vídeo mientras ya se está cerrando.
        self.stop_event.set()
        with self.state_lock:
            self.thread_active = False
        self.close_overlay(fast=True)
        t = self.playback_thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=3)

    def _trigger_start(self, event_name):
        with self.state_lock:
            if self.thread_active:
                log(f"El proceso de overlay ya está activo, ignorando evento: {event_name}")
                return
            self.thread_active = True

        log(f"Iniciando hilo de procesamiento gatillado por: {event_name}")
        self.stop_event.set()
        t = self.playback_thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=3)

        self.stop_event.clear()
        self.playback_thread = threading.Thread(target=self._process_playback)
        self.playback_thread.daemon = True
        self.playback_thread.start()

    # ------------------------------------------------------------------
    def close_overlay(self, fast=False):
        """Cierra el overlay de forma segura si está en pantalla (idempotente)."""
        with self.state_lock:
            ov = self.active_overlay
        if ov:
            try:
                ov.close(fast=fast)
                log("Overlay de control parental cerrado.")
            except Exception as e:
                log(f"Error cerrando el overlay: {e}", level=xbmc.LOGWARNING)
            with self.state_lock:
                if self.active_overlay is ov:
                    self.active_overlay = None

    def _get_seconds(self, setting_id, default):
        """Lee un ajuste de segundos limitándolo al rango 0-30."""
        try:
            value = int(ADDON.getSetting(setting_id) or default)
        except Exception:
            value = default
        return min(max(value, 0), 30)

    # ------------------------------------------------------------------
    def _process_playback(self):
        try:
            # 0. Interruptor general del overlay
            if not get_bool('show_overlay', True):
                log("El overlay está desactivado en los ajustes; no se muestra.")
                return

            # 1. Esperar los segundos configurados antes de iniciar el procesamiento
            wait_steps = self._get_seconds('overlay_delay', 1) * 10
            for _ in range(wait_steps):
                if self.stop_event.is_set() or not self.isPlayingVideo():
                    return
                time.sleep(0.1)

            if self.stop_event.is_set():
                return

            # 2. Extraer el ID de IMDb del video actual
            imdb_id = self._get_imdb_id()
            if not imdb_id:
                log("No se pudo extraer un ID de IMDb válido para el contenido en reproducción.")
                return

            log(f"ID de IMDb extraído exitosamente: {imdb_id}. Buscando guía de contenido...")

            # Detectar el tipo de medio y obtener temporada/episodio
            media_type  = 'movie'
            season_num  = None
            episode_num = None
            try:
                info_tag = self.getVideoInfoTag()
                m_type   = info_tag.getMediaType()

                if m_type == 'episode':
                    media_type  = 'show'
                    season_num  = info_tag.getSeason()
                    episode_num = info_tag.getEpisode()
                elif m_type == 'movie':
                    media_type = 'movie'
                else:
                    content = xbmc.getInfoLabel('VideoPlayer.Content')
                    tvshow  = xbmc.getInfoLabel('VideoPlayer.TVShowTitle')
                    if 'episode' in content or 'show' in content or tvshow:
                        media_type  = 'show'
                        season_num  = int(xbmc.getInfoLabel('VideoPlayer.Season')  or 0) or None
                        episode_num = int(xbmc.getInfoLabel('VideoPlayer.Episode') or 0) or None
            except Exception as ex:
                log(f"Error detectando tipo de medio: {ex}", level=xbmc.LOGWARNING)
                content = xbmc.getInfoLabel('VideoPlayer.Content')
                if 'episode' in content or 'show' in content:
                    media_type = 'show'

            # Si es episodio, resolver el IMDb ID específico del episodio
            show_imdb_id = imdb_id
            if media_type == 'show' and season_num and episode_num:
                ep_id = self._get_episode_imdb_id(imdb_id, season_num, episode_num)
                if ep_id:
                    log(f"ID específico del episodio S{season_num:02d}E{episode_num:02d}: {ep_id}")
                    imdb_id = ep_id
                else:
                    log(f"No se encontró ID de episodio, usando ID de serie: {imdb_id}", level=xbmc.LOGWARNING)

            # 3. Descargar la guía parental de IMDb
            try:
                res = main.get_parental_guide(imdb_id, progress_dialog=None, silent=True, media_type=media_type)
                if res and len(res) == 4:
                    movie_title, guide, stinger_info, meta = res
                elif res and len(res) == 3:
                    movie_title, guide, stinger_info = res
                    meta = {}
                elif res and len(res) == 2:
                    movie_title, guide = res
                    stinger_info, meta = None, {}
                else:
                    movie_title, guide, stinger_info, meta = None, None, None, {}

                # Si es un episodio y no tiene guía propia en IMDb, heredar la guía general de la serie
                if media_type == 'show' and main.is_empty_guide(guide) and show_imdb_id and show_imdb_id != imdb_id:
                    log(f"El episodio no tiene guía propia en IMDb, obteniendo guía de la serie: {show_imdb_id}")
                    s_res = main.get_parental_guide(show_imdb_id, progress_dialog=None, silent=True, media_type='show')
                    if s_res and len(s_res) >= 2 and s_res[1]:
                        guide = s_res[1]
                        if not movie_title:
                            movie_title = s_res[0]
                        if len(s_res) == 4 and s_res[3]:
                            meta = s_res[3]
            except Exception as e:
                log(f"Error al descargar la guía parental: {e}", level=xbmc.LOGERROR)
                return

            if movie_title is None:
                log(f"No se pudo resolver información para el ID: {imdb_id}.")
                return

            if self.stop_event.is_set() or not self.isPlayingVideo():
                return

            # 4. Crear y mostrar la ventana superpuesta en la pantalla
            log(f"Mostrando overlay inyectado para: {movie_title}")
            overlay = ParentalGuideOverlay()
            with self.state_lock:
                self.active_overlay = overlay
            try:
                overlay.show_guide(
                    guide, stinger_info=stinger_info, meta=meta,
                    # La duración mínima es 1 s: así el HUD nunca se cierra de golpe.
                    hold_seconds=max(1, self._get_seconds('overlay_duration', 8)),
                    should_abort=lambda: self.stop_event.is_set() or not self.isPlayingVideo(),
                    imdb_id=imdb_id)
            finally:
                # 5. Cerrar el overlay (rápido si la reproducción ya paró)
                self.close_overlay(fast=self.stop_event.is_set() or not self.isPlayingVideo())

        except Exception as e:
            log(f"Excepción en el procesamiento del overlay: {e}", level=xbmc.LOGERROR)
        finally:
            with self.state_lock:
                self.thread_active = False

    # ------------------------------------------------------------------
    def _get_imdb_id(self):
        """Intenta obtener el ID de IMDb (de la serie o película) de múltiples fuentes en Kodi."""
        imdb_id = ""
        try:
            info_tag = self.getVideoInfoTag()
            if info_tag:
                try:
                    imdb_id = info_tag.getUniqueID('imdb')
                except Exception:
                    pass
                try:
                    if not imdb_id:
                        imdb_id = info_tag.getIMDBNumber()
                except Exception:
                    pass

            if not imdb_id:
                imdb_id = xbmc.getInfoLabel('ListItem.IMDBNumber')
            if not imdb_id:
                imdb_id = xbmc.getInfoLabel('Player.IMDBNumber')
            if not imdb_id:
                imdb_id = xbmc.getInfoLabel('VideoPlayer.IMDBNumber')

            if not imdb_id:
                playing_file = self.getPlayingFile()
                import re
                match = re.search(r'(tt\d{7,8})', playing_file)
                if match:
                    imdb_id = match.group(1)
        except Exception as e:
            log(f"Error extrayendo el ID de IMDb: {e}", level=xbmc.LOGWARNING)

        if imdb_id and isinstance(imdb_id, str) and imdb_id.startswith('tt'):
            return imdb_id
        return None

    def _get_episode_imdb_id(self, series_id, season, episode):
        """Resuelve el IMDb ID específico de un episodio usando la API de GraphQL de IMDb vía main.py."""
        return main.get_episode_imdb_id(series_id, season, episode)


def main_loop():
    monitor = xbmc.Monitor()
    player = ParentalPlayer()

    log("Servicio de Control Parental IMDb iniciado.")
    log_active_settings()

    while not monitor.abortRequested():
        if monitor.waitForAbort(1):
            break

    # Cierre limpio al apagar Kodi
    player.stop_event.set()
    player.close_overlay(fast=True)
    log("Servicio de Control Parental IMDb detenido.")


if __name__ == '__main__':
    main_loop()
