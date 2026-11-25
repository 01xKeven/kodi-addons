#!/usr/bin/python
# -*- coding: utf-8 -*-
import jurialmunkey.scache
from tmdbhelper.lib.addon.logger import kodi_log
from tmdbhelper.lib.addon.plugin import get_setting
from tmdbhelper.lib.files.futils import FileUtils
from tmdbhelper.lib.files.dbdata import DATABASE_NAME


class SimpleCache(jurialmunkey.scache.SimpleCache):
    _basefolder = get_setting('cache_location', 'str') or ''
    _fileutils = FileUtils()  # Import to use plugin addon_data folder not the module one
    _home_window = None
    _is_initializing = False

    @staticmethod
    def kodi_log(msg, level=0):
        kodi_log(msg, level)

    def __init__(self, folder=None, filename=None):
        folder = folder or DATABASE_NAME
        self._basefolder = self._basefolder
        self._fileutils = self._fileutils
        self._db_file = self._fileutils.get_file_path(
            f'{self._basefolder}{folder}',
            filename or 'defaultcache.db',
            join_addon_data=(f'{self._basefolder}{folder}' == folder)
        )
        from hashlib import sha256
        database_id = sha256(self._db_file.encode('utf-8')).hexdigest()
        self._sc_name = f'{folder}_{filename or "defaultcache.db"}_simplecache_{database_id}'
        self.check_cleanup()

    @property
    def _window_home(self):
        if SimpleCache._home_window is None:
            from xbmcgui import Window
            SimpleCache._home_window = Window(10000)
        return SimpleCache._home_window

    def get_window_property(self, name):
        return self._window_home.getProperty(name)

    def set_window_property(self, name, value):
        return self._window_home.setProperty(name, value)

    def del_window_property(self, name):
        return self._window_home.clearProperty(name)

    def _set_pragmas(self, connection):
        connection.execute("PRAGMA synchronous=NORMAL")
        if self._is_initializing:
            connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA cache_size=-8000")
        connection.execute("PRAGMA mmap_size=134217728")
        return connection

    def _init_database(self):
        from jurialmunkey.locker import MutexPropLock
        from jurialmunkey.tmdate import set_timestamp
        with MutexPropLock(f'{self._db_file}.lockfile', kodi_log=self.kodi_log):
            if self.get_window_property(f'{self._sc_name}.clean.lastexecuted'):
                return
            self._is_initializing = True
            try:
                self._create_database()
            finally:
                self._is_initializing = False
            cur_time = set_timestamp(0, True)
            self.set_window_property(f'{self._sc_name}.clean.lastexecuted', str(cur_time - self._auto_clean_interval + 600))

    def check_cleanup(self):
        from jurialmunkey.tmdate import set_timestamp
        lastexecuted = self.get_window_property(f'{self._sc_name}.clean.lastexecuted')
        if not lastexecuted:
            self._init_database()
            return
        # Defer heavy cleanup/VACUUM while Kodi Home & startup widgets are still loading
        if not self.get_window_property('ServiceStarted'):
            return
        cur_time = set_timestamp(0, True)
        if (int(lastexecuted) + self._auto_clean_interval) < cur_time:
            self._do_cleanup()
