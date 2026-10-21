"""Subprocess probe: does the Dock really get the themed, full-resolution icon?

Runs under the native Cocoa platform, applies FrameGuard's application icon
for two themes and reads NSApp.applicationIconImage back from AppKit, which
is exactly what the Dock draws. Prints one line per theme:
    ICON <theme> <largest-rep-px> <plate colour at 25%/80%>
Exit codes: 0 done, 3 not running on Cocoa (caller skips).
"""
import ctypes
import ctypes.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv[:1])
if app.platformName() != "cocoa":
    print("NOT_COCOA", app.platformName(), flush=True)
    os._exit(3)

from frameguard import gui  # noqa: E402
from frameguard.theme import get_theme  # noqa: E402

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


def dock_image():
    nsapp = send(ctypes.c_void_p)(objc.objc_getClass(b"NSApplication"), sel("sharedApplication"))
    image = send(ctypes.c_void_p)(nsapp, sel("applicationIconImage"))
    reps = send(ctypes.c_void_p)(image, sel("representations"))
    best, largest = None, 0
    for i in range(send(ctypes.c_ulong)(reps, sel("count"))):
        rep = send(ctypes.c_void_p, ctypes.c_ulong)(reps, sel("objectAtIndex:"), i)
        width = send(ctypes.c_long)(rep, sel("pixelsWide"))
        if width > largest:
            best, largest = rep, width
    if best is None:
        return 0, None
    # AppKit stores its own snapshot rep; go through TIFF -> bitmap -> PNG.
    tiff = send(ctypes.c_void_p)(image, sel("TIFFRepresentation"))
    bitmap = send(ctypes.c_void_p, ctypes.c_void_p)(
        objc.objc_getClass(b"NSBitmapImageRep"), sel("imageRepWithData:"), tiff)
    data = send(ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p)(
        bitmap, sel("representationUsingType:properties:"), 4, None)  # PNG
    raw = ctypes.string_at(send(ctypes.c_void_p)(data, sel("bytes")),
                           send(ctypes.c_ulong)(data, sel("length")))
    return largest, QImage.fromData(raw)


for name in ("Blue Hour", "Clean Light"):
    gui._apply_application_icon(get_theme(name))
    largest, image = dock_image()
    colour = image.pixelColor(int(image.width() * 0.25), int(image.height() * 0.80)).name() \
        if image is not None and not image.isNull() else "none"
    print("ICON", name.replace(" ", "_"), largest, colour, flush=True)
os._exit(0)
