"""The PO screen. A PO comes from an Order Form, so this is read-only, unlocked for amendments, and where it is issued."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QGridLayout, QHBoxLayout,
                               QHeaderView, QLineEdit, QMenu, QStyledItemDelegate,
                               QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from .. import payments, masters, orders, pdf
from ..db import POError, data_dir
from ..money import (DISC_AMOUNT, DISC_NONE, DISC_PERCENT, OFFERED_UNITS, VAT_EXCLUSIVE, VAT_INCLUSIVE, VAT_NONE, compute_line, compute_totals,
                     fmt_milli, fmt_peso, fmt_percent, fmt_plain, fmt_qty, fx_to_centavos, m_to_qty, parse_milli, parse_percent, parse_pesos, parse_pieces,
                     pieces_per_unit)
from .common import (open_path, show_if, confirm_reprint, button, chip_style, confirm, ask_text, date_edit, date_value, escape_shortcut, fill_table, fmt_date, fmt_datetime, guarded, info, label, make_table, panel, save_file,
                     select_data, selected_key, set_date, set_terms, set_widths, terms_combo)
from . import common, theme


def write_print_file(stem: str, data: bytes) -> Path:
    """Writes a PO to open in the PDF viewer, in the print folder of the data folder. Several PCs may share that folder, so the name
    has the time to the millisecond and the computer, a file already there is never written over, and one open in a viewer on
    another PC (Windows refuses to write it) just means the next free name is used."""
    folder = data_dir() / "print"
    folder.mkdir(exist_ok=True)
    when = datetime.now()
    base = f"{stem} {when:%Y-%m-%d %H%M%S}-{when.microsecond // 1000:03d} {common.computer_name()}"
    for attempt in range(50):
        path = folder / (f"{base}.pdf" if not attempt else f"{base} ({attempt + 1}).pdf")
        try:
            with open(path, "xb") as out:
                out.write(data)
        except (FileExistsError, PermissionError):
            continue
        return path
    raise POError("The print file couldn't be saved in the print folder. Close any open print files and try again.")

# Column layout of the item grid
C_DEPT, C_CODE, C_DESC, C_SIZE, C_COLOR, C_QTY, C_UNIT, C_COST, C_D1, C_D2, C_NET, C_AMOUNT, \
    C_SUPCODE, C_BRAND, C_BOX, C_SELL, C_FXCUR, C_FXRATE, C_FXCOST, C_REMARKS = range(20)
HEADERS = ["Dept", "SKU", "Description", "Size", "Color", "Pieces", "UOM", "Cost per UOM", "Disc 1 %", "Disc 2 %", "Net per UOM", "Amount",
           "Supplier code", "Brand", "Box of", "Selling", "FX", "FX rate", "FX cost", "Remarks"]
MORE_COLUMNS = (C_SUPCODE, C_BRAND, C_BOX, C_SELL, C_FXCUR, C_FXRATE, C_FXCOST, C_REMARKS)
RIGHT = (C_QTY, C_COST, C_D1, C_D2, C_NET, C_AMOUNT, C_BOX, C_SELL, C_FXRATE, C_FXCOST)
READ_ONLY = (C_NET, C_AMOUNT)
HEADING_PADDING = 16  # the header's own margins around its text
DONOR_MIN_WIDTH = 92  # Dept and SKU give room to a cut heading, but never shrink below this


class ComboDelegate(QStyledItemDelegate):
    def __init__(self, choices: list[str], parent=None):
        super().__init__(parent)
        self.choices = choices

    def createEditor(self, parent, option, index):
        c = QComboBox(parent)
        c.addItems(self.choices)
        c.activated.connect(lambda *_: (self.commitData.emit(c), self.closeEditor.emit(c)))
        return c

    def setEditorData(self, editor, index):
        i = editor.findText(index.data() or "")
        editor.setCurrentIndex(max(i, 0))

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText())


class POPage(QWidget):
    closed = Signal()
    changed = Signal()
    open_of = Signal(int)

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self.conn, self.user = conn, user
        self.po_id = None
        self.po = None
        self.amending = False
        self._recalculating = False
        # A new row's first discount. A PO from before Order Forms starts rows with none, as the supplier's quote is typed in.
        self._default_discount = 0
        self.dept_names = {r["id"]: r["name"] for r in masters.list_departments(conn)}
        self.dept_choices = [f"{i} {n}" for i, n in self.dept_names.items()]
        self._build()

    # ------------------------------------------------------------ layout

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 14, 20, 14)
        root.setSpacing(10)

        top = QHBoxLayout()
        self.back = button("Go back", self._leave)
        escape_shortcut(self, self._leave)  # Esc goes back, after asking if anything is unsaved
        top.addWidget(self.back)
        top.addSpacing(8)
        self.title = label("Purchase order", "Title")
        top.addWidget(self.title)
        self.status = label()
        top.addWidget(self.status)
        top.addStretch(1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(6)
        top.addLayout(self.actions)
        root.addLayout(top)

        self.message = label("", "Notice")
        self.message.setWordWrap(True)
        self.message.hide()
        root.addWidget(self.message)

        header, grid_host = panel()
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        self.supplier, self.branch, self.parent_of = QLineEdit(), QLineEdit(), QLineEdit()
        for w in (self.supplier, self.branch, self.parent_of):
            w.setReadOnly(True)
        self.po_date = date_edit()
        self.expected = date_edit(optional=True)
        self.arrangement = QComboBox()
        for code, text in orders.ARRANGEMENTS.items():
            self.arrangement.addItem(text, code)
        self.terms = terms_combo()
        self.instructions = QLineEdit()
        self.instructions.setPlaceholderText("Prints on the supplier's copy")
        self.legacy = QLineEdit()
        self.legacy.setPlaceholderText("From your accounting system")
        self.remarks = QLineEdit()
        self.remarks.setPlaceholderText("Prints on the supplier's copy")
        self.internal = QLineEdit()
        self.internal.setPlaceholderText("Internal only, never on the supplier's papers")
        fields = [("Supplier", self.supplier, 0, 0, 2), ("Branch", self.branch, 0, 2, 1), ("Parent OF", self.parent_of, 0, 3, 1), ("PO date", self.po_date, 0, 4, 1),
                  ("Expected delivery", self.expected, 0, 5, 1), ("Delivery", self.arrangement, 2, 0, 1), ("Payment terms", self.terms, 2, 1, 1),
                  ("Delivery instructions", self.instructions, 2, 2, 1), ("External PO number", self.legacy, 2, 3, 1), ("Remarks", self.remarks, 2, 4, 1),
                  ("Internal notes", self.internal, 2, 5, 1)]
        self.field_labels = {}
        for text, widget, row, col, span in fields:
            self.field_labels[widget] = label(text, "Muted")
            grid.addWidget(self.field_labels[widget], row, col, 1, span)
            grid.addWidget(widget, row + 1, col, 1, span)
        for col, stretch in enumerate((2, 2, 2, 2, 2, 2)):
            grid.setColumnStretch(col, stretch)
        grid_host.addLayout(grid)
        root.addWidget(header)
        self.header_panel = header
        # Read only, the boxes give way to the facts in plain lines, as on the OF, so nothing is greyed out or cut short.
        self.facts = label("", "PanelTitle")
        self.facts.setWordWrap(True)
        self.notes_line = label("", "Muted")
        self.notes_line.setWordWrap(True)
        self.facts.hide()
        self.notes_line.hide()
        root.addWidget(self.facts)
        root.addWidget(self.notes_line)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        root.addWidget(self.tabs, 1)
        self.tabs.addTab(self._items_tab(), "Items")
        self.history = make_table(["When", "Who", "What happened"], stretch=2)
        set_widths(self.history, {0: 150, 1: 170})
        self.tabs.addTab(self.history, "History")
        self.tabs.addTab(self._attachments_tab(), "Attachments")
        self.versions = make_table(["Revision", "Issued", "Issued by", "Fingerprint"], stretch=3)
        set_widths(self.versions, {0: 80, 1: 150, 2: 170})
        self.versions.doubleClicked.connect(lambda *_: self.open_version())
        self.tabs.addTab(self.versions, "Issued versions")

        self.save_shortcut = QShortcut(QKeySequence("Ctrl+S"), self, activated=lambda: self.primary_save and self.primary_save.isEnabled() and self.primary_save.click())
        self.add_shortcut = QShortcut(QKeySequence("Ctrl+N"), self, activated=self.add_row)
        self.primary_save = None

    def _items_tab(self) -> QWidget:
        host = QWidget()
        outer = QVBoxLayout(host)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)
        bar = QHBoxLayout()
        self.add_btn = button("Add blank row", self.add_row)
        self.add_btn.setToolTip("Ctrl+N")
        self.remove_btn = button("Remove row", self.remove_row)
        self.more = QCheckBox("More columns")
        self.more.setToolTip("Supplier code, brand, box, selling price, foreign currency, remarks")
        self.more.toggled.connect(self._toggle_more)
        for w in (self.add_btn, self.remove_btn, self.more):
            bar.addWidget(w)
        bar.addStretch(1)
        outer.addLayout(bar)

        self.grid = QTableWidget(0, len(HEADERS))
        self.grid.setHorizontalHeaderLabels(HEADERS)
        self.grid.verticalHeader().setDefaultSectionSize(28)
        self.grid.verticalHeader().setMinimumWidth(28)
        self.grid.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.grid.setEditTriggers(QAbstractItemView.EditTrigger.AllEditTriggers)
        self.grid.setTabKeyNavigation(True)
        self.grid.horizontalHeader().setHighlightSections(False)
        self.grid.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.grid.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.grid.horizontalHeader().setSectionResizeMode(C_DESC, QHeaderView.ResizeMode.Stretch)
        for col in RIGHT:
            self.grid.horizontalHeaderItem(col).setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.grid.setItemDelegateForColumn(C_DEPT, ComboDelegate(self.dept_choices, self.grid))
        self.grid.setItemDelegateForColumn(C_UNIT, ComboDelegate(list(OFFERED_UNITS), self.grid))
        set_widths(self.grid, {C_DEPT: 118, C_CODE: 130, C_SIZE: 66, C_COLOR: 86, C_QTY: 58, C_UNIT: 76, C_COST: 80, C_D1: 62, C_D2: 62,
                               C_NET: 76, C_AMOUNT: 92, C_SUPCODE: 100, C_BRAND: 80, C_BOX: 52, C_SELL: 72, C_FXCUR: 36, C_FXRATE: 60,
                               C_FXCOST: 60, C_REMARKS: 150})
        self.grid.itemChanged.connect(self._cell_changed)
        self.grid.installEventFilter(self)
        for col in MORE_COLUMNS:
            self.grid.setColumnHidden(col, True)
        outer.addWidget(self.grid, 1)
        outer.addWidget(self._summary())
        return host

    def _summary(self) -> QWidget:
        """A band under the grid, so the grid keeps the window's full width."""
        host = QWidget()
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 6, 0, 0)
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
        controls_host = QWidget()
        controls_host.setLayout(controls)
        controls_host.setFixedWidth(370)
        controls.setContentsMargins(0, 0, 0, 0)
        row.addWidget(controls_host)
        self.money_controls = controls_host
        for w in (self.vat_mode, self.disc_type):
            w.currentIndexChanged.connect(self._recalculate)
        for w in (self.disc_value, self.charges):
            w.textChanged.connect(self._recalculate)

        self.dept_table = make_table(["Department", "Pieces", "Amount"], numeric=(1, 2), stretch=0)
        set_widths(self.dept_table, {1: 60, 2: 100})
        row.addWidget(self.dept_table, 1)

        totals = QVBoxLayout()
        totals.setSpacing(2)
        self.breakdown = QGridLayout()
        self.breakdown.setVerticalSpacing(2)
        self.breakdown.setHorizontalSpacing(16)
        self.rows = {}
        for r, (key, text) in enumerate((("qty", "Total quantity"), ("gross", "Gross amount"), ("item", "Item discounts"), ("overall", "Overall discount"),
                                         ("vat", "VAT"), ("charges", "Other charges"))):
            name, value = label(text, "Muted"), label("")
            value.setAlignment(Qt.AlignmentFlag.AlignRight)
            self.breakdown.addWidget(name, r, 0)
            self.breakdown.addWidget(value, r, 1)
            self.rows[key] = (name, value)
        totals.addLayout(self.breakdown)
        total_row = QHBoxLayout()
        total_row.addWidget(label("PO total", "PanelTitle"))
        self.grand = label(fmt_peso(0), "Total")
        self.grand.setAlignment(Qt.AlignmentFlag.AlignRight)
        total_row.addWidget(self.grand, 1)
        totals.addLayout(total_row)
        self.approval_note = label("", "Muted")
        self.approval_note.setAlignment(Qt.AlignmentFlag.AlignRight)
        totals.addWidget(self.approval_note)
        totals.addStretch(1)
        totals_host = QWidget()
        totals_host.setLayout(totals)
        totals.setContentsMargins(0, 0, 0, 0)
        totals_host.setFixedWidth(330)
        row.addWidget(totals_host)
        host.setFixedHeight(174)
        return host

    def _attachments_tab(self) -> QWidget:
        host = QWidget()
        col = QVBoxLayout(host)
        bar = QHBoxLayout()
        self.attach_btn = button("Add a file", self._attach)
        self.open_attach = button("Open", self._open_attachment)
        bar.addWidget(self.attach_btn)
        bar.addWidget(self.open_attach)
        bar.addStretch(1)
        col.addLayout(bar)
        self.attach_table = make_table(["File", "Added", "Size"], numeric=(2,), stretch=0)
        col.addWidget(self.attach_table, 1)
        return host

    # ------------------------------------------------------------ loading

    def open_po(self, po_id: int):
        self.po_id, self.amending = po_id, False
        self.po = orders.get_po(self.conn, po_id)
        self._show_po()

    def _show_po(self):
        po = self.po
        self.supplier.setText(f"{po['supplier']['code']}  {po['supplier']['name']}")
        self.branch.setText(f"{po['branch']['code']}  {po['branch']['name']}")
        self.parent_of.setText(po["of_number"] or "Made before Order Forms")
        set_date(self.po_date, po["po_date"])
        set_date(self.expected, po["expected_delivery_date"])
        select_data(self.arrangement, po["delivery_arrangement"])
        set_terms(self.terms, po["payment_terms"])
        # Each box shows the start of its text when the text is longer than the box.
        for box, key in ((self.instructions, "delivery_instructions"), (self.legacy, "legacy_po_number"), (self.remarks, "remarks"),
                         (self.internal, "internal_notes")):
            box.setText(po[key])
            box.setCursorPosition(0)
        select_data(self.vat_mode, po["vat_mode"])
        select_data(self.disc_type, po["overall_discount_type"])
        value = po["overall_discount_value"]
        self.disc_value.setText("" if not value else fmt_percent(value).rstrip("%") if po["overall_discount_type"] == DISC_PERCENT else fmt_plain(value))
        self.charges.setText(fmt_plain(po["other_charges_c"]) if po["other_charges_c"] else "")
        self.charges_note.setText(po["other_charges_note"])
        self._recalculating = True
        self.grid.setRowCount(0)
        for item in po["items"]:
            self._append(item)
        self._recalculating = False
        rev = f"  Rev. {po['revision']}" if po["revision"] else ""
        self.title.setText((po["po_number"] or f"Draft #{po['id']}") + rev)
        self._apply_state()
        self._fit_headers()
        self._recalculate()
        self._load_history()
        self._load_attachments()
        self._load_versions()

    def _append(self, it: dict | None = None):
        it = it or {}
        r = self.grid.rowCount()
        self.grid.insertRow(r)
        dept = it.get("department_id")
        values = {
            C_DEPT: f"{dept} {self.dept_names[dept]}" if dept else "",
            C_CODE: it.get("item_code", ""), C_DESC: it.get("description", ""), C_SIZE: it.get("size", ""), C_COLOR: it.get("color", ""),
            C_QTY: fmt_qty(it["qty_m"] * pieces_per_unit(it.get("uom"))) if it.get("qty_m") else "", C_UNIT: it.get("uom") or "PC",
            C_COST: fmt_plain(it["unit_cost_c"]) if it.get("unit_cost_c") is not None else "",
            C_D1: fmt_percent(it.get("disc1_bp")).rstrip("%"), C_D2: fmt_percent(it.get("disc2_bp")).rstrip("%"),
            C_NET: "", C_AMOUNT: "",
            C_SUPCODE: it.get("supplier_item_code", ""), C_BRAND: it.get("brand", ""), C_BOX: str(it["box_size"]) if it.get("box_size") else "",
            C_SELL: fmt_plain(it["selling_c"]) if it.get("selling_c") else "", C_FXCUR: it.get("fx_currency", ""),
            C_FXRATE: fmt_milli(it.get("fx_rate_m")), C_FXCOST: fmt_milli(it.get("fx_cost_m")), C_REMARKS: it.get("remarks", ""),
        }
        for c in range(len(HEADERS)):
            cell = QTableWidgetItem(values.get(c, "") or "")
            if c in RIGHT:
                cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if c in READ_ONLY:
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.grid.setItem(r, c, cell)
        tag = self.grid.item(r, C_DEPT)
        tag.setData(Qt.ItemDataRole.UserRole, {"id": it.get("id"), "product_id": it.get("product_id")})
        # On a PO from an OF only the quantity can change. Say so by locking the rest, rather than refusing on save.
        if self._linked():
            for c in range(len(HEADERS)):
                if c != C_QTY:
                    self.grid.item(r, c).setFlags(self.grid.item(r, c).flags() & ~Qt.ItemFlag.ItemIsEditable)
        return r

    def _linked(self) -> bool:
        return bool(self.po and self.po["of_id"])

    # ------------------------------------------------------------ state

    def _editable(self) -> bool:
        return self.amending

    def _apply_state(self):
        editable = self._editable()
        linked = self._linked()
        for w in (self.po_date,):
            w.setEnabled(False)
        for w in (self.expected, self.arrangement, self.terms, self.instructions, self.remarks, self.internal):
            w.setEnabled(editable)
        for w in (self.vat_mode, self.disc_type, self.disc_value, self.charges, self.charges_note):
            w.setEnabled(editable and not linked)
        self.remove_btn.setVisible(editable)
        self.add_btn.setVisible(editable and not linked)
        self.legacy.setEnabled(editable or self._legacy_editable())
        self.grid.setEditTriggers(QAbstractItemView.EditTrigger.AllEditTriggers if editable else QAbstractItemView.EditTrigger.NoEditTriggers)
        show_if(self.attach_btn, self.po is not None and self.user.can("po.edit"))
        self._show_facts(editable)
        self._status_label()
        self._build_actions()

    def _show_facts(self, editable: bool):
        """Read only, the header is a fact line like the OF's. Only the external PO number keeps its box while it can still be saved."""
        legacy_open = not editable and self._legacy_editable()
        for widget, name in self.field_labels.items():
            shown = editable or (widget is self.legacy and legacy_open)
            widget.setVisible(shown)
            name.setVisible(shown)
        self.header_panel.setVisible(editable or legacy_open)
        # Read only, the discount and charges boxes are just repeats of the totals beside them, as on the OF.
        self.money_controls.setVisible(editable)
        po = self.po
        read_only = not editable and po is not None
        if read_only:
            facts = [po["supplier"]["name"], po["branch"]["name"], f"OF {po['of_number']}" if po["of_number"] else "Made before Order Forms",
                     f"PO date {fmt_date(po['po_date'])}",
                     f"Expected {fmt_date(po['expected_delivery_date'])}" if po.get("expected_delivery_date") else "Expected delivery to be confirmed",
                     orders.ARRANGEMENTS.get(po["delivery_arrangement"], po["delivery_arrangement"] or ""), f"Terms {po['payment_terms'] or 'as agreed'}"]
            if po["legacy_po_number"] and not legacy_open:
                facts.append(f"External PO {po['legacy_po_number']}")
            notes = [f"{name}: {po[k]}" for k, name in (("delivery_instructions", "Delivery"), ("remarks", "Remarks"), ("internal_notes", "Internal notes"))
                     if po.get(k)]
            self.facts.setText("   |   ".join(f for f in facts if f))
            self.notes_line.setText("     ".join(notes))
        self.facts.setVisible(read_only)
        self.notes_line.setVisible(read_only and bool(self.notes_line.text()))

    def _fit_headers(self):
        """A number column is at least as wide as its heading, so 'Cost per UOM' and 'Net per UOM' are never cut short.
        Only a heading that is really cut is widened, and the room comes from Dept and SKU, which are wider than their headings,
        so Description keeps its width. A width the person set themselves is left alone."""
        header = self.grid.horizontalHeader()
        kept = getattr(self.grid, "kept_widths", {})
        fm = QFontMetrics(header.font())

        def heading(col):
            return fm.horizontalAdvance(HEADERS[col]) + HEADING_PADDING

        extra = 0
        for col in RIGHT:
            if col in kept:
                continue
            need = heading(col)
            if self.grid.columnWidth(col) < need:
                if not self.grid.isColumnHidden(col):
                    extra += need - self.grid.columnWidth(col)
                self.grid.setColumnWidth(col, need)
        for col in (C_DEPT, C_CODE):
            if extra <= 0:
                break
            if col in kept or self.grid.isColumnHidden(col):
                continue
            spare = self.grid.columnWidth(col) - max(heading(col), DONOR_MIN_WIDTH)
            give = min(max(spare, 0), extra)
            if give:
                self.grid.setColumnWidth(col, self.grid.columnWidth(col) - give)
                extra -= give

    def _legacy_editable(self) -> bool:
        """The external PO number is only known after an approved PO is entered in your accounting system, so it stays open."""
        return bool(self.po) and self.po["status"] not in (orders.DRAFT, orders.CANCELLED) and self.user.can("po.edit")

    def save_legacy_number(self):
        orders.set_legacy_number(self.conn, self.user, self.po_id, self.legacy.text())
        self.po = orders.get_po(self.conn, self.po_id)
        self._load_history()
        self.changed.emit()
        info(self, "Saved", "The external PO number was saved.")

    def retheme(self):
        """Repaint the colours set on individual labels. Nothing typed on the screen changes."""
        if self.po:
            self.status.setStyleSheet(chip_style(self.po["status"]))
        if self.approval_note.styleSheet():
            self.approval_note.setStyleSheet(f"color:{theme.current.RED};")

    def _status_label(self):
        if not self.po:
            self.status.clear()
            self.status.setStyleSheet("")
            return
        code = self.po["status"]
        self.status.setText(orders.STATUS_LABELS[code])
        self.status.setStyleSheet(chip_style(code))
        self.message.hide()
        if code == orders.CANCELLED:
            self._notice(f"Cancelled: {self.po['cancel_reason']}", "NoticeBad")
        elif code == orders.CLOSED:
            self._notice(f"Closed: {self.po['close_reason']}", "Notice")
        elif (code == orders.APPROVED and self.po["issued_revision"] is not None) or orders.unsent(self.po):
            where = "It goes out from the Print tab with its OF." if self._linked() else "Issue it when ready."
            self._notice(f"Revision {self.po['revision']} hasn't gone to the supplier yet. They still hold revision {self.po['issued_revision']}. {where}", "Notice")
        elif code == orders.DRAFT or code == orders.PENDING:
            self._notice("This PO was started before Order Forms. It can only be cancelled now. Raise the order as a new OF.", "Notice")
        off = payments.dr_differs(self.conn, self.po_id)
        if off:
            self._notice(f"DR differs: the supplier's DRs come to {fmt_peso(abs(off))} {'more' if off > 0 else 'less'} than what this PO came to. Accounting looks into it on Payments.", "NoticeBad")
        pending = [a for a in orders.amendments(self.conn, self.po_id) if a["status"] == orders.PENDING]
        if pending:
            self._notice(f"Amendment waiting for approval: {pending[0]['reason']}. The PO below shows the current approved values.", "Notice")

    def _notice(self, text: str, name: str):
        self.message.setObjectName(name)
        self.message.setStyleSheet("")
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
        add = lambda text, fn, name="": (b := button(text, fn, name), self.actions.addWidget(b))[0]
        if self.amending:
            self.primary_save = add("Save amendment", self.save_amendment, "Primary")
            add("Discard changes", self._discard_amendment)
            return
        status = self.po["status"]
        u = self.user
        # The everyday buttons stay in the bar. The rare ones go under More, so the screen fits a 1366 px laptop.
        more: list[tuple[str, object]] = []
        if self._linked():
            add("Open parent OF", lambda: self.open_of.emit(self.po["of_id"]))
        if status in (orders.DRAFT, orders.PENDING) and u.can("po.cancel"):
            more.append(("Cancel PO", self.cancel_po))
        if status in (orders.APPROVED, orders.ISSUED, orders.PARTIAL):
            if status in (orders.APPROVED, orders.ISSUED) and u.can("po.cancel") and orders.can_cancel(self.conn, self.po["id"]):
                more.append(("Cancel PO", self.cancel_po))
            if status in (orders.ISSUED, orders.PARTIAL) and u.can("po.close") and orders.can_close(self.conn, self.po["id"]):
                more.append(("Close PO", self.close_po))
            if u.can("po.amend") and not orders.paid_by(self.conn, self.po["of_id"]):
                add("Amend", self.start_amendment)
        if status == orders.FULL and u.can("po.close") and orders.can_close(self.conn, self.po["id"]):
            more.append(("Close PO", self.close_po))
        if u.can("po.receive_outside") and orders.can_receive_outside(self.conn, self.po["id"]):
            more.append(("Received in full outside the program", self.receive_outside))
        if self._legacy_editable():
            more.append(("Save external number", self.save_legacy_number))
        if status in pdf.PRINTABLE:
            more.append(("Save PDF", self.save_pdf))
            add("Preview" if status == orders.APPROVED or orders.unsent(self.po) else "Print again", self.print_pdf)
        if more:
            self.more_btn = add("More", None)
            menu = QMenu(self.more_btn)
            for text, fn in more:
                menu.addAction(text, lambda fn=fn: guarded(self, fn))
            self.more_btn.setMenu(menu)
            self.more_actions = [text for text, _ in more]
        else:
            self.more_actions = []
        # A PO from an OF prints with its OF from the Print tab, so the warehouse copy and slips go with it. Only a PO from before OFs prints here.
        if orders.to_issue(self.po) and u.can("po.issue") and not self._linked():
            add("Print and issue", self.issue_po, "Primary")

    # ------------------------------------------------------------ grid

    def add_row(self, item: dict | None = None):
        if not self._editable() or self._linked():
            return
        last_dept = None
        if self.grid.rowCount():
            cell = self.grid.item(self.grid.rowCount() - 1, C_DEPT)
            text = cell.text() if cell else ""
            if text:
                last_dept = int(text.split()[0])
        data = dict(item) if isinstance(item, dict) else {"department_id": last_dept}
        if self._default_discount and not data.get("disc1_bp"):
            data["disc1_bp"] = self._default_discount
        self._recalculating = True
        r = self._append(data)
        self._recalculating = False
        self._recalculate()
        self.grid.setCurrentCell(r, C_DESC if data.get("item_code") or last_dept else C_DEPT)
        return r

    def remove_row(self):
        row = self.grid.currentRow()
        if row < 0:
            return
        tag = self.grid.item(row, C_DEPT).data(Qt.ItemDataRole.UserRole) or {}
        desc = self.grid.item(row, C_DESC).text()
        if desc and not confirm(self, "Remove row", f"Remove '{desc}' from this PO?", "Remove"):
            return
        self.grid.removeRow(row)
        self._recalculate()
        return tag

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if obj is self.grid and event.type() == QEvent.Type.KeyPress and self._editable():
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.grid.currentRow() == self.grid.rowCount() - 1 \
                    and self.grid.currentColumn() in (C_D2, C_AMOUNT, C_REMARKS):
                self.add_row()
                return True
        return super().eventFilter(obj, event)

    def _toggle_more(self, on: bool):
        for col in MORE_COLUMNS:
            self.grid.setColumnHidden(col, not on)

    def _cell(self, r: int, c: int) -> str:
        item = self.grid.item(r, c)
        return item.text().strip() if item else ""

    def _cell_changed(self, item):
        if self._recalculating:
            return
        r, c = item.row(), item.column()
        if c in (C_FXRATE, C_FXCOST):
            try:
                rate, cost = parse_milli(self._cell(r, C_FXRATE)), parse_milli(self._cell(r, C_FXCOST))
            except ValueError:
                rate = cost = 0
            if rate and cost:
                self._recalculating = True
                self.grid.item(r, C_COST).setText(fmt_plain(fx_to_centavos(cost, rate)))
                self._recalculating = False
        self._recalculate()

    def _row_values(self, r: int, strict: bool = True) -> dict | None:
        """One grid row as an item. Blank rows give None. strict raises on bad numbers, otherwise it returns None."""
        texts = [self._cell(r, c) for c in (C_CODE, C_DESC, C_QTY, C_COST)]
        if not any(texts):
            return None
        tag = self.grid.item(r, C_DEPT).data(Qt.ItemDataRole.UserRole) or {}
        where = f"Row {r + 1}: "
        try:
            dept_text = self._cell(r, C_DEPT)
            uom = (self._cell(r, C_UNIT) or "PC").upper()
            qty_m = parse_pieces(self._cell(r, C_QTY), uom)
            box = self._cell(r, C_BOX)
            if box and not box.isdigit():
                raise ValueError("box size must be a whole number")
            selling = self._cell(r, C_SELL)
            return {
                "id": tag.get("id"), "product_id": tag.get("product_id"),
                "department_id": int(dept_text.split()[0]) if dept_text else None,
                "item_code": self._cell(r, C_CODE), "description": self._cell(r, C_DESC), "size": self._cell(r, C_SIZE),
                "color": self._cell(r, C_COLOR), "qty_m": qty_m, "uom": uom, "pieces_per_unit": pieces_per_unit(uom),
                "unit_cost_c": parse_pesos(self._cell(r, C_COST)), "disc1_bp": parse_percent(self._cell(r, C_D1)),
                "disc2_bp": parse_percent(self._cell(r, C_D2)), "supplier_item_code": self._cell(r, C_SUPCODE), "brand": self._cell(r, C_BRAND),
                "box_size": int(box) if box else None, "selling_c": parse_pesos(selling) if selling else None,
                "fx_currency": self._cell(r, C_FXCUR).upper(), "fx_rate_m": parse_milli(self._cell(r, C_FXRATE)),
                "fx_cost_m": parse_milli(self._cell(r, C_FXCOST)), "remarks": self._cell(r, C_REMARKS),
            }
        except ValueError as exc:
            if strict:
                raise POError(where + str(exc)) from None
            return None

    def _collect_items(self) -> list[dict]:
        out = []
        for r in range(self.grid.rowCount()):
            values = self._row_values(r)
            if values:
                out.append(values)
        return out

    def _collect_header(self) -> dict:
        disc_type = self.disc_type.currentData()
        text = self.disc_value.text()
        disc = parse_percent(text) if disc_type == DISC_PERCENT else parse_pesos(text) if disc_type == DISC_AMOUNT else 0
        return {
            "expected_delivery_date": date_value(self.expected), "delivery_arrangement": self.arrangement.currentData(),
            "payment_terms": self.terms.currentText(), "delivery_instructions": self.instructions.text(), "remarks": self.remarks.text(),
            "internal_notes": self.internal.text(), "legacy_po_number": self.legacy.text(), "overall_discount_type": disc_type,
            "overall_discount_value": disc, "vat_mode": self.vat_mode.currentData(), "vat_rate_bp": self.po["vat_rate_bp"],
            "other_charges_c": parse_pesos(self.charges.text()), "other_charges_note": self.charges_note.text(),
        }

    def _recalculate(self, *_):
        if self._recalculating:
            return
        self._recalculating = True
        calc = []
        for r in range(self.grid.rowCount()):
            v = self._row_values(r, strict=False)
            net, amount = "", ""
            if v and v["qty_m"] > 0:
                try:
                    qty = m_to_qty(v["qty_m"])
                    a = compute_line(qty, v["unit_cost_c"], v["disc1_bp"], v["disc2_bp"])
                    net, amount = fmt_plain(a.net_unit), fmt_plain(a.total)
                    if v["department_id"]:
                        calc.append({"department_id": v["department_id"], "qty": qty, "pieces_per_unit": v["pieces_per_unit"],
                                     "unit_cost": v["unit_cost_c"], "disc1_bp": v["disc1_bp"], "disc2_bp": v["disc2_bp"]})
                except ValueError:
                    pass
            self.grid.item(r, C_NET).setText(net)
            self.grid.item(r, C_AMOUNT).setText(amount)
        self._recalculating = False
        try:
            disc_type = self.disc_type.currentData()
            text = self.disc_value.text()
            value = parse_percent(text) if disc_type == DISC_PERCENT else parse_pesos(text) if disc_type == DISC_AMOUNT else 0
            rate = self.po["vat_rate_bp"] if self.po else 1200
            t = compute_totals(calc, disc_type, value, self.vat_mode.currentData(), rate, parse_pesos(self.charges.text()))
        except ValueError as exc:
            self.approval_note.setText(str(exc))
            self.approval_note.setObjectName("Error")
            self.approval_note.setStyleSheet(f"color:{theme.current.RED};")
            return
        self.approval_note.setStyleSheet("")
        self.rows["qty"][1].setText(f"{t.total_pieces:,} pcs" + (f" ({t.total_qty:,} packs)" if t.total_pieces != t.total_qty else ""))
        self.rows["gross"][1].setText(fmt_peso(t.gross))
        self.rows["item"][1].setText("-" + fmt_peso(t.item_discount) if t.item_discount else fmt_peso(0))
        self.rows["overall"][1].setText("-" + fmt_peso(t.overall_discount) if t.overall_discount else fmt_peso(0))
        self.rows["vat"][0].setText("VAT included" if self.vat_mode.currentData() == VAT_INCLUSIVE else "VAT")
        self.rows["vat"][1].setText(fmt_peso(t.vat))
        for w in self.rows["vat"]:
            w.setVisible(bool(t.vat))
        self.rows["charges"][1].setText(fmt_peso(t.other_charges))
        self.grand.setText(fmt_peso(t.grand_total))
        self.approval_note.setText("Approved through its Order Form." if self._linked() else "")
        rows = [[f"{d} {self.dept_names[d]}", f"{x.pieces:,}", fmt_plain(x.amount)] for d, x in sorted(t.departments.items())]
        fill_table(self.dept_table, rows)

    # ------------------------------------------------------------ actions

    def issue_po(self):
        rev = f" revision {self.po['revision']}" if self.po["revision"] else ""
        if not confirm(self, "Print and issue PO", f"Issue {self.po['po_number']}{rev} to {self.po['supplier']['name']}? "
                       "It opens for printing and moves to Completed. Who issued it and when are recorded, and this exact document is kept for good.",
                       "Print and issue"):
            return
        orders.issue(self.conn, self.user, self.po_id)
        self.open_po(self.po_id)
        self.changed.emit()
        self._open_copy(self._pdf_bytes(first=True))

    def cancel_po(self):
        name = self.po["po_number"] or f"Draft #{self.po_id}"
        reason = ask_text(self, "Cancel PO", f"Why is {name} being cancelled? Its number will never be used again.")
        if reason is None:
            return
        orders.cancel(self.conn, self.user, self.po_id, reason)
        self.open_po(self.po_id)
        self.changed.emit()

    def receive_outside(self):
        """Until branches count in the program. The PO closes as delivered in full, so anything short has to come off the PO first."""
        reason = ask_text(self, "Received in full outside the program",
                          f"Only if every piece on {self.po['po_number']} arrived. If something is short, amend the PO's quantities first.\n\n"
                          "Where is this delivery recorded? For example: receiving report no. 1234.")
        if reason is None:
            return
        orders.receive_outside(self.conn, self.user, self.po_id, reason)
        self.open_po(self.po_id)
        self.changed.emit()

    def close_po(self):
        reason = ask_text(self, "Close PO", "Why is this PO being closed? For example: supplier can't deliver the rest.")
        if reason is None:
            return
        orders.close(self.conn, self.user, self.po_id, reason)
        self.open_po(self.po_id)
        self.changed.emit()

    def _is_issued_copy(self) -> bool:
        return self.po["status"] != orders.CANCELLED and self.po["issued_revision"] == self.po["revision"]

    def _pdf_bytes(self, first: bool = False) -> bytes:
        """The supplier's copy: the stored original once this revision is issued, otherwise a marked preview."""
        if self._is_issued_copy():
            stored = orders.issued_pdf(self.conn, None if first else self.user, self.po_id, self.po["revision"])
            if stored:
                return stored
        return pdf.render_supplier_po(self.conn, self.po_id)

    def _may_print(self) -> bool:
        """A preview is free. An issued copy printing again asks first."""
        return not self._is_issued_copy() or confirm_reprint(self, self.po["po_number"])

    def _file_name(self) -> str:
        return self.po["po_number"] + (f"-Rev{self.po['revision']}" if self.po["revision"] else "") + ".pdf"

    def save_pdf(self):
        if not self._may_print():
            return
        path = save_file(self, "Save supplier PDF", str(Path.home() / "Documents" / self._file_name()), "PDF (*.pdf)")
        if path:
            Path(path).write_bytes(self._pdf_bytes())
            open_path(path)

    def save_blind_sheet(self):
        path = save_file(self, "Save the blind receiving sheet", str(Path.home() / "Documents" / f"{self.po['po_number']} Blind sheet.pdf"), "PDF (*.pdf)")
        if path:
            Path(path).write_bytes(pdf.blind_copy(self.conn, self.user, self.po_id))
            open_path(path)

    def print_pdf(self):
        """Opens in the PDF viewer, where it can be printed. A preview is free. An issued file printing again is marked as a copy, and recorded."""
        if self._may_print():
            self._open_copy(self._pdf_bytes())

    def _open_copy(self, data: bytes):
        path = write_print_file(self._file_name().removesuffix(".pdf"), data)
        self._load_history()
        open_path(path)

    def _load_versions(self):
        rows = orders.issued_versions(self.conn, self.po_id)
        fill_table(self.versions, [[r["revision"], fmt_datetime(r["issued_at"]), r["issued_name"], r["sha256"][:16]] for r in rows],
                   keys=[r["revision"] for r in rows])
        self.tabs.setTabText(3, f"Issued versions ({len(rows)})" if rows else "Issued versions")

    def open_version(self):
        revision = selected_key(self.versions)
        if revision is None:
            return
        if not confirm_reprint(self, f"{self.po['po_number']} revision {revision}"):
            return
        path = write_print_file(f"{self.po['po_number']}-issued-rev{revision}", orders.issued_pdf(self.conn, self.user, self.po_id, revision))
        self._load_history()
        open_path(path)

    def start_amendment(self):
        self.amending = True
        self._apply_state()
        self._notice("Amending. Only quantities and delivery terms can change here. A new price or product needs a new OF. "
                     "More quantity goes to the approver." if self._linked() else
                     "Amending. The supplier, branch and PO date stay as they are. More quantity or a higher total goes to the approver.", "Notice")

    def _discard_amendment(self):
        self.open_po(self.po_id)

    def save_amendment(self):
        header, items = self._collect_header(), self._collect_items()
        reason = ask_text(self, "Amend PO", "What is changing and why? This is kept in the PO's history.")
        if reason is None:
            return
        changes = {k: header[k] for k in orders.AMENDABLE_HEADER if header[k] != self.po[k] and not (header[k] in ("", None) and self.po[k] in ("", None))}
        result = orders.amend(self.conn, self.user, self.po_id, changes, items, reason)
        self.open_po(self.po_id)
        self.changed.emit()
        if result["status"] == "APPLIED" and result["amendment_id"] is None:
            info(self, "Saved", "Only the internal notes changed, so the supplier's copy still stands. Nothing needs printing again.")
        elif result["status"] == "APPLIED":
            where = "It goes to the supplier from the Print tab, with its OF." if self._linked() else "Print and issue it to the supplier when ready."
            info(self, "Amended", f"Saved as revision {result['revision']}. {where} The earlier issued version is kept.")
        else:
            info(self, "Sent for approval", "This amendment increases the PO, so it waits for the approver.")

    def _leave(self):
        if self._editable() and self._dirty() and not confirm(self, "Leave this PO", "Leave without saving your changes?", "Leave"):
            return
        self.closed.emit()

    def _dirty(self) -> bool:
        return self.amending

    # ------------------------------------------------------------ tabs

    def _load_history(self):
        rows = []
        for r in orders.audit_trail(self.conn, self.po_id):
            rows.append([fmt_datetime(r["occurred_at"]), r["user_name"], r["summary"] or r["action"]])
        fill_table(self.history, rows)

    def _load_attachments(self):
        files = orders.list_attachments(self.conn, self.po_id, self.user)
        fill_table(self.attach_table, [[f["file_name"], fmt_datetime(f["added_at"]), f"{f['size_bytes'] / 1024:,.0f} KB"] for f in files],
                   keys=[f["id"] for f in files])

    def _attach(self):
        path, _ = QFileDialog.getOpenFileName(self, "Add a file to this PO")
        if path:
            orders.add_attachment(self.conn, self.user, self.po_id, path)
            self._load_attachments()
            self._load_history()

    def _open_attachment(self):
        # The row picked, by its id, so a sorted list still opens the file that was clicked.
        key = selected_key(self.attach_table)
        if key is None:
            return
        found = [f for f in orders.list_attachments(self.conn, self.po_id, self.user) if f["id"] == key]
        if not found:
            return
        row = found[0]
        if orders.is_program(row["file_name"]) or orders.is_program(row["stored_name"]):
            raise POError(f"'{row['file_name']}' is a program, so it isn't opened from here. Ask the purchaser for the document itself.")
        path = orders.attachment_path(row)
        if not path.is_file():
            raise POError(f"'{row['file_name']}' is no longer in the attachments folder.")
        if orders.attachment_changed(row) and not confirm(
                self, "Open file", f"This file changed since it was attached. Open '{row['file_name']}' anyway?", "Open"):
            return
        open_path(path)
