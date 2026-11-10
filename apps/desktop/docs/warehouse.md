# Central Warehouse

Every printed branch PO goes to the Central Warehouse before it reaches a branch. The warehouse login (role `WAREHOUSE`) records each supplier delivery per OF: the **boxes for each branch PO** (0 when nothing came for that branch) and, for suppliers whose goods come **without stickers** (supplier has a `sticker_rate_c`), the **pieces of each product it stickered**. Figures can be corrected until that branch is **shipped**; after that they are frozen by triggers. A supplier that delivers in several goes gets one record per delivery (`delivery_no` 1, 2, ...). The same page also lists **claim stub (CS) and item pass (IP) goods coming back** from branches so the warehouse can count them on arrival. Flow position: order → approval → print/issue → **warehouse receive → ship** → branch blind counts (gated on "shipped") → differences → supplier checks (payment terms start on the last warehouse `received_at`). The warehouse figure is what the supplier delivered; the branch count is what arrived. Pieces lost in between are a transit loss for the home office, never a supplier charge (`warehouse.py` docstring, lines 1-9).

## Files

| File | Role | Key public API |
|---|---|---|
| `atlaspo/warehouse.py` | Service, no Qt | `search`, `lines`, `receive`, `ship`, `live_since`, `stickers`, `shipped_on`, `left_central`, `boxes_sent`; constants `TO_RECEIVE/TO_SHIP/SHIPPED`, `TABS`, `REISSUE`, `MAX_COUNT`; private `_pos` (also used by the UI) |
| `atlaspo/ui/warehouse_page.py` | Screen "Central Warehouse" | `CentralWarehousePage` (`save`, `ship`, `ship_one`, `count_return`, `refresh`) |
| `atlaspo/claim_stubs.py`, `atlaspo/item_passes.py` | Arrival counts of returned goods | `arrived`, `count_arrived`, `is_counted`, `claim_stubs.record_arrival` |
| `atlaspo/schema.py` | `MIGRATE_V24` (legacy tables), `MIGRATE_V25` (current tables, triggers, `goods_arrivals`) | |
| `atlaspo/auth.py` | `PERMISSIONS`, `_check_receiver_alone`, `warehouse_login_missing` | |

## Data model

- **`warehouse_deliveries`** (V25, `schema.py:1562`): `id, po_id, delivery_no (>0), boxes (>=0), remarks, received_by, received_at, corrected_by, corrected_at, shipped_by, shipped_at`, `UNIQUE(po_id, delivery_no)`. A row with `shipped_at IS NULL` is the PO's "current" delivery (at most one by construction in `receive`).
- **`warehouse_delivery_items`** (V25): `PRIMARY KEY(delivery_id, po_item_id)`, `pieces_m >= 0` (thousandths of a piece). Only written for sticker suppliers.
- **Triggers** (V25, `schema.py:1586-1598`): `warehouse_delivery_fixed` aborts any UPDATE once `shipped_at` is set, and any change to `po_id`, `delivery_no`, `received_by`, `received_at` ever; `warehouse_delivery_no_delete`; items: no INSERT/UPDATE once the delivery shipped, no re-keying, no DELETE.
- **Legacy, frozen**: `warehouse_receipts`, `warehouse_stickered` (V24, `schema.py:1526-1547`). V25 copied them into the new tables as delivery 1. Nothing writes them; never drop them.
- **`goods_arrivals`** (V25): `(kind 'CS'|'IP', item_id)` PK, `pieces_m`, `counted_by`, `counted_at`; no update/delete ("Counts are permanent"). Written by `claim_stubs.record_arrival`.
- **Status triggers**: `claim_stub_not_back`, `item_pass_not_back` (`schema.py:~1810-1818`) forbid AT_WAREHOUSE → ISSUED/PRINTED.
- **Settings**: `warehouse.live_since` (inserted by V24 at migration time; POs first issued before it are never listed); `receiving.after_shipping` (default `"1"`, `schema.py:340`; sample data sets `"0"`).

Per-PO stage (computed in `_pos`, `warehouse.py:63`, never stored):

```
(issued, not counted, not paid, not outside, no delivery) --receive--> TO_SHIP --ship--> SHIPPED
SHIPPED --receive(another=True), only while can_more--> TO_SHIP (delivery_no+1)
TO_SHIP + PO amended back to APPROVED --> TO_SHIP with reissue=True (correctable, not shippable)
```

Listed POs: `of_id NOT NULL`, status in `ISSUED, PARTIAL, FULL, CLOSED, APPROVED`, first issue (`MIN(po_issued_versions.issued_at)`, else `issued_at`) `>= live_since`. A PO with no deliveries shows as TO_RECEIVE only if status is exactly `ISSUED`, no `branch_counts`, no `outside_receipts`, and no ACTIVE `payment_checks` for its OF; otherwise it is dropped. `can_more` = stage SHIPPED and status in `ISSUED, PARTIAL` and not paid and not outside.

## Rules enforced

Permissions: `warehouse.receive` = {`WAREHOUSE`} only; `receiving.review` = {ADMIN, BUYER, APPROVER, VIEWER, ACCOUNTING, WAREHOUSE} (`auth.py:40-42`).

1. Only the warehouse records/corrects/ships: `receive`, `ship` call `require(user, "warehouse.receive")`. Reading (`search`, `lines`) needs `receiving.review`.
2. A WAREHOUSE login has no other role (separation of duties: the warehouse checks what suppliers deliver) — `auth._check_receiver_alone` (`auth.py:273`).
3. First receive for an order: every listed branch PO in scope needs a box figure, 0 allowed (`receive`, `warehouse.py:213-217`). Boxes must be ASCII-digit text, `<= MAX_COUNT` (10^9) (`_boxes`).
4. Sticker suppliers: every product line of every targeted PO needs a pieces figure, 0 allowed, parsed with `parse_qty(...,"PC")`, `<= MAX_COUNT*1000` (`_pieces`). Whether stickers apply comes from `order_forms.supplier_snapshot.sticker_rate_c` (taken at approval), falling back to the live supplier (`stickers`).
5. More stickered than still due (`ordered_m - before_m`) is allowed by the service; only the UI asks "Save anyway?" (`_over_ordered`, `warehouse_page.py:396`).
6. Corrections only until shipped: service edits only the `current` delivery; triggers block anything after `shipped_at`. `received_by/received_at` never change; the corrector goes to `corrected_by/at` and the old figures to the audit log.
7. Lost-update guard: `expect_delivery` = {po_id: `_mark` tuple or None}. `_mark` = (id, corrected_at or received_at, boxes, remarks, pieces tuple). Mismatch → `POError("Someone else saved this delivery. Open it again.")`. Optional in the service; the UI always passes it.
8. Later deliveries only for unfinished, unpaid POs (`can_more`) and only via `another=True`; blank branches are skipped.
9. Shipping: only POs with a current delivery. A PO whose status is back to `APPROVED` (amended, awaiting reprint) cannot ship: named explicitly → error with `REISSUE`; shipping the whole order silently leaves it behind unless all ready POs are held (`ship`, `warehouse.py:267-271`).
10. Branch counting is held until shipped: `receiving.not_shipped_yet` (`receiving.py:329`) blocks a first count while shipped deliveries < the branch's delivery number, only when `receiving.after_shipping == "1"` and an active WAREHOUSE login exists (`auth.warehouse_login_missing`).
11. Returned CS/IP goods: `arrived` (ISSUED/PRINTED → AT_WAREHOUSE + count) and `count_arrived` (AT_WAREHOUSE but uncounted, legacy) need `warehouse.receive`. Every line needs a figure. `collected` (`goods.release`: ADMIN, BUYER, WAREHOUSE) refuses until counted. IP's 90 days start at `arrived_at`; CS's run from the check date (`paid_on`), not arrival.

## Main flows

**Receive / correct** — `receive(conn, user, of_id, boxes, stickered=None, remarks=None, another=False, only=None, expect_delivery=None)`:
`require` → `tx` → `_pos(of_id)` (filtered by `only`) → mark check → collect targets (current or TO_RECEIVE; or `can_more` with `another`) → none → error → if sticker, `_pieces` for each line of target POs via `lines()` → per target: unchanged (boxes, pieces, remarks) → skip; current → `UPDATE ... corrected_by/at` + audit **`WAREHOUSE_CORRECT`** (previous/new boxes, remarks, changed pieces); else `INSERT` delivery `delivery_no = len(deliveries)+1` + audit **`WAREHOUSE_RECEIVE`** → upsert items → if any pieces, one audit **`WAREHOUSE_STICKERED`** on the `order_form`. `remarks=None` keeps the current remarks.

**Ship** — `ship(conn, user, of_id, po_ids=None)`: `require` → `tx` → ready = POs with current delivery (∩ po_ids) → reissue check → set `shipped_by/at` per PO → audit **`WAREHOUSE_SHIP`** per PO.

**Count returned goods** — UI `count_return` → `claim_stub_page.count_goods` dialog → `claim_stubs/item_passes.arrived` (audits `CS_COUNTED`/`IP_COUNTED` then `CS_ARRIVED`/`IP_ARRIVED`) or `count_arrived` (count audit only).

## Screens

`CentralWarehousePage` (sidebar "Central Warehouse"; built only if `receiving.review`; a WAREHOUSE-only login sees just Central Warehouse, Claim stubs, Item passes — `main_window.py:238`).
- Tabs: To receive / To ship / Shipped / Coming back, with counts. An OF appears under every tab one of its POs is in. Shipped is paged (`Pager`); the others show everything.
- Search box (OF/PO number, supplier name/code), debounced 300 ms. Branch toggle buttons with per-tab counts narrow orders, lines and returns; with a branch chosen, `save` passes `only=` so other branches are not required.
- Order table → details: boxes table (per branch PO; editable Boxes cell tinted, multi-select; per-row "Ship" button), stickers table (Ordered, Earlier deliveries, Stickered pcs; shown only for sticker suppliers), remarks, buttons "Another delivery at the warehouse", "Save received"/"Save changes", "Mark shipped"/"Mark N shipped" (selected rows; ready rows preselected).
- Unsaved typing is kept per OF in `self.drafts`; Ship with a draft offers "Save and ship" (`_save_then` re-reads `service._pos` after save).
- Non-WAREHOUSE roles: read-only (no buttons, cells not editable, remarks read-only, notes don't tell them to press anything).
- Coming back tab: CS `ISSUED` / IP `PRINTED` (on the way) plus AT_WAREHOUSE not yet counted; button "Goods arrived: count them".
- Keyboard: cells edit on any key (`AllEditTriggers`); no page-specific shortcuts. Ship actions confirm first.
- Service calls: `warehouse.search`, `lines`, `receive`, `ship`, `_pos`; `claim_stubs/item_passes.search/items/arrived/count_arrived/is_counted`; `masters.list_branches`. Emits `changed` after writes.

## Money and numbers

No money. Pieces are thousandths (`_m`, `parse_qty`/`fmt_qty`); ordered pieces = `qty_m * pieces_per_unit`. Boxes are plain integers. `delivery_no` is per PO, sequential from 1. No rounding.

## Interactions with other modules

- Calls: `orders` (status constants), `db.tx/audit/get_setting/timestamp`, `money`.
- Called by: `receiving.py` (`left_central` for pieces that left central, `boxes_sent` in `boxes_check`; raw SQL on `warehouse_deliveries` in `not_shipped_yet`, `_shipped_from_central`, line ~676), `payments.central_day` (terms start at `MAX(received_at)`), `reports.py:162`, `ui/of_page.py:1323`, `sample_data.py` (inserts rows directly, then exercises `receive/ship`), `ui/main_window.py`.

## Tests

`tests/test_warehouse.py`, `tests/test_fix_warehouse.py` (concurrency marks, reissue, save-and-ship, limits), `tests/test_after_shipping.py` (branch gate). Also touched: `test_review_fixes.py`, `test_rules_logins.py`, `test_rules_paging_lists.py`, `test_fix_of_lists.py`, `test_fix_pdf_reports.py`, `test_sample_data.py`.

From `apps/desktop`: `python -E -m pytest tests/test_warehouse.py tests/test_fix_warehouse.py tests/test_after_shipping.py -q -p no:cacheprovider`

## Gotchas for changing this module

- No `orders.write_guard` is needed: warehouse tables have their own triggers, independent of PO locking. Never UPDATE a shipped delivery or DELETE any row; triggers abort, by design.
- `received_at` is immutable and drives payment terms (`payments.central_day`); a correction does not move it, a later delivery does.
- `stage` is computed, not stored. Changing `_pos` filters changes the tabs, `receive`, `ship` and `sample_data` together.
- Calling `receive` without `expect_delivery` silently overwrites a concurrent save; keep passing it from screens.
- `live_since == ""` (missing setting) makes the `>=` filter match everything.
- TO_RECEIVE requires status exactly `ISSUED`; a PARTIAL PO with no warehouse record is deliberately not asked for.
- Leaving held (reissue) POs behind on whole-order ship is deliberate; refusing on named ship is too.
- Over-ordered stickered pieces are allowed by the service on purpose (UI confirm only).
- WAREHOUSE is a single-role login; ADMIN cannot record warehouse goods (`warehouse.receive` is WAREHOUSE only).
- `warehouse_receipts` / `warehouse_stickered` are legacy: don't write them, don't drop them.
