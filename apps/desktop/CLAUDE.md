# Desktop app (`apps/desktop`)

PySide6 desktop program over one SQLite file. It is complete, it is in production, and it is the specification for every business rule. Python 3.14 is the target; 3.12 works for tests.

**For business behaviour** (screens, rules, roles, backups), go to the matching heading in [README.md](README.md) and read just that section: `## Rules the system enforces`, `## Roles`, `## Daily work`. Don't read the whole file.

## Layout

```
atlaspo/            services: pure business logic, no Qt
  db.py             connect, tx(), audit(), settings, data folder, migration runner
  schema.py         every table, trigger and migration (MIGRATE_V2 … MIGRATE_V30), plus SCHEMA_VERSION
  auth.py           users, ROLES, PERMISSIONS map, require(user, perm)
  money.py          all arithmetic: Decimal in, whole centavos out
  order_forms.py    OF lifecycle: draft → approval → numbering → PO generation, amendments
  orders.py         PO lifecycle after generation: issue, versions, amend, cancel, close, write_guard
  warehouse.py      Central Warehouse deliveries and shipping to branches
  receiving.py      blind double count, comparing counts, recounts
  debit_memos.py · claim_stubs.py · item_passes.py   shortages · damaged goods · excess goods
  delivery_receipts.py · payments.py · selling.py    DRs · checks and cash flow · selling prices
  masters.py        suppliers, branches, products
  inventory.py      stock on hand (read from purchasing records), stock counts, adjustments, transfers
  stock_sales.py · reorder.py   sales and customer returns, POS import, sales reports · reorder levels and draft OFs
  pdf.py · reports.py · backup.py · sample_data.py
  ui/               screens only; they call services and never touch SQL rules
tests/              pytest, about 1,090 tests; conftest.py has the World fixture (a small business)
```

To find something, grep for the function name. Each module's first lines are a docstring that states its rules.

## Module docs

Before changing a feature, read its doc in [docs/](docs/). Each one covers files, data model, rules with where they are enforced, permissions, flows, screens, tests and gotchas.

| Feature | Doc | Code |
|---|---|---|
| Order Forms: draft, approval, numbering, PO generation, amendments | [docs/order-forms.md](docs/order-forms.md) | `order_forms.py`, `of_layout.py`, `ui/of_page.py` |
| Purchase Orders and DRs: issue, print, amend, cancel, close | [docs/purchase-orders.md](docs/purchase-orders.md) | `orders.py`, `delivery_receipts.py`, `ui/po_page.py` |
| Central Warehouse: deliveries and shipping | [docs/warehouse.md](docs/warehouse.md) | `warehouse.py`, `ui/warehouse_page.py` |
| Branch receiving: blind double count, recounts, damage | [docs/receiving.md](docs/receiving.md) | `receiving.py`, `ui/receiving_page.py` |
| Differences: debit memos, claim stubs, item passes | [docs/differences-dm-cs-ip.md](docs/differences-dm-cs-ip.md) | `debit_memos.py`, `claim_stubs.py`, `item_passes.py`, their pages |
| Payments, checks, cash flow, selling prices | [docs/payments.md](docs/payments.md) | `payments.py`, `selling.py`, `ui/payments_page.py`, `ui/selling_page.py` |
| Platform: database, schema and migrations, roles and permissions, money, backup | [docs/platform.md](docs/platform.md) | `db.py`, `schema.py`, `auth.py`, `money.py`, `backup.py` |
| Master data, printed papers, reports, sample data | [docs/masters-pdf-reports.md](docs/masters-pdf-reports.md) | `masters.py`, `pdf.py`, `reports.py`, `sample_data.py` |
| Inventory: stock on hand, counts, adjustments, transfers, sales, reorder, the Purchasing / Inventory switch | [docs/inventory.md](docs/inventory.md) | `inventory.py`, `stock_sales.py`, `reorder.py`, `ui/inventory_page.py`, `ui/sales_page.py` |
| UI shell: main window, navigation, shared widgets, theme, settings | [docs/ui-shell.md](docs/ui-shell.md) | `ui/main_window.py`, `ui/common.py`, `ui/pages.py`, `ui/theme.py`, `ui/settings_page.py` |

When you change a feature, update its doc in the same branch. The `/update-context` skill does this before every push or PR.

## How a service is written

```python
def issue(conn, user: User, po_id: int) -> None:
    auth.require(user, "po.issue")          # permission first; raises PermissionDenied
    with db.tx(conn):                       # BEGIN IMMEDIATE: one write transaction
        ...                                 # rules; raise POError("message for the person")
        db.audit(conn, user, "issue", "purchase_order", po_id)
```

- `POError` text is shown to the user as written, so phrase it plainly in purchasing words.
- Changing a locked (past-draft) OF or PO requires `with orders.write_guard(conn):` inside the transaction. Database triggers reject the change otherwise. That is deliberate, not a bug to work around.
- The database enforces rules too: append-only tables, permanent numbers, one PO per branch per OF. Check `schema.py` before assuming a write will be allowed.
- Schema change: add `MIGRATE_V{n+1}` in `schema.py` and bump `SCHEMA_VERSION`. Every database, new or old, reaches the current version through the same migrations.
- Multi-PC: the file may sit on a network share. Keep rollback journal mode (never WAL), and take the write lock before reading a number you will use.

## Commands (run in `apps/desktop`)

- One test file, which is the normal way: `python -E -m pytest tests/test_<area>.py -q -p no:cacheprovider`
- The whole suite takes 12 to 25 minutes, so run it only for a finished change. Run it in the background.
- Sample program: `python -E "Atlas PO ERP (Sample Data).pyw"` (login `owner` / `123`). Real program: `python -E "Atlas PO ERP.pyw"`.
- Build the exe: `Build EXE.cmd`.

## Gotchas

- Always use `python -E`, so stray `PYTHON*` environment variables are ignored.
- Scripts and experiments must set `PO_DATA_DIR` and `PO_SAMPLE_DIR` to temporary folders **before** importing `atlaspo`. Never open `Documents\Atlas PO ERP`, which is real data.
- `pypdf` is pinned below 7. Version 7 breaks `pdf.stamp_copy` (reprint stamping).
- Two screen-width tests, `test_fix3::…_at_1366` and `test_fix_dm_ip::…debit_memo_date…`, fail on some machines because of fonts and DPI. They fail on the original code too, so they don't mean your change broke something.
- Screen tests need a `QApplication`. Never open a dialog that waits for a click.
