"""Subprocess probe: open FrameGuard's menu-bar menu the way macOS 27 does.

macOS 27 opens status-item menus from a scene action, so NSApp.currentEvent
is not a mouse event when NSMenuDidBeginTrackingNotification is posted. Qt's
QStatusItemDelegate then calls -[NSEvent clickCount] on it, which throws an
uncaught NSInternalInconsistencyException and aborts the process.

This probe reproduces exactly that sequence. Exit codes: 0 survived (prints
SURVIVED), 3 no status item available (caller skips), anything else crashed.
Run with QT_QPA_PLATFORM=cocoa. Pass "noguard" to skip the fix.
"""
import ctypes
import ctypes.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtGui import QColor, QIcon, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon  # noqa: E402

app = QApplication(sys.argv[:1])
if not QSystemTrayIcon.isSystemTrayAvailable():
    print("NO_STATUS_ITEM: QSystemTrayIcon.isSystemTrayAvailable() is False", flush=True)
    os._exit(3)
pixmap = QPixmap(32, 32)
pixmap.fill(QColor("red"))
tray = QSystemTrayIcon(QIcon(pixmap))
menu = QMenu()
menu.addAction("Quit", app.quit)
if "noguard" not in sys.argv:
    from frameguard import gui
    print("guard:", gui._install_macos_status_item_guard(), flush=True)
tray.setContextMenu(menu)
tray.show()

objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
objc.objc_getClass.restype = ctypes.c_void_p
objc.objc_getClass.argtypes = [ctypes.c_char_p]
objc.sel_registerName.restype = ctypes.c_void_p
objc.sel_registerName.argtypes = [ctypes.c_char_p]


def send(restype, *argtypes):
    return ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)(
        ("objc_msgSend", objc))


def sel(name):
    return objc.sel_registerName(name.encode())


def cls(name):
    return objc.objc_getClass(name.encode())


def nsstring(text):
    return send(ctypes.c_void_p, ctypes.c_char_p)(
        cls("NSString"), sel("stringWithUTF8String:"), text.encode())


def fire():
    nsapp = send(ctypes.c_void_p)(cls("NSApplication"), sel("sharedApplication"))
    windows = send(ctypes.c_void_p)(nsapp, sel("windows"))
    status_menu = None
    for index in range(send(ctypes.c_ulong)(windows, sel("count"))):
        window = send(ctypes.c_void_p, ctypes.c_ulong)(windows, sel("objectAtIndex:"), index)
        if send(ctypes.c_bool, ctypes.c_void_p)(window, sel("respondsToSelector:"), sel("statusItem")):
            item = send(ctypes.c_void_p)(window, sel("statusItem"))
            found = send(ctypes.c_void_p)(item, sel("menu")) if item else None
            status_menu = found or status_menu
    if not status_menu:
        print("NO_STATUS_ITEM: no NSStatusBarWindow with a menu was found", flush=True)
        os._exit(3)
    event = ctypes.CFUNCTYPE(
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_double,
        ctypes.c_double, ctypes.c_ulong, ctypes.c_double, ctypes.c_long, ctypes.c_void_p,
        ctypes.c_short, ctypes.c_long, ctypes.c_long)(("objc_msgSend", objc))(
        cls("NSEvent"),
        sel("otherEventWithType:location:modifierFlags:timestamp:windowNumber:context:subtype:data1:data2:"),
        15, 0.0, 0.0, 0, 0.0, 0, None, 0, 0, 0)  # NSEventTypeApplicationDefined
    send(None, ctypes.c_void_p, ctypes.c_bool)(nsapp, sel("postEvent:atStart:"), event, True)
    ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulonglong,
                     ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool)(("objc_msgSend", objc))(
        nsapp, sel("nextEventMatchingMask:untilDate:inMode:dequeue:"),
        0xFFFFFFFFFFFFFFFF, None, nsstring("kCFRunLoopDefaultMode"), True)
    center = send(ctypes.c_void_p)(cls("NSNotificationCenter"), sel("defaultCenter"))
    send(None, ctypes.c_void_p, ctypes.c_void_p)(
        center, sel("postNotificationName:object:"),
        nsstring("NSMenuDidBeginTrackingNotification"), status_menu)
    print("SURVIVED", flush=True)
    # Exit straight away: the verdict is in, and Qt/AppKit teardown of the
    # status item must not be able to turn a pass into a CI failure.
    sys.stdout.flush()
    os._exit(0)


QTimer.singleShot(1000, fire)
QTimer.singleShot(20000, lambda: os._exit(4))
app.exec()
