---
name: update-context
description: Bring the project's agent documentation (CLAUDE.md files, apps/desktop/docs/*.md module docs, READMEs) in line with the code changes on the current branch. Run it before every `git push` and before every `gh pr create` (a hook blocks both until it has run for the current commit), and whenever the user says "update context", "update the docs" or "/update-context".
---

# /update-context

Future agents rely on the docs to understand a module without reading all of its code. A doc that describes yesterday's code is worse than no doc, because it is trusted. This skill keeps them true after every change.

Run it after your code commits and before you push or open a PR. It is quick when nothing doc-relevant changed: check, record, move on.

## 1. Find what changed

```bash
BASE=$(git merge-base HEAD origin/main 2>/dev/null || git merge-base HEAD main)
git diff --name-only "$BASE"...HEAD      # committed on this branch
git status --porcelain                   # uncommitted work (commit or stash it first)
```

If `HEAD` equals `BASE` (pushing straight to `main`), use the commits that are not on the remote yet: `git log --name-only @{u}..HEAD`.

Read the diff itself (`git diff "$BASE"...HEAD -- <path>`) for each changed source file. File names alone are not enough to tell whether a rule changed.

## 2. Map changed files to docs

| Changed path | Docs to check |
|---|---|
| `apps/desktop/atlaspo/order_forms.py`, `of_layout.py`, `ui/of_page.py` | `apps/desktop/docs/order-forms.md` |
| `apps/desktop/atlaspo/orders.py`, `delivery_receipts.py`, `ui/po_page.py` | `apps/desktop/docs/purchase-orders.md` |
| `apps/desktop/atlaspo/warehouse.py`, `ui/warehouse_page.py` | `apps/desktop/docs/warehouse.md` |
| `apps/desktop/atlaspo/receiving.py`, `ui/receiving_page.py` | `apps/desktop/docs/receiving.md` |
| `apps/desktop/atlaspo/debit_memos.py`, `claim_stubs.py`, `item_passes.py` and their `ui/*_page.py` | `apps/desktop/docs/differences-dm-cs-ip.md` |
| `apps/desktop/atlaspo/payments.py`, `selling.py`, `ui/payments_page.py`, `ui/selling_page.py` | `apps/desktop/docs/payments.md` |
| `apps/desktop/atlaspo/db.py`, `schema.py`, `auth.py`, `money.py`, `backup.py`, `__main__.py` | `apps/desktop/docs/platform.md` (plus every module doc whose tables, permissions or migrations changed) |
| `apps/desktop/atlaspo/masters.py`, `pdf.py`, `reports.py`, `sample_data.py` | `apps/desktop/docs/masters-pdf-reports.md` |
| `apps/desktop/atlaspo/ui/main_window.py`, `common.py`, `pages.py`, `theme.py`, `settings_page.py` | `apps/desktop/docs/ui-shell.md` |
| `apps/desktop/tests/**` | the matching module doc's `## Tests` section; test count in `apps/desktop/CLAUDE.md` if it moved by more than ~50 |
| New desktop module or screen, moved files, new commands or gotchas | `apps/desktop/CLAUDE.md` (layout, commands, module-doc index) and a new or extended module doc |
| User-visible desktop behaviour, roles, rules, backups | the matching section of `apps/desktop/README.md` (`## Daily work`, `## Rules the system enforces`, `## Roles`, `## Backups`) |
| `apps/api/**` | `apps/api/CLAUDE.md` |
| `apps/web/**` | `apps/web/CLAUDE.md` |
| `packages/**` | `packages/CLAUDE.md` |
| `agents/**`, `.claude/agents/**` | `agents/README.md`, `agents/COORDINATION.md` |
| Workspace layout, root commands, glossary, cross-cutting rules, ported features | root `CLAUDE.md` (`## Known gaps` when a desktop rule is ported to TypeScript), root `README.md` |

A file that matches no row (for example a lockfile or an asset) usually needs no doc change.

## 3. Update the docs

For each doc you picked, open it next to the diff and fix what the change made false or incomplete:

- function, class and file names, and `file.py:line` citations that moved far
- statuses and transitions, business rules and where each is enforced
- permission strings and which roles hold them
- tables, columns, triggers, and the migration list (`MIGRATE_V{n}`, `SCHEMA_VERSION`)
- money formulas and rounding
- screens: what each role sees, actions, keyboard behaviour
- test files and commands
- new gotchas you hit while making the change. This is the most valuable thing you can add.

Rules for the edit:

- Verify each statement against the code as it is now. Don't document intent or plans.
- Change only what the diff affects. Don't rewrite or restyle sections you didn't need to touch.
- Keep each doc's existing structure and its dense, plain style. Use purchasing words (OF, PO, DR, supplier, branch).
- A new module or screen gets its own doc in `apps/desktop/docs/` (same headings as the others) and a row in the module-doc table in `apps/desktop/CLAUDE.md` and in step 2 of this skill.
- Never put secrets, real customer data or `.env` values in a doc.
- If the code looks wrong against a documented rule, don't "fix" the doc to match the bug. Report the mismatch to the user.

## 4. Check the links

Every relative link and backticked path you added must exist:

```bash
git diff --name-only "$BASE" -- '*.md' | xargs grep -ohE '\]\([^)#]+' | sed 's/](//' | sort -u
```

Resolve each against the linking file's folder and confirm it exists.

## 5. Commit and record

If any doc changed, commit only the docs:

```bash
git add -- '*.md'
git commit -m "docs: update context for <short summary of the change>"
```

Then record that context is current for this commit. The pre-push hook reads this file:

```bash
git rev-parse HEAD > "$(git rev-parse --git-dir)/update-context-head"
```

Record it even when no doc needed changing; that is a valid outcome of this skill. Record it only after the docs commit, never before, and never without doing steps 1–3: the hook trusts this file.

## 6. Report

Tell the user in two or three lines which docs you updated (or that none needed it) and any code-versus-doc mismatches you found. Then continue with the push or PR you were about to make.
