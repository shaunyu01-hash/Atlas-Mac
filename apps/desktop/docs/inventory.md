# Inventory (phase 1)

Stock on hand at every branch and at the Central Warehouse, with a stock card per product, blind double stock counts, approved adjustments and transfers between places. It is a second system inside the same program: the **Purchasing | Inventory** switch at the top of the sidebar swaps the whole sidebar and screen. The service docstring (`inventory.py:1-25`) is the authoritative summary.

**The key design choice:** purchasing already records every piece that moves, in permanent tables. Inventory reads that stock straight from those records (`inventory.MOVES`) and never copies it. Purchasing code has no inventory hooks, so the two can never disagree. Inventory writes only `stock_movements`, from its own three papers.

## Files

| File | Role | Key public API |
|---|---|---|
| `atlaspo/inventory.py` | Service, no Qt | `scope`, `locations`, `place_name`, `unit`, `value_c`, `book`, `on_hand`, `valuation`, `stock_card`, `movement_summary`, `not_in_catalog`, `set_live_since`; counts `open_count`, `counts`, `count_sheet`, `save_count`, `my_figures`, `count_results`, `moved_since`, `post_count`, `cancel_count`; adjustments `prepare_adjustment`, `decide_adjustment`, `adjustments`, `adjustment_lines`, `adjustments_waiting`; transfers `send_transfer`, `receive_transfer`, `cancel_transfer`, `transfers`, `transfer_lines`; `products_for`, `find_product` |
| `atlaspo/ui/inventory_page.py` | Screens | `StockPage`, `CountsPage` (+ `StartCountDialog`, `CountSheetDialog`), `AdjustmentsPage` (+ `AdjustmentDialog`), `TransfersPage` (+ `SendTransferDialog`, `ReceiveTransferDialog`), `InventoryReportsPage`, `ProductLines` |
| `atlaspo/ui/main_window.py` | The switch | `show_system`, `_inventory_layout`, `_inv_navigate`, `_INVENTORY_PAGES` |
| `atlaspo/schema.py` | `MIGRATE_V29` | |

## Data model

- **Locations:** `0` is the Central Warehouse (it is not a row in `branches`); any other code is a branch code. `_check_place` validates it.
- **Item:** `products.id` (one SKU, size and color). Stock is kept in the product's **base unit**: pieces when `pieces_per_unit > 1`, else the product's own UOM, so KG or M keep fractions. Quantities are thousandths (`_m`).
- **Buckets:** `GOOD` (for sale), `DAMAGED` (at a branch, waiting for a claim stub or write-off), `HELD` (at the warehouse, waiting for the supplier). **In transit** is not a bucket: `_in_transit` works it out from item passes still `PRINTED` (heading to the warehouse) and transfers still `SENT` (heading to `to_code`).
- **Tables (V29)**, all with no-delete triggers:
  - `stock_movements`: append-only, `kind` in COUNT, ADJUSTMENT, TRANSFER_OUT, TRANSFER_IN, TRANSFER_BACK; `UNIQUE(kind, ref_id, location_code, product_id, bucket)`.
  - `stock_counts` (COUNTING → AGREED → POSTED; COUNTING or AGREED → CANCELLED, enforced by `stock_count_status_path`); `stock_count_lines` (`book_m` snapshot at start; `agreed_m` set once); `stock_count_entries` (each person's figure per round; append-only; only while COUNTING).
  - `stock_adjustments` (PENDING → APPROVED or REJECTED; `CHECK decided_by <> prepared_by`) and `stock_adjustment_lines` (only while PENDING).
  - `stock_transfers` (SENT → RECEIVED or CANCELLED; the receiver must differ from the sender) and `stock_transfer_lines` (`received_m` set once; no lines after the TRANSFER_OUT movement exists).
  - `stock_number_sequences(kind SC/SA/ST, year)`: numbers `YYYY-SC-00001` and so on, never backward.
- **Setting `inventory.live_since`** (set by V29 to the migration day; sample data sets `''` for "everything"): POs whose **first** agreed receipt is before it are left off the books, with everything that follows from them (CS, IP, returns). The first stock count at each place gives its opening stock. Admin changes it from Stock on hand (`set_live_since`, `settings.edit`).

## Movements read from purchasing (`MOVES`)

| kind | When | Effect |
|---|---|---|
| RECEIVED | `branch_received` row in force (`receiving._IN_FORCE`), `accepted_at` | branch GOOD + (pieces − damaged), DAMAGED + damaged |
| DAMAGE_FOUND | late-damage claim stub approved (unless cancelled before arriving) | branch GOOD −, DAMAGED + |
| CS_SENT / CS_ARRIVED | claim stub `arrived_at` | branch DAMAGED − paper pieces; warehouse HELD + counted pieces (`goods_arrivals`, else paper) |
| IP_SENT | item pass printed (not cancelled), `printed_at` | branch GOOD − |
| IP_ARRIVED | item pass `arrived_at` | warehouse HELD + counted |
| COLLECTED / FORFEITED | CS or IP status, `closed_at` | warehouse HELD −; FORFEITED also GOOD + (the goods are sold from the warehouse, or transferred) |
| RETURNED | legacy `excess_returns` | branch GOOD − |
| WRITTEN_OFF | old ISSUED debit memo lines with reason DAMAGED | branch DAMAGED − |

A recount that replaces an accepted count replaces its stock too, because only counts in force are read. Purchased lines whose product matches no catalog product (by `product_id`, then SKU) have `product_id NULL`. They are kept off the books and listed by `not_in_catalog`.

## Rules enforced

Permissions (`auth.PERMISSIONS`):

| Permission | Roles | Meaning |
|---|---|---|
| `inventory.view` | ADMIN, BUYER, APPROVER, VIEWER, ACCOUNTING | Books, costs, stock card, reports |
| `count.manage` | ADMIN, BUYER, APPROVER | Start and cancel counts; read results |
| `stock.count` | RECEIVER, WAREHOUSE | Count blind |
| `stock.post` | ADMIN, APPROVER | Post counts, approve or reject adjustments, cancel transfers |
| `adjust.prepare` | ADMIN, BUYER, RECEIVER, WAREHOUSE | Prepare adjustments |
| `transfer.send` | ADMIN, BUYER, RECEIVER, WAREHOUSE | Send stock |
| `transfer.receive` | ADMIN, RECEIVER, WAREHOUSE | Count in a transfer |

1. **Place scope** (`scope`): a receiver works at their branch only; a WAREHOUSE-only login works at location 0 only; everyone else works everywhere. Refusals read "That isn't your branch."
2. **Receivers and the warehouse never see the books or costs.** `count_sheet` returns products only; `my_figures` returns only the caller's figures; `transfer_lines` hides `sent_m` from the receiving end while SENT; `adjustment_lines` and `count_results` drop values without `inventory.view`.
3. **One open count per place.** `open_count` lists every product with non-zero GOOD book stock (in the department, if one is chosen) and snapshots `book_m`.
4. **Blind double count.** One save per person per round; the second person's save compares the two, line by line. Agreed lines are fixed (`agreed_m`). The rest go to the next round, which lists only those. A product found by one counter (`found`, by SKU, must be in the catalog and the department) joins the count. Later counters must give it a figure. When no lines are left, the count is AGREED. Every listed product needs a figure (0 allowed). An empty save is refused.
5. **Posting** (`stock.post`, never a counter) writes COUNT movements of `agreed − book_m` and sets POSTED. The count assumes no stock moved at the place since it started. `moved_since` counts movements that did, and the screen warns. Negative COUNT lines are what was sold or went missing: there is no sales capture in phase 1.
6. **Adjustments:** the reason sets the direction. WRITE_OFF (DAMAGED by default), INTERNAL_USE and RETURNED remove stock; FOUND adds GOOD; DAMAGED moves GOOD to DAMAGED; OTHER takes a signed figure and needs a note. The preparer never approves (service plus CHECK). A rejection needs a reason.
7. **Transfers:** GOOD only. The goods leave the sender's books when sent (TRANSFER_OUT) and reach the receiver's books when counted in (TRANSFER_IN, received figure). The difference is a loss on the way, noted in the audit summary. Management cancels a SENT transfer (TRANSFER_BACK).
8. **Stock may go below zero.** It is flagged (`below_zero`, the "Below zero only" filter), never blocked.
9. **Value** is quantity × `products.last_cost_c / pieces_per_unit`, rounded half up per line. Products with no cost show "No cost".

Audit actions: `STOCK_COUNT_OPEN`, `STOCK_COUNT`, `STOCK_COUNT_MISMATCH`, `STOCK_COUNT_MATCH`, `STOCK_COUNT_POST`, `STOCK_COUNT_CANCEL`, `STOCK_ADJUST_PREPARE`, `STOCK_ADJUST_APPROVE`, `STOCK_ADJUST_REJECT`, `STOCK_TRANSFER_SEND`, `STOCK_TRANSFER_RECEIVE`, `STOCK_TRANSFER_CANCEL`, and `SETTING_CHANGE` for the start date.

## Screens

- **Sidebar switch** (`MainWindow.show_system`): the switch shows only for people with some inventory permission (`_inventory_layout`). Switching away from an unsaved OF or PO asks first. The Adjustments entry shows the count waiting for `stock.post` holders.
- **Stock on hand** (`inventory.view`): filters for place, department, search and below zero; totals; a stock card for the selected row (newest first, balances after each movement); "Change start date" for Admin.
- **Stock counts:** tabs by status. Managers see books at start, agreed figures, the difference and its value, and each person's figures per round, with Start, Post and Cancel. Counters see a Count button (blind sheet plus "Found, not on the list") and their own figures.
- **Adjustments:** New adjustment (place, reason, stock bucket, product lines); Approve and Reject for someone other than the preparer.
- **Transfers:** Send stock; "Goods arrived: count them" (blind); Cancel transfer.
- **Inventory reports:** stock value per place; movement totals between dates (negative stock count lines are sold or missing); lines not in the catalog.

## Tests

`tests/test_inventory.py` (21 tests: purchasing-derived stock, blind counts, adjustments, transfers, permissions, permanence, the switch and screens). From `apps/desktop`:

```
python -E -m pytest tests/test_inventory.py -q -p no:cacheprovider
```

## Gotchas

- **Never add inventory writes to purchasing services.** Purchasing stock is derived. If a purchasing flow changes what it records, update `MOVES` and its test instead.
- `MOVES` is built in Python as a CTE, not as a database view, so a later table-rebuild migration can't trip over a view that reads the table.
- Location 0 is not a branch row. Don't join `stock_*.location_code` to `branches` without handling 0 (`place_name` does).
- The count's `book_m` is a snapshot at start. Receiving goods at a place while it is being counted makes the posted difference wrong. The screen warns (`moved_since`), but nothing blocks it.
- Not yet built (later phases): sales or POS capture, reorder points, costing methods other than last cost, serial or lot tracking, unit conversions.

# Phase 2: sales, customer returns, reorder

Sales take stock off the books the day they're recorded, so the books are right between counts and a count's difference is real loss. Reorder levels turn low stock into draft Order Forms, which closes the loop back to purchasing.

## Files

| File | Role | Key public API |
|---|---|---|
| `atlaspo/stock_sales.py` | Sales and returns, POS files, sales reports | `record`, `read_file`, `import_file`, `cancel`, `papers`, `paper_lines`, `list_price_c`, `sales_report`, `daily_sales`, `movers`, `shrinkage`, `value_by_department` |
| `atlaspo/reorder.py` | Levels and suggested orders | `levels`, `set_levels`, `on_order`, `make_orders` |
| `atlaspo/ui/sales_page.py` | Screens | `SalesPage` (+ `RecordSalesDialog`, `ImportSalesDialog`), `ReorderPage` (+ `LevelsDialog`, `ReorderSettingsDialog`), `SalesReportsWidget` (its tabs are moved into Inventory reports) |

## Data model (V30)

- `stock_sales`: one paper per place and day. `kind` is SALE or RETURN; `source` is ENTRY or IMPORT (with `file_name` and `file_sha256`); `status` is POSTED → CANCELLED. Header fields are fixed; cancelled stays cancelled; no delete. Numbers come from `sale_number_sequences(kind, year)`: `YYYY-SL-00001` for sales and `YYYY-RT-00001` for returns.
- `stock_sale_lines`: `qty_m` in the stock unit; `price_c` (one unit) and `amount_c`; `cost_c` (piece cost then, may be NULL); `bucket` (sales GOOD; returns GOOD or DAMAGED). Append-only, and only while the paper is POSTED.
- `reorder_levels(location_code, product_id, min_m, target_m, set_by, set_at)`: hand-set levels. Upsert or delete (back to the program's levels); every change is audited (`REORDER_LEVELS`).
- Settings (in `DEFAULT_SETTINGS`): `inventory.sales_days` 60, `inventory.lead_days` 14, `inventory.cover_days` 14, `inventory.dead_days` 90.
- `MOVES` gains SOLD (−qty GOOD) and CUSTOMER_RETURN (+qty into the chosen bucket) from POSTED papers. Their time is `MIN(sale_date 23:59:59, entered_at)`. `KINDS["COUNT"]` now reads "Stock count difference".

## Rules

| Permission | Roles |
|---|---|
| `sales.record` | ADMIN, BUYER, RECEIVER, WAREHOUSE (own place only, via `inventory.scope`) |
| `reorder.edit` | ADMIN, BUYER |
| Cancel sales | `stock.post` (ADMIN, APPROVER), with a reason |
| Sales reports, Reorder page | `inventory.view` |
| Make draft OFs | `of.edit` and `inventory.view` |

1. **Price:** an amount typed (or from the file) sets the unit price; else a typed unit price; else the catalog price, `products.selling_c / pieces_per_unit`. The cost is a snapshot of `last_cost_c / pieces_per_unit`, so profit never changes later.
2. **Dates** (`_check_date`): no future days, and nothing dated before the day a non-cancelled stock count started at that place, because the count already took those sales in. The count's own day is allowed.
3. **POS files** (`read_file`): CSV (separator sniffed; UTF-8 or cp1252), with headings on line 1. It needs a SKU-like and a quantity-like column (`_SKU`, `_QTY`); amount or price columns are optional. Lines for the same SKU are added together. SKUs not in the catalog are listed (`unmatched`) and left out. The same file (sha256) can't be imported twice for a place. Files larger than 20 MB are refused.
4. **Branch staff** never see cost or profit (`paper_lines` drops them without `inventory.view`).
5. **Reorder levels** (`levels`): branches only (OFs deliver to branches).
   - The minimum is average daily net sales over `sales_days`, times `lead_days`. The target adds `cover_days`. Hand-set levels win.
   - *Have* = for sale + in transit + still due on APPROVED, ISSUED or PARTIAL POs (`on_order`, in pieces).
   - Low when target > 0 and have < minimum. The need is target − have, rounded up to whole order units unless the UOM can be split and `pieces_per_unit` is 1.
   - A row is orderable only with a supplier, a last cost and an active product.
6. **Draft OFs** (`make_orders`): one `order_forms.create_draft` per supplier, each branch's units as `allocations_m`, cost = `last_cost_c`, the supplier's payment terms, and a remark saying it came from inventory (audit `REORDER_DRAFT`). They then go through normal approval.
7. **Reports:** net sales by product or place with gross profit and margin; fast and slow movers (sold over `sales_days`, per day, days left, last sold, "not moving" after `dead_days` with stock); missing stock (posted COUNT movements: missing and over, valued); stock value by department.

Audit actions: `SALES_RECORD`, `SALES_RETURN`, `SALES_CANCEL`, `REORDER_LEVELS`, `REORDER_DRAFT`.

## Screens

- **Sales** (Inventory sidebar, "Sales and reorder"): tabs Sales, Customer returns, Cancelled and All; place and date filters; net total. Buttons: Record sales, Customer return, Import POS file (preview with matched and unmatched lines), and Cancel these sales (management).
- **Reorder:** low products (or every product), with an editable Order (units) column. "Set levels" sets or clears hand levels. "Make draft Order Forms" uses the selected rows when more than one is selected, otherwise every orderable low row. "How levels are worked out" (Admin) edits the four day settings.
- **Inventory reports** gains the tabs Sales, Fast and slow, Missing stock and By department.

## Tests

`tests/test_inventory_sales.py` (13 tests). The sample data adds a week of typed sales at every branch, a damaged customer return, and two hand-set levels at South that put lines on the reorder list.

## Gotchas

- The rule against selling before a count's start day is what stops double counting. Keep it if dates or counts change.
- `reorder.levels` reads `inventory.on_hand`, which needs `inventory.view`, so the Reorder page is office only.
- `SalesReportsWidget` hands its tab pages to `InventoryReportsPage`. It stays alive as `InventoryReportsPage.sales` to refresh them.
