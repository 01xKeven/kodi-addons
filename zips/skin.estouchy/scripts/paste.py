# -*- coding: utf-8 -*-
import sys
import os
import json
import time

try:
    import xbmc
    import xbmcgui
    IN_KODI = True
except ImportError:
    IN_KODI = False

def get_windows_clipboard_ctypes():
    """64-bit safe Windows ctypes clipboard reader"""
    text = ""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        user32.OpenClipboard.restype = ctypes.c_bool
        user32.GetClipboardData.argtypes = [ctypes.c_uint]
        user32.GetClipboardData.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.restype = ctypes.c_bool
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = ctypes.c_bool

        for _ in range(6):
            if user32.OpenClipboard(None):
                try:
                    # 13 = CF_UNICODETEXT
                    if user32.IsClipboardFormatAvailable(13):
                        h_data = user32.GetClipboardData(13)
                        if h_data:
                            p_data = kernel32.GlobalLock(h_data)
                            if p_data:
                                text = ctypes.c_wchar_p(p_data).value
                                kernel32.GlobalUnlock(h_data)
                    elif user32.IsClipboardFormatAvailable(1):  # 1 = CF_TEXT
                        h_data = user32.GetClipboardData(1)
                        if h_data:
                            p_data = kernel32.GlobalLock(h_data)
                            if p_data:
                                raw = ctypes.c_char_p(p_data).value
                                if raw:
                                    text = raw.decode('utf-8', errors='ignore')
                                kernel32.GlobalUnlock(h_data)
                finally:
                    user32.CloseClipboard()
                if text:
                    return text
            time.sleep(0.04)
    except Exception:
        pass
    return text

def get_windows_clipboard_powershell():
    """PowerShell fallback for Windows"""
    try:
        import subprocess
        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        p = subprocess.run(
            ['powershell', '-NoProfile', '-Command', 'Get-Clipboard'],
            capture_output=True,
            text=True,
            timeout=2,
            startupinfo=startupinfo
        )
        return p.stdout.strip()
    except Exception:
        return ""

def get_tkinter_clipboard():
    """Tkinter fallback for Linux / macOS"""
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        text = root.clipboard_get()
        root.destroy()
        return text
    except Exception:
        return ""

def get_android_clipboard():
    """Android clipboard reader via Pyjnius"""
    try:
        from jnius import autoclass
        PythonActivity = autoclass('org.kodi.kodi.Splash')
        activity = PythonActivity.mActivity
        Context = autoclass('android.content.Context')
        clipboard = activity.getSystemService(Context.CLIPBOARD_SERVICE)
        if clipboard.hasPrimaryClip():
            clipData = clipboard.getPrimaryClip()
            item = clipData.getItemAt(0)
            text = str(item.getText())
            return text
    except Exception:
        return ""

def get_clipboard():
    # 1. Try Windows 64-bit safe ctypes first (instant 0ms)
    if os.name == 'nt' or sys.platform.startswith('win'):
        text = get_windows_clipboard_ctypes()
        if text:
            return text
        # 2. Windows PowerShell fallback
        text = get_windows_clipboard_powershell()
        if text:
            return text

    # 3. Android fallback
    text = get_android_clipboard()
    if text:
        return text

    # 4. Tkinter fallback (Linux / macOS)
    text = get_tkinter_clipboard()
    if text:
        return text

    return ""

def main():
    text = get_clipboard()
    if text:
        if IN_KODI:
            # Send text into active Virtual Keyboard via JSON-RPC Input.SendText
            payload = json.dumps({
                "jsonrpc": "2.0",
                "method": "Input.SendText",
                "params": {
                    "text": text,
                    "done": False
                },
                "id": 1
            })
            xbmc.executeJSONRPC(payload)
    elif IN_KODI:
        xbmcgui.Dialog().notification("Portapapeles", "No hay texto en el portapapeles", xbmcgui.NOTIFICATION_INFO, 2000)

if __name__ == '__main__':
    main()
