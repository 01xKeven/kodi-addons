import sys
import os
import time
import threading
import xbmc
import xbmcgui
import xbmcaddon

# Configuración y rutas del addon
ADDON = xbmcaddon.Addon()
ADDON_PATH = ADDON.getAddonInfo('path')
ADDON_NAME = ADDON.getAddonInfo('name')

# Añadir el directorio raíz al path para poder importar main.py
sys.path.append(ADDON_PATH)
import main

def log(msg, level=xbmc.LOGINFO):
    """Función para escribir en el log de Kodi."""
    xbmc.log(f"[{ADDON_NAME} Service] {msg}", level=level)

class LabelData:
    """Almacena los datos de cada etiqueta para poder regenerar el texto con alpha variable."""
    def __init__(self, cat_name, sev_text, color_hex):
        self.cat_name  = cat_name
        self.sev_text  = sev_text
        self.color_hex = color_hex  # formato AARRGGBB p.ej. "FF2ECC71"

    def make_text(self, alpha):
        """Genera el texto de la etiqueta con el canal alpha especificado (0x00-0xFF)."""
        rgb = self.color_hex[2:]           # "2ECC71"  (sin el byte de alpha)
        a   = f"{alpha:02X}"               # "FF"
        name = f"[B][COLOR={a}FFFFFF]{self.cat_name}[/COLOR][/B]"
        dot  = f"[COLOR={a}AAAAAA]  ·  [/COLOR]"
        sev  = f"[COLOR={a}{rgb}]{self.sev_text}[/COLOR]"
        return f"{name}{dot}{sev}"


# Pasos de alpha para fade-in y fade-out (cada paso ~35ms → ~245ms por control)
_FADE_IN  = [0x00, 0x20, 0x45, 0x70, 0xA0, 0xCF, 0xFF]
_FADE_OUT = [0xFF, 0xCF, 0xA0, 0x70, 0x45, 0x20, 0x00]
_STEP_S   = 0.035   # segundos por paso de fade


class ParentalGuideOverlay(object):
    """HUD no intrusivo inyectado en la ventana de reproducción, con fade suave."""

    def __init__(self):
        self.window = xbmcgui.Window(12005)   # WINDOW_FULLSCREEN_VIDEO
        self.bar    = None                     # ControlImage
        self.labels = []                       # lista de (ControlLabel, LabelData)

    # ------------------------------------------------------------------
    def show_guide(self, ratings):
        log("Construyendo HUD con fade suave...")

        severity_colors = {
            "none":     "FF7F8C8D",
            "mild":     "FF2ECC71",
            "moderate": "FFF39C12",
            "severe":   "FFE74C3C",
        }

        def normalize_category(cat_name):
            c = cat_name.lower().strip()
            if "desnud" in c or "nudity" in c or "sex" in c:      return "Desnudos"
            if "violenc" in c or "sangr" in c:                     return "Violencia"
            if "obscen" in c or "groser" in c or "profan" in c or "languag" in c:
                                                                    return "Groserias"
            if "alcohol" in c or "drog" in c or "drink" in c:     return "Alcohol/Drogas"
            if "intens" in c or "terror" in c or "fright" in c:   return "Escenas intensas"
            return cat_name

        normalized = {}
        for item in ratings:
            raw = item.get('raw_category')
            if raw:
                normalized[raw] = item
            else:
                normalized[normalize_category(item.get('category', ''))] = item

        categories_keys = ["SEXUAL_CONTENT", "VIOLENCE", "PROFANITY", "ALCOHOL_DRUGS", "FRIGHTENING_INTENSE_SCENES"]
        texture = os.path.join(ADDON_PATH, 'resources', 'skins', 'Default', '720p', 'rounded_bar.png')

        # ── Barra lateral (ControlImage) ─────────────────────────────
        try:
            self.bar = xbmcgui.ControlImage(-1000, 55, 4, 175, texture)
            self.window.addControl(self.bar)
            self.bar.setColorDiffuse("00FFFFFF")   # transparente antes de moverse
            self.bar.setPosition(50, 55)           # mover a posición correcta (ya invisible)
        except Exception as e:
            log(f"Error barra: {e}", level=xbmc.LOGWARNING)

        # ── Etiquetas (ControlLabel) ──────────────────────────────────
        y = 55
        for cat_key in categories_keys:
            data = normalized.get(cat_key)
            if not data:
                spanish_fallback = {
                    "SEXUAL_CONTENT": "Desnudos",
                    "VIOLENCE": "Violencia",
                    "PROFANITY": "Groserias",
                    "ALCOHOL_DRUGS": "Alcohol/Drogas",
                    "FRIGHTENING_INTENSE_SCENES": "Escenas intensas"
                }.get(cat_key)
                data = normalized.get(spanish_fallback)

            cat_label = main.get_string(cat_key)
            sev_t     = main.get_string("none")
            col_h     = severity_colors["none"]

            if data:
                sev_t = data.get('severity_text', main.get_string('none'))
                sc    = data.get('severity_class', '')
                if   'none'     in sc.lower(): col_h = severity_colors["none"]
                elif 'mild'     in sc.lower(): col_h = severity_colors["mild"]
                elif 'moderate' in sc.lower(): col_h = severity_colors["moderate"]
                elif 'severe'   in sc.lower(): col_h = severity_colors["severe"]
                else:
                    lt = sev_t.lower()
                    if   lt in ['leve', 'mild']: col_h = severity_colors["mild"]
                    elif lt in ['moderada', 'moderate']: col_h = severity_colors["moderate"]
                    elif lt in ['severa', 'severe']: col_h = severity_colors["severe"]
                    elif lt in ['ninguno', 'none']: col_h = severity_colors["none"]

            ld = LabelData(cat_label, sev_t, col_h)
            try:
                lbl = xbmcgui.ControlLabel(70, y, 390, 30, ld.make_text(0x00),
                                           font='font12', textColor='0x00000000')
                self.window.addControl(lbl)
                self.labels.append((lbl, ld))
            except Exception as e:
                log(f"Error etiqueta {cat_label}: {e}", level=xbmc.LOGWARNING)
            y += 35

        # ── Animación de entrada escalonada ───────────────────────────
        # Fade-in de la barra primero
        if self.bar:
            for alpha in _FADE_IN:
                self.bar.setColorDiffuse(f"{alpha:02X}FFFFFF")
                time.sleep(_STEP_S)

        # Fade-in de cada etiqueta con retardo entre ellas
        for lbl, ld in self.labels:
            for alpha in _FADE_IN:
                try:
                    lbl.setLabel(ld.make_text(alpha))
                except Exception:
                    pass
                time.sleep(_STEP_S)

    # ------------------------------------------------------------------
    def close(self):
        log("Cerrando HUD con fade-out...")

        # Fade-out de etiquetas en orden inverso
        for lbl, ld in reversed(self.labels):
            for alpha in _FADE_OUT:
                try:
                    lbl.setLabel(ld.make_text(alpha))
                except Exception:
                    pass
                time.sleep(_STEP_S)

        # Fade-out de la barra
        if self.bar:
            for alpha in _FADE_OUT:
                try:
                    self.bar.setColorDiffuse(f"{alpha:02X}FFFFFF")
                except Exception:
                    pass
                time.sleep(_STEP_S)

        # Remover todos los controles
        for lbl, _ in self.labels:
            try:
                self.window.removeControl(lbl)
            except Exception:
                pass
        if self.bar:
            try:
                self.window.removeControl(self.bar)
            except Exception:
                pass
        self.labels = []
        self.bar    = None





class ParentalPlayer(xbmc.Player):
    """Reproductor personalizado para detectar e interactuar con la reproducción de videos."""
    def __init__(self):
        super().__init__()
        self.active_overlay = None
        self.playback_thread = None
        self.stop_event = threading.Event()
        self.playback_lock = threading.Lock()
        self.thread_active = False

    def onPlayBackStarted(self):
        log("Evento de reproducción iniciado (onPlayBackStarted) recibido.")
        self._trigger_start("onPlayBackStarted")

    def onAVStarted(self):
        log("Evento de audio/video iniciado (onAVStarted) recibido.")
        self._trigger_start("onAVStarted")

    def _trigger_start(self, event_name):
        with self.playback_lock:
            if self.thread_active:
                log(f"El proceso de overlay ya está activo, ignorando evento: {event_name}")
                return
            self.thread_active = True

        log(f"Iniciando hilo de procesamiento gatillado por: {event_name}")
        self.stop_event.set()
        if self.playback_thread and self.playback_thread.is_alive():
            self.playback_thread.join()
            
        self.stop_event.clear()
        self.playback_thread = threading.Thread(target=self._process_playback)
        self.playback_thread.daemon = True
        self.playback_thread.start()

    def onPlayBackStopped(self):
        log("Reproducción detenida por el usuario.")
        with self.playback_lock:
            self.thread_active = False
        self.stop_event.set()
        self.close_overlay()

    def onPlayBackEnded(self):
        log("Reproducción finalizada.")
        with self.playback_lock:
            self.thread_active = False
        self.stop_event.set()
        self.close_overlay()

    def close_overlay(self):
        """Cierra el overlay de forma segura si está en pantalla."""
        if self.active_overlay:
            try:
                self.active_overlay.close()
                log("Overlay de control parental cerrado.")
            except Exception as e:
                log(f"Error cerrando el overlay: {e}", level=xbmc.LOGWARNING)
            self.active_overlay = None

    def _process_playback(self):
        try:
            # 1. Esperar 1 segundo antes de iniciar el procesamiento
            for _ in range(10):
                if self.stop_event.is_set() or not self.isPlayingVideo():
                    return
                time.sleep(0.1)

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
            if media_type == 'show' and season_num and episode_num:
                ep_id = self._get_episode_imdb_id(imdb_id, season_num, episode_num)
                if ep_id:
                    log(f"ID específico del episodio S{season_num:02d}E{episode_num:02d}: {ep_id}")
                    imdb_id = ep_id
                else:
                    log(f"No se encontró ID de episodio, usando ID de serie: {imdb_id}", level=xbmc.LOGWARNING)



            # 3. Descargar la guía parental de MdbList
            try:
                movie_title, guide = main.get_parental_guide(imdb_id, progress_dialog=None, silent=True, media_type=media_type)
            except Exception as e:
                log(f"Error al descargar la guía parental: {e}", level=xbmc.LOGERROR)
                return

            if not guide:
                log(f"No se encontró información de guía parental para el ID: {imdb_id}.")
                return

            # 4. Crear y mostrar la ventana superpuesta en la pantalla
            if self.stop_event.is_set() or not self.isPlayingVideo():
                return

            log(f"Mostrando overlay inyectado para: {movie_title}")
            try:
                self.active_overlay = ParentalGuideOverlay()
                self.active_overlay.show_guide(guide)
                
                # 5. Mantener en pantalla por exactamente 7 segundos
                for _ in range(70):
                    if self.stop_event.is_set() or not self.isPlayingVideo():
                        break
                    time.sleep(0.1)
                    
                # 6. Cerrar el overlay
                self.close_overlay()
                
            except Exception as e:
                log(f"Excepción al inicializar/mostrar el overlay XML: {e}", level=xbmc.LOGERROR)
        finally:
            with self.playback_lock:
                self.thread_active = False

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
        """Resuelve el IMDb ID específico de un episodio usando la API de GraphQL de IMDb via main.py."""
        return main.get_episode_imdb_id(series_id, season, episode)


def main_loop():
    monitor = xbmc.Monitor()
    player = ParentalPlayer()
    
    log("Servicio de Control Parental MdbList iniciado.")
    
    while not monitor.abortRequested():
        if monitor.waitForAbort(1):
            break
            
    player.close_overlay()
    log("Servicio de Control Parental MdbList detenido.")

if __name__ == '__main__':
    main_loop()
