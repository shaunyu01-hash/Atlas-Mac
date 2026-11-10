# Scenario: inventory, forensic

Each role works its normal day in the inventory system, then tries to break the rules it is held to. Every result is checked in the database
and the audit log, not only on screen. Each agent runs in its own throwaway database built from the sample data (`sample_data.build`), with
`PO_DATA_DIR` and `PO_SAMPLE_DIR` set to fresh folders under `agents/runs/<run-id>/<agent>/`.

## What every agent does

1. **Normal work.** Do your role's inventory tasks (see your agent file) on the sample data, as yourself.
2. **Loopholes.** Try each way your role could get around a rule (below). Use the program's services only, with your own login. A refusal
   is a pass; anything that goes through is a finding.
3. **Forensics.** After each step, read the database (read-only SQL is allowed for checking, never for writing) and confirm:
   - stock moved exactly as the rule says, at the right place, in the right bucket, by the right amount;
   - an audit row was written in the same step, naming you;
   - nothing you shouldn't see came back from any service (book quantities, costs, other branches, the other counter's figures).
4. **Report.** For each finding: severity (CRITICAL, HIGH, MEDIUM, LOW), what you did, what happened, what should have happened, and the
   exact calls to repeat it. Also list every loophole you tried that held.

## Loopholes to try, by role

- **Branch receiver:** sale at ₱0 or far below price to hide missing stock; return to stock for sale without approval; backdate a sale before
  a count; cancel or decide your own paper; see the books or costs through any service; act at another branch; count twice in one round;
  learn the other counter's figures; count in your own transfer; send stock out and have it count in short to "lose" it; import the same POS
  file twice; negative or huge quantities; a product from another department on a department count.
- **Warehouse receiver:** act at a branch; see books or costs; receive a transfer sent by yourself; count in a transfer while the warehouse is
  being counted.
- **Purchaser:** approve your own adjustment; post a count; make the same goods into two draft OFs; order from an inactive supplier; change a
  product's pieces per unit after it has stock; change a cost to move stock value; see blind figures before agreement.
- **Management:** approve your own adjustment or sale; post a count you counted; cancel sales a later count took in; cancel your own paper;
  edit a decided paper; post with a transfer on the way.
- **Accounting:** any write in inventory; anything that changes a supplier's check amount through inventory or catalog cost.
- **Forensic auditor:** recompute stock for every place and product from the raw tables (receipts in force, claim stubs, item passes, debit
  memos, sales, inventory movements) independently of `inventory.MOVES`, and compare with `inventory.on_hand`; try direct SQL tampering
  (update, delete, insert) on every inventory table and confirm the triggers refuse it; confirm every write has an audit row.
