"""Build CSV downloads from vendor_data rows."""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any, Dict, Iterable, List, Tuple

EXPORT_CSV_COLUMNS = (
    "id",
    "sr_no",
    "name",
    "contact_person",
    "address",
    "city",
    "state",
    "pincode",
    "phone",
    "email",
    "pan",
    "gst",
    "msme",
    "rfp_no",
    "bank_name",
    "beneficiary_name",
    "account_no",
    "ifsc_code",
    "aadhar",
    "source_file",
    "source",
    "created_at",
    "updated_at",
)


def _cell_value(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat(sep=" ", timespec="seconds")
    return str(value)


def vendors_to_csv(rows: Iterable[Dict[str, Any]]) -> Tuple[str, str]:
    """
    Return (csv_text, download_filename) for a full vendor_data export.
    """
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=EXPORT_CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _cell_value(row.get(key)) for key in EXPORT_CSV_COLUMNS})

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"vendor_data_{stamp}.csv"
    return buf.getvalue(), filename
