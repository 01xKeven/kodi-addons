from xbmcgui import Dialog, INPUT_ALPHANUM
from tmdbhelper.lib.addon.plugin import get_localized, convert_type, PLUGINPATH
from jurialmunkey.ftools import cached_property
from jurialmunkey.parser import merge_two_dicts
from urllib.parse import urlencode

from tmdbhelper.lib.items.container import ContainerDefaultCacheDirectory
from tmdbhelper.lib.items.directories.lists_default import ItemCache


class ListGemini(ContainerDefaultCacheDirectory):

    cache_days = 0.25  # 6 hours default cache

    @cached_property
    def cache_name(self):
        return '_'.join(map(str, self.cache_name_tuple))

    @cached_property
    def cache_name_tuple(self):
        return (f'{self.__class__.__name__}', self.query, )

    @cached_property
    def gemini(self):
        from tmdbhelper.lib.api.gemini.api import Gemini
        return Gemini()

    @ItemCache('ItemContainer.db')
    def _fetch_cached(self):
        return self.get_prompt_items()

    def get_cached_response(self):
        data = self._fetch_cached()
        if not data:
            data = self.get_prompt_items()
        return data

    def get_prompt_items(self):
        from tmdbhelper.lib.addon.dialog import BusyDialog
        with BusyDialog():
            try:
                data = self.gemini.get_prompt_items(self.query)
            except Exception as e:
                from tmdbhelper.lib.addon.logger import kodi_log
                kodi_log(f'Gemini get_prompt_items exception: {e}', 1)
                data = None
        return data

    def get_items(self, query=None, **kwargs):
        if not self.gemini.api_key:
            Dialog().ok('Gemini', f"{get_localized(32150)}[CR]{get_localized(32151).format('https://aistudio.google.com/app/api-keys')}")
            return
        self.query = query or Dialog().input(get_localized(32044), type=INPUT_ALPHANUM)
        if not self.query:
            return
        items = self.get_cached_response()
        if not items:
            Dialog().notification('Gemini', 'No se encontraron resultados para la consulta', time=3500)
            return

        # Actualizar la ruta del contenedor para que al dar "Atrás" vuelva limpiamente al menú anterior
        if not kwargs.get('query'):
            params = merge_two_dicts(kwargs, {
                'info': 'gemini',
                'query': self.query,
                'update_listing': 'True'})
            self.container_update = f'{PLUGINPATH}?{urlencode(params)}'
            self.parent_params = params

        self.container_content = convert_type('both', 'container', items=items)
        self.plugin_name = f'Gemini - {self.query}'
        return items
