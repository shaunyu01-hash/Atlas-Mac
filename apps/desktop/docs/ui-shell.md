# UI shell and shared widgets

The desktop UI is one `QMainWindow` (`ui/main_window.py:MainWindow`) with a fixed 212 px sidebar (`QListWidget#Nav`) and a `QStackedWidget` of pages. Pages are built lazily the first time they are opened, the sidebar is assembled per role, and after any save the page on screen is redrawn while every other built page is marked stale and redrawn when next shown. All shared building blocks (tables, trees, paging, column chooser, date boxes, month picker, dialogs, error handling) live in `ui/common.py`; all colour lives in `ui/theme.py` as tokens fed into one application stylesheet. Screens call service functions (`order_forms`, `masters`, `auth`, ...) and wrap every user action in `guarded`, which turns `POError` / `PermissionDenied` into a "Can't do that" message box.

## Files

| File | Role | Key classes / functions |
|---|---|---|
| `atlaspo/__main__.py` | Program start, login loop, restart after logout/restore | `main`, `Session.start/restart/restart_after_restore`, `install_error_handler` |
| `Atlas PO ERP.pyw` | Real-data launcher (2 lines: `main()`) | — |
| `Atlas PO ERP (Sample Data).pyw` | Calls `sample_data.ensure()` (sets `PO_DATA_DIR` to the sample folder, or `PO_SAMPLE_DIR`) then `main()` | — |
| `ui/main_window.py` | Login dialogs, main window, sidebar, page registry, sidebar counts, theme toggle | `FirstRunDialog`, `LoginDialog`, `ChangePasswordDialog`, `MainWindow` (`_page`, `_wire`, `_navigate`, `refresh_all`, `_update_counts`, `_retheme`) |
| `ui/common.py` | Every shared widget and helper | `guarded`, `button`/`ActionButton`, `make_table`, `make_tree`, `fill_table`, `Pager`, `keep_place`, `smart_columns`/`ColumnChooser`, `WidthKeeper`, `date_edit`/`SmartDateEdit`, `month_picker`, `confirm`, `ask_approve`, `StatusDelegate`, `PersonDelegate`, `show_people` |
| `ui/theme.py` | Light/dark tokens, stylesheet, `QPalette`, saved mode, per-user `QSettings` | `LIGHT`, `DARK`, `current`, `apply`, `stylesheet`, `status_colors`, `saved_mode`, `save_mode`, `_settings`, `PERSON_COLORS` |
| `ui/settings_page.py` | Admin settings: approval policy, company/tax, OF layout, users, backup/restore | `SettingsPage`, `UserDialog` |
| `ui/pages.py` | Approvals, Print, Suppliers, Product catalog, Branches, Reports + layout helpers | `page_layout`, `StretchFloor`, `ApprovalsPage`, `PrintPage`, `SuppliersPage`, `ProductsPage`, `BranchesPage`, `ReportsPage` |
| `ui/assets/` | `app.ico`/`app.png`, combo arrows `down.svg` / `down-dark.svg`, sidebar glyphs `icons/*.svg` (plus `check.svg` for checkboxes) | — |

Other page modules (`of_page.py`, `po_page.py`, `receiving_page.py`, `payments_page.py`, `warehouse_page.py`, `claim_stub_page.py`, `item_pass_page.py`, `debit_memo_page.py`, `selling_page.py`) are documented with their feature areas.

## Startup and login

`main()` (`__main__.py:99`): sets `sys.excepthook = common.excepthook`, creates `QApplication`, applies `theme.saved_mode()`, then `Session.start()`:

1. `db.open_database()` + `PRAGMA quick_check`. A `POError`, a busy/locked database (`db.BUSY_MESSAGE`) or any other error is explained in a `QMessageBox.critical` and the program exits; unexpected errors go to `errors.log` via `common.log_error`.
2. No users yet: `FirstRunDialog` creates the first admin (`auth.create_first_admin`). No default password ships. Otherwise `LoginDialog` (`auth.login`); wrong logins show the `POError` text inline in `self.problem` (red `#Error` label), clear the password and refocus it. Sample databases (`sample_data.is_sample`) show the sample logins hint.
3. `user.must_change_password` forces `ChangePasswordDialog(required=True)`; cancelling exits.
4. `backup.auto_backup(at_start=True)`, then `MainWindow(conn, user, on_logout=restart, on_restored=restart_after_restore)`.

Log out (`MainWindow._logout`) runs `auto_backup` then `Session.restart`, which closes the window and connection and shows the login again (`setQuitOnLastWindowClosed(False)` while the dialog comes up). `closeEvent` also backs up; a failure is shown but never blocks closing. After a restore (`SettingsPage.restored`) the session reopens without the closing backup.

## Navigation

The sidebar is built once in `MainWindow.__init__` (`main_window.py:205-269`). `factories` maps an attribute name to a page class, or `None` when the user lacks the permission (`receiving.count`, `receiving.review`, `payment.view`, `settings.edit`). Roles RECEIVER and WAREHOUSE can never be combined with another role (`auth._check_receiver_alone`), so the "only" checks below are exact.

| Who (`user.roles`) | Sidebar groups and entries |
|---|---|
| `{"RECEIVER"}` | Receiving: Receiving |
| `{"ACCOUNTING"}` | Accounting: Payments, Central Warehouse, Received, Debit memos, Claim stubs, Item passes |
| `{"WAREHOUSE"}` | Warehouse: Central Warehouse, Claim stubs, Item passes |
| ADMIN | Purchasing: Orders, Approvals, Print, Central Warehouse, Received, Debit memos, Claim stubs, Item passes, Payments · Sales: Selling prices · Records: Inquiry, Suppliers, Product catalog, Branches · Reports and settings: Reports, Settings |
| APPROVER, VIEWER | As ADMIN but group "Reports" without Settings |
| BUYER | As APPROVER but no Payments (`payment.view` excludes BUYER) |

ACCOUNTING with another role (e.g. APPROVER) falls into the full layout. Seeing a page does not mean being able to act: buttons are hidden per permission inside each page (`show_if`), and every service still calls `auth.require`.

Not in the sidebar: `of_page` (`OFPage`) and `po_page` (`POPage`) are opened via `open_of(id)` / `open_po(id)` signals, and leave via their `closed` signal (`_back_from_document`). `Ctrl+Shift+N` → `new_of` (ignored for limited roles or without `of.edit`).

**Registry and lazy building.** `MainWindow.__getattr__` builds a page on first access (`win.received` builds it); `_page(name)` calls `cls(self.conn, self.user)`, wires it (`_wire`), and adds it to the stack. `_built(name)` returns a page only if it already exists. `win.pages` / `win.nav_rows` build every page (tests use them). `_fit_to` gives every off-screen page an `Ignored` size policy so one wide page does not hold the window wide.

**Refresh contract.**
- `_navigate(row)`: if the page already existed, `_prepare(page)` (reload supplier/choice combos on Inquiry, Orders, Reports, Products) then `page.refresh()`. A freshly built page is assumed current.
- `refresh_all()`: refresh the visible page, mark every other built page stale (`self._stale`), update counts. Stale pages refresh in `_shown` when the stack switches to them, however they were reached.
- `_wire` connects `changed` → `refresh_all` only for `of_page, po_page, orders_page, approvals, print_pos, suppliers, products, branches, settings` (and `payments` when `received` exists). Receiving-side pages use targeted links instead (`receiving → received, item_passes`; `received → debit_memos, claim_stubs`; `debit_memos/claim_stubs/item_passes/central → received`), via `_call_built(name, "refresh")`, which never builds a page.
- `_update_counts` writes "Approvals  (n)", "Print  (n)", "Claim stubs  (n)" (`cs.approve_request`), "Payments  (n)" (`payment.signoff`) straight from services. For unbuilt pages it calls the unbound method with the window as `self` (`ApprovalsPage.count(self)`, `PrintPage.count_to_print(self)`), so those methods may only use `self.conn` / `self.user`. It also shows "No Warehouse login yet" to `users.edit` holders (`auth.warehouse_login_missing`).
- Leaving an open OF/PO by sidebar asks the document first (`_leave_document` → `page._leave()`); if the user stays, the sidebar row is reverted with signals blocked.

## Shared widgets and helpers (common.py)

| Helper | Purpose | When to use |
|---|---|---|
| `guarded(parent, fn, *args)` | Runs `fn`; `POError` (incl. `PermissionDenied`, a subclass, `db.py:21`) and `ValueError` → `error()` "Can't do that" box with the text as written; locked DB → `BUSY_MESSAGE` + one line in `errors.log`; anything else → `show_unexpected` (critical box + traceback in `errors.log`). Returns `None` on failure. | Around every action that is not already a `button(...)` callback: shortcuts, `returnPressed`, dialog `accepted` |
| `button(text, on_click, name="", lock=True)` / `ActionButton` | Push button whose click is already `guarded` and locked for 350 ms (`LOCK_MS`) against double clicks. `name` = `"Primary"` (teal), `"Danger"`, `"Count"`. `lock=False` still guards. | Every button. Never connect `QPushButton.clicked` raw |
| `excepthook` | Installed as `sys.excepthook`; errors outside buttons get the same treatment | Already installed by `main()` |
| `error`, `info`, `confirm(parent, title, msg, yes)`, `ask_text`, `ask_approve(parent, what)` / `ApproveDialog` | Message boxes. `confirm` puts "Go back" (default + Esc) left and the action as `Primary` right; a stray Enter does nothing. `ask_approve` returns remarks (maybe "") or `None` | All confirmations; never a raw `QMessageBox.question` |
| `confirm_reprint`, `print_order_forms`, `open_for_printing`, `choose_printer`, `print_once`, `open_email_draft`, `save_file` | Printing: combined PDF in `data_dir()/print` opened in the viewer; or direct one-copy printing | Any print/export action |
| `label(text, name)` | `QLabel` with an object name for styling (`Title`, `PanelTitle`, `Muted`, `Warn`, `Error`, `Empty`, `Total`, `Notice`, `NoticeOk`, `NoticeBad`) | All static text |
| `panel()`, `narrow(inner, 620)`, `left_aligned(w)`, `workflow_strip(steps)` | `#Panel` frame + layout; width-capped form host; natural-width button in a form row; step strip with `mark(i)` | Page layout |
| `page_layout(widget, title)` (in `pages.py`) | Root `QVBoxLayout` (margins 20/14) with a title row; returns `(root, top)` | First line of every page `__init__` |
| `StretchFloor` (in `pages.py`) | One stretching column that never shrinks below a minimum; other columns can `squeeze` first | Lists whose main text must stay readable at 1366 px |
| `make_table(headers, numeric=(), stretch=None)` | Read-only, single-row-select `QTableWidget`, right-aligned numeric columns, header elides right, sortable | Flat lists |
| `make_tree(...)`, `fit_tree_columns(...)`, `hide_unused(tree, chooser, cols)` | Same for `QTreeWidget` (rows open to children; double-click is left for opening). Column fitting respects user widths | Grouped lists |
| `fill_table(table, rows, keys, status_col)` | Fills display strings; `keys[r]` goes on column 0 as `UserRole`; `status_col` gets a `StatusDelegate` chip; re-applies sort | Every table fill |
| `selected_key(table)`, `select_data(combo, value)`, `combo_value(combo)` | Read the row id / set or read a combo's data (`combo_value` only if the typed text is a real choice) | Selection and combos |
| `status_item`, `chip_style(code)`, `StatusDelegate` | Status chips from `theme.status_colors`; labels from `orders.STATUS_LABELS` | Status columns / document status labels |
| `show_people(table, col, chips, user_ids)` / `PersonDelegate` | Coloured initials chips (`auth.chips(conn)`) | "Who counted / who did it" columns |
| `smart_columns(table, key, hidden=())` → `ColumnChooser` | Per-login column choice saved in `QSettings` `columns/<key>`; right-click headers or `chooser.open_under(btn)`. First column always stays. Includes a `WidthKeeper` | Every list; key convention `f"<page>-{user.username}"` |
| `WidthKeeper`, `set_widths(table, {col: px})` | Remembers dragged widths (`view.kept_widths`); `set_widths` never overrides them | Starting widths |
| `enable_sort`, `resort`, `sort_key`, `SortItem`, `PIN_BOTTOM_ROLE` | Header-click sorting that understands dates ("Nov 2, 2026"), pesos, counts; property `no_sort` disables; pinned rows (Totals) stay last | Automatic via `make_table`/`make_tree` |
| `Pager(on_change)` + `keep_place(view)` | Paging (50/100/200 per page, saved as `lists/page_size`). In `on_change`, refill inside `with keep_place(view):` and pass rows through `pager.take(rows)` or call `pager.show_counts(shown, total)` after a SQL `LIMIT`. Call `pager.reset()` on new search/filter/tab. Hidden when one page fits | Any list that can grow without bound |
| `register_tabs(tabs, on_change)`, `show_tab_counts` | `QTabBar` holding keys in tab data, with counts in the labels | Register-style pages |
| `date_edit(value, optional=False)` / `SmartDateEdit`, `set_date`, `date_value` | `MM-dd-yyyy` box typed month-day-year without the mouse; optional boxes show "Not set" (Delete clears); impossible dates are refused and `date_value` then raises `POError` so nothing saves. ISO strings in/out | Every date field |
| `month_picker(date_from, date_to)` | Combo of the last 36 months (`MONTHS_BACK`) that fills From/To with one refresh | Beside every From/To filter (tested) |
| `search_combo()`, `terms_combo()`, `set_terms` | Type-to-find combo (contains, case-insensitive); payment-terms combo with no autocomplete | Supplier/product pickers, terms |
| `escape_shortcut(widget, action)` | Esc runs `action`, but only cancels cell editing if a cell is being edited | Pages with a Back action |
| `show_if(widget, on)` | Hides (not greys) controls the user may not use | Every permission-dependent control |
| `fmt_date`, `fmt_datetime`, `show_text`, `icon(name)`, `app_icon()` | "Nov 2, 2026"; fill a read-only box showing its start; theme-tinted sidebar glyph | Display |

Money and quantity inputs: there is no shared input widget. Pages use plain `QLineEdit` (sometimes with a `QRegularExpressionValidator`, e.g. `SuppliersPage`) and parse with `money.parse_pesos`, `parse_percent`, `parse_qty`, `parse_pieces`; display with `fmt_peso`, `fmt_plain`, `fmt_qty`, `fmt_percent`. Parse errors raise `POError`/`ValueError`, which `guarded` shows.

## Theme

`theme.py` holds two `SimpleNamespace` palettes with identical keys (`LIGHT`, `DARK`; test enforced): surfaces `BG, PANEL, BORDER, CONTROL, HEADER_BG`, text `TEXT, MUTED, FAINT`, accent `ACCENT, ACCENT_HOVER, ACCENT_TEXT, ACCENT_PALE`, data colours `RED/AMBER/GREEN/BLUE` + `*_PALE`, `GRAY_PALE`, `BRANCH_TINT`, `ROW_RULE`, `HOVER`, `PRESSED`, `PRIMARY_OFF(_TEXT)`, `DANGER_LINE`, `SCROLL`, `TOOLTIP(_TEXT)`, `ARROW`, `EDITABLE` (cell to type in). `theme.current` is the active palette; `theme.EDITABLE` resolves through a module `__getattr__`.

`theme.apply(mode)` swaps `current`, sets the Qt colour scheme (dark title bar), an app `QPalette` (for message boxes, calendar), and `app.setStyleSheet(stylesheet(current))`. The mode is saved per Windows user in `QSettings("Atlas PO ERP", "PO System")` key `appearance/mode` (never in the database). The sidebar "Dark mode"/"Light mode" button calls `_toggle_theme` → `apply`, `save_mode`, `_retheme` (re-tints nav icons, calls `of_page.retheme()`, `po_page.retheme()`, `orders_page.refresh()`). PDFs always stay light.

Styling a new widget: give it an object name already in the stylesheet (`label(..., "Muted")`, `button(..., "Primary")`, `panel()`) rather than an inline stylesheet. If you must paint a colour in code (item foregrounds, delegates, inline `setStyleSheet`), read it from `theme.current.X` at paint/refresh time, never a hex literal, and make sure your page repaints on refresh, or add it to `_retheme`. New tokens go in **both** palettes; the stylesheet may only use palette colours (`test_dark_stylesheet_has_no_leftover_light_colour`). Colour is for data (status chips, branch columns); chrome stays neutral with teal for the active thing. Nothing animates.

## Settings and general pages

`settings_page.py`: `SettingsPage` (ADMIN only, factory gated by `settings.edit`) is a `QTabWidget` with Approval policy (`orders.policy`, threshold, approvers), Company (company fields, logo, VAT rate/default), Order Form layout (field/column order, `of_layout`), Users (`make_table` + `UserDialog` for new/edit, reset password, chips, role-conflict notice for legacy Buying+Accounting logins), Backup and data (backup folder, keep days, Back up now, Restore selected / from a file, health check). Signals: `changed` (→ `refresh_all` and `of_page.apply_layout`), `restored` (→ session restart). Save handlers call `auth.require(self.user, "settings.edit")` themselves before writing settings.

`pages.py` (there is no dashboard page; the first sidebar page, Orders, lives in `of_page.py`): `ApprovalsPage` (pending OFs and amendments, approve/reject with `ask_approve`, separation-of-duties checks in `_may_decide_*`, rejected history with a `Pager`), `PrintPage` (print-and-issue batches, DRs to email, delivery receipts; side panel closed by Esc), `SuppliersPage`, `ProductsPage`, `BranchesPage` (list + form split, editing gated by `supplier.edit` / `master.edit` / `branch.manage`; read-only fields when not allowed), `ReportsPage` (report list from `reports.REPORTS`, date/supplier/product filters, pinned Total row, Export to Excel).

## How to add a new screen

1. Create `class MyPage(QWidget)` in the right `ui/*_page.py` with signature `__init__(self, conn, user, parent=None)`; store `self.conn, self.user`. Declare `changed = Signal()` (and `open_of`/`open_po = Signal(int)` if it opens documents).
2. Start with `root, top = page_layout(self, "Title")`. Put actions in `top` using `button(...)`; the main action uses `"Primary"`. Hide disallowed actions with `show_if(btn, user.can("perm"))`.
3. Lists: `make_table`/`make_tree`, `set_widths`, `smart_columns(table, f"mypage-{user.username}")` with a "Columns" button, `fill_table(..., keys=ids, status_col=...)`. Unbounded lists get a `Pager` and `keep_place`. Date filters get `date_edit(optional=True)` pairs plus `month_picker`.
4. Write `refresh(self)`: read through service functions (UI may do small read-only queries but never enforces rules), refill widgets, keep selection. The window calls it on every navigation to an already-built page and when it is stale.
5. Actions: call the service (which does `auth.require` + `db.tx` + `db.audit`), then `self.refresh()` and `self.changed.emit()`. Raise `POError("plain message")` for user mistakes; `button`/`guarded` display it. Use `confirm(...)` before anything irreversible.
6. Register in `MainWindow.__init__`: add `"my_page": MyPage` (or `MyPage if user.can(...) else None`) to `factories`, then add `("Sidebar text", "<glyph>", "my_page")` to the right group in `layout`, and to the limited-role layouts if those roles need it. A new glyph is a 24×24 SVG in `ui/assets/icons/` drawn in `#5B6470` (recoloured by `icon()`).
7. Wire it in `_wire`: add the name to the `refresh_all` list (line 334) or to `links`; connect `open_of`/`open_po`. If it has supplier combos to reload, extend `_prepare`. If the sidebar label needs a count, extend `_update_counts` with a service query, not a page build.
8. Check the 1366×768 laptop: `NAV_ROW = 26`/`NAV_HEAD = 20` are tuned so every entry fits without scrolling; adding entries may break that.
9. Add a test (below).

## Keyboard and UX conventions

- `Ctrl+Shift+N` new OF (window). On OF/PO: `Ctrl+S` presses the primary save button, `Ctrl+N` adds a row, Esc goes Back via `escape_shortcut` (asks about unsaved changes).
- Lists: `Ctrl+O` opens the selected document (Approvals, Print, Orders, Inquiry); Enter/double-click opens (`itemActivated`, `cellDoubleClicked`); Esc closes an open side panel (Print, Orders, Payments). Number boxes search on Enter (`returnPressed` wrapped in `guarded`).
- Confirmations default to "Go back"; Enter never confirms by accident. `ApproveDialog` focuses the remarks box.
- Dates are typed `MMDDYY`/`MMDDYYYY` with no mouse; Tab leaves a "Not set" box in one step.
- Hide what a person cannot do instead of greying it; read-only forms say why (e.g. "Only Admin can add or change a branch.").
- Wording: plain purchasing words, calm and exact; error text is the `POError` message verbatim. Lists show "Nothing matches..." style empty notes.
- Sidebar counts are short ("Payments  (3)") with a tooltip for detail.

## Tests

Run one file from `apps/desktop`: `python -E -m pytest tests/test_x.py -q -p no:cacheprovider`. Do not run the full suite for UI work (12–25 min).

Conventions (`tests/conftest.py`, `test_fix_shell.py`): most files set `os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")` before importing Qt, and define `app` as `QApplication.instance() or QApplication([])`. `conftest` autouse fixtures delete leftover top-level widgets (`free_windows`) and redirect `theme._settings` to a temp INI (`own_settings`), so column/page-size/dark-mode choices never touch the registry. `env`/`conn`/`world` give a temp `PO_DATA_DIR` database with admin `boss`, buyer, warehouse, supplier `ABC`, and branches `NORTH, SOUTH, EAST, WEST = 2, 4, 6, 8`. Build `MainWindow(world.conn, world.admin)` and access pages as attributes (`win.received`). Never let a dialog block: monkeypatch `QMessageBox.critical/warning/information`, `common.error`/`common.info`, and the copies imported into the module under test (see the `boxes` fixture), and `common.confirm`/`ask_approve` when an action asks. Use `app.processEvents()` (a `pump` helper) for deferred `QTimer` work.

Relevant files: `test_fix_shell.py` (lazy pages, login, sidebar, settings users), `test_theme.py` (contrast, palettes, toggle keeps typing), `test_chips_and_columns.py` (chips, column chooser), `test_month_picker.py` (also asserts date-filter pages have `.month`), `test_rules_paging_helper.py` / `test_rules_paging_lists.py` (Pager on each list), `test_fix_pages_b.py` (pages.py screens), `test_fix_common.py` (confirm, dates, sorting, pinned rows, busy DB, excepthook, widths), `test_ui.py` (broad screen tests).

Known DPI/font-flaky width tests that also fail on untouched code: `test_fix3.py::test_received_counting_and_documents_show_whole_at_1366` and `test_fix_dm_ip.py::test_the_debit_memo_date_has_room_for_any_date`.

## Gotchas

- Scripts must set `PO_DATA_DIR` and `PO_SAMPLE_DIR` to temp folders before importing `atlaspo`; `db.data_dir()` otherwise falls back to `Documents\Atlas PO ERP` (real data). Always `python -E`.
- `from .common import error` copies the function into the importing module: patching `common.error` covers `guarded` but not a page that calls its own imported `error`/`info`.
- `PermissionDenied` subclasses `POError`, so `guarded` shows its text as an ordinary "Can't do that" message. Never catch it to hide a button's failure; hide the button with `show_if` and leave `auth.require` in the service.
- A page whose `changed` is not wired in `_wire` silently leaves sidebar counts and other pages stale.
- `ApprovalsPage.count` / `PrintPage.count_to_print` run with the window as `self`; keep them to `self.conn`/`self.user`.
- Inline colours set from `theme.current` (e.g. Settings user-conflict red) only update on the page's next `refresh`, unless the page is in `_retheme`.
- `QSettings` names `("Atlas PO ERP", "PO System")` must not change; they key everyone's saved columns, widths, page size and mode.
- `fill_table` disables sorting during the fill and re-sorts; to keep a Total last, set `PIN_BOTTOM_ROLE` on its first cell (see `ReportsPage.run`).
- Some pages run small read-only SQL directly (`BranchesPage.refresh` counts POs; `_selected` reads a branch row). Keep any rule or write in services.
- `ActionButton.setEnabled` during the 350 ms lock is remembered and applied after unlock; tests that click twice quickly must wait or `processEvents`.
