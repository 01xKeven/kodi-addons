import xbmc
import xbmcgui
import xbmcaddon
import xbmcvfs
import os
import json

def get_state_file():
    addon = xbmcaddon.Addon('plugin.video.balandro.bridge.multi')
    try:
        profile_dir = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
    except AttributeError:
        profile_dir = xbmc.translatePath(addon.getAddonInfo('profile'))
        
    return os.path.join(profile_dir, 'resume_state.json')

def check_resume_state():
    state_file = get_state_file()
    if not os.path.exists(state_file):
        return

    try:
        with open(state_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            
        title = data.get('title', 'Desconocido')
        url = data.get('url', '')
        
        if url:
            dialog = xbmcgui.Dialog()
            msg = "Kodi se cerró inesperadamente durante la reproducción de:\n[COLOR gold]%s[/COLOR]\n\n¿Deseas volver a buscar enlaces para este contenido?" % title
            yes = dialog.yesno("Balandro Bridge Multi - Reanudar", msg, yeslabel="Sí", nolabel="No")
            
            if yes:
                xbmc.executebuiltin('RunPlugin(%s)' % url)
                
    except Exception as e:
        xbmc.log('Balandro Bridge Multi: Error al leer estado de reanudacion - %s' % str(e), xbmc.LOGERROR)
    finally:
        # Siempre limpiar el archivo para no volver a preguntar en el proximo reinicio
        try:
            if os.path.exists(state_file):
                os.remove(state_file)
        except:
            pass

if __name__ == '__main__':
    monitor = xbmc.Monitor()
    
    # Esperar a que Kodi termine de cargar y estemos en la ventana principal
    while not monitor.abortRequested():
        if xbmc.getCondVisibility('Window.IsActive(home)') or xbmc.getCondVisibility('System.HasAddon(plugin.video.balandro.bridge.multi)'):
            break
        monitor.waitForAbort(1)
        
    # Darle un margen extra de 2 segundos para que la interfaz este totalmente lista y libre
    if not monitor.abortRequested():
        monitor.waitForAbort(2)
        check_resume_state()

    # Bucle de monitoreo de reproducción activa para borrar el estado al salir limpiamente
    was_playing = False
    stop_count = 0

    while not monitor.abortRequested():
        if monitor.waitForAbort(1):
            break

        state_file = get_state_file()
        if os.path.exists(state_file):
            if xbmc.Player().isPlaying():
                was_playing = True
                stop_count = 0
            else:
                if was_playing:
                    # El reproductor estuvo activo pero ahora se detuvo
                    stop_count += 1
                    if stop_count >= 5:  # 5 segundos de margen para transiciones y buffering
                        try:
                            if os.path.exists(state_file):
                                os.remove(state_file)
                        except:
                            pass
                        was_playing = False
                        stop_count = 0
        else:
            was_playing = False
            stop_count = 0
