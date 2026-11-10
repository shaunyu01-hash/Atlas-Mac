"""Dark mode: readable in both modes, remembered, and switching never disturbs work on the screen."""
from __future__ import annotations

import re

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from atlaspo import orders, pdf
from atlaspo.ui import theme
from atlaspo.ui.main_window import MainWindow


def contrast(a: str, b: str) -> float:
    def lum(text):
        c = QColor(text)
        return sum(w * (v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4) for w, v in zip((0.2126, 0.7152, 0.0722), (c.redF(), c.greenF(), c.blueF())))
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def themed(app, tmp_path, monkeypatch):
    store = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(theme, "_settings", lambda: store)
    yield store
    theme.apply(theme.LIGHT_MODE)
    app.setStyleSheet("")


@pytest.fixture
def window(themed, world):
    w = MainWindow(world.conn, world.admin)
    yield w
    w.deleteLater()


@pytest.mark.parametrize("mode", theme.PALETTES)
def test_text_is_readable_in_both_modes(mode):
    p = theme.PALETTES[mode]
    pairs = [(p.TEXT, s) for s in (p.BG, p.PANEL, p.HEADER_BG, p.ACCENT_PALE, p.BRANCH_TINT, p.GRAY_PALE)]
    pairs += [(p.MUTED, s) for s in (p.BG, p.PANEL, p.GRAY_PALE)]
    pairs += [(p.FAINT, s) for s in (p.BG, p.PANEL)]
    pairs += [(p.ACCENT_TEXT, s) for s in (p.BG, p.PANEL, p.ACCENT_PALE)]
    pairs += [(p.RED, s) for s in (p.BG, p.PANEL, p.RED_PALE)]
    pairs += [(p.AMBER, p.AMBER_PALE), (p.GREEN, p.GREEN_PALE), (p.BLUE, p.BLUE_PALE), (p.TOOLTIP_TEXT, p.TOOLTIP)]
    pairs += [("#FFFFFF", p.ACCENT), ("#FFFFFF", p.ACCENT_HOVER)]
    failing = [(a, b, round(contrast(a, b), 2)) for a, b in pairs if contrast(a, b) < 4.5]
    assert failing == []


@pytest.mark.parametrize("mode", theme.PALETTES)
def test_focus_outline_and_controls_can_be_seen(mode):
    p = theme.PALETTES[mode]
    assert contrast(p.ACCENT, p.PANEL) >= 3


def test_both_palettes_have_the_same_roles():
    assert vars(theme.LIGHT).keys() == vars(theme.DARK).keys()


def test_dark_stylesheet_has_no_leftover_light_colour():
    sheet = theme.stylesheet(theme.DARK)
    allowed = {v.upper() for v in vars(theme.DARK).values()}
    leftover = {c.upper() for c in re.findall(r"#[0-9A-Fa-f]{6}", sheet)} - allowed
    assert leftover == set()
    assert (theme.ASSETS / theme.DARK.ARROW).exists()


def test_every_status_has_a_chip_in_both_modes(themed):
    for mode in theme.PALETTES:
        theme.apply(mode)
        for code in (*orders.STATUS_LABELS, "UNKNOWN"):
            fg, bg = theme.status_colors(code)
            assert contrast(fg, bg) >= 4.5, (mode, code)


def test_light_is_the_default_and_the_choice_is_remembered(themed):
    assert theme.saved_mode() == theme.LIGHT_MODE
    theme.save_mode(theme.DARK_MODE)
    assert theme.saved_mode() == theme.DARK_MODE
    themed.setValue("appearance/mode", "nonsense")
    assert theme.saved_mode() == theme.LIGHT_MODE


def test_button_switches_everything_and_back(window, themed, app):
    assert window.theme_button.text() == "Dark mode"
    window.theme_button.click()
    assert theme.is_dark() and window.theme_button.text() == "Light mode"
    assert theme.saved_mode() == theme.DARK_MODE
    assert app.palette().color(app.palette().ColorRole.Base) == QColor(theme.DARK.PANEL)
    window.theme_button.click()
    assert not theme.is_dark() and theme.saved_mode() == theme.LIGHT_MODE


def test_switching_keeps_what_is_typed_and_repaints_cells(window, world):
    window.new_of()
    page = window.of_page
    page.grid.setRowCount(0)
    page._append({"department_id": 1, "description": "Unsaved work", "unit_cost_c": 12300, "allocations_m": {2: 7000}})
    page.remarks.setText("Not saved yet")
    page._recalculate()
    branch_col = page.branch_cols[2]
    window.theme_button.click()
    assert page.grid.item(0, 2).text() == "Unsaved work"
    assert page.grid.item(0, branch_col).text() == "7"
    assert page.remarks.text() == "Not saved yet"
    assert page.grid.item(0, branch_col).background().color() == QColor(theme.DARK.BRANCH_TINT)


def test_status_label_follows_the_mode(window, world):
    window.open_po(world.po(100))
    window.theme_button.click()
    fg, bg = theme.status_colors("APPROVED")
    assert fg == theme.DARK.GREEN and bg in window.po_page.status.styleSheet()


def test_a_new_window_after_logging_out_starts_in_the_right_mode(window, world):
    window.theme_button.click()
    again = MainWindow(world.conn, world.admin)
    assert again.theme_button.text() == "Light mode"
    again.deleteLater()


def test_documents_stay_light_for_printing(window, world):
    """PDFs have their own colours and never read the screen's theme."""
    window.theme_button.click()
    assert pdf.render_supplier_po(world.conn, world.po(100)).startswith(b"%PDF")
    assert not hasattr(pdf, "theme")
    assert pdf.INK == pdf.colors.HexColor("#1D2329")
