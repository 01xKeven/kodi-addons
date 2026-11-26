from tmdbhelper.lib.addon.thread import SafeThread
from jurialmunkey.ftools import cached_property


CRONJOB_POLL_TIME = 600


class CronJobMonitor(SafeThread):

    _poll_time = CRONJOB_POLL_TIME

    def __init__(self, parent, update_hour=0):
        SafeThread.__init__(self)
        self.daemon = True
        self.exit = False
        self.update_hour = update_hour
        self.update_monitor = parent.update_monitor

    def _on_startup(self):
        from jurialmunkey.window import get_property
        try:
            if self.update_monitor.abortRequested() or self.exit:
                return
            self._do_trakt_authorization()
        finally:
            get_property('TMDbHelper.StartupSyncDone', 'True')
            get_property('TMDbHelper.StartupSyncInProgress', clear_property=True)
        if self.update_monitor.abortRequested() or self.exit:
            return
        self._do_recache_kodidb()

    def _on_poll(self):
        if self.update_monitor.abortRequested() or self.exit:
            return
        self._do_database_maintenance()
        if self.update_monitor.abortRequested() or self.exit:
            return
        self._do_delete_old_databases()
        if self.update_monitor.abortRequested() or self.exit:
            return
        self._do_library_update_check()
        if self.update_monitor.abortRequested() or self.exit:
            return
        self._do_delete_old_log_files()

    @cached_property
    def trakt_api(self):
        from tmdbhelper.lib.api.trakt.api import TraktAPI
        return TraktAPI()

    @cached_property
    def database_maintenance(self):
        from tmdbhelper.lib.script.method.maintenance import DatabaseMaintenance
        return DatabaseMaintenance()

    def _do_database_maintenance(self):
        if self.update_monitor.abortRequested() or self.exit:
            return
        self.database_maintenance.vacuum()

    def _do_delete_old_databases(self):
        if self.update_monitor.abortRequested() or self.exit:
            return
        self.database_maintenance.delete_legacy_folders()

    def _do_recache_kodidb(self):
        if self.update_monitor.abortRequested() or self.exit:
            return
        from tmdbhelper.lib.addon.plugin import get_setting
        if not get_setting('enable_kodi_library', 'bool'):
            return
        if not get_setting('recache_kodidb_on_startup', 'bool'):
            return
        self.database_maintenance.recache_kodidb(notification=False)

    def _do_delete_old_log_files_in_folder(self, folder, regex, days=-7):
        if self.update_monitor.abortRequested() or self.exit:
            return
        from tmdbhelper.lib.files.futils import get_files_in_folder, get_write_path, delete_file
        from tmdbhelper.lib.addon.tmdate import get_todays_date
        import xbmcvfs
        path = get_write_path(folder, make_dir=False)
        if not xbmcvfs.exists(path):
            return
        date = get_todays_date(str_fmt="%Y-%m-%d", days=days)
        for file in get_files_in_folder(path, regex=regex):
            if self.update_monitor.abortRequested() or self.exit:
                break
            if file > date:
                continue
            delete_file(folder, file)

    def _do_delete_old_log_files(self):
        if self.update_monitor.abortRequested() or self.exit:
            return
        for folder, regex in (
            ('log_tagger', r".*\.json$"),
            ('log_library', r".*\.json$"),
            ('timer_report', r".*\.txt$"),
        ):
            if self.update_monitor.abortRequested() or self.exit:
                break
            self._do_delete_old_log_files_in_folder(folder, regex)

    def _do_trakt_authorization(self):
        if self.update_monitor.abortRequested() or self.exit:
            return
        from jurialmunkey.window import get_property
        self.trakt_api.authorize()
        if self.update_monitor.abortRequested() or self.exit:
            return
        if not get_property('TraktIsAuth', is_type=float):
            return
        try:
            from tmdbhelper.lib.addon.plugin import get_setting
            from tmdbhelper.lib.sync.datasync import SyncData
            from tmdbhelper.lib.sync.activity import SyncLastActivities
            sd = SyncData(trakt_api=self.trakt_api)
            _ = SyncLastActivities(sd).json
            if get_setting('trakt_watchedindicators') and not self.update_monitor.waitForAbort(4):
                sd.sync('movie', ('plays', 'playback_progress'))
                sd.sync('show', ('plays', 'watched_episodes', 'aired_episodes', 'playback_progress'))
        except Exception:
            pass
        if self.update_monitor.abortRequested() or self.exit:
            return
        from tmdbhelper.lib.script.method.trakt import get_stats
        get_stats()

    def _do_library_update(self):
        if self.update_monitor.abortRequested() or self.exit:
            return
        from tmdbhelper.lib.addon.plugin import executebuiltin
        from tmdbhelper.lib.addon.tmdate import get_datetime_now, get_timedelta
        executebuiltin('RunScript(plugin.video.themoviedb.helper,library_autoupdate)')
        executebuiltin(f'Skin.SetString(TMDbHelper.AutoUpdate.LastTime,{get_datetime_now().strftime("%Y-%m-%dT%H:%M:%S")})')
        self.library_update_next += get_timedelta(hours=24)  # Set next update for tomorrow

    def _do_library_update_check(self):
        if self.update_monitor.abortRequested() or self.exit:
            return
        from jurialmunkey.parser import try_int
        from tmdbhelper.lib.addon.tmdate import convert_timestamp, get_datetime_now, get_timedelta, get_datetime_today, get_datetime_time, get_datetime_combine
        from tmdbhelper.lib.addon.plugin import get_setting, get_infolabel
        if not get_setting('library_autoupdate'):
            return
        self.library_update_next = get_datetime_combine(get_datetime_today(), get_datetime_time(try_int(self.update_hour)))
        self.library_update_last = get_infolabel('Skin.String(TMDbHelper.AutoUpdate.LastTime)')
        self.library_update_last = convert_timestamp(self.library_update_last) if self.library_update_last else None

        # If we've already updated the library today then set a new next update time for tomorrow
        if self.library_update_last and self.library_update_last > self.library_update_next:
            self.library_update_next += get_timedelta(hours=24)

        # If the next update timestamp has elapsed then we should update the library
        if get_datetime_now() > self.library_update_next:
            self._do_library_update()

    def run(self):
        # Allow Kodi Home & initial widgets to boot with zero CPU/SQLite/network contention
        if self.update_monitor.waitForAbort(4) or self.exit:
            return
        self._on_startup()

        while not self.update_monitor.abortRequested() and not self.exit:
            if self.update_monitor.waitForAbort(self._poll_time):
                break
            if self.update_monitor.abortRequested() or self.exit:
                break
            self._on_poll()
