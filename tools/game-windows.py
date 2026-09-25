#!/usr/bin/env python3
"""Keep the arcade's game windows away from the desktop's window manager (./tesla start --native).

On the car there is no window manager. Beach Buggy Racing 2 opens a plain X window "cobalt" and
QtCar moves it over its game area (ExternalAppView, 0,60 1920x1140). MAME's window stays below
QtCar, which shows the game's picture itself and sends the key presses with XTest (they go to the
window with the input focus). On a desktop the window manager frames these windows instead (title
bar, its own placement, the desktop's panel above the game, the focus on QtCar: no keys for MAME).
This watches the X server and takes them back from the window manager: override-redirect,
unmapped, reparented to the root window, mapped again; cobalt at QtCar's place and on top, MAME
below QtCar with the keyboard focus.

  game-windows.py [--top cobalt] [--hidden MAME] [--tidk DIR] [--hide-cursor] [-v]   ($DISPLAY, $XAUTHORITY)

--tidk: the folder with QtCar's TIDK_WINDOW_{NAME,LEFT,TOP,WIDTH,HEIGHT} for the game (the
chroot's /tmp/games/cobalt/tidk). QtCar moves the window there once, when the game starts, and the
window manager swallows that; so the window named TIDK_WINDOW_NAME is put there.

--hide-cursor: no mouse cursor while this runs, as on the car. On a desktop it stays visible
(the touchscreen is grabbed by tesla-touch, so X never hides it) and in the arcade it moves:
QtCar turns the steering wheel into mouse motion with XTest for the trackball games.
"""
import argparse, ctypes, ctypes.util, os, sys, time

X = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
Window = ctypes.c_ulong


class XSetWindowAttributes(ctypes.Structure):
    _fields_ = [("background_pixmap", ctypes.c_ulong), ("background_pixel", ctypes.c_ulong),
                ("border_pixmap", ctypes.c_ulong), ("border_pixel", ctypes.c_ulong),
                ("bit_gravity", ctypes.c_int), ("win_gravity", ctypes.c_int), ("backing_store", ctypes.c_int),
                ("backing_planes", ctypes.c_ulong), ("backing_pixel", ctypes.c_ulong),
                ("save_under", ctypes.c_int), ("event_mask", ctypes.c_long),
                ("do_not_propagate_mask", ctypes.c_long), ("override_redirect", ctypes.c_int),
                ("colormap", ctypes.c_ulong), ("cursor", ctypes.c_ulong)]


class XWindowAttributes(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int), ("width", ctypes.c_int), ("height", ctypes.c_int),
                ("border_width", ctypes.c_int), ("depth", ctypes.c_int), ("visual", ctypes.c_void_p),
                ("root", Window), ("class", ctypes.c_int), ("bit_gravity", ctypes.c_int),
                ("win_gravity", ctypes.c_int), ("backing_store", ctypes.c_int),
                ("backing_planes", ctypes.c_ulong), ("backing_pixel", ctypes.c_ulong),
                ("save_under", ctypes.c_int), ("colormap", ctypes.c_ulong), ("map_installed", ctypes.c_int),
                ("map_state", ctypes.c_int), ("all_event_masks", ctypes.c_long),
                ("your_event_mask", ctypes.c_long), ("do_not_propagate_mask", ctypes.c_long),
                ("override_redirect", ctypes.c_int), ("screen", ctypes.c_void_p)]


CWOverrideRedirect = 1 << 9
IsViewable = 2
RevertToParent, CurrentTime = 2, 0
X.XOpenDisplay.restype = ctypes.c_void_p
X.XOpenDisplay.argtypes = [ctypes.c_char_p]
X.XDefaultRootWindow.restype = Window
X.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
X.XQueryTree.argtypes = [ctypes.c_void_p, Window, ctypes.POINTER(Window), ctypes.POINTER(Window),
                         ctypes.POINTER(ctypes.POINTER(Window)), ctypes.POINTER(ctypes.c_uint)]
X.XFetchName.argtypes = [ctypes.c_void_p, Window, ctypes.POINTER(ctypes.c_char_p)]
X.XGetWindowAttributes.argtypes = [ctypes.c_void_p, Window, ctypes.POINTER(XWindowAttributes)]
X.XChangeWindowAttributes.argtypes = [ctypes.c_void_p, Window, ctypes.c_ulong, ctypes.POINTER(XSetWindowAttributes)]
X.XTranslateCoordinates.argtypes = [ctypes.c_void_p, Window, Window, ctypes.c_int, ctypes.c_int,
                                    ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(Window)]
X.XReparentWindow.argtypes = [ctypes.c_void_p, Window, Window, ctypes.c_int, ctypes.c_int]
X.XMoveResizeWindow.argtypes = [ctypes.c_void_p, Window, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
X.XGetInputFocus.argtypes = [ctypes.c_void_p, ctypes.POINTER(Window), ctypes.POINTER(ctypes.c_int)]
X.XSetInputFocus.argtypes = [ctypes.c_void_p, Window, ctypes.c_int, ctypes.c_ulong]
for f in ("XUnmapWindow", "XMapRaised", "XMapWindow", "XRaiseWindow", "XLowerWindow"):
    getattr(X, f).argtypes = [ctypes.c_void_p, Window]
X.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
X.XFree.argtypes = [ctypes.c_void_p]
X.XSetErrorHandler.argtypes = [ctypes.c_void_p]
# windows vanish while we look at them: ignore X errors (BadWindow) instead of exiting
ERROR_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(lambda d, e: 0)


def children(dpy, w):
    root, parent, kids, n = Window(), Window(), ctypes.POINTER(Window)(), ctypes.c_uint()
    if not X.XQueryTree(dpy, w, ctypes.byref(root), ctypes.byref(parent), ctypes.byref(kids), ctypes.byref(n)):
        return None, []
    out = [kids[i] for i in range(n.value)]
    if kids:
        X.XFree(kids)
    return parent.value, out


def name(dpy, w):
    p = ctypes.c_char_p()
    if X.XFetchName(dpy, w, ctypes.byref(p)) and p.value is not None:
        s = p.value.decode(errors="replace")
        X.XFree(p)
        return s
    return None


def attrs(dpy, w):
    a = XWindowAttributes()
    return a if X.XGetWindowAttributes(dpy, w, ctypes.byref(a)) else None


def tidk_geometry(d):
    """(name, x, y, width, height) from QtCar's TIDK_WINDOW_* files, or None."""
    try:
        v = {k: open(os.path.join(d, "TIDK_WINDOW_" + k)).read().strip()
             for k in ("NAME", "LEFT", "TOP", "WIDTH", "HEIGHT")}
        return v["NAME"], int(v["LEFT"]), int(v["TOP"]), int(v["WIDTH"]), int(v["HEIGHT"])
    except (OSError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", default="cobalt", help="windows that show the game: on top (name prefixes)")
    ap.add_argument("--hidden", default="MAME", help="windows QtCar shows itself: below QtCar, with the "
                    "keyboard focus for QtCar's XTest keys (name prefixes)")
    ap.add_argument("--tidk", metavar="DIR", help="QtCar's TIDK_WINDOW_* files (where the game window goes)")
    ap.add_argument("--hide-cursor", action="store_true", help="no mouse cursor while this runs")
    ap.add_argument("-v", action="store_true")
    args = ap.parse_args()
    top_names = tuple(n for n in args.top.split(",") if n)
    hidden_names = tuple(n for n in args.hidden.split(",") if n)
    dpy = X.XOpenDisplay(None)
    if not dpy:
        sys.exit("game-windows: can't open the display")
    root = X.XDefaultRootWindow(dpy)
    if args.hide_cursor:
        # XFixes: hidden for as long as this client is connected (back when it exits). Needs
        # version 4, negotiated first (else the server refuses the request).
        xf = ctypes.CDLL(ctypes.util.find_library("Xfixes") or "libXfixes.so.3")
        xf.XFixesQueryVersion.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
        xf.XFixesHideCursor.argtypes = [ctypes.c_void_p, Window]
        major, minor = ctypes.c_int(6), ctypes.c_int(0)
        errors = []
        handler = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(lambda d, e: errors.append(1) or 0)
        X.XSetErrorHandler(handler)
        if xf.XFixesQueryVersion(dpy, ctypes.byref(major), ctypes.byref(minor)) and major.value >= 4:
            xf.XFixesHideCursor(dpy, root)
            X.XSync(dpy, 0)
        print("game-windows: mouse cursor %s (XFixes %d.%d)" % ("hidden" if major.value >= 4 and not errors
              else "not hidden", major.value, minor.value), file=sys.stderr, flush=True)
    X.XSetErrorHandler(ERROR_HANDLER)
    taken = {}                                 # window -> "top" | "hidden"
    while True:
        for frame in children(dpy, root)[1]:
            # a window manager frame holds the client one level down
            for w in children(dpy, frame)[1]:
                n = name(dpy, w)
                kind = "top" if n and n.startswith(top_names) else "hidden" if n and n.startswith(hidden_names) else None
                if not kind:
                    continue
                a = attrs(dpy, w)
                if not a or a.override_redirect or a.map_state != IsViewable:
                    continue
                x, y, dummy = ctypes.c_int(), ctypes.c_int(), Window()
                X.XTranslateCoordinates(dpy, w, root, 0, 0, ctypes.byref(x), ctypes.byref(y), ctypes.byref(dummy))
                sa = XSetWindowAttributes(override_redirect=1)
                X.XChangeWindowAttributes(dpy, w, CWOverrideRedirect, ctypes.byref(sa))
                X.XUnmapWindow(dpy, w)
                X.XSync(dpy, 0)
                time.sleep(0.2)                # the window manager drops its frame
                X.XReparentWindow(dpy, w, root, x.value, y.value)
                geo = tidk_geometry(args.tidk) if args.tidk else None
                if geo and geo[0] == n:
                    X.XMoveResizeWindow(dpy, w, *geo[1:])
                    x.value, y.value = geo[1], geo[2]
                if kind == "top":
                    X.XMapRaised(dpy, w)
                else:                          # as QtCar's MameWindow::lowerX11Window does
                    X.XMapWindow(dpy, w)
                    X.XLowerWindow(dpy, w)
                X.XSync(dpy, 0)
                taken[w] = kind
                print("game-windows: took %s (0x%x) from the window manager, at %d,%d %dx%d, %s"
                      % (n, w, x.value, y.value, a.width, a.height, kind), file=sys.stderr, flush=True)
        stack = children(dpy, root)[1]         # bottom to top
        for w in [w for w in taken if w not in stack]:
            del taken[w]
        for w in stack:
            # on the root window already: taken by an earlier run of this helper, or there is no
            # window manager (Xvfb): only stacking and focus to look after
            n = name(dpy, w)
            if w not in taken and n and n.startswith(top_names + hidden_names):
                taken[w] = "top" if n.startswith(top_names) else "hidden"
        for w, kind in taken.items():
            if kind == "top":
                # above the desktop's windows (the window manager raises its own frames, e.g.
                # QtCar's when it gets the focus)
                above = stack[stack.index(w) + 1:]
                if any((attrs(dpy, o) or XWindowAttributes()).map_state == IsViewable
                       and not (attrs(dpy, o) or XWindowAttributes()).override_redirect for o in above):
                    X.XRaiseWindow(dpy, w)
                    X.XSync(dpy, 0)
                    if args.v:
                        print("game-windows: raised 0x%x" % w, file=sys.stderr, flush=True)
            elif (attrs(dpy, w) or XWindowAttributes()).map_state == IsViewable:
                # QtCar sends the game's keys with XTest: they go to the focus window. Without a
                # window manager (the car) that's MAME's; here the window manager gives it to QtCar.
                focus, revert = Window(), ctypes.c_int()
                X.XGetInputFocus(dpy, ctypes.byref(focus), ctypes.byref(revert))
                if focus.value != w:
                    X.XSetInputFocus(dpy, w, RevertToParent, CurrentTime)
                    X.XSync(dpy, 0)
                    if args.v:
                        print("game-windows: focus to 0x%x (was 0x%x)" % (w, focus.value), file=sys.stderr, flush=True)
        time.sleep(0.5)


if __name__ == "__main__":
    main()
