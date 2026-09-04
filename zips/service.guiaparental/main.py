import sys
import re
import xbmc
import xbmcgui
import xbmcaddon
import requests

# Configuración del Addon
_addon_name = "Control Parental IMDb"
ADDON = xbmcaddon.Addon()

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
    'audio_only': 30022,
}

FALLBACK_STRINGS = {
    'es': {
        'SEXUAL_CONTENT': "Desnudos",
        'VIOLENCE': "Violencia",
        'PROFANITY': "Groserias",
        'ALCOHOL_DRUGS': "Alcohol y Drogas",
        'FRIGHTENING_INTENSE_SCENES': "Escenas intensas",
        'none': "Ninguno",
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
        'audio_only': "Solo audio"
    },
    'en': {
        'SEXUAL_CONTENT': "Nudity",
        'VIOLENCE': "Violence",
        'PROFANITY': "Profanity",
        'ALCOHOL_DRUGS': "Alcohol & Drugs",
        'FRIGHTENING_INTENSE_SCENES': "Intense Scenes",
        'none': "None",
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
        'audio_only': "Audio only"
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

def severity_to_class(level):
    """Convierte el nivel de severidad de IMDb a la clase visual interna."""
    mapping = {
        "none":     (get_string("none"),  "noneVotes"),
        "mild":     (get_string("mild"),  "mildVotes"),
        "moderate": (get_string("moderate"), "moderateVotes"),
        "severe":   (get_string("severe"),   "severeVotes"),
    }
    return mapping.get(level, (get_string("none"), "noneVotes"))

def get_episode_imdb_id(series_id, season, episode):
    """Resuelve el IMDb ID específico de un episodio usando la API de GraphQL de IMDb."""
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
        r = requests.post(url, json={'query': query}, headers=headers, timeout=12)
        if r.status_code != 200:
            log(f"IMDb GraphQL devolvió código {r.status_code} al buscar episodios", level=xbmc.LOGWARNING)
            return None
            
        data = r.json()
        edges = data.get('data', {}).get('title', {}).get('episodes', {}).get('episodes', {}).get('edges', [])
        
        for edge in edges:
            node = edge.get('node', {})
            ep_num_str = node.get('series', {}).get('displayableEpisodeNumber', {}).get('episodeNumber', {}).get('episodeNumber')
            if ep_num_str and int(ep_num_str) == int(episode):
                ep_id = node.get('id', '')
                if ep_id.startswith('tt'):
                    return ep_id
                    
        log(f"Episodio S{season:02d}E{episode:02d} no encontrado en la respuesta de GraphQL", level=xbmc.LOGWARNING)
    except Exception as e:
        log(f"Error buscando ID de episodio en GraphQL: {e}", level=xbmc.LOGWARNING)
    return None

def parse_crazy_credits(crazy_credits_edges):
    """
    Analiza las entradas de crazyCredits para extraer la información de post-créditos.
    Retorna un diccionario con:
      - 'has_stinger': bool
      - 'stinger_text': str
      - 'stinger_color': str (formato hex Kodi AARRGGBB)
    """
    if not crazy_credits_edges:
        return {
            'has_stinger': False,
            'stinger_text': get_string("none"),
            'stinger_color': "FF7F8C8D"
        }

    def clean_stinger_text(t):
        t = re.sub(r'<[^>]+>', ' ', t)
        t = t.replace('&#39;', "'").replace('&quot;', '"').replace('&amp;', '&')
        t = re.sub(r'^(spoiler\s*:\s*|in\s+a\s+|there\s+is\s+a\s+)', '', t, flags=re.IGNORECASE)
        return t.strip()

    def are_duplicate_scenes(t1, t2):
        w1 = set(re.findall(r'\b[a-z]{4,}\b', t1.lower()))
        w2 = set(re.findall(r'\b[a-z]{4,}\b', t2.lower()))
        stop_words = {
            'credits', 'scene', 'after', 'during', 'closing', 'ending', 'appears', 
            'there', 'shows', 'movie', 'scenes', 'credit', 'beginning', 'first',
            'another', 'extra', 'final', 'mid-credit', 'post-credit', 'leads',
            'spoilers', 'spoiler'
        }
        w1 -= stop_words
        w2 -= stop_words
        if w1 and w2:
            overlap = len(w1 & w2)
            jaccard = overlap / len(w1 | w2)
            if overlap >= 3:
                return True
            if overlap >= 2 and jaccard > 0.25:
                return True
        return False

    mid_scenes = []
    after_scenes = []
    audio_only = 0

    for edge in crazy_credits_edges:
        raw_text = edge.get('node', {}).get('text', {}).get('plaidHtml', '')
        text = raw_text.lower()
        
        # 1. Filtro para ignorar notas de trivia general/resumen entre episodios
        if re.search(r'\b(only\s+\d+\s+episodes|episodes\s+have\s+post|episodes\s+got|there\s+are\s+\w+\s+extra\s+scenes|two\s+extra\s+scenes|last\s+episode,\s+there\s+are)\b', text):
            continue

        # 2. Ignorar menciones de dibujos, tipografías, disclaimer o logos que NO sean escenas narrativas
        if any(x in text for x in ['animated sequence featuring', 'illustrated drawings', 'drawings of', 'disclaimer in the closing', 'logo in the opening', 'costume designers', 'statement at the start', 'style and font of', 'concept art paintings']):
            continue

        # 3. Filtros clásicos de exclusión (dedicatorias, logos, etc.)
        if any(x in text for x in ['dedicated to', 'in memory of', 'aspect ratio', 'soundtrack', 'tribute', 'filmed in', 'special thanks']):
            if not any(x in text for x in ['scene', 'dialogue', 'audio', 'appears after', 'after the closing credits', 'hammer hitting']):
                continue

        # 4. Listas múltiples en HTML (ej. Guardians of the Galaxy Vol. 2)
        if '<ul>' in raw_text:
            li_items = re.findall(r'<li>(.*?)</li>', raw_text, flags=re.IGNORECASE)
            if len(li_items) >= 2:
                for li in li_items:
                    c_li = clean_stinger_text(li)
                    if not any(are_duplicate_scenes(c_li, s) for s in after_scenes):
                        after_scenes.append(c_li)
                continue

        # 5. Detección de audio exclusivo (ej. martilleo en Endgame)
        is_audio = any(x in text for x in ['sound of a hammer', 'sound of', 'voice of', 'heard after', 'hammering']) and not any(x in text for x in ['scene', 'video', 'footage', 'epilogue', 'post-credit'])
        is_scene = any(x in text for x in ['scene', 'epilogue', 'post-credits epilogue', 'mid-credits', 'post-credits', 'after the credits', 'after the closing credits', 'trailer for'])

        if is_audio and not is_scene:
            audio_only += 1
            continue

        # 6. Ignorar menciones explícitas de que NO hay escena
        if 'does not have a scene' in text or 'no scene' in text:
            continue

        # 7. Ignorar acciones menores de música / créditos
        if 'walks off with the music' in text or 'picks up his walkman' in text:
            continue

        # 8. Clasificación de tipo
        is_mid = any(x in text for x in ['mid-credit', 'mid credit', 'during the credit', 'during the closing credit', 'during the end credit', 'throughout the credit', 'beginning of the credits'])
        is_after = any(x in text for x in ['after the credit', 'after the closing credit', 'after the end credit', 'at the end of the closing credit', 'end of the credits', 'after the main credits', 'scene in the closing credits', 'scene at the end of the closing credits', 'post-credits epilogue', 'trailer for'])

        cleaned = clean_stinger_text(raw_text)

        if not is_mid and not is_after and is_scene:
            is_after = True

        if is_mid and not is_after:
            if not any(are_duplicate_scenes(cleaned, s) for s in mid_scenes):
                mid_scenes.append(cleaned)
        elif is_after and not is_mid:
            if not any(are_duplicate_scenes(cleaned, s) for s in after_scenes):
                after_scenes.append(cleaned)
        elif is_mid and is_after:
            if not any(are_duplicate_scenes(cleaned, s) for s in mid_scenes) and not any(are_duplicate_scenes(cleaned, s) for s in after_scenes):
                after_scenes.append(cleaned)

    total_scenes = len(mid_scenes) + len(after_scenes)

    if total_scenes == 0 and audio_only > 0:
        return {
            'has_stinger': True,
            'stinger_text': get_string("audio_only"),
            'stinger_color': "FFF1C40F"
        }

    if total_scenes == 0:
        return {
            'has_stinger': False,
            'stinger_text': get_string("none"),
            'stinger_color': "FF7F8C8D"
        }

    return {
        'has_stinger': True,
        'stinger_text': str(total_scenes),
        'stinger_color': "FF00E5FF"
    }

def get_parental_guide(imdb_id, progress_dialog=None, silent=False, media_type=None):
    """
    Obtiene la guía parental directamente de IMDb usando la API de GraphQL.
    Devuelve (title, parental_guide_list, stinger_info) o (None, None, None) en caso de error.
    """
    if not imdb_id or not imdb_id.startswith('tt') or not imdb_id[2:].isdigit():
        log(f"ID de IMDb inválido: {imdb_id}", level=xbmc.LOGERROR)
        if not silent:
            xbmcgui.Dialog().ok(get_string("error_title"), get_string("error_format"))
        return None, None, None

    if progress_dialog:
        progress_dialog.update(20, get_string("connecting"))

    try:
        url = 'https://api.graphql.imdb.com/'
        query = """
        query {
          title(id: "%s") {
            id
            titleText {
              text
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
            crazyCredits(first: 10) {
              edges {
                node {
                  text {
                    plaidHtml
                  }
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
        
        log(f"Consultando IMDb GraphQL para la guía parental y post-créditos: {imdb_id}")
        r = requests.post(url, json={'query': query}, headers=headers, timeout=12)

        if r.status_code != 200:
            log(f"IMDb GraphQL respondió con código {r.status_code}", level=xbmc.LOGWARNING)
            return imdb_id, None, None

        data = r.json()
        title_data = data.get("data", {}).get("title")
        if not title_data:
            log(f"No hay datos de título en GraphQL para ID: {imdb_id}")
            return imdb_id, None, None
            
        movie_title = title_data.get("titleText", {}).get("text", imdb_id)
        parents_guide = title_data.get("parentsGuide")
        
        # Procesar post-créditos
        crazy_credits_edges = title_data.get("crazyCredits", {}).get("edges", [])
        stinger_info = parse_crazy_credits(crazy_credits_edges)
        
        if not parents_guide:
            log(f"No hay guía parental en GraphQL para ID: {imdb_id}")
            return movie_title, {}, stinger_info

        categories = parents_guide.get("categories", [])
        if not categories:
            log(f"No hay categorías de guía parental en GraphQL para ID: {imdb_id}")
            return movie_title, {}, stinger_info

    except Exception as err:
        log(f"Error al conectar con IMDb GraphQL: {err}", level=xbmc.LOGERROR)
        if not silent:
            xbmcgui.Dialog().ok(get_string("error_title"), get_string("error_connect").format(error=err))
        return None, None, None

    if progress_dialog:
        progress_dialog.update(70, get_string("processing"))

    # Map GraphQL category IDs to our internal category keys
    # GraphQL category IDs: NUDITY, VIOLENCE, PROFANITY, ALCOHOL, FRIGHTENING
    graphql_cat_map = {
        "NUDITY": "SEXUAL_CONTENT",
        "VIOLENCE": "VIOLENCE",
        "PROFANITY": "PROFANITY",
        "ALCOHOL": "ALCOHOL_DRUGS",
        "FRIGHTENING": "FRIGHTENING_INTENSE_SCENES"
    }

    # Indexar por categoría interna
    entries_by_cat = {}
    for cat_item in categories:
        g_cat_id = cat_item.get("category", {}).get("id")
        internal_key = graphql_cat_map.get(g_cat_id)
        if internal_key:
            entries_by_cat[internal_key] = cat_item

    parental_guide = []
    for cat_key in CATEGORY_ORDER:
        cat_label = get_string(cat_key)
        entry = entries_by_cat.get(cat_key)

        if entry and entry.get("severity"):
            # GraphQL severity.id can be: noneVotes, mildVotes, moderateVotes, severeVotes
            sev_id = entry["severity"].get("id", "noneVotes")
            # Map severity.id back to clean level: none, mild, moderate, severe
            level = sev_id.replace("Votes", "")
            sev_text, sev_class = severity_to_class(level)
        else:
            sev_text, sev_class = get_string("none"), "noneVotes"

        parental_guide.append({
            'category':      cat_label,
            'raw_category':  cat_key,
            'severity_text': sev_text,
            'severity_class': sev_class,
            'item_list':     []
        })

    if progress_dialog:
        progress_dialog.update(95, get_string("finalizing"))

    return movie_title, parental_guide, stinger_info


def process_id(imdb_id):
    """Lógica de carga y visualización formateada para búsqueda manual."""
    log(f"Procesando ID manualmente: {imdb_id}")

    pDialog = xbmcgui.DialogProgress()
    pDialog.create(get_string("title"), get_string("connecting"))

    res = get_parental_guide(imdb_id, pDialog)
    if res and len(res) == 3:
        movie_title, guide, stinger_info = res
    elif res and len(res) == 2:
        movie_title, guide = res
        stinger_info = None
    else:
        movie_title, guide, stinger_info = None, None, None

    if not pDialog.iscanceled():
        pDialog.update(100, get_string("finalizing"))
        pDialog.close()

    if movie_title is not None and guide is not None:
        if guide:
            text = ""
            for item in guide:
                visual_info = SEVERITY_VISUALS.get(item['severity_class'], {'color': 'white', 'char': ' '})
                color = visual_info['color']
                text += f"{visual_info['char']} [B]{item['category']}:[/B] [COLOR={color}]{item['severity_text']}[/COLOR]\n\n"

            if stinger_info and stinger_info.get('stinger_text'):
                color_map = {
                    "FF00E5FF": "cyan",
                    "FFF1C40F": "yellow",
                    "FF7F8C8D": "grey"
                }
                c_name = color_map.get(stinger_info.get('stinger_color', ''), 'white')
                text += f"🎬 [B]{get_string('post_credits')}:[/B] [COLOR={c_name}]{stinger_info['stinger_text']}[/COLOR]\n"

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