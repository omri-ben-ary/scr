"""Scriptorium: git-backed research task queues with a hashed dependency graph."""
from __future__ import annotations

import os as _os
import subprocess as _sp

__version__ = "0.1.0"

# On Windows, a console program started from a windowless parent (the UI and the watcher run under
# pythonw) gets a brand-new console window — one per git call, per scr step, per worker. Seen
# 2026-09-16 as "a scr.exe window keeps popping up". Default every child to CREATE_NO_WINDOW unless
# the caller explicitly asks for a console (CREATE_NEW_CONSOLE: the Discuss / scriber tabs).
if _os.name == "nt" and not getattr(_sp.Popen, "_scr_patched", False):
    _orig_init = _sp.Popen.__init__

    def _init(self, *args, **kw):
        flags = kw.get("creationflags", 0)
        if not flags & _sp.CREATE_NEW_CONSOLE:  # type: ignore[attr-defined]
            kw["creationflags"] = flags | _sp.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        _orig_init(self, *args, **kw)

    _sp.Popen.__init__ = _init  # type: ignore[method-assign]
    _sp.Popen._scr_patched = True  # type: ignore[attr-defined]
