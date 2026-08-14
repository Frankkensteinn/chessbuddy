"""ChessBuddy entry point."""
from __future__ import annotations

import sys

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication

from . import theme
from .main_window import MainWindow


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("ChessBuddy")
    app.setOrganizationName("chessbuddy")
    app.setStyle("Fusion")                 # predictable base for the QSS
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(theme.APP_QSS)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
