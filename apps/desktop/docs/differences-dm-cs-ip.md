# Differences: debit memos, claim stubs, item passes

Once two receivers at a branch agree on a delivery (blind count, `receiving.py`), every difference against the PO is settled on one of three permanent, numbered papers. A **debit memo (DM)** charges the supplier for short pieces. A **claim stub (CS)** charges the supplier for damaged pieces, which travel back to Central Warehouse and wait for the supplier to collect them; the supplier may collect only after receiving the check that took the stub off, and has 90 days from that day. An **item pass (IP)** sends excess pieces back from the branch; it carries no money (excess is never paid), and the supplier has 90 days from when the goods reach the warehouse. DM and CS totals feed `payments.py` (they lower the order's check). Flow position: order → approval → warehouse → blind counts → **differences** → supplier checks.

## Files

| File | Role | Key public functions |
|---|---|---|
| `atlaspo/debit_memos.py` | DM service, plus pricing helpers shared by CS and payments | `amount_c`, `discount_share`, `shown_cost`, `outstanding`, `can_issue`, `issue`, `cancel`, `can_cancel`, `arrived_since`, `save_external_number`, `get`, `search`, `items`, `document` |
| `atlaspo/claim_stubs.py` | CS service, goods-coming-back helpers shared with IP, damage found later, branch damage reports | `outstanding`, `can_issue`, `issue`, `_create`, `late_lines`, `issue_late`, `record_arrival`, `arrival_counts`, `counted`, `arrived`, `count_arrived`, `collected`, `paid_on`, `days_left`, `forfeit`, `cancel`, `can_cancel`, `is_paid`, `document`, `print_pass`, `passes_for_branch`, `for_sale`, `late_numbers`, `report_numbers`, `report_lines`, `send_report`, `reports`, `approve_report`, `reject_report`, `reports_waiting` |
| `atlaspo/item_passes.py` | IP service | `to_print`, `issue`, `print_failed`, `arrived`, `count_arrived`, `collected`, `forfeit`, `cancel`, `search`, `for_branch`, `items`, `is_counted`, `as_on_paper` |
| `atlaspo/ui/debit_memo_page.py` | `DebitMemoDialog` (approve from Received) and `DebitMemosPage` (register) | |
| `atlaspo/ui/claim_stub_page.py` | `ClaimStubDialog` (subclass of `DebitMemoDialog`), `CountGoodsDialog`/`count_goods`, `LateDamagePanel`, `ClaimStubsPage`; helpers `_in_dates`, `_matching`, `_days`, `EVERY` reused by the other two pages | |
| `atlaspo/ui/item_pass_page.py` | `ItemPassesPage` (home office register) | |

Branch-side screens live in `ui/receiving_page.py` (item-pass printing, damaged goods pass, damage reports: `_passes_view`, `print_pass`, `print_cs_pass`, `find_damage`, `send_damage`). The warehouse counts returns from `ui/warehouse_page.py` (`_returns`, `count_return`). PDFs come from `pdf.render_debit_memo`, `render_claim_stub`, `render_item_pass`, `render_claim_stub_pass`.

## Data model (`schema.py`)

| Table | Migration | Notes |
|---|---|---|
| `dm_number_sequences`, `cs_number_sequences`, `ip_number_sequences` | V12, V14, V20 | `(year, last_number)`. Triggers `*_sequence_never_backward` and `*_sequence_no_delete`. |
| `debit_memos` | V12 (+`paid_back_check_id` V28) | status `ISSUED`/`CANCELLED`. `pdf BLOB DEFAULT x''`, `sha256`, `legacy_dm_number` (external number), `prepared_by/at`, `cancelled_by/at`, `cancel_reason`. |
| `debit_memo_items` | V12 | `reason IN ('SHORT','DAMAGED')`, product copied onto the line, `pieces_m > 0`, `net_unit_c` (the discounted pack cost shown), `amount_c`. `po_item_id` is deliberately **not** a FK, so a PO amendment never breaks the memo. |
| `claim_stubs` | V14 | status `ISSUED`/`AT_WAREHOUSE`/`COLLECTED`/`FORFEITED`/`CANCELLED`. `approved_by/at`, `arrived_by/at`, `closed_by/at`, `collected_by_name`, `legacy_cs_number`, `pdf`, `sha256`. |
| `claim_stub_items` | V14 | Same shape as DM lines without `reason`. |
| `item_passes` | V20 | status `PRINTED`/`AT_WAREHOUSE`/`COLLECTED`/`FORFEITED`/`CANCELLED`. `pdf` is NULL until written (unlike DM/CS, which start as `x''`). No money columns. |
| `item_pass_items` | V20 | Pieces only. A line may be a product not on the PO (count refers to it as minus its id, see `receiving._extra_lines`). |
| `goods_arrivals` | V25 | `(kind IN ('CS','IP'), item_id)` primary key: one warehouse count per stub/pass **line**, ever. No update or delete. |
| `late_damage` | V26 | One row per CS made for damage found after the count: `reason`, `found_by/at`, `paid_before` (the check that had already paid the order, or NULL). |
| `claim_requests`, `claim_request_items` | V27 | Branch damage reports: `PENDING`/`APPROVED`/`REJECTED`, `decided_by/at`, `decision_reason`, `cs_id`. |

**Status transitions** (service-enforced; triggers below guard the edges):
- DM: `ISSUED → CANCELLED`. Nothing else.
- CS: `ISSUED → AT_WAREHOUSE → COLLECTED | FORFEITED`; `ISSUED | AT_WAREHOUSE → CANCELLED` (only while the order is unpaid).
- IP: `PRINTED → AT_WAREHOUSE → COLLECTED | FORFEITED`; `PRINTED → CANCELLED` only.
- Report: `PENDING → APPROVED` (creates the CS) `| REJECTED`.

**Triggers that bite:**
- Header fixed: `debit_memo_fixed`, `claim_stub_fixed`, `item_pass_fixed` reject any update of number/year/seq/date/po/supplier/total/remarks/preparer. Note `remarks` is fixed too.
- PDF written once: `*_pdf_fixed` (DM/CS when `length(OLD.pdf) > 0`, IP when `OLD.pdf IS NOT NULL`). So the issuing transaction must INSERT header → INSERT lines → render → UPDATE pdf, in that order.
- Lines frozen: `*_items_no_update/no_delete`; `debit_memo_items_after_issue`, `claim_stub_items_after_approval`, `item_pass_items_after_print` (V28) refuse new lines once the pdf exists or status left the first state.
- No deletes anywhere: `debit_memo_no_delete`, `claim_stub_no_delete`, `item_pass_no_delete`, `late_damage_no_*`, `claim_request_no_delete`, `goods_arrivals_no_*`.
- Terminal states stay: `debit_memo_stays_cancelled`, `claim_stub_finished_stays`, `item_pass_finished_stays` (V21); no going back from warehouse: `claim_stub_not_back`, `item_pass_not_back` (V28).
- `debit_memo_cancel_fixed` (cancel reason/by/at fixed once cancelled); `debit_memo_paid_back_fixed` (`paid_back_check_id` may only be set in the same UPDATE that moves `ISSUED → CANCELLED`); `debit_memo_paid_back_new` (never set on INSERT).
- `late_damage_paid_before_known` (must name a real check).
- Reports: `claim_request_decided_stays`, `claim_request_items_after_decision`, `claim_request_items_once_sent` (no lines after the `CS_REQUEST` audit row exists, or for report ids at or below the V28 upgrade's `last_claim_request`), `claim_request_approved_new/_stub` (APPROVED needs `cs_id`).

## Rules enforced

Permissions (`auth.PERMISSIONS`): `dm.issue` = ADMIN, BUYER · `dm.cancel` = ADMIN, APPROVER · `cs.approve_request` = ADMIN, APPROVER, ACCOUNTING · `receiving.count` = RECEIVER · `receiving.review` = ADMIN, BUYER, APPROVER, VIEWER, ACCOUNTING, WAREHOUSE · `warehouse.receive` = WAREHOUSE · `goods.release` = ADMIN, BUYER, WAREHOUSE.

1. **A charge follows an agreed count on a live PO.** `_agreed()` (in both DM and CS) needs a `branch_counts` row with status `APPROVED` and PO not `CANCELLED`; `receiving.progress()` must be empty (no count open). Enforced in `outstanding`, `issue`, `can_issue`, `late_lines`. `dm.issue`.
2. **Never charge more than is outstanding.** DM: short = `max(ordered_m - supplier_m, 0)` minus live SHORT pieces on memos (`debit_memos._outstanding`). `supplier_m` is the larger of branch-received and what left Central Warehouse, so transit loss is never the supplier's (`receiving._finish_lines`). CS: `damaged_m` minus live non-late stub pieces. Over-typing raises `POError`. `dm.issue`.
3. **Only shortages go on a DM, only damage on a CS.** `debit_memos._outstanding` emits only `SHORT` lines; `DAMAGED` stays in the schema CHECK and `REASONS` for old data.
4. **Issuing is approving.** The purchaser who issues is recorded as `prepared_by` (DM) / `approved_by` (CS); there is no second approver. Management oversight is through cancel (`dm.cancel`). An ADMIN holds both.
5. **Numbers are permanent and per year.** `YYYY-DM-00001`, `YYYY-CS-00001`, `YYYY-IP-00001`, taken via upsert on the sequence table inside `tx()` (write lock first). Cancelled papers keep their number.
6. **The PDF is kept exactly.** Rendered once inside the issuing transaction, with `sha256`. `document()` returns the original on the first print and `pdf.stamp_copy(..., n, user)` on later prints; each print is audited (`DM_PRINT`, `CS_PRINT`). Readers need `receiving.review`.
7. **DM cancel** (`dm.cancel`, reason required): allowed whenever `ISSUED`, even after payment. If a live check already pays the OF, its id is stored as `paid_back_check_id` in the same UPDATE, and `payments` pays the memo back on the supplier's next check (`payments.paid_back_memos`, `_paid_back_pieces`).
8. **CS cancel** (`dm.cancel`, reason required): only `ISSUED`/`AT_WAREHOUSE`, and refused once the OF has a live check (`orders.refuse_if_paid`). `can_cancel` mirrors this for the button.
9. **IP cancel** (`dm.cancel`, reason required): only while `PRINTED` (goods not at the warehouse). Its pieces go back to needing a pass. `print_failed()` lets the receiver who printed it (`receiving.count`, same `printed_by`, not arrived) cancel straight away with the printer error as the reason.
10. **Item passes print once.** `issue()` covers every excess piece still left (`receiving.excess_lines`, `left_m`); a second call finds none. Only someone at the PO's branch (`receiving._reachable`: own branch, or ADMIN with no branch). `receiving.count`.
11. **Receivers never see ordered quantities or money.** `to_print` returns only `PASS_FIELDS`; `report_lines` strips cost; `reports()` drops `net_unit_c` and amounts for users without `receiving.review`, and limits receivers to their branch.
12. **Only the warehouse counts goods in.** `record_arrival` requires `warehouse.receive`; every line needs a number (0 if none came). Counts are permanent (`goods_arrivals` PK). A figure above the paper is refused (it would invent goods held for the supplier). Short is noted in the `CS_COUNTED`/`IP_COUNTED` audit summary and listed on inventory's Missing stock report as "Short on arrival from a branch".
13. **Collect or forfeit only counted goods at the warehouse.** `collected` (`goods.release`, collector name required) and `forfeit` (`dm.issue`) need `AT_WAREHOUSE` and `is_counted`.
14. **CS pickup gate tied to the check.** `claim_stubs.collected` refuses until `paid_on()` returns a date: the `check_claims.claimed_on` of the live check paying the OF, or, for a zero-amount `PROGRAM` check (settled by credit), its `check_date`. For a late-damage stub with `paid_before` set, it is the first live check to the same supplier recorded at or after `found_at`, excluding `paid_before`.
15. **90 days.** `FORFEIT_AFTER_DAYS = 90`; `days_left(d) = 90 - (today - d).days`; forfeit only when `days_left < 0`. CS counts from `paid_on`; IP counts from `arrived_at`.
16. **Damage found later.** `late_lines` room per product = `received_m - damaged_m - (late stub pieces + PENDING report pieces)`. `issue_late` (`dm.issue`, reason required) makes one CS per branch PO with a `late_damage` row; if the order is paid, `paid_before` is that check and the amount comes off the next check (`payments.late_claims`).
17. **Branch damage reports.** `send_report` (`receiving.count`, login must have a branch, reason required) writes one `claim_requests` row per PO; lines are written once via `_add_report_lines`. `approve_report` (`cs.approve_request`) refuses self-approval (`requested_by == user.id`), re-checks room with `leaving_out=request_id`, and creates the CS through `_create` with a `late_damage` row. `reject_report` needs a reason (the branch sees it).
18. **Damaged goods pass.** `print_pass` (`receiving.count`, own branch only, CS still `ISSUED`). The first print is the original; later prints are stamped copies. With `deliver`, the file is handed over before the audit row is written, so a pass that failed to open stays "not printed".

## Main flows

- **DM issue**: Received → To check → *Make debit memo* (`receiving_page.make_debit_memo`, button shown by `can_issue`) → `DebitMemoDialog` loads `outstanding` → user lowers/clears pieces → confirm → `issue(conn, user, po_id, {(po_item_id, reason): text}, remarks)`: require → `tx` → PO exists → not counting → `_agreed` → validate lines → bump `dm_number_sequences` → INSERT header + lines → `pdf.render_debit_memo` → UPDATE pdf/sha256 → audit `DM_ISSUE` → dialog prints at once via `document()` (`DM_PRINT`).
- **DM cancel**: `cancel(conn, user, dm_id, reason)` → `ISSUED` check → computes pieces `arrived_since` (late delivery) → finds live check on the OF → UPDATE status/cancel fields/`paid_back_check_id` → audit `DM_CANCEL`, with a summary that says whether a check paid it, how many pieces came since, and whether the rest count as short again.
- **DM external number**: `save_external_number` (`dm.issue`) → `legacy_dm_number` → `DM_EXTERNAL_NUMBER`.
- **CS issue** (from count): same dialog flow (`ClaimStubDialog`, does **not** print on approve) → `issue` → `_create` → `CS_ISSUE`.
- **CS life**: branch prints pass (`print_pass`, `CS_PASS_PRINT`) → warehouse `arrived(cs_id, pieces)` (`record_arrival` writes `goods_arrivals`, `CS_COUNTED`; status `AT_WAREHOUSE`, `CS_ARRIVED`) → after `paid_on`: `collected` (`CS_COLLECTED`) or, 90 days later, `forfeit` (`CS_FORFEIT`; goods then listed by `for_sale`). Goods recorded arrived before counting existed are counted with `count_arrived` (no status change).
- **Late damage (office)**: Claim stubs → *Damage found later* → `LateDamagePanel.find` (`late_lines`) → type pieces → `_ask_first` confirm (names the stubs, total, "comes off next check" if paid) → `issue_late` → one `_create` per PO with `late_damage` → `CS_ISSUE` with "damage found after delivery".
- **Branch report**: Receiving → Damage tab → `report_lines` → `send_report` (`CS_REQUEST`) → office Claim stubs → *Branch reports* tab → `approve_report` (`CS_ISSUE` + `CS_REQUEST_APPROVE`) or `reject_report` (`CS_REQUEST_REJECT`, entity `purchase_order`).
- **IP**: Receiving → Item passes tab → `to_print` → *Print item pass* → confirm → `choose_printer` → `issue` (`IP_PRINT`, returns pdf) → `print_once`; on printer error `print_failed` (`IP_CANCEL`) and a `POError` saying so. Warehouse `arrived` (`IP_COUNTED`, `IP_ARRIVED`) → `collected` (`IP_COLLECTED`) or `forfeit` after 90 days (`IP_FORFEIT`). Management `cancel` while `PRINTED` (`IP_CANCEL`).

## Screens

- **Sidebar pages** (`main_window.py`): Debit memos, Claim stubs and Item passes are built only for `receiving.review` holders (so not RECEIVER).
- **`DebitMemoDialog` / `ClaimStubDialog`**: Pieces is the only editable, tinted column (`PaintBackground`/`TintDelegate`). Amount and total recalculate as you type. Enter = approve after a confirm (Approve is default, Cancel is not auto-default), so typed figures are not lost. DM button reads "Approve and print"; CS button reads "Approve". DM shows a note warning that charging before a pending delivery leaves the PO on To check.
- **`DebitMemosPage`**: tabs Approved / Cancelled / All with counts; search (number, PO, OF, supplier/code, external number), From/To dates, month picker, Clear filters, Columns chooser, a page at a time (`Pager`). Selected memo: lines table, note (approver, remarks, or "N pieces since delivered"). Buttons: Print / Print again (any viewer, asks before a reprint), external number + Save (`dm.issue`), Cancel debit memo (`dm.cancel`, set apart by spacing).
- **`ClaimStubsPage`**: tabs by status label (warehouse name from `company.warehouse_name`) + All + Branch reports, which shorten in the middle on narrow windows (`FitTabs`). ISSUED and AT_WAREHOUSE tabs show every row; the others page. Tick boxes + "Select all not printed" + "Print ticked" (`dm.issue` or `cs.approve_request`) print originals in one file. Days left shows "Waiting for payment" until `paid_on`. Buttons by permission: Goods arrived / Count goods (`warehouse.receive`), Collected (`goods.release`, only when counted and `paid_on`), Forfeit, for sale (`dm.issue`, only past 90 days), Cancel stub (`can_cancel`), Damage found later (`dm.issue`, opens `LateDamagePanel` in a splitter at half width). The Branch reports view has Approve and Reject (`cs.approve_request`, only on PENDING rows).
- **`ItemPassesPage`**: same register pattern, without money. No print button: only the branch prints. Buttons: Goods arrived / Count goods, Collected, Forfeit, Cancel pass (PRINTED only, `dm.cancel`).
- **`CountGoodsDialog`**: "On the paper" vs a tinted "Counted" column; every line required.
- **Branch (`receiving_page`)**: Item passes tab shows passes to print, damaged goods passes (with "Pass printed" time), and passes already printed with where the goods are now. The tab title counts what is left to print. Damage tab: find a PO/OF (completer from `report_numbers`), narrow, type pieces, send, see sent reports and decisions.

## Money and numbers

- Quantities are thousandths (`_m`); money is whole centavos. `amount_c(pieces_m, net_unit_c, ppu, share) = round_half_up(pieces_m * net_unit_c * kept / (ppu * 1000 * whole))`. It works from the **pack** cost, so per-piece rounding never changes a total.
- `discount_share(po)` = `(Σline_total_c - overall_discount_c, Σline_total_c)`, or `(1,1)` with no discount. A charge never takes back more than was paid.
- `shown_cost` = pack cost after the discount, rounded. It is stored as the line's `net_unit_c` and printed. The line's `amount_c` is computed from the undiscounted `cost_c` × share, so `pieces × shown_cost` can differ from `amount_c` by a rounding centavo.
- The header `total_c` = Σ line `amount_c`. IPs carry no money; printing one "settles" the excess (`receiving._settled_from` checks `left_m == 0`).
- Late-damage and report pieces parse as `"PC"` (`parse_qty(text, "PC")`); count-based DM/CS pieces parse with the line's UOM.

## Interactions with other modules

- **receiving.py**: `lines_of`, `progress`, `dm_pieces`, `excess_lines`, `_reachable`, `_settled` all read these tables. `dm_pieces` counts ISSUED DMs and non-cancelled CSs **excluding late-damage stubs**, so late damage never changes whether the count is "settled". A recount (`receiving` reopen check, ~line 400) is refused while the order is paid or while any DM/CS/IP is live (`orders.live_charges`): management cancels them first.
- **orders.py**: `paid_by`, `refuse_if_paid`, `live_charges` (lists live DM/CS/IP numbers; also blocks PO cancel/amend paths).
- **payments.py**: `charges(of_id)` subtracts ISSUED DMs and non-cancelled CSs (late ones included) from the order; `_po_lines` uses `amount_c`/`discount_share` for short and unclaimed values; `paid_back_memos`, `late_claims` adjust the supplier's next check; `paid_on` reads `check_claims` and credit settlements (`PROGRAM`, amount 0).
- **pdf.py**: renders the papers; `pdf.py:950` appends every ISSUED DM of an OF to an OF print by calling `debit_memos.document`, which records a `DM_PRINT` for each.
- **warehouse_page.py**: lists CS `ISSUED` / IP `PRINTED` and uncounted `AT_WAREHOUSE` returns, and counts them with `count_goods` + `arrived`/`count_arrived`.
- **backup.py**: integrity checks (non-cancelled papers have a pdf and lines; `late_damage.paid_before` exists).
- **sample_data.py**: exercises every flow and is a good worked example.

## Tests

Main files: `tests/test_debit_memos.py`, `test_claim_stubs.py`, `test_fix_claim_stubs.py` (late damage panel, tabs, print-inside-tx, warehouse-only count), `test_fix_dm_ip.py` (dialog Enter, cancel summaries, item-pass page), `test_fix2_payments-passes.py`, `test_fix2_claims-receiving.py`. Also touched in `test_receiving.py`, `test_review_fixes.py`, `test_business_rules.py`, `test_audit_fixes.py`, `test_rules_discount.py`, `test_rules_money.py`, `test_payments.py`, `test_fix_payments.py`, `test_warehouse.py`, `test_rules_paging_lists.py`.

From `apps/desktop`: `python -E -m pytest tests/test_debit_memos.py tests/test_claim_stubs.py tests/test_fix_claim_stubs.py tests/test_fix_dm_ip.py -q -p no:cacheprovider`. `test_fix_dm_ip::…debit_memo_date…` is a known font/DPI-flaky width test.

## Gotchas for changing this module

- **Nested `tx()` is a no-op join** (`db.tx` yields if `conn.in_transaction`). `_create`, `late_lines`, `get` and `record_arrival` run inside callers' transactions. `approve_report` calls `get()` inside its tx, so the approver needs `receiving.review` as well as `cs.approve_request` (ACCOUNTING/APPROVER/ADMIN do).
- **Write order inside issue is forced by triggers**: lines before pdf; never UPDATE header money or remarks afterwards. Fix a wrong paper by cancelling it and issuing a new one.
- **`goods_arrivals` is one count per line forever.** There is no recount of returned goods; a wrong count stays on record (only the audit note shows the difference).
- **The CS and IP 90-day clocks start from different dates** (check received vs arrival). Tests assert the docstrings say "check" (`test_the_documentation_says_the_90_days_run_from_the_check`), so keep that wording.
- **CS can't be cancelled once paid, but a DM can.** That is deliberate: a DM cancel after payment becomes a payback on the next check, recorded through `paid_back_check_id`, which can only be set in the cancelling UPDATE.
- **Late-damage stubs are excluded from `dm_pieces`** but included in `payments.charges`. Don't "fix" one side to match the other.
- `issue()`/`issue_late()` do not refuse a paid order (only cancel does). Damage found after payment is the designed path; it goes through `late_damage.paid_before`.
- **Branch scoping**: `print_pass` compares `user.branch_code` exactly (an ADMIN without a branch can't print a damaged goods pass), while `item_passes.issue` uses `_reachable` (ADMIN without a branch can).
- **Possible defects found while writing this (unverified by tests):**
  - `debit_memos.document` reads the earlier-print count **outside** the `tx` (line ~229), unlike `claim_stubs.document`/`print_pass`, which were fixed to read inside the lock. Two PCs printing at once could both get an unstamped "original".
  - `DebitMemoDialog._recalculate` previews `amount_c(pieces, shown_cost, ppu)` without `share`, while `issue` uses `amount_c(pieces, cost_c, ppu, share)`. With an overall discount, the on-screen total can be a centavo off the stored total.
  - `item_passes.for_branch` takes `LIMIT 200` over **all** branches before filtering by branch, so a branch's older passes can drop off its "Already printed" list once other branches have 200 newer passes.
  - `reject_report` audits with entity `purchase_order`/po_id, while `approve_report` uses `claim_stub`/cs_id. Neither is linked to `claim_request` by id, so trace a decision by `po_id` and time.
  - The README says a claim stub "works like a debit memo", but a CS does not print on approve. It prints from the Claim stubs page.
- UI permission gates are sometimes stricter than the service (for example, CS "Print ticked" needs `dm.issue` or `cs.approve_request`, while `document()` only needs `receiving.review`). The service is the authority. Never loosen a `require()` to make a screen work.
