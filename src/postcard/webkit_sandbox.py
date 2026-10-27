"""Let WebKit's sandboxed web processes find the session's graphics drivers.

WebKit runs every web process inside a bubblewrap sandbox that sees the
system directories and a few well-known user ones, nothing else. When the
session points libglvnd at a Mesa installed somewhere else (for example a
driver build under the home directory, selected through
__EGL_VENDOR_LIBRARY_FILENAMES), the EGL vendor file is invisible inside the
sandbox: libglvnd finds no EGL implementation, the web process aborts with
"Could not create default EGL display" and every message body stays blank.

webkit_web_context_add_path_to_sandbox() is the API for exactly this. It must
run before the first web process is spawned, so the application calls
expose_graphics_driver_paths() during startup.
"""

import logging
import os

import gi

gi.require_version("WebKit", "6.0")
from gi.repository import WebKit  # noqa: E402

logger = logging.getLogger(__name__)

# Variables naming files (the directory holding them is what gets exposed).
_FILE_VARS = ("__EGL_VENDOR_LIBRARY_FILENAMES",)
# Variables naming directories.
_DIR_VARS = ("__EGL_VENDOR_LIBRARY_DIRS", "LIBGL_DRIVERS_PATH", "GBM_BACKENDS_PATH")
# Already mounted by WebKit itself; add_path_to_sandbox() also refuses some of them.
_ALWAYS_VISIBLE = ("/usr/", "/lib", "/etc/", "/dev/", "/proc/", "/sys/")


def _paths_from_environment() -> list[str]:
    paths: list[str] = []
    for var, is_file in [(v, True) for v in _FILE_VARS] + [(v, False) for v in _DIR_VARS]:
        for entry in filter(None, os.environ.get(var, "").split(os.pathsep)):
            path = os.path.realpath(os.path.dirname(entry) if is_file else entry)
            if path in paths or not os.path.isdir(path) or path.startswith(_ALWAYS_VISIBLE):
                continue
            if path in (os.path.expanduser("~"), "/home", "/"):
                continue
            paths.append(path)
    return paths


def expose_graphics_driver_paths() -> None:
    """Mount, read-only, the driver paths the session's variables point at."""
    paths = _paths_from_environment()
    if not paths:
        return
    context = WebKit.WebContext.get_default()
    for path in paths:
        context.add_path_to_sandbox(path, True)
        logger.info("web process sandbox: exposing %s", path)
