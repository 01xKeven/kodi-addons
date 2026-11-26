from jurialmunkey.modimp import importmodule

"""
ITEMMETA FACTORY

Mapping data from database in format configured for use in Kodi to pass to ListItem constructor

"""


def import_movie():
    from tmdbhelper.lib.items.database.itemmeta_factories.concrete_classes.movie import Movie
    return Movie


def import_tvshow():
    from tmdbhelper.lib.items.database.itemmeta_factories.concrete_classes.tvshow import Tvshow
    return Tvshow


def import_season():
    from tmdbhelper.lib.items.database.itemmeta_factories.concrete_classes.season import Season
    return Season


def import_episode():
    from tmdbhelper.lib.items.database.itemmeta_factories.concrete_classes.episode import Episode
    return Episode


def import_episode_group():
    from tmdbhelper.lib.items.database.itemmeta_factories.concrete_classes.episode_group import EpisodeGroup
    return EpisodeGroup


def import_episode_group_season():
    from tmdbhelper.lib.items.database.itemmeta_factories.concrete_classes.episode_group import EpisodeGroupSeason
    return EpisodeGroupSeason


def import_person():
    from tmdbhelper.lib.items.database.itemmeta_factories.concrete_classes.person import Person
    return Person


def import_set():
    from tmdbhelper.lib.items.database.itemmeta_factories.concrete_classes.series import Series
    return Series


FACTORY_ROUTES = {
    'movie': import_movie,
    'tvshow': import_tvshow,
    'season': import_season,
    'episode': import_episode,
    'episode_group': import_episode_group,
    'episode_group_season': import_episode_group_season,
    'person': import_person,
    'set': import_set,
}


_CLASS_CACHE = {}


def ItemMetaFactory(parent_db_cache, data):
    mediatype = parent_db_cache.mediatype
    cls = _CLASS_CACHE.get(mediatype)
    if cls is None:
        cls = _CLASS_CACHE[mediatype] = FACTORY_ROUTES[mediatype]()
    class_obj = cls(parent_db_cache)
    class_obj.mediatype = mediatype
    class_obj.data = data
    return class_obj
