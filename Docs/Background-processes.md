# Background processes

## Exact combo dedupe (`vendor_data`)

Deletes near-exact duplicate rows from the database. Does **not** affect the Run Process UI flags (those only highlight matches; they do not delete).

### When it runs

- On app startup (`app.py` → `_run_exact_dedupe_background("startup")`)
- After a successful Master Excel upload (`"after-master-upload"`)
- After Link Documents auto-inserts any rows (`"after-link-docs"`)

Runs in a **daemon thread**. A lock skips a second run if one is already in progress.

### Match rule (all required)

1. **Vendor name must be the same** (normalized; case / spacing ignored).
2. At least **3 other identity fields** must match exactly among: `phone`, `email`, `pan`, `gst`, `msme`, `aadhar`.

Rows without a usable name are skipped. Empty fields do not count as matches.

### What gets kept / deleted

Matching rows are clustered (union-find). **Lowest `id` is kept**; the rest are deleted.

### Code

- Trigger: `app.py` → `_run_exact_dedupe_background`
- Logic: `db_dedupe.py` → `remove_exact_combo_duplicates` (`MIN_OTHER_MATCHING_FIELDS = 3`)
