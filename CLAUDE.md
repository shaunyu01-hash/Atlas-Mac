# Atlas PO ERP

Purchasing system for a Philippine multi-branch business that buys from suppliers through one central warehouse: order → approval → warehouse → blind branch counts → differences → supplier checks. Accuracy beats everything: never overpay a supplier, never lose a record.

Two generations live side by side:
- **Desktop app** (Python, PySide6, SQLite): complete and in production use. It is the source of truth for every business rule.
- **TypeScript stack** (Hono API, React web app, Supabase): early scaffold. Today it covers sign-in, suppliers and custom fields. It will port the desktop rules over time.

## Where to go next

Read only the doc for the area you're touching. Each one is short and links onward.

| Working on | Read |
|---|---|
| Desktop app, business rules, Python | [apps/desktop/CLAUDE.md](apps/desktop/CLAUDE.md), then the one module doc in [apps/desktop/docs/](apps/desktop/docs/) for your feature |
| API routes, auth, OpenAPI | [apps/api/CLAUDE.md](apps/api/CLAUDE.md) |
| Web screens | [apps/web/CLAUDE.md](apps/web/CLAUDE.md) |
| Shared schemas, rules, database schema | [packages/CLAUDE.md](packages/CLAUDE.md) |
| Role-play test agents and scenario runs | [agents/README.md](agents/README.md), then [agents/COORDINATION.md](agents/COORDINATION.md) |
| Who the users are and the UI principles | [PRODUCT.md](PRODUCT.md) |

## Glossary

- **OF** (Order Form): one supplier order across all branches. It is the master document and is approved as a whole.
- **PO** (Purchase Order): one per branch, generated from an approved OF and issued to the supplier.
- **DR** (Delivery Receipt): the supplier's delivery paper, one per issued PO.
- **Blind count**: two receivers at a branch count a delivery without seeing the ordered quantities. Only a count both agree on is recorded.
- **DM** (Debit Memo): charges the supplier for short goods.
- **CS** (Claim Stub): damaged goods held for the supplier to collect.
- **IP** (Item Pass): excess goods sent back from a branch.
- **Check**: the supplier payment, worked out from the agreed counts and followed from printed to claimed to cleared.

## Rules that hold everywhere

- **Money is whole centavos** (`123450` = ₱1,234.50), never floats. Desktop suffixes: `_c` for centavos, `_bp` for basis points, `_m` for thousandths.
- **Separation of duties is a feature.** The preparer is never the approver, and receivers see only their own branch. Never weaken a permission check to make something work; a permission error is a finding to report.
- **Records are append-only.** Audit logs, approvals and issued document versions are never edited or deleted.
- **Secrets never go in tracked files.** Use `.env` files, which are gitignored; `.env.example` files hold placeholders only.
- UI wording uses plain purchasing words (OF, PO, DR, supplier, branch) in a calm, exact tone. Keyboard speed matters.

## Before you push or open a PR

Run the `/update-context` skill ([.claude/skills/update-context/SKILL.md](.claude/skills/update-context/SKILL.md)) after your last code commit and before every `git push` or `gh pr create`. It updates the CLAUDE.md files, module docs and READMEs that your change made stale, commits them, and records the commit. A hook in [.claude/settings.json](.claude/settings.json) blocks pushes and PRs until it has run for the current `HEAD`.

## Commands (repository root)

- `pnpm install`, then `pnpm dev`: API on :8787, web app on :5173. Needs `.env` files; see each app's doc.
- `pnpm typecheck` · `pnpm test` · `pnpm lint` (Biome) · `pnpm build`
- For the desktop app, use the commands in [apps/desktop/CLAUDE.md](apps/desktop/CLAUDE.md). It is not part of the pnpm workspace.

## Known gaps

- The TypeScript stack has not yet ported OFs, POs, receiving or payments. Before porting a rule, read the desktop module and its tests; they are the spec.
- Role names differ between the stacks. Desktop uses `ADMIN`, `BUYER`, `APPROVER`, `ACCOUNTING`, `WAREHOUSE`, `RECEIVER`, `VIEWER`. TypeScript uses lowercase names plus `owner`. Align them when auth is ported.
