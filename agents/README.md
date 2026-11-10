# Atlas PO ERP agents

AI agents for the Atlas PO ERP purchasing program (`apps/desktop/`, PySide6 + SQLite), made for a business with a central warehouse and several branches. Some play each role in that business and run the program from start to finish in a throwaway database; others review the code; one supervises the whole and guards changes.

The agent definitions are in `.claude/agents/`. Claude Code finds them there. Each file states what the application does, its architecture in brief, the business rules in force, the agent's responsibilities and tables, which modules it may inspect, whom it works with, and what it must never change without the owner. How they work together, who owns which files, and the change protocol are in [COORDINATION.md](COORDINATION.md).

## Supervision

| Agent | Job title | Area |
|---|---|---|
| `chief-architect` | Chief Systems Architect & UI/UX Quality Director | Screenshots of every screen (several sizes, both themes), redundancy (true vs role-specific vs convenience), UX proposals with a business reason, impact analysis before every change and independent review after, CRITICAL/HIGH/MEDIUM/LOW/OPTIONAL reports, gatekeeping to the owner. Changes no code |

## Role agents (test runs)

| Agent | Job title | Program role |
|---|---|---|
| `purchaser` | Purchasing Staff | BUYER |
| `supplier` | Supplier's Sales Representative (simulated) | none, never logs in |
| `management` | Operations Manager (Approver) | APPROVER |
| `warehouse-receiver` | Warehouse Receiver, Central Warehouse. The Warehouse Head has the same role | WAREHOUSE (login alone) |
| `branch-receiver` | Branch Receiver, counter 1 or 2 (run per branch and counter) | RECEIVER (one branch) |
| `accounting` | Accounting Staff (Payables) | ACCOUNTING |
| `auditor` | QA Auditor | reads only |

Each one's tasks are in its file, in order.

## Code reviewers

These don't play a role in a run. Each owns one question about the code, with no overlap, and reports findings, changing nothing. A problem outside its area is handed to the owner named in COORDINATION.md.

| Agent | Job title | Owns the question | Main files |
|---|---|---|---|
| `reviewer-ordering` | Ordering and Documents Auditor | Is the right thing ordered, approved, numbered and printed? | masters, order_forms, orders, delivery_receipts, pdf, reports |
| `reviewer-receiving` | Receiving and Warehouse Auditor | Is every piece where the program says, counted by the rules? | warehouse, receiving, item_passes, goods side of claim_stubs and debit_memos |
| `reviewer-payments` | Payments and Money Auditor | Is every peso right, and nothing paid too early? | payments, money, selling, pricing in debit_memos, money side of claim_stubs |
| `reviewer-access` | Access and Data Safety Auditor | Can only the right person do it, and is the record safe? | auth, every require(), main_window roles, db, schema, backup |
| `reviewer-screens` | Screens and Usability Reviewer | Can each person use the screen without confusion or lost work? | presentation in atlaspo/ui, theme |

Ask for a review with: "Run the five reviewers on the whole system, then the Chief Architect on their findings."

Ask before a change with: "Chief Architect: impact analysis for <change>." Nothing is implemented until the owner approves (see COORDINATION.md, section 2).

## Running a scenario

In Claude Code, from the repo root:

> Run the scenario in agents/scenarios/happy-path.md with the agents.

The main Claude session coordinates: it creates `agents/runs/<run-id>/`, sets `PO_DATA_DIR` and `PO_SAMPLE_DIR` to folders inside it, sets up the users in that database, hands each step to the right agent, then runs the auditor.

## Folders

- `scenarios/`: one file per test run, in order of steps.
- `runs/`: throwaway databases and logs, one folder per run. Not in Git.
- `reports/`: auditor reports. Not in Git.

Never point an agent at the real database (`Documents\Atlas PO ERP`) or the sample folder (`Documents\Atlas PO ERP Sample`).
