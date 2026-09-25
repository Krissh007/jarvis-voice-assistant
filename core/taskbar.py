"""Taskbar identity: JARVIS's window groups under its pinned taskbar icon.

Windows decides which taskbar button a window belongs to by its AppUserModelID. Python's windows
would otherwise count as "pythonw.exe", so a pinned JARVIS shortcut and the running JARVIS window
would show as two separate buttons. JARVIS sets one ID on its own process and writes the same ID
into its desktop icon, so a pin made from that icon groups with the window: minimising goes to
the pinned icon and clicking it brings the window back.

Never edit a shortcut that is already pinned (the files in the "User Pinned/TaskBar" folder): Windows
treats a changed pin as a different app and silently unpins it. Tag the desktop icon, then pin it.
"""

from __future__ import annotations

import ctypes
import functools
import logging
from ctypes import POINTER, Structure, byref, c_ulong, c_ushort, c_void_p, c_wchar_p
from pathlib import Path

log = logging.getLogger(__name__)

APP_ID = "JARVIS.Desktop.Assistant"  # "JARVIS.VoiceAssistant" was retired: Windows blocked it after an unpin
STGM_READWRITE = 0x2
VT_LPWSTR = 31


def set_process_app_id(app_id: str = APP_ID) -> None:
    """Call before any window exists."""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(c_wchar_p(app_id))
    except Exception:
        log.exception("couldn't set the taskbar id")


@functools.cache  # the COM types must be defined once, or ctypes treats each copy as a different type
def _interfaces():
    import comtypes
    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

    class PROPERTYKEY(Structure):
        _fields_ = [("fmtid", GUID), ("pid", c_ulong)]

    class PROPVARIANT(Structure):  # just the VT_LPWSTR case; 24 bytes on 64-bit Windows
        _fields_ = [("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort), ("r3", c_ushort),
                    ("pwszVal", c_wchar_p), ("pad", c_void_p)]

    class IPropertyStore(IUnknown):
        _iid_ = GUID("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(c_ulong), "cProps")),
            COMMETHOD([], HRESULT, "GetAt", (["in"], c_ulong, "iProp"), (["out"], POINTER(PROPERTYKEY), "pkey")),
            COMMETHOD([], HRESULT, "GetValue", (["in"], POINTER(PROPERTYKEY), "key"),
                      (["out"], POINTER(PROPVARIANT), "pv")),
            COMMETHOD([], HRESULT, "SetValue", (["in"], POINTER(PROPERTYKEY), "key"),
                      (["in"], POINTER(PROPVARIANT), "propvar")),
            COMMETHOD([], HRESULT, "Commit"),
        ]

    key = PROPERTYKEY(GUID("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"), 5)  # PKEY_AppUserModel_ID
    return comtypes, PROPVARIANT, IPropertyStore, key


def _open(path: Path):
    import comtypes.client
    from comtypes.persist import IPersistFile
    from comtypes.shelllink import IShellLinkW, ShellLink

    comtypes, _, IPropertyStore, key = _interfaces()
    comtypes.CoInitialize()
    link = comtypes.client.CreateObject(ShellLink, interface=IShellLinkW)
    persist = link.QueryInterface(IPersistFile)
    persist.Load(str(path), STGM_READWRITE)
    return link, persist, link.QueryInterface(IPropertyStore), key


def shortcut_app_id(path: Path) -> str | None:
    _, _, store, key = _open(path)
    value = store.GetValue(byref(key))
    return value.pwszVal if value.vt == VT_LPWSTR else None


def set_shortcut_app_id(path: Path, app_id: str = APP_ID) -> bool:
    """Write the ID into a .lnk. True if it changed. Refuses pinned copies (Windows would unpin them)."""
    if is_pinned_copy(path):
        raise ValueError("won't edit a pinned taskbar shortcut: Windows would unpin it")
    _, PROPVARIANT, _, _ = _interfaces()
    _, persist, store, key = _open(path)
    current = store.GetValue(byref(key))
    if current.vt == VT_LPWSTR and current.pwszVal == app_id:
        return False
    value = PROPVARIANT()
    value.vt = VT_LPWSTR
    value.pwszVal = app_id
    store.SetValue(byref(key), byref(value))
    store.Commit()
    persist.Save(str(path), True)
    return True


def is_jarvis_shortcut(path: Path, root: Path) -> bool:
    """A shortcut that starts this JARVIS (the desktop icon or a pinned copy of it)."""
    try:
        link, _, _, _ = _open(path)
        return Path(link.GetPath(0)).resolve().is_relative_to(root.resolve())
    except Exception:
        return False


def is_pinned_copy(path: Path) -> bool:
    return "user pinned" in str(path).lower()


def tag_shortcut(lnk: Path, root: Path) -> bool:
    """Give a JARVIS desktop shortcut the taskbar ID (never a pinned copy). True if it changed."""
    if is_pinned_copy(lnk) or not lnk.exists() or not is_jarvis_shortcut(lnk, root):
        return False
    try:
        changed = set_shortcut_app_id(lnk)
    except Exception:
        log.exception("couldn't tag %s", lnk)
        return False
    if changed:
        log.info("taskbar id added to %s", lnk)
    return changed
