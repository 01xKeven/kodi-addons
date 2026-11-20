import sys
import re
import json
import xbmc
import xbmcgui
import xbmcaddon
import requests

# Configuración del Addon
_addon_name = "Control Parental IMDb"
ADDON_ID = "service.guiaparental"


def create_addon(addon_id=ADDON_ID, attempts=20, wait_ms=250):
    """Devuelve el objeto del addon aunque Kodi lo esté actualizando en ese momento.

    Si Kodi instala una versión nueva del addon justo mientras se inicia el
    servicio, durante unos instantes el addon todavía no está registrado y
    xbmcaddon.Addon(id) lanza «Unknown addon id». Reintentamos unos segundos
    para que el arranque no falle ni muestre un error al usuario.
    """
    try:
        # Sin id: Kodi lo deduce de la carpeta del script en ejecución.
        return xbmcaddon.Addon()
    except Exception:
        pass

    for _ in range(attempts):
        try:
            return xbmcaddon.Addon(addon_id)
        except Exception:
            try:
                xbmc.sleep(wait_ms)
            except Exception:
                pass

    # Último intento: si falla, que el error quede visible en el log.
    return xbmcaddon.Addon(addon_id)


ADDON = create_addon()

# Diccionario para los colores y símbolos de severidad
SEVERITY_VISUALS = {
    "noneVotes":     {'color': 'white',  'char': '[COLOR white]█[/COLOR]'},
    "mildVotes":     {'color': 'green',  'char': '[COLOR green]█[/COLOR]'},
    "moderateVotes": {'color': 'yellow', 'char': '[COLOR yellow]█[/COLOR]'},
    "severeVotes":   {'color': 'red',    'char': '[COLOR red]█[/COLOR]'}
}

# Orden de aparición en pantalla
CATEGORY_ORDER = ["SEXUAL_CONTENT", "VIOLENCE", "PROFANITY", "ALCOHOL_DRUGS", "FRIGHTENING_INTENSE_SCENES"]

# Textos según el idioma utilizando strings.po de Kodi
STRING_MAPPING = {
    'SEXUAL_CONTENT': 30000,
    'VIOLENCE': 30001,
    'PROFANITY': 30002,
    'ALCOHOL_DRUGS': 30003,
    'FRIGHTENING_INTENSE_SCENES': 30004,
    'none': 30005,
    'no_data': 30030,
    'mild': 30006,
    'moderate': 30007,
    'severe': 30008,
    'title': 30009,
    'connecting': 30010,
    'processing': 30011,
    'finalizing': 30012,
    'no_results_title': 30013,
    'no_results_body': 30014,
    'error_title': 30015,
    'error_format': 30016,
    'error_connect': 30017,
    'keyboard_prompt': 30018,
    'post_credits': 30021,
    'rating': 30035,
    'rated': 30036,
    'votes': 30037,
    'show_overlay': 30038,
    'show_rating': 30039,
    'show_certificate': 30040,
    'show_post_credits': 30041,
    'layout': 30042,
    'layout_sidebar': 30043,
    'layout_minimal': 30046,
    'animation': 30048,
    'anim_fade': 30049,
    'anim_slide': 30050,
    'anim_slide_v': 30051,
    'anim_stagger': 30052,
    'anim_none': 30053,
    'anim_classic': 30063,
    'show_overall': 30054,
    'overall': 30055,
    'show_extra_meta': 30056,
    'show_imdb_id': 30057,
    'show_countdown': 30058,
    'year': 30059,
    'runtime': 30060,
    'genres': 30061,
    'imdb_id': 30062,
}

FALLBACK_STRINGS = {
    'es': {
        'SEXUAL_CONTENT': "Desnudos",
        'VIOLENCE': "Violencia",
        'PROFANITY': "Groserias",
        'ALCOHOL_DRUGS': "Alcohol y Drogas",
        'FRIGHTENING_INTENSE_SCENES': "Escenas intensas",
        'none': "Ninguna",
        'no_data': "Sin datos",
        'mild': "Leve",
        'moderate': "Moderada",
        'severe': "Severa",
        'title': "Control Parental IMDb",
        'connecting': "Conectando con IMDb...",
        'processing': "Procesando clasificaciones...",
        'finalizing': "Finalizando...",
        'no_results_title': "Sin Resultados",
        'no_results_body': 'No se encontró guía parental para "{title}".',
        'error_title': "Error",
        'error_format': "El formato del ID de IMDb es incorrecto.",
        'error_connect': "No se pudo obtener la guía parental: {error}",
        'keyboard_prompt': "Introduce el ID de IMDb (ttxxxxxxx)",
        'post_credits': "Post-Créditos",
        'rating': "Valoración",
        'rated': "Clasificación",
        'votes': "votos",
        'show_overlay': "Mostrar overlay",
        'show_rating': "Mostrar valoración y votos",
        'show_certificate': "Mostrar clasificación por edades",
        'show_post_credits': "Mostrar escenas post-créditos (requiere API de TMDb)",
        'layout': "Diseño del overlay",
        'layout_sidebar': "Barra lateral",
        'layout_minimal': "Minimalista",
        'animation': "Animación del overlay",
        'anim_classic': "Clásica (original)",
        'anim_fade': "Fundido",
        'anim_slide': "Deslizar lateral",
        'anim_slide_v': "Deslizar vertical",
        'anim_stagger': "Escalonada",
        'anim_none': "Sin animación",
        'show_overall': "Mostrar nivel general",
        'overall': "Nivel general",
        'show_extra_meta': "Mostrar año, duración y géneros",
        'show_imdb_id': "Mostrar ID de IMDb",
        'show_countdown': "Mostrar barra de duración",
        'year': "Año",
        'runtime': "Duración",
        'genres': "Géneros",
        'imdb_id': "ID IMDb",
    },
    'en': {
        'SEXUAL_CONTENT': "Nudity",
        'VIOLENCE': "Violence",
        'PROFANITY': "Profanity",
        'ALCOHOL_DRUGS': "Alcohol & Drugs",
        'FRIGHTENING_INTENSE_SCENES': "Intense Scenes",
        'none': "None",
        'no_data': "No data",
        'mild': "Mild",
        'moderate': "Moderate",
        'severe': "Severe",
        'title': "IMDb Parental Guide",
        'connecting': "Connecting to IMDb...",
        'processing': "Processing ratings...",
        'finalizing': "Finalizing...",
        'no_results_title': "No Results",
        'no_results_body': 'No parental guide found for "{title}".',
        'error_title': "Error",
        'error_format': "The format of the IMDb ID is incorrect.",
        'error_connect': "Could not retrieve parental guide: {error}",
        'keyboard_prompt': "Enter IMDb ID (ttxxxxxxx)",
        'post_credits': "Post-Credits",
        'rating': "Rating",
        'rated': "Rated",
        'votes': "votes",
        'show_overlay': "Show overlay",
        'show_rating': "Show rating and votes",
        'show_certificate': "Show age rating",
        'show_post_credits': "Show post-credits scenes (TMDb API required)",
        'layout': "Overlay design",
        'layout_sidebar': "Sidebar",
        'layout_minimal': "Minimal",
        'animation': "Overlay animation",
        'anim_classic': "Classic (original)",
        'anim_fade': "Fade",
        'anim_slide': "Slide side",
        'anim_slide_v': "Slide vertical",
        'anim_stagger': "Staggered",
        'anim_none': "No animation",
        'show_overall': "Show overall level",
        'overall': "Overall level",
        'show_extra_meta': "Show year, runtime and genres",
        'show_imdb_id': "Show IMDb ID",
        'show_countdown': "Show duration bar",
        'year': "Year",
        'runtime': "Runtime",
        'genres': "Genres",
        'imdb_id': "IMDb ID",
    }
}

def get_language():
    try:
        # Método 1: Código ISO 639-1 (ej. 'en', 'es')
        lang = xbmc.getLanguage(xbmc.ISO_639_1)
        if lang:
            return 'es' if lang.lower().startswith('es') else 'en'
    except Exception:
        pass
    try:
        # Método 2: Nombre en inglés del idioma (ej. 'English', 'Spanish')
        lang_name = xbmc.getLanguage()
        if lang_name:
            if 'spanish' in lang_name.lower() or 'espanol' in lang_name.lower():
                return 'es'
            return 'en'
    except Exception:
        pass
    # Defecto: inglés
    return 'en'

def get_string(key):
    string_id = STRING_MAPPING.get(key, key)
    if isinstance(string_id, int):
        try:
            val = ADDON.getLocalizedString(string_id)
            if val:
                return val
        except Exception:
            pass
    # Fallback si getLocalizedString devuelve vacío (p.ej. al arrancar Kodi)
    lang = get_language()
    result = FALLBACK_STRINGS[lang].get(key, FALLBACK_STRINGS['en'].get(key, str(key)))
    return result

def log(msg, level=xbmc.LOGINFO):
    """Función para escribir en el log de Kodi."""
    xbmc.log(f"[{_addon_name}] {msg}", level=level)

# Keywords de TMDB para escenas durante/post créditos (dato estructurado, sin parsear comentarios)
TMDB_KEYWORD_POST = 179430  # "post-credit scene"
TMDB_KEYWORD_MID = 179431   # "during-credits stinger"

# Caché en memoria por sesión: imdb_id -> conteo (0-2) o None si no se pudo resolver
_tmdb_stinger_cache = {}

# Caché en memoria por sesión para la guía parental completa
_parental_guide_cache = {}

# API Key predeterminada de MDBList (oculta en código, no expuesta en ajustes)
_DEFAULT_MDBLIST_API_KEY = "0cmajscves055oixkk32bjd5z"


def get_mdblist_api_key():
    """Devuelve la API key predeterminada de MDBList."""
    return _DEFAULT_MDBLIST_API_KEY


TMDB_GENRE_NAMES = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime",
    99: "Documentary", 18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History",
    27: "Horror", 10402: "Music", 9648: "Mystery", 10749: "Romance", 878: "Sci-Fi",
    10770: "TV Movie", 53: "Thriller", 10752: "War", 37: "Western",
    10759: "Action & Adventure", 10762: "Kids", 10763: "News", 10764: "Reality",
    10765: "Sci-Fi & Fantasy", 10766: "Soap", 10767: "Talk", 10768: "War & Politics"
}


def get_tmdb_genres(imdb_id):
    """Obtiene géneros en inglés desde TMDb /find."""
    api_key = get_tmdb_api_key()
    if not api_key:
        return []
    try:
        r = requests.get(
            f'https://api.themoviedb.org/3/find/{imdb_id}',
            params={'api_key': api_key, 'external_source': 'imdb_id'},
            timeout=5
        )
        if r.status_code == 200:
            data = r.json()
            genre_ids = []
            for key in ('movie_results', 'tv_results'):
                items = data.get(key, [])
                if items and items[0].get('genre_ids'):
                    genre_ids = items[0]['genre_ids']
                    break
            return [TMDB_GENRE_NAMES[gid] for gid in genre_ids if gid in TMDB_GENRE_NAMES]
    except Exception:
        pass
    return []


def _match_kw_term(term, kw):
    """Comprueba si el término coincide exactamente o como token delimitado por guiones."""
    target = f'-{term}-'
    subject = f'-{kw}-'
    return target in subject


def get_mdblist_parental_guide(imdb_id):
    """
    Obtiene la guía parental y metadatos desde MDBList.
    Clasifica las 5 categorías oficiales utilizando keywords granulares,
    certificación y puntuación de Common Sense Media.
    """
    mdblist_key = get_mdblist_api_key()
    if not mdblist_key:
        return None

    tmdb_key = get_tmdb_api_key()

    try:
        url = f'https://mdblist.com/api/?apikey={mdblist_key}&i={imdb_id}'
        r = requests.get(url, timeout=6)
        if r.status_code != 200:
            log(f"MDBList devolvió código HTTP {r.status_code} para {imdb_id}", level=xbmc.LOGWARNING)
            return None

        d = r.json()

        # Si no se encuentra (ej. es ID de episodio) y tenemos TMDb key, buscar la serie padre
        if d.get('response') is False and tmdb_key:
            try:
                r_find = requests.get(
                    f'https://api.themoviedb.org/3/find/{imdb_id}',
                    params={'api_key': tmdb_key, 'external_source': 'imdb_id'},
                    timeout=5
                )
                if r_find.status_code == 200:
                    ep_res = r_find.json().get('tv_episode_results', [])
                    if ep_res and ep_res[0].get('show_id'):
                        show_id = ep_res[0]['show_id']
                        r_ext = requests.get(
                            f'https://api.themoviedb.org/3/tv/{show_id}/external_ids',
                            params={'api_key': tmdb_key},
                            timeout=5
                        )
                        if r_ext.status_code == 200:
                            parent_imdb = r_ext.json().get('imdb_id')
                            if parent_imdb and parent_imdb != imdb_id:
                                log(f"ID {imdb_id} es un episodio; consultando serie padre {parent_imdb} en MDBList")
                                return get_mdblist_parental_guide(parent_imdb)
            except Exception as e:
                log(f"Error resolviendo serie padre en TMDb para MDBList: {e}", level=xbmc.LOGWARNING)

        if not d or d.get('response') is False or not d.get('title'):
            return None

        title = d.get('title')
        cert = str(d.get('certification') or '').upper()
        cs = d.get('commonsense') or 0
        try:
            cs = int(cs)
        except Exception:
            cs = 0

        is_adult = any(x in cert for x in ['18', 'R', 'TV-MA', 'NC-17', 'MA 15', 'ADULT']) or cs >= 16
        is_teen = any(x in cert for x in ['16', '15', '14', 'PG-13', 'TV-14', 'M']) or (13 <= cs < 16)
        is_kids = any(x == cert for x in ['G', 'TV-Y', 'TV-G', 'TV-Y7', 'U', 'AL']) or (0 < cs <= 8)

        kws = [k['name'].lower() for k in d.get('keywords', []) if isinstance(k, dict) and 'name' in k]

        def has_any(term_list):
            for term in term_list:
                for kw in kws:
                    if _match_kw_term(term, kw):
                        return True
            return False

        def calc_sev(severe_kws, moderate_kws, mild_kws, adult_weight=False):
            if is_kids:
                return "none"
            if has_any(severe_kws):
                return "severe" if (is_adult or not is_kids) else "moderate"
            if has_any(moderate_kws):
                return "severe" if (is_adult and adult_weight) else "moderate"
            if has_any(mild_kws):
                return "moderate" if (is_adult and adult_weight) else "mild"
            if is_adult and adult_weight:
                return "mild"
            return "none"

        sex_sev = calc_sev(
            ['full-frontal-nudity', 'explicit-sex', 'hardcore', 'eroticism', 'naked-female-breasts', 'gangbang', 'sexual-assault', 'incest', 'rape'],
            ['nudity', 'sex-scene', 'female-nudity', 'male-nudity', 'erotic', 'nude', 'rear-nudity', 'topless'],
            ['kiss', 'cleavage', 'bikini', 'underwear', 'seduction', 'flirting', 'innuendo']
        )
        if is_kids:
            sex_sev = "none"

        viol_sev = calc_sev(
            ['bloody-violence', 'extreme-violence', 'gore', 'torture', 'mutilation', 'decapitation', 'massacre', 'brutal', 'slaughter'],
            ['violence', 'blood', 'murder', 'shotgun', 'machine-gun', 'gunfight', 'stabbing', 'kill', 'assassin', 'dead-body', 'corpse'],
            ['fight', 'gun', 'weapon', 'police', 'chase', 'combat', 'brawl', 'punch', 'martial-arts'],
            adult_weight=True
        )
        if is_kids:
            viol_sev = "mild" if has_any(['fight', 'chase', 'danger', 'action']) else "none"

        if is_kids:
            prof_sev = "none"
        elif has_any(['f-word', 'profanity', 'strong-language', 'foul-language', 'swearing', 'vulgarity', 'f-bomb']):
            prof_sev = "severe" if is_adult else "moderate"
        elif is_adult:
            prof_sev = "moderate"
        elif is_teen:
            prof_sev = "mild"
        else:
            prof_sev = "none"

        drug_sev = calc_sev(
            ['illegal-drug', 'drug-trafficking', 'drug-dealer', 'drug-ring', 'drug-use', 'cocaine', 'heroin', 'meth', 'overdose', 'drug-addiction', 'substance-abuse'],
            ['drugs', 'marijuana', 'cannabis', 'alcoholism', 'drunk', 'drugged', 'intoxication'],
            ['alcohol', 'smoking', 'cigarette', 'cigar', 'beer', 'wine', 'bar', 'drinking']
        )
        if is_kids:
            drug_sev = "none"

        fear_sev = calc_sev(
            ['cosmic-horror', 'psychological-horror', 'supernatural-horror', 'terrifying', 'gore-horror', 'nightmare-inducing', 'gruesome'],
            ['horror', 'terror', 'chilling', 'horror-elements', 'creepy', 'demon', 'haunted', 'slasher', 'disturbing'],
            ['suspense', 'fear', 'tension', 'thriller', 'mystery', 'dark', 'claustrophobia']
        )
        if is_kids:
            fear_sev = "mild" if has_any(['fear', 'monster', 'dark']) else "none"

        cats_map = {
            'SEXUAL_CONTENT': sex_sev,
            'VIOLENCE': viol_sev,
            'PROFANITY': prof_sev,
            'ALCOHOL_DRUGS': drug_sev,
            'FRIGHTENING_INTENSE_SCENES': fear_sev
        }

        guide = []
        for cat_key in CATEGORY_ORDER:
            cat_label = get_string(cat_key)
            sev = cats_map.get(cat_key, "none")
            sev_text, sev_class = severity_to_class(sev)
            guide.append({
                'category':      cat_label,
                'raw_category':  cat_key,
                'severity_text': sev_text,
                'severity_class': sev_class,
                'item_list':     [],
                'is_default':    False
            })

        imdb_rating, imdb_votes = None, None
        for r_item in d.get('ratings', []):
            if r_item.get('source') == 'imdb':
                imdb_rating = r_item.get('value')
                imdb_votes = r_item.get('votes')
                break

        certificate = d.get('certification')
        if not certificate and cs:
            certificate = f"{cs}+"

        runtime_min = d.get('runtime') or 0
        runtime_sec = int(runtime_min) * 60 if runtime_min else None

        genres = []
        if d.get('genres'):
            for g in d['genres']:
                if isinstance(g, dict) and g.get('name'):
                    genres.append(g['name'])
                elif isinstance(g, str):
                    genres.append(g)

        meta = {
            'rating': imdb_rating,
            'votes': imdb_votes,
            'certificate': certificate,
            'year': d.get('year'),
            'runtime': runtime_sec,
            'genres': genres
        }
        return title, guide, meta
    except Exception as err:
        log(f"Error consultando MDBList: {err}", level=xbmc.LOGWARNING)
        return None


def get_tmdb_api_key():
    """Devuelve la API key de TMDB configurada en los ajustes (o cadena vacía)."""
    try:
        return (ADDON.getSetting('tmdb_api_key') or '').strip()
    except Exception:
        return ''


def get_tmdb_movie_id(imdb_id, api_key):
    """Mapea un IMDb ID a su TMDB ID usando /find. Devuelve int o None."""
    try:
        r = requests.get(
            f'https://api.themoviedb.org/3/find/{imdb_id}',
            params={'api_key': api_key, 'external_source': 'imdb_id'},
            timeout=10
        )
        if r.status_code != 200:
            log(f"TMDb /find devolvió código {r.status_code} para {imdb_id}", level=xbmc.LOGWARNING)
            return None
        results = r.json().get('movie_results', [])
        if results and results[0].get('id'):
            return int(results[0]['id'])
        log(f"TMDb no encontró película para {imdb_id}", level=xbmc.LOGWARNING)
    except Exception as e:
        log(f"Error mapeando IMDb ID en TMDb: {e}", level=xbmc.LOGWARNING)
    return None


def get_tmdb_stinger_count(imdb_id):
    """
    Cuenta los tramos con stinger según TMDB (0-2): mitad (179431) y/o final (179430).
    Devuelve None si no hay API key o no se pudo resolver (la fila se oculta).
    """
    api_key = get_tmdb_api_key()
    if not api_key:
        # Las escenas post-créditos dependen de TMDb: sin clave de API no hay dato.
        log("Sin clave de API de TMDb: no se pueden consultar las escenas "
            "post-créditos. Configúrala en los ajustes del addon, pestaña «API».",
            level=xbmc.LOGWARNING)
        return None
    if imdb_id in _tmdb_stinger_cache:
        return _tmdb_stinger_cache[imdb_id]
    count = None
    try:
        tmdb_id = get_tmdb_movie_id(imdb_id, api_key)
        if tmdb_id:
            r = requests.get(
                f'https://api.themoviedb.org/3/movie/{tmdb_id}/keywords',
                params={'api_key': api_key},
                timeout=10
            )
            if r.status_code == 200:
                kw_ids = {k.get('id') for k in r.json().get('keywords', [])}
                count = (TMDB_KEYWORD_MID in kw_ids) + (TMDB_KEYWORD_POST in kw_ids)
            else:
                log(f"TMDb keywords devolvió código {r.status_code} para {tmdb_id}", level=xbmc.LOGWARNING)
    except Exception as e:
        log(f"Error consultando keywords de TMDb: {e}", level=xbmc.LOGWARNING)
    _tmdb_stinger_cache[imdb_id] = count
    return count


def get_tmdb_stinger_info(imdb_id):
    """
    Construye el dict stinger_info para el overlay desde TMDB.
    Solo devuelve datos si hay al menos un tramo con stinger; si no hay
    (o no hay API key / falló la consulta) devuelve None y la fila se oculta.
    """
    count = get_tmdb_stinger_count(imdb_id)
    if not count:
        return None
    return {
        'has_stinger': True,
        'stinger_text': str(count),
        'stinger_color': "FF00E5FF"
    }

def severity_to_class(level):
    """Convierte el nivel de severidad de IMDb a la clase visual interna."""
    mapping = {
        "none":     (get_string("none"),  "noneVotes"),
        "mild":     (get_string("mild"),  "mildVotes"),
        "moderate": (get_string("moderate"), "moderateVotes"),
        "severe":   (get_string("severe"),   "severeVotes"),
    }
    return mapping.get(level, (get_string("none"), "noneVotes"))


def format_votes(n):
    """Formatea el número de votos de forma compacta: 1200 -> 1.2k, 3253614 -> 3.3M."""
    try:
        n = int(n)
    except Exception:
        return ""
    if n >= 1_000_000:
        return ("%.1fM" % (n / 1_000_000.0)).replace(".0M", "M")
    if n >= 1_000:
        return ("%.1fk" % (n / 1_000.0)).replace(".0k", "k")
    return str(n)


def format_runtime(seconds):
    """Formatea la duración: 8160 -> '2h 16min'."""
    try:
        total = int(seconds)
    except Exception:
        return ""
    if total <= 0:
        return ""
    minutes = total // 60
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}min"
    return f"{minutes}min"


# IMDb devuelve los géneros en inglés; los traducimos al español si el idioma de Kodi lo es.
_GENRES_ES = {
    "Action": "Acción", "Adult": "Adultos", "Adventure": "Aventura", "Animation": "Animación",
    "Biography": "Biografía", "Comedy": "Comedia", "Crime": "Crimen", "Documentary": "Documental",
    "Drama": "Drama", "Family": "Familia", "Fantasy": "Fantasía", "Film-Noir": "Cine negro",
    "Game-Show": "Concurso", "History": "Historia", "Horror": "Terror", "Music": "Música",
    "Musical": "Musical", "Mystery": "Misterio", "News": "Noticias", "Reality-TV": "Reality show",
    "Romance": "Romance", "Sci-Fi": "Ciencia ficción", "Short": "Cortometraje", "Sport": "Deporte",
    "Talk-Show": "Tertulia", "Thriller": "Suspense", "War": "Bélica", "Western": "Western",
}


def translate_genres(genres):
    """Traduce la lista de géneros de IMDb al idioma actual de Kodi (español o inglés)."""
    if not genres:
        return []
    try:
        lang = (xbmc.getLanguage(xbmc.ISO_639_1) or 'en').lower()
    except Exception:
        lang = 'en'
    if lang.startswith('es'):
        return [_GENRES_ES.get(g, g) for g in genres]
    return list(genres)


def get_bool_setting(setting_id, default=True):
    """Lee un ajuste booleano del addon ('true'/'false')."""
    try:
        val = ADDON.getSetting(setting_id)
    except Exception:
        return default
    if val is None or val == '':
        return default
    return str(val).strip().lower() in ('true', '1', 'yes', 'on')

def get_episode_imdb_id(series_id, season, episode):
    """Resuelve el IMDb ID específico de un episodio usando TMDb o GraphQL de IMDb."""
    tmdb_key = get_tmdb_api_key()
    if tmdb_key:
        try:
            r = requests.get(
                f'https://api.themoviedb.org/3/find/{series_id}',
                params={'api_key': tmdb_key, 'external_source': 'imdb_id'},
                timeout=5
            )
            if r.status_code == 200:
                tv_res = r.json().get('tv_results', [])
                if tv_res and tv_res[0].get('id'):
                    tv_id = tv_res[0]['id']
                    r_ep = requests.get(
                        f'https://api.themoviedb.org/3/tv/{tv_id}/season/{season}/episode/{episode}/external_ids',
                        params={'api_key': tmdb_key},
                        timeout=5
                    )
                    if r_ep.status_code == 200:
                        ep_imdb = r_ep.json().get('imdb_id')
                        if ep_imdb and ep_imdb.startswith('tt'):
                            log(f"ID de episodio S{season:02d}E{episode:02d} resuelto vía TMDb: {ep_imdb}")
                            return ep_imdb
        except Exception as e:
            log(f"Error resolviendo episodio con TMDb: {e}", level=xbmc.LOGWARNING)

    # Intento con IMDb GraphQL (con timeout corto para no demorar la reproducción)
    try:
        url = 'https://api.graphql.imdb.com/'
        query = """
        query {
          title(id: "%s") {
            episodes {
              episodes(filter: {includeSeasons: ["%s"]}, first: 250) {
                edges {
                  node {
                    id
                    series {
                      displayableEpisodeNumber {
                        episodeNumber {
                          episodeNumber
                        }
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """ % (series_id, season)
        
        headers = {
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36',
            'Referer': 'https://www.imdb.com/',
            'Origin': 'https://www.imdb.com',
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9,es;q=0.8'
        }
        r = requests.post(url, json={'query': query}, headers=headers, timeout=3)
        if r.status_code == 200:
            data = r.json()
            edges = data.get('data', {}).get('title', {}).get('episodes', {}).get('episodes', {}).get('edges', [])
            for edge in edges:
                node = edge.get('node', {})
                ep_num_str = node.get('series', {}).get('displayableEpisodeNumber', {}).get('episodeNumber', {}).get('episodeNumber')
                if ep_num_str and int(ep_num_str) == int(episode):
                    ep_id = node.get('id', '')
                    if ep_id.startswith('tt'):
                        return ep_id
        else:
            log(f"IMDb GraphQL devolvió código {r.status_code} al buscar episodios", level=xbmc.LOGWARNING)
    except Exception as e:
        log(f"Error buscando ID de episodio en GraphQL: {e}", level=xbmc.LOGWARNING)
    return None


def empty_guide():
    """Guía de 5 filas marcadas como sin información (distinto de votado 'ninguna')."""
    return [{
        'category': get_string(cat_key),
        'raw_category': cat_key,
        'severity_text': get_string('no_data'),
        'severity_class': 'noneVotes',
        'item_list': [],
        'is_default': True
    } for cat_key in CATEGORY_ORDER]


def is_empty_guide(guide):
    """True si la guía no trae información real (todas las filas por defecto)."""
    return not guide or all(item.get('is_default') for item in guide)


def get_parental_guide(imdb_id, progress_dialog=None, silent=False, media_type=None):
    """
    Obtiene la guía parental y metadatos de IMDb.
    Si la API de IMDb no responde o es bloqueada (ej. 403 WAF), devuelve None
    para que el overlay no se muestre de forma errónea o vacía.
    """
    if not imdb_id or not imdb_id.startswith('tt') or not imdb_id[2:].isdigit():
        log(f"ID de IMDb inválido: {imdb_id}", level=xbmc.LOGERROR)
        if not silent:
            xbmcgui.Dialog().ok(get_string("error_title"), get_string("error_format"))
        return None, None, None, None

    if imdb_id in _parental_guide_cache:
        log(f"Guía parental recuperada de caché para {imdb_id}")
        return _parental_guide_cache[imdb_id]

    if progress_dialog:
        progress_dialog.update(20, get_string("connecting"))

    movie_title = None
    parental_guide = None
    meta = {}

    # 1. Intentar resolver con MDBList (fuente primaria precisa, sin bloqueo CloudFront WAF)
    try:
        mdblist_res = get_mdblist_parental_guide(imdb_id)
        if mdblist_res:
            movie_title, parental_guide, meta = mdblist_res
            log(f"Guía parental obtenida exitosamente vía MDBList para: {movie_title}")
    except Exception as e:
        log(f"Error procesando guía con MDBList: {e}", level=xbmc.LOGWARNING)

    # 2. Fallback secundario: IMDb GraphQL (si estuviera accesible)
    if not parental_guide:
        try:
            url = 'https://api.graphql.imdb.com/'
            query = """
            query {
              title(id: "%s") {
                id
                titleText {
                  text
                }
                ratingsSummary {
                  aggregateRating
                  voteCount
                }
                certificate {
                  rating
                }
                releaseYear {
                  year
                }
                runtime {
                  seconds
                }
                genres {
                  genres {
                    text
                  }
                }
                parentsGuide {
                  categories {
                    category {
                      id
                      text
                    }
                    severity {
                      id
                    }
                  }
                }
              }
            }
            """ % imdb_id

            headers = {
                'Content-Type': 'application/json',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36',
                'Referer': 'https://www.imdb.com/',
                'Origin': 'https://www.imdb.com',
                'Accept': '*/*',
                'Accept-Language': 'en-US,en;q=0.9,es;q=0.8'
            }
            
            log(f"Consultando IMDb GraphQL para la guía parental: {imdb_id}")
            r = requests.post(url, json={'query': query}, headers=headers, timeout=5)

            if r.status_code == 200:
                data = r.json()
                title_data = data.get("data", {}).get("title")
                if title_data:
                    movie_title = title_data.get("titleText", {}).get("text", imdb_id)
                    ratings_summary = title_data.get("ratingsSummary") or {}
                    genres = []
                    try:
                        for g in ((title_data.get("genres") or {}).get("genres") or []):
                            if g and g.get("text"):
                                genres.append(g["text"])
                    except Exception:
                        genres = []
                    meta = {
                        'rating': ratings_summary.get("aggregateRating"),
                        'votes': ratings_summary.get("voteCount"),
                        'certificate': (title_data.get("certificate") or {}).get("rating"),
                        'year': (title_data.get("releaseYear") or {}).get("year"),
                        'runtime': (title_data.get("runtime") or {}).get("seconds"),
                        'genres': genres,
                    }
                    parents_guide = title_data.get("parentsGuide")
                    categories = parents_guide.get("categories", []) if parents_guide else []
                    if categories:
                        graphql_cat_map = {
                            "NUDITY": "SEXUAL_CONTENT",
                            "VIOLENCE": "VIOLENCE",
                            "PROFANITY": "PROFANITY",
                            "ALCOHOL": "ALCOHOL_DRUGS",
                            "FRIGHTENING": "FRIGHTENING_INTENSE_SCENES"
                        }
                        entries_by_cat = {}
                        for cat_item in categories:
                            g_cat_id = cat_item.get("category", {}).get("id")
                            internal_key = graphql_cat_map.get(g_cat_id)
                            if internal_key:
                                entries_by_cat[internal_key] = cat_item

                        pg = []
                        for cat_key in CATEGORY_ORDER:
                            cat_label = get_string(cat_key)
                            entry = entries_by_cat.get(cat_key)
                            if entry and entry.get("severity"):
                                sev_id = entry["severity"].get("id", "noneVotes")
                                level = sev_id.replace("Votes", "")
                                sev_text, sev_class = severity_to_class(level)
                            else:
                                sev_text, sev_class = get_string("no_data"), "noneVotes"
                            pg.append({
                                'category':      cat_label,
                                'raw_category':  cat_key,
                                'severity_text': sev_text,
                                'severity_class': sev_class,
                                'item_list':     [],
                                'is_default':    False
                            })
                        parental_guide = pg
                        log(f"Guía parental de IMDb GraphQL obtenida con éxito para {movie_title}")
            else:
                log(f"IMDb GraphQL respondió con código {r.status_code} (bloqueo WAF CloudFront)", level=xbmc.LOGWARNING)
        except Exception as err:
            log(f"Error al consultar IMDb GraphQL: {err}", level=xbmc.LOGWARNING)

    # Si no se obtuvieron categorías válidas, NO mostramos el overlay (falla silenciosa intencionada)
    if not parental_guide:
        log(f"No se pudo obtener guía parental válida para {imdb_id} (API sin datos o no disponible). Overlay desactivado.")
        return None, None, None, None

    # Post-créditos desde TMDb
    stinger_info = get_tmdb_stinger_info(imdb_id)

    # Rellenar géneros desde TMDb si vienen vacíos
    if not meta.get('genres'):
        meta['genres'] = get_tmdb_genres(imdb_id)

    if progress_dialog:
        progress_dialog.update(95, get_string("finalizing"))

    result = (movie_title, parental_guide, stinger_info, meta)
    _parental_guide_cache[imdb_id] = result
    return result


def process_id(imdb_id):
    """Lógica de carga y visualización formateada para búsqueda manual."""
    log(f"Procesando ID manualmente: {imdb_id}")

    pDialog = xbmcgui.DialogProgress()
    pDialog.create(get_string("title"), get_string("connecting"))

    res = get_parental_guide(imdb_id, pDialog)
    if res and len(res) == 4:
        movie_title, guide, stinger_info, meta = res
    elif res and len(res) == 3:
        movie_title, guide, stinger_info = res
        meta = {}
    elif res and len(res) == 2:
        movie_title, guide = res
        stinger_info = None
        meta = {}
    else:
        movie_title, guide, stinger_info, meta = None, None, None, {}

    if not pDialog.iscanceled():
        pDialog.update(100, get_string("finalizing"))
        pDialog.close()

    if movie_title is not None and guide is not None:
        if guide:
            text = ""
            if get_bool_setting('show_rating', True) and meta.get('rating'):
                votes_txt = (" / " + format_votes(meta.get('votes'))) if meta.get('votes') else ""
                text += f"[B]{get_string('rating')}:[/B] [COLOR=FFD24A]{meta['rating']}{votes_txt}[/COLOR]\n"
            if get_bool_setting('show_certificate', True) and meta.get('certificate'):
                text += f"[B]{get_string('rated')}:[/B] [COLOR=4AC1FF]{meta['certificate']}[/COLOR]\n"
            if (get_bool_setting('show_rating', True) and meta.get('rating')) or (get_bool_setting('show_certificate', True) and meta.get('certificate')):
                text += "\n"

            for item in guide:
                visual_info = SEVERITY_VISUALS.get(item['severity_class'], {'color': 'white', 'char': ' '})
                color = visual_info['color']
                text += f"{visual_info['char']} [B]{item['category']}:[/B] [COLOR={color}]{item['severity_text']}[/COLOR]\n\n"

            if stinger_info and stinger_info.get('stinger_text') and get_bool_setting('show_post_credits', True):
                c_name = 'cyan' if stinger_info.get('has_stinger') else 'grey'
                text += f"[B]{get_string('post_credits')}:[/B] [COLOR={c_name}]{stinger_info['stinger_text']}[/COLOR]\n"

            xbmcgui.Dialog().textviewer(f"{get_string('title')}: {movie_title}", text)
        else:
            xbmcgui.Dialog().ok(get_string("no_results_title"), get_string("no_results_body").format(title=movie_title))


def main():
    while True:
        kb = xbmc.Keyboard('', get_string("keyboard_prompt"))
        kb.doModal()
        if kb.isConfirmed() and kb.getText():
            process_id(kb.getText().strip())
            break
        else:
            break


if __name__ == '__main__':
    main()