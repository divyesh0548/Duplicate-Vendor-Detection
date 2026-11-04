# Document submission options

## 1. Standard pack (Vendor Document Pack)

- One vendor at a time: enter vendor name + upload MSME / PAN / Aadhaar / other docs.
- AI extracts fields and inserts **one** row (`AI-Extracted`).
- After insert, shows matches against the existing database.

## 2. Document only (quotations)

- Single PDF/image that may contain **several** vendors / quotations.
- AI extracts one row per vendor and inserts them all.
- Duplicate check is **within that document only**; optional button to compare with the full DB.

## 3. Link Documents

- Upload **two or more** files in one go (quotations, PAN, Aadhaar, etc.).
- Finds shared names / phones / emails / GST / PAN / MSME / Aadhaar **across those files only**.
- Auto-inserts a row when **name + phone + email** are all present; other rows can be added with +.
- Optional compare with the database; does not auto-insert every extracted person.
