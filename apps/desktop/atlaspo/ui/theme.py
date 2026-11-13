"""Design tokens, so every screen reads as one family.

Colour goes on data, not chrome: status chips and the branch columns carry it, and one indigo marks what is active.
Nothing animates. People here repeat these actions all day from the keyboard, so every response is immediate.
Light is the family look and the default. Dark uses the same roles, so every screen works in either. PDFs always stay light.
"""
import re
import sys
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

ASSETS = Path(__file__).parent / "assets"

MAC = sys.platform == "darwin"

# A Mac has no Segoe UI, so it uses its own system font. Qt on a Mac counts 72 points to the inch where Windows counts 96, so a size
# in pt would come out a quarter smaller there. stylesheet() turns every pt into the pixels Windows would draw, and the screens look alike.
FONT = "Helvetica Neue" if MAC else "Segoe UI Variable Text"
FONT_DISPLAY = "Helvetica Neue" if MAC else "Segoe UI Variable Display"


def pt(size: float) -> float:
    """A point size in code (QFont.setPointSizeF) that draws as large as it does on Windows."""
    return size * 96 / 72 if MAC else size

LIGHT_MODE, DARK_MODE = "light", "dark"

LIGHT = SimpleNamespace(
    BG="#F4F5F9", PANEL="#FFFFFF", BORDER="#E2E4EE", CONTROL="#858BA0", HEADER_BG="#F1F2F8",
    TEXT="#1B1F33", MUTED="#565C75", FAINT="#676D85",
    ACCENT="#4338CA", ACCENT_HOVER="#3730A3", ACCENT_TEXT="#3730A3", ACCENT_PALE="#E6E8FB",
    RED="#B42318", RED_PALE="#FDECEA", AMBER="#B54708", AMBER_PALE="#FEF3E2",
    GREEN="#1E7A46", GREEN_PALE="#E6F4EC", BLUE="#1F5FA8", BLUE_PALE="#E7F0FA",
    GRAY_PALE="#ECEEF4", BRANCH_TINT="#EFF0FD", ROW_RULE="#EBEDF3", COL_TINT="#EFF1FA",
    HOVER="#F0F1F7", PRESSED="#E5E7F0", PRIMARY_OFF="#B4B1EC", PRIMARY_OFF_TEXT="#3B3690", DANGER_LINE="#E5B5B0", SCROLL="#C8CBD8",
    TOOLTIP="#1B1F33", TOOLTIP_TEXT="#FFFFFF", ARROW="down.svg", EDITABLE="#8984DB",
    SIDE_BG="#1B2140", SIDE_BORDER="#1B2140", SIDE_TEXT="#E7E9F5", SIDE_MUTED="#A9AECB", SIDE_HEAD="#9097B8", SIDE_ICON="#A9AECB",
    SIDE_HOVER="#262D52", SIDE_SEL_BG="#4338CA", SIDE_SEL_TEXT="#FFFFFF", SIDE_BUTTON="#232A4D", SIDE_BUTTON_LINE="#3E4675",
    SIDE_BUTTON_HOVER="#2C3459", SIDE_ALERT="#FCA5A5", SIDE_FOCUS="#C7CCF5",
)
DARK = SimpleNamespace(
    BG="#12141F", PANEL="#1A1D2B", BORDER="#2A2E42", CONTROL="#6A7090", HEADER_BG="#21253A",
    TEXT="#E6E8F2", MUTED="#A4A9C2", FAINT="#9398B3",
    ACCENT="#5D5AD6", ACCENT_HOVER="#4F4BC4", ACCENT_TEXT="#A9B6FC", ACCENT_PALE="#2A2E5A",
    RED="#F58E84", RED_PALE="#3F211E", AMBER="#F2AE69", AMBER_PALE="#3B2B16",
    GREEN="#72D09A", GREEN_PALE="#173522", BLUE="#8DBCF1", BLUE_PALE="#182E48",
    GRAY_PALE="#2A2E40", BRANCH_TINT="#22264A", ROW_RULE="#262A3B", COL_TINT="#23273B",
    HOVER="#242838", PRESSED="#2D3245", PRIMARY_OFF="#2E3160", PRIMARY_OFF_TEXT="#A4A9C2", DANGER_LINE="#74403A", SCROLL="#474D66",
    TOOLTIP="#E6E8F2", TOOLTIP_TEXT="#12141F", ARROW="down-dark.svg", EDITABLE="#615DB1",
    SIDE_BG="#0E1020", SIDE_BORDER="#262A3B", SIDE_TEXT="#E6E8F2", SIDE_MUTED="#A4A9C2", SIDE_HEAD="#8A90AD", SIDE_ICON="#A4A9C2",
    SIDE_HOVER="#1C2036", SIDE_SEL_BG="#4F4BC4", SIDE_SEL_TEXT="#FFFFFF", SIDE_BUTTON="#171A2E", SIDE_BUTTON_LINE="#343957",
    SIDE_BUTTON_HOVER="#20243C", SIDE_ALERT="#F58E84", SIDE_FOCUS="#C7CCF5",
)
# The sidebar is deep navy in both modes, with the active page as an indigo pill. COL_TINT shades every other column of a list (common.tint_columns).
# CONTROL, the edge of every box and button, and the dimmed main button's text stand out at least 3 to 1 from what's around them.
# EDITABLE is the cell to type in, at least 3 to 1 from a normal row in light and as near as dark allows (2.95) while its text stays easy to read (4.5 to 1).
PALETTES = {LIGHT_MODE: LIGHT, DARK_MODE: DARK}


def __getattr__(name: str):
    """theme.EDITABLE is the current mode's color for a cell to type in, the same as theme.current.EDITABLE."""
    if name == "EDITABLE":
        return current.EDITABLE
    raise AttributeError(name)


# One set for both modes: solid enough that white initials read on each, so a person is the same color everywhere.
# New people take these in order and usually arrive in pairs, so neighbors here are far apart. A saved color is its place in this list: add to the end, never reorder.
PERSON_COLORS = (
    ("Blue", "#1D4ED8"), ("Orange", "#C2410C"), ("Green", "#15803D"), ("Pink", "#BE185D"), ("Teal", "#0F766E"), ("Amber", "#A16207"),
    ("Indigo", "#4338CA"), ("Red", "#B42318"), ("Olive", "#4D7C0F"), ("Purple", "#7E22CE"), ("Cyan", "#0E7490"), ("Stone", "#57534E"),
)

current = LIGHT


def status_colors(code: str) -> tuple[str, str]:
    """Text and background of a status chip, in the current mode."""
    p = current
    return {
        "DRAFT": (p.MUTED, p.GRAY_PALE),
        "PENDING_APPROVAL": (p.AMBER, p.AMBER_PALE),
        "APPROVED": (p.GREEN, p.GREEN_PALE),
        "ISSUED": (p.BLUE, p.BLUE_PALE),
        "PARTIALLY_DELIVERED": (p.BLUE, p.BLUE_PALE),
        "FULLY_DELIVERED": (p.ACCENT_TEXT, p.ACCENT_PALE),
        "CLOSED": (p.MUTED, p.GRAY_PALE),
        "CANCELLED": (p.RED, p.RED_PALE),
    }.get(code, (p.TEXT, p.GRAY_PALE))


def is_dark() -> bool:
    return current is DARK


def _settings() -> QSettings:
    """Belongs to the Windows user, not the database: it isn't a business record, so it needs no audit or backup."""
    return QSettings("Atlas PO ERP", "PO System")  # changing these names would lose everyone's saved columns and choices


def saved_mode() -> str:
    mode = _settings().value("appearance/mode", LIGHT_MODE)
    return mode if mode in PALETTES else LIGHT_MODE


def save_mode(mode: str) -> None:
    _settings().setValue("appearance/mode", mode)


def _palette(p: SimpleNamespace) -> QPalette:
    """For what the stylesheet doesn't reach, such as message boxes and the calendar popup."""
    pal = QPalette()
    roles = {
        QPalette.ColorRole.Window: p.BG, QPalette.ColorRole.WindowText: p.TEXT, QPalette.ColorRole.Base: p.PANEL,
        QPalette.ColorRole.AlternateBase: p.HEADER_BG, QPalette.ColorRole.Text: p.TEXT, QPalette.ColorRole.Button: p.PANEL,
        QPalette.ColorRole.ButtonText: p.TEXT, QPalette.ColorRole.ToolTipBase: p.TOOLTIP, QPalette.ColorRole.ToolTipText: p.TOOLTIP_TEXT,
        QPalette.ColorRole.Highlight: p.ACCENT, QPalette.ColorRole.HighlightedText: "#FFFFFF", QPalette.ColorRole.PlaceholderText: p.FAINT,
        QPalette.ColorRole.Link: p.ACCENT_TEXT,
    }
    for role, color in roles.items():
        pal.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(p.FAINT))
    return pal


def apply(mode: str) -> None:
    """Switch the whole program. Open screens keep what's typed in them; see MainWindow._retheme for what paints its own colour."""
    global current
    current = PALETTES[mode]
    app = QApplication.instance()
    # Also what makes Windows draw a dark title bar.
    QGuiApplication.styleHints().setColorScheme(Qt.ColorScheme.Dark if mode == DARK_MODE else Qt.ColorScheme.Light)
    app.setPalette(_palette(current))
    app.setStyleSheet(stylesheet(current))


def stylesheet(p: SimpleNamespace) -> str:
    sheet = _stylesheet(p)
    if MAC:
        sheet = re.sub(r"(\d+(?:\.\d+)?)pt\b", lambda m: f"{round(float(m.group(1)) * 96 / 72)}px", sheet)
    return sheet


def _stylesheet(p: SimpleNamespace) -> str:
    return f"""
QWidget {{ color: {p.TEXT}; font-family: "{FONT}"; font-size: 10pt; }}
QMainWindow, #Main, QDialog {{ background: {p.BG}; }}
#Sidebar {{ background: {p.SIDE_BG}; border-right: 1px solid {p.SIDE_BORDER}; }}
#Sidebar QLabel {{ color: {p.SIDE_TEXT}; background: transparent; }}
#Sidebar QLabel#Muted, #Sidebar QLabel#BrandSub {{ color: {p.SIDE_MUTED}; }}
#Sidebar QLabel#Error {{ color: {p.SIDE_ALERT}; }}
#Sidebar QPushButton {{ background: {p.SIDE_BUTTON}; color: {p.SIDE_TEXT}; border: 1px solid {p.SIDE_BUTTON_LINE}; }}
#Sidebar QPushButton:hover {{ background: {p.SIDE_BUTTON_HOVER}; }}
#Sidebar QPushButton:focus {{ border: 1px solid {p.SIDE_FOCUS}; }}
#Sidebar QPushButton#Count:checked {{ background: {p.SIDE_SEL_BG}; color: {p.SIDE_SEL_TEXT}; }}
#Brand {{ font-family: "{FONT_DISPLAY}"; font-size: 11.5pt; font-weight: 600; color: {p.SIDE_TEXT}; }}
#BrandSub {{ color: {p.MUTED}; font-size: 8.5pt; }}
#StepDone {{ color: {p.ACCENT_TEXT}; font-size: 9pt; }}
#StepNow {{ color: white; background: {p.ACCENT}; border-radius: 4px; font-size: 9pt; font-weight: 600; }}
#StepNext {{ color: {p.FAINT}; font-size: 9pt; }}
#StepLine {{ color: {p.SCROLL}; font-size: 9pt; }}  /* quieter than the next step's text, as before */
#Empty {{ color: {p.MUTED}; font-size: 10pt; padding: 28px; }}
#Muted {{ color: {p.MUTED}; font-size: 9pt; }}
#Warn {{ color: {p.AMBER}; font-size: 9pt; }}
#Error {{ color: {p.RED}; font-size: 9pt; }}
#Title {{ font-family: "{FONT_DISPLAY}"; font-size: 17pt; font-weight: 600; }}
#Panel {{ background: {p.PANEL}; border: 1px solid {p.BORDER}; border-radius: 6px; }}
#PanelTitle {{ font-size: 10.5pt; font-weight: 600; }}
#Total {{ font-family: "{FONT_DISPLAY}"; font-size: 14pt; font-weight: 600; }}
#Notice {{ background: {p.AMBER_PALE}; color: {p.AMBER}; border-radius: 4px; padding: 6px 10px; }}
#NoticeOk {{ background: {p.GREEN_PALE}; color: {p.GREEN}; border-radius: 4px; padding: 6px 10px; }}
#NoticeBad {{ background: {p.RED_PALE}; color: {p.RED}; border-radius: 4px; padding: 6px 10px; }}

QListWidget#Nav {{ background: transparent; border: none; outline: 0; }}
QListWidget#Nav::item {{ color: {p.SIDE_TEXT}; padding: 7px 10px; border-radius: 5px; margin: 1px 4px; }}
QListWidget#Nav::item:hover {{ background: {p.SIDE_HOVER}; }}
QListWidget#Nav::item:selected {{ background: {p.SIDE_SEL_BG}; color: {p.SIDE_SEL_TEXT}; font-weight: 600; }}
QListWidget#Nav::item:disabled {{ background: transparent; color: {p.SIDE_HEAD}; font-size: 8.5pt; font-weight: 600; padding: 12px 10px 3px 10px; }}

QPushButton {{ background: {p.PANEL}; border: 1px solid {p.CONTROL}; border-radius: 5px; padding: 6px 14px; }}
QPushButton:hover {{ background: {p.HOVER}; }}
QPushButton:pressed {{ background: {p.PRESSED}; }}
QPushButton:focus {{ border: 1px solid {p.ACCENT}; }}
QPushButton:disabled {{ color: {p.FAINT}; background: {p.BG}; }}
QPushButton#Primary {{ background: {p.ACCENT}; color: white; border-color: {p.ACCENT}; font-weight: 600; }}
QPushButton#Primary:hover {{ background: {p.ACCENT_HOVER}; border-color: {p.ACCENT_HOVER}; }}
QPushButton#Primary:disabled {{ background: {p.PRIMARY_OFF}; border-color: {p.PRIMARY_OFF}; color: {p.PRIMARY_OFF_TEXT}; }}
QPushButton#Danger {{ color: {p.RED}; border-color: {p.DANGER_LINE}; }}
QPushButton#Danger:hover {{ background: {p.RED_PALE}; }}
QPushButton#Count {{ background: transparent; border: none; border-radius: 4px; padding: 5px 10px; color: {p.MUTED}; }}
QPushButton#Count:hover {{ background: {p.GRAY_PALE}; }}
QPushButton#Count:checked {{ background: {p.ACCENT_PALE}; color: {p.ACCENT_TEXT}; }}

QLineEdit, QComboBox, QDateEdit, QSpinBox, QPlainTextEdit {{ background: {p.PANEL}; border: 1px solid {p.CONTROL};
    border-radius: 5px; padding: 4px 6px; min-height: 20px; selection-background-color: {p.ACCENT}; selection-color: white; }}
QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QSpinBox:focus, QPlainTextEdit:focus {{ border: 1px solid {p.ACCENT}; }}
QLineEdit:read-only, QPlainTextEdit:read-only {{ background: {p.BG}; color: {p.MUTED}; }}
QLineEdit:disabled, QComboBox:disabled, QDateEdit:disabled, QPlainTextEdit:disabled {{ background: {p.BG}; color: {p.MUTED}; }}

QComboBox::drop-down, QDateEdit::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow, QDateEdit::down-arrow {{ image: url(ICONS/{p.ARROW}); width: 10px; height: 6px; }}
QComboBox QAbstractItemView {{ background: {p.PANEL}; border: 1px solid {p.CONTROL}; selection-background-color: {p.ACCENT_PALE}; selection-color: {p.TEXT}; outline: 0; }}

QTableWidget, QTableView, QTreeWidget {{ background: {p.PANEL}; alternate-background-color: {p.PANEL}; border: 1px solid {p.BORDER}; border-radius: 6px;
    gridline-color: {p.ROW_RULE}; selection-background-color: {p.ACCENT_PALE}; selection-color: {p.TEXT}; outline: 0; }}
QTableWidget::item, QTableView::item, QTreeWidget::item {{ padding: 3px 6px; border-bottom: 1px solid {p.ROW_RULE}; }}
QTableWidget::item:selected, QTableView::item:selected, QTreeWidget::item:selected {{ background: {p.ACCENT_PALE}; color: {p.TEXT}; }}
QTableWidget::item:focus {{ border: 1px solid {p.ACCENT}; }}
QTableWidget#Footer {{ background: {p.HEADER_BG}; font-weight: 600; }}
QTableWidget#Footer::item {{ border-bottom: none; }}
QTableWidget#Footer QHeaderView::section {{ background: {p.HEADER_BG}; border: none; }}
QHeaderView::section:vertical {{ color: {p.FAINT}; font-weight: 400; border: none; border-bottom: 1px solid {p.ROW_RULE}; padding: 0 4px; }}
QHeaderView {{ background: {p.PANEL}; border: none; }}
QHeaderView::section {{ background: {p.PANEL}; color: {p.MUTED}; font-size: 9pt; font-weight: 600; border: none;
    border-bottom: 1px solid {p.CONTROL}; padding: 6px; }}
QTableWidget QLineEdit {{ border: 1px solid {p.ACCENT}; border-radius: 0; padding: 0 4px; }}

QTabWidget::pane {{ border: 1px solid {p.BORDER}; border-radius: 6px; background: {p.PANEL}; top: -1px; }}
QTabBar::tab {{ background: transparent; padding: 7px 14px; color: {p.MUTED}; border-bottom: 2px solid transparent; }}
QTabBar::tab:hover {{ color: {p.TEXT}; }}
QTabBar::tab:selected {{ color: {p.ACCENT_TEXT}; border-bottom: 2px solid {p.ACCENT}; font-weight: 600; }}
QCheckBox {{ spacing: 6px; }}
QCheckBox::indicator, QTableWidget::indicator, QListWidget::indicator {{ width: 15px; height: 15px; border: 1px solid {p.CONTROL}; border-radius: 4px; background: {p.PANEL}; }}
QCheckBox::indicator:hover, QTableWidget::indicator:hover, QListWidget::indicator:hover {{ border-color: {p.ACCENT}; }}
QCheckBox::indicator:checked, QTableWidget::indicator:checked, QListWidget::indicator:checked {{ background: {p.ACCENT}; border-color: {p.ACCENT}; image: url(ICONS/icons/check.svg); }}
QCheckBox::indicator:disabled {{ background: {p.BG}; border-color: {p.BORDER}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {p.SCROLL}; border-radius: 4px; min-height: 24px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {p.SCROLL}; border-radius: 4px; min-width: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background: none; border: none; width: 0; height: 0; }}
QToolTip {{ background: {p.TOOLTIP}; color: {p.TOOLTIP_TEXT}; border: none; padding: 4px 6px; }}
QTableCornerButton::section {{ background: {p.PANEL}; border: none; }}
QTextEdit {{ background: {p.PANEL}; border: 1px solid {p.CONTROL}; border-radius: 5px; }}
QMenu {{ background: {p.PANEL}; border: 1px solid {p.CONTROL}; padding: 4px; }}
QMenu::item {{ padding: 5px 18px; border-radius: 4px; }}
QMenu::item:selected {{ background: {p.ACCENT_PALE}; color: {p.TEXT}; }}
QMenu::item:disabled {{ color: {p.FAINT}; }}
QMenu::separator {{ height: 1px; background: {p.BORDER}; margin: 4px 6px; }}
QCheckBox#MenuCheck {{ padding: 5px 18px 5px 10px; }}
QCalendarWidget QWidget#qt_calendar_navigationbar {{ background: {p.HEADER_BG}; }}
QCalendarWidget QToolButton {{ background: transparent; color: {p.TEXT}; border: none; padding: 3px 6px; }}
QCalendarWidget QToolButton:hover {{ background: {p.HOVER}; border-radius: 4px; }}
QCalendarWidget QAbstractItemView {{ background: {p.PANEL}; color: {p.TEXT}; selection-background-color: {p.ACCENT}; selection-color: white; outline: 0; border: none; }}
QCalendarWidget QAbstractItemView::item {{ padding: 0; border: none; }}
QCalendarWidget QAbstractItemView::item:selected {{ background: {p.ACCENT}; color: white; }}
""".replace("ICONS", ASSETS.as_posix())
