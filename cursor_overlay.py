"""
AURA Gaze Cursor Overlay
========================
A transparent, always-on-top, click-through NSPanel that renders the gaze
cursor dot on the full screen — not limited to the webview window.

Thread-safety: position/visibility changes can be called from any thread.
They are forwarded to the Cocoa main run-loop via
performSelectorOnMainThread_withObject_waitUntilDone_.
"""

import sys
import threading

# Only meaningful on macOS
if sys.platform != "darwin":
    def init_overlay():       pass
    def set_position(*a, **k): pass
    def hide():               pass
    def destroy():            pass
else:
    import objc
    from AppKit import (
        NSPanel, NSView, NSColor, NSScreen,
        NSBackingStoreBuffered, NSGraphicsContext,
        NSWindowStyleMaskBorderless,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorStationary,
        NSWindowCollectionBehaviorIgnoresCycle,
        NSStatusWindowLevel, NSObject,
    )
    from Foundation import NSMakeRect, NSMakePoint
    import Quartz

    CURSOR_SIZE = 36          # px — diameter of the gaze dot window

    # ── Shared state (written from any thread, read on main thread) ──────────
    _pos     = [0.0, 0.0]    # [quartz_x, quartz_y]
    _visible = [False]        # [bool]
    _alpha   = [1.0]          # [float] — 1.0 active, 0.3 paused
    _win     = [None]         # [NSPanel | None]
    _sh      = [0.0]          # [screen height in AppKit points]
    _mgr     = [None]         # [_Manager | None]
    _lock    = threading.Lock()

    # ── Custom NSView that draws the glowing dot ─────────────────────────────
    class _CursorView(NSView):
        def drawRect_(self, rect):
            ctx = NSGraphicsContext.currentContext().CGContext()
            w, h = rect.size.width, rect.size.height
            cx, cy = w / 2, h / 2
            r  = min(w, h) / 2 - 3

            # Inner translucent cyan fill
            Quartz.CGContextSetRGBFillColor(ctx, 0.0, 0.82, 1.0, 0.60)
            Quartz.CGContextFillEllipseInRect(
                ctx, Quartz.CGRectMake(cx - r * 0.55, cy - r * 0.55, r * 1.1, r * 1.1)
            )
            # Outer ring
            Quartz.CGContextSetRGBStrokeColor(ctx, 0.0, 0.82, 1.0, 0.92)
            Quartz.CGContextSetLineWidth(ctx, 1.5)
            Quartz.CGContextStrokeEllipseInRect(
                ctx, Quartz.CGRectMake(cx - r, cy - r, r * 2, r * 2)
            )

    # ── NSObject proxy — all selectors run on the Cocoa main thread ──────────
    class _Manager(NSObject):

        def create_(self, _):
            """Creates the overlay NSPanel. Must run on main thread."""
            screen = NSScreen.mainScreen()
            if screen is None:
                return
            _sh[0] = screen.frame().size.height

            panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, CURSOR_SIZE, CURSOR_SIZE),
                NSWindowStyleMaskBorderless,
                NSBackingStoreBuffered,
                False,
            )
            panel.setBackgroundColor_(NSColor.clearColor())
            panel.setOpaque_(False)
            # Sit above status bar, control strip, everything else
            panel.setLevel_(NSStatusWindowLevel + 200)
            panel.setIgnoresMouseEvents_(True)   # ← click-through!
            panel.setCollectionBehavior_(
                NSWindowCollectionBehaviorCanJoinAllSpaces |
                NSWindowCollectionBehaviorStationary   |
                NSWindowCollectionBehaviorIgnoresCycle
            )
            panel.setHasShadow_(False)
            panel.setAlphaValue_(0.0)            # hidden until first update

            view = _CursorView.alloc().initWithFrame_(
                NSMakeRect(0, 0, CURSOR_SIZE, CURSOR_SIZE)
            )
            panel.setContentView_(view)
            panel.orderFrontRegardless()
            _win[0] = panel

        def update_(self, _):
            """Move and show/hide the overlay. Must run on main thread."""
            panel = _win[0]
            if panel is None:
                return

            if _visible[0]:
                qx, qy = _pos
                # Quartz: top-left origin → AppKit: bottom-left origin
                ax = qx - CURSOR_SIZE / 2
                ay = _sh[0] - qy - CURSOR_SIZE / 2
                panel.setFrameOrigin_(NSMakePoint(ax, ay))
                panel.setAlphaValue_(_alpha[0])
            else:
                panel.setAlphaValue_(0.0)

        def destroy_(self, _):
            """Close and release the overlay window."""
            panel = _win[0]
            if panel:
                panel.orderOut_(None)
                _win[0] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def init_overlay():
        """
        Create the overlay window. Call once from any non-main thread
        (e.g. the macos_controller asyncio thread) AFTER the Cocoa main
        run-loop is already running (i.e. after webview.start()).
        Blocks until the main thread has created the window.
        """
        m = _Manager.alloc().init()
        _mgr[0] = m
        m.performSelectorOnMainThread_withObject_waitUntilDone_(
            "create:", None, True
        )

    _last_update: float = 0.0          # timestamp of last overlay update
    _UPDATE_INTERVAL: float = 0.016    # ~60fps max (16ms between updates)

    def set_position(x: float, y: float, visible: bool = True, paused: bool = False):
        """
        Update cursor position and visibility from any thread.
        x, y are Quartz screen coordinates (top-left origin).
        Runs at up to 60fps smoothly.
        """
        import time as _time
        now = _time.monotonic()
        with _lock:
            _pos[0], _pos[1] = x, y
            _visible[0] = visible
            _alpha[0] = 0.30 if paused else 1.0
            if now - set_position._last_update < _UPDATE_INTERVAL:
                return          # skip this frame — too soon
            set_position._last_update = now
        m = _mgr[0]
        if m:
            m.performSelectorOnMainThread_withObject_waitUntilDone_(
                "update:", None, False
            )
    set_position._last_update = 0.0


    def hide():
        """Immediately hide the cursor from any thread."""
        with _lock:
            _visible[0] = False
        m = _mgr[0]
        if m:
            m.performSelectorOnMainThread_withObject_waitUntilDone_(
                "update:", None, False
            )

    def destroy():
        """Close the overlay window cleanly (call on shutdown)."""
        m = _mgr[0]
        if m:
            m.performSelectorOnMainThread_withObject_waitUntilDone_(
                "destroy:", None, False
            )
