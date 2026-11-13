"""Small shared pieces: labels, table helpers, status cell, messages, dates."""
from __future__ import annotations

import os
import platform
import re
import sqlite3
import sys
import tempfile
import traceback
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

from PySide6.QtCore import QDate, QEvent, QObject, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QIcon, QPageSize, QPainter, QPixmap
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPrintSupport import QAbstractPrintDialog, QPrintDialog, QPrinter
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QCompleter, QDateEdit, QDialog, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QMenu, QTabBar,
                               QInputDialog, QLabel, QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy, QStyle, QStyledItemDelegate, QTableWidget, QTableWidgetItem,
                               QTreeWidget, QVBoxLayout, QWidget, QWidgetAction)

from .. import pdf
from ..db import BUSY_MESSAGE, POError, data_dir
from ..db import _is_busy as _busy_text
from ..masters import NO_TERMS, PAYMENT_TERMS
from ..orders import STATUS_LABELS
from . import theme
from .theme import ASSETS

STATUS_ROLE = Qt.ItemDataRole.UserRole + 1
CHIP_ROLE = Qt.ItemDataRole.UserRole + 2
# A row whose first cell holds True here, such as a Total row, stays last whichever way the list is sorted.
PIN_BOTTOM_ROLE = Qt.ItemDataRole.UserRole + 40


class StatusDelegate(QStyledItemDelegate):
    """Draws a status as a small chip that hugs its label, so a column of them scans without filling whole cells."""

    def paint(self, painter, option, index):
        code = index.data(STATUS_ROLE)
        if code is None:
            return super().paint(painter, option, index)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, QColor(theme.current.ACCENT_PALE))
        fg, bg = theme.status_colors(code)
        text = index.data() or ""
        width = min(option.fontMetrics.horizontalAdvance(text) + 14, option.rect.width() - 8)
        chip = QRectF(option.rect.left() + 4, option.rect.center().y() - 10, width, 20)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(bg))
        painter.drawRoundedRect(chip, 4, 4)
        painter.setPen(QColor(fg))
        painter.drawText(chip, Qt.AlignmentFlag.AlignCenter, text)
        painter.setPen(QColor(theme.current.ROW_RULE))  # this delegate paints the whole cell, so the row's rule too
        painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())
        painter.restore()


class PersonDelegate(QStyledItemDelegate):
    """Draws a person as their initials on their own color. The full name shows on hover, or beside the chip when named."""

    def __init__(self, parent=None, named: bool = False):
        super().__init__(parent)
        self.named = named

    def paint(self, painter, option, index):
        person = index.data(CHIP_ROLE)
        if person is None:
            return super().paint(painter, option, index)
        initials, color = person
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, QColor(theme.current.ACCENT_PALE))
        width = max(option.fontMetrics.horizontalAdvance(initials) + 14, 30)
        chip = QRectF(option.rect.left() + 4, option.rect.center().y() - 10, width, 20)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.PERSON_COLORS[color][1]))
        painter.drawRoundedRect(chip, 4, 4)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(chip, Qt.AlignmentFlag.AlignCenter, initials)
        if self.named:
            room = option.rect.adjusted(width + 12, 0, -4, 0)
            painter.setPen(QColor(theme.current.TEXT))
            painter.drawText(room, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, option.fontMetrics.elidedText(index.data() or "", Qt.TextElideMode.ElideRight, room.width()))
        painter.setPen(QColor(theme.current.ROW_RULE))  # this delegate paints the whole cell, so the row's rule too
        painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())
        painter.restore()


def show_people(table: QTableWidget, col: int, chips: dict, user_ids: list, named: bool = False) -> None:
    """Turns a filled column of names into chips, one user id per row. A row with nobody yet stays empty."""
    if not isinstance(table.itemDelegateForColumn(col), PersonDelegate):
        table.setItemDelegateForColumn(col, PersonDelegate(table, named))
    for row, user_id in enumerate(user_ids):
        chip = chips.get(user_id)
        if chip:
            item = table.item(row, col)
            item.setData(CHIP_ROLE, (chip.initials, chip.color))
            item.setToolTip(chip.name)


class WidthKeeper(QObject):
    """A column the person widens or narrows by dragging its heading's edge stays that wide, for their login on this PC: across a refresh and next time.
    The page's own code reads `view.kept_widths` (set_widths, fit_tree_columns do) so it doesn't put its starting width back over theirs."""

    def __init__(self, view: QTreeWidget | QTableWidget, key: str):
        super().__init__(view)
        self.view = view
        self.header = view.header() if isinstance(view, QTreeWidget) else view.horizontalHeader()
        self._dragging, self._resized_now = False, set()
        self.kept: dict[int, int] = {}
        self.load(key)
        self._viewport = self.header.viewport()
        self._viewport.installEventFilter(self)
        self.header.sectionResized.connect(self._section_resized)
        self.header.sectionHandleDoubleClicked.connect(lambda col: QTimer.singleShot(0, self, lambda: self.save({col})))
        # After the page has set its own starting widths.
        QTimer.singleShot(0, self, self.restore)

    def load(self, key: str) -> None:
        """The widths kept under this name. A list whose columns change with what it shows (the OF grid's branches) keeps one set for each layout."""
        self.key = key
        self.kept.clear()
        for pair in str(theme._settings().value(f"widths/{key}") or "").split():
            col, _, width = pair.partition(":")
            if col.isdigit() and width.isdigit():
                self.kept[int(col)] = max(30, min(int(width), 2000))
        self.view.kept_widths = self.kept

    def restore(self) -> None:
        for col, width in self.kept.items():
            if col < self.header.count():
                self.header.resizeSection(col, width)

    def _resizable(self, col: int) -> bool:
        header = self.header
        if header.sectionResizeMode(col) != QHeaderView.ResizeMode.Interactive:
            return False
        if header.stretchLastSection():
            last = next((header.logicalIndex(v) for v in range(header.count() - 1, -1, -1) if not header.isSectionHidden(header.logicalIndex(v))), None)
            return col != last
        return True

    def _section_resized(self, col: int, _old: int, new: int) -> None:
        if self._dragging and self._resizable(col):
            self._resized_now.add(col)

    def eventFilter(self, obj, event):  # noqa: N802 (Qt's name)
        if obj is self._viewport:  # compared in Python: while the list closes, its heading may already be gone
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._dragging, self._resized_now = True, set()
            elif event.type() == QEvent.Type.MouseButtonRelease and self._dragging:
                self._dragging = False
                if self._resized_now:
                    self.save(self._resized_now)
        return super().eventFilter(obj, event)

    def save(self, cols=None) -> None:
        for col in (cols if cols is not None else [c for c in range(self.header.count()) if self._resizable(c)]):
            self.kept[col] = max(30, min(self.header.sectionSize(col), 2000))
        self.view.kept_widths = self.kept
        theme._settings().setValue(f"widths/{self.key}", " ".join(f"{c}:{w}" for c, w in sorted(self.kept.items())))

    def forget(self) -> None:
        self.kept.clear()
        theme._settings().remove(f"widths/{self.key}")


class ColumnChooser(QMenu):
    """Each person chooses which columns a list shows, kept for their login on this PC. It stays open while they tick, so several can change at once.
    Right-clicking the headings opens it too."""
    columns_changed = Signal()

    def __init__(self, tree: QTreeWidget | QTableWidget, key: str, columns: dict[str, int], hidden: tuple[str, ...] = ()):
        super().__init__(tree)
        self.tree, self.key, self.columns, self.hidden = tree, key, columns, hidden
        is_tree = isinstance(tree, QTreeWidget)
        header = tree.header() if is_tree else tree.horizontalHeader()
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(lambda at: self.exec(header.mapToGlobal(at)))
        self.checks: dict[str, QCheckBox] = {}
        for name, col in columns.items():
            box = QCheckBox(" ".join((tree.headerItem().text(col) if is_tree else tree.horizontalHeaderItem(col).text()).split()))  # a heading on two lines reads as one here
            box.setObjectName("MenuCheck")
            box.toggled.connect(self._changed)
            action = QWidgetAction(self)
            action.setDefaultWidget(box)
            self.addAction(action)
            self.checks[name] = box
        self.addSeparator()
        reset = QAction("Back to the usual columns", self)
        reset.triggered.connect(self.reset)
        self.addAction(reset)
        saved = theme._settings().value(f"columns/{key}")
        self._show([n for n in columns if n not in hidden] if saved is None else saved.split())
        # A column the person widens or narrows by dragging its heading's edge stays that wide next time.
        self.header = header
        self.widths = WidthKeeper(tree, key)
        self.kept = self.widths.kept

    def restore_widths(self) -> None:
        self.widths.restore()

    def _save_widths(self, cols=None) -> None:
        self.widths.save(cols)

    def _show(self, shown: list[str]) -> None:
        for name, box in self.checks.items():
            box.blockSignals(True)
            box.setChecked(name in shown)
            box.blockSignals(False)
        self._apply()

    def _apply(self) -> None:
        for name, col in self.columns.items():
            self.tree.setColumnHidden(col, not self.checks[name].isChecked())
        self.columns_changed.emit()

    def open_under(self, widget: QWidget) -> None:
        self.exec(widget.mapToGlobal(widget.rect().bottomLeft()))

    def _changed(self, *_) -> None:
        self._apply()
        theme._settings().setValue(f"columns/{self.key}", " ".join(n for n, box in self.checks.items() if box.isChecked()))

    def reset(self) -> None:
        self._show([n for n in self.columns if n not in self.hidden])
        theme._settings().remove(f"columns/{self.key}")
        self.widths.forget()


def smart_columns(table: QTableWidget, key: str, hidden: tuple[str, ...] = ()) -> ColumnChooser:
    """Lets each person choose a plain list's columns. The first stays, because it says which row is which. If the stretching column is hidden, the last one takes the room."""
    slug = lambda text: " ".join(text.split()).lower().replace(" ", "-")  # a heading split over two lines keeps the name it had on one
    names = {slug(table.horizontalHeaderItem(col).text()): col for col in range(1, table.columnCount()) if table.horizontalHeaderItem(col).text()}
    table.horizontalHeader().setStretchLastSection(True)
    return ColumnChooser(table, key, names, tuple(slug(h) for h in hidden))


def chip_style(code: str) -> str:
    """The stylesheet for a document's status label."""
    fg, bg = theme.status_colors(code)
    return f"color:{fg};background:{bg};border-radius:4px;padding:3px 9px;font-weight:600;"


def show_if(widget: QWidget, on: bool) -> None:
    """A button that can't do anything here isn't shown at all, because a greyed-out one only raises questions. One not placed on the page, because this person may not use it, stays off it."""
    widget.setEnabled(bool(on))
    if widget.parentWidget() is not None or not on:
        widget.setVisible(bool(on))


def label(text: str = "", name: str = "") -> QLabel:
    w = QLabel(text)
    if name:
        w.setObjectName(name)
    return w


def show_text(box, text: str) -> None:
    """Fills a box, such as one that can only be read, showing the start of the text when it's longer than the box."""
    box.setText(text or "")
    box.setCursorPosition(0)


def register_tabs(tabs: dict, on_change) -> QTabBar:
    """Tabs along the top of a register. Each holds its key in its tab data."""
    bar = QTabBar()
    bar.setExpanding(False)
    bar.setDrawBase(False)
    for key, text in tabs.items():
        bar.setTabData(bar.addTab(text), key)
    bar.currentChanged.connect(lambda i: on_change(bar.tabData(i)))
    return bar


def show_tab_counts(bar: QTabBar, tabs: dict, counts: dict):
    for i in range(bar.count()):
        key = bar.tabData(i)
        bar.setTabText(i, f"{tabs[key]}  {counts[key]}")


class ActionButton(QPushButton):
    """A button that can't run its action twice from one double click. It stays locked until shortly after the action finishes, so a second click
    queued meanwhile is ignored rather than acting on whatever is selected next. Anything the action itself does to the button is kept."""

    LOCK_MS = 350

    def __init__(self, text: str, on_click=None, lock: bool = True):
        super().__init__(text)
        self._wanted, self._busy = True, False
        if on_click:
            self.clicked.connect(lambda *_: self._run(on_click) if lock else guarded(self, on_click))

    def setEnabled(self, on: bool) -> None:  # noqa: N802 (Qt's name)
        self._wanted = bool(on)
        if not self._busy:
            super().setEnabled(on)

    def _run(self, on_click) -> None:
        if self._busy:
            return
        self._busy = True
        QPushButton.setEnabled(self, False)
        try:
            guarded(self, on_click)
        finally:
            QTimer.singleShot(self.LOCK_MS, self._unlock)

    def _unlock(self) -> None:
        self._busy = False
        QPushButton.setEnabled(self, self._wanted)


def button(text: str, on_click=None, name: str = "", lock: bool = True) -> QPushButton:
    b = ActionButton(text, on_click, lock)
    if name:
        b.setObjectName(name)
    return b


def icon(name: str) -> QIcon:
    """A sidebar icon drawn in the current mode's ink, at three times its size so it stays sharp on high-DPI screens."""
    svg = (ASSETS / "icons" / f"{name}.svg").read_text(encoding="utf-8")
    svg = svg.replace("#5B6470", theme.current.MUTED).replace('width="24" height="24"', 'width="72" height="72"')
    pixmap = QPixmap()
    pixmap.loadFromData(svg.encode("utf-8"), "SVG")
    return QIcon(pixmap)


def app_icon() -> QIcon:
    return QIcon(str(ASSETS / "app.ico"))


def workflow_strip(steps: list[str]):
    """A row of steps. Returns the widget and a function that marks which step the document is at (None hides it)."""
    host = QWidget()
    row = QHBoxLayout(host)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(6)
    labels = []
    for n, text in enumerate(steps):
        if n:
            row.addWidget(label("/", "StepLine"))
        labels.append(label(text))
        row.addWidget(labels[-1])
    row.addStretch(1)

    def mark(current: int | None) -> None:
        host.setVisible(current is not None)
        for n, w in enumerate(labels):
            w.setObjectName("StepNext" if current is None or n > current else "StepNow" if n == current else "StepDone")
            w.style().unpolish(w)
            w.style().polish(w)
            pad = 8 if n == current else 0
            w.setContentsMargins(pad, 2, pad, 2)

    return host, mark


def left_aligned(widget: QWidget) -> QWidget:
    """Keep a button at its natural width inside a form row."""
    host = QWidget()
    row = QHBoxLayout(host)
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(widget)
    row.addStretch(1)
    return host


def narrow(inner: QWidget, width: int = 620) -> QWidget:
    """Long forms stay readable on a wide window."""
    inner.setMaximumWidth(width)
    host = QWidget()
    row = QHBoxLayout(host)
    row.setContentsMargins(4, 8, 4, 4)
    row.addWidget(inner, 1, Qt.AlignmentFlag.AlignTop)
    row.addStretch(0)
    return host


def panel(name: str = "Panel") -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName(name)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(12, 10, 12, 12)
    layout.setSpacing(8)
    return frame, layout


def guarded(parent, fn, *args):
    """Run fn. A broken rule shows its message and leaves everything as it was. Anything else, such as a printer, file or database error,
    is shown too and written to errors.log in the data folder, so nobody is left wondering whether it worked."""
    try:
        return fn(*args)
    except POError as exc:
        error(parent, str(exc))
    except ValueError as exc:
        error(parent, str(exc))
    except sqlite3.OperationalError as exc:
        if is_busy(exc):
            log_busy(exc)
            error(parent, BUSY_MESSAGE)
        else:
            show_unexpected(parent, exc)
    except Exception as exc:  # noqa: BLE001 (shown and logged, never swallowed)
        if is_busy(exc):
            log_busy(exc)
            error(parent, BUSY_MESSAGE)
        else:
            show_unexpected(parent, exc)
    return None


def is_busy(exc: BaseException | None) -> bool:
    """The database was locked by another computer for longer than the wait: during a save, or while a list was read. Nothing was saved, and trying again works.
    Also true when that database error is the cause of another error, such as one raised while a list refreshed. The wording is db.py's own test."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, sqlite3.OperationalError) and _busy_text(exc):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def computer_name() -> str:
    """This PC's name, so a shared errors.log or print file says which computer it came from."""
    name = os.environ.get("COMPUTERNAME") or platform.node() or "this PC"
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", name).strip(" .") or "this PC"


def show_unexpected(parent, exc: BaseException) -> None:
    log_error(exc)
    QMessageBox.critical(parent, "Something went wrong", f"{exc}\n\nAnything saved before this point is kept. The details are in errors.log in the data folder.")


def excepthook(exc_type, exc, tb) -> None:
    """For sys.excepthook: an error outside a button, such as in a search box or a list's selection, is shown and written to errors.log the same way, instead of vanishing."""
    if issubclass(exc_type, KeyboardInterrupt) or QApplication.instance() is None:
        sys.__excepthook__(exc_type, exc, tb)
        return
    if exc is not None and exc.__traceback__ is None:
        exc = exc.with_traceback(tb)
    try:
        if isinstance(exc, (POError, ValueError)):
            error(QApplication.activeWindow(), str(exc))
        elif is_busy(exc):
            log_busy(exc)
            error(QApplication.activeWindow(), BUSY_MESSAGE)
        else:
            show_unexpected(QApplication.activeWindow(), exc)
    except Exception:  # noqa: BLE001 (the message itself failed: at least it's written down)
        log_error(exc)


def log_error(exc: BaseException) -> None:
    try:
        with open(data_dir() / "errors.log", "a", encoding="utf-8") as log:
            # Several PCs may share one data folder, so each entry says which computer it came from.
            log.write(f"--- {datetime.now():%Y-%m-%d %H:%M:%S} on {computer_name()}\n{''.join(traceback.format_exception(exc))}\n")
    except OSError:
        pass


def log_busy(exc: BaseException) -> None:
    """One line, no crash details: enough to see later how often saves had to wait for another computer."""
    try:
        with open(data_dir() / "errors.log", "a", encoding="utf-8") as log:
            log.write(f"--- {datetime.now():%Y-%m-%d %H:%M:%S} on {computer_name()}: another computer was saving, nothing saved ({exc})\n")
    except OSError:
        pass


def error(parent, message: str) -> None:
    QMessageBox.warning(parent, "Can't do that", message)


def info(parent, title: str, message: str) -> None:
    QMessageBox.information(parent, title, message)


def confirm(parent, title: str, message: str, yes: str = "Yes") -> bool:
    """Go back on the left, the action on the right in the main color, as in the program's own dialogs.
    Go back has the focus, so a stray Enter, like Esc, changes nothing: the action takes a click, or Tab to it first."""
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(message)
    # Both take the same role, so they stay in the order added on every system.
    back = box.addButton("Go back", QMessageBox.ButtonRole.ActionRole)
    # Named before it's added: the box styles a button when it's added, and a name given later wouldn't paint it in the main color.
    go = QPushButton(yes)
    go.setObjectName("Primary")
    box.addButton(go, QMessageBox.ButtonRole.ActionRole)
    box.setDefaultButton(back)
    box.setEscapeButton(back)
    back.setFocus()
    box.exec()
    return box.clickedButton() is go


def ask_text(parent, title: str, prompt: str) -> str | None:
    text, ok = QInputDialog.getMultiLineText(parent, title, prompt)
    return text.strip() if ok else None


class ApproveDialog(QDialog):
    """The one question every Approve asks, wherever it's pressed: which OF, from whom, for how much, with a box for remarks that may stay empty.
    Go back on the left, Approve on the right in the main color. The remarks box has the cursor, so Enter starts a new line there, and Esc goes back."""

    def __init__(self, parent, what: str):
        super().__init__(parent)
        self.setWindowTitle("Approve")
        col = QVBoxLayout(self)
        self.question = QLabel(f"Approve {what}?")
        self.question.setWordWrap(True)
        col.addWidget(self.question)
        col.addWidget(QLabel("Remarks are optional and never print."))
        self.remarks = QPlainTextEdit()
        self.remarks.setTabChangesFocus(True)
        self.remarks.setFixedHeight(90)
        col.addWidget(self.remarks)
        row = QHBoxLayout()
        row.addStretch(1)
        self.back = QPushButton("Go back")
        self.back.setAutoDefault(False)
        self.back.clicked.connect(self.reject)
        self.go = QPushButton("Approve")
        self.go.setObjectName("Primary")
        self.go.setAutoDefault(False)
        self.go.clicked.connect(self.accept)
        row.addWidget(self.back)
        row.addWidget(self.go)
        col.addLayout(row)
        self.setMinimumWidth(420)
        self.remarks.setFocus()

    def answer(self) -> str | None:
        """The remarks (perhaps empty) once approved, or None after Go back."""
        return self.remarks.toPlainText().strip() if self.result() == QDialog.DialogCode.Accepted else None


def ask_approve(parent, what: str) -> str | None:
    """Asks to approve, with optional remarks. The remarks, perhaps empty, or None if the person went back."""
    box = ApproveDialog(parent, what)
    box.exec()
    return box.answer()


def save_file(parent, title: str, default: str, kinds: str) -> str:
    path, _ = QFileDialog.getSaveFileName(parent, title, default, kinds)
    return path


def fmt_date(iso: str | None) -> str:
    """Local convention: Nov 2, 2026."""
    if not iso:
        return ""
    try:
        d = date.fromisoformat(str(iso)[:10])
    except ValueError:
        return str(iso)
    return f"{d.strftime('%b')} {d.day}, {d.year}"


def fmt_datetime(iso: str | None) -> str:
    if not iso:
        return ""
    return f"{fmt_date(iso)} {str(iso)[11:16]}"


class SmartDateEdit(QDateEdit):
    """A date that may be left unset. Internally that's the minimum date, but nobody ever sees the year 2000:
    the calendar opens on this month, the first key or arrow starts from today, and Delete goes back to unset.
    Typing goes month, day, year, so 110526 or 11052026 is 11-05-2026 with no mouse. The calendar is still there for anyone who wants it.
    A typed date that doesn't exist, such as 02-29-2025, is refused with a message rather than quietly changed to another day."""

    _wanted: tuple[int, int] | None = None  # (month, day) typed when the box couldn't show that day yet
    _before: QDate | None = None
    _refused: str | None = None  # a typed date that was refused, until the person next types in, comes back to or sets the box
    _told: bool = False  # the refusal was already said, by a save that read the box

    def forget_refusal(self, *_) -> None:
        self._refused, self._told = None, False

    def is_unset(self) -> bool:
        return bool(self.specialValueText()) and self.date() == self.minimumDate()

    def eventFilter(self, obj, event):
        if obj is self.calendarWidget() and event.type() == QEvent.Type.Show and self.is_unset():
            today = QDate.currentDate()
            obj.setCurrentPage(today.year(), today.month())
        return super().eventFilter(obj, event)

    def stepBy(self, steps: int) -> None:
        if self.is_unset():
            self.setDate(QDate.currentDate())
            return
        super().stepBy(steps)

    def focusNextPrevChild(self, forward: bool) -> bool:  # noqa: N802 (Qt's name)
        """Tab leaves a box that says Not set, instead of going round its hidden parts."""
        if self.is_unset():
            return QWidget.focusNextPrevChild(self, forward)
        return super().focusNextPrevChild(forward)

    def keyPressEvent(self, event):
        key = event.key()
        self.forget_refusal()
        if self.is_unset() and key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            QWidget.focusNextPrevChild(self, key == Qt.Key.Key_Tab and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            return
        if self.specialValueText() and key == Qt.Key.Key_Delete:
            self.setDate(self.minimumDate())
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not self._settle():
            return  # refused: Enter doesn't also press the dialog's button
        if self.is_unset() and event.text().isdigit():
            self._before = self.date()
            self.setDate(QDate.currentDate())
            self._start_typing()
        day = self.currentSection() == QDateEdit.Section.DaySection and event.text().isdigit()
        year = self.currentSection() == QDateEdit.Section.YearSection and event.text().isdigit()
        if day:
            typed = event.text() if self.lineEdit().hasSelectedText() else self.sectionText(QDateEdit.Section.DaySection) + event.text()
            month = self.date().month()
        super().keyPressEvent(event)
        if day and typed.isdigit() and len(typed) <= 2:
            shown = self.sectionText(QDateEdit.Section.DaySection)
            # Qt changes a day the month can't have yet, such as 29 while the year still says 2026, to its last day. Remember what was typed.
            if 28 < int(typed) <= 31 and shown.isdigit() and int(shown) != int(typed):
                self._wanted = (month, int(typed))
            elif shown.isdigit() and int(shown) == int(typed):
                self._wanted = None
        if year and self._wanted and len(self.sectionText(QDateEdit.Section.YearSection)) == 4:
            self._settle()  # the year is complete, so the remembered day can go in, or be refused, now

    def _typed_date(self) -> tuple[str, QDate | None] | None:
        """What the person typed, read from the box before Qt corrects it. None if it isn't a whole date."""
        found = re.fullmatch(r"(\d{1,2})-(\d{1,2})-(\d{1,4})", self.lineEdit().text().strip())
        if not found:
            return None
        month, day, year = (int(g) for g in found.groups())
        if len(found.group(3)) == 2:
            year += 2000  # 26 is 2026, as before
        elif len(found.group(3)) in (1, 3):
            return found.group(0), None  # a year not finished, such as 2 or 202, isn't guessed
        if self._wanted and self._wanted[0] == month:
            day = self._wanted[1]
        when = QDate(year, month, day)
        shown = f"{month:02d}-{day:02d}-{year:04d}"
        return shown, (when if when.isValid() and self.minimumDate() <= when <= self.maximumDate() else None)

    def _settle(self) -> bool:
        """Puts the typed date in, or refuses it with a message and goes back to the date from before. False if refused."""
        typed = None if self.is_unset() else self._typed_date()
        self._wanted = None
        if typed is None:
            return True
        text, when = typed
        if when is not None:
            if when != self.date():
                self.setDate(when)
            return True
        self.setDate(self._before if self._before is not None else self.date())
        # Until it's typed again, a save that reads this box stops (see date_value) rather than saving the date from before.
        self._refused, self._told = text, False
        QTimer.singleShot(0, self, self._say_refused)
        return False

    def _say_refused(self) -> None:
        if self._refused and not self._told:
            self._told = True
            error(self.window(), f"{self._refused} isn't a real date, so the date wasn't changed. Type it again.")

    def _start_typing(self) -> None:
        self.setCurrentSection(QDateEdit.Section.MonthSection)
        self.setSelectedSection(QDateEdit.Section.MonthSection)

    def focusInEvent(self, event):
        """Wherever it was left, typing starts again from the month."""
        super().focusInEvent(event)
        self._before, self._wanted = self.date(), None
        if self._told:
            self.forget_refusal()  # back in the box after being told: what it shows now is what they see
        if not self.is_unset():
            self._start_typing()

    def focusOutEvent(self, event):
        self._settle()
        super().focusOutEvent(event)


def date_edit(value: str | None = None, optional: bool = False) -> QDateEdit:
    w = SmartDateEdit()
    w.setCalendarPopup(True)
    w.calendarWidget().installEventFilter(w)
    w.setDisplayFormat("MM-dd-yyyy")
    if optional:
        w.setMinimumDate(QDate(2000, 1, 1))
        w.setSpecialValueText("Not set")
    set_date(w, value)
    w.dateChanged.connect(w.forget_refusal)  # a date picked from the calendar or set by the page replaces a refused one
    return w


MONTHS_BACK = 36


def month_picker(date_from: QDateEdit, date_to: QDateEdit) -> QComboBox:
    """A whole month in one pick, beside From and To, so nobody has to type the first and last day.
    Picking a month fills both dates; typing a date by hand puts the picker back to "Month" unless the dates still make up one month."""
    box = QComboBox()
    box.setToolTip("Pick a month to fill From and To with its first and last day")
    box.addItem("Month", None)
    first = QDate.currentDate()
    first = QDate(first.year(), first.month(), 1)
    for n in range(MONTHS_BACK):
        month = first.addMonths(-n)
        box.addItem(month.toString("MMM yyyy"), month)
    # Short, so a page with From, To and the month still fits a laptop window.
    box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    box.setMinimumContentsLength(5)
    box.setFixedWidth(98)

    def picked(index: int) -> None:
        month = box.itemData(index)
        if month is None:
            return
        for w in (date_from, date_to):
            w.blockSignals(True)
        set_date(date_from, month.toString("yyyy-MM-dd"))
        set_date(date_to, month.addMonths(1).addDays(-1).toString("yyyy-MM-dd"))
        for w in (date_from, date_to):
            w.blockSignals(False)
        # One refresh, with both dates already in place.
        date_from.dateChanged.emit(date_from.date())

    def typed(*_) -> None:
        a, b = date_from.date(), date_to.date()
        whole = a.day() == 1 and b == a.addMonths(1).addDays(-1) and not (date_from.specialValueText() and a == date_from.minimumDate())
        index = box.findData(a) if whole else 0
        box.blockSignals(True)
        box.setCurrentIndex(max(index, 0))
        box.blockSignals(False)

    box.activated.connect(picked)
    date_from.dateChanged.connect(typed)
    date_to.dateChanged.connect(typed)
    return box


def set_date(w: QDateEdit, value: str | None) -> None:
    if isinstance(w, SmartDateEdit):
        w.forget_refusal()
    if value:
        w.setDate(QDate.fromString(str(value)[:10], "yyyy-MM-dd"))
    elif w.specialValueText():
        w.setDate(w.minimumDate())
    else:
        w.setDate(QDate.currentDate())


def date_value(w: QDateEdit) -> str | None:
    if isinstance(w, SmartDateEdit) and not w.is_unset() and w.lineEdit().text() != w.date().toString(w.displayFormat()):
        w._settle()  # still being typed, such as a two-digit year, when a shortcut saves
    if isinstance(w, SmartDateEdit) and w._refused:
        # The typed date was refused: the box went back to the date from before, which the person never meant to save.
        w._told = True
        raise POError(f"{w._refused} isn't a real date, so nothing was saved. Type it again.")
    if w.specialValueText() and w.date() == w.minimumDate():
        return None
    return w.date().toString("yyyy-MM-dd")


def make_table(headers: list[str], *, numeric: tuple[int, ...] = (), stretch: int | None = None) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    t.verticalHeader().setVisible(False)
    t.verticalHeader().setDefaultSectionSize(30)
    t.setShowGrid(False)
    t.setWordWrap(False)
    t.horizontalHeader().setHighlightSections(False)
    t.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    # A heading too wide for its column ends in "...", rather than losing its first letters when it's right-aligned.
    t.horizontalHeader().setTextElideMode(Qt.TextElideMode.ElideRight)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    t.setProperty("numeric", list(numeric))
    if stretch is not None:
        t.horizontalHeader().setSectionResizeMode(stretch, QHeaderView.ResizeMode.Stretch)
    for col in numeric:
        head = t.horizontalHeaderItem(col)
        head.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    enable_sort(t)
    return t


def make_tree(headers: list[str], *, numeric: tuple[int, ...] = (), stretch: int | None = None) -> QTreeWidget:
    """A table whose rows open to show the rows under them. Enter or a double-click is left for opening the document."""
    t = QTreeWidget()
    t.setColumnCount(len(headers))
    t.setHeaderLabels(headers)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    t.setExpandsOnDoubleClick(False)
    t.setUniformRowHeights(True)
    t.setIndentation(16)
    t.setWordWrap(False)
    header = t.header()
    header.setStretchLastSection(False)
    header.setHighlightSections(False)
    header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    header.setTextElideMode(Qt.TextElideMode.ElideRight)
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    if stretch is not None:
        header.setSectionResizeMode(stretch, QHeaderView.ResizeMode.Stretch)
    for col in numeric:
        t.headerItem().setTextAlignment(col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    enable_sort(t)
    return t


def fit_tree_columns(tree: QTreeWidget, stretch: int | None, shrink: int | None, min_stretch: int = 170, min_shrink: int = 130, keep=()) -> None:
    """Every column showing as wide as what it holds, except those in keep, with the rest of the room going to the stretch column. On a narrow window, the shrink column gives way first."""
    if stretch is None:
        shrink = None
    kept = getattr(tree, "kept_widths", {})
    for col in range(tree.columnCount()):
        if col in kept and col != stretch:
            tree.setColumnWidth(col, kept[col])  # the person set this width themselves
        elif col != stretch and col not in keep and not tree.isColumnHidden(col):
            tree.resizeColumnToContents(col)
            tree.setColumnWidth(col, tree.columnWidth(col) + 10)
    room = tree.viewport().width() - sum(tree.columnWidth(c) for c in range(tree.columnCount()) if c != stretch)
    if shrink is not None and shrink not in kept and room < min_stretch:
        tree.setColumnWidth(shrink, max(min_shrink, tree.columnWidth(shrink) - (min_stretch - room)))


def open_for_printing(documents: list[bytes], name: str) -> None:
    """Puts the documents in one file and opens it in the PDF viewer, where one print command prints them all."""
    data = pdf.combine(documents)
    folder = data_dir() / "print"
    folder.mkdir(exist_ok=True)
    # Several PCs may share this folder: the name has the time to the millisecond and the computer, and a file that already exists,
    # or is open in a viewer on another PC, is never written over. The next free name is used instead.
    when = datetime.now()
    stem = f"{name} {when:%Y-%m-%d %H%M%S}-{when.microsecond // 1000:03d} {computer_name()}"
    for attempt in range(50):
        path = folder / (f"{stem}.pdf" if not attempt else f"{stem} ({attempt + 1}).pdf")
        try:
            with open(path, "xb") as out:
                out.write(data)
        except (FileExistsError, PermissionError):
            continue
        break
    else:
        raise POError("The print file couldn't be saved in the print folder. Close any open print files and try again.")
    open_path(path)


def choose_printer(parent) -> QPrinter | None:
    """Asks which printer, for a document that prints once. None if the person went back, and then nothing has happened."""
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    dialog = QPrintDialog(printer, parent)
    dialog.setOption(QAbstractPrintDialog.PrintDialogOption.PrintToFile, False)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return printer


def print_once(printer: QPrinter, document: bytes) -> None:
    """Sends a document straight to the printer, one copy of each page. It never opens in a PDF viewer, which could print it again and again, and no file is left behind."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        path = Path(folder) / "document.pdf"
        path.write_bytes(document)
        pdf_file = QPdfDocument()
        if pdf_file.load(str(path)) != QPdfDocument.Error.None_:
            raise POError("The document couldn't be read for printing.")
        printer.setCopyCount(1)
        printer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        printer.setFullPage(True)
        painter = QPainter()
        # A printer that can't start, such as one that is off or gone, prints nothing. Saying so lets the caller undo what it recorded.
        if not painter.begin(printer):
            pdf_file.close()
            del pdf_file
            raise POError("The printer did not start. Nothing was printed.")
        try:
            target = printer.paperRect(QPrinter.Unit.DevicePixel).toRect()
            for page in range(pdf_file.pageCount()):
                if page:
                    printer.newPage()
                # A4 at 300 dpi. At the printer's own resolution one page would take over half a gigabyte.
                painter.drawImage(target, pdf_file.render(page, QSize(2480, 3508)))
        finally:
            finished = painter.end()
            pdf_file.close()
            # The file stays locked until this object is gone.
            del pdf_file
        if not finished:
            raise POError("The printer did not finish the document. Check what came out of the printer.")


def open_path(path) -> None:
    """Opens a file in this computer's own program for it (a PDF in the PDF viewer), or a folder in Explorer or Finder.
    Windows uses os.startfile, as it always has. A Mac or Linux asks the desktop through Qt."""
    if hasattr(os, "startfile"):
        os.startfile(path)
        return
    if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).resolve()))):
        raise POError(f"The file was saved, but this computer couldn't open it. It is at {path}.")


def open_email_draft(path: Path, to: str, subject: str, body: str) -> None:
    """Opens a new message in this PC's email program, and the folder holding the file to attach. Email programs won't accept an attachment from a link, so a person drags it in."""
    QDesktopServices.openUrl(QUrl(f"mailto:{quote(to)}?subject={quote(subject)}&body={quote(body)}"))
    open_path(path.parent)


def confirm_reprint(parent, what: str) -> bool:
    """A second copy can be mistaken for a second document, so printing one is a choice, and the copy says what it is."""
    return confirm(parent, "Print again", f"{what} has already been printed. This copy will be marked as printed again, with your name, and it's recorded. Print it?", "Print again")


def print_order_forms(parent, conn, user, of_ids: list[int], paper_forms: bool = False) -> bool:
    """Each warehouse OF followed by its slips and POs, as one file, with paper count forms if asked for. False if the person went back, and then nothing has changed."""
    n = len(of_ids)
    if not confirm(parent, "Print and issue", f"Print {n} Order Form{'s' if n != 1 else ''} with {'their' if n != 1 else 'its'} POs, as one file? "
                   f"Each order prints its warehouse copy for the supplier to sign, {'two paper count forms per branch ' if paper_forms else ''}and cut slips, then its POs. Suppliers we sticker for also get their DRs. The others' DRs wait under DRs to email. "
                   "The office copy comes later, once the delivery is complete. Any POs not yet made get their numbers now, which are never reused. "
                   "The POs are issued and move to Completed, and each exact document is kept for good.", "Print and issue"):
        return False
    documents, problems = [], []
    try:
        for of_id in of_ids:
            document, stopped = pdf.print_order_form(conn, user, of_id, paper_forms)
            problems += stopped
            if document:
                documents.append(document)
    finally:
        # Orders already issued before one fails still open for printing: their originals can't be made again.
        if documents:
            open_for_printing(documents, "OF and PO batch")
    if problems:
        error(parent, "These weren't printed, so they aren't in the file:\n" + "\n".join(problems))
    return True


def fill_table(table: QTableWidget, rows: list[list], keys: list | None = None, status_col: int | None = None) -> None:
    """rows hold display strings. keys go on column 0 as the row's id."""
    numeric = set(table.property("numeric") or [])
    table.setSortingEnabled(False)
    table.setRowCount(len(rows))
    if status_col is not None and not isinstance(table.itemDelegateForColumn(status_col), StatusDelegate):
        table.setItemDelegateForColumn(status_col, StatusDelegate(table))
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            if c == status_col:
                item = status_item(str(value))
            else:
                item = SortItem("" if value is None else str(value))
                if c in numeric:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if c == 0 and keys is not None:
                item.setData(Qt.ItemDataRole.UserRole, keys[r])
            table.setItem(r, c, item)
    resort(table)


def status_item(code: str) -> QTableWidgetItem:
    item = SortItem(STATUS_LABELS.get(code, code))
    item.setData(STATUS_ROLE, code)
    return item


def selected_key(table: QTableWidget):
    row = table.currentRow()
    if row < 0 or table.item(row, 0) is None:
        return None
    return table.item(row, 0).data(Qt.ItemDataRole.UserRole)


# ------------------------------------------------------------------ long lists, a page at a time

# Long lists show one page at a time so they stay fast and easy to read, with Previous and Next,
# and each person picks 50, 100 or 200 a page (remembered). Search and filters still look through everything in the
# database and go back to page 1; only what's drawn on screen is limited.
PAGE_SIZES = (50, 100, 200)
PAGE_SIZE = 50
_jumping = False  # True while a page change re-fills a list: keep_place then goes to the top instead of staying put


def saved_page_size() -> int:
    try:
        size = int(theme._settings().value("lists/page_size", PAGE_SIZE))
    except (TypeError, ValueError):
        size = PAGE_SIZE
    return size if size in PAGE_SIZES else PAGE_SIZE


class Pager(QWidget):
    """'Page 2 of 9 · 51-100 of 450', Previous / Next and a Show 50/100/200 choice, under a list. Hidden when one page holds everything.
    The page re-fills its list in on_change, passing its rows, newest first, through take() (or counting in SQL, fetching up to
    limit, keeping rows from offset, and calling show_counts)."""

    TIP = "Search and filters look through everything, not only this page."

    def __init__(self, on_change, parent: QWidget | None = None, page_size: int | None = None):
        super().__init__(parent)
        self.setObjectName("Pager")
        self.on_change = on_change
        self.page_size = max(1, int(page_size)) if page_size else saved_page_size()
        self.page = 0
        self.total = 0
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 2, 0, 0)
        row.setSpacing(8)
        self.prev_button = button("\u2039 Previous", lambda: self.go(-1))
        self.next_button = button("Next \u203a", lambda: self.go(1))
        self.label = label("", "Muted")
        self.label.setToolTip(self.TIP)
        self.size_box = QComboBox()
        for size in PAGE_SIZES:
            self.size_box.addItem(f"{size} a page", size)
        if self.page_size not in PAGE_SIZES:
            self.size_box.addItem(f"{self.page_size} a page", self.page_size)
        self.size_box.setCurrentIndex(self.size_box.findData(self.page_size))
        self.size_box.setToolTip("How many rows each page shows. Remembered for next time.")
        self.size_box.currentIndexChanged.connect(self._size_chosen)
        row.addWidget(self.prev_button)
        row.addWidget(self.next_button)
        row.addWidget(self.label)
        row.addStretch(1)
        row.addWidget(label("Show", "Muted"))
        row.addWidget(self.size_box)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)  # one line high, never taking room from the list
        self.setVisible(False)

    # where this page starts and ends in the full list
    @property
    def offset(self) -> int:
        return self.page * self.page_size

    @property
    def limit(self) -> int:
        return (self.page + 1) * self.page_size

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.page_size))

    def reset(self) -> None:
        """Back to page 1: for a new search, filter, dates or tab."""
        self.page = 0

    def go(self, step: int) -> None:
        page = min(max(0, self.page + step), self.pages - 1)
        if page == self.page:
            return
        self.page = page
        self._refill()

    def _size_chosen(self, index: int) -> None:
        size = self.size_box.itemData(index)
        if not size or size == self.page_size:
            return
        first = self.offset
        self.page_size = int(size)
        self.page = first // self.page_size  # the row at the top stays on the page shown
        try:
            theme._settings().setValue("lists/page_size", self.page_size)
        except Exception:
            pass
        self._refill()

    def _refill(self) -> None:
        global _jumping
        _jumping = True
        try:
            self.on_change()
        finally:
            _jumping = False

    def take(self, rows) -> list:
        """The rows on this page, in the order given. The label counts all of them."""
        rows = list(rows)
        self.total = len(rows)
        self.page = min(self.page, self.pages - 1)
        self._show()
        return rows[self.offset:self.limit]

    def show_counts(self, shown: int, total: int) -> None:
        """For a page that counts its rows in the database instead of passing them all."""
        self.total = max(int(total), 0)
        self.page = min(self.page, self.pages - 1)
        self._show()

    def _show(self) -> None:
        if self.total == 0:
            self.label.setText("")
        else:
            last = min(self.limit, self.total)
            self.label.setText(f"Page {self.page + 1:,} of {self.pages:,} \u00b7 {self.offset + 1:,}\u2013{last:,} of {self.total:,}")
        self.prev_button.setEnabled(self.page > 0)
        self.next_button.setEnabled(self.page < self.pages - 1)
        self.setVisible(self.total > min(PAGE_SIZES) or self.total > self.page_size)


def _view_key(view):
    if isinstance(view, QTreeWidget):
        item = view.currentItem()
        return None if item is None else item.data(0, Qt.ItemDataRole.UserRole)
    return selected_key(view)


def _select_view_key(view, key) -> bool:
    if isinstance(view, QTreeWidget):
        def find(item):
            if item.data(0, Qt.ItemDataRole.UserRole) == key:
                return item
            for i in range(item.childCount()):
                found = find(item.child(i))
                if found is not None:
                    return found
            return None
        for i in range(view.topLevelItemCount()):
            found = find(view.topLevelItem(i))
            if found is not None:
                view.setCurrentItem(found)
                return True
        return False
    for r in range(view.rowCount()):
        item = view.item(r, 0)
        if item is not None and item.data(Qt.ItemDataRole.UserRole) == key:
            view.setCurrentCell(r, view.currentColumn() if view.currentColumn() >= 0 else 0)
            return True
    return False


@contextmanager
def keep_place(view):
    """Around a list's re-fill: the same row stays selected and the list stays scrolled where it was. After Previous or Next, the new page starts at its top."""
    bar = view.verticalScrollBar()
    where, key = bar.value(), _view_key(view)
    try:
        yield view
    finally:
        if _jumping:  # a new page: start at its top
            view.setCurrentItem(None)  # nothing chosen on the new page, so no details pane shows a row from it
            view.clearSelection()
            where = 0
        elif key is not None:
            _select_view_key(view, key)

        def scroll_back():
            try:
                view.verticalScrollBar().setValue(where)
            except RuntimeError:  # the list closed meanwhile
                pass
        scroll_back()
        QTimer.singleShot(0, view, scroll_back)  # again once the list has laid out its new rows


def set_widths(table: QTableWidget, widths: dict[int, int]) -> None:
    """A column the person has set the width of themselves keeps their width."""
    kept = getattr(table, "kept_widths", {})
    for col, width in widths.items():
        table.setColumnWidth(col, kept.get(col, width))


def search_combo(parent=None) -> QComboBox:
    """A combo you can type into: finds anywhere in the text, and only accepts real choices."""
    c = QComboBox(parent)
    c.setEditable(True)
    c.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    comp = c.completer()
    comp.setFilterMode(Qt.MatchFlag.MatchContains)
    comp.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
    comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
    return c


def combo_value(c: QComboBox):
    """The chosen item's data, only if the typed text really is one of the choices."""
    index = c.findText(c.currentText().strip(), Qt.MatchFlag.MatchFixedString)
    return c.itemData(index) if index >= 0 else None


def terms_combo() -> QComboBox:
    """Pick a standard term or type a custom one. No autocomplete, or typing 4 would quietly become 45D."""
    c = QComboBox()
    c.setEditable(True)
    c.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    c.setCompleter(None)
    c.lineEdit().setPlaceholderText("Choose or type")
    for terms in PAYMENT_TERMS:
        c.addItem(terms)
    set_terms(c)
    return c


def set_terms(c: QComboBox, value: str = NO_TERMS) -> None:
    c.setCurrentText(value)


def select_data(c: QComboBox, value) -> None:
    index = c.findData(value)
    if index >= 0:
        c.setCurrentIndex(index)


def escape_shortcut(widget: QWidget, action) -> None:
    """Esc runs action, such as going back. While a cell is being typed in, Esc only cancels that, as people expect."""
    from PySide6.QtGui import QKeySequence, QShortcut
    from PySide6.QtWidgets import QApplication

    def pressed():
        focus = QApplication.focusWidget()
        while focus is not None:
            if isinstance(focus, QAbstractItemView) and focus.state() == QAbstractItemView.State.EditingState:
                # Through the view's own index form: a table or tree widget's version takes an item instead.
                QAbstractItemView.closePersistentEditor(focus, focus.currentIndex())
                focus.setFocus()
                return
            focus = focus.parentWidget()
        action()

    widget.escape_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), widget, activated=pressed)


# ------------------------------------------------------------------ sorting by a heading

_DATE = re.compile(r"^[A-Z][a-z]{2} \d{1,2}, \d{4}")
# Only a number that stands on its own: "2026-OF-00013" is a document number and sorts as text.
_NUMBER = re.compile(r"^[+-]?₱?\s?-?[\d,]*\d(?:\.\d+)?(?=$|\s|%|,\s)")


def sort_key(text: str) -> tuple:
    """What a cell sorts by: a date as a date, an amount or count as a number, "86 of 232" by its first number, and anything else as text, with blanks last."""
    t = (text or "").strip()
    if not t:
        return (2, 0.0, "")
    day = _DATE.match(t)
    if day:
        try:
            when = datetime.strptime(day.group(0), "%b %d, %Y")
            clock = re.match(r"\s+(\d{1,2}):(\d{2})", t[day.end():])
            minutes = int(clock.group(1)) * 60 + int(clock.group(2)) if clock else 0
            # Counted in minutes from year 1, so any year sorts, before 1970 or after 3000 too.
            return (0, float(when.toordinal() * 1440 + minutes), "")
        except ValueError:
            pass
    number = _NUMBER.match(t)
    if number:
        try:
            return (0, float(number.group(0).replace("₱", "").replace(",", "").replace(" ", "")), "")
        except ValueError:
            pass
    return (1, 0.0, t.lower())


class SortItem(QTableWidgetItem):
    """A table cell that sorts by its value, not its letters."""

    def __lt__(self, other):
        return sort_key(self.text()) < sort_key(other.text())


def enable_sort(view) -> None:
    """Pressing a heading sorts the list by it, and pressing again turns it around. The choice stays through a refresh.
    A list whose rows are typed into, or whose order means something, such as Checks to make by section, can switch it off with the no_sort property."""
    header = view.header() if isinstance(view, QTreeWidget) else view.horizontalHeader()
    header.setSectionsClickable(True)

    def clicked(col: int):
        if view.property("no_sort") or (isinstance(view, QTableWidget) and view.editTriggers() != QAbstractItemView.EditTrigger.NoEditTriggers):
            return
        same = view.property("sort_col") == col
        order = Qt.SortOrder.DescendingOrder if same and view.property("sort_order") == "asc" else Qt.SortOrder.AscendingOrder
        view.setProperty("sort_col", col)
        view.setProperty("sort_order", "asc" if order == Qt.SortOrder.AscendingOrder else "desc")
        header.setSortIndicatorShown(True)
        header.setSortIndicator(col, order)
        resort(view)

    header.sectionClicked.connect(clicked)


def resort(view) -> None:
    """Puts the list back in the order its heading was pressed for, after it was filled again."""
    col = view.property("sort_col")
    if col is None or view.property("no_sort"):
        return
    descending = view.property("sort_order") == "desc"
    if isinstance(view, QTableWidget):
        view.sortItems(col, Qt.SortOrder.DescendingOrder if descending else Qt.SortOrder.AscendingOrder)
        _pinned_last(view)
        return
    # A tree sorts its top rows; what's under each stays with it, open or closed as it was.
    current = view.currentItem()
    opened = set()

    def remember(item):
        for i in range(item.childCount()):
            child = item.child(i)
            if child.isExpanded():
                opened.add(id(child))
            remember(child)
    tops = [view.topLevelItem(i) for i in range(view.topLevelItemCount())]
    for top in tops:
        if top.isExpanded():
            opened.add(id(top))
        remember(top)
    pinned = [top for top in tops if top.data(0, PIN_BOTTOM_ROLE)]
    ordered = sorted((top for top in tops if not top.data(0, PIN_BOTTOM_ROLE)), key=lambda item: sort_key(item.text(col)), reverse=descending) + pinned
    view.blockSignals(True)
    while view.topLevelItemCount():
        view.takeTopLevelItem(0)
    view.addTopLevelItems(ordered)

    def restore(item):
        item.setExpanded(id(item) in opened)
        for i in range(item.childCount()):
            restore(item.child(i))
    for top in ordered:
        restore(top)
    if current is not None:
        view.setCurrentItem(current)
    view.blockSignals(False)


def _pinned_last(table: QTableWidget) -> None:
    """Moves rows marked with PIN_BOTTOM_ROLE, such as a Total, back to the bottom after a sort, in the order they were in."""
    rows = [r for r in range(table.rowCount()) if table.item(r, 0) is not None and table.item(r, 0).data(PIN_BOTTOM_ROLE)]
    if not rows or rows == list(range(table.rowCount() - len(rows), table.rowCount())):
        return
    current = table.currentRow()
    moved = []
    for r in reversed(rows):
        moved.insert(0, [table.takeItem(r, c) for c in range(table.columnCount())])
        table.removeRow(r)
    for items in moved:
        r = table.rowCount()
        table.insertRow(r)
        for c, item in enumerate(items):
            if item is not None:
                table.setItem(r, c, item)
    if current in rows:
        table.setCurrentCell(table.rowCount() - len(rows) + rows.index(current), 0)


def hide_unused(tree, chooser, cols) -> None:
    """A column that's empty for everything listed takes no room, unless the person switched it off anyway. It comes back as soon as a row has something in it."""
    names = {col: name for name, col in chooser.columns.items()} if chooser else {}
    items = []
    for i in range(tree.topLevelItemCount()):
        top = tree.topLevelItem(i)
        items.append(top)
        items += [top.child(k) for k in range(top.childCount())]
    for col in cols:
        name = names.get(col)
        if name and not chooser.checks[name].isChecked():
            tree.setColumnHidden(col, True)
            continue
        tree.setColumnHidden(col, not any(item.text(col).strip() for item in items))
