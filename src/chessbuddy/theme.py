"""Visual themes for ChessBuddy.

Two built-in palettes (``dark`` and ``light``) applied app-wide through one
QSS stylesheet. The colours are exposed both as template variables (for the
QSS) and as module-level names (``theme.BG``, ``theme.EVAL_DARK``, ...) so
widgets that paint directly — the board, the eval bar — pick up the current
palette at paint time.

Switch at runtime with :func:`apply`: it rebinds every colour, rebuilds the
QSS and (given the app) re-applies it, so the whole UI repaints.
"""
from __future__ import annotations

from string import Template

# ------------------------------------------------------------------ palettes
DARK = {
    "BG": "#262421",                # window background
    "BG_PANEL": "#312e2b",          # cards / panels
    "BG_PANEL_HI": "#38342f",       # card hover
    "BG_RAISED": "#3d3a37",         # buttons
    "BG_RAISED_HI": "#4a4642",      # button hover
    "BG_RAISED_PRESSED": "#33302d",
    "BG_DISABLED": "#2e2b28",
    "BG_SUNKEN": "#1e1c1a",         # inputs / status bar
    "BORDER": "#4a4642",
    "BORDER_SOFT": "#3f3b37",
    "BORDER_INPUT": "#45413d",

    "TEXT": "#ecebea",
    "MUTED": "#a39e99",
    "FAINT": "#7a756f",
    "FEN_TEXT": "#d6d2cd",

    "ACCENT": "#81b64c",            # chess.com green
    "ACCENT_HI": "#a3d160",
    "ACCENT_LO": "#6f9a3f",
    "ACCENT_TINT": "#37432c",       # selected card background
    "ON_ACCENT": "#22251c",         # text on top of the accent
    "ACCENT_DISABLED_BG": "#46523a",
    "ACCENT_DISABLED_TEXT": "#8b9481",

    "GOOD": "#a3d160",
    "INACCURACY": "#d9c97a",
    "DANGER": "#e06666",
    "WARN": "#d9a93f",

    # eval chips (white / black advantage tones)
    "CHIP_WHITE_BG": "#ece9e6",
    "CHIP_WHITE_TEXT": "#262421",
    "CHIP_WHITE_BORDER": "#ece9e6",

    # side-to-move chip
    "SIDE_W_BG": "#ece9e6",
    "SIDE_W_TEXT": "#262421",
    "SIDE_W_BORDER": "#ece9e6",
    "SIDE_B_BG": "#171614",
    "SIDE_B_TEXT": "#ecebea",

    "SLIDER_GROOVE": "#45413d",
    "SLIDER_HANDLE": "#c9c4bd",
    "SLIDER_HANDLE_HI": "#ffffff",
    "STATUS_BORDER_TOP": "#2c2925",

    # board widget
    "BOARD_BG": "#211e1b",          # around the grid
    "BOARD_FRAME": "#3a3733",       # rounded frame behind the grid
    "EVAL_LIGHT": "#ece9e6",
    "EVAL_DARK": "#403d39",
    "EVAL_CHIP_BG": "#141312",
}

LIGHT = {
    "BG": "#f2efe9",
    "BG_PANEL": "#ffffff",
    "BG_PANEL_HI": "#f7f4ee",
    "BG_RAISED": "#e8e4dc",
    "BG_RAISED_HI": "#dcd7cd",
    "BG_RAISED_PRESSED": "#d3cdc2",
    "BG_DISABLED": "#efede8",
    "BG_SUNKEN": "#e3ded4",
    "BORDER": "#c9c3b8",
    "BORDER_SOFT": "#d8d2c7",
    "BORDER_INPUT": "#c2bbb0",

    "TEXT": "#2b2823",
    "MUTED": "#6f6a60",
    "FAINT": "#9a948a",
    "FEN_TEXT": "#4a453d",

    "ACCENT": "#629924",
    "ACCENT_HI": "#76b030",
    "ACCENT_LO": "#55871e",
    "ACCENT_TINT": "#e7efd9",
    "ON_ACCENT": "#ffffff",
    "ACCENT_DISABLED_BG": "#cdd8bc",
    "ACCENT_DISABLED_TEXT": "#839170",

    "GOOD": "#55871e",
    "INACCURACY": "#a08416",
    "DANGER": "#c0392b",
    "WARN": "#a07a17",

    "CHIP_WHITE_BG": "#ffffff",
    "CHIP_WHITE_TEXT": "#3a3733",
    "CHIP_WHITE_BORDER": "#c9c3b8",

    "SIDE_W_BG": "#ffffff",
    "SIDE_W_TEXT": "#3a3733",
    "SIDE_W_BORDER": "#c9c3b8",
    "SIDE_B_BG": "#3a3733",
    "SIDE_B_TEXT": "#f2f0ec",

    "SLIDER_GROOVE": "#c9c3b8",
    "SLIDER_HANDLE": "#8a847a",
    "SLIDER_HANDLE_HI": "#3a3733",
    "STATUS_BORDER_TOP": "#d8d2c7",

    "BOARD_BG": "#e7e1d6",
    "BOARD_FRAME": "#cbc3b6",
    "EVAL_LIGHT": "#f4f2ee",
    "EVAL_DARK": "#57524a",
    "EVAL_CHIP_BG": "#3a3733",
}

PALETTES = {"dark": DARK, "light": LIGHT}


def repolish(widget) -> None:
    """Re-apply the stylesheet after changing a dynamic property."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


_QSS = Template(r"""
QWidget {
    background: $BG;
    color: $TEXT;
    font-family: "Segoe UI", "Noto Sans", "Helvetica Neue", sans-serif;
    font-size: 13px;
}
QMainWindow, QDialog { background: $BG; }
QLabel, QCheckBox, QSlider, QStatusBar, QFrame { background: transparent; }
QWidget[transparent="true"] { background: transparent; }
QToolTip {
    background: $BG_SUNKEN; color: $TEXT;
    border: 1px solid $BORDER; padding: 5px 7px;
}

/* ---------------------------------------------------------- buttons */
QPushButton {
    background: $BG_RAISED;
    border: 1px solid $BORDER;
    border-radius: 7px;
    padding: 6px 14px;
    color: $TEXT;
}
QPushButton:hover  { background: $BG_RAISED_HI; border-color: $BORDER; }
QPushButton:pressed { background: $BG_RAISED_PRESSED; }
QPushButton:disabled { background: $BG_DISABLED; color: $FAINT; border-color: $BORDER_SOFT; }
QPushButton:focus { outline: none; }

QPushButton[accent="true"] {
    background: $ACCENT; border-color: $ACCENT;
    color: $ON_ACCENT; font-weight: 600;
}
QPushButton[accent="true"]:hover  { background: $ACCENT_HI; border-color: $ACCENT_HI; }
QPushButton[accent="true"]:pressed { background: $ACCENT_LO; border-color: $ACCENT_LO; }
QPushButton[accent="true"]:disabled {
    background: $ACCENT_DISABLED_BG; border-color: $ACCENT_DISABLED_BG;
    color: $ACCENT_DISABLED_TEXT;
}

/* small round transport buttons (prev / play / next / exit) */
QPushButton#iconBtn {
    min-width: 30px; max-width: 30px;
    min-height: 30px; max-height: 30px;
    border-radius: 15px;
    font-size: 14px; font-weight: 700;
    padding: 0 0 2px 0;
}

/* clickable moves inside the continuation explorer */
QPushButton#movePill {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 3px 8px;
    color: $TEXT;
    font-weight: 500;
    text-align: left;
}
QPushButton#movePill:hover { background: $BG_RAISED; }
QPushButton#movePill[current="true"] {
    background: $ACCENT; color: $ON_ACCENT; font-weight: 700;
}

/* move-number / '…' cells in the explorer grid (column 0 / placeholder) */
QLabel#moveNum {
    color: $FAINT;
    font-size: 11px;
    font-weight: 700;
    padding: 3px 4px;
}

/* piece palette */
QPushButton#paletteBtn {
    background: $BG_PANEL;
    border: 2px solid transparent;
    border-radius: 10px;
    padding: 3px;
}
QPushButton#paletteBtn:hover { background: $BG_RAISED; }
QPushButton#paletteBtn:checked {
    background: $ACCENT_TINT; border: 2px solid $ACCENT;
}

/* side-to-move chip */
QPushButton#sideChip { border-radius: 13px; padding: 6px 14px; font-weight: 600; }
QPushButton#sideChip[side="w"] { background: $SIDE_W_BG; color: $SIDE_W_TEXT; border: 1px solid $SIDE_W_BORDER; }
QPushButton#sideChip[side="b"] { background: $SIDE_B_BG; color: $SIDE_B_TEXT; border: 1px solid $BORDER; }
QPushButton#sideChip:hover { border-color: $ACCENT; }

/* blunder-check toggle */
QPushButton#blunderBtn:checked {
    background: $ACCENT_TINT; border: 1px solid $ACCENT;
    color: $ACCENT_HI; font-weight: 600;
}

/* ---------------------------------------------------------- inputs */
QLineEdit {
    background: $BG_SUNKEN;
    border: 1px solid $BORDER_INPUT;
    border-radius: 7px;
    padding: 6px 10px;
    selection-background-color: $ACCENT;
    selection-color: $ON_ACCENT;
}
QLineEdit:focus { border: 1px solid $ACCENT; }
QLineEdit#fenEdit {
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 12px; color: $FEN_TEXT;
}

QSlider::groove:horizontal { height: 5px; background: $SLIDER_GROOVE; border-radius: 2px; }
QSlider::sub-page:horizontal { background: $ACCENT; border-radius: 2px; }
QSlider::handle:horizontal {
    width: 15px; height: 15px; margin: -6px 0;
    border-radius: 7px; background: $SLIDER_HANDLE; border: none;
}
QSlider::handle:horizontal:hover { background: $SLIDER_HANDLE_HI; }
QSlider::handle:horizontal:disabled { background: $FAINT; }

QCheckBox { spacing: 7px; }
QCheckBox::indicator {
    width: 16px; height: 16px;
    border-radius: 4px;
    border: 1px solid $BORDER; background: $BG_SUNKEN;
}
QCheckBox::indicator:hover { border-color: $ACCENT; }
QCheckBox::indicator:checked { background: $ACCENT; border-color: $ACCENT; }

QStatusBar { background: $BG_SUNKEN; color: $MUTED; border-top: 1px solid $STATUS_BORDER_TOP; }
QStatusBar::item { border: none; }
QMessageBox { background: $BG; }
QMessageBox QLabel { color: $TEXT; }

/* ---------------------------------------------------------- labels */
QLabel#muted        { color: $MUTED; }
QLabel#statusLabel  { color: $FAINT; font-size: 11px; }
QLabel#panelTitle   { font-size: 15px; font-weight: 700; }
QLabel#caption      { color: $FAINT; font-size: 10px; font-weight: 700; }
QLabel#rankBadge {
    background: $BG_RAISED; color: $MUTED;
    border-radius: 11px; font-weight: 700; font-size: 11px;
}
QLabel#rankBadge[first="true"] { background: $ACCENT_TINT; color: $ACCENT_HI; }
QLabel#lineMove     { font-size: 14px; font-weight: 700; }
QLabel#linePreview  { color: $FAINT; font-size: 11px; }
QLabel#lineDepth    { color: $FAINT; font-size: 11px; }
QLabel#explorerTitle { font-weight: 700; font-size: 12px; }
QLabel#plyLabel     { color: $MUTED; font-size: 11px; }
QLabel#blunderResult {
    background: $BG_PANEL; border-radius: 8px;
    padding: 8px 10px; font-size: 12px;
}
QLabel#blunderResult[tone="good"]       { color: $GOOD; }
QLabel#blunderResult[tone="inaccuracy"] { color: $INACCURACY; }
QLabel#blunderResult[tone="mistake"]    { color: $WARN; }
QLabel#blunderResult[tone="blunder"]    { color: $DANGER; font-weight: 700; }

/* eval chip on each engine line */
QLabel#evalChip { border-radius: 9px; padding: 2px 8px; font-weight: 700; font-size: 11px; }
QLabel#evalChip[tone="white"] { background: $CHIP_WHITE_BG; color: $CHIP_WHITE_TEXT; border: 1px solid $CHIP_WHITE_BORDER; }
QLabel#evalChip[tone="black"] { background: $SIDE_B_BG; color: $SIDE_B_TEXT; border: 1px solid $BORDER; }
QLabel#evalChip[tone="even"]  { background: $BG_RAISED; color: $TEXT; }

/* ---------------------------------------------------------- cards */
QFrame#lineCard {
    background: $BG_PANEL;
    border: 1px solid transparent;
    border-radius: 10px;
}
QFrame#lineCard:hover { background: $BG_PANEL_HI; border: 1px solid $BORDER; }
QFrame#lineCard[selected="true"] { background: $ACCENT_TINT; border: 1px solid $ACCENT; }

QFrame#explorer {
    background: $BG_PANEL;
    border: 1px solid $BORDER_SOFT;
    border-radius: 10px;
}

QFrame#vSep { background: $BORDER_SOFT; max-width: 1px; }
""")


def _bind(pal: dict) -> None:
    """Expose a palette's colours as module-level names (theme.BG, ...)."""
    for key, value in pal.items():
        globals()[key] = value


_bind(DARK)

_current_name = "dark"


def current() -> str:
    """Name of the active palette ("dark" or "light")."""
    return _current_name


def build_qss(pal: dict | None = None) -> str:
    """Render the stylesheet for a palette (default: the active one)."""
    return _QSS.substitute(pal if pal is not None else PALETTES[_current_name])


APP_QSS = build_qss()


def apply(name: str, app=None) -> str:
    """Switch to the palette ``name`` ("dark" | "light").

    Rebinds every module-level colour (so the board and eval bar repaint
    correctly), rebuilds the QSS and — when ``app`` is given — re-applies it
    to the whole application. Returns the name that is now active.
    """
    global APP_QSS, _current_name
    if name not in PALETTES:
        raise ValueError(f"unknown theme {name!r}; choose from {sorted(PALETTES)}")
    _current_name = name
    pal = PALETTES[name]
    _bind(pal)
    APP_QSS = build_qss()
    if app is not None:
        app.setStyleSheet(APP_QSS)
    return name
