# Platform: database, schema, auth, money, backup

Everything the business services stand on. One SQLite file (`po.sqlite` in the data folder) opened in autocommit mode, with every
write wrapped in `db.tx()` (`BEGIN IMMEDIATE`). The schema is version 1 DDL plus migrations `MIGRATE_V2` to `MIGRATE_V31`, run by one
runner for new and old files alike. Many rules live in the database as triggers (append-only tables, permanent numbers, locked
documents, status paths), so a service can't break them even by mistake. `auth` holds the role/permission map and the login rules,
`money` does all arithmetic in `Decimal` and returns whole centavos, `backup` makes checked copies, restores safely and runs the
health check. The file may sit on a network share used by several PCs at once, which shapes almost every choice below.

## Files

| File | Role | Key public names |
|---|---|---|
| `atlaspo/db.py` | Connection, transactions, data folder, migration runner, audit, settings | `POError`, `PermissionDenied`, `DatabaseBusy`, `DataFolderUnreachable`, `now/timestamp/today`, `data_dir`, `db_path`, `pointer_folder`, `connect`, `tx`, `initialize`, `open_database`, `audit`, `get_setting`, `set_settings`, `BUSY_TIMEOUT_MS` |
| `atlaspo/schema.py` | All DDL, seed data, migrations | `SCHEMA_VERSION` (31), `DDL`, `DEPARTMENTS`, `DESTINATIONS`, `DEFAULT_SETTINGS`, `MIGRATE_V2..V31`, `SUPPLIER_TERM_COLUMNS`, `V28_COLUMNS`, `V28_TRIGGERS`, `V28_TRIGGER_SQL` |
| `atlaspo/auth.py` | Users, passwords, roles, permissions, person chips | `ROLES`, `PERMISSIONS`, `User`, `require`, `login`, `create_first_admin`, `create_user`, `update_user`, `reset_password`, `change_own_password`, `role_conflict`, `list_users`, `chips`, `get_user`, `has_users`, `warehouse_login_missing`, `job_line` |
| `atlaspo/money.py` | Pure arithmetic, parsing, formatting | `round_centavos`, `parse_*`, `fmt_*`, `compute_line`, `compute_totals`, `compute_order`, `split_by_weight`, `requires_approval`, `selling_from_cost`, ... |
| `atlaspo/backup.py` | Backups, restore, health check | `backup_folder`, `manual_backup`, `auto_backup`, `list_backups`, `restore`, `integrity_report`, `attachments_zip` |
| `atlaspo/__main__.py` | Program start | `Session`, `main` |

## Database and transactions

- **Errors** (`db.py:17-31`): `POError` text is shown to the user as written. `PermissionDenied(POError)`. `DatabaseBusy(POError, sqlite3.OperationalError)` carries `BUSY_MESSAGE`. `DataFolderUnreachable(POError, OSError)`.
- **Clock**: `db.now()` is the only clock; `timestamp()` is `now()` as `YYYY-MM-DD HH:MM:SS`. Tests monkeypatch `now` in `db`, `order_forms`, `backup` (conftest `clock`). Migrations' own audit rows use SQLite `datetime('now','localtime')` instead.
- **`connect(path=None)`** (`db.py:100`): `sqlite3.connect(..., timeout=BUSY_TIMEOUT_MS/1000, isolation_level=None)` (autocommit; transactions are explicit), `row_factory = sqlite3.Row`, `foreign_keys=ON`, `busy_timeout=15000`, `journal_mode=DELETE` (refuses with `JOURNAL_MESSAGE` if SQLite reports anything else, e.g. a WAL file held open elsewhere; `:memory:` may report `memory`), `synchronous=FULL`, `recursive_triggers=ON` (so `INSERT OR REPLACE` fires the no-delete triggers). Busy during setup becomes `DatabaseBusy`.
- **`tx(conn)`** (`db.py:141`): if `conn.in_transaction` it just yields (joins the outer transaction; no savepoint). Otherwise `BEGIN IMMEDIATE` (write lock taken first, so a number read inside is safe from other PCs), `COMMIT` on success, `ROLLBACK` on any exception including a failed commit. Busy on begin/commit becomes `DatabaseBusy`.
- **`audit(conn, user, action, entity_type, entity_id=None, *, po_id=None, of_id=None, summary="", previous=None, new=None)`** (`db.py:489`): inserts into `audit_logs` (`occurred_at, user_id, user_name, action, entity_type, entity_id, po_id, of_id, document_number, summary, previous_value, new_value`). `user=None` logs as `"system"`. `document_number` is looked up from `po_id` (PO number) or else `of_id` (OF number). Non-string `previous`/`new` are JSON-encoded (`sort_keys`, `default=str`). Call it inside the same `tx`. `audit_logs` is append-only (triggers `audit_no_update`, `audit_no_delete`).
- **Settings**: table `system_settings(key, value, updated_at, updated_by)`. `get_setting(conn, key)` falls back to `schema.DEFAULT_SETTINGS` then `""`. `set_settings(conn, user, values)` (`db.py:518`) needs `settings.edit` (checked with `user.can`, raises `PermissionDenied`), validates `approval.approver_user_ids` (each id must be an active APPROVER or ADMIN), upserts only changed keys and audits each as `APPROVAL_POLICY_CHANGE` (keys `approval.*`) or `SETTING_CHANGE`.
  Defaults (`schema.py:337`): approval on, threshold `10000000` c (P100,000), comparison `GT`, basis `OF_TOTAL`, self-approval `0`, VAT 1200 bp, default VAT mode `NONE`, `receiving.after_shipping=1`, `backup.folder=""`, `backup.keep_days=30`, company fields.
- **Data folder** `data_dir()` (`db.py:74`): `PO_DATA_DIR` if set; else, only in the frozen .exe, the first non-comment line of `data-folder.txt` beside the exe (`pointer_folder`, env vars expanded, quotes stripped); else `~/Documents/Atlas PO ERP`. A pointer folder that isn't a directory raises `DataFolderUnreachable` and is never created. Other folders are `mkdir`ed; failure raises `DataFolderUnreachable` (worded "shared" for UNC paths). `db_path()` = `data_dir()/po.sqlite`. `PO_SAMPLE_DIR` is read by `sample_data.FOLDER` (default `~/Documents/Atlas PO ERP Sample`); `sample_data.ensure()` sets `PO_DATA_DIR` to it, then builds the sample file once.
- **`open_database(path=None)`** (`db.py:469`): refuses to create an empty database in a pointer (shared) folder with no `po.sqlite`; then `connect`, `initialize`, `_optimize`. Busy anywhere becomes `DatabaseBusy`.
- **Migration runner** `initialize(conn)` (`db.py:165`):
  1. `user_version > SCHEMA_VERSION` -> "made by a newer version". Equal -> return.
  2. Version 0 with tables -> "not a PO database".
  3. Version > 0: `_backup_before_migration` copies the file to `backup.folder` or `data_dir()/backups` as `po-before-migration-v{N}-<time ms>-<COMPUTERNAME>.sqlite` (no folder writable -> refuse). Then `_refuse_clashing_tables` compares any `CREATE TABLE IF NOT EXISTS` of the pending migrations with existing tables of the same name.
  4. `_upgrade` (`db.py:247`): version 0 runs `_create` (DDL + departments + destinations + default settings, `user_version=1`, in one locked transaction; skipped if another PC already made it), then each `if version < n: _migrate(conn, MIGRATE_Vn)`.
  5. After an upgrade, `_shape_problem` compares every table's columns with a fresh in-memory database; a missing table/column restores the pre-migration copy and raises.
  - `_migrate(conn, script, extra=None)` (`db.py:434`): `foreign_keys=OFF`, `BEGIN IMMEDIATE`, re-reads `user_version` and skips if already at the script's target (from its last `PRAGMA user_version = N`), prepends `extra(conn)` (V17 and V28 add only missing columns), and if the script contains `RENAME TO` (a table rebuild) prepends `DROP TRIGGER IF EXISTS` for every V28 trigger and, on a file already at >= 28, appends `V28_TRIGGER_SQL`. Statements run one by one via `_run`/`_statements` (never `executescript`, which would commit). `PRAGMA foreign_key_check` must be empty, then `COMMIT`; `foreign_keys=ON` in `finally`.

## Schema map

Final tables (v28), grouped. `_c` centavos, `_bp` basis points, `_m` thousandths.

- **People**: `users` (username `UNIQUE COLLATE NOCASE`, scrypt hash, `active`, `must_change_password`); `user_roles` (CHECK on the 7 roles, rebuilt in V9/V18/V23); `user_branches` (receiver's one branch); `user_chips` (initials, color 0-11); `user_titles` (job title, no permission effect).
- **Masters**: `departments` (ids 1-99, fixed, undeletable); `branches` (permanent `code`, `kind` BRANCH/WAREHOUSE, undeletable; seeded 2 North, 4 South, 6 East, 8 West); `suppliers` (+`sticker_rate_c`, `term_discount_bp/_days`); `products`; `selling_prices` (hand-set prices); `forfeit_prices`.
- **Ordering**: `order_forms`, `order_form_items`, `order_form_allocations` (qty per branch), `of_approvals`, `of_amendments`, `of_number_sequences`; `purchase_orders` (one per branch per OF, `of_id`, `branch_code`), `purchase_order_items`, `po_approvals`, `po_amendments`, `po_issued_versions` (exact PDF + sha256 of each issued revision), `po_number_sequences` (per year and branch), `legacy_po_number_sequences` (frozen), `po_attachments`, `po_write_guard`; view `v_outstanding_items`.
- **Warehouse**: `warehouse_deliveries`, `warehouse_delivery_items`; `warehouse_receipts`, `warehouse_stickered` (frozen legacy since V25, never drop); `goods_arrivals` (CS/IP goods counted back).
- **Receiving**: `branch_counts` (status WAITING/APPROVED/MISMATCH/REPLACED, attempt and round), `branch_count_items`, `branch_count_damage`, `branch_count_boxes`, `branch_received`, `branch_extra_items` (lines not on the PO, referenced as minus id), `excess_returns`, `count_reopens`, `outside_receipts`.
- **Supplier documents**: `debit_memos`/`debit_memo_items`/`dm_number_sequences`; `claim_stubs`/`claim_stub_items`/`cs_number_sequences`; `item_passes`/`item_pass_items`/`ip_number_sequences`; `claim_requests`/`claim_request_items` (branch damage reports); `late_damage`.
- **Payments**: `delivery_receipts`, `dr_entries`, `dr_line_differences`, `payment_differences`, `payment_checks`, `payment_check_ofs`, `check_claims`, `check_clears`, `payment_signoffs`.
- **System**: `audit_logs` (was `po_audit_logs`, renamed in V2), `system_settings`, `sqlite_stat1` (from `PRAGMA optimize`).

**Guards** (147 triggers in a fresh file). Each raises `RAISE(ABORT, '<plain message>')`:
- *Append-only (no UPDATE, no DELETE)*: `audit_logs`, `po_approvals`, `of_approvals`, `po_issued_versions`, `branch_count_items`, `branch_count_damage`, `branch_count_boxes`, `branch_received`, `branch_extra_items`, `excess_returns`, `debit_memo_items`, `claim_stub_items`, `item_pass_items`, `claim_request_items`, `payment_check_ofs`, `outside_receipts`, `goods_arrivals`, `late_damage`, `dr_line_differences`.
- *Never deleted*: departments, branches, all `*_number_sequences`, `branch_counts`, debit memos, claim stubs, item passes, delivery receipts, payment checks, warehouse deliveries/receipts/stickered, claim requests, count reopens, payment sign-offs, attachments, decided amendments; numbered or non-draft OFs/POs.
- *Fixed once set*: numbers (`po_number_fixed`, `of_number_fixed`, `dr_number_fixed`, `check_number_fixed` also amount and supplier), sequences only go up (`*_sequence_never_backward`), branch codes, department ids, memo/stub/pass headers and PDFs (`*_fixed`, `*_pdf_fixed`), approvals on OF/PO, `pos_generated_at`, `payment_status` (always `UNPAID`), check date/credit/record/cancel fields, `expected_c`, `paid_back_check_id`.
- *Lock (bypassed only while a row exists in `po_write_guard`)*: `po_terms_locked`, `of_terms_locked`, `items_locked_*`, `of_items_locked_*`, `of_alloc_locked_*`, `po_parent_fixed`, `po_pieces_locked`, `of_pieces_locked`, `po_status_path`, `of_status_path`. Use `orders.write_guard(conn)` inside a `tx` (`orders.py:71`; it refuses outside a transaction and deletes its token in `finally`).
- *Status paths*: `po_status_path`/`of_status_path` (V28), `branch_count_status_path` (V21), `debit_memo_stays_cancelled`, `claim_stub_finished_stays`/`item_pass_finished_stays`, `*_not_back`, `check_stays_cancelled`, `claim_request_decided_stays`, `payment_signoffs_signed_stays`, `warehouse_shipped_stays`, `count_reopens_used_once`.
- *No new lines after finish* (V28): `debit_memo_items_after_issue`, `claim_stub_items_after_approval`, `item_pass_items_after_print`, `branch_count_*_after_agreed`, `claim_request_items_after_decision/_once_sent`, `check_orders_not_cancelled/_once_recorded/_paid_once`, `warehouse_delivery_items_shipped_*`.
- *Others*: `po_requires_approved_of` (POs only from an APPROVED OF).

**Migration history**

| V | What changed |
|---|---|
| 1 | `DDL`: PO-first design, departments, users (4 roles), suppliers, destinations, products, POs, approvals, audit. |
| 2 | OF becomes master: `branches` from destinations, `order_forms`+items+allocations+approvals+amendments, POs rebuilt per branch, per-branch PO numbering, `po_issued_versions`, `po_audit_logs` -> `audit_logs` (+`of_id`, `document_number`). |
| 3 | Deletes unused inactive old branches 1, 3, 5. |
| 4 | Standard supplier codes/terms; `approval.approver_user_id` -> list `approval.approver_user_ids`. |
| 5 | `suppliers.sticker_rate_c`; catalog units mapped to offered units. |
| 6 | Default VAT mode `NONE`. |
| 7 | Warehouse renamed Central Warehouse. |
| 8 | `selling_prices`. |
| 9 | RECEIVER role, `user_branches`, `branch_counts`, `branch_count_items`. |
| 10 | `branch_count_damage`. |
| 11 | Two blind counts: `branch_counts` rebuilt (attempt/round), `branch_received`. |
| 12 | Debit memos. |
| 13 | `branch_extra_items`, `excess_returns`. |
| 14 | Claim stubs. |
| 15 | `user_chips`. |
| 16 | `delivery_receipts`. |
| 17 | Payments: `dr_entries`, `payment_differences`, `payment_checks`, `payment_check_ofs`; supplier term-discount columns (via `SUPPLIER_TERM_COLUMNS`). |
| 18 | ACCOUNTING role, `check_claims`. |
| 19 | `check_clears`. |
| 20 | Item passes. |
| 21 | Status/permanence guards from the 6 Nov audit; audit-log indexes. |
| 22 | `outside_receipts`, `count_reopens`. |
| 23 | WAREHOUSE role, `payment_signoffs`, `forfeit_prices`. |
| 24 | `warehouse_receipts`, `warehouse_stickered` (now legacy). |
| 25 | `warehouse_deliveries`(+items), `branch_count_boxes`, `goods_arrivals`, `user_titles`. |
| 26 | `late_damage`. |
| 27 | `claim_requests`, `claim_request_items`. |
| 28 | Columns `payment_checks.expected_c`, `payment_differences.dr_lower_c`, `debit_memos.paid_back_check_id` (backfilled), `dr_line_differences`, ~40 guards (status paths, no lines after finish), index cleanup. |
| 29 | Inventory: `stock_movements`, `stock_counts`/`_lines`/`_entries`, `stock_adjustments`/`_lines`, `stock_transfers`/`_lines`, `stock_number_sequences`, setting `inventory.live_since`. See [inventory.md](inventory.md). |
| 30 | Sales: `stock_sales`, `stock_sale_lines`, `sale_number_sequences`; `reorder_levels`. See [inventory.md](inventory.md). |
| 31 | `sale_holds` (unusual sales wait for approval); lock triggers on decided inventory papers. See [inventory.md](inventory.md). |

**Adding a migration (V29)**
1. In `schema.py`, add `MIGRATE_V29 = """..."""` ending with an `INSERT INTO audit_logs(... 'MIGRATE_V29' ...)` row and `PRAGMA user_version = 29;` (the runner reads the target from that line; without it the script runs every time).
2. Use `CREATE TABLE IF NOT EXISTS name (` with the closing `);` at the start of a line (the clash check's regex needs that shape), and `CREATE TRIGGER/INDEX IF NOT EXISTS`. New columns: SQLite has no `ADD COLUMN IF NOT EXISTS`, so put them in a dict like `V28_COLUMNS` and pass `lambda c: _missing_columns(c, ...)` as `extra`.
3. Bump `SCHEMA_VERSION = 29` and add `if version < 29: _migrate(conn, schema.MIGRATE_V29)` to `db._upgrade`.
4. A table rebuild (`RENAME TO`) works with foreign keys off, but triggers that read the rebuilt table must be dropped and recreated; the runner only does this automatically for V28's guards.
5. Never edit an old migration; old files run them. Add tests that upgrade a file from the previous version (see `test_rules_schema_multipc.py::upgraded`). New guards appear automatically in `integrity_report`'s missing-trigger check.

## Roles and permissions

`ROLES` (`auth.py:14`): ADMIN Administrator, BUYER Purchaser, APPROVER Approver, VIEWER View only, RECEIVER Receiver, ACCOUNTING Accounting, WAREHOUSE Warehouse.
`PERMISSIONS` (`auth.py:16`), X = granted:

| Permission | ADMIN | BUYER | APPROVER | ACCOUNTING | WAREHOUSE | RECEIVER | VIEWER |
|---|---|---|---|---|---|---|---|
| of.edit, of.cancel, of.amend, of.generate | X | X | | | | | |
| of.approve, po.approve | X | | X | | | | |
| po.edit, po.issue, po.cancel, po.amend, po.close | X | X | | | | | |
| master.edit, price.edit, dm.issue | X | X | | | | | |
| branch.manage, supplier.edit, settings.edit, users.edit, backup.manage, po.receive_outside | X | | | | | | |
| receiving.count | | | | | | X | |
| receiving.review | X | X | X | X | X | | X |
| warehouse.receive | | | | | X | | |
| goods.release | X | X | | | X | | |
| dm.cancel (also CS/IP cancel), receiving.reopen, payment.signoff | X | | X | | | | |
| payment.edit | X | | | X | | | |
| payment.view | X | | X | X | | | X |
| cs.approve_request | X | | X | X | | | |

- `User` (frozen dataclass, `auth.py:90`): `id, username, full_name, roles: frozenset, must_change_password, branch_code, title`; `.display`, `.can(perm)` (a KeyError for an unknown permission name).
- `require(user, perm)` raises `PermissionDenied("<name> does not have permission for this (<perm>).")`. Call it first in every service, before `tx`.
- **Role rules** (`_insert_user`, `update_user`): at least one known role; RECEIVER alone and WAREHOUSE alone (`_check_receiver_alone`); BUYER+ACCOUNTING refused (`role_conflict`; APPROVER combinations are deliberately left allowed; turning a conflicting login off is always allowed). A RECEIVER needs one existing branch; nobody else gets one (`_set_branch`). The last active ADMIN can't be demoted or turned off.
- **Passwords**: scrypt N=2^14, r=8, p=1, 16-byte salt, stored `scrypt$N$r$p$salt$key`; `MIN_PASSWORD = 8`. `create_user` defaults `must_change=True`; `reset_password` sets `must_change_password=1`; `change_own_password` needs the current one and a different new one.
- **Usernames**: unique by Unicode `casefold` + NFC (`_name_key`), max 200 chars (also full name).
- **`login`** (`auth.py:400`): looks up `username.strip()` (column is `COLLATE NOCASE`), always runs scrypt (a dummy hash for unknown names), and every refusal is the same `LOGIN_REFUSED` text; it logs `LOGIN_FAILED` (with "Turned-off account" or "Wrong username or password") or `LOGIN`.
- `create_first_admin` (empty database only) gives ADMIN+APPROVER and sets `approval.approver_user_ids` to that user.
- **Separation of duties elsewhere**: preparer != approver lives in `order_forms._check_approver` and `orders.py:833` (`approval.allow_self_approval`, approver must be on `approver_user_ids`, and anyone who created/edited/submitted the OF can't approve it). Receivers seeing only their branch is enforced in the receiving services via `user.branch_code`.

## Money

`money.py`, all `Decimal`, half-up rounding (`ROUND_HALF_UP`) unless noted. Units: centavos (int), basis points (350 = 3.50%), thousandths `_m` (quantities, FX cost and rate).
- `round_centavos(Decimal) -> int`: half up to a whole number (input already in centavos).
- `parse_pesos(text)`: strips `,`, `₱`, `PHP`; blank/None -> 0; floats via `repr`; -> centavos; `ValueError` on junk. Negative amounts are accepted.
- `parse_percent(text)` -> bp; `parse_milli(text)` -> thousandths (half up); `parse_qty(text, uom)` -> thousandths, exact (more than 3 decimals, negatives, non-finite, or decimals on a whole-number unit raise).
- `parse_pieces(text, uom)`: pieces typed -> units in thousandths (`qty_m // pieces_per_unit`); refuses non-multiples of the pack size with a suggestion.
- `fmt_plain(c)` `1,234.50`; `fmt_peso(c)` `₱1,234.50` / `-₱...`; `fmt_percent(bp)` and `fmt_milli(m)` give `""` for 0/None; `fmt_qty(m)` whole or decimal.
- `UNITS`/`OFFERED_UNITS`, `pieces_per_unit(uom)`, `allows_decimal(uom)` (M, YD, KG only); `m_to_qty`, `qty_to_m` (half up).
- `per_piece(unit_cost_c, pieces)`; `sticker_deduction(pieces, rate_c)`; `fx_to_centavos(fx_cost_m, fx_rate_m)` = cost x rate x 100, half up.
- `selling_from_cost(cost_c)`: `ceil(cost / 0.70)` rounded **up** to a multiple of P10 (1000 c), integer arithmetic; `None` for cost <= 0.
- `discount_factor(d1, d2)` = (1-d1)(1-d2), compound. `compute_line(qty, unit_cost, d1, d2) -> LineAmounts(gross, discount, total, net_unit)`; qty > 0, cost >= 0, discounts 0-10000.
- `compute_totals(lines, overall_type, overall_value, vat_mode, vat_rate_bp=1200, other_charges=0) -> POTotals`: item discounts per line; overall PERCENT (bp) or AMOUNT (c, <= merchandise); VAT INCLUSIVE extracts VAT without adding, EXCLUSIVE adds, NONE ignores; other charges added last, no VAT. Per-department totals.
- `split_by_weight(total, weights)`: largest remainder, ties to the lower key, always sums to `total`; zero weights give all to the smallest key.
- `compute_order(lines with allocations, ...) -> OrderTotals`: overall discount and charges split across branches by merchandise value, then each branch is a full `compute_totals` (VAT per branch); OF totals are sums of branch figures, so OF = sum of POs to the centavo.
- `requires_approval(total, enabled, threshold, comparison)`: `GT` strict, `GTE` inclusive.

## Backup and restore

- **Folder**: `backup_folder(conn)` = setting `backup.folder` or `data_dir()/backups` (created).
- **Copy** `_copy` (`backup.py:98`): SQLite backup API into `<target>.<uuid>.partial`, in 256-page steps with 20 ms rests so other PCs can save; after 5 restarts the rest is copied in one go (`_copy_in_steps`). The copy must pass `PRAGMA integrity_check` + `foreign_key_check` (`_integrity`) before `os.replace` onto the target. An existing backup is never overwritten except today's auto copy. Then `_save_attachments` zips `data_dir()/attachments` to `<stem>-attachments.zip` beside it (keeps files already in the zip).
- **When**: `auto_backup(conn, at_start)` writes `po-auto-YYYY-MM-DD.sqlite` (one per day, replaced) at start-up (`__main__`, skipped if written in the last 600 s by another PC), at logout (`MainWindow._logout`) and at window close (`MainWindow.closeEvent`). It sweeps `*.partial`/`*.restoring-*` leftovers older than 1 h, and deletes auto copies (and their zips) older than `backup.keep_days`. `manual_backup(conn, user)` needs `backup.manage`, names `po-manual-<time ms>.sqlite` (`_unique`, claims the name with `O_EXCL`), audits `BACKUP`; manual copies are never pruned. Migrations make `po-before-migration-v{N}-...` copies (db.py).
- **`restore(conn, user, source, live_path)`** (`backup.py:476`), needs `backup.manage`: refuses a missing file, the live file itself, a file failing `_integrity`, a sample-data file (`sample.data=1`) into a real database, a file without `purchase_orders` or with `user_version` outside 1..current. Then: copies the source to `<live>.restoring-<uuid>.sqlite`, upgrades it with `db.initialize`, writes a `RESTORE` audit row into that copy, re-checks it; takes `BEGIN EXCLUSIVE` (2 s wait, else `BUSY`), makes a safety copy `po-before-restore-...` and retries up to 3 times if `PRAGMA data_version` shows another PC saved meanwhile; swaps the copy into the live file through the backup API (journalled: all or nothing). Afterwards zips current attachments beside the safety copy, puts back attachments from the source zip that are missing (never overwrites, skips path-like names), closes `conn` and returns the safety path. On failure nothing changes and `conn` stays open. The UI then restarts the session (`Session.restart_after_restore`).
- **`integrity_report(conn)`** (`backup.py:328`, Settings health check): integrity/foreign-key checks, a leftover `po_write_guard` row, guards missing compared to a fresh database, every PO total recomputed with `orders._totals`, PO/OF/DM/CS/IP counters behind their highest number, OF item totals vs allocations, OF total vs its live POs, document PDFs empty or sha256 changed, DM/CS totals vs lines, issued PO PDFs changed, orphan PO-line references (`_PO_LINE_REFS`), attachments missing/changed/unowned.

## Startup

`main()` (`__main__.py:99`): installs `common.excepthook`, creates `QApplication`, applies the saved theme, then `Session.start()`:
`db.open_database()` and `PRAGMA quick_check` (a `POError` or busy error is shown plainly; any other error is logged to `errors.log` and shown); quick-check failures show a "restore a backup" warning but continue. No users -> `FirstRunDialog` (creates the first admin), else `LoginDialog`. `must_change_password` forces `ChangePasswordDialog`. Then `backup.auto_backup(at_start=True)` (a `POError` is a warning only) and `MainWindow`. Logout and restore restart the session without quitting. Launchers: `Atlas PO ERP.pyw` calls `main()`; the sample launcher calls `sample_data.ensure()` first.

## Tests

Run from `apps/desktop`: `python -E -m pytest tests/test_x.py -q -p no:cacheprovider` (needs pytest and PySide6 installed). Do not run the full suite for a small change.
- `test_money.py` (money helpers), `test_rules_money.py` (payment money rules built on them).
- `test_fix_schema.py` (guards, V28), `test_rules_schema_multipc.py` (V27 -> V28 upgrade, two PCs upgrading at once, busy messages, journal mode, `data-folder.txt`, stepwise backup, start-up copy freshness).
- `test_rules_logins.py` (Buying+Accounting conflict), `test_fix_masters_auth.py` (login and input limits), `test_fix_backup.py` (restore safety, health check), plus `integrity_report` assertions across `test_acceptance.py` and others.
- `conftest.py`: `env` sets `PO_DATA_DIR` to `tmp_path`; `conn` = `db.open_database()` there; `world` = `World(conn)`: first admin `boss` (ADMIN+APPROVER, on the approver list), buyer `buyer`, warehouse login `nestor`, all with password `correct-horse`; supplier `ABC`; branches are the migration seeds `NORTH, SOUTH, EAST, WEST = 2, 4, 6, 8`; `receiving.after_shipping` is set to `0`. Helpers `draft`, `approved`, `po`. `clock(*datetime_args)` freezes `now`. Autouse fixtures redirect QSettings to a temp file and delete leftover windows.

## Gotchas

- `tx()` nesting joins the outer transaction; there are no savepoints. Catching a `POError` inside an outer `tx` keeps the partial writes of the failed inner step.
- Never switch to WAL, never use `executescript` inside a transaction, and take the lock (`tx`) before reading any counter you will write back.
- `connect()` is autocommit: a bare `conn.execute("INSERT ...")` outside `tx` commits on its own and skips the audit pairing.
- Locked rows need `orders.write_guard` inside `tx`. A guard row that leaks (committed outside a transaction) unlocks every document; `integrity_report` flags it.
- A new permission must be added to `PERMISSIONS`; `User.can` raises `KeyError` on an unknown name. Never widen a permission to make a test pass; Admin deliberately can't `receiving.count` or `warehouse.receive`.
- New roles need a migration that rebuilds `user_roles` (its CHECK lists roles) plus the `ROLES` map.
- A new trigger that reads a table a later migration rebuilds will block that rebuild; the runner only handles V28's guards (`V28_TRIGGERS`).
- `_shape_problem` builds a fresh in-memory database once per process (`_fresh_columns`), and `backup._fresh_triggers` likewise; both are cached by `SCHEMA_VERSION`.
- Restoring an older backup runs migrations on the temporary copy, which writes a `po-before-migration-*` file into the folder named by the *backup's* `backup.folder` setting (or this PC's `data_dir()/backups`).
- Migration safety copies are not integrity-checked and don't include attachments, unlike `_copy`.
- `login` matches with SQLite `NOCASE` (ASCII-only folding) while uniqueness uses Unicode `casefold`, so a non-ASCII username typed in another case is refused.
- `auto_backup` reads `backup.keep_days` with `int()`; a non-numeric value raises `ValueError`, which start-up doesn't catch (it catches only `POError`).
- The `selling_from_cost` docstring's examples (39.00 -> 40.00) describe the price after the margin, not the cost: a cost of P39.00 gives P60.00.
