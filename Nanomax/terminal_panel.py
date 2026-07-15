import atexit
import ctypes
import os
import re
import shutil
import sys
import textwrap


ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
STD_OUTPUT_HANDLE = -11
_VT_ENABLED = False
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"


def rgb(r, g, b):
    return f"\033[38;2;{int(r)};{int(g)};{int(b)}m"


def bg_rgb(r, g, b):
    return f"\033[48;2;{int(r)};{int(g)};{int(b)}m"


FG_TEXT = rgb(226, 229, 233)
FG_DIM = rgb(128, 136, 145)
FG_SECTION = rgb(112, 191, 206)
FG_LABEL = rgb(142, 201, 120)
FG_VALUE = rgb(226, 156, 138)
FG_GOOD = rgb(122, 214, 152)
FG_WARN = rgb(224, 192, 104)
FG_BAD = rgb(228, 124, 108)
FG_MUTED = rgb(170, 177, 185)
BG_HEADER = bg_rgb(43, 53, 66)
KEYWORD_STYLES = {
    "OK": (BOLD, FG_GOOD),
    "YES": (BOLD, FG_GOOD),
    "READY": (BOLD, FG_GOOD),
    "CONNECTED": (BOLD, FG_GOOD),
    "NORMAL": (BOLD, FG_GOOD),
    "COMPLETED": (BOLD, FG_GOOD),
    "TRUE": (BOLD, FG_GOOD),
    "WAIT": (BOLD, FG_WARN),
    "WARNING": (BOLD, FG_WARN),
    "WARN": (BOLD, FG_WARN),
    "DISABLED": (BOLD, FG_WARN),
    "UNAVAILABLE": (BOLD, FG_WARN),
    "NO": (BOLD, FG_BAD),
    "BLOCKED": (BOLD, FG_BAD),
    "ERROR": (BOLD, FG_BAD),
    "FAILED": (BOLD, FG_BAD),
    "FAIL": (BOLD, FG_BAD),
    "OUT": (BOLD, FG_BAD),
}
KEYWORD_RE = re.compile(
    r"\b(OK|YES|READY|CONNECTED|NORMAL|COMPLETED|TRUE|WAIT|WARNING|WARN|DISABLED|UNAVAILABLE|NO|BLOCKED|ERROR|FAILED|FAIL|OUT)\b",
    flags=re.IGNORECASE,
)


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
    def replace(match):
        word = match.group(0)
        style = KEYWORD_STYLES.get(word.upper())
        return colorize(word, *style) if style else word

    return KEYWORD_RE.sub(replace, line)


def _highlight_keys(line):
    return re.sub(
        r"(?<!\S)([A-Za-z][A-Za-z0-9_./-]{0,31})(=)",
        lambda match: colorize(match.group(1), BOLD, FG_LABEL) + match.group(2),
        line,
    )


def _style_value(text):
    parts = []
    start = 0
    for match in KEYWORD_RE.finditer(text):
        prefix = text[start:match.start()]
        if prefix:
            parts.append(colorize(prefix, FG_VALUE))
        style = KEYWORD_STYLES.get(match.group(0).upper())
        parts.append(colorize(match.group(0), *style))
        start = match.end()
    suffix = text[start:]
    if suffix:
        parts.append(colorize(suffix, FG_VALUE))
    return "".join(parts) if parts else colorize(text, FG_VALUE)


def _style_command_segment(text):
    def replace(match):
        token = match.group(0)
        if token.startswith("<") and token.endswith(">"):
            return colorize(token, BOLD, FG_WARN)
        if token in {"/", "|", ":", "->", "=>", "+=", "-=", "...", ","}:
            return colorize(token, DIM, FG_LABEL)
        return colorize(token, BOLD, FG_LABEL)

    return re.sub(r"<[^>]+>|/|\||:|->|=>|\+=|-=|\.{3}|,|[A-Za-z0-9_./+-]+", replace, text)


def _style_assignment_cells(line):
    tokens = re.split(r"(\s{2,})", line)
    styled = []
    for token in tokens:
        if not token:
            continue
        if re.fullmatch(r"\s{2,}", token):
            styled.append(token)
            continue
        if "=" not in token:
            styled.append(token)
            continue
        key, value = token.split("=", 1)
        styled.append(colorize(key, BOLD, FG_LABEL) + "=" + _style_value(value))
    return "".join(styled)


def _style_indented_line(line):
    if "=" in line:
        return _style_assignment_cells(line)
    match = re.match(r"(\s*)(.*)", line)
    indent, body = match.groups()
    pieces = re.split(r"(\s{2,})", body)
    text_pieces = [piece for piece in pieces if piece and not re.fullmatch(r"\s{2,}", piece)]
    styled = [indent]
    text_index = 0
    total_text = len(text_pieces)
    for piece in pieces:
        if not piece:
            continue
        if re.fullmatch(r"\s{2,}", piece):
            styled.append(piece)
            continue
        text_index += 1
        if text_index < total_text:
            styled.append(_style_command_segment(piece))
        else:
            styled.append(colorize(piece, FG_TEXT))
    return "".join(styled)


def _wrap_cell_text(text, width):
    width = max(12, int(width))
    chunks = textwrap.wrap(
        str(text),
        width=width,
        break_long_words=False,
        break_on_hyphens=False,
    )
    if not chunks:
        return [""]
    wrapped = []
    for chunk in chunks:
        if len(chunk) <= width:
            wrapped.append(chunk)
        else:
            wrapped.extend(
                textwrap.wrap(
                    chunk,
                    width=width,
                    break_long_words=True,
                    break_on_hyphens=False,
                )
            )
    return wrapped or [""]


def _wrap_plain_line(text, width):
    text = str(text)
    if len(text) <= width:
        return [text]
    wrap_prefixes = (
        "Message:",
        "Start prompt:",
        "Trajectory:",
        "Travel check:",
        "Travel/step check:",
        "DAQ message:",
        "DAQ timings:",
    )
    for prefix in wrap_prefixes:
        if text.startswith(prefix):
            body = text[len(prefix):].lstrip()
            usable = max(12, width - len(prefix) - 1)
            chunks = _wrap_cell_text(body, usable)
            lines = [f"{prefix} {chunks[0]}"]
            lines.extend("  " + chunk for chunk in chunks[1:])
            return lines
    if not text.startswith("  "):
        return [truncate_line(text, width + 1)]
    match = re.match(r"(\s*)(.*)", text)
    indent, body = match.groups()
    usable = max(12, width - len(indent))
    lines = _wrap_cell_text(body, usable)
    return [indent + line for line in lines]


def format_section_lines(title, items, width=None):
    rows = [f"[{title}]"]
    width = terminal_width() if width is None else max(80, int(width))
    available = max(40, width - 3)
    gap = "  "
    texts = []
    for name, value, hint in items:
        text = f"{name}={value}"
        if hint:
            text = f"{text} ({hint})"
        texts.append(text)

    columns = 3 if width >= 120 else 2 if width >= 92 else 1
    while columns > 1:
        cell_width = (available - (columns - 1) * len(gap)) // columns
        if max(len(text) for text in texts) <= cell_width:
            break
        columns -= 1
    cell_width = max(24, min(64, (available - (columns - 1) * len(gap)) // columns))
    wrapped_cells = [_wrap_cell_text(text, cell_width) for text in texts]

    for index in range(0, len(wrapped_cells), columns):
        group = wrapped_cells[index:index + columns]
        height = max(len(cell) for cell in group)
        for row_index in range(height):
            pieces = []
            for cell in group:
                piece = cell[row_index] if row_index < len(cell) else ""
                pieces.append(piece.ljust(cell_width))
            rows.append("  " + gap.join(pieces).rstrip())
    return rows


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
            return colorize(plain, FG_DIM)
        if plain.startswith("PAM "):
            return colorize(plain.center(width - 1), BOLD, FG_TEXT, BG_HEADER)
        if plain.startswith("[") and "]" in plain:
            title, rest = plain.split("]", 1)
            return colorize(title + "]", BOLD, FG_SECTION) + colorize(rest, FG_DIM)
        if plain.startswith("Hotkeys:") or plain.startswith("Commands after"):
            return colorize(plain, BOLD, FG_WARN)
        if plain.startswith("  "):
            return _highlight_keywords(_style_indented_line(plain))
        if plain.startswith("Message:"):
            return colorize("Message:", BOLD, FG_WARN) + _highlight_keywords(colorize(plain[len("Message:"):], FG_TEXT))
        if plain.startswith("Start prompt:"):
            return colorize("Start prompt:", BOLD, FG_SECTION) + _highlight_keywords(colorize(plain[len("Start prompt:"):], FG_TEXT))
        if plain.startswith(("Trajectory:", "Travel check:", "Travel/step check:", "DAQ message:", "DAQ timings:")):
            prefix, rest = plain.split(":", 1)
            return colorize(prefix + ":", BOLD, FG_SECTION) + _highlight_keywords(colorize(rest, FG_TEXT))
        return _highlight_keywords(_highlight_keys(plain))

    def render(self, lines):
        width = terminal_width()
        expanded = []
        for line in lines:
            parts = str(line).splitlines() or [""]
            expanded.extend(parts)
        normalized = []
        for line in expanded:
            normalized.extend(_wrap_plain_line(line, width - 1))
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
