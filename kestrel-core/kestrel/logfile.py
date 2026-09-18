"""
Where Kestrel writes down what happened.

The desktop apps used to print everything — the node's chatter, and a full
Python traceback whenever anything went wrong — straight to whatever
console the app happened to be launched from. On Windows that is the black
window behind `run.bat`, so the first sign of trouble was a screenful of
code appearing over the top of it. Worse, launched without a console at
all (pythonw), `sys.stdout` and `sys.stderr` are None, and a `print` at
the wrong moment is not noise but a crash.

So: a log file next to the app, and a console that stays quiet in the
apps and stays talkative on the command line, where output is the point.

Nothing here can raise. A logger that throws while reporting a problem is
worse than no logger, and this one is called from exactly those moments.
"""

import os
import sys
import threading
import time

_LOCK = threading.Lock()
_PATH = None
_QUIET = False                     # True in the GUI apps: console off
MAX_BYTES = 1_000_000              # ~a few thousand lines, then rotate once


def setup(directory: str, name: str = "kestrel", *, quiet: bool = True):
    """Start logging to `directory`/kestrel-log.txt. Returns the path.

    `quiet` silences the console, which is what a windowed app wants. The
    CLI leaves it False so that running a node in a terminal still prints.
    """
    global _PATH, _QUIET
    _QUIET = bool(quiet)
    try:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "kestrel-log.txt")
        _rotate(path)
        _PATH = path
        # File only: on the command line these two lines are noise in front
        # of whatever the person actually asked for.
        write(f"--- {name} starting, {time.strftime('%Y-%m-%d %H:%M:%S')} "
              f"---", to_console=False)
        write(f"python {sys.version.split()[0]} on {sys.platform}",
              to_console=False)
        if _QUIET:
            _catch_all()
        return path
    except Exception:
        _PATH = None               # no log is survivable; crashing is not
        return None


def _catch_all():
    """Send anything Python would have printed to the log file instead.

    The handlers above cover the paths Kestrel knows about. These are the
    ones it doesn't: a failure on the way up before any of it is running,
    and a worker thread dying. Both print a full traceback to stderr by
    default, which is the wall of code people see over their terminal —
    and under pythonw, stderr is None and the printing itself raises.
    """
    def on_main(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return
        import traceback
        exception("unhandled", exc,
                  tb="".join(traceback.format_exception(exc_type, exc, tb)))

    def on_thread(args):
        if issubclass(args.exc_type, SystemExit):
            return
        import traceback
        exception(f"thread {getattr(args.thread, 'name', '?')}",
                  args.exc_value or args.exc_type(),
                  tb="".join(traceback.format_exception(
                      args.exc_type, args.exc_value, args.exc_traceback)))

    try:
        sys.excepthook = on_main
        threading.excepthook = on_thread
    except Exception:
        pass


def path():
    return _PATH


def _rotate(path):
    try:
        if os.path.exists(path) and os.path.getsize(path) > MAX_BYTES:
            old = path + ".1"
            try:
                os.remove(old)
            except OSError:
                pass
            os.replace(path, old)
    except Exception:
        pass


def write(line: str, *, level: str = "info", to_console: bool = True):
    """One line to the log file. Never raises, never blocks for long."""
    stamp = time.strftime("%H:%M:%S")
    text = f"{stamp} {line}"
    if _PATH:
        try:
            with _LOCK:
                with open(_PATH, "a", encoding="utf-8", errors="replace") as f:
                    f.write(text + "\n")
        except Exception:
            pass
    if to_console:
        console(text)


def console(text: str):
    """Print, unless nobody asked for output or there is nowhere to send it.

    Silent in the desktop apps, because a window that started from a
    double-click has no business writing on the terminal behind it — the
    same text is in the log file. Launched by pythonw there is no console
    at all and these streams are None; writing to them is an
    AttributeError in the middle of whatever was being reported.
    """
    if _QUIET:
        return
    try:
        stream = sys.stderr or sys.stdout
        if stream is None:
            return
        stream.write(text + "\n")
        stream.flush()
    except Exception:
        pass


def exception(where: str, exc: BaseException, *, tb: str = None):
    """A failure, with its traceback — to the file, never to the console."""
    write(f"[error] {where}: {type(exc).__name__}: {exc}", level="error")
    if tb is None:
        import traceback
        tb = traceback.format_exc()
    if _PATH and tb:
        try:
            with _LOCK:
                with open(_PATH, "a", encoding="utf-8",
                          errors="replace") as f:
                    f.write(tb.rstrip() + "\n")
        except Exception:
            pass
    # `write` above already echoed the one-line summary where that is
    # wanted; the traceback stays in the file either way.
