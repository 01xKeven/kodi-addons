from tmdbhelper.lib.addon.plugin import get_setting
from jurialmunkey.window import get_property, wait_for_property
from tmdbhelper.lib.monitor.listitemtools import ListItemMonitorFunctions
from tmdbhelper.lib.monitor.cronjob import CronJobMonitor
from tmdbhelper.lib.monitor.player import PlayerMonitor
from tmdbhelper.lib.monitor.update import UpdateMonitor
from tmdbhelper.lib.monitor.imgmon import ImagesMonitor
from tmdbhelper.lib.monitor.poller import Poller, POLL_MIN_INCREMENT, POLL_MAX_INCREMENT
from tmdbhelper.lib.addon.thread import SafeThread
from threading import Lock


class ServiceMonitor(Poller):
    def __init__(self):
        self.exit = False
        self.listitem = None

    def run(self):
        self.mutex_lock = Lock()

        self.update_monitor = UpdateMonitor()
        self.player_monitor = PlayerMonitor()

        get_property('TMDbHelper.StartupSyncDone', clear_property=True)
        get_property('TMDbHelper.StartupSyncInProgress', 'True')
        self.run_cron_job()
        self.run_images_monitor()

        self.listitem_funcs = ListItemMonitorFunctions(self)

        get_property('ServiceStarted', 'True')

        self.poller()

    def run_cron_job(self):
        self.cron_job = CronJobMonitor(self, update_hour=get_setting('library_autoupdate_hour', 'int'))
        self.cron_job.setName('Cron Thread')
        self.cron_job.start()

    def run_images_monitor(self):
        self.images_monitor = ImagesMonitor(self)
        self.images_monitor.setName('Image Thread')
        self.images_monitor.start()

    def _on_listitem(self):
        self.listitem_funcs.on_listitem()
        self._on_idle(POLL_MIN_INCREMENT)

    def _on_scroll(self):
        self.listitem_funcs.on_scroll()
        self._on_idle(POLL_MIN_INCREMENT)

    def _on_fullscreen(self):
        self.player_monitor.on_fullscreen()
        self._on_idle(POLL_MAX_INCREMENT)

    def _on_context(self):
        self.listitem_funcs.on_context_listitem()
        self._on_idle(POLL_MIN_INCREMENT)

    def _on_clear(self):
        """
        IF we've got properties to clear lets clear them and then jump back in the loop
        """
        self.listitem_funcs.reset_current_item()  # Reset current item so that it will retrigger lookup on return to previous window
        self.listitem_funcs.clear_properties()
        self._on_idle(POLL_MIN_INCREMENT)

    def _on_exit(self):
        try:
            self.cron_job.exit = True
        except AttributeError:
            pass
        try:
            self.images_monitor.exit = True
        except AttributeError:
            pass
        for th_attr in ('cron_job', 'images_monitor'):
            try:
                th = getattr(self, th_attr, None)
                if th and th.is_alive():
                    th.join(timeout=0.5)
            except Exception:
                pass
        if not self.update_monitor.abortRequested():
            get_property('ServiceStarted', clear_property=True)
            get_property('ServiceStop', clear_property=True)
        else:
            try:
                import tmdbhelper.lib.addon.plugin as _p_mod
                _p_mod.ADDON = None
                if hasattr(_p_mod, 'KODIPLUGIN') and _p_mod.KODIPLUGIN is not None:
                    _p_mod.KODIPLUGIN._addon = None
            except Exception:
                pass
            for attr in ('listitem_funcs', 'player_monitor', 'update_monitor', 'cron_job', 'images_monitor'):
                try:
                    setattr(self, attr, None)
                except Exception:
                    pass


def restart_service_monitor():
    if get_property('ServiceStarted') == 'True':
        wait_for_property('ServiceStop', value='True', set_property=True)  # Stop service
    wait_for_property('ServiceStop', value=None)  # Wait until Service clears property
    SafeThread(target=ServiceMonitor().run).start()
