# Atlas PO ERP (purchasing)

An offline Windows program for purchasing. An **Order Form (OF)** holds one whole supplier order across branches. Once approved, it generates one **Purchase Order (PO)** per branch. It needs no internet connection.

| Document | Who gets it | What it's for |
|---|---|---|
| **OF, office copy** | Accounting | Costs, cost per piece, totals, and any sticker deduction to settle on delivery |
| **OF, warehouse copy** | Warehouse, signed by the supplier | Checking the complete delivery and each branch's quantity, with the costs the final payment is based on |
| **PO, one per branch** | Supplier | The reference for their Delivery Receipt (DR). Delivers to the main warehouse, for that branch, showing only that branch's quantities |
| **Cut slips, four to a page** | Each branch | Cut apart and sent ahead so the branch knows which PO is being delivered to it. Shows no product, quantity or price |
| **Blind receiving sheet, one per branch** | Branch warehouse | What to look for, with a blank to write the pieces received. Shows no quantity or price. Two receivers (R1 and R2) each count it without seeing the other's figures; only a count both agree on is recorded |

## Install and open

**On this PC:** double-click **Atlas PO ERP** on the desktop. It runs from source, using the Python already installed.

**Another PC, or a standalone program:** run `Build EXE.cmd` (close the program first; it needs `pip install pyinstaller`), then copy the whole `dist\Atlas PO ERP` folder and double-click `Atlas PO ERP.exe`. It also builds `dist\Atlas PO ERP (Sample Data)`, the practice program. The program is 64-bit, so it won't run on a 32-bit PC.

**From source:** `pip install -r requirements.txt`, then `python -E "Atlas PO ERP.pyw"`.

**Run the tests:** `python -E -m pytest tests -q`.

**Updating to a new version:** nothing to do. The first time the new version opens, it saves `po-before-migration-...sqlite` in the backup folder, then updates the database in one step. If anything goes wrong, the database stays exactly as it was.

## Sample data

Double-click **Atlas PO ERP (Sample Data)** on the desktop to practise on a separate database with invented suppliers, products and about 14 months of orders. Log in as `owner`, the master account, with the password `123`. It can do everything, including approving what it prepared. `admin`, `approver`, `purchaser`, `purchaser2` and `viewer` use the same password, for trying other roles. `owner` and `approver` can approve. It also has one invented closed branch, to show how an inactive branch keeps its history.

- **Password:** `123` is for the sample only. Logins made in the program still need at least 8 characters.
- **It's separate:** it lives in `Documents\Atlas PO ERP Sample` and never touches your real database, whose numbers can't be reused. Its window title, sidebar and every PDF say sample data.
- **Start over:** close the program and delete that folder. The next opening builds it again.
- **Receiving:** two receivers per branch, `nor1` and `nor2` for North, `sou1` and `sou2` for South, `eas1` and `eas2` for East, `wes1` and `wes2` for West. Received shows deliveries that are complete, with damaged pieces, short, over, split in two, missing a product, recounted, not matching after two rounds, and waiting for a second count. A few printed POs are left uncounted to practise on.
- **Selling prices:** a few are changed by hand, so you can see them beside the automatic ones.
- **`.exe`:** `Build EXE.cmd` builds its own, `Atlas PO ERP (Sample Data).exe`, using the same practice folder.

## First use

1. **Create the administrator.** The first screen asks for this person. They're also the approver until you choose someone else. No default password exists.
2. **Fill in company details and the logo** under Settings, Company. They print on both documents.
3. **Add each branch's delivery address** under Branches. It prints on that branch's POs.
4. **Tick the approvers** under Settings, Approval policy, after adding users under Settings, Users. Any one of them can approve. Whoever prepared an OF can't approve it, so a business with one person needs a second approver or the exception setting.
5. **Add suppliers and products** under Suppliers and Product catalog. Products also join the catalog on their own when an OF that uses them is approved.

## Daily work

| Task | How |
|---|---|
| What to do next | **Orders** lists every order, and each says its **Next step**. Select one and press the button at the top right; it does that step. One click on an order shows its branch POs on the same page. A double-click or Enter opens the full OF or PO, for its rows, amending or cancelling |
| Start an order | Orders, New order form. Choose the supplier, then a **department tab**, then add its products from the catalog or type them. Enter makes the next row in that department, so nobody types the department. The catalog offers only that department. **All** shows the whole order, grouped by department after saving. Totals always cover the whole order |
| Rearrange the OF screen | Administrators only: Settings, Order Form layout. Drag the fields and columns, then Save layout. It applies to everyone, and never changes the data or printed documents |
| Old drafts | A draft not saved in over 7 days shows **Over 7 days old** in the list and a reminder when opened. It's only a warning: nothing is cancelled or removed. Saving it restarts the count |
| Allocate to branches | Type a quantity under each branch. Enter moves along the branches, then to the next row. Blank or zero means no PO for that branch |
| Find an order | Orders has tabs: **Open** for work not yet printed, **Completed**, **Cancelled** and **All**. Each tab shows its count, and search and filters apply to all of them. Searching a PO number opens its order and lands on that PO. Choosing a branch shows only that branch's POs. A list of POs alone is the PO Register report. A PO is Completed once it's issued. An OF is Completed once all its POs that weren't cancelled are issued. Its **Printed** column shows the date the last one was printed, or progress such as 1 of 4 while some are left |
| Dark or light | **Dark mode** or **Light mode** at the bottom of the sidebar. It switches at once, keeps anything you've typed, and is remembered for your Windows user. PDFs always print light |
| Keys | Ctrl+Shift+N new OF, Ctrl+N new row, Ctrl+S save draft, Enter next branch, Delete clears a cell |
| More fields | More columns adds second discount, net unit, order quantity check, supplier code, brand, box, selling price, foreign currency, remarks |
| Check against a quote | Under More columns, type the supplier's total in Order qty. Total turns red and saving is refused until the branches add up to it |
| New SKUs | When an OF is approved, every SKU it doesn't already know joins the Product catalog under that supplier, with no extra step. Drafts and rejected OFs add nothing. A row with no SKU stays on that OF only, and an existing product is never overwritten. Each SKU belongs to one supplier, so another supplier's SKU is refused when the OF is saved |
| Find a product | Product catalog lists by supplier. The supplier box shows just that supplier |
| Stickers | On a supplier, tick Deduct stickers and set the rate per piece, ₱0.50 unless changed. The OF then shows the deduction and what's left to pay. The office OF prints it based on what was ordered, with spaces to write the pieces received and the final deduction. The warehouse OF shows the deduction too, because the supplier signs it. It never changes the OF total, the approval check or the supplier's PO. Once receiving exists, it can settle this from the warehouse count |
| Dates | A date left as Not set opens its calendar on this month. An arrow key or a typed number starts from today. Delete sets it back to Not set |
| Submit | Save and submit. A draft with several departments becomes one OF per department, each with its own number and branch POs. Approval is decided on the whole order's total: at or below the limit they're approved and numbered at once, and above it each waits for the approver |
| Approve or reject | Approvals, or inside the OF. A rejected OF returns to Draft with the reason showing |
| Make the POs | Print the approved OF from **Print**, To print. That makes and issues its branch POs, and it can only happen once. Orders' Go to Print takes you there |
| Count a delivery (branches) | **Receiving** in the sidebar. Press the PO in the list, or type the OF or PO number from the slip, then type the pieces counted for each product, 0 if it didn't arrive, including damaged ones. In Damaged, type how many of those are damaged, or leave it blank for none. Only that branch's products show, with no quantities or prices |
| Second count | A second receiver does the same on their own, without seeing the first count. Receivers can only ever see their own numbers. If the two match on every product and on damaged, the delivery is recorded and the PO updates. There's no approving |
| Counts that don't match | Only the products that differ come back, for two people to count again. Nothing reaches the PO until the whole delivery agrees. After two rounds that don't match, Received tells the home office to check |
| Second delivery of a PO | Type its number. An accepted PO leaves the list, short or complete, so the list can't tell a receiver that a PO came up short |
| Redo your own count | While your count waits for the second receiver, **Count mine again** lets you redo it, once. After that another receiver has to count |
| Recount an accepted delivery | Only Admin or an Approver can allow it, with a reason, and only once per delivery. It's refused once the PO is paid or has a debit memo, claim stub or item pass that hasn't been cancelled. Two receivers then count it again, and the new figures take over once they agree. The earlier counts stay on record for good |
| See differences (home office) | **Received** in the sidebar. **To check** holds what's still being counted, or differs from the order, or has damaged pieces. **Completed delivery** holds what arrived exactly as ordered, ready for the next step. Both show ordered against received, and every person's numbers for every count. The **By receiver** tab shows each person's counts that didn't match and products they got wrong, meaning they counted something other than the figure two people later agreed on |
| Make a debit memo | On **Received**, **To check**, select a PO that's short and press **Make debit memo**. The pieces fill in at the PO's net cost. Lower or clear any you don't want to charge, then **Approve and print**. Your name is recorded as approving it, it gets a permanent number and that exact PDF is kept. Once everything is settled, the PO moves to **Completed delivery** |
| Make a claim stub | For damaged goods, on **Received**, **To check**, select the PO and press **Make claim stub**. It works like a debit memo, and the supplier collects the goods with it |
| Follow damaged goods | **Claim stubs** in the sidebar, by where the goods are. The warehouse presses **Goods arrived** when they reach Central Warehouse from the branch. The supplier's 90 days start once they've received the check that took the claim stub off. The supplier picks up the claim stub at the office, and **Collected** records who took the goods. After 90 days, **Forfeit, for sale** marks them ready to sell |
| Find or cancel a debit memo | **Debit memos** in the sidebar. Print again, save the external DM number, or cancel with a reason. Management can see who approved each one. A cancelled memo keeps its number and the PO goes back to **To check** |
| Excess delivery | Extra pieces aren't charged. They show on **To check** as **Excess delivery: to return**, and **Mark excess returned** records who sent them back, which moves the PO on. If a product arrives that isn't on the PO, a receiver presses **Add product not on the PO** while counting. It doesn't join the catalog, and the other receiver sees it without the number and counts it too |
| Follow a check | **Payments**. **Future checks** are printed and dated ahead, and **Unclaimed** checks have reached their date and the supplier hasn't taken them. **Mark claimed** records when the supplier took one, and it moves to **Claimed**. **Mark cleared** records when the bank paid it, from the statement, and it moves to **Cleared**. **Cash flow** shows the cash needed by each date, for every check that hasn't cleared and checks still to make |
| Add an accounting user | Settings, Users, New user. Tick Accounting. They see only Payments, Received, Debit memos and Claim stubs |
| Add a receiver | Settings, Users, New user. Tick Receiver and choose the branch. Each receiver needs their own login. A receiver sees only the Receiving screen |
| See who counted | Counted by and Matched with show each person's chip: their initials on the branch's color for first or second receiver. Hover shows the full name, and **Received**, **By receiver** shows both. Initials and color come automatically. To change them: Settings, Users, Edit user |
| Selling prices | **Selling prices** in the sidebar lists every product on an approved OF, per piece. The automatic price is a 30% gross margin on the net cost per piece, then up to a multiple of ₱10. Stickers play no part. Type over a price to change it, clear it to go back. The price on the order shows beside it for comparison. **Forfeited goods** prices claim stub and item pass goods the supplier didn't take back, the same way |
| Print in one go | **Print** in the sidebar. Tick the approved Order Forms, or Select all, then Print and issue. They open as one PDF to print once: each warehouse OF, then its cut slips and branch POs, and DRs for suppliers with a sticker deduction. POs not yet made are generated, and all are issued. An OF already part printed prints only the POs left. An OF or PO that can't go is listed and left out; the rest still print |
| Email a DR | **Print**, DRs to email. Press Save DRs and start email: it saves one file for the order and opens a new message to the supplier. Attach the file, send it, then press Mark as sent. A PO amended and issued again comes back to this list |
| Find or reprint a DR | **Print**, All DRs. Print DR prints it here for any supplier. Another delivery makes a new DR number for a further delivery on the same PO. Orders and Inquiry also find a DR number when you search, and have a DR number column under Columns |
| Look up old orders | **Inquiry** in the sidebar finds numbered OFs and POs by number, supplier, SKU or product, department, branch, status or date. Click one to see its products on the same page. Nothing there changes anything; Open goes to the full document |
| Print again in one go | **Print**, Printed tab. Tick and Print again. Each is recorded as a copy printed again |
| Final OF | Only needed when deductions changed what the supplier signed. It prints from **Payments** only: under Checks to make before the check, and as a copy on the check's tab after |
| Pay a finished order | **Payments**. A delivery that matches its order goes straight to Checks to make. Others wait under Check the DRs: compare the paper and press DRs match. If one DR's amount is different, press DR differs and type only that one. Under Checks to make, print any Final OF first, which moves that check up to Ready for checks. Tick the checks, print them by check date, then press Record and type the first and last numbers. To print a check again, cancel it first from **Future checks** or **Unclaimed**. A DR lower than the program's figure is paid as the DR says, at once. A DR higher than ours that you accept, or a check for a different amount, waits for Admin or an Approver to press **Sign off**, and someone other than the person who entered it has to sign |
| Term discount | On a supplier, the % and the days it applies within. The check is then due on those shorter days, and the discount prints on the OF the supplier signs |
| One copy | On the OF, Warehouse OF PDF |
| Paper count form, if a branch might lose internet | Receivers count in the program. Before printing, tick **Add paper count forms** on Print for two blank forms per branch. Get them to the branch ahead of time. They type the counts in when the program is back, and nothing is recorded until then |
| Print something again | Anyone can, after a question. The copy prints with **COPY 2. PRINTED AGAIN**, the date and their name at the top of every page, the lists show **(2x)**, and it's recorded. That covers POs, the OF copies, debit memos and claim stubs, including Save PDF and older issued versions. The kept file never changes. A Preview, marked as one, isn't counted |
| Look something up | **Inquiry** shows nothing until asked. **Find an OF** waits for a number, supplier or product. **All OFs** lists everything, with filters. **OFs by date** waits for a date. The list shows the supplier code, with the name on hover and on the line under the list |
| Choose the columns on a list | **Columns** on the page, or right-click the column headings, on every main list. Tick to show, untick to hide. Orders and Inquiry also offer Supplier name, Delivery date, Terms, Departments, Branches, Products, Prepared by, External PO, Remarks, Delivered and one column per branch. Each login keeps its own choice on that PC. **Back to the usual columns** undoes it |
| Send to supplier | Open the branch PO, Print and issue. This opens the PDF to print, records who and when, keeps that exact PDF, and moves the PO to Completed |
| Check before issuing | Preview shows a copy marked PREVIEW. NOT YET ISSUED. It doesn't complete anything |
| Print again | Print again opens the original issued PDF. Issued versions lists every revision |
| Accounting system cross-reference | Type the number in External PO number and press Save external number |
| Change an OF, POs not made yet | Amend on the OF. The number stays and the revision goes up |
| Change a quantity after POs exist | Amend on that branch's PO. The OF follows. An issued PO goes back to Approved and to the Open tab until you issue the new revision; the earlier version is kept |
| New price or new product after POs exist | Create a new OF |
| Stop an order | Cancel OF cancels it and all its POs. Cancel PO stops one branch. Close PO ends one that won't complete. All need a reason |

## Rules the system enforces

- **OF numbers:** `2026-OF-00001`, assigned at approval, restarting each January, and never reused.
- **PO numbers:** `2026-2-00001` is year, branch code, sequence. Each branch counts separately per year. Never reused.
- **Supplier codes:** up to 4 letters or numbers.
- **Payment terms:** choose 7D, 30D, 45D, 60D, 90D or None, or type your own, on suppliers and on documents. Days are always written the same way, so `120 days` becomes `120D`. Anything else, such as `COD`, stays as typed, up to 60 characters. Documents already issued keep what they printed.
- **Branches:** a new database starts with 2 North, 4 South, 6 East and 8 West; rename or add your own under Branches. Codes are permanent. Only active branches appear on new OFs. Inactive ones keep all their history, and an administrator can reactivate them.
- **Approval:** strictly above the limit by default; an administrator can change it to "At the limit or above" in Settings. It's compared with the **total OF across all branches**, so a big order can't slip through as small POs. POs inherit the OF's approval. An administrator can change the limit, who approves and the basis, or turn it off. Several people can be approvers, and any one of them is enough. History is always kept.
- **Cost:** net = unit cost x (1 - Disc 1) x (1 - Disc 2). Discounts compound; they don't add.
- **OF total:** always the exact sum of its branch POs. An overall discount and other charges are shared across branches by merchandise value. VAT is worked out per branch.
- **Units:** `PC`, `3 IN 1`, `4 IN 1`, `6 IN 1` or `1 DOZEN`. Quantities are always pieces. Enter the cost per pack or dozen and the program works out the cost per piece, so 30 of `3 IN 1` at 90.00 is 10 packs, ₱900.00, or 30.00 each. A quantity that isn't a whole number of packs is refused. Documents made earlier keep the units they had. "Box of" is a label for larger cartons.
- **VAT:** no order carries VAT for now, so there's no VAT choice on screen. **VAT registered** stays on the supplier. An order saved with VAT before then keeps it.
- **Never on a supplier's PO:** other branches' quantities, internal and warehouse notes, approval comments, selling price, foreign currency rate and cost.
- **One department per OF:** a percent discount carries to each OF, and an amount off or other charges are shared by value, to the centavo. An approved OF can't take on another department. OFs made before this rule keep the departments they have.
- **Departments on paper:** the warehouse OF and the supplier PO print a header for each department with its subtotals. Documents already issued keep what they printed.
- **Delivery status:** comes from the POs. Recording a DR won't mark anything received once receiving exists; only warehouse counts will.

## Roles

| Role | Can |
|---|---|
| Administrator | Everything, including settings, users, branches, backup and restore |
| Purchaser | Create, submit, print and issue, amend, cancel and close; edit products; set selling prices; approve debit memos and claim stubs; record collected and forfeited goods. No payments, and suppliers are Admin only |
| Accounting | Payments and checks. Can see Received, Debit memos and Claim stubs. No ordering. An amount that differs waits for Admin or an Approver to sign it off |
| Warehouse | Central Warehouse: boxes for each branch, pieces stickered for goods that come without stickers, and Mark shipped. Goods arrived at the warehouse for claim stubs and item passes. Nothing else |
| Receiver | Count deliveries at their own branch, blind. Only receivers count, not Admin |
| Approver | Approve or reject, if ticked as an approver under Settings, Approval policy. Sign off payment amounts that differ, and cancel debit memos, claim stubs and item passes |
| View only | See everything, change nothing |

## Backups

**Automatic:** one copy a day when the program opens and when it closes, kept for 30 days by default. Manual copies are never removed automatically.

**Where:** the `backups` folder beside the database, or the folder you choose under Settings, Backup and data. Choose another drive: a backup on the same drive is lost with that drive.

**Back up now:** Settings, Backup and data, Back up now.

**Restore:** Settings, Backup and data, then Restore selected or Restore from a file. The file is checked first, and the current data is saved as `po-before-restore-...` before anything is replaced. An older backup is brought up to date as it's restored. You then log in again.

**Health check:** Check database health confirms the file is sound, every PO's total matches its rows, each OF's quantities match its allocations, each OF's total matches its POs, and the number counters are right.

## Where the data is

`Documents\Atlas PO ERP`: `po.sqlite` (the database, including every issued PDF), `backups`, `attachments`. To use another folder, set the `PO_DATA_DIR` environment variable.

## Code map

| Path | Holds |
|---|---|
| `atlaspo/money.py` | All arithmetic, using Decimal, whole centavos and thousandths of a unit |
| `atlaspo/schema.py` | Tables, database-level protections, and the migrations |
| `atlaspo/receiving.py` | Two blind branch counts, comparing them, recounts, and the home office's results |
| `atlaspo/debit_memos.py` | Debit memos for what's short, from agreed counts |
| `atlaspo/claim_stubs.py` | Claim stubs for damaged goods, and following them until collected or forfeited |
| `atlaspo/selling.py` | Selling prices of purchased items, for the barcode stickers |
| `atlaspo/order_forms.py` | The OF lifecycle, approval, numbering, PO generation, OF amendments |
| `atlaspo/orders.py` | The PO lifecycle after generation: issuing, versions, amendments |
| `atlaspo/masters.py`, `auth.py` | Suppliers, branches, products; users and roles |
| `atlaspo/pdf.py`, `reports.py`, `backup.py` | The printed documents, the reports, backup and restore |
| `atlaspo/sample_data.py` | Builds the separate practice database through the real rules |
| `atlaspo/ui/` | The screens. `of_page.py` holds the allocation grid |
| `tests/` | Calculations, the acceptance tests, the migration, and screens driven automatically |
