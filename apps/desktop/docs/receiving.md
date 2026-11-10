# Branch receiving (blind double count)

Branch receiving comes after the Central Warehouse ships a PO to a branch. Two receivers at the branch count the delivery separately. Neither sees the ordered quantity, the cost, the price or the other person's count. Only figures both people agree on are recorded (`branch_received`). Those agreed figures set the PO's received quantities and status (ISSUED, PARTIAL or FULL). They also drive everything downstream: shortages go on debit memos, damage on claim stubs and excess on item passes, and the check is worked out from them. Products that disagree are counted again by two people. After two rounds that don't agree, counting stops until the home office allows another round. The home office can also reopen a matched delivery for a recount, once, with a reason. Damage found after the count is agreed is reported by a receiver (pieces only) and approved by Accounting or management, which turns it into a claim stub. The service docstring (`receiving.py:1-14`) is the authoritative summary.

## Files

| File | Role | Key public API |
|---|---|---|
| `atlaspo/receiving.py` | Counting state machine, comparing counts, recount allowances, home office results, settlement checks | `find_pos`, `to_count`, `sheet_key`, `sheet`, `save_count`, `read_quantity`, `can_redo`, `branch_counts`, `count_lines`, `reopen`, `reopen_label`, `progress`, `po_results`, `received_list`, `po_lines`, `po_history`, `review_lines`, `by_receiver`, `lines_of`, `excess_lines`, `dm_pieces`, `of_delivery_complete`, `delivered_by_po`, `taken_as_delivered`, `live_since`, `boxes_check`, `not_shipped_yet` |
| `atlaspo/ui/receiving_page.py` | `ReceivingPage` (receivers) and `ReceivedPage` (home office) | `ReceivingPage.save_count`, `.recount`, `.send_damage`, `.print_pass`, `.print_cs_pass`; `ReceivedPage.reopen`, `.make_debit_memo`, `.make_claim_stub` |
| `atlaspo/claim_stubs.py` (section "damage reported by a branch", ~line 515) | Branch damage reports, plus damage found later | `report_numbers`, `report_lines`, `send_report`, `reports`, `approve_report`, `reject_report`, `late_lines`, `issue_late`, `passes_for_branch`, `print_pass` |
| `atlaspo/item_passes.py` | Excess goods leaving the branch; reads `receiving.excess_lines` | `to_print`, `issue`, `for_branch` |
| `atlaspo/auth.py:38-57` | Permissions; receiver-only and branch rules | `PERMISSIONS`, `_check_receiver_alone`, `_set_branch` |

## Data model

Quantities are in thousandths of a piece (`_m`, so 1 piece is 1000). Every table here is append-only through triggers. Most raise `'A count is permanent. Recount instead'`.

- **`branch_counts`** (V9, rebuilt in V11). One row per person per round: `po_id, delivery_no, attempt_no, round_no, status, counted_by, counted_at, approved_by, approved_at, recount_of, reason`. `CHECK (approved_by <> counted_by)`. On an agreed pair, `approved_by` means "whose count this one matched", not an approver (`_compare`, lines 475-476).
  - `delivery_no`: each physical delivery of the PO (split deliveries).
  - `attempt_no`: goes up when the home office reopens a matched delivery for a recount.
  - `round_no`: goes up when a pair disagrees.
  - `recount_of`: points to the replaced count on a redo, or to the accepted count on a recount.
- **`branch_count_items`** (V9): the person's figure per line (`pieces_m`, `remarks`, plus a snapshot of code, description, size, color and UOM). `po_item_id` is **negative for products not on the PO**: minus the `branch_extra_items.id`.
- **`branch_count_damage`** (V10): `damaged_m > 0`, only where there is damage. The damaged pieces are *inside* `pieces_m`.
- **`branch_received`** (V11): agreed figures, PK `(po_id, delivery_no, attempt_no, po_item_id)`. Only rows of a delivery's latest fully agreed attempt are in force (`_IN_FORCE`, line 39). Rows from an attempt that hasn't fully agreed sit there but don't count yet.
- **`branch_extra_items`** (V13): products that arrived but aren't on the PO, per delivery. `excess_returns` (V13): older excess returns, which still count as "returned".
- **`count_reopens`** (V22): home office allowances, `kind IN ('RECOUNT','ROUND')`, with `reason` and `opened_by`. `used_by_count_id` is set once, by the first count it lets happen. The trigger `count_reopens_used_once` blocks any other update. Rows can't be deleted.
- **`branch_count_boxes`** (schema.py ~1604): boxes typed per count.
- **`claim_requests` and `claim_request_items`** (V27): branch damage reports (PENDING, then APPROVED or REJECTED). **`late_damage`** (V26): marks claim stubs made for damage found later.
- V28 triggers (schema.py ~1820-1858):
  - Items, damage and boxes can't be inserted into a count that is MISMATCH, or into one that is agreed or replaced once its attempt has `branch_received` rows.
  - An APPROVED count must name `approved_by`, and that never changes afterwards.
  - Report lines can't be added once the report is decided or already logged as sent (`CS_REQUEST` audit row).
  - An APPROVED report must have its `cs_id`.
- Setting `receiving.after_shipping` (default `"1"`, schema.py:340; sample data sets `"0"`).

Count status path (trigger `branch_count_status_path`, V21):

```
WAITING --second count agrees on every line--> APPROVED --later attempt fully agrees--> REPLACED
WAITING --any line differs--> MISMATCH   (final; agreed lines still go to branch_received)
WAITING --same person redoes once--> REPLACED
```

`_state()` (line 96) works out what is needed next from the latest rows:

| `need` | When |
|---|---|
| `SECOND` | a WAITING count exists |
| `AGAIN` | the latest count is MISMATCH and `round <= 2 + ROUND allowances` |
| `HELD` | the latest count is MISMATCH and past that limit |
| `RECOUNT` | an unused RECOUNT allowance exists |
| `FIRST` | nothing has been counted yet; a new delivery is `MAX(delivery_no)+1` |
| `None` | everything agreed |

## Rules enforced

Permissions (auth.py:38-57):
- `receiving.count`: RECEIVER only.
- `receiving.review`: ADMIN, BUYER, APPROVER, VIEWER, ACCOUNTING, WAREHOUSE.
- `receiving.reopen`: ADMIN, APPROVER.
- `cs.approve_request`: ADMIN, APPROVER, ACCOUNTING.
- `dm.issue`: ADMIN, BUYER (used for damage found later).

Rules:

1. **Only receivers count, never Admin.** Enforced by `require(user, "receiving.count")` on every receiver entry point. Blind counting would be meaningless for someone who can see orders.
2. **A receiver holds no other role and must have a branch.** Enforced by `auth._check_receiver_alone` and `auth._set_branch`.
3. **Branch isolation.**
   - `_po()` and `_reachable()` refuse POs of another branch with "That PO isn't for your branch."
   - `find_pos` filters after the query and never reveals that a number exists elsewhere.
   - `to_count` and `branch_counts` filter by `user.branch_code`.
   - `item_passes.to_print` and `for_branch`, and `claim_stubs.passes_for_branch`, `report_numbers` and `reports`, also filter by branch.
4. **Blind sheet.**
   - `sheet()` and `_line_rows()` return only code, description, size, color and UOM, plus `previous`. The docstring says: "New fields only go here if a receiver may see them."
   - `previous_counts` returns only the caller's own figures.
   - `count_lines` refuses anyone but the counter. `review_lines` (any person's numbers) needs `receiving.review`.
5. **Each product needs a number (0 allowed); damaged ≤ counted; blank damaged means 0.** Enforced by `save_count` and `read_quantity` (parse via `money.parse_qty`, limit `MAX_PIECES_M` = 10^9 pieces). The UI pre-checks the same rules in `_check_sheet` and marks bad cells red.
6. **No self-match.** A person whose count is WAITING can't open or save the second count (`sheet`, `save_count` line 265). The `CHECK approved_by <> counted_by` constraint backs this up.
7. **Redo once.** Each person may redo their own WAITING count once (`can_redo`, `_redone`). The old count becomes REPLACED.
8. **Matching is exact per line on (pieces_m, damaged_m)** (`_compare`).
   - Agreed lines are written to `branch_received` straight away.
   - A line only one person has (for example an extra the second person added) counts as a mismatch.
   - The PO totals only change once a whole attempt agrees (`_apply_totals`).
9. **Two rounds, then HELD** (`CHECK_AFTER = 2`). While HELD, `sheet` and `save_count` raise `STOPPED`. Each `ROUND` allowance adds one round. The allowance is used by the first count of that round (`save_count` lines 300-303).
10. **Reopen a matched delivery: once per delivery, with a reason, `receiving.reopen`** (`reopen`, `_why_not_reopen`). Refused when:
    - the PO is closed or cancelled;
    - counting is still open;
    - a RECOUNT allowance was already given for that delivery;
    - the order is paid (`orders.paid_by`);
    - there are live DMs, CSs or IPs (`orders.live_charges`).

    A HELD delivery skips these checks and gets a ROUND allowance instead.
11. **The accepted count stands until the recount agrees.** `_compare` marks earlier APPROVED attempts REPLACED only when the new attempt fully agrees.
12. **A new delivery waits for shipping.** `not_shipped_yet` applies only when the setting is `"1"` and a warehouse login exists. It compares shipped `warehouse_deliveries` against `delivery_no`. Second counts, rounds and recounts are never held back.
13. **Boxes are required** on a whole-delivery count (FIRST, SECOND, RECOUNT or None, round 1) once the Central Warehouse has shipped the PO. The input must be digits only, at most `MAX_BOXES`.
14. **Stale-sheet guard.** The UI reads `sheet_key` before `sheet` and passes it back as `expect`. If it no longer matches, `save_count` raises `OUT_OF_DATE` and saves nothing.
15. **Extras.**
    - `_add_extra` refuses an extra that matches a PO product (code, size and color, folded with `code_key` because SQLite's `upper()` only folds A-Z).
    - It also refuses a duplicate extra in the same delivery.
16. **Damage reports (receivers).**
    - Pieces only; `reports()` strips `net_unit_c` for receivers.
    - The amount reported per product is limited to received − branch-counted damage − damage found later − pending reports (`late_lines`).
    - Only POs that are agreed and have no open counting can be reported on.
    - A reason is required.
    - Each report's lines are written once (`_add_report_lines`, AUD-130, plus a trigger).
17. **Approving a report** (`cs.approve_request`).
    - Refused for whoever filed the report.
    - The available room is checked again, excluding this report's own pieces. If there isn't room, the person is told to reject it.
    - A rejection needs a reason, which the branch sees.

## Main flows

- **Count (receiver).**
  1. The UI calls `find_pos(number)`, which checks the number matches an OF or PO, the branch, and that the status is COUNTABLE (ISSUED, PARTIAL or FULL).
  2. It then calls `sheet_key` and `sheet`.
  3. `save_count(pieces, remarks, recount_of, damaged, extras, boxes, expect)` runs in `tx`. It:
     - checks the status, HELD, shipping and `expect`;
     - handles a redo or recount;
     - inserts extras, the count (WAITING), its items, damage and boxes;
     - writes audit `BRANCH_COUNT`;
     - uses an allowance if one applies.
  4. If it is a second count, `_compare` runs and writes `branch_received`. It then writes either `BRANCH_COUNT_MISMATCH`, or `BRANCH_COUNT_MATCH` plus `_apply_totals` (inside `orders.write_guard`).
  5. It returns `{count_id, outcome, mismatched}`.
- **Redo own count.** On "My branch's counts", `recount()` sets `recount_of` to the waiting count id. `save_count` then sets `redo=True`, marks the old count REPLACED and returns WAITING.
- **Reopen or allow a round (home office).** `ReceivedPage.reopen` asks for a reason, then calls `receiving.reopen`. That inserts a `count_reopens` row and writes audit `COUNT_REOPEN`. The next `_state` gives RECOUNT (all delivery lines, new attempt) or AGAIN.
- **Branch damage report.** `send_report` writes `claim_requests` (PENDING), its items and audit `CS_REQUEST`, one per PO. `approve_report` calls `_create` (claim stub plus `late_damage`) and writes audit `CS_ISSUE` and `CS_REQUEST_APPROVE`. `reject_report` writes audit `CS_REQUEST_REJECT`. After approval, the pass prints under Item passes (`print_pass`, audit `CS_PASS_PRINT`, later prints stamped as copies).
- **Damage found later (purchaser).** `claim_stubs.issue_late` (`dm.issue`) is the same flow without the request step.

## Screens

- **`ReceivingPage`** (the sidebar page "receiving", only if `receiving.count`, main_window.py:209). The header shows the branch name. It has four tabs; the first three show work-waiting counts in the tab title:
  - **Count a delivery:**
    - A list from `to_count`. Its "What's needed" column shows First/Second/Count again/Recount. A PO leaves the list once it is accepted, short or not, so the list doesn't reveal a shortage.
    - Typing a number and pressing Enter (or Open) opens it. When an OF covers several POs, a picker appears.
    - The grid has editable Pieces, Damaged and Remarks columns (all edit triggers). Focus starts on the first Pieces cell.
    - A "You counted before" column shows only when there are own earlier figures, with a prompt not to copy them.
    - Other controls: "Add product not on the PO" (a blank PC line), "Boxes received" (hidden in AGAIN rounds), "Clear what I typed", "Go back" (asks first if anything is typed), and "Save count" (validates, then confirms).
  - **My branch's counts:** `branch_counts` table. Selecting your own count shows its lines (`count_lines`). Anyone else's shows "only X can see these numbers". "Count mine again" appears only when `can_redo`.
  - **Item passes:** excess passes to print (`ip_service.to_print`; prints once, and a failed print is cancelled via `print_failed`), damaged goods passes (`cs_service.passes_for_branch` and `print_pass`), and passes already printed.
  - **Report damage:** a number box with a contains-match completer from `report_numbers`, a product filter, a grid ("Can report", "Damaged pieces"), a reason, "Send for approval", and a "Sent" list with status and the rejection reason or CS number.
- **`ReceivedPage`** (needs `receiving.review`):
  - Tabs: To check, Completed delivery (paged), By receiver (`by_receiver`). Search is debounced 300 ms. There is one branch filter button per branch, each showing a count.
  - "Make debit memo" and "Make claim stub" show if the user has `dm.issue` and `can_issue` allows it. The reopen button reads "Reopen for recount" or "Allow one more count" (`reopen_label`) and needs `receiving.reopen`.
  - Detail panes:
    - lines (`po_lines`, with a "Left warehouse" column when the Central Warehouse counted);
    - notes on transit loss and boxes (`boxes_check`);
    - count history (`po_history`);
    - one person's lines (`review_lines`).

## Money and numbers

No centavo arithmetic in `receiving.py`: quantities only, in thousandths.
- `received_qty_m` is set to `(got + ppu//2) // ppu`, that is agreed pieces converted to units, rounded half up (`_apply_totals`).
- Ordered pieces are `qty_m * pieces_per_unit`.
- Supplier view (`_finish_lines`): `supplier_m = max(received, left_central)` when the warehouse counted. Transit loss (`left_central − received`) is never charged to the supplier.
- Short = `ordered − supplier_m` (`_settled_from`).
- There are no document numbers here. Claim stub numbers (`YYYY-CS-NNNNN`) and amounts (`debit_memos.amount_c`) come from claim_stubs.

## Interactions with other modules

- **Calls:**
  - `orders.write_guard`, `orders.live_charges` and `orders.paid_by`;
  - `warehouse.left_central` and `warehouse.boxes_sent`;
  - `auth.warehouse_login_missing`;
  - `claim_stubs.warehouse_name`;
  - `masters.code_key`;
  - `money.parse_qty` and `money.fmt_qty`.
- **Called by:**
  - `payments` (`of_delivery_complete` gates the check; also `lines_of`, `excess_lines`, `dm_pieces`, `taken_as_delivered`);
  - `orders` (`close` and `can_close` use `progress` and `live_since`; `_refresh_delivery` calls `_apply_totals` after an amendment);
  - `claim_stubs` and `item_passes` (`progress`, `lines_of`, `excess_lines`, `_reachable`);
  - `debit_memos`;
  - `ui/of_page.py` (`delivered_by_po`, `_IN_FORCE`, `of_delivery_complete`);
  - `pdf.py` (`of_delivery_complete`);
  - `sample_data.py`.
- **Settlement.** "Completed" (`_settled_from`) means:
  - SHORT on DMs equals the shortage;
  - DAMAGED on claim stubs equals the agreed damage;
  - no excess is left over.

  Late-damage stubs are left out of `dm_pieces`, so damage found later never moves a PO back to To check.

## Tests

`tests/test_receiving.py` (37 tests), `tests/test_fix_receiving.py` (18), `tests/test_fix2_claims-receiving.py` (5: report lines frozen, extras case-folding, CS pass printing) and `tests/test_after_shipping.py` (3). Rules are also covered in `test_business_rules.py`, `test_claim_stubs.py`, `test_rules_paging_lists.py` and `test_ui.py`. Run them from `apps/desktop`:

```
python -E -m pytest tests/test_receiving.py tests/test_fix_receiving.py "tests/test_fix2_claims-receiving.py" tests/test_after_shipping.py -q -p no:cacheprovider
```

## Gotchas for changing this module

- **Nothing is ever updated or deleted except status and approval fields, and those only forward.** To change a count, write a new count (redo or recount). Don't try to edit `branch_count_items` or `branch_received`; the triggers will abort.
- **The V28 "after agreed" triggers** reject item, damage or box inserts into a count that is already MISMATCH or agreed. In `save_count`, insert the items *before* `_compare` changes the status. A migration that rebuilds `branch_counts` must drop and recreate those guards (see the note at schema.py ~1931).
- **`_apply_totals` must stay inside `orders.write_guard`**, because the PO is past draft.
- **Blind-count leakage is the main risk.** Never add ordered quantity, cost, price, the other person's figure, or "short/complete" wording to `sheet`, `to_count`, `branch_counts`, `count_lines`, `report_lines` or `reports` for receivers. Some things that look like leaks are deliberate:
  - An AGAIN round shows only the lines that didn't match, and says so.
  - The Item passes tab shows excess pieces after agreement.
- **Negative `po_item_id` means an extra line.** Joins against `purchase_order_items` silently drop extras. Damage on an extra line isn't required to be charged for "Completed" (`_settled_from` checks only PO lines), and extras can't be reported as later damage (the FK on `claim_request_items`).
- **`approved_by` is "matched with", not an approver.** On the first count it holds the second counter's id, and the second count holds the first counter's id.
- **A MISMATCH round still writes `branch_received` for agreed lines**, but those rows only count once the attempt fully agrees (`_IN_FORCE`). Don't sum `branch_received` without `_IN_FORCE`.
- **The ADMIN branches in `_reachable`, `find_pos`, `to_count` and `count_lines` can't be reached**, because ADMIN never holds `receiving.count` and receivers can't hold other roles. Keep it that way. Don't grant Admin counting.
- **`branch_counts()` only filters when `user.branch_code` is set.** A receiver without a branch would see every branch. `_set_branch` prevents that today, but `to_count` returns `[]` in that case while `branch_counts` does not.
- **"Boxes received" can show in the wrong round.** The UI hides it only when `need_label == AGAIN`. For the *second* count of a later round, the label is SECOND, so the field shows (it's optional there). `boxes_counted` then includes that later-round figure, which can make `boxes_check` report a box disagreement that isn't real.
- **Take the write lock first.** Keep reads of `_state` and `MAX(attempt_no)` inside `tx()` (already done), because the file may be on a network share.
