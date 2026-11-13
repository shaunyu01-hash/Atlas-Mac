# Atlas PO ERP on a Mac

The desktop program runs on any Mac with macOS 12 or newer, on Apple silicon (M1 and later) or Intel. The sample version opens a complete invented business, with suppliers, products, 14 months of orders, deliveries, counts, debit memos and checks, so it is ready to present.

## Option 1: the Mac app (recommended for presenting)

### Get the app (once)

1. On GitHub, open the repository's **Actions** tab, choose **Mac app** and click **Run workflow**. It takes about 10 minutes.
2. Open the finished run and download **Atlas-PO-ERP-Mac-apple-silicon** (M1, M2, M3, M4 and later) or **Atlas-PO-ERP-Mac-intel** (older Macs). If you're not sure, open  > About This Mac: "Chip: Apple M…" means Apple silicon.
3. Unzip it. It holds two zips: **Atlas PO ERP (Sample Data)** is the presentation copy. Unzip that one too, then drag **Atlas PO ERP (Sample Data).app** into Applications.

Pushing a tag such as `v1.0` also attaches the zips to a GitHub release, which gives a plain download link.

### First opening on each Mac

The app isn't signed with an Apple developer account, so macOS stops it the first time:

1. Double-click the app. macOS says it can't check it. Click **Done** (or **OK**).
2. Open **System Settings > Privacy & Security**, scroll down to the message about Atlas PO ERP, and click **Open Anyway**. Confirm with your Mac password.
3. If the Mac asks whether the app may use your **Documents** folder, click **Allow**. That's where the sample data is kept.

Faster alternative: in Terminal, run `xattr -dr com.apple.quarantine "/Applications/Atlas PO ERP (Sample Data).app"`, then double-click the app.

The very first start builds the sample business, so the window takes about 10 seconds to appear. Later starts are immediate.

## Option 2: run it from this folder (needs Python)

1. Install Python 3.12 or newer from python.org.
2. Copy or clone the repository to the Mac, open `apps/desktop/mac` in Finder and double-click **Start Atlas PO (Sample Data).command**. If macOS blocks it, right-click it and choose **Open**, or use **Open Anyway** as above.
3. The first start downloads what the program needs (a few minutes, needs internet). Later starts take seconds.

## Logging in for the presentation

Every sample login uses the password `123`.

| Login | Who | Shows |
|---|---|---|
| `owner` | Owner, master account | Everything, including approving their own OF |
| `purchaser`, `purchaser2` | Buyers | Entering an OF, printing and issuing POs, debit memos, claim stubs |
| `approver` | Operations manager | Approving or rejecting OFs, signing off differences |
| `warehouse` | Central Warehouse | Recording supplier deliveries and shipping to branches |
| `nor1` and `nor2` (also `sou`, `eas`, `wes`) | Branch receivers | The blind double count: each sees only their own branch, never the ordered quantity |
| `accounting` | Accounting | Comparing DRs, checks, cash flow, supplier balances |
| `viewer` | Read-only | Looking without changing anything |

A good order for a demo: `purchaser` makes and submits an OF, `approver` approves it, `purchaser` issues the POs, `warehouse` records the delivery, `nor1` and `nor2` count it blind, then `accounting` works out the check. A few issued POs are left uncounted on purpose, so the count can be shown live.

## Good to know

- **Keyboard:** shortcuts shown as Ctrl work with ⌘ Command on a Mac (⌘S saves, ⌘N adds a line, ⌘O opens, ⌘⇧N starts a new OF, ⌘↩ saves a stock count).
- **Printing:** every printed paper opens in Preview, where it can be printed or shown full screen.
- **Start the sample over:** quit the app and delete the folder **Documents/Atlas PO ERP Sample**. The next start builds a fresh copy.
- **Separate from real data:** the sample never touches a real database. The window title, the sidebar and every PDF say it is sample data.
- **Building on a Mac yourself:** in `apps/desktop`, run `bash mac/build_mac.sh`. The apps appear in `dist`, and copy-ready zips in `dist/mac`.
