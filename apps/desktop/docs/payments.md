# Payments and selling prices

The last step of the flow (order → approval → warehouse → blind branch counts → differences → **supplier checks**). Once every live PO of an approved OF has a delivery the branch receivers agreed on, the order lands on the Payments screen. Accounting compares the supplier's DRs only when something about the order was not clean, works out the check (live PO totals less debit memos, claim stubs, uncharged shortage, stickers, term discount and any lower DR), groups everything a supplier is due on the same Saturday into one check, records the number of a check printed **outside** the program, and follows it printed → claimed → cleared. Amounts that differ from the program's figure need a written reason and a sign-off by someone else (Admin or Approver). A per-supplier running balance carries overpayments (credit) and underpayments ("We owe") to the next check. Selling prices (a separate "Sales" page) give each purchased piece, and forfeited claim-stub / item-pass goods, an automatic SRP that a purchaser may override.

## Files

| File | Role | Key public functions / classes |
|---|---|---|
| `atlaspo/payments.py` | All payment rules (no Qt). Module docstring states the rules. | `settlement`, `order_row`, `orders_in_payment`, `checks_due`, `record_check`, `record_checks`, `settle_by_credit`, `cancel_check`, `claim_check`/`undo_claim`, `clear_check`/`mark_cleared_many`/`undo_clear`, `drs_match`, `save_dr_entries`, `save_dr_lines`, `dr_lines`, `dr_total`, `line_amount`, `accept_difference`, `sign_off_difference`, `sign_off_check`, `supplier_balance`, `supplier_credit`, `agreed_deduction`, `credit_without_orders`, `owed_without_orders`, `late_claims`, `paid_back_memos`, `cash_flow`, `cash_flow_by_month`, `checks`, `orders_on_check`, `dr_differs`, `needs_final_of`, `final_of_due`, `waiting_signoffs`, `next_check_number`, `check_numbers`, `one_look`, `CheckDifference` |
| `atlaspo/selling.py` | Selling prices for approved OF lines and forfeited goods. | `list_items`, `set_price`, `forfeited_items`, `set_forfeit_price`, `item_key`, `round_up` |
| `atlaspo/ui/payments_page.py` | Payments screen. | `PaymentsPage` (l.959), `DrPanel` (l.58), `CheckDialog` (l.562), `BatchCheckDialog` (l.615), `ClaimDialog`, `ClearDialog`, `ClearTickedDialog`, `DifferenceDialog` (l.856) |
| `atlaspo/ui/selling_page.py` | Selling prices screen. | `SellingPricesPage` |

Helpers used from elsewhere: `debit_memos.amount_c/discount_share/shown_cost/SHORT`, `order_forms.get_of/term_base`, `receiving.lines_of/of_delivery_complete/taken_as_delivered/dm_pieces/excess_lines`, `delivery_receipts.ensure_first`, `money.round_centavos/sticker_deduction/per_piece/selling_from_cost/parse_pesos/parse_qty`.

## Data model

All in `schema.py`; `SCHEMA_VERSION = 28`.

| Table (migration) | Columns of note | Mutability |
|---|---|---|
| `dr_entries` (V17) | `dr_id` PK, `dr_date`, `total_c >= 0`, `entered_by/at` | Upserted and **deleted** by the service (no trigger). History lives in audit log + `dr_line_differences`. |
| `dr_line_differences` (V28) | `dr_id`, `save_no`, `po_item_id` (no FK on purpose), `pieces_m`, `unit_c` (NULL = same as ours) | Append-only (triggers `dr_line_differences_fixed/_no_delete`). Newest `save_no` per DR is current; a set holding one row with NULL `po_item_id` = "no product differs". |
| `payment_differences` (V17, `dr_lower_c` V28) | `of_id` PK, `difference_c` (higher part accepted), `reason`, `accepted_by/at`, `dr_lower_c >= 0` | Upserted by `accept_difference` and `_keep_dr_lower`. |
| `payment_checks` (V17, `expected_c` V28) | `check_number` UNIQUE, `source` EXTERNAL\|PROGRAM, `check_date`, `supplier_id`, `amount_c >= 0`, `credit_applied_c` (>0 credit taken, <0 owed added), `difference_reason`, `status` ACTIVE\|CANCELLED, `cancelled_*`, `expected_c` | Never deleted; number/amount/supplier/date/credit/source/reason/recorder/expected fixed; CANCELLED is terminal; cancel fields fixed once set. |
| `payment_check_ofs` (V17) | `(check_id, of_id)` PK, `amount_c` = the order's `pay_c` at recording | No update/delete. Insert refused if check cancelled, already logged (`CHECK_RECORD`/`CHECK_CREDIT` audit row exists) or claimed/cleared, or if the OF already has another ACTIVE check (`check_orders_paid_once`). |
| `check_claims` (V18) / `check_clears` (V19) | PK `check_id`; `claimed_on`, `claimed_by_name` / `cleared_on` | Rows **deleted** by `undo_claim` / `undo_clear` (no trigger; audit keeps previous values). |
| `payment_signoffs` (V23) | `kind` DIFFERENCE\|CHECK, `ref_id` (of_id or check_id), `amount_c`, `requested_by/at`, `signed_by/at` | No delete; once signed nothing changes; only `signed_by/at` may be filled. Latest row per (kind, ref_id) counts. |
| `late_damage` (V26) | `cs_id` PK, `reason`, `found_by/at`, `paid_before` (check id) | Append-only; `paid_before` must name an existing check (V28 trigger). Written by `claim_stubs`. |
| `debit_memos.paid_back_check_id` (V28) | live check that had paid the OF when the DM was cancelled | Set only in the same update that cancels an ISSUED memo; then fixed. Written by `debit_memos.cancel`. |
| `selling_prices` (V8) | `(of_id, item_key)` PK, `price_c > 0`, `set_by/at` | Only hand prices; row deleted to go back to automatic. |
| `forfeit_prices` (V23) | `(kind CS\|IP, item_id)` PK, `price_c > 0` | Same pattern. |
| `purchase_orders.payment_status` | always `UNPAID` | V28 triggers forbid the program from ever changing it. Payment state is derived, not stored. |

**Order stage** (`order_row`, payments.py:745-759), evaluated in this order: `PAID` (has ACTIVE check) → `NOT_DONE` (delivery not complete, or a live PO without an APPROVED branch count) → `DM_FIRST` (`unclaimed_c > 0`) → `ENTER_DRS` (order has a reason to compare (`_why_to_compare`) and not every live PO has a typed DR) → `DIFFERENCE` (higher DRs not accepted at the current figure) → `SIGN_OFF` (accepted, sign-off missing or for a different figure) → `TERMS` (terms give no date) or `TO_PAY`. A clean order (no `why`) skips straight to `TO_PAY`.

**Check bucket** (`check_bucket`, `checks`): `CLEARED` if cleared (or settled by credit) → `CLAIMED` if claimed → `FUTURE` if `check_date > today` else `DUE` ("Unclaimed"). Transitions: record → claim (blocked while sign-off pending) → clear (claimed first, date ≥ claimed date). Undo clear → undo claim → cancel (cancel refused once claimed). Cancel puts the orders back on the list.

## Rules enforced

Permissions (`auth.PERMISSIONS`): `payment.view` = ADMIN, ACCOUNTING, APPROVER, VIEWER · `payment.edit` = ADMIN, ACCOUNTING · `payment.signoff` = ADMIN, APPROVER · `price.edit` = ADMIN, BUYER. `auth.role_conflict` refuses BUYER+ACCOUNTING on one login.

1. **Nothing is paid before both branch receivers agreed on every live PO.** `counted_by_branches`, `orders_in_payment` (SQL `HAVING`), `_ready_rows` → NOT_DONE error. POs closed before receiving or received outside the program are paid outside the program.
2. **Shortage needs a debit memo before payment** (DM_FIRST). `order_row`, `_ready_rows`. The purchaser issues it (`dm.issue`).
3. **Clean orders need no DR typing.** `_why_to_compare` lists reasons (deductions, PO cancelled/closed/amended, no count, short, lost in transit, damaged, excess). Empty list → TO_PAY directly.
4. **DRs are compared per PO, beyond a ₱1 tolerance** (`TOLERANCE_C = 100`) so an over at one branch can't hide an under at another (`order_row` l.707-717).
5. **A lower DR is paid at the DR amount immediately, no acceptance, no sign-off** (`_dr_lower`, `_lower_part`). Once a check pays the order, the lower part is frozen in `payment_differences.dr_lower_c` (`_keep_dr_lower`) so later counts never take it off again.
6. **A higher DR needs Accounting's written reason (`accept_difference`, `payment.edit`) and a sign-off by someone else (`sign_off_difference`, `payment.signoff`); the program's lower figure is still paid.** If figures change later the acceptance no longer matches (`agreed = {higher, off}`) and it must be redone. Sign-off is refused if any PO lacks a count.
7. **Nobody signs off their own figures.** `_sign` refuses `requested_by == user.id`. Only a request made by an ADMIN login is signed at creation (`_ask_signoff`); ACCOUNTING+APPROVER on one login still needs another person.
8. **A check for a different amount (>₱1 either way) needs a reason** (`CheckDifference` raised by `record_check`) **and a sign-off** before it can be claimed (`claim_check` → `check_waiting`).
9. **A lower check with a reason, once signed off, is final** (`agreed_deduction`): the gap is not carried as "We owe". Unsigned, or cancelled, it is carried. Needs `expected_c`; checks from before V28 without it get none.
10. **Check numbers: digits only, ≤20 digits, never reused, even after cancel** (`_check_number`, UNIQUE column, `record_check`). `CREDIT-` prefix reserved.
11. **One check pays one supplier; an order has at most one live check** (`_ready_rows`, `_link_orders`, trigger `check_orders_paid_once`). Orders on a check are fixed once logged.
12. **A recorded check never changes; re-printing = cancel with a reason, then record a new one** (`cancel_check`, triggers). Cancel refused after claim.
13. **Settle by credit** when the supplier's credit covers the orders (`expected <= TOLERANCE_C`): a PROGRAM check `CREDIT-00001…`, amount 0, counts as CLEARED, always asks for a sign-off (`settle_by_credit`).
14. **DRs can't change once an order has a check** (`save_dr_entries`, `save_dr_lines`), and DR dates can't be in the future. Other modules use `orders.refuse_if_paid` to block changes to a paid order.
15. **Claimed/cleared dates not in the future; cleared not before claimed** (`claim_check`, `_clear_day`, `_clear`). A post-dated check may be claimed before its date.
16. **Amounts above ₱100 billion are refused** (`MAX_AMOUNT_C = 10**13`, `_amount`); negative DR totals/prices refused.
17. **Reads are gated by `payment.view` when a user is passed**; `user=None` means the program itself (`_may_view`). `dr_lines` and `receipts_to_enter` require `payment.edit`.
18. **Selling prices**: only for APPROVED OFs (`set_price`) or FORFEITED CS/IP lines (`set_forfeit_price`); > 0; rounded **up** to ₱10 (`round_up`); `price.edit`. Prices under SRP (manual only) or under cost are flagged, not refused (`_flag`).

## Main flows

- **Compare DRs (ENTER_DRS).** `drs_match(user, of_id)` (`payment.edit`): stage must be ENTER_DRS; `receipts_to_enter` (creates missing first DRs via `delivery_receipts.ensure_first`, i.e. it writes); fills the first DR of each untyped PO with that PO's `delivered_c` and suggested date via `save_dr_entries`; audit `DR_ENTRY` per DR + `DRS_MATCH`.
- **One DR differs.** `dr_lines` → screen; `save_dr_lines(user, of_id, {dr_id: (date, {po_item_id: (pieces, unit)})})`: blank = ours; values equal to ours are dropped; total = `dr_total` = `base_c` (PO `delivered_c` less other DRs' typed totals) + Σ(their line amount − ours). Writes `dr_entries` upsert + new `dr_line_differences` set; audit `DR_LINES`. All products back to ours → `DR_ENTRY_CLEARED` and the entry is deleted. `save_dr_entries` (total typed alone) replaces any product set; audits `DR_ENTRY` / `DR_ENTRY_CLEARED`.
- **Accept higher difference.** `accept_difference(user, of_id, reason)`: stage DIFFERENCE, reason required; upserts `payment_differences` (higher part, `dr_lower_c`); audit `PAYMENT_DIFFERENCE`; `_ask_signoff("DIFFERENCE")` → `PAYMENT_SIGNOFF_ASKED` (or `PAYMENT_SIGNOFF` if Admin).
- **Sign off.** `sign_off_difference` (stage SIGN_OFF, all POs counted) / `sign_off_check` (check ACTIVE) → `_sign` (`payment.signoff`, not own) → audit `PAYMENT_SIGNOFF`.
- **Checks to make.** `checks_due` groups TO_PAY orders with a due date by (Saturday, supplier); the supplier balance goes on the earliest group first: credit = min(balance, amount), owed = −balance; `check_c = amount − credit + owed`.
- **Record check.** `record_check(user, of_ids, number, date, amount_text, reason="", groups=None)`: validate number/amount (>0)/date; works out `_ready_rows` + `_expected` before the write lock and repeats inside only if `PRAGMA data_version` changed (AUD-146); refuses a used number; `CheckDifference` if off by >₱1 with no reason; inserts `payment_checks` (EXTERNAL, `expected_c`), then `_link_orders` (insert `payment_check_ofs`, `_keep_dr_lower`) **before** audit; sign-off asked if different; audit `CHECK_RECORD` per order. `record_checks` = batch, all-or-none, with `checks_due` worked out once.
- **Settle by credit.** `settle_by_credit(user, of_ids)` → PROGRAM check, amount 0, `credit_applied_c = total`, audit `CHECK_CREDIT` per order, sign-off asked.
- **Lifecycle.** `claim_check` (`CHECK_CLAIM`), `undo_claim` (reason, `CHECK_UNCLAIM`), `clear_check` / `mark_cleared_many` (`CHECK_CLEAR`), `undo_clear` (reason, `CHECK_UNCLEAR`), `cancel_check` (reason, `CHECK_CANCEL` per order). All `payment.edit`.
- **Selling.** `set_price(user, of_id, key, price_c|None)` → upsert/delete `selling_prices`, audit `SELLING_PRICE` on `order_form`. `set_forfeit_price(user, kind, item_id, price_c|None)` → `forfeit_prices`, audit `FORFEIT_PRICE` on `claim_stub`/`item_pass`.

## Screens

**Payments** (`PaymentsPage`): built only if `payment.view` (main_window.py:212); sits in the Purchasing/Accounting sidebar group. BUYER and RECEIVER never see it. Sidebar shows `Payments (n)` for `payment.signoff` holders (`waiting_signoffs`). Tabs: *Check the DRs* (stages DM_FIRST, ENTER_DRS, DIFFERENCE, SIGN_OFF, TERMS), *Checks to make* (sections "Print the Final OF first" vs "Ready for checks", tick boxes for batch), *Future checks*, *Unclaimed*, *Claimed* (tick to clear many), *Cleared* (hidden until searched or "Show all cleared"), *Cancelled checks*, *Cash flow* (By date / By month). One primary + one second button per selection (`_show_action` l.1468, `_primary` l.1617, `_second` l.1678): DRs match / The other DRs match, Accept the difference, DR differs (opens `DrPanel` on the right), Sign off, Print Final OF, Record N checks (`BatchCheckDialog`: first or last number, numbers run on from `next_check_number`), Record check (`CheckDialog`, warns when lower that the gap becomes final), Settle by credit, Mark claimed, Mark (N) cleared, Undo claim, Undo cleared, Cancel check (Danger, spaced away). Edit buttons need `payment.edit`; Sign off needs `payment.signoff`; "Print Final OF again" is the only action open to view-only users. Breakdown line under the list (`_breakdown`) lists each deduction, late claim stubs (`late_claims`) and paid-back memos (`paid_back_memos`). Keyboard: search debounced 300 ms; Esc closes the DR panel; in dialogs Enter presses the action, never Cancel (`_buttons`); DR product grid edits on any key. All refresh reads run inside `payments.one_look`.

**Selling prices** (`SellingPricesPage`): every non-limited login (not RECEIVER-only, ACCOUNTING-only or WAREHOUSE-only). Tabs *Bought* (`list_items`, max 2000 rows) and *Forfeited goods* (`forfeited_items`). Only the Selling price column is editable and only with `price.edit`; type to change, clear to revert (`_save` → `set_price` / `set_forfeit_price`). "Only below SRP" filter; below-SRP red, below-cost red bold. Services take no user for reading, so no view gate.

## Money and numbers

Per order, `_settlement` (payments.py:257-287), all integers in centavos; pieces in thousandths (`_m`); basis points (`_bp`):

```
total_c      = Σ live PO grand_total_c                      (incl. other charges and VAT on top)
charged_c    = Σ ISSUED debit memo total_c + Σ non-cancelled claim stub total_c (live POs)
per line:    short = max(ordered_m − supplier_m, 0)  (0 if taken_as_delivered)
             on_memo = min(SHORT pieces on ISSUED DMs, short); back = min(paid-back DM pieces, short − on_memo)
             unclaimed_c = amount_c(short − on_memo) − amount_c(back)
             amount_c(p) = round_half_up(p × net_unit_c × kept / (pieces_per_unit × 1000 × whole))   (discount_share of the PO)
delivered_c  = total_c − short_dm_c − unclaimed_c − paid_back_c          (what DRs are compared to, per PO)
sticker_c    = round(Σ min(supplier_m, ordered_m)/1000 × OF.sticker_rate_c)   (every piece, any pack)
term_c       = round(term_base(delivered_c, Σ other_charges_c, Σ VAT_c of EXCLUSIVE POs, cs_c) × term_discount_bp / 10000)
dr_lower_c   = Σ per PO (delivered_c − typed DRs) where > ₱1   (frozen once paid)
pay_c        = total_c − charged_c − unclaimed_c − sticker_c − term_c − dr_lower_c
changed      = pay_c ≠ OF.net_payable_c or any PO cancelled   → Final OF needed
```

No VAT or EWT is computed here; VAT is whatever is in the PO totals. DR line amounts with a typed unit price: `round(pieces_m × unit_c / (pieces_per_unit × 1000))` (`line_amount`; unit is per ordered unit, after overall discount).

**Check date** (`order_row` l.720-744): start = latest over live POs of the last Central Warehouse `received_at` (`central_day`), else the latest typed DR date, else the last agreed count date (`_last_count_day`). Days = supplier `term_discount_days` when the OF has `term_discount_bp` and days, else `terms_days(payment_terms)` (`"30D"`→30, `""`/`"None"`→0, anything else → None = "typed by hand", treated as 0 and flagged). Due = `next_saturday(start + days)` (a Saturday maps to itself). Overflow → stage TERMS.

**Expected check** (`_expected`): Σ `pay_c` − credit from that `checks_due` group (or from `supplier_balance` if no group). **Supplier balance** (`_supplier_balance`): Σ ACTIVE checks' `amount_c` + Σ agreed deductions − Σ current `pay_c` of orders on ACTIVE checks; |x| ≤ ₱1 → 0. Positive = credit off the next check, negative = owed added. This is how damage found later (a new claim stub lowers `pay_c` of a paid order) and memos cancelled after payment (`paid_back_c` raises it) reach the next check automatically.

**Cash flow** (`cash_flow`): per check date, uncleared ACTIVE checks (printed vs claimed; settled-by-credit excluded as CLEARED) + checks still to make (`check_c`); counts waiting-sign-off and date-passed unclaimed checks; running total.

**Selling** (money.py:117-127, selling.py): `cost_c = round(net_unit_c / pieces_per_unit)` from the OF item; `auto_c = ceil(cost_c / 0.70)` up to a multiple of ₱10 (`SELLING_COST_SHARE = 70`, `ROUND_SELLING_C = 1000`); hand price rounded up to ₱10; `margin = (price − cost) / price`. Stickers play no part.

## Interactions with other modules

- Calls: `order_forms.get_of/term_base`, `receiving.*` (counts, completeness, DM pieces, excess), `debit_memos` helpers, `delivery_receipts.ensure_first`.
- Called by: `pdf.py` (`settlement` for the Final/office OF; `OF_PRINT_OFFICE` audit stores `pay_c`, read back by `final_of_due`), `ui/po_page.py` (`dr_differs`), `ui/main_window.py` (`waiting_signoffs`), `sample_data.py`.
- Reads its tables: `claim_stubs.paid_on/_paid_on_many` (claim-stub pickup and 90-day clock start when the paying check is claimed, or on the date of a credit settlement; for late damage, the first later check); `claim_stubs` writes `late_damage.paid_before` via `orders.paid_by`; `debit_memos.cancel` writes `paid_back_check_id`; `orders.paid_by/refuse_if_paid` block changes to paid orders.

## Tests

`tests/test_payments.py` (main), `test_fix_payments.py`, `test_fix_payments_ui.py`, `test_rules_money.py` (agreed deduction, lower DRs, paid-back memos, AUD-146 locking), `test_rules_dr_lines.py` (DR products + panel), `test_fix2_payments-passes.py`, `test_review_fixes.py`, `test_business_rules.py`, `test_audit_fixes.py`, `test_rules_discount.py` (discount share used for shortage values), selling in `test_ui.py`, `test_fix_dm_ip.py`, `test_acceptance.py`, `test_sample_data.py`. From `apps/desktop`:

`python -E -m pytest tests/test_payments.py -q -p no:cacheprovider` (swap the file name; never the whole suite for a small change).

## Gotchas for changing this module

- **Never overpay.** Every new deduction must reduce `pay_c` in `_settlement`; `supplier_balance` derives from it, so changing `pay_c` for already-paid orders silently moves money onto the next check. That is intended for late claim stubs and paid-back memos, but anything else that alters a paid order's figures does the same.
- `_link_orders` must run **before** the `CHECK_RECORD`/`CHECK_CREDIT` audit row: trigger `check_orders_once_recorded` keys on that audit row.
- `one_look` memoises in a module global. Never save inside it; `record_check` only uses it before taking the lock.
- `record_check`'s pre-lock working-out relies on `PRAGMA data_version`; keep rollback journal (no WAL).
- The "Final OF first" gate is **UI-only** (`_section` in payments_page); `record_check` does not check `final_of_due`. Likewise the check date typed in `CheckDialog` is not validated against `due`, and the term discount is taken regardless of the date entered.
- `check_claims`, `check_clears`, `dr_entries`, `payment_differences`, `selling_prices`, `forfeit_prices` are mutable by design (undo/upsert/delete); the permanent record is the audit log, `dr_line_differences`, `payment_signoffs` and `payment_checks`.
- `receipts_to_enter` writes (creates first DRs) even though it looks like a read.
- Terms typed by hand (COD, down payment) give due = next Saturday after the start day, flagged only in the breakdown text.
- `unclaimed_c` is deducted in `pay_c` even though DM_FIRST blocks payment while it is > 0: it is a safety net for figures that move after payment.
- A settlement by credit counts as CLEARED and opens the claim-stub pickup (`claim_stubs.paid_on`) on its date even while its sign-off is still pending.
- `set_forfeit_price` lacks `set_price`'s int-type check and ₱100-billion cap; a huge value would surface as a SQLite overflow, not a `POError`.
- The selling cost uses the OF line's `net_unit_c` and does not apply a PO overall discount (`discount_share`), unlike shortage valuation in payments.
- Role names, permissions and the sign-off rule are the spec for the TypeScript port; do not weaken `_sign`'s self-sign refusal.
