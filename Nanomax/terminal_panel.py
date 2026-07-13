import atexit
import ctypes
import os
import re
import shutil
import sys


ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
STD_OUTPUT_HANDLE = -11
_VT_ENABLED = False
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
FG_RED = "\033[31m"
FG_GREEN = "\033[32m"
FG_YELLOW = "\033[33m"
FG_BLUE = "\033[34m"
FG_CYAN = "\033[36m"
FG_WHITE = "\033[37m"
FG_BRIGHT_BLACK = "\033[90m"
FG_BRIGHT_RED = "\033[91m"
FG_BRIGHT_GREEN = "\033[92m"
FG_BRIGHT_YELLOW = "\033[93m"
FG_BRIGHT_CYAN = "\033[96m"
BG_BLUE = "\033[44m"


def enable_virtual_terminal():
    global _VT_ENABLED
    if _VT_ENABLED or os.name != "nt" or not sys.stdout.isatty():
        return
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        _VT_ENABLED = True
    except Exception:
        _VT_ENABLED = True


def terminal_width():
    return max(80, shutil.get_terminal_size((120, 30)).columns)


def truncate_line(text, width=None):
    width = (terminal_width() if width is None else max(80, int(width))) - 1
    text = str(text)
    return text if len(text) <= width else text[: width - 3] + "..."


def strip_ansi(text):
    return ANSI_RE.sub("", str(text))


def visible_len(text):
    return len(strip_ansi(text))


def pad_ansi(text, width):
    return str(text) + " " * max(0, int(width) - visible_len(text))


def colorize(text, *styles):
    return "".join(styles) + str(text) + RESET


def _highlight_keywords(line):
    colors = {
        "OK": (BOLD, FG_BRIGHT_GREEN),
        "YES": (BOLD, FG_BRIGHT_GREEN),
        "READY": (BOLD, FG_BRIGHT_GREEN),
        "CONNECTED": (BOLD, FG_BRIGHT_GREEN),
        "NORMAL": (BOLD, FG_BRIGHT_GREEN),
        "COMPLETED": (BOLD, FG_BRIGHT_GREEN),
        "TRUE": (BOLD, FG_BRIGHT_GREEN),
        "WAIT": (BOLD, FG_BRIGHT_YELLOW),
        "WARNING": (BOLD, FG_BRIGHT_YELLOW),
        "WARN": (BOLD, FG_BRIGHT_YELLOW),
        "DISABLED": (BOLD, FG_BRIGHT_YELLOW),
        "UNAVAILABLE": (BOLD, FG_BRIGHT_YELLOW),
        "NO": (BOLD, FG_BRIGHT_RED),
        "BLOCKED": (BOLD, FG_BRIGHT_RED),
        "ERROR": (BOLD, FG_BRIGHT_RED),
        "FAILED": (BOLD, FG_BRIGHT_RED),
        "FAIL": (BOLD, FG_BRIGHT_RED),
        "OUT": (BOLD, FG_BRIGHT_RED),
    }

    def replace(match):
        word = match.group(0)
        style = colors.get(word.upper())
        return colorize(word, *style) if style else word

    return re.sub(
        r"\b(OK|YES|READY|CONNECTED|NORMAL|COMPLETED|TRUE|WAIT|WARNING|WARN|DISABLED|UNAVAILABLE|NO|BLOCKED|ERROR|FAILED|FAIL|OUT)\b",
        replace,
        line,
        flags=re.IGNORECASE,
    )


def _highlight_keys(line):
    return re.sub(
        r"(?<!\S)([A-Za-z][A-Za-z0-9_./-]{0,31})(=)",
        lambda match: colorize(match.group(1), BOLD, FG_BRIGHT_GREEN) + match.group(2),
        line,
    )


def _style_indented_line(line):
    if "=" in line:
        return _highlight_keywords(_highlight_keys(line))
    match = re.match(r"(\s{2,})(\S[^ ]*(?:\s*/\s*\S[^ ]*)?)(.*)", line)
    if not match:
        return line
    indent, key, rest = match.groups()
    return indent + colorize(key, BOLD, FG_BRIGHT_GREEN) + colorize(rest, FG_WHITE)


class TerminalPanelRenderer:
    """Low-flicker fixed-panel renderer for Windows console TUI screens."""

    def __init__(self):
        self._line_count = 0
        self._started = False
        self._cursor_hidden = False
        atexit.register(self.show_cursor)

    def hide_cursor(self):
        if sys.stdout.isatty() and not self._cursor_hidden:
            enable_virtual_terminal()
            sys.stdout.write("\033[?25l")
            self._cursor_hidden = True

    def show_cursor(self):
        if sys.stdout.isatty() and self._cursor_hidden:
            sys.stdout.write("\033[?25h")
            sys.stdout.flush()
            self._cursor_hidden = False

    def _style_line(self, line, width):
        plain = str(line)
        if set(plain) == {"="}:
            return colorize(plain, FG_BRIGHT_BLACK)
        if plain.startswith("PAM "):
            return colorize(plain.center(width - 1), BOLD, FG_WHITE, BG_BLUE)
        if plain.startswith("[") and "]" in plain:
            title, rest = plain.split("]", 1)
            return colorize(title + "]", BOLD, FG_BRIGHT_CYAN) + colorize(rest, FG_BRIGHT_BLACK)
        if plain.startswith("Hotkeys:") or plain.startswith("Commands after"):
            return colorize(plain, BOLD, FG_BRIGHT_YELLOW)
        if plain.startswith("  "):
            return _highlight_keywords(_style_indented_line(plain))
        if plain.startswith("Message:"):
            return colorize("Message:", BOLD, FG_BRIGHT_YELLOW) + _highlight_keywords(plain[len("Message:"):])
        if plain.startswith("Start prompt:"):
            return colorize("Start prompt:", BOLD, FG_BRIGHT_CYAN) + _highlight_keywords(plain[len("Start prompt:"):])
        if plain.startswith(("Trajectory:", "Travel check:", "Travel/step check:", "DAQ message:", "DAQ timings:")):
            prefix, rest = plain.split(":", 1)
            return colorize(prefix + ":", BOLD, FG_BRIGHT_CYAN) + _highlight_keywords(rest)
        return _highlight_keywords(_highlight_keys(plain))

    def render(self, lines):
        width = terminal_width()
        expanded = []
        for line in lines:
            parts = str(line).splitlines() or [""]
            expanded.extend(parts)
        normalized = [truncate_line(line, width) for line in expanded]
        if not sys.stdout.isatty():
            print("\n".join(normalized), flush=True)
            return

        enable_virtual_terminal()
        self.hide_cursor()
        styled = [self._style_line(line, width) for line in normalized]
        if self._started:
            sys.stdout.write("\033[H")
        else:
            sys.stdout.write("\033[2J\033[H")
            self._started = True

        for line in styled:
            sys.stdout.write(pad_ansi(line, width - 1))
            sys.stdout.write("\033[K\n")

        for _ in range(max(0, self._line_count - len(normalized))):
            sys.stdout.write("\033[K\n")

        # Clear command prompts or old panel tail below the current frame.
        sys.stdout.write("\033[J")
        sys.stdout.flush()
        self._line_count = len(normalized)
