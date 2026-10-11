"""Tema escuro da interface, inspirado em dashboards Bootstrap."""

FOLHA = """
* { outline: none; }
QWidget {
    background: #0e1320;
    color: #d7e0ee;
    font-family: "Segoe UI", "Noto Sans";
    font-size: 10pt;
}
QMainWindow, QDialog { background: #0e1320; }
QStatusBar { background: #101726; color: #8ea3c8; border-top: 1px solid #1e2a44; }
QStatusBar QLabel { color: #8ea3c8; padding: 0 6px; }

QGroupBox {
    border: 1px solid #1e2a44;
    border-radius: 8px;
    margin-top: 14px;
    padding: 12px 8px 8px 8px;
    background: #101726;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: #7aa2f7;
}

QPushButton {
    background: #1d2638;
    border: 1px solid #2b3b5e;
    border-radius: 6px;
    padding: 7px 16px;
    color: #d7e0ee;
    font-weight: 500;
}
QPushButton:hover { background: #24314c; border-color: #3b82f6; }
QPushButton:pressed { background: #182138; }
QPushButton:disabled { color: #5b6780; background: #161d2e; border-color: #22304c; }
QPushButton#btnIniciar { background: #123524; border-color: #16a34a; font-weight: 700; }
QPushButton#btnIniciar:hover { background: #17492f; border-color: #22c55e; }
QPushButton#btnIniciar:disabled { background: #14202a; border-color: #22304c; color: #5b6780; }
QPushButton#btnPausar { background: #3a2b10; border-color: #d97706; font-weight: 600; }
QPushButton#btnPausar:hover { background: #4a3613; border-color: #f59e0b; }
QPushButton#btnEmergencia {
    background: #64151a;
    border-color: #ef4444;
    font-weight: 800;
    padding: 7px 20px;
}
QPushButton#btnEmergencia:hover { background: #7f1d1d; border-color: #f87171; }
QPushButton#btnAplicar { background: #142a52; border-color: #3b82f6; font-weight: 600; }
QPushButton#btnAplicar:hover { background: #1a3768; }

QLineEdit, QDoubleSpinBox, QComboBox {
    background: #0f1524;
    border: 1px solid #26334f;
    border-radius: 6px;
    padding: 4px 8px;
    selection-background-color: #1e3a8a;
    selection-color: #ffffff;
}
QLineEdit:focus, QDoubleSpinBox:focus, QComboBox:focus { border-color: #3b82f6; }
QLineEdit:disabled, QDoubleSpinBox:disabled, QComboBox:disabled { color: #5b6780; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background: #101726;
    border: 1px solid #26334f;
    selection-background-color: #1e3a8a;
}
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
    background: #1d2638; border: 1px solid #26334f; width: 16px;
}

QTreeWidget, QTableWidget, QListWidget {
    background: #0f1524;
    alternate-background-color: #121a2c;
    gridline-color: #1c2740;
    border: 1px solid #1e2a44;
    border-radius: 6px;
    selection-background-color: #1e3a8a;
    selection-color: #ffffff;
}
QHeaderView::section {
    background: #16203a;
    color: #9db3d8;
    border: none;
    border-right: 1px solid #1e2a44;
    padding: 5px 6px;
    font-weight: 600;
}
QTableCornerButton::section { background: #16203a; border: none; }

QSplitter::handle { background: #22304c; }
QSplitter::handle:horizontal { width: 3px; }
QSplitter::handle:vertical { height: 3px; }
QSplitter::handle:hover { background: #3b82f6; }

QTabWidget::pane {
    border: 1px solid #1e2a44;
    border-radius: 6px;
    top: -1px;
    background: #0e1320;
}
QTabBar::tab {
    background: #121a2c;
    color: #8ea3c8;
    padding: 8px 20px;
    margin-right: 2px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    font-weight: 600;
}
QTabBar::tab:selected { background: #1a2440; color: #e6edf7; border-bottom: 2px solid #3b82f6; }
QTabBar::tab:hover { color: #b7c9e8; }

QLabel { background: transparent; }
QLabel#chipEstado {
    border-radius: 10px;
    padding: 3px 14px;
    font-weight: 700;
    color: #ffffff;
    background: #334155;
}
QLabel#chipParado { background: #b45309; }
QLabel#chipAtivo { background: #15803d; }
QLabel#chipErro { background: #b91c1c; }
QLabel#chipEmergencia { background: #b91c1c; }
QLabel.rotulo { color: #8ea3c8; font-size: 9pt; }
QLabel.valor { color: #e6edf7; font-weight: 600; font-size: 11pt; }
QLabel.valorPositivo { color: #22c55e; font-weight: 600; font-size: 11pt; }
QLabel.valorNegativo { color: #ef4444; font-weight: 600; font-size: 11pt; }

QCheckBox::indicator, QRadioButton::indicator {
    width: 14px; height: 14px;
    border: 1px solid #26334f;
    background: #0f1524;
    border-radius: 3px;
}
QRadioButton::indicator { border-radius: 8px; }
QCheckBox::indicator:checked, QRadioButton::indicator:checked {
    background: #3b82f6;
    border-color: #60a5fa;
}

QScrollBar:vertical {
    background: #101726; width: 10px; margin: 0;
}
QScrollBar::handle:vertical { background: #26334f; border-radius: 5px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: #3b82f6; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: #101726; height: 10px; margin: 0; }
QScrollBar::handle:horizontal { background: #26334f; border-radius: 5px; min-width: 24px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

QMessageBox { background: #101726; }
QMessageBox QLabel { color: #d7e0ee; }
QMenu { background: #101726; border: 1px solid #26334f; }
QMenu::item { padding: 5px 24px; }
QMenu::item:selected { background: #1e3a8a; color: #ffffff; }
QToolTip { background: #16203a; color: #d7e0ee; border: 1px solid #26334f; }
"""
