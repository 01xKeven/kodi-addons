import sys
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
}

FALLBACK_STRINGS = {
    'es': {
        'SEXUAL_CONTENT': "Desnudos",
        'VIOLENCE': "Violencia",
        'PROFANITY': "Groserias",
        'ALCOHOL_DRUGS': "Alcohol/Drogas",
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
        'keyboard_prompt': "Introduce el ID de IMDb (ttxxxxxxx)"
    },
    'en': {
        'SEXUAL_CONTENT': "Nudity",
        'VIOLENCE': "Violence",
        'PROFANITY': "Profanity",
        'ALCOHOL_DRUGS': "Alcohol/Drugs",
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
        'keyboard_prompt': "Enter IMDb ID (ttxxxxxxx)"
    }
}

def get_language():
    try:
        lang = xbmc.getLanguage(xbmc.ISO_639_1)
        if lang and lang.lower().startswith('es'):
            return 'es'
    except Exception:
        pass
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
    # Fallback si getLocalizedString devuelve vacío o falla (p.ej. antes de reiniciar Kodi)
    lang = get_language()
    return FALLBACK_STRINGS[lang].get(key, str(key))

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

def get_parental_guide(imdb_id, progress_dialog=None, silent=False, media_type=None):
    """
    Obtiene la guía parental directamente de IMDb usando la API de GraphQL.
    Devuelve (title, parental_guide_list) o (None, None) en caso de error.
    """
    if not imdb_id or not imdb_id.startswith('tt') or not imdb_id[2:].isdigit():
        log(f"ID de IMDb inválido: {imdb_id}", level=xbmc.LOGERROR)
        if not silent:
            xbmcgui.Dialog().ok(get_string("error_title"), get_string("error_format"))
        return None, None

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
        r = requests.post(url, json={'query': query}, headers=headers, timeout=12)

        if r.status_code != 200:
            log(f"IMDb GraphQL respondió con código {r.status_code}", level=xbmc.LOGWARNING)
            return imdb_id, None

        data = r.json()
        title_data = data.get("data", {}).get("title")
        if not title_data:
            log(f"No hay datos de título en GraphQL para ID: {imdb_id}")
            return imdb_id, None
            
        movie_title = title_data.get("titleText", {}).get("text", imdb_id)
        parents_guide = title_data.get("parentsGuide")
        if not parents_guide:
            log(f"No hay guía parental en GraphQL para ID: {imdb_id}")
            return movie_title, {}

        categories = parents_guide.get("categories", [])
        if not categories:
            log(f"No hay categorías de guía parental en GraphQL para ID: {imdb_id}")
            return movie_title, {}

    except Exception as err:
        log(f"Error al conectar con IMDb GraphQL: {err}", level=xbmc.LOGERROR)
        if not silent:
            xbmcgui.Dialog().ok(get_string("error_title"), get_string("error_connect").format(error=err))
        return None, None

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

    return movie_title, parental_guide


def process_id(imdb_id):
    """Lógica de carga y visualización formateada para búsqueda manual."""
    log(f"Procesando ID manualmente: {imdb_id}")

    pDialog = xbmcgui.DialogProgress()
    pDialog.create(get_string("title"), get_string("connecting"))

    movie_title, guide = get_parental_guide(imdb_id, pDialog)

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