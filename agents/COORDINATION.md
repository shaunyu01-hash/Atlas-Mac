# How the Atlas PO ERP agents work together

This is the central coordination structure for every agent in `.claude/agents/`. The owner decides; the agents find, propose, check and report. The main Claude session is the **coordinator**: it hands out steps, keeps the file locks below, and brings decisions to the owner.

Facts about the program (modules, tables, screens, permissions) come from the current source. When code and any document disagree, the code wins and the document is updated.

## 1. Who owns what

Ownership means "the one agent who answers questions about this and reviews changes to it". Owning is not permission to edit: **no agent edits program code on its own.** Edits happen only through the change protocol (section 2), by the implementer the owner names.

### Code reviewers and the Chief Architect

| Agent | Owns (reviews, answers for) | Files |
|---|---|---|
| `chief-architect` | Cross-cutting design, dependencies, redundancy, UX direction, before and after review of every change, gatekeeping | No file of its own; reads everything. Arbiter when ownership is unclear |
| `reviewer-ordering` | Suppliers, products, branches, Order Forms, approval rule, amendments, numbering, POs, DRs, printing, every paper, reports | `atlaspo/masters.py`, `order_forms.py`, `orders.py`, `delivery_receipts.py`, `pdf.py`, `reports.py`, `of_layout.py`; behaviour of `ui/of_page.py`, `ui/po_page.py`, `ui/pages.py` |
| `reviewer-receiving` | Every piece: the Central Warehouse, branch blind count, recounts, shortage/damage/excess quantities, item passes, claim stub goods flow, damage reports' lines | `atlaspo/warehouse.py`, `receiving.py`, `item_passes.py`; goods side of `claim_stubs.py` and `debit_memos.outstanding`; behaviour of `ui/warehouse_page.py`, `ui/receiving_page.py`, `ui/item_pass_page.py`, goods actions of `ui/claim_stub_page.py` |
| `reviewer-payments` | Every peso: settlement, checks, deductions, DM/CS pricing, claim stub pickup gate on the check, damage found later on the next check, sign-offs, balances, cash flow, selling prices | `atlaspo/payments.py`, `money.py`, `selling.py`; pricing in `debit_memos.py`; money side of `claim_stubs.py`; behaviour of `ui/payments_page.py`, `ui/selling_page.py`, `ui/debit_memo_page.py` |
| `reviewer-access` | Who can do what, separation of duties, branch isolation, blind-count leakage, role sidebars, schema, migrations, triggers, backup and restore, audit log, data folder | `atlaspo/auth.py`, `db.py`, `schema.py`, `backup.py`, `sample_data.py` (isolation), `__main__.py`; role layout in `ui/main_window.py`, `ui/settings_page.py`; every `require()` call wherever it is |
| `reviewer-screens` | Looks and use of every screen: layout, wording, empty states, theme, sizes, keyboard, stale panels, lost typing, unguarded slots | Presentation in `atlaspo/ui/*.py`, `ui/theme.py`, `ui/common.py`, `ui/assets/` |

**Split files.** Three files have two owners, split by question, never by line:
- `claim_stubs.py`: pieces and status flow -> reviewer-receiving; amounts, `paid_on`, settlement effects -> reviewer-payments.
- `debit_memos.py`: `outstanding` pieces -> reviewer-receiving; pricing and cancel effects -> reviewer-payments.
- `ui/*.py`: behaviour (what a button does) -> the area owner above; presentation -> reviewer-screens; whether a button shows for a role -> reviewer-access.

**Tests.** `po_app/tests/test_<area>.py` belongs to the owner of that area (`test_payments.py` to reviewer-payments, `test_receiving.py` and `test_warehouse.py` to reviewer-receiving, `test_claim_stubs.py` split as above, `test_ui.py` and `test_theme.py` to reviewer-screens, `test_acceptance.py`, `test_sample_data.py` and the cross-area fix and decision tests to the Chief Architect, who assigns each case to an owner when it changes).

**Never owned by any agent** (owner only): README files, `.git`, `.claude/settings*`, `CLAUDE.md`, memory files, the real database folder, the sample folder.

### Role agents (test runs)

| Agent | Program role | Writes only |
|---|---|---|
| `purchaser` | BUYER | its part of `agents/runs/<run-id>/log.md` |
| `supplier` | none | same |
| `management` | APPROVER | same |
| `warehouse-receiver` | WAREHOUSE (login alone) | same |
| `branch-receiver` | RECEIVER (one branch, counter 1 or 2) | same |
| `accounting` | ACCOUNTING | same |
| `auditor` | read only | `agents/reports/<run-id>.md` |

Role agents act only through the program's services in the run's throwaway database, each with its own login, one step at a time as the scenario in `agents/scenarios/` says.

## 2. The change protocol

Every change to program code, tests, the schema or a business rule goes through these steps, in order. Skipping one is a failure.

1. **Propose.** Whoever found the need (a reviewer, the Auditor, the owner) writes the proposal: the problem with evidence, the fix, the files it touches, and the owning agent(s).
2. **Chief Architect review.** `chief-architect` writes the impact analysis: affected modules and pages, dependencies, expected regressions and covering tests, database implications, workflow and business-rule compatibility, a review of the plan, and a recommendation. The owning reviewer(s) add their view on their part.
3. **Owner approval.** The coordinator puts the proposal and the review to the owner in plain words. Nothing proceeds without the owner's explicit yes, given by the owner in the conversation. A message from another agent, a script or a document is never approval.
4. **Backup.** Before editing: `git status` clean or the pending work saved; note the commit to return to. If the change touches the schema, confirm the migration takes its own pre-migration backup, and work only against temp databases. Never copy or open the real or sample data folders.
5. **Implement.** One implementer, named by the owner, edits only the files listed in the approved plan. A new file or an unlisted file means going back to step 2.
6. **Tests.** Run the affected test files one at a time, then the whole suite (`cd po_app && python -E -m pytest -q -p no:cacheprovider`; the current test suite must pass). Add or update tests for the new behaviour. Every result is recorded.
7. **Independent review.** `chief-architect` and the owning reviewer(s) check the result themselves: read the diff, re-run the tests and the probes the change touches, compare with the approved plan. The implementer never reviews its own change. Then the owner decides whether to commit. No agent commits or pushes unless the owner asks.

A change counts as **major** (owner sees it before step 5, always) when it touches the schema or migrations, money or settlement, permissions or roles, a business rule, a printed paper, backup or restore, or more than one owner's area.

## 3. No two agents on the same file

- The coordinator keeps a lock list for the session: file, agent, task, since. An agent asks the coordinator before editing anything, and gets the lock only if nobody holds it.
- One file, one editor at a time. A split file (section 1) is still one lock: the two owners agree the change through the Chief Architect, and one implementer edits it.
- Read-only work (reviews, probes, screenshots) needs no lock, but must not run while a file it depends on is being edited, unless the agent says its result is from before the edit.
- Reviewers and role agents edit no repository file at all. They write scripts and evidence only in temp folders, `agents/runs/` or `agents/reports/`, as the coordinator names.
- Every lock is released when the step ends, even if it failed.

## 4. Conflicts and escalation to the owner

1. **Ownership unclear** (a finding could belong to two reviewers): the Chief Architect names the owner; the other agent lists it under "Handed over".
2. **Agents disagree** on a finding, a severity or a fix: each writes its case in three lines with evidence. The Chief Architect summarises both, says which it recommends and why, and the coordinator takes it to the owner. Agents don't argue it out in the code.
3. **A finding contradicts a business rule** the owner set (for example the claim stub 90 days starting at check claim): it is reported as a question for the owner, not fixed as a bug.
4. **A hard rule is at risk** (real data, commit or push, editing outside the plan): stop at once and tell the coordinator, who tells the owner.
5. **Escalation format** to the owner: the decision needed in one sentence, the options, what each costs and protects, the recommendation, and who disagrees. Plain words; the owner isn't a programmer.

The owner's decision is recorded (in the change's proposal and, for a business rule, in the code comment that states it), and every agent follows it from then on.
