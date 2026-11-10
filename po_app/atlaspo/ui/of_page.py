"""Order Forms: the register, and the OF screen with its branch allocation grid.

The grid is the heart of the program. One row per product, one column per branch, everything on one screen,
and Enter walks through the branch quantities so a whole order can be keyed without touching the mouse.
"""
from __future__ import annotations

from datetime import timedelta

import os
from pathlib import Path

from PySide6.QtCore import QEvent, QStringListModel, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QCompleter, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
                               QLineEdit, QPushButton, QScrollArea, QSplitter, QStyle, QStyledItemDelegate, QTableWidget, QTableWidgetItem, QTabWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import auth, masters, of_layout, order_forms as of_service, orders, pdf, receiving, reports
from ..db import POError, get_setting, today
from ..money import (DISC_AMOUNT, DISC_NONE, DISC_PERCENT, OFFERED_UNITS, VAT_EXCLUSIVE, VAT_INCLUSIVE, VAT_NONE, compute_line, compute_order, fmt_milli, fmt_peso,
                     fmt_percent, fmt_plain, fmt_qty, fx_to_centavos, m_to_qty, parse_milli, parse_percent, parse_pesos, parse_pieces, parse_qty, per_piece, pieces_per_unit, qty_to_m, sticker_deduction)
from .common import (month_picker, show_if, STATUS_ROLE, ColumnChooser, StatusDelegate, WidthKeeper, confirm_reprint, ask_approve, ask_text, button, chip_style, combo_value, confirm, date_edit, date_value, fill_table, fit_tree_columns, fmt_date, fmt_datetime, escape_shortcut, guarded, hide_unused, info, label, left_aligned, resort,
                     make_table, make_tree, open_for_printing, panel, print_order_forms, register_tabs, save_file, search_combo, select_data, selected_key, set_date, set_terms,
                     set_widths, show_tab_counts, terms_combo, workflow_strip, Pager, keep_place)
from .po_page import ComboDelegate
from . import theme

# Columns before the branches. After them come Total, Net, Amount and the optional ones, placed by _layout_columns.
C_DEPT, C_CODE, C_DESC, C_SIZE, C_COLOR, C_UNIT, C_COST, C_D1, C_D2 = range(9)
OPTIONAL = ("Net unit", "Order qty", "Supplier code", "Brand", "Box of", "Selling", "FX", "FX rate", "FX cost", "Remarks")
STEPS = ["Draft", "Approval", "Approved", "Branch POs", "Issued"]
RIGHT_AFTER = ("Total", "Per pc", "Net unit", "Amount", "Order qty", "Box of", "Selling", "FX rate", "FX cost")
READ_ONLY_AFTER = ("Total", "Per pc", "Net unit", "Amount")
# On a narrow screen the product name keeps at least this much room. Per pc steps back into More columns first, then the grid scrolls sideways.
PRODUCT_MIN = 120
PER_PC_WIDTH = 62
# The grid always shows at least this many rows. When the window is shorter than that, the whole page scrolls instead.
MIN_GRID_ROWS = 6
# Space after a red button, so it sits apart from the everyday buttons beside it.
DANGER_GAP = 28


def fit_headings(table, cols) -> None:
    """Widen each of these columns, if needed, so its whole heading shows."""
    header = table.horizontalHeader()
    fm = header.fontMetrics()
    for c in cols:
        text = table.horizontalHeaderItem(c).text()
        # The heading's own padding and sort arrow, measured, with the text measured on its own as a floor.
        need = max(header.sectionSizeFromContents(c).width(), fm.horizontalAdvance(text) + 24)
        if table.columnWidth(c) < need:
            table.setColumnWidth(c, need)


def may_approve(conn, user, of_id: int, prepared_by: int) -> bool:
    """Whether this person may approve or reject this OF: on the approver list, and not the one who prepared or worked on it unless self-approval is on.
    The same check the approval itself makes, so no button is offered that would only be refused."""
    try:
        of_service._check_approver(conn, user, prepared_by, of_id)
    except POError:
        return False
    return True


class EnterMoves(QStyledItemDelegate):
    """Enter in a cell commits it and moves to the next place a purchaser would type, like a fast keyboard-entry screen."""

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page

    def eventFilter(self, editor, event):
        if event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.commitData.emit(editor)
            self.closeEditor.emit(editor, QStyledItemDelegate.EndEditHint.NoHint)
            QTimer.singleShot(0, self.page.advance)
            return True
        return super().eventFilter(editor, event)


class DepartmentGrid(QTableWidget):
    """Draws a line above each row that starts a new department, so groups read at a glance without rows that can't be typed in."""

    def __init__(self, *args):
        super().__init__(*args)
        self.group_starts: set[int] = set()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self.viewport())
        painter.setPen(QPen(QColor(theme.current.ACCENT), 1.5))
        for row in self.group_starts:
            y = self.rowViewportPosition(row)
            if row < self.rowCount():
                painter.drawLine(0, y, self.viewport().width(), y)
        painter.end()


class OFPage(QWidget):
    closed = Signal()
    changed = Signal()
    open_po = Signal(int)

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self.conn, self.user = conn, user
        self.of_id = None
        self.of = None
        self.amending = False
        self._recalculating = False
        self._catalog_rows = {}
        self._default_discount = 0
        self._sticker_rate = 0
        self.branches: list[dict] = []
        self.dept_names = {r["id"]: r["name"] for r in masters.list_departments(conn)}
        self.dept_choices = [f"{i} {n}" for i, n in self.dept_names.items()]
        self._build()

    # ------------------------------------------------------------ layout

    def _build(self):
        # On a short window the page scrolls as a whole, so the grid never shrinks below a few rows that can be typed in.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroller = QScrollArea()
        self.scroller.setWidgetResizable(True)
        self.scroller.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        body.setObjectName("OFBody")
        self.scroller.setWidget(body)
        outer.addWidget(self.scroller)
        root = QVBoxLayout(body)
        root.setContentsMargins(20, 14, 20, 14)
        root.setSpacing(8)
        top = QHBoxLayout()
        self.back = button("Go back", self._leave)
        top.addWidget(self.back)
        top.addSpacing(8)
        self.title = label("New order form", "Title")
        top.addWidget(self.title)
        self.status = label()
        top.addWidget(self.status)
        top.addStretch(1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(6)
        top.addLayout(self.actions)
        root.addLayout(top)
        self.strip, self._mark_step = workflow_strip(STEPS)
        root.addWidget(self.strip)

        self.message = label("", "Notice")
        self.message.setWordWrap(True)
        self.message.hide()
        root.addWidget(self.message)

        header, host = panel()
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)
        self.supplier = search_combo()
        self.supplier.lineEdit().setPlaceholderText("Type a supplier code or name")
        self.of_date = date_edit()
        self.expected = date_edit(optional=True)
        self.arrangement = QComboBox()
        for code, text in orders.ARRANGEMENTS.items():
            self.arrangement.addItem(text, code)
        self.terms = terms_combo()
        self.instructions, self.remarks, self.warehouse_notes, self.internal = QLineEdit(), QLineEdit(), QLineEdit(), QLineEdit()
        self.instructions.setPlaceholderText("Prints on every branch PO")
        self.remarks.setPlaceholderText("Prints on the POs and the warehouse copy")
        self.warehouse_notes.setPlaceholderText("Warehouse copy only")
        self.internal.setPlaceholderText("Never on the supplier's papers. Shows on the Final OF for accounting")
        self.field_grid = grid
        self.field_widgets = {key: (label(text, "Muted"), getattr(self, key)) for key, text in of_layout.FIELDS.items()}
        self._place_fields()
        for col in range(5):
            grid.setColumnStretch(col, 1)
        host.addLayout(grid)
        root.addWidget(header)
        self.header_panel = header
        # Once it can't be edited, the boxes give way to the facts in two plain lines, so looking an OF up isn't a page of greyed-out fields.
        self.facts = label("", "PanelTitle")
        self.facts.setWordWrap(True)
        self.notes_line = label("", "Muted")
        self.notes_line.setWordWrap(True)
        root.addWidget(self.facts)
        root.addWidget(self.notes_line)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        root.addWidget(self.tabs, 1)
        self.tabs.addTab(self._allocation_tab(), "Branch allocation")
        self.po_table = make_table(["PO number", "Branch", "Pieces", "Total", "Status", "Issued"], numeric=(2, 3), stretch=1)
        set_widths(self.po_table, {0: 150, 2: 80, 3: 120, 4: 150, 5: 130})
        self.po_table.doubleClicked.connect(lambda *_: self._open_po())
        self.tabs.addTab(self.po_table, "Branch POs")
        self.history = make_table(["When", "Who", "Document", "What happened"], stretch=3)
        set_widths(self.history, {0: 150, 1: 170, 2: 130})
        self.tabs.addTab(self.history, "History")

        self.supplier.currentIndexChanged.connect(self._supplier_chosen)
        self.primary_save = None
        escape_shortcut(self, self._leave)
        self.save_shortcut = QShortcut(QKeySequence("Ctrl+S"), self, activated=lambda: self.primary_save and self.primary_save.isEnabled() and self.primary_save.click())
        self.add_shortcut = QShortcut(QKeySequence("Ctrl+N"), self, activated=self.add_row)

    def _place_fields(self):
        """Five to a row in the order an administrator chose. The supplier is wider, so it takes two."""
        # Take every field out before placing it again. Adding a widget that's already in the grid leaves Qt and PySide disagreeing about who owns it, which crashed the program later.
        for caption, widget in self.field_widgets.values():
            self.field_grid.removeWidget(caption)
            self.field_grid.removeWidget(widget)
        row, col = 0, 0
        previous = None
        for key in of_layout.field_order(self.conn):
            caption, widget = self.field_widgets[key]
            span = 2 if key == "supplier" else 1
            if col + span > 5:
                row, col = row + 2, 0
            self.field_grid.addWidget(caption, row, col, 1, span)
            self.field_grid.addWidget(widget, row + 1, col, 1, span)
            col += span
            if previous:
                QWidget.setTabOrder(previous, widget)
            previous = widget

    def apply_layout(self):
        self._place_fields()
        if self.branches:
            self._arrange_columns()

    def _arrange_columns(self):
        logical = {name: i for i, name in enumerate(of_layout.LEADING)} | self.after
        order = []
        for name in of_layout.column_order(self.conn):
            order += sorted(self.branch_cols.values()) if name == of_layout.BRANCHES else [logical[name]]
        for header in (self.grid.horizontalHeader(), self.footer.horizontalHeader()):
            for place, col in enumerate(order):
                header.moveSection(header.visualIndex(col), place)

    def _row_department(self, r: int) -> int | None:
        text = self._cell(r, C_DEPT)
        return int(text.split()[0]) if text else None

    def _reset_department(self):
        self.dept_filter = None
        self.dept_tabs.blockSignals(True)
        self.dept_tabs.setCurrentIndex(0)
        self.dept_tabs.blockSignals(False)

    def _set_department(self, dept: int | None):
        """Working in one department: only its rows show, and whatever is added belongs to it, so nobody types the department."""
        self.dept_filter = dept
        self._apply_department()
        if dept and self._editable():
            visible = [r for r in range(self.grid.rowCount()) if not self.grid.isRowHidden(r)]
            row = visible[-1] if visible else self.add_row()
            self.grid.setCurrentCell(row, C_CODE)
            self.grid.setFocus()

    def _apply_department(self):
        dept = self.dept_filter
        was, self._recalculating = self._recalculating, True
        for r in range(self.grid.rowCount()):
            # The empty row a new OF starts with, or one left unfinished, joins the department being worked on.
            if dept and self._editable() and not self._row_department(r) and self._row_values(r, strict=False) is None:
                self.grid.item(r, C_DEPT).setText(self.dept_labels[dept])
            self.grid.setRowHidden(r, dept is not None and self._row_department(r) != dept)
        self._recalculating = was
        self.grid.setColumnHidden(C_DEPT, dept is not None)
        self.footer.setColumnHidden(C_DEPT, dept is not None)
        self._fit_product()
        self._mark_departments()

    def _show_department_counts(self):
        counts = dict.fromkeys(self.dept_labels, 0)
        for r in range(self.grid.rowCount()):
            if self._row_values(r, strict=False) is not None:
                counts[None] += 1
                dept = self._row_department(r)
                if dept in counts:
                    counts[dept] += 1
        show_tab_counts(self.dept_tabs, self.dept_labels, counts)

    def _mark_departments(self):
        """The first row of each department shows it in bold. Repeats fade, so the eye reads one header per group."""
        starts, previous = set(), None
        for r in range(self.grid.rowCount()):
            cell = self.grid.item(r, C_DEPT)
            if cell is None:
                continue
            first = cell.text() != previous
            font = QFont(cell.font())
            font.setBold(first)
            cell.setFont(font)
            cell.setForeground(QColor(theme.current.TEXT if first else theme.current.FAINT))
            if first and r:
                starts.add(r)
            previous = cell.text()
        # Looking at one department there are no groups to divide.
        self.grid.group_starts = set() if self.dept_filter else starts
        self.grid.viewport().update()

    def _allocation_tab(self) -> QWidget:
        host = QWidget()
        outer = QVBoxLayout(host)
        outer.setContentsMargins(4, 8, 4, 8)
        outer.setSpacing(0)
        bar = QHBoxLayout()
        self.dept_filter = None
        self.dept_labels = {None: "All", **{i: f"{i} {n}" for i, n in self.dept_names.items()}}
        self.dept_tabs = register_tabs(self.dept_labels, self._set_department)
        outer.addWidget(self.dept_tabs)
        outer.addSpacing(6)
        self.catalog = QLineEdit()
        self.catalog.setPlaceholderText("Add from catalog: type a SKU or description, then pick it")
        self.catalog.setMinimumWidth(380)
        self._model = QStringListModel(self)
        comp = QCompleter(self._model, self)
        comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        comp.setFilterMode(Qt.MatchFlag.MatchContains)
        comp.setMaxVisibleItems(12)
        self.catalog.setCompleter(comp)
        self.catalog.textEdited.connect(self._catalog_search)
        comp.activated[str].connect(self._catalog_pick)
        bar.addWidget(self.catalog, 2)
        self.add_btn = button("Add row", self.add_row)
        self.add_btn.setToolTip("Ctrl+N")
        self.remove_btn = button("Remove row", self.remove_row)
        self.more = QCheckBox("More columns")
        self.more.setToolTip("Second discount, net unit, order quantity check, supplier code, brand, box, selling price, foreign currency, remarks. On a small screen, also the cost per piece")
        self.more.toggled.connect(lambda *_: self._apply_optional())
        for w in (self.add_btn, self.remove_btn, self.more):
            bar.addWidget(w)
        bar.addStretch(1)
        outer.addLayout(bar)
        outer.addSpacing(6)

        self.grid = DepartmentGrid(0, 0)
        self.grid.verticalHeader().setDefaultSectionSize(30)
        self.grid.verticalHeader().setMinimumWidth(28)
        self.grid.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.grid.setTabKeyNavigation(True)
        self.grid.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.grid.horizontalHeader().setHighlightSections(False)
        self.grid.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.grid.itemChanged.connect(self._cell_changed)
        self.grid.installEventFilter(self)
        # A wider or narrower grid decides how much room the product name gets.
        self._grid_viewport = self.grid.viewport()
        self._grid_viewport.installEventFilter(self)
        outer.addWidget(self.grid, 1)
        self.row_problem = label("", "Error")
        self.row_problem.setWordWrap(True)
        self.row_problem.hide()
        outer.addWidget(self.row_problem)

        # Stays under the grid with the same columns, so each branch's total sits below its own quantities.
        self.footer = QTableWidget(2, 0)
        self.footer.setObjectName("Footer")
        self.footer.horizontalHeader().hide()
        self.footer.verticalHeader().setDefaultSectionSize(26)
        self.footer.verticalHeader().setMinimumWidth(28)
        self.footer.setFixedHeight(54)
        self.footer.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.footer.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.footer.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.footer.setShowGrid(False)
        self.footer.setVerticalHeaderLabels(["", ""])
        self.footer.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.footer.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.grid.horizontalHeader().sectionResized.connect(lambda col, _, new: self.footer.setColumnWidth(col, new))
        self.grid.horizontalScrollBar().valueChanged.connect(self.footer.horizontalScrollBar().setValue)
        # A column the person drags wider or narrower stays that way when the OF is opened again or the screen refreshes, as on the lists.
        # The branches decide which column is which, so each set of branches keeps its own widths (see _layout_columns).
        self.grid_widths = WidthKeeper(self.grid, self._grid_widths_key([]))
        outer.addWidget(self.footer)
        outer.addSpacing(8)
        outer.addWidget(self._summary())
        return host

    def _summary(self) -> QWidget:
        host = QWidget()
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(18)
        self.vat_mode = QComboBox()
        self.vat_mode.addItem("VAT included in costs", VAT_INCLUSIVE)
        self.vat_mode.addItem("Add VAT on top", VAT_EXCLUSIVE)
        self.vat_mode.addItem("No VAT", VAT_NONE)
        self.disc_type = QComboBox()
        self.disc_type.addItem("None", DISC_NONE)
        self.disc_type.addItem("Percent", DISC_PERCENT)
        self.disc_type.addItem("Amount", DISC_AMOUNT)
        self.disc_value = QLineEdit()
        self.disc_value.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.disc_value.setPlaceholderText("Value")
        self.charges = QLineEdit()
        self.charges.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.charges.setPlaceholderText("0.00")
        self.charges_note = QLineEdit()
        self.charges_note.setPlaceholderText("What for, such as freight")
        controls = QGridLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setHorizontalSpacing(8)
        controls.setVerticalSpacing(6)
        # No order carries VAT for now, so there's nothing to choose. An older order that has VAT still shows it in the totals.
        self.vat_mode.hide()
        controls.addWidget(label("Overall discount", "Muted"), 1, 0)
        controls.addWidget(self.disc_type, 1, 1)
        controls.addWidget(self.disc_value, 1, 2)
        controls.addWidget(label("Other charges", "Muted"), 2, 0)
        controls.addWidget(self.charges, 2, 1)
        controls.addWidget(self.charges_note, 2, 2)
        controls.setRowStretch(3, 1)
        box = QWidget()
        box.setLayout(controls)
        box.setFixedWidth(370)
        self.money_controls = box
        row.addWidget(box)
        for w in (self.vat_mode, self.disc_type):
            w.currentIndexChanged.connect(self._recalculate)
        for w in (self.disc_value, self.charges):
            w.textChanged.connect(self._recalculate)

        self.dept_table = make_table(["Department", "Pieces", "Amount"], numeric=(1, 2), stretch=0)
        self.dept_table.verticalHeader().setDefaultSectionSize(24)
        set_widths(self.dept_table, {1: 70, 2: 100})
        row.addWidget(self.dept_table, 1)

        totals = QVBoxLayout()
        totals.setContentsMargins(0, 0, 0, 0)
        totals.setSpacing(2)
        lines = QGridLayout()
        lines.setVerticalSpacing(1)
        lines.setHorizontalSpacing(16)
        self.rows = {}
        for r, (key, text) in enumerate((("qty", "Total quantity"), ("gross", "Gross amount"), ("item", "Item discounts"), ("overall", "Overall discount"),
                                         ("vat", "VAT"), ("charges", "Other charges"), ("sticker", "Less stickers"), ("net", "To pay after stickers"))):
            name, value = label(text, "Muted"), label("")
            value.setAlignment(Qt.AlignmentFlag.AlignRight)
            lines.addWidget(name, r, 0)
            lines.addWidget(value, r, 1)
            self.rows[key] = (name, value)
        totals.addLayout(lines)
        total_row = QHBoxLayout()
        total_row.addWidget(label("OF total", "PanelTitle"))
        self.grand = label(fmt_peso(0), "Total")
        self.grand.setAlignment(Qt.AlignmentFlag.AlignRight)
        total_row.addWidget(self.grand, 1)
        totals.addLayout(total_row)
        self.approval_note = label("", "Muted")
        self.approval_note.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.approval_note.setWordWrap(True)
        totals.addWidget(self.approval_note)
        totals.addStretch(1)
        box2 = QWidget()
        box2.setLayout(totals)
        box2.setFixedWidth(340)
        row.addWidget(box2)
        host.setFixedHeight(166)
        self.summary_host = host
        return host

    # ------------------------------------------------------------ grid columns, which depend on the branches

    def _grid_widths_key(self, branches: list[dict]) -> str:
        return f"of-grid-{self.user.username}-" + ".".join(str(b["code"]) for b in branches)

    def _layout_columns(self, branches: list[dict]):
        self.branches = branches
        # The widths this person dragged for this set of branches, read before the starting widths so those don't cover them.
        self.grid_widths.load(self._grid_widths_key(branches))
        self.branch_cols = {b["code"]: len(of_layout.LEADING) + i for i, b in enumerate(branches)}
        base = len(of_layout.LEADING) + len(branches)
        self.after = {name: base + i for i, name in enumerate(of_layout.AFTER)}
        heads = of_layout.LEADING + [b["name"] for b in branches] + ["Total pcs" if a == "Total" else a for a in of_layout.AFTER]
        self._recalculating = True
        self.grid.setRowCount(0)
        self.grid.setColumnCount(len(heads))
        self.grid.setHorizontalHeaderLabels(heads)
        self.footer.setColumnCount(len(heads))
        # A column hidden for the last OF must not stay hidden here, where the same place may hold a branch.
        for col in range(len(heads)):
            self.grid.setColumnHidden(col, False)
            self.footer.setColumnHidden(col, False)
        self.grid.setItemDelegate(EnterMoves(self, self.grid))
        dept = ComboDelegate(self.dept_choices, self.grid)
        unit = ComboDelegate(list(OFFERED_UNITS), self.grid)
        self.grid.setItemDelegateForColumn(C_DEPT, dept)
        self.grid.setItemDelegateForColumn(C_UNIT, unit)
        self._right = {C_COST, C_D1, C_D2, *self.branch_cols.values(), *[self.after[n] for n in RIGHT_AFTER]}
        self._read_only = {self.after[n] for n in READ_ONLY_AFTER}
        header = self.grid.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for col in self._right:
            self.grid.horizontalHeaderItem(col).setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        # Narrow enough that the product name still has room on a 1366 laptop. The department tabs above say the department in full.
        # Color keeps room for 'Light Blue': it tells apart rows of the same stock code.
        set_widths(self.grid, {C_DEPT: 90, C_CODE: 78, C_SIZE: 54, C_COLOR: 86, C_UNIT: 64, C_COST: 74, C_D1: 62, C_D2: 62})
        for col in self.branch_cols.values():
            self.grid.setColumnWidth(col, 66)
        widths = {"Total": 60, "Per pc": PER_PC_WIDTH, "Net unit": 72, "Amount": 96, "Order qty": 66, "Supplier code": 96, "Brand": 80, "Box of": 52, "Selling": 72, "FX": 36,
                  "FX rate": 60, "FX cost": 60, "Remarks": 150}
        for name, width in widths.items():
            self.grid.setColumnWidth(self.after[name], width)
        self.grid_widths.restore()
        for col in range(self.grid.columnCount()):
            self.footer.setColumnWidth(col, self.grid.columnWidth(col))
        frame = 2 * self.grid.frameWidth()
        bar = self.grid.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)
        self.grid.setMinimumHeight(header.sizeHint().height() + MIN_GRID_ROWS * self.grid.verticalHeader().defaultSectionSize() + frame + bar)
        self._apply_optional()
        self._arrange_columns()
        self._recalculating = False

    def _apply_optional(self):
        if not self.branches:
            return
        on = self.more.isChecked()
        for col in [C_D2] + [self.after[name] for name in OPTIONAL]:
            self.grid.setColumnHidden(col, not on)
            self.footer.setColumnHidden(col, not on)
        # Product takes whatever is left, so nothing scrolls sideways. With every column open, it gets a fixed width and the grid scrolls.
        header = self.grid.horizontalHeader()
        header.setSectionResizeMode(C_DESC, QHeaderView.ResizeMode.Interactive if on else QHeaderView.ResizeMode.Stretch)
        if on:
            self.grid.setColumnWidth(C_DESC, self.grid_widths.kept.get(C_DESC, 220))
        self._fit_product()

    def _fit_product(self):
        """Product takes whatever room is left, but never less than PRODUCT_MIN. On a narrow screen Per pc steps back into More columns first;
        if that still isn't enough, the product keeps its room and the grid scrolls sideways a little."""
        if not self.branches:
            return
        per_pc = self.after["Per pc"]
        # Off screen the grid has no real width yet. It fits again when it's shown.
        if not self._editable() or self.more.isChecked() or not self.grid.isVisible():
            self.grid.setColumnHidden(per_pc, False)
            self.footer.setColumnHidden(per_pc, False)
            return
        g = self.grid
        others = sum(g.columnWidth(c) for c in range(g.columnCount()) if c not in (C_DESC, per_pc) and not g.isColumnHidden(c))
        room = g.viewport().width() - others
        narrow = room - PER_PC_WIDTH < PRODUCT_MIN
        if g.isColumnHidden(per_pc) != narrow:
            g.setColumnHidden(per_pc, narrow)
            self.footer.setColumnHidden(per_pc, narrow)
            if not narrow:
                g.setColumnWidth(per_pc, PER_PC_WIDTH)
        if not narrow:
            room -= PER_PC_WIDTH
        header = g.horizontalHeader()
        if room >= PRODUCT_MIN:
            header.setSectionResizeMode(C_DESC, QHeaderView.ResizeMode.Stretch)
        else:
            header.setSectionResizeMode(C_DESC, QHeaderView.ResizeMode.Interactive)
            g.setColumnWidth(C_DESC, max(PRODUCT_MIN, self.grid_widths.kept.get(C_DESC, PRODUCT_MIN)))

    # ------------------------------------------------------------ loading

    def _load_suppliers(self):
        self.supplier.blockSignals(True)
        self.supplier.clear()
        for s in masters.search_suppliers(self.conn, include_inactive=bool(self.of)):
            self.supplier.addItem(f"{s['code']}  {s['name']}", s["id"])
        self.supplier.setCurrentIndex(-1)
        self.supplier.blockSignals(False)

    def new_of(self):
        self.of_id, self.of, self.amending = None, None, False
        self._default_discount = 0
        self._sticker_rate = 0
        self._reset_department()
        self._load_suppliers()
        self._layout_columns([{"code": b["code"], "name": b["name"]} for b in of_service.active_branches(self.conn)])
        set_date(self.of_date, today().isoformat())
        set_date(self.expected, None)
        self.arrangement.setCurrentIndex(0)
        set_terms(self.terms)
        for w in (self.instructions, self.remarks, self.warehouse_notes, self.internal, self.disc_value, self.charges, self.charges_note):
            w.clear()
        select_data(self.vat_mode, VAT_NONE)
        self.disc_type.setCurrentIndex(0)
        self.add_row()
        self.title.setText("New order form")
        self.tabs.setCurrentIndex(0)
        self._apply_state()
        self.supplier.setFocus()
        self._recalculate()

    def open_of(self, of_id: int):
        self.of_id, self.amending = of_id, False
        self.of = of_service.get_of(self.conn, of_id)
        self._load_suppliers()
        self.tabs.setCurrentIndex(0)
        self._reset_department()
        self._show_of()

    def _show_of(self):
        o = self.of
        self._sticker_rate = o["sticker_rate_c"]
        self._layout_columns(o["branch_columns"])
        select_data(self.supplier, o["supplier_id"])
        set_date(self.of_date, o["of_date"])
        set_date(self.expected, o["expected_delivery_date"])
        select_data(self.arrangement, o["delivery_arrangement"])
        set_terms(self.terms, o["payment_terms"])
        self.instructions.setText(o["delivery_instructions"])
        self.remarks.setText(o["remarks"])
        self.warehouse_notes.setText(o["warehouse_notes"])
        self.internal.setText(o["internal_notes"])
        select_data(self.vat_mode, o["vat_mode"] if o["status"] != orders.DRAFT else VAT_NONE)
        select_data(self.disc_type, o["overall_discount_type"])
        value = o["overall_discount_value"]
        self.disc_value.setText("" if not value else fmt_percent(value).rstrip("%") if o["overall_discount_type"] == DISC_PERCENT else fmt_plain(value))
        self.charges.setText(fmt_plain(o["other_charges_c"]) if o["other_charges_c"] else "")
        self.charges_note.setText(o["other_charges_note"])
        self._recalculating = True
        for item in o["items"]:
            self._append(item)
        self._recalculating = False
        self._apply_department()
        if any(i["disc2_bp"] for i in o["items"]):  # a discount that changes the money should never sit out of sight
            self.more.setChecked(True)
        rev = f"  Rev. {o['revision']}" if o["revision"] else ""
        self.title.setText((o["of_number"] or f"Draft OF #{o['id']}") + rev)
        self._apply_state()
        self._recalculate()
        self._load_pos()
        self._load_history()

    def _append(self, it: dict | None = None) -> int:
        it = it or {}
        r = self.grid.rowCount()
        self.grid.insertRow(r)
        dept = it.get("department_id")
        values = {
            C_DEPT: f"{dept} {self.dept_names[dept]}" if dept else "", C_CODE: it.get("item_code", ""), C_DESC: it.get("description", ""),
            C_SIZE: it.get("size", ""), C_COLOR: it.get("color", ""), C_UNIT: it.get("uom") or "PC",
            C_COST: fmt_plain(it["unit_cost_c"]) if it.get("unit_cost_c") is not None else "",
            C_D1: fmt_percent(it.get("disc1_bp")).rstrip("%"), C_D2: fmt_percent(it.get("disc2_bp")).rstrip("%"),
            self.after["Supplier code"]: it.get("supplier_item_code", ""), self.after["Brand"]: it.get("brand", ""),
            self.after["Box of"]: str(it["box_size"]) if it.get("box_size") else "", self.after["Selling"]: fmt_plain(it["selling_c"]) if it.get("selling_c") else "",
            self.after["FX"]: it.get("fx_currency", ""), self.after["FX rate"]: fmt_milli(it.get("fx_rate_m")),
            self.after["FX cost"]: fmt_milli(it.get("fx_cost_m")), self.after["Remarks"]: it.get("remarks", ""),
        }
        allocated = it.get("allocations_m") or {}
        for code, col in self.branch_cols.items():
            values[col] = fmt_qty(allocated[code] * pieces_per_unit(it.get("uom"))) if allocated.get(code) else ""
        for c in range(self.grid.columnCount()):
            cell = QTableWidgetItem(values.get(c, "") or "")
            if c in self._right:
                cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if c in self._read_only:
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if c in self.branch_cols.values():
                cell.setBackground(QColor(theme.current.BRANCH_TINT))
            if c == self.after["Total"]:
                font = QFont(cell.font())
                font.setBold(True)
                cell.setFont(font)
            self.grid.setItem(r, c, cell)
        self.grid.item(r, C_DEPT).setData(Qt.ItemDataRole.UserRole, {"product_id": it.get("product_id")})
        return r

    # ------------------------------------------------------------ state

    def _editable(self) -> bool:
        if self.amending:
            return True
        return (self.of is None or self.of["status"] == orders.DRAFT) and self.user.can("of.edit")

    def _apply_state(self):
        editable = self._editable()
        fixed = editable and not self.amending
        for w in (self.supplier, self.of_date):
            w.setEnabled(fixed)
        for w in (self.expected, self.arrangement, self.terms, self.instructions, self.remarks, self.warehouse_notes, self.internal,
                  self.vat_mode, self.disc_type, self.disc_value, self.charges, self.charges_note):
            w.setEnabled(editable)
        for w in (self.catalog, self.add_btn, self.remove_btn):
            w.setEnabled(editable)
            w.setVisible(editable)
        self.grid.setEditTriggers(QAbstractItemView.EditTrigger.AllEditTriggers if editable else QAbstractItemView.EditTrigger.NoEditTriggers)
        self.header_panel.setVisible(editable)
        # Read only, the discount and charges boxes are just repeats of the totals beside them, and the product keeps room to be read.
        self.money_controls.setVisible(editable)
        if not editable:
            self.grid.horizontalHeader().setSectionResizeMode(C_DESC, QHeaderView.ResizeMode.Interactive)
            self.grid.setColumnWidth(C_DESC, self.grid_widths.kept.get(C_DESC, 200))
            self._fit_product()
        else:
            self._apply_optional()
        o = self.of
        if not editable and o is not None:
            facts = [o["supplier"]["name"], f"OF date {fmt_date(o['of_date'])}", f"Expected {fmt_date(o['expected_delivery_date'])}" if o.get("expected_delivery_date") else "Expected delivery to be confirmed",
                     orders.ARRANGEMENTS.get(o["delivery_arrangement"], o["delivery_arrangement"]), f"Terms {o['payment_terms'] or 'as agreed'}"]
            notes = [f"{name}: {o[k]}" for k, name in (("delivery_instructions", "Delivery"), ("remarks", "Remarks"), ("warehouse_notes", "Warehouse notes"), ("internal_notes", "Internal notes")) if o.get(k)]
            self.facts.setText("   |   ".join(facts))
            self.notes_line.setText("     ".join(notes))
        self.facts.setVisible(not editable and o is not None)
        self.notes_line.setVisible(not editable and o is not None and bool(self.notes_line.text()))
        for i in (1, 2):
            self.tabs.setTabEnabled(i, self.of is not None)
        self._status_label()
        self._build_actions()

    def _step(self):
        o = self.of
        if o is None or o["status"] == orders.DRAFT:
            return 0
        if o["status"] == orders.PENDING:
            return 1
        if o["status"] == orders.CANCELLED:
            return None
        if not o["pos"]:
            return 2
        return 4 if all(p["status"] != orders.APPROVED for p in o["pos"]) else 3

    def retheme(self):
        """Repaint the colours set on individual cells and labels. Nothing typed on the screen changes."""
        if self.of:
            self.status.setStyleSheet(chip_style(self.of["status"]))
        if self.approval_note.styleSheet():
            self.approval_note.setStyleSheet(f"color:{theme.current.RED};")
        was, self._recalculating = self._recalculating, True
        for r in range(self.grid.rowCount()):
            for col in self.branch_cols.values():
                self.grid.item(r, col).setBackground(QColor(theme.current.BRANCH_TINT))
            total = self.grid.item(r, self.after["Total"])
            if total.toolTip():
                total.setData(Qt.ItemDataRole.ForegroundRole, QColor(theme.current.RED))
        self._mark_departments()
        self._recalculating = was

    def _status_label(self):
        self._mark_step(self._step())
        self.message.hide()
        if not self.of:
            self.status.clear()
            self.status.setStyleSheet("")
            return
        code = self.of["status"]
        self.status.setText(orders.STATUS_LABELS[code])
        self.status.setStyleSheet(chip_style(code))
        if code == orders.DRAFT:
            rejected = [h for h in of_service.approval_history(self.conn, self.of_id) if h["decision"] == "REJECTED" and not h["amendment_id"]]
            old = (f"This draft hasn't been saved in over {of_service.DRAFT_WARN_DAYS} days. Check its costs against the supplier's quote, "
                   "then save and submit it, or cancel it if it's no longer needed." if of_service.draft_is_old(self.of) else "")
            if rejected:
                self._notice(f"Returned by {rejected[-1]['decided_name']}: {rejected[-1]['reason']}" + (f"\n{old}" if old else ""), "NoticeBad")
            elif old:
                self._notice(old, "Notice")
        elif code == orders.CANCELLED:
            self._notice(f"Cancelled: {self.of['cancel_reason']}", "NoticeBad")
        elif code == orders.APPROVED and not self.of["pos"]:
            self._notice("Approved. Print it from the Print tab when you're ready. The branch POs get their numbers at that moment.", "NoticeOk")
        pending = [a for a in of_service.amendments(self.conn, self.of_id) if a["status"] == orders.PENDING]
        if pending:
            self._notice(f"Amendment waiting for approval: {pending[0]['reason']}. Below are the current approved values.", "Notice")

    def _notice(self, text: str, name: str):
        self.message.setObjectName(name)
        self.message.style().unpolish(self.message)
        self.message.style().polish(self.message)
        self.message.setText(text)
        self.message.show()

    def _build_actions(self):
        while self.actions.count():
            w = self.actions.takeAt(0).widget()
            if w:
                w.setParent(None)
        self.primary_save = None
        after_danger = [False]

        def add(text, fn, name=""):
            # A gap between a red button and the next one, so a slip of the mouse doesn't land on it.
            if after_danger[0]:
                self.actions.addSpacing(DANGER_GAP)
            after_danger[0] = name == "Danger"
            b = button(text, fn, name)
            self.actions.addWidget(b)
            return b
        if self.amending:
            self.primary_save = add("Save amendment", self.save_amendment, "Primary")
            add("Discard changes", lambda: self.open_of(self.of_id))
            return
        status = self.of["status"] if self.of else orders.DRAFT
        u = self.user
        if status == orders.DRAFT and u.can("of.edit"):
            if self.of:
                add("Cancel OF", self.cancel_of, "Danger")
            self.primary_save = add("Save draft", self.save_draft)
            add("Save and submit", self.submit_of, "Primary")
        if status == orders.PENDING:
            if u.can("of.cancel"):
                add("Cancel OF", self.cancel_of, "Danger")
            if u.can("of.approve") and may_approve(self.conn, u, self.of_id, self.of["prepared_by"]):
                add("Reject", self.reject_of)
                add("Approve", self.approve_of, "Primary")
        if status == orders.APPROVED:
            if u.can("of.cancel") and of_service.can_cancel(self.conn, self.of_id):
                add("Cancel OF", self.cancel_of, "Danger")
            if not self.of["pos_generated_at"] and u.can("of.amend"):
                add("Amend", self.start_amendment)
            # One route each: the Print tab prints and issues, which makes the branch POs, and the Final OF prints from Payments.
            add("Warehouse OF PDF", self.make_pdf)
        if status == orders.CANCELLED and self.of["of_number"]:
            add("Warehouse OF PDF", self.make_pdf)

    # ------------------------------------------------------------ grid

    def add_row(self, item: dict | None = None):
        if not self._editable():
            return None
        last_dept = None
        if self.grid.rowCount():
            cell = self.grid.item(self.grid.rowCount() - 1, C_DEPT)
            text = cell.text() if cell else ""
            if text:
                last_dept = int(text.split()[0])
        data = dict(item) if isinstance(item, dict) else {"department_id": self.dept_filter or last_dept}
        if self._default_discount and not data.get("disc1_bp"):
            data["disc1_bp"] = self._default_discount
        self._recalculating = True
        r = self._append(data)
        self._recalculating = False
        self._recalculate()
        self.grid.setCurrentCell(r, C_CODE if data.get("department_id") else C_DEPT)
        return r

    def remove_row(self):
        row = self.grid.currentRow()
        if row < 0:
            return
        desc = self.grid.item(row, C_DESC).text()
        if desc and not confirm(self, "Remove product", f"Remove '{desc}' from this OF?", "Remove"):
            return
        self.grid.removeRow(row)
        self._recalculate()

    def advance(self):
        """Where Enter goes. Branch to branch along the row, then the first branch of the next row, adding one at the end."""
        r, c = self.grid.currentRow(), self.grid.currentColumn()
        if r < 0 or not self.branches:
            return
        # On screen order, not the order columns were created in, since an administrator can rearrange them.
        header = self.grid.horizontalHeader()
        shown = sorted((col for col in range(self.grid.columnCount()) if not self.grid.isColumnHidden(col) and col not in self._read_only), key=header.visualIndex)
        last = max(self.branch_cols.values())
        if header.visualIndex(c) >= header.visualIndex(last):
            below = next((x for x in range(r + 1, self.grid.rowCount()) if not self.grid.isRowHidden(x)), None)
            if below is None:
                if self._row_values(r, strict=False) is None:
                    return
                below = self.add_row()
            self.grid.setCurrentCell(below, C_CODE)
            return
        self.grid.setCurrentCell(r, next(col for col in shown if header.visualIndex(col) > header.visualIndex(c)))

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show) and obj is self._grid_viewport:
            self._fit_product()
            return False
        if obj is self.grid and event.type() == QEvent.Type.KeyPress and self._editable():
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.advance()
                return True
            if event.key() == Qt.Key.Key_Delete and self.grid.currentColumn() not in self._read_only and self.grid.currentItem():
                self.grid.currentItem().setText("")
                return True
        return super().eventFilter(obj, event)

    def _cell(self, r: int, c: int) -> str:
        item = self.grid.item(r, c)
        return item.text().strip() if item else ""

    def _cell_changed(self, item):
        if self._recalculating:
            return
        r, c = item.row(), item.column()
        if c in (self.after["FX rate"], self.after["FX cost"]):
            try:
                rate, cost = parse_milli(self._cell(r, self.after["FX rate"])), parse_milli(self._cell(r, self.after["FX cost"]))
            except ValueError:
                rate = cost = 0
            if rate and cost:
                self._recalculating = True
                self.grid.item(r, C_COST).setText(fmt_plain(fx_to_centavos(cost, rate)))
                self._recalculating = False
        self._recalculate()

    def _row_values(self, r: int, strict: bool = True) -> dict | None:
        """One grid row as an item. Blank rows give None. strict raises on bad numbers, otherwise it returns None."""
        branch_texts = {code: self._cell(r, col) for code, col in self.branch_cols.items()}
        if not any([self._cell(r, C_CODE), self._cell(r, C_DESC), self._cell(r, C_COST), *branch_texts.values()]):
            return None
        tag = self.grid.item(r, C_DEPT).data(Qt.ItemDataRole.UserRole) or {}
        try:
            dept_text = self._cell(r, C_DEPT)
            uom = (self._cell(r, C_UNIT) or "PC").upper()
            box = self._cell(r, self.after["Box of"])
            if box and not box.isdigit():
                raise ValueError("box size must be a whole number")
            selling = self._cell(r, self.after["Selling"])
            return {
                "product_id": tag.get("product_id"), "department_id": int(dept_text.split()[0]) if dept_text else None,
                "item_code": self._cell(r, C_CODE), "description": self._cell(r, C_DESC), "size": self._cell(r, C_SIZE), "color": self._cell(r, C_COLOR),
                "uom": uom, "pieces_per_unit": pieces_per_unit(uom), "unit_cost_c": parse_pesos(self._cell(r, C_COST)),
                "disc1_bp": parse_percent(self._cell(r, C_D1)), "disc2_bp": parse_percent(self._cell(r, C_D2)),
                "allocations_m": {code: parse_pieces(text, uom) for code, text in branch_texts.items() if text},
                "ordered_qty": self._cell(r, self.after["Order qty"]), "supplier_item_code": self._cell(r, self.after["Supplier code"]),
                "brand": self._cell(r, self.after["Brand"]), "box_size": int(box) if box else None, "selling_c": parse_pesos(selling) if selling else None,
                "fx_currency": self._cell(r, self.after["FX"]).upper(), "fx_rate_m": parse_milli(self._cell(r, self.after["FX rate"])),
                "fx_cost_m": parse_milli(self._cell(r, self.after["FX cost"])), "remarks": self._cell(r, self.after["Remarks"]),
            }
        except ValueError as exc:
            if strict:
                raise POError(f"Row {r + 1}: {exc}") from None
            return None

    def _collect_items(self) -> list[dict]:
        return [v for r in range(self.grid.rowCount()) if (v := self._row_values(r))]

    def _discount_value(self) -> int:
        kind, text = self.disc_type.currentData(), self.disc_value.text()
        return parse_percent(text) if kind == DISC_PERCENT else parse_pesos(text) if kind == DISC_AMOUNT else 0

    def _vat_rate(self) -> int:
        return self.of["vat_rate_bp"] if self.of else int(get_setting(self.conn, "tax.vat_rate_bp") or 1200)

    def _collect_header(self) -> dict:
        return {
            "of_date": date_value(self.of_date), "supplier_id": combo_value(self.supplier) if self.supplier.isEnabled() or not self.of else self.of["supplier_id"],
            "expected_delivery_date": date_value(self.expected), "delivery_arrangement": self.arrangement.currentData(), "payment_terms": self.terms.currentText(),
            "delivery_instructions": self.instructions.text(), "remarks": self.remarks.text(), "warehouse_notes": self.warehouse_notes.text(),
            "internal_notes": self.internal.text(), "overall_discount_type": self.disc_type.currentData(), "overall_discount_value": self._discount_value(),
            "vat_mode": self.vat_mode.currentData(), "vat_rate_bp": self._vat_rate(), "other_charges_c": parse_pesos(self.charges.text()),
            "other_charges_note": self.charges_note.text(),
        }

    def _typed_pieces(self, r: int) -> int:
        total = 0
        for col in self.branch_cols.values():
            try:
                total += parse_qty(self._cell(r, col), self._cell(r, C_UNIT) or "PC")
            except ValueError:
                pass
        return total

    def _mark_full_packs(self, r: int) -> str:
        """Turns a quantity that isn't a full pack red and returns what's wrong with the first one, so a row never just goes blank."""
        uom, first = self._cell(r, C_UNIT) or "PC", ""
        for col in self.branch_cols.values():
            cell, problem = self.grid.item(r, col), ""
            try:
                parse_pieces(cell.text(), uom)
            except ValueError as exc:
                problem = str(exc)
            cell.setData(Qt.ItemDataRole.ForegroundRole, QColor(theme.current.RED) if problem else None)
            cell.setToolTip(problem)
            first = first or problem
        return first

    def _recalculate(self, *_):
        if self._recalculating or not self.branches:
            return
        self._recalculating = True
        lines = []
        row_problem = ""
        order_col, total_col = self.after["Order qty"], self.after["Total"]
        for r in range(self.grid.rowCount()):
            v = self._row_values(r, strict=False)
            total_m, net, amount, each = 0, "", "", ""
            if v:
                total_m = sum(v["allocations_m"].values())
                try:
                    net_unit = compute_line(1, v["unit_cost_c"], v["disc1_bp"], v["disc2_bp"]).net_unit
                    net = fmt_plain(net_unit)
                    each = fmt_plain(per_piece(net_unit, v["pieces_per_unit"])) if v["unit_cost_c"] else ""
                    amount_c = sum(compute_line(m_to_qty(q), v["unit_cost_c"], v["disc1_bp"], v["disc2_bp"]).total for q in v["allocations_m"].values() if q > 0)
                    amount = fmt_plain(amount_c) if total_m else ""
                    if v["department_id"]:
                        lines.append({"department_id": v["department_id"], "pieces_per_unit": v["pieces_per_unit"], "unit_cost": v["unit_cost_c"],
                                      "disc1_bp": v["disc1_bp"], "disc2_bp": v["disc2_bp"], "allocations": {k: m_to_qty(q) for k, q in v["allocations_m"].items()}})
                except ValueError:
                    pass
            # Quantities are typed in pieces. One that isn't a full pack is marked, and the row still shows what was typed.
            pieces_m = total_m * v["pieces_per_unit"] if v else self._typed_pieces(r)
            problem = self._mark_full_packs(r)
            if problem and not row_problem:
                row_problem = f"Row {r + 1}: {problem}."
            total_cell = self.grid.item(r, total_col)
            total_cell.setText(fmt_qty(pieces_m) if pieces_m else "")
            wanted = self._cell(r, order_col).replace(",", "")
            try:
                mismatch = bool(wanted) and v is not None and parse_qty(wanted, v["uom"]) != pieces_m
            except ValueError:
                mismatch = True
            total_cell.setData(Qt.ItemDataRole.ForegroundRole, QColor(theme.current.RED) if mismatch else None)
            total_cell.setToolTip(f"The branch quantities don't add up to the order quantity of {wanted} pieces." if mismatch else "")
            self.grid.item(r, self.after["Net unit"]).setText(net)
            self.grid.item(r, self.after["Per pc"]).setText(each)
            self.grid.item(r, self.after["Amount"]).setText(amount)
        self._mark_departments()
        self._show_department_counts()
        self.row_problem.setText(row_problem)
        self.row_problem.setVisible(bool(row_problem))
        self._recalculating = False
        # Once POs exist they are the record, including any that were cancelled or amended, so the money comes from them.
        generated = bool(self.of and self.of["pos"] and not self._editable())
        try:
            if generated:
                t = compute_order(lines, vat_mode=VAT_NONE)
            else:
                t = compute_order(lines, self.disc_type.currentData(), self._discount_value(), self.vat_mode.currentData(), self._vat_rate(), parse_pesos(self.charges.text()))
        except ValueError as exc:
            self.approval_note.setText(str(exc))
            self.approval_note.setStyleSheet(f"color:{theme.current.RED};")
            return
        self.approval_note.setStyleSheet("")
        if generated:
            t.total_qty, t.total_pieces = m_to_qty(self.of["total_qty_m"]), m_to_qty(self.of["total_pieces_m"])
            t.gross, t.item_discount, t.overall_discount = self.of["gross_c"], self.of["item_discount_c"], self.of["overall_discount_c"]
            t.vat, t.other_charges, t.grand_total = self.of["vat_c"], self.of["other_charges_c"], self.of["grand_total_c"]
        self.rows["qty"][1].setText(f"{t.total_pieces:,} pcs" + (f" ({t.total_qty:,} packs)" if t.total_pieces != t.total_qty else ""))
        deduction = sticker_deduction(t.total_pieces, self._sticker_rate)
        for key in ("sticker", "net"):
            for w in self.rows[key]:
                w.setVisible(bool(deduction))
        self.rows["sticker"][0].setText(f"Less stickers, {t.total_pieces:,} pcs x {fmt_plain(self._sticker_rate)}")
        self.rows["sticker"][1].setText("-" + fmt_peso(deduction))
        self.rows["net"][1].setText(fmt_peso(t.grand_total - deduction))
        self.summary_host.setFixedHeight(206 if deduction else 166)
        self.rows["gross"][1].setText(fmt_peso(t.gross))
        self.rows["item"][1].setText("-" + fmt_peso(t.item_discount) if t.item_discount else fmt_peso(0))
        self.rows["overall"][1].setText("-" + fmt_peso(t.overall_discount) if t.overall_discount else fmt_peso(0))
        self.rows["vat"][0].setText("VAT included" if self.vat_mode.currentData() == VAT_INCLUSIVE else "VAT")
        self.rows["vat"][1].setText(fmt_peso(t.vat))
        for w in self.rows["vat"]:
            w.setVisible(bool(t.vat))
        self.rows["charges"][1].setText(fmt_peso(t.other_charges))
        self.grand.setText(fmt_peso(t.grand_total))
        self._approval_note(t)
        self._fill_footer(t)
        fill_table(self.dept_table, [[f"{d} {self.dept_names[d]}", f"{x.pieces:,}", fmt_plain(x.amount)] for d, x in sorted(t.departments.items())])

    def _approval_note(self, t):
        p = orders.policy(self.conn)
        amount = of_service.approval_amount(self.conn, t)
        if self.of and self.of["status"] == orders.APPROVED:
            note = f"Approved by {self.of['approved_by_name']}." if self.of["approved_by_name"] else "Approved within the limit."
        elif not p["enabled"]:
            note = "Approval policy is off. This OF is approved when submitted."
        elif orders.needs_approval(self.conn, amount):
            note = f"Needs approval: above {fmt_peso(p['threshold_c'])}."
        else:
            note = f"No approval needed: within {fmt_peso(p['threshold_c'])}."
        if len(t.departments) > 1 and self._editable() and not self.amending:
            note = f"{note} Submitting makes {len(t.departments)} Order Forms, one per department."
        self.approval_note.setText(note)

    def _fill_footer(self, t):
        def put(row, col, text, right=True):
            cell = QTableWidgetItem(text)
            if right:
                cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.footer.setItem(row, col, cell)

        self.footer.setColumnWidth(C_DESC, self.grid.columnWidth(C_DESC))
        self.footer.clearContents()
        put(0, C_DESC, "Pieces", right=False)
        put(1, C_DESC, "Amount", right=False)
        live = {p["branch_code"]: p["grand_total_c"] for p in self.of["pos"] if p["status"] != orders.CANCELLED} if self.of and not self._editable() else {}
        for code, col in self.branch_cols.items():
            b = t.branches.get(code)
            put(0, col, fmt_qty(qty_to_m(b.total_pieces)) if b else "0")
            amount = live.get(code, b.grand_total if b else 0)
            # Whole pesos fit the narrow column. The exact figure is on the PO and in the tooltip.
            put(1, col, f"{amount // 100:,}")
            self.footer.item(1, col).setToolTip(fmt_peso(amount))
        put(0, self.after["Total"], f"{t.total_pieces:,}")
        put(1, self.after["Amount"], fmt_plain(t.grand_total))

    # ------------------------------------------------------------ catalog

    def _catalog_search(self, text: str):
        if len(text.strip()) < 2:
            return
        supplier = combo_value(self.supplier) if self.supplier.isEnabled() else None
        found = masters.search_products(self.conn, text, department_id=self.dept_filter, supplier_id=supplier, limit=40)
        self._catalog_rows = {f"{p['item_code']}  |  {p['description']}": p for p in found}
        self._model.setStringList(list(self._catalog_rows))

    def _catalog_pick(self, text: str):
        p = self._catalog_rows.get(text)
        if p is None:
            return
        cost = masters.last_purchase_cost(self.conn, p["id"])
        item = {"department_id": p["department_id"], "product_id": p["id"], "item_code": p["item_code"], "description": p["description"], "brand": p["brand"],
                "size": p["size"], "color": p["color"], "uom": p["uom"], "box_size": p["box_size"], "supplier_item_code": p["supplier_item_code"],
                "unit_cost_c": cost, "selling_c": p["selling_c"]}
        # Fill the empty row left by a new OF instead of leaving it behind.
        last = self.grid.rowCount() - 1
        if last >= 0 and self._row_values(last, strict=False) is None:
            self.grid.removeRow(last)
        r = self.add_row(item)
        self.grid.setCurrentCell(r, min(self.branch_cols.values()))
        self.catalog.clear()

    def _supplier_chosen(self, *_):
        supplier_id = combo_value(self.supplier)
        if supplier_id is None or self.of:
            return
        row = self.conn.execute("SELECT payment_terms, vat_mode, default_discount_bp, sticker_rate_c FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
        if row:
            set_terms(self.terms, row["payment_terms"])
            self._default_discount = row["default_discount_bp"]
            self._sticker_rate = row["sticker_rate_c"]
            self._recalculate()

    # ------------------------------------------------------------ actions

    def _save(self) -> int:
        header, items = self._collect_header(), self._collect_items()
        if self.of_id is None:
            self.of_id = of_service.create_draft(self.conn, self.user, header, items)
        else:
            of_service.update_draft(self.conn, self.user, self.of_id, header, items)
        self.of = of_service.get_of(self.conn, self.of_id)
        self.changed.emit()
        return self.of_id

    def save_draft(self):
        self._save()
        self._show_of()

    def submit_of(self):
        if not self._collect_items():
            raise POError("Add at least one product before submitting.")
        self._save()
        results = of_service.submit_order(self.conn, self.user, self.of_id)
        self.open_of(results[0][0])
        self.changed.emit()
        if len(results) > 1:
            lines = []
            for of_id, _status in results:
                o = of_service.get_of(self.conn, of_id)
                names = ", ".join(sorted({i["department"] for i in o["items"]}))
                lines.append(f"{o['of_number'] or f'Draft OF #{of_id}'}  {names}  {fmt_peso(o['grand_total_c'])}  {orders.STATUS_LABELS[o['status']]}")
            info(self, "One Order Form per department", "\n".join(lines))
            return
        # Said on the screen itself, not in a box that needs an extra press.
        result = results[0][1]
        if result == orders.PENDING:
            self._notice("Sent for approval. It gets its number when approved.", "NoticeOk")
        else:
            self._notice(f"{self.of['of_number']} is approved. It is now on Print.", "NoticeOk")

    def approve_of(self):
        # The same question as Approve on the Approvals page, shared from common: which OF, from whom, for how much, and optional remarks.
        what = f"{self.of['of_number'] or f'draft OF #{self.of_id}'}, {self.of['supplier']['name']}, {fmt_peso(self.of['grand_total_c'])}"
        remarks = ask_approve(self, what)
        if remarks is None:
            return
        number = of_service.approve(self.conn, self.user, self.of_id, remarks)
        self.open_of(self.of_id)
        self.changed.emit()
        self._notice(f"{number} is approved. It is now on Print.", "NoticeOk")

    def reject_of(self):
        reason = ask_text(self, "Reject", "Why is this OF being rejected? The purchaser will see this.")
        if reason is None:
            return
        of_service.reject(self.conn, self.user, self.of_id, reason)
        self.open_of(self.of_id)
        self.changed.emit()

    def generate(self):
        o = self.of
        lines = [f"{b['name']}: {fmt_qty(sum(i['allocations_m'].get(b['code'], 0) * i['pieces_per_unit'] for i in o['items']))} pcs" for b in o["branch_columns"]
                 if any(i["allocations_m"].get(b["code"]) for i in o["items"])]
        if not confirm(self, "Generate branch purchase orders", f"Create {len(lines)} purchase orders from {o['of_number']}?\n\n" + "\n".join(lines) +
                       "\n\nEach gets its own official number, and those numbers are never reused.", "Generate"):
            return
        made = of_service.generate_pos(self.conn, self.user, self.of_id)
        self.open_of(self.of_id)
        self.tabs.setCurrentIndex(1)
        self.changed.emit()
        info(self, "Generated", "\n".join(f"{p['po_number']}  {p['branch_name']}" for p in made))

    def cancel_of(self):
        name = self.of["of_number"] or f"Draft OF #{self.of_id}"
        extra = f" Its {len(self.of['pos'])} branch POs are cancelled with it." if self.of["pos"] else ""
        reason = ask_text(self, "Cancel OF", f"Why is {name} being cancelled?{extra} Numbers are never used again.")
        if reason is None:
            return
        of_service.cancel(self.conn, self.user, self.of_id, reason)
        self.open_of(self.of_id)
        self.changed.emit()

    def make_pdf(self):
        if pdf.warehouse_printed(self.conn, self.of_id) and not confirm_reprint(self, f"The warehouse copy of {self.of['of_number']}"):
            return
        default = str(Path.home() / "Documents" / f"{self.of['of_number']} Warehouse.pdf")
        path = save_file(self, "Save the warehouse copy", default, "PDF (*.pdf)")
        if path:
            pdf.warehouse_of(self.conn, self.user, self.of_id, path)
            os.startfile(path)  # opens in the PDF viewer, where it can be printed
            self._load_history()

    def make_office_pdf(self):
        if pdf.office_printed_at(self.conn, self.of_id) and not confirm_reprint(self, f"The office copy of {self.of['of_number']}"):
            return
        default = str(Path.home() / "Documents" / f"{self.of['of_number']} Office.pdf")
        path = save_file(self, "Save the office copy", default, "PDF (*.pdf)")
        if path:
            pdf.office_of(self.conn, self.user, self.of_id, path)
            os.startfile(path)
            self._load_history()

    def start_amendment(self):
        self.amending = True
        self._apply_state()
        self._notice("Amending. The supplier and OF date stay as they are. More quantity or a higher total goes to the approver.", "Notice")

    def save_amendment(self):
        header, items = self._collect_header(), self._collect_items()
        reason = ask_text(self, "Amend OF", "What is changing and why? This is kept in the OF's history.")
        if reason is None:
            return
        result = of_service.amend(self.conn, self.user, self.of_id, header, items, reason)
        self.open_of(self.of_id)
        self.changed.emit()
        if result["status"] == "APPLIED":
            info(self, "Amended", f"Saved as revision {result['revision']}.")
        else:
            info(self, "Sent for approval", "This amendment increases the OF, so it waits for the approver.")

    def _leave(self):
        if self._editable() and self._dirty() and not confirm(self, "Leave this OF", "Leave without saving your changes?", "Leave"):
            return
        self.closed.emit()

    def _dirty(self) -> bool:
        if self.amending:
            return True
        try:
            items = self._collect_items()
            header = self._collect_header()
        except (POError, ValueError):
            return True
        if self.of is None:
            return bool(items) or combo_value(self.supplier) is not None
        if self.of["status"] != orders.DRAFT:
            return False
        if any((header[k] or "") != (self.of[k] or "") for k in of_service.HEADER_FIELDS if k != "supplier_id"):
            return True
        keys = ("department_id", "description", "item_code", "size", "color", "uom", "unit_cost_c", "disc1_bp", "disc2_bp", "allocations_m")
        return [tuple(i[k] for k in keys) for i in self.of["items"]] != [tuple(i[k] for k in keys) for i in items]

    # ------------------------------------------------------------ tabs

    def _load_pos(self):
        pos = self.of["pos"]
        fill_table(self.po_table, [[p["po_number"], f"{p['branch_code']} {p['branch_name']}", fmt_qty(p["total_pieces_m"]), fmt_plain(p["grand_total_c"]), p["status"],
                                    fmt_date(p["issued_at"])] for p in pos], keys=[p["id"] for p in pos], status_col=4)
        self.tabs.setTabText(1, f"Branch POs ({len(pos)})" if pos else "Branch POs")

    def _open_po(self):
        key = selected_key(self.po_table)
        if key:
            self.open_po.emit(key)

    def _load_history(self):
        fill_table(self.history, [[fmt_datetime(r["occurred_at"]), r["user_name"], r["document_number"] or "", r["summary"] or r["action"]]
                                  for r in of_service.audit_trail(self.conn, self.of_id)])


# Off until someone chooses them under Columns.
EXTRA_COLUMNS = {"name": ("Supplier name", 220), "delivery": ("Delivery date", 100), "terms": ("Terms", 80), "departments": ("Departments", 170), "branches": ("Branches", 76),
                 "products": ("Products", 76), "prepared": ("Prepared by", 150), "external": ("External PO", 100), "remarks": ("Remarks", 220), "dr": ("DR number", 150)}


def extra_column_values(key, row, people, department_names) -> list[str]:
    """The optional columns, in EXTRA_COLUMNS order. Some only mean something on an OF, or only on a PO."""
    is_of = key[0] == "OF"
    departments = ", ".join(department_names.get(int(d), d) for d in row["depts"].split(",")) if row["depts"] else ""
    prepared = people.get(row["prepared_by"])
    return [row["supplier"], fmt_date(row["expected_delivery_date"]), row["payment_terms"], departments, str(row["branch_count"]) if is_of else "",
            str(row["product_count"]), prepared.name if prepared else "", "" if is_of else row["legacy_po_number"], row["remarks"], "" if is_of else row["dr_numbers"] or ""]


def choose_columns(tree, key: str, shown: dict[str, int], first_extra: int, branches: list = (), delivery_on: bool = False) -> ColumnChooser:
    """The first column stays: it holds the number and the arrow that opens an OF's POs."""
    for col, (_, width) in enumerate(EXTRA_COLUMNS.values(), first_extra):
        tree.setColumnWidth(col, width)
    first_delivery = first_extra + len(EXTRA_COLUMNS)
    delivery = {name: col for col, name in enumerate(["delivered"] + [f"branch{b['code']}" for b in branches], first_delivery)}
    for col in delivery.values():
        tree.setColumnWidth(col, 96)
    names = {**shown, **{name: col for col, name in enumerate(EXTRA_COLUMNS, first_extra)}, **delivery}
    return ColumnChooser(tree, key, names, tuple(EXTRA_COLUMNS) + (() if delivery_on else tuple(delivery)))


def delivery_headers(branches: list) -> list[str]:
    return ["Delivered"] + [b["name"] for b in branches]


def delivered_with_detail(conn) -> dict[int, dict]:
    """receiving.delivered_by_po, with each PO's pieces split the way the supplier is paid for them: shown_m is no more than was ordered of each product,
    extra_m is what came over the order or wasn't on the PO, and lost_m is what the Central Warehouse sent that never reached the branch. Display only."""
    delivered = receiving.delivered_by_po(conn)
    for d in delivered.values():
        d["shown_m"], d["extra_m"], d["lost_m"] = d["received_m"], 0, 0
    counted = {}
    # Only POs with an agreed count can have pieces over the order or lost on the way, and only deliveries the branch has agreed a count for are set against what the warehouse sent.
    for r in conn.execute(
            "SELECT i.po_id, i.qty_m * i.pieces_per_unit AS ordered_m,"
            f" (SELECT COALESCE(SUM(r.pieces_m), 0) FROM branch_received r WHERE r.po_item_id = i.id AND {receiving._IN_FORCE}) AS received_m,"
            " (SELECT SUM(w.pieces_m) FROM warehouse_delivery_items w JOIN warehouse_deliveries d ON d.id = w.delivery_id WHERE d.po_id = i.po_id AND w.po_item_id = i.id"
            "  AND EXISTS (SELECT 1 FROM branch_counts b WHERE b.po_id = d.po_id AND b.delivery_no = d.delivery_no AND b.status = 'APPROVED')) AS central_m"
            " FROM purchase_order_items i WHERE i.po_id IN (SELECT po_id FROM branch_counts WHERE status = 'APPROVED')"):
        supplier = r["received_m"] if r["central_m"] is None else max(r["received_m"], r["central_m"])
        c = counted.setdefault(r["po_id"], {"shown_m": 0, "extra_m": 0, "lost_m": 0})
        c["shown_m"] += min(supplier, r["ordered_m"])
        c["extra_m"] += max(supplier - r["ordered_m"], 0)
        c["lost_m"] += 0 if r["central_m"] is None else max(r["central_m"] - r["received_m"], 0)
    # Products that weren't on the PO are all extra.
    for po_id, pieces in conn.execute(f"SELECT r.po_id, SUM(r.pieces_m) FROM branch_received r WHERE r.po_item_id < 0 AND {receiving._IN_FORCE} GROUP BY r.po_id"):
        counted.setdefault(po_id, {"shown_m": 0, "extra_m": 0, "lost_m": 0})["extra_m"] += pieces or 0
    for po_id, c in counted.items():
        if po_id in delivered:
            delivered[po_id].update(c)
    return delivered


def _shown(r: dict) -> int:
    return r.get("shown_m", r["received_m"])


def _delivered_text(rows: list[dict]) -> str:
    if not rows:
        return ""
    if all(not r["counted"] and r["status"] == orders.CLOSED for r in rows):
        return f"{fmt_qty(sum(r['ordered_m'] for r in rows))}, closed"
    text = f"{fmt_qty(sum(_shown(r) for r in rows))} of {fmt_qty(sum(r['ordered_m'] for r in rows))}"
    extra, lost = sum(r.get("extra_m", 0) for r in rows), sum(r.get("lost_m", 0) for r in rows)
    if extra:
        text += f", {fmt_qty(extra)} extra"
    if lost:
        text += f", {fmt_qty(lost)} lost on the way"
    return text


def add_delivery(item, first_col: int, key, delivered: dict, branches: list) -> None:
    """Pieces delivered in total and to each branch. Green once it's all there, amber once some is.
    Pieces over the order, or not on it, and pieces lost on the way to the branch read separately, so the figure matches what the supplier is paid for."""
    mine = [r for r in delivered.values() if (r["id"] == key[1] if key[0] == "PO" else r["of_id"] == key[1])]
    groups = [mine] + [[r for r in mine if r["branch_code"] == b["code"]] for b in branches]
    for col, rows in enumerate(groups, first_col):
        text = _delivered_text(rows)
        item.setText(col, text)
        if "extra" in text or "lost" in text:
            item.setToolTip(col, text)
        item.setTextAlignment(col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        received, ordered = sum(_shown(r) for r in rows), sum(r["ordered_m"] for r in rows)
        if rows and received == ordered:
            item.setForeground(col, QColor(theme.current.GREEN))
        elif received:
            item.setForeground(col, QColor(theme.current.AMBER))


class DocumentPanel(QWidget):
    """What an OF or PO holds, beside the list, so a quick look needs no new screen. Only its button opens the whole thing."""
    open_doc = Signal(str, int)
    closed = Signal()

    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn, self.key = conn, None
        self.department_names = {d["id"]: d["name"] for d in masters.list_departments(conn)}
        self.setMinimumWidth(380)
        col = QVBoxLayout(self)
        col.setContentsMargins(10, 0, 0, 0)
        col.setSpacing(6)
        head = QHBoxLayout()
        self.title = label("", "PanelTitle")
        head.addWidget(self.title)
        self.status = label("")
        head.addWidget(self.status)
        head.addStretch(1)
        # Optional: an OF's pieces for each branch, one column each. Remembered for this Windows user.
        self.by_branch = QCheckBox("Pieces per branch")
        try:
            self.by_branch.setChecked(theme._settings().value("orders/panel-by-branch", True, type=bool))
        except Exception:
            pass
        self.by_branch.toggled.connect(self._branch_toggled)
        head.addWidget(self.by_branch)
        head.addWidget(button("Close", self.closed.emit, lock=False))
        col.addLayout(head)
        self.facts = label("", "Muted")
        self.facts.setWordWrap(True)
        col.addWidget(self.facts)
        self.notes = label("", "Muted")
        self.notes.setWordWrap(True)
        col.addWidget(self.notes)
        # Narrow, so only what a quick look needs: the product, how many pieces, and what they cost. The whole OF has every column.
        self.items = make_table(["Product", "Pieces", "Per pc", "Amount"], numeric=(1, 2, 3), stretch=0)
        set_widths(self.items, {1: 60, 2: 70, 3: 90})
        self.items.setWordWrap(True)
        col.addWidget(self.items, 2)
        # For an OF, its branch POs: which number went to which branch, and where each stands.
        self.branches = make_table(["Branch", "PO number", "Pieces", "Status"], numeric=(2,), stretch=1)
        set_widths(self.branches, {0: 90, 2: 56, 3: 110})
        col.addWidget(self.branches, 1)
        self.open_btn = button("Open the whole OF", self._open, "Primary")
        col.addWidget(left_aligned(self.open_btn))

    def show_doc(self, key):
        self.key = key
        if key is None:
            for w in (self.title, self.status, self.facts, self.notes):
                w.clear()
            self.items.setRowCount(0)
            self.branches.hide()
            self.open_btn.hide()
            return
        if key[0] == "OF":
            doc = of_service.get_of(self.conn, key[1])
            number = doc["of_number"] or f"Draft OF #{doc['id']}"
            facts = [doc["supplier"]["name"], f"OF date {fmt_date(doc['of_date'])}", f"{len([p for p in doc['pos'] if p['status'] != orders.CANCELLED])} branch POs" if doc["pos"] else "POs not made yet"]
            quantity = "total_qty_m"
        else:
            doc = orders.get_po(self.conn, key[1])
            number = doc["po_number"] or f"Draft PO #{doc['id']}"
            facts = [doc["supplier"]["name"], doc["branch"]["name"], f"PO date {fmt_date(doc['po_date'])}"]
            quantity = "qty_m"
        if doc.get("expected_delivery_date"):
            facts.append(f"Expected {fmt_date(doc['expected_delivery_date'])}")
        departments = sorted({i["department_id"] for i in doc["items"]})
        facts += [", ".join(f"{d} {self.department_names.get(d, '')}".strip() for d in departments), f"Terms {doc['payment_terms'] or 'as agreed'}", f"Total {fmt_peso(doc['grand_total_c'])}"]
        self.title.setText(number)
        self.status.setText(orders.STATUS_LABELS.get(doc["status"], doc["status"]))
        self.status.setStyleSheet(chip_style(doc["status"]))
        self.facts.setText("  |  ".join(f for f in facts if f))
        notes = [f"{name}: {doc[k]}" for k, name in (("remarks", "Remarks"), ("cancel_reason", "Cancelled"), ("close_reason", "Closed")) if doc.get(k)]
        self.notes.setText("   ".join(notes))
        self.notes.setVisible(bool(notes))
        # With Pieces per branch on, an OF shows a column for each branch that gets any, between the product and its total pieces.
        branches = [b for b in doc.get("branch_columns", []) if any(i["allocations_m"].get(b["code"]) for i in doc["items"])] if key[0] == "OF" and self.by_branch.isChecked() else []
        self.by_branch.setVisible(key[0] == "OF")
        self._columns([b["name"] for b in branches])
        rows = []
        for i in doc["items"]:
            discount = " + ".join(x for x in (fmt_percent(i["disc1_bp"]), fmt_percent(i["disc2_bp"])) if x)
            product = " ".join(x for x in (i["item_code"], i["description"]) if x) + "".join(f", {x}" for x in (i["size"], i["color"]) if x) + (f", less {discount}" if discount else "")
            split = [fmt_qty(i["allocations_m"].get(b["code"], 0) * i["pieces_per_unit"]) if i["allocations_m"].get(b["code"]) else "" for b in branches]
            rows.append([product, *split, fmt_qty((i[quantity] or 0) * i["pieces_per_unit"]), fmt_plain(per_piece(i["net_unit_c"], i["pieces_per_unit"])), fmt_plain(i["line_total_c"])])
        fill_table(self.items, rows)
        self.items.resizeRowsToContents()
        pos = doc.get("pos") if key[0] == "OF" else None
        if pos:
            fill_table(self.branches, [[p["branch_name"], p["po_number"] or "", fmt_qty(p["total_pieces_m"]), orders.STATUS_LABELS.get(p["status"], p["status"])] for p in pos])
        self.branches.setVisible(bool(pos))
        self.open_btn.setText("Open the whole OF" if key[0] == "OF" else "Open the whole PO")
        self.open_btn.show()

    def _columns(self, branch_names: list[str]):
        headers = ["Product", *branch_names, "Pieces", "Per pc", "Amount"]
        self.items.setColumnCount(len(headers))
        self.items.setHorizontalHeaderLabels(headers)
        numeric = list(range(1, len(headers)))
        self.items.setProperty("numeric", numeric)
        for c in numeric:
            self.items.horizontalHeaderItem(c).setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header = self.items.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        n = len(branch_names)
        if n:
            # The product keeps room to be read, and the table scrolls sideways if the branches need more.
            self.items.setColumnWidth(0, 200)
        else:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in range(1, 1 + n):
            self.items.setColumnWidth(c, max(48, self.items.fontMetrics().horizontalAdvance(headers[c]) + 16))
        set_widths(self.items, {1 + n: 58, 2 + n: 64, 3 + n: 84})

    def _branch_toggled(self, on: bool):
        try:
            theme._settings().setValue("orders/panel-by-branch", on)
        except Exception:
            pass
        if self.key:
            self.show_doc(self.key)

    def _open(self):
        if self.key:
            self.open_doc.emit(self.key[0], self.key[1])


def newest_first(ofs: list, legacy: list) -> list[tuple[str, dict]]:
    """OFs and POs from before OFs as one list, newest first: by date, then number. A draft, with no number yet, is the newest of its day. ("OF", row) or ("PO", row)."""
    rows = [("OF", r) for r in ofs] + [("PO", r) for r in legacy]
    def key(pair):
        kind, r = pair
        number = r["of_number"] if kind == "OF" else r["po_number"]
        return (r["of_date"] if kind == "OF" else r["po_date"]) or "", not number, number or "", r["id"]
    return sorted(rows, key=key, reverse=True)


def search_changed(box, timer: QTimer, refresh, text: str) -> None:
    """Typed text waits for a 300 ms pause before the list is looked up again. Text the program puts in, or a cleared box, shows at once."""
    if box.isModified() and text.strip():
        timer.start()
    else:
        refresh()


class OrdersPage(QWidget):
    """The work list: orders still open, those completed lately, and cancelled ones, which are kept for good. Older completed orders are in Inquiry, so this list stays short.
    An OF row opens with one click to show its branch POs, and says what to do next."""
    new_of = Signal()
    open_of = Signal(int)
    open_po = Signal(int)
    go_approvals = Signal()
    # The OF to tick on Print, so it needs no finding again there.
    go_print = Signal(int)
    go_completed = Signal()
    changed = Signal()
    RECENT_DAYS = 30
    TABS = {orders.OPEN: "Open", orders.COMPLETED: "Recently completed", orders.CANCELLED: "Cancelled"}

    C_NUMBER, C_DATE, C_WHO, C_QTY, C_TOTAL, C_STATUS, C_PRINTED, C_NEXT = range(8)
    HEADERS = ["Order / PO", "Date", "Supplier / branch", "Pieces", "Total", "Status", "Printed", "Next step"]

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self.conn, self.user = conn, user
        self.people, self.department_names = {}, {}
        self.tab = orders.OPEN
        self.status_filter = None
        self.rows, self.legacy_rows, self.po_rows = [], [], {}
        self.expanded: set[int] = set()
        self.current_key = None
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 14, 20, 14)
        root.setSpacing(10)
        top = QHBoxLayout()
        top.addWidget(label("Orders", "Title"))
        top.addStretch(1)
        self.amount = label("", "PanelTitle")
        top.addWidget(label("Approved orders", "Muted"))
        top.addWidget(self.amount)
        top.addSpacing(12)
        top.addWidget(button("Clear filters", self.clear_filters))
        top.addWidget(button("Export to Excel", self.export))
        self.columns_btn = button("Columns", lambda: self.chooser.open_under(self.columns_btn))
        top.addWidget(self.columns_btn)
        self.new_btn = button("New order form", self.new_of.emit)
        self.new_btn.setToolTip("Ctrl+Shift+N")
        show_if(self.new_btn, user.can("of.edit"))
        top.addWidget(self.new_btn)
        root.addLayout(top)

        self.tabs = register_tabs(self.TABS, self._set_tab)
        tab_row = QHBoxLayout()
        tab_row.addWidget(self.tabs)
        tab_row.addStretch(1)
        # Older completed orders are the history, in Inquiry. One press opens it on them.
        tab_row.addWidget(button(f"Older than {self.RECENT_DAYS} days: Inquiry", self.go_completed.emit, lock=False))
        root.addLayout(tab_row)

        strip = QHBoxLayout()
        strip.setSpacing(2)
        self.group = QButtonGroup(self)
        self.count_buttons = {}
        for code, text in [(None, "All open")] + [(s, orders.STATUS_LABELS[s]) for s in (orders.DRAFT, orders.PENDING, orders.APPROVED)]:
            b = QPushButton(text)
            b.setObjectName("Count")
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, c=code: self._set_status(c))
            self.group.addButton(b)
            strip.addWidget(b)
            self.count_buttons[code] = b
        self.count_buttons[None].setChecked(True)
        strip.addStretch(1)
        root.addLayout(strip)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.search = QLineEdit()
        # Short enough to read whole on a 1366 laptop. The tooltip has the rest.
        self.search.setPlaceholderText("OF, PO, DR, supplier or code")
        self.search.setToolTip("Search by OF, PO or DR number, supplier name or supplier code")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(280)
        self.branch, self.department, self.year = QComboBox(), QComboBox(), QComboBox()
        # A long branch or department name must not widen the whole window.
        for c in (self.branch, self.department):
            c.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            c.setMinimumContentsLength(11)
        self.date_from, self.date_to = date_edit(optional=True), date_edit(optional=True)
        self.search.setMinimumWidth(200)
        bar.addWidget(self.search, 2)
        for text, w in (("Branch", self.branch), ("Department", self.department), ("Year", self.year), ("From", self.date_from), ("To", self.date_to)):
            bar.addWidget(label(text, "Muted"))
            bar.addWidget(w)
        self.month = month_picker(self.date_from, self.date_to)
        bar.addWidget(self.month)
        bar.addStretch(1)
        root.addLayout(bar)

        action = QHBoxLayout()
        self.selected_note = label("", "PanelTitle")
        action.addWidget(self.selected_note)
        action.addStretch(1)
        # A draft, or an OF still waiting for approval, can be cancelled from the list. It moves to Cancelled with its reason, and nothing is deleted.
        self.cancel_btn = button("Cancel OF", self._cancel_selected, "Danger")
        self.next_btn = button("", self.do_next_step, "Primary")
        action.addWidget(self.cancel_btn)
        # Room between the red button and the everyday one, so a slip of the mouse doesn't land on Cancel.
        action.addSpacing(DANGER_GAP)
        action.addWidget(self.next_btn)
        root.addLayout(action)

        self.branch_list = masters.list_branches(conn, branches_only=True)
        self.tree = make_tree(self.HEADERS + [header for header, _ in EXTRA_COLUMNS.values()] + delivery_headers(self.branch_list), numeric=(self.C_QTY, self.C_TOTAL), stretch=self.C_WHO)
        self.chooser = choose_columns(self.tree, f"orders-{user.username}", {"date": self.C_DATE, "who": self.C_WHO, "pieces": self.C_QTY, "total": self.C_TOTAL, "status": self.C_STATUS,
                                                                            "printed": self.C_PRINTED, "next": self.C_NEXT}, len(self.HEADERS), self.branch_list)
        self.chooser.columns_changed.connect(self._fit_columns)
        self.tree.setItemDelegateForColumn(self.C_STATUS, StatusDelegate(self.tree))
        # Only the arrow opens a row. A click on the row just selects it.
        # Double-click or Enter only shows the order in the panel beside the list. The panel's button opens the whole OF or PO.
        self.tree.itemActivated.connect(lambda item, *_: self._peek(item))
        self.tree.itemExpanded.connect(lambda item: self.expanded.add(item.data(0, Qt.ItemDataRole.UserRole)[1]))
        self.tree.itemCollapsed.connect(lambda item: self.expanded.discard(item.data(0, Qt.ItemDataRole.UserRole)[1]))
        # Opening a row can bring in longer text, so the columns fit again. Queued, because the row's height and text settle first.
        self.tree.itemExpanded.connect(lambda *_: QTimer.singleShot(0, self._fit_columns))
        self.tree.itemCollapsed.connect(lambda *_: QTimer.singleShot(0, self._fit_columns))
        self.tree.currentItemChanged.connect(lambda *_: self._show_action())
        # Selecting a row shows it on the right, so nobody has to open it and come back.
        self.panel = DocumentPanel(conn)
        self.panel.open_doc.connect(lambda kind, doc_id: (self.open_of if kind == "OF" else self.open_po).emit(doc_id))
        self.split = QSplitter(Qt.Orientation.Horizontal)
        self.split.addWidget(self.tree)
        self.split.addWidget(self.panel)
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 2)
        self.split.setChildrenCollapsible(False)
        self.split.splitterMoved.connect(lambda *_: self._fit_columns())
        # The list keeps the whole width until someone double-clicks an order. Then the panel opens on the right and follows the selection until it's closed.
        self.panel.hide()
        self.panel.closed.connect(self._close_panel)
        # Ctrl+O opens the selected OF or PO in full, the same as the panel's button.
        self.open_shortcut = QShortcut(QKeySequence("Ctrl+O"), self, activated=lambda: guarded(self, self._open_selected))
        self.close_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=lambda: self.panel.isVisibleTo(self) and self._close_panel())
        root.addWidget(self.split, 1)
        self.empty = label("", "Empty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.hide()
        # The empty message takes the room the list would, so the search box and tabs stay where they were.
        root.addWidget(self.empty, 1)
        # A long list shows a page at a time, newest first, so it stays fast. Search and filters still look at every order.
        self.pager = Pager(self._show_more)
        root.addWidget(self.pager)

        # Typing waits for a pause before the list is worked out again, not every key. Text put in by the program, or a cleared box, shows at once.
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(300)
        self.search_timer.timeout.connect(self._filter_changed)
        self.search.textChanged.connect(lambda text: search_changed(self.search, self.search_timer, self._filter_changed, text))
        for c in (self.branch, self.department, self.year):
            c.currentIndexChanged.connect(self._filter_changed)
        for d in (self.date_from, self.date_to):
            d.dateChanged.connect(self._filter_changed)
        self.reload_choices()
        self.refresh()

    def _filter_changed(self, *_):
        """A new search, filter or date starts again from the newest 200."""
        self.pager.reset()
        self.refresh()

    def _show_more(self):
        with keep_place(self.tree):
            self.refresh()

    def reload_choices(self):
        combos = (self.branch, self.department, self.year)
        current = [c.currentData() for c in combos]
        for c in combos:
            c.blockSignals(True)
            c.clear()
            c.addItem("All", None)
        for b in masters.list_branches(self.conn):
            self.branch.addItem(f"{b['code']} {b['name']}" + ("" if b["active"] else " (inactive)"), b["code"])
        for d in masters.list_departments(self.conn):
            self.department.addItem(f"{d['id']} {d['name']}", d["id"])
        years = {str(today().year)} | {r[0] for r in self.conn.execute("SELECT substr(of_date,1,4) FROM order_forms UNION SELECT substr(po_date,1,4) FROM purchase_orders")}
        for y in sorted(years, reverse=True):
            self.year.addItem(y, y)
        for c, value in zip(combos, current):
            select_data(c, value)
            c.blockSignals(False)

    def _filters(self) -> dict:
        return dict(text=self.search.text(), branch_code=self.branch.currentData(), department_id=self.department.currentData(), year=self.year.currentData(),
                    date_from=date_value(self.date_from), date_to=date_value(self.date_to))

    def clear_filters(self):
        """Back to every order on this tab: no search, every branch, department and year, no dates, and All open."""
        boxes = (self.search, self.branch, self.department, self.year, self.date_from, self.date_to)
        for w in boxes:
            w.blockSignals(True)
        self.search.clear()
        for c in (self.branch, self.department, self.year):
            c.setCurrentIndex(0)
        set_date(self.date_from, None)
        set_date(self.date_to, None)
        for w in boxes:
            w.blockSignals(False)
        self.status_filter = None
        self.count_buttons[None].setChecked(True)
        self.pager.reset()
        self.refresh()

    def _empty_text(self) -> str:
        text = self.search.text().strip()
        if text:
            return f"No match for '{text}'. Clear the search to see more. Older completed orders are in Inquiry."
        # Only someone who can start an order form is pointed at its button.
        return "No orders here. Older completed orders are in Inquiry." + (" Or press New order form to start one." if self.user.can("of.edit") else "")

    def _set_tab(self, tab):
        self.tab = tab
        self.status_filter = None
        self.count_buttons[None].setChecked(True)
        # Only the Open tab holds more than one kind of OF to narrow down.
        for b in self.count_buttons.values():
            b.setVisible(tab == orders.OPEN)
        self.pager.reset()
        self.refresh()

    def _set_status(self, code):
        self.status_filter = code
        self.pager.reset()
        self.refresh()

    def _legacy_tab(self, status: str) -> str:
        return next(tab for tab, statuses in orders.PO_TAB_STATUSES.items() if status in statuses)

    def refresh(self, *_):
        self.search_timer.stop()
        filters = self._filters()
        # One search of the OFs gives the tab counts, the amount and the list, so a branch filter runs once for each refresh, not three times.
        found = of_service.search(self.conn, **filters, limit=1000000)
        by_tab = {t: 0 for t in orders.PO_TAB_STATUSES}
        open_by_status = {s: 0 for s in of_service.OF_STATUSES}
        approved_c = 0
        for r in found:
            by_tab[r["tab"]] += 1
            if r["tab"] == orders.OPEN:
                open_by_status[r["status"]] += 1
            if r["status"] == orders.APPROVED:
                approved_c += r["grand_total_c"]
        # POs from before Order Forms have no OF to sit under, so they are orders in their own right.
        legacy = [r for r in orders.search(self.conn, **filters, limit=1000000) if r["of_id"] is None]
        for r in legacy:
            by_tab[self._legacy_tab(r["status"])] += 1
        self.amount.setText(fmt_peso(approved_c))
        # Recently completed: every branch PO printed within the last few weeks.
        cutoff = (today() - timedelta(days=self.RECENT_DAYS)).isoformat()
        recent = [r for r in found if r["tab"] == orders.COMPLETED and (r["printed_at"] or "")[:10] >= cutoff]
        recent_legacy = [r for r in legacy if self._legacy_tab(r["status"]) == orders.COMPLETED and (r["issued_at"] or "")[:10] >= cutoff]
        show_tab_counts(self.tabs, self.TABS, {orders.OPEN: by_tab[orders.OPEN], orders.COMPLETED: len(recent) + len(recent_legacy), orders.CANCELLED: by_tab[orders.CANCELLED]})
        self.count_buttons[None].setText(f"All open  {by_tab[orders.OPEN]}")
        for code, b in self.count_buttons.items():
            if code:
                b.setText(f"{orders.STATUS_LABELS[code]}  {open_by_status[code]}")
        if self.tab == orders.COMPLETED:
            self.rows, self.legacy_rows = recent, recent_legacy
        else:
            self.rows = [r for r in found if r["tab"] == self.tab and (not self.status_filter or r["status"] == self.status_filter)]
            self.legacy_rows = [r for r in legacy if self._legacy_tab(r["status"]) == self.tab and (not self.status_filter or r["status"] == self.status_filter)]
        # Every filter has run on every order. Only the rows taken are built, newest first, OFs and older POs together.
        self.shown = self.pager.take(newest_first(self.rows, self.legacy_rows))
        self.po_rows = {}
        for po in orders.search(self.conn, branch_code=self.branch.currentData(), limit=1000000):
            if po["of_id"]:
                self.po_rows.setdefault(po["of_id"], []).append(po)
        self._fill_tree()
        self._fit_columns()

    # While the panel is open the list is narrower, so it keeps only what picks the order out. Closing the panel brings the rest back.
    NARROW_HIDES = ("C_DATE", "C_TOTAL", "C_PRINTED")

    def _peek(self, item):
        """Double-click or Enter: the order's details on the right, without leaving the list."""
        self.panel.show_doc(item.data(0, Qt.ItemDataRole.UserRole))
        if not self.panel.isVisibleTo(self):
            self.hidden_for_panel = [c for c in [getattr(self, n) for n in self.NARROW_HIDES] + list(range(len(self.HEADERS), self.tree.columnCount())) if not self.tree.isColumnHidden(c)]
            for c in self.hidden_for_panel:
                self.tree.setColumnHidden(c, True)
            self.panel.show()
            total = sum(self.split.sizes()) or self.width()
            self.split.setSizes([int(total * 0.55), int(total * 0.45)])
        QTimer.singleShot(0, self._fit_columns)

    def _close_panel(self):
        self.panel.hide()
        for c in getattr(self, "hidden_for_panel", []):
            self.tree.setColumnHidden(c, False)
        self.hidden_for_panel = []
        QTimer.singleShot(0, self._fit_columns)

    def _fit_columns(self):
        extras = range(len(self.HEADERS), self.tree.columnCount())
        # With extras on, the list can run past the window, where a stretching column would be squeezed. So the last column takes any spare room instead.
        stretch = self.C_WHO if not self.tree.isColumnHidden(self.C_WHO) and all(self.tree.isColumnHidden(c) for c in extras) else None
        self.tree.header().setSectionResizeMode(self.C_WHO, QHeaderView.ResizeMode.Interactive if stretch is None else QHeaderView.ResizeMode.Stretch)
        self.tree.header().setStretchLastSection(stretch is None)
        fit_tree_columns(self.tree, stretch=stretch, shrink=self.C_NEXT, keep=extras)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_columns()

    def showEvent(self, event):
        super().showEvent(event)
        self._fit_columns()

    def _add_row(self, parent, key, values: list, status: str | None, row, who_tip: str):
        extra = self.reprints.get(key, 0)
        if extra:
            values[self.C_PRINTED] = f"{values[self.C_PRINTED]} ({extra + 1}x)"
        item = QTreeWidgetItem(parent, [("" if v is None else str(v)) for v in values + extra_column_values(key, row, self.people, self.department_names)])
        add_delivery(item, len(self.HEADERS) + len(EXTRA_COLUMNS), key, self.delivered, self.branch_list)
        item.setData(0, Qt.ItemDataRole.UserRole, key)
        item.setToolTip(self.C_WHO, who_tip)
        for col in (self.C_QTY, self.C_TOTAL):
            item.setTextAlignment(col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if status:
            item.setText(self.C_STATUS, orders.STATUS_LABELS.get(status, status))
            item.setData(self.C_STATUS, STATUS_ROLE, status)
        return item

    def _came(self, pos: list[int]) -> str:
        """How much of these POs two receivers agreed has arrived, as "Delivered 100 of 120", or nothing before anything came."""
        got = [self.delivered[p] for p in pos if p in self.delivered]
        received, ordered = sum(_shown(d) for d in got), sum(d["ordered_m"] for d in got)
        return f"Delivered {fmt_qty(received)} of {fmt_qty(ordered)}" if received else ""

    def _po_values(self, po, who: str) -> list:
        step = orders.next_step(po)
        if po["status"] == orders.PARTIAL:
            step = self._came([po["id"]]) or step
        return [po["po_number"] or f"Draft #{po['id']}", fmt_date(po["po_date"]), who, fmt_qty(po["total_pieces_m"]), fmt_plain(po["grand_total_c"]), "",
                fmt_date(po["issued_at"]), step]

    def _fill_tree(self):
        self.reprints = pdf.reprint_counts(self.conn)
        self.people = auth.chips(self.conn)
        self.delivered = delivered_with_detail(self.conn)
        self.department_names = {d["id"]: d["name"] for d in masters.list_departments(self.conn)}
        text = self.search.text().strip().lower()
        wanted = None
        self.tree.blockSignals(True)
        self.tree.clear()
        shown_ofs = [r for kind, r in self.shown if kind == "OF"]
        for r in shown_ofs:
            step = r["next_step"] + (f", over {of_service.DRAFT_WARN_DAYS} days old" if r["old_draft"] else "")
            if r["next_step"] == "Partially delivered":
                step = self._came([p["id"] for p in self.po_rows.get(r["id"], []) if p["status"] != orders.CANCELLED]) or step
            top = self._add_row(self.tree, ("OF", r["id"]), [r["of_number"] or f"Draft OF #{r['id']}", fmt_date(r["of_date"]), r["supplier_code"], fmt_qty(r["total_pieces_m"]),
                                                            fmt_plain(r["grand_total_c"]), "", fmt_date(r["printed_at"]) or r["printed_part"], step], r["status"], r, r["supplier"])
            if r["old_draft"]:
                top.setForeground(self.C_NEXT, QColor(theme.current.AMBER))
            for po in sorted(self.po_rows.get(r["id"], []), key=lambda p: p["branch_code"]):
                who = f"{po['branch_code']} {po['branch']}"
                child = self._add_row(top, ("PO", po["id"]), self._po_values(po, who), po["status"], po, who)
                # A search for a PO or DR number opens its order and lands on that PO.
                if text and (text in (po["po_number"] or "").lower() or text in (po["dr_numbers"] or "").lower()):
                    self.expanded.add(r["id"])
                    wanted = wanted or child
            top.setExpanded(r["id"] in self.expanded)
        for po in (r for kind, r in self.shown if kind == "PO"):
            self._add_row(self.tree, ("PO", po["id"]), self._po_values(po, f"{po['supplier_code']}, {po['branch']}"), po["status"], po, f"{po['supplier']}, {po['branch']}")
        resort(self.tree)
        # A branch column with nothing in it, such as a closed branch, takes no room.
        if not self.panel.isVisibleTo(self):
            hide_unused(self.tree, self.chooser, list(range(len(self.HEADERS) + len(EXTRA_COLUMNS) + 1, self.tree.columnCount())))
        first = self.tree.topLevelItem(0)
        self.tree.setCurrentItem(wanted or self.item_for(self.current_key) or first)
        self.tree.blockSignals(False)
        self.split.setVisible(first is not None)
        self.tree.setVisible(first is not None)
        self.empty.setText(self._empty_text())
        self.empty.setVisible(first is None)
        self._show_action()

    def item_for(self, key):
        """The row showing this ('OF' or 'PO', id), or None."""
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for item in (top, *(top.child(c) for c in range(top.childCount()))):
                if tuple(item.data(0, Qt.ItemDataRole.UserRole)) == key:
                    return item
        return None

    def _key(self):
        item = self.tree.currentItem()
        return tuple(item.data(0, Qt.ItemDataRole.UserRole)) if item else None

    def _clicked(self, item, _column):
        if item.childCount():
            item.setExpanded(not item.isExpanded())

    def _next_action(self):
        """What the button does for the selected row: its label, what it runs, and whether this person may. None when there's nothing to do."""
        key = self._key()
        if key is None:
            return None
        may_issue = self.user.can("po.issue")
        if key[0] == "PO":
            po = orders.get_po(self.conn, key[1])
            if po["status"] == orders.APPROVED:
                # A PO from before OFs has no OF to print with, so it prints from its own screen.
                return ("Go to Print", lambda: self.go_print.emit(po["of_id"]), may_issue) if po["of_id"] else ("Open and print", self._open_selected, may_issue)
            return None
        row = next((r for r in self.rows if r["id"] == key[1]), None)
        on_list = self.user.id in orders.policy(self.conn)["approver_user_ids"]
        step = row["next_step"] if row else ""
        if step == of_service.NEXT_SUBMIT:
            return "Open and finish", self._open_selected, self.user.can("of.edit")
        if step == of_service.NEXT_APPROVE:
            # The same check the approval makes, so someone who prepared it (with self-approval off) isn't sent to an OF with no Approve.
            if on_list and self.user.can("of.approve") and may_approve(self.conn, self.user, key[1], row["prepared_by"]):
                return "Review and approve", self._open_selected, True
            return "Waiting for approval", self._open_selected, False
        if step == of_service.NEXT_AMENDMENT:
            return ("Decide the amendment", self.go_approvals.emit, True) if on_list else ("Waiting for approval", self.go_approvals.emit, False)
        if step == of_service.NEXT_PRINT:
            return "Go to Print", lambda: self.go_print.emit(key[1]), may_issue
        return None

    def _show_action(self):
        key = self._key()
        if key:
            self.current_key = key
        item = self.tree.currentItem()
        self.selected_note.setText(f"{item.text(self.C_NUMBER)}, {item.toolTip(self.C_WHO)}" if item else "")
        if self.panel.isVisibleTo(self):
            self.panel.show_doc(key if item is not None else None)
        show_if(self.cancel_btn, self._cancellable() is not None)
        action = self._next_action()
        show_if(self.next_btn, action is not None and action[2])
        if action:
            self.next_btn.setText(action[0])

    def _cancellable(self) -> dict | None:
        """The selected OF, if it's a draft or waiting for approval and this person may cancel it."""
        key = self._key()
        if not key or key[0] != "OF" or not self.user.can("of.cancel"):
            return None
        row = next((r for r in self.rows if r["id"] == key[1]), None)
        return row if row and row["status"] in (orders.DRAFT, orders.PENDING) else None

    def _cancel_selected(self):
        row = self._cancellable()
        if not row:
            return
        name = row["of_number"] or f"Draft OF #{row['id']}"
        reason = ask_text(self, "Cancel OF", f"Why is {name} being cancelled? It moves to the Cancelled tab with your reason, and nothing is deleted.")
        if reason is None:
            return
        of_service.cancel(self.conn, self.user, row["id"], reason)
        self.refresh()
        self.changed.emit()

    def do_next_step(self):
        action = self._next_action()
        if action and action[2]:
            action[1]()

    def _open_selected(self):
        key = self._key()
        if key:
            (self.open_of if key[0] == "OF" else self.open_po).emit(key[1])

    def export(self):
        path = save_file(self, "Export order forms", str(Path.home() / "Documents" / f"Order Forms {today().isoformat()}.xlsx"), "Excel (*.xlsx)")
        if path:
            reports.of_register_excel(self.rows, path)
            info(self, "Exported", f"Saved {len(self.rows)} order forms to {path}. For a list of POs, use the PO Register report.")


class InquiryPage(QWidget):
    """Looks up numbered OFs and POs, newest first, including by the product on them. Nothing on this page changes anything.
    It shows nothing until asked, so a long history doesn't land on the screen all at once."""
    open_of = Signal(int)
    open_po = Signal(int)
    ONE, ALL, DATES = "ONE", "ALL", "DATES"
    MODES = {ONE: "Find an OF", ALL: "All OFs", DATES: "OFs by date"}
    C_NUMBER, C_DATE, C_WHO, C_QTY, C_TOTAL, C_STATUS, C_PRINTED = range(7)
    HEADERS = ["Order / PO", "Date", "Supplier / branch", "Pieces", "Total", "Status", "Printed"]
    # Where an order stands, end to end. An OF's own status is Approved from printing on, which says nothing about delivery or payment, so the list shows this.
    TO_PRINT, AWAITING, DELIVERED, PAID, CLEARED = "TO_PRINT", "AWAITING", "DELIVERED", "PAID", "CLEARED"
    STAGES = {TO_PRINT: "To print", AWAITING: "Awaiting delivery", DELIVERED: "Delivered, to pay", PAID: "Paid", CLEARED: "Cleared", orders.CANCELLED: "Cancelled"}
    # Each borrows a document status's colour, so the chips read the same as everywhere else.
    STAGE_COLOURS = {TO_PRINT: orders.APPROVED, AWAITING: orders.ISSUED, DELIVERED: orders.PARTIAL, PAID: orders.FULL, CLEARED: orders.CLOSED, orders.CANCELLED: orders.CANCELLED}

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self.conn, self.user = conn, user
        self.rows, self.legacy_rows = [], []
        self.people, self.department_names = {}, {}
        self.expanded: set[int] = set()
        self.mode = self.ONE
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 14, 20, 14)
        root.setSpacing(10)
        top = QHBoxLayout()
        top.addWidget(label("Inquiry", "Title"))
        top.addStretch(1)
        self.count = label("", "Muted")
        top.addWidget(self.count)
        top.addSpacing(12)
        top.addWidget(button("Clear filters", self.clear_filters))
        self.columns_btn = button("Columns", lambda: self.chooser.open_under(self.columns_btn))
        top.addWidget(self.columns_btn)
        self.open_btn = button("Open", self._open_selected)
        top.addWidget(self.open_btn)
        root.addLayout(top)
        self.tabs = register_tabs(self.MODES, self._set_mode)
        root.addWidget(self.tabs)

        first = QHBoxLayout()
        first.setSpacing(8)
        self.search, self.product = QLineEdit(), QLineEdit()
        self.search.setPlaceholderText("OF, PO or DR number, supplier or code")
        self.product.setPlaceholderText("SKU or product")
        for w in (self.search, self.product):
            w.setClearButtonEnabled(True)
            first.addWidget(w, 1)
        root.addLayout(first)
        self.more = QWidget()
        # The filters sit on two short rows, so All OFs and OFs by date fit a 1280 px screen at 150% scaling without making the window wider.
        rows = QVBoxLayout(self.more)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(6)
        second, third = QHBoxLayout(), QHBoxLayout()
        for line in (second, third):
            line.setSpacing(8)
            rows.addLayout(line)
        self.supplier, self.department, self.branch, self.status = search_combo(), QComboBox(), QComboBox(), QComboBox()
        self.status.addItem("All", None)
        for stage, text in self.STAGES.items():
            self.status.addItem(text, stage)
        self.date_from, self.date_to = date_edit(optional=True), date_edit(optional=True)
        for line, text, w in ((second, "Supplier", self.supplier), (second, "Department", self.department), (second, "Branch", self.branch),
                              (third, "Status", self.status), (third, "From", self.date_from), (third, "To", self.date_to)):
            line.addWidget(label(text, "Muted"))
            line.addWidget(w)
        self.month = month_picker(self.date_from, self.date_to)
        third.addWidget(self.month)
        second.addStretch(1)
        third.addStretch(1)
        root.addWidget(self.more)
        self.more.hide()
        for c in (self.supplier, self.department, self.branch):
            c.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            c.setMinimumContentsLength(11)

        self.split = split = QSplitter(Qt.Orientation.Vertical)
        self.branch_list = masters.list_branches(conn, branches_only=True)
        self.tree = make_tree(self.HEADERS + [header for header, _ in EXTRA_COLUMNS.values()] + delivery_headers(self.branch_list), numeric=(self.C_QTY, self.C_TOTAL))
        # Whichever column is showing last takes the spare room, whatever someone has hidden.
        self.tree.header().setStretchLastSection(True)
        for col, width in {self.C_NUMBER: 170, self.C_DATE: 96, self.C_WHO: 140, self.C_QTY: 70, self.C_TOTAL: 110, self.C_STATUS: 140, self.C_PRINTED: 130}.items():
            self.tree.setColumnWidth(col, width)
        self.chooser = choose_columns(self.tree, f"inquiry-{user.username}", {"date": self.C_DATE, "who": self.C_WHO, "pieces": self.C_QTY, "total": self.C_TOTAL, "status": self.C_STATUS,
                                                                             "printed": self.C_PRINTED}, len(self.HEADERS), self.branch_list, delivery_on=True)
        # Delivered sits beside Pieces, so what arrived reads against what was ordered without scrolling.
        first_delivery = len(self.HEADERS) + len(EXTRA_COLUMNS)
        for offset in range(len(self.branch_list) + 1):
            self.tree.header().moveSection(self.tree.header().visualIndex(first_delivery + offset), self.C_QTY + 1 + offset)
        # Where each order stands reads first, beside the supplier, before the pieces and branches.
        self.tree.header().moveSection(self.tree.header().visualIndex(self.C_STATUS), self.C_WHO + 1)
        self.tree.setColumnWidth(self.C_STATUS, 210)
        self.tree.setItemDelegateForColumn(self.C_STATUS, StatusDelegate(self.tree))
        # Only the arrow opens a row. A click on the row just selects it.
        self.tree.itemActivated.connect(lambda *_: self._open_selected())
        self.tree.itemExpanded.connect(lambda item: self.expanded.add(item.data(0, Qt.ItemDataRole.UserRole)[1]))
        self.tree.itemCollapsed.connect(lambda item: self.expanded.discard(item.data(0, Qt.ItemDataRole.UserRole)[1]))
        self.tree.currentItemChanged.connect(lambda *_: self._show_detail())
        split.addWidget(self.tree)
        detail = QWidget()
        col = QVBoxLayout(detail)
        col.setContentsMargins(0, 6, 0, 0)
        self.summary = label("", "PanelTitle")
        self.summary.setWordWrap(True)
        col.addWidget(self.summary)
        self.items = make_table(["Department", "SKU", "Product", "Size", "Color", "UOM", "Pieces", "Cost per UOM", "Disc.", "Cost per pc", "Amount"], numeric=(6, 7, 8, 9, 10), stretch=2)
        set_widths(self.items, {0: 130, 1: 120, 3: 60, 4: 80, 5: 76, 6: 62, 7: 84, 8: 70, 9: 72, 10: 100})
        # A right-aligned heading that doesn't fit loses its first letters ("ost per UOM"), so each number column is at least as wide as its heading.
        fit_headings(self.items, (6, 7, 8, 9, 10))
        col.addWidget(self.items, 1)
        split.addWidget(detail)
        split.setSizes([420, 260])
        root.addWidget(split, 1)
        self.empty = label("", "Empty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.hide()
        # The message takes the room the list would, so the boxes above stay where they were.
        root.addWidget(self.empty, 1)
        # A page at a time, newest first. Search and filters still look through every order.
        self.pager = Pager(self._show_more)
        root.addWidget(self.pager)
        # Ctrl+O opens the selected OF or PO in full, as on Orders, Approvals and Print.
        self.open_shortcut = QShortcut(QKeySequence("Ctrl+O"), self, activated=lambda: guarded(self, self._open_selected))

        # Typing waits for a pause before looking up again, not every key. Text put in by the program, or a cleared box, shows at once.
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(300)
        self.search_timer.timeout.connect(self._filter_changed)
        for w in (self.search, self.product):
            w.textChanged.connect(lambda text, w=w: search_changed(w, self.search_timer, self._filter_changed, text))
        for c in (self.supplier, self.department, self.branch, self.status):
            c.currentIndexChanged.connect(self._filter_changed)
        for d in (self.date_from, self.date_to):
            d.dateChanged.connect(self._filter_changed)
        self.reload_choices()
        self.refresh()

    def _filter_changed(self, *_):
        """A new search, filter or date starts again from the newest 200."""
        self.pager.reset()
        self.refresh()

    def _show_more(self):
        with keep_place(self.tree):
            self.refresh()

    def reload_choices(self):
        combos = (self.supplier, self.department, self.branch)
        current = [combo_value(self.supplier), self.department.currentData(), self.branch.currentData()]
        for c in combos:
            c.blockSignals(True)
            c.clear()
            c.addItem("All", None)
        for s in masters.search_suppliers(self.conn):
            self.supplier.addItem(f"{s['code']}  {s['name']}", s["id"])
        for d in masters.list_departments(self.conn):
            self.department.addItem(f"{d['id']} {d['name']}", d["id"])
        for b in masters.list_branches(self.conn):
            self.branch.addItem(f"{b['code']} {b['name']}" + ("" if b["active"] else " (inactive)"), b["code"])
        for c, value in zip(combos, current):
            select_data(c, value)
            c.blockSignals(False)

    def show_completed(self):
        """Every order, newest first, each saying where it stands: delivered, paid or cleared. Status narrows it down."""
        self.tabs.setCurrentIndex(list(self.MODES).index(self.ALL))
        self._set_mode(self.ALL)
        self.refresh()

    def _set_mode(self, mode):
        """Each choice starts clean, so a filter that's now hidden can't quietly narrow the list."""
        self.mode = mode
        self.more.setVisible(mode != self.ONE)
        self.clear_filters()

    def _waiting_message(self) -> str:
        """What to do first, or nothing once there's something to look up."""
        if self.mode == self.ONE and not (self.search.text().strip() or self.product.text().strip()):
            return "Type an OF, PO or DR number or supplier, or a SKU or product. To see more, choose All OFs or OFs by date."
        if self.mode == self.DATES and not (date_value(self.date_from) or date_value(self.date_to)):
            return "Choose a From or To date."
        return ""

    def clear_filters(self):
        for w in (self.search, self.product, self.supplier, self.department, self.branch, self.status, self.date_from, self.date_to):
            w.blockSignals(True)
        self.search.clear()
        self.product.clear()
        for c in (self.supplier, self.department, self.branch, self.status):
            c.setCurrentIndex(0)
        set_date(self.date_from, None)
        set_date(self.date_to, None)
        for w in (self.search, self.product, self.supplier, self.department, self.branch, self.status, self.date_from, self.date_to):
            w.blockSignals(False)
        self.pager.reset()
        self.refresh()

    def refresh(self, *_):
        self.search_timer.stop()
        self.reprints = pdf.reprint_counts(self.conn)
        waiting = self._waiting_message()
        if waiting:
            self.rows, self.legacy_rows = [], []
            self.pager.take([])
            self.tree.blockSignals(True)
            self.tree.clear()
            self.tree.blockSignals(False)
            self.count.clear()
            self.empty.setText(waiting)
            self.split.hide()
            self.tree.hide()
            self.empty.show()
            show_if(self.open_btn, False)
            self._show_detail()
            return
        self.split.show()
        typed = " ".join(t for t in (self.search.text().strip(), self.product.text().strip()) if t)
        self.empty.setText((f"No match for '{typed}'. " if typed else "Nothing matches. ") + "Try fewer filters. Drafts and OFs still waiting for approval are under Orders.")
        filters = dict(text=self.search.text(), product=self.product.text().strip(), supplier_id=combo_value(self.supplier), department_id=self.department.currentData(),
                       branch_code=self.branch.currentData(), date_from=date_value(self.date_from), date_to=date_value(self.date_to))
        stage = self.status.currentData()
        found = [r for r in of_service.search(self.conn, **filters, limit=1000000) if r["of_number"]]
        checks = {r["of_id"]: dict(r) for r in self.conn.execute(
            "SELECT o.of_id, c.check_number, c.source, c.amount_c, k.claimed_on, b.cleared_on FROM payment_check_ofs o JOIN payment_checks c ON c.id = o.check_id"
            " LEFT JOIN check_claims k ON k.check_id = c.id LEFT JOIN check_clears b ON b.check_id = c.id WHERE c.status = 'ACTIVE'")}
        # Where each order stands takes a look at its deliveries, so it is worked out for every order only when Status narrows the list, otherwise only for the rows shown.
        if stage:
            for r in found:
                r["stage"], r["stage_text"] = self._stage(r, checks.get(r["id"]))
            found = [r for r in found if r["stage"] == stage]
        # POs from before Order Forms have no OF, so they are results of their own. They show with no stage chosen, or as cancelled.
        legacy = [r for r in orders.search(self.conn, **filters, tab=orders.CANCELLED if stage == orders.CANCELLED else None, limit=1000000)
                  if r["of_id"] is None and r["po_number"]] if stage in (None, orders.CANCELLED) else []
        # Every filter has run on every order. Only the rows taken are built, newest first, OFs and older POs together.
        shown = self.pager.take(newest_first(found, legacy))
        self.count.setText(f"{len(found) + len(legacy):,} found")
        self.rows = [r for kind, r in shown if kind == "OF"]
        self.legacy_rows = [r for kind, r in shown if kind == "PO"]
        for r in self.rows:
            if "stage" not in r:
                r["stage"], r["stage_text"] = self._stage(r, checks.get(r["id"]))
        branch = self.branch.currentData()
        pos: dict[int, list] = {}
        for po in orders.search(self.conn, branch_code=branch, limit=1000000):
            if po["of_id"]:
                pos.setdefault(po["of_id"], []).append(po)
        self.people = auth.chips(self.conn)
        self.delivered = delivered_with_detail(self.conn)
        self.department_names = {d["id"]: d["name"] for d in masters.list_departments(self.conn)}
        self.tree.blockSignals(True)
        self.tree.clear()
        for r in self.rows:
            top = self._add(self.tree, ("OF", r["id"]), r["of_number"], r["of_date"], r["supplier_code"], r["total_pieces_m"], r["grand_total_c"], r["status"], fmt_date(r["printed_at"]) or r["printed_part"], r, r["supplier"])
            top.setText(self.C_STATUS, r["stage_text"])
            top.setData(self.C_STATUS, STATUS_ROLE, self.STAGE_COLOURS[r["stage"]])
            for po in sorted(pos.get(r["id"], []), key=lambda p: p["branch_code"]):
                who = f"{po['branch_code']} {po['branch']}"
                self._add(top, ("PO", po["id"]), po["po_number"], po["po_date"], who, po["total_pieces_m"], po["grand_total_c"], po["status"], fmt_date(po["issued_at"]), po, who)
            top.setExpanded(r["id"] in self.expanded)
        for po in self.legacy_rows:
            self._add(self.tree, ("PO", po["id"]), po["po_number"], po["po_date"], f"{po['supplier_code']}, {po['branch']}", po["total_pieces_m"], po["grand_total_c"], po["status"], fmt_date(po["issued_at"]), po, f"{po['supplier']}, {po['branch']}")
        resort(self.tree)
        hide_unused(self.tree, self.chooser, list(range(len(self.HEADERS) + len(EXTRA_COLUMNS) + 1, self.tree.columnCount())))
        first = self.tree.topLevelItem(0)
        self.tree.setCurrentItem(first)
        self.tree.blockSignals(False)
        self.split.setVisible(first is not None)
        self.tree.setVisible(first is not None)
        self.empty.setVisible(first is None)
        show_if(self.open_btn, first is not None)
        self._show_detail()

    def _stage(self, r: dict, check: dict | None) -> tuple[str, str]:
        """Where this OF stands: printed or not, delivered, paid, cleared. Returns the stage and the words for it, with the check or the date when there is one."""
        if r["tab"] == orders.CANCELLED:
            return orders.CANCELLED, self.STAGES[orders.CANCELLED]
        if check:
            if check["source"] == "PROGRAM" and check["amount_c"] == 0:
                return self.CLEARED, f"Cleared, settled by credit"
            if check["cleared_on"]:
                return self.CLEARED, f"Cleared {fmt_date(check['cleared_on'])}"
            return self.PAID, f"Paid, check {check['check_number']}" + (", claimed" if check["claimed_on"] else "")
        if r["tab"] == orders.OPEN:
            return self.TO_PRINT, self.STAGES[self.TO_PRINT]
        if receiving.of_delivery_complete(self.conn, r["id"]):
            return self.DELIVERED, self.STAGES[self.DELIVERED]
        # Spelled as the PO status is, so one column never shows the same thing two ways.
        return self.AWAITING, orders.STATUS_LABELS[orders.PARTIAL] if r["delivery"] == "Partially delivered" else self.STAGES[self.AWAITING]

    def _add(self, parent, key, number, day, who, pieces_m, total_c, status, printed, row, who_tip):
        extra = self.reprints.get(key, 0)
        if extra:
            printed = f"{printed} ({extra + 1}x)"
        item = QTreeWidgetItem(parent, [number, fmt_date(day), who, fmt_qty(pieces_m), fmt_plain(total_c), orders.STATUS_LABELS.get(status, status), printed or ""] + extra_column_values(key, row, self.people, self.department_names))
        add_delivery(item, len(self.HEADERS) + len(EXTRA_COLUMNS), key, self.delivered, self.branch_list)
        item.setData(0, Qt.ItemDataRole.UserRole, key)
        item.setToolTip(self.C_WHO, who_tip)
        item.setData(self.C_STATUS, STATUS_ROLE, status)
        for col in (self.C_QTY, self.C_TOTAL):
            item.setTextAlignment(col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return item

    def _key(self):
        item = self.tree.currentItem()
        return tuple(item.data(0, Qt.ItemDataRole.UserRole)) if item else None

    def _clicked(self, item, _column):
        if item.childCount():
            item.setExpanded(not item.isExpanded())

    def _show_detail(self):
        key = self._key()
        if key is None:
            self.summary.clear()
            self.items.setRowCount(0)
            return
        if key[0] == "OF":
            doc = of_service.get_of(self.conn, key[1])
            number, day, quantity = doc["of_number"], doc["of_date"], "total_qty_m"
        else:
            doc = orders.get_po(self.conn, key[1])
            number, day, quantity = doc["po_number"], doc["po_date"], "qty_m"
        parts = [number, doc["supplier"]["name"], fmt_date(day), f"Terms {doc['payment_terms'] or 'as agreed'}", f"Total {fmt_peso(doc['grand_total_c'])}"]
        if key[0] == "PO":
            parts.insert(2, doc["branch"]["name"])
        self.summary.setText("  |  ".join(parts))
        rows = []
        for i in doc["items"]:
            discount = " + ".join(x for x in (fmt_percent(i["disc1_bp"]), fmt_percent(i["disc2_bp"])) if x)
            rows.append([f"{i['department_id']} {self.department.itemText(self.department.findData(i['department_id'])).split(' ', 1)[-1]}", i["item_code"], i["description"], i["size"], i["color"], i["uom"],
                         fmt_qty(i[quantity] * i["pieces_per_unit"]), fmt_plain(i["unit_cost_c"]), discount, fmt_plain(per_piece(i["net_unit_c"], i["pieces_per_unit"])), fmt_plain(i["line_total_c"])])
        fill_table(self.items, rows)

    def _open_selected(self):
        key = self._key()
        if key:
            (self.open_of if key[0] == "OF" else self.open_po).emit(key[1])
