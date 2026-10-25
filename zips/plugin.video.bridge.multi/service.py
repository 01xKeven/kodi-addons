# -*- coding: utf-8 -*-
import xbmc
import xbmcaddon
import os

def run_service():
    try:
        import xbmcgui, xbmcvfs
        xbmcgui.Window(10000).setProperty('BridgeMulti.SearchProcessRunning', 'false')
        xbmcgui.Window(10000).clearProperty('BridgeMulti.SearchProcessStartTime')
        xbmcgui.Window(10000).clearProperty('BridgeMulti.SearchProcessTitle')
        b_path = xbmcvfs.translatePath('special://userdata/addon_data/plugin.video.bridge.multi/')
        l_file = os.path.join(b_path, 'bridge_search.lock')
        if os.path.exists(l_file): os.remove(l_file)
    except Exception:
        pass
    try:
        import xbmcgui
        xbmcgui.Window(10000).setProperty('BridgeMulti.SearchProcessRunning', 'false')
        xbmcgui.Window(10000).clearProperty('BridgeMulti.SearchProcessStartTime')
        xbmcgui.Window(10000).clearProperty('BridgeMulti.SearchProcessTitle')
    except Exception:
        pass
    try:
        import default
        if hasattr(default, 'check_and_run_migration'):
            default.check_and_run_migration()
        if hasattr(default, 'check_orphan_players'):
            default.check_orphan_players()
    except Exception as e:
        xbmc.log("Multi Bridge Service: Error running startup migration: " + str(e), xbmc.LOGWARNING)

if __name__ == '__main__':
    run_service()
