from jurialmunkey.ftools import cached_property
from tmdbhelper.lib.sync.auth import SyncAuthorisation


class SyncPlayData(SyncAuthorisation):
    def __init__(self, pauseplayprogress=False, watchedindicators=False, container=None, **kwargs):
        super().__init__('sync_source_watched', **kwargs)
        self._pauseplayprogress = pauseplayprogress  # Set play progress using paused at position
        self._watchedindicators = watchedindicators  # Set watched status and playcount
        self._container = container

    @cached_property
    def trakt_api(self):
        if self._container is not None:
            return self._container.trakt_api
        return super().trakt_api

    @cached_property
    def mdblist_api(self):
        if self._container is not None:
            return self._container.mdblist_api
        return super().mdblist_api

    @property
    def is_enabled(self):
        if not self._watchedindicators and not self._pauseplayprogress:
            return False
        if not self.is_authorized:
            return False
        return bool(self.syncdata)

    def is_sync(func):
        def wrapper(self, *args, **kwargs):
            if not self.is_enabled:
                return
            return func(self, *args, **kwargs)
        return wrapper

    @cached_property
    def syncdata(self):
        from tmdbhelper.lib.sync.datasync import SyncDataFactory
        return SyncDataFactory(self)

    @is_sync
    def pre_sync(self, info=None, tmdb_type=None, **kwargs):
        info_movies = ('stars_in_movies', 'crew_in_movies', 'trakt_userlist', 'stars_in_both', 'crew_in_both',)
        if tmdb_type in ('movie', 'both',) or info in info_movies:
            if self._watchedindicators:
                self.syncdata.sync('movie', ('plays', ))
            if self._pauseplayprogress:
                self.syncdata.sync('movie', ('playback_progress', ))

        info_tvshow = ('stars_in_tvshows', 'crew_in_tvshows', 'trakt_userlist', 'trakt_calendar', 'stars_in_both', 'crew_in_both', 'specified_episodes')
        if tmdb_type in ('tv', 'season', 'both',) or info in info_tvshow:
            if self._watchedindicators:
                self.syncdata.sync('show', ('plays', 'watched_episodes', 'aired_episodes', ))
            if self._pauseplayprogress:
                self.syncdata.sync('show', ('playback_progress', ))

    _NON_MEDIA_INFOS = frozenset((
        'genres', 'years', 'countries', 'networks', 'studios',
        'trakt_genres', 'trakt_years', 'dir_search', 'dir_tvdb_genres',
        'random_genres', 'random_providers', 'random_networks',
        'random_studios', 'random_keywords', 'all_items', 'search',
        'dir_multisearch',
    ))

    def pre_sync_start(self, info=None, tmdb_type=None, **kwargs):
        if not self._watchedindicators and not self._pauseplayprogress:
            return
        if info in self._NON_MEDIA_INFOS:
            return
        from jurialmunkey.window import get_property
        if not get_property('TMDbHelper.StartupSyncDone') and get_property('ServiceStop') != 'True' and (
            get_property('TMDbHelper.StartupSyncInProgress') == 'True' or not get_property('ServiceStarted')
        ):
            return
        info_movies = ('stars_in_movies', 'crew_in_movies', 'trakt_userlist', 'stars_in_both', 'crew_in_both',)
        info_tvshow = ('stars_in_tvshows', 'crew_in_tvshows', 'trakt_userlist', 'trakt_calendar', 'stars_in_both', 'crew_in_both', 'specified_episodes')
        needs_movie = (tmdb_type in ('movie', 'both',) or info in info_movies)
        needs_show = (tmdb_type in ('tv', 'season', 'both',) or info in info_tvshow)
        if not needs_movie and not needs_show:
            return
        if not self.is_enabled:
            return
        from tmdbhelper.lib.addon.thread import SafeThread
        self._pre_sync = SafeThread(target=self.pre_sync, kwargs={'info': info, 'tmdb_type': tmdb_type, **kwargs})
        self._pre_sync.start()

    def pre_sync_join(self):
        thread = getattr(self, '_pre_sync', None)
        if thread is None:
            return
        thread.join()
