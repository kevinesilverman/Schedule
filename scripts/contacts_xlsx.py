"""Write data/contacts.enc out as an Excel file for PFS Redux import.
Usage: python scripts/contacts_xlsx.py out.xlsx   (prints base64 of the file size)"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from payload import dec_file
from openpyxl import Workbook
from openpyxl.styles import Font
COLS = ["status","first","last","email","phone","mobile","company","title","address","website","account","first_seen","updated_at","source_subject"]
rows = dec_file("data/contacts.enc") or []
wb = Workbook(); ws = wb.active; ws.title = "Contacts"
ws.append(COLS)
for c in ws[1]: c.font = Font(bold=True)
for r in sorted(rows, key=lambda x: x.get("first_seen",""), reverse=True):
    ws.append([r.get(k,"") for k in COLS])
ws.freeze_panes = "A2"
for col, w in zip("ABCDEFGHIJKLMN", [8,14,16,30,16,16,24,22,30,24,9,20,20,40]):
    ws.column_dimensions[col].width = w
wb.save(sys.argv[1])
print(len(rows), "contacts ->", sys.argv[1], os.path.getsize(sys.argv[1]), "bytes")
