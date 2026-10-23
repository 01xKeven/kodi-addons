# -*- coding: utf-8 -*-
import xbmc, xbmcaddon, xbmcgui, xbmcplugin, os, sys, xbmcvfs, glob
import shutil
import urllib
import re
import time
from resources.lib.modules.backtothefuture import PY2
from resources.lib.modules import maintenance

# Code to map the old translatePath
if PY2:
    translatePath = xbmc.translatePath
    loglevel = xbmc.LOGNOTICE
else:
    translatePath = xbmcvfs.translatePath
    loglevel = xbmc.LOGINFO

AddonID = 'script.ezmaintenanceplus'
packagesdir = translatePath(os.path.join('special://home/addons/packages', ''))
thumbnails = translatePath(os.path.join('special://home/userdata/Thumbnails', ''))
dialog = xbmcgui.Dialog()
setting = xbmcaddon.Addon().getSetting
iconpath = translatePath(os.path.join('special://home/addons/' + AddonID, 'icon.png'))


def log(message):
    xbmc.log('ezmaintenanceplus: %s' % message, level=loglevel)


def toInt(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def folderStats(path):
    """Devuelve (tamaño en bytes, número de archivos .zip) de una carpeta."""
    total = 0
    zips = 0
    for dirpath, dirnames, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                total += os.path.getsize(fp)
            except (OSError, IOError):
                pass
            if f.lower().endswith('.zip'):
                zips += 1
    return total, zips


def toMB(size):
    return size / 1024.0 / 1024.0


def notify(message):
    try:
        dialog.notification('Mantenimiento+', message, iconpath, 7000)
    except Exception:
        xbmc.executebuiltin('Notification(%s, %s, %s, %s)' % ('Mantenimiento+', message, '7000', iconpath))


def startupMaintenance():
    """Limpieza automática al iniciar Kodi, sin preguntar nada al usuario."""
    notifyMode = setting('notify_mode') == 'true'
    autoClean = setting('startup.cache') == 'true'
    filesize = toInt(setting('filesize_alert'), 200)
    filesizeThumb = toInt(setting('filesizethumb_alert'), 500)
    maxPackages = toInt(setting('packagenumbers_alert'), 50)

    pkgSize, pkgZips = folderStats(packagesdir)
    thumbSize, _ = folderStats(thumbnails)

    actions = []

    # Paquetes: se limpia solo si supera el límite de MB o de número de zip.
    if pkgSize > filesize * 1024 * 1024 or pkgZips > maxPackages:
        freed = maintenance.purgePackages(mode='silent')
        if not freed:
            freed = toMB(pkgSize)
        actions.append('Paquetes: %.0f MB liberados' % freed)
        log('limpieza automática de paquetes: %.2f MB liberados' % freed)

    # Miniaturas: se limpia solo si supera el límite de MB.
    if thumbSize > filesizeThumb * 1024 * 1024:
        freed = maintenance.deleteThumbnails(mode='silent')
        if not freed:
            freed = toMB(thumbSize)
        actions.append('Miniaturas: %.0f MB liberadas' % freed)
        log('limpieza automática de miniaturas: %.2f MB liberadas' % freed)

    # Caché temporal: solo si la opción está activada en ajustes.
    if autoClean:
        freed = maintenance.clearCache(mode='silent') or 0.0
        if freed >= 1:
            actions.append('Caché: %.0f MB liberados' % freed)
            log('limpieza automática de caché: %.2f MB liberados' % freed)

    pkgSize, _ = folderStats(packagesdir)
    thumbSize, _ = folderStats(thumbnails)
    stats = 'Paquetes: %.0f MB - Miniaturas: %.0f MB' % (toMB(pkgSize), toMB(thumbSize))

    # Solo se notifica si de verdad se ha limpiado algo.
    if actions:
        notify('Limpieza automática: ' + ' | '.join(actions))
    elif notifyMode:
        notify(stats)


maintenance.logMaintenance("Service started")


class Monitor(xbmc.Monitor):

    def __init__(self):
        xbmc.Monitor.__init__(self)
        maintenance.logMaintenance("Monitor init")
        maintenance.determineNextMaintenance()

    def onSettingsChanged(self):
        maintenance.logMaintenance("onSettingsChanged")
        maintenance.determineNextMaintenance()


if __name__ == '__main__':

    monitor = Monitor()

    # Se espera un poco a que Kodi termine de arrancar antes de tocar archivos.
    if not monitor.waitForAbort(5):
        startupMaintenance()

    while not monitor.abortRequested():
        # Sleep/wait for abort for 10 seconds
        if monitor.waitForAbort(10):
            # Abort was requested while waiting. We should exit
            break
        maintenance.logMaintenance("monitor loop")
        if not xbmc.Player().isPlayingVideo():
            nextMaintenance = maintenance.getNextMaintenance()
            maintenance.logMaintenance("time.time() = %s, nextMaintenance = %s" % (str(time.time()), str(nextMaintenance)))
            if nextMaintenance > 0 and time.time() >= nextMaintenance:
                log("AutoClean started")
                freed = maintenance.clearCache(mode='silent') or 0.0
                log("AutoClean done")
                if freed >= 1:
                    notify('Limpieza automática: Caché: %.0f MB liberados' % freed)
                maintenance.determineNextMaintenance()

    del monitor
