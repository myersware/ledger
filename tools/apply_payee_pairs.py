"""Apply approved rows (approve=y) from data/payee-pairs-review.csv.
  python3 tools/apply_payee_pairs.py            # preview only
  python3 tools/apply_payee_pairs.py --apply    # dated backup, then merge, one edit_log batch per row
Batch ids go to data/backups/payee-pairs-batches.txt; undo one with: python3 app.py --revert <batch>"""
import csv, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core, names
rows = [r for r in csv.DictReader(open("data/payee-pairs-review.csv")) if r["approve"].strip().lower() in ("y", "yes")]
for r in rows: print(f"{r['group_id']:>4}  {r['target_name']}  <=  {r['variant_names']}")
print(len(rows), "approved groups")
if "--apply" not in sys.argv or not rows: sys.exit()
print("backup:", core.backup("pre-payee-pairs"))
with open("data/backups/payee-pairs-batches.txt", "a") as f:
    for r in rows:
        out = names.pay_merge(None, dict(names=[n.strip() for n in r["variant_names"].split(" | ")], into=r["target_name"].strip()))
        f.write(f"{out['batch']}\t{r['group_id']}\t{r['target_name']}\n")
print("done; batch ids in data/backups/payee-pairs-batches.txt")
