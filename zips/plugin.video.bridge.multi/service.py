# -*- coding: utf-8 -*-
import xbmc
import xbmcaddon
import os

def run_service():
    try:
        import default
        if hasattr(default, 'check_and_run_migration'):
            default.check_and_run_migration()
    except Exception as e:
        xbmc.log("Bridge Multi Service: Error running startup migration: " + str(e), xbmc.LOGWARNING)

if __name__ == '__main__':
    run_service()
