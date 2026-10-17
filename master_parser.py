"""Parse master vendor Excel/CSV files using header & column keyword lists."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from column_keywords import (
    COLUMN_DETECTION_KEYWORDS,
    HEADER_DETECTION_KEYWORDS,
    HEADER_MATCH_THRESHOLD,
    VENDOR_DATA_COLUMNS,
)


def _normalize(text: Any) -> str:
    if text is None or (isinstance(text, float) and pd.isna(text)):
        return ""
    value = str(text).strip().lower()
    value = value.replace("\n", " ").replace("\r", " ")
    value = re.sub(r"\s+", " ", value)
    return value


def _cell_matches_keyword(cell: str, keyword: str) -> bool:
    kw = _normalize(keyword)
    if not cell or not kw:
        return False
    return kw in cell or cell in kw


def detect_header_row(rows: List[List[Any]], max_scan: int = 30) -> Optional[int]:
    """Return index of the first row that looks like a header, or None."""
    limit = min(len(rows), max_scan)
    best_idx = None
    best_score = 0

    for idx in range(limit):
        cells = [_normalize(c) for c in rows[idx]]
        score = 0
        for cell in cells:
            if not cell:
                continue
            if any(_cell_matches_keyword(cell, kw) for kw in HEADER_DETECTION_KEYWORDS):
                score += 1
        if score >= HEADER_MATCH_THRESHOLD and score > best_score:
            best_score = score
            best_idx = idx

    return best_idx


def map_columns(header_cells: List[Any]) -> Dict[str, int]:
    """
    Map DB field names to column indexes using COLUMN_DETECTION_KEYWORDS.
    Each DB field gets at most one column (first matching keyword wins).
    """
    normalized = [_normalize(c) for c in header_cells]
    used_indexes = set()
    mapping: Dict[str, int] = {}

    for field, keywords in COLUMN_DETECTION_KEYWORDS.items():
        for kw in keywords:
            for col_idx, cell in enumerate(normalized):
                if col_idx in used_indexes or not cell:
                    continue
                if _cell_matches_keyword(cell, kw):
                    mapping[field] = col_idx
                    used_indexes.add(col_idx)
                    break
            if field in mapping:
                break

    return mapping


def _clean_value(value: Any) -> Optional[str]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "nill", "nil", "-"}:
        return None
    # Excel often prefixes account numbers with a leading apostrophe
    if text.startswith("'"):
        text = text[1:].strip() or None
    return text


def _row_to_record(row: List[Any], col_map: Dict[str, int]) -> Optional[Dict[str, Optional[str]]]:
    record = {field: None for field in VENDOR_DATA_COLUMNS}
    has_data = False
    for field, col_idx in col_map.items():
        if col_idx >= len(row):
            continue
        cleaned = _clean_value(row[col_idx])
        record[field] = cleaned
        if cleaned is not None:
            has_data = True
    if not has_data:
        return None
    # Skip rows that are clearly empty of vendor identity
    if not record.get("name") and not record.get("pan") and not record.get("phone"):
        return None
    return record


def parse_master_file(path: Path) -> Tuple[List[Dict[str, Optional[str]]], Dict[str, Any]]:
    """
    Parse an .xlsx / .xls / .csv master file into vendor_data row dicts.

    Returns (records, meta) where meta includes header_row, column_map, sheet.
    """
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix == ".csv":
        df_raw = pd.read_csv(path, header=None, dtype=object)
        sheet_name = path.name
    elif suffix in {".xlsx", ".xls"}:
        xl = pd.ExcelFile(path)
        sheet_name = xl.sheet_names[0]
        df_raw = pd.read_excel(path, sheet_name=sheet_name, header=None, dtype=object)
    else:
        raise ValueError(f"Unsupported master file type: {suffix}")

    rows = df_raw.values.tolist()
    if not rows:
        raise ValueError("Master file is empty.")

    header_idx = detect_header_row(rows)
    if header_idx is None:
        raise ValueError(
            "Could not detect a header row. "
            "Update HEADER_DETECTION_KEYWORDS if the sheet layout is new."
        )

    header_cells = rows[header_idx]
    col_map = map_columns(header_cells)
    if "name" not in col_map and "pan" not in col_map:
        raise ValueError(
            "Could not map Vendor Name / PAN columns. "
            "Update COLUMN_DETECTION_KEYWORDS if headers differ."
        )

    records: List[Dict[str, Optional[str]]] = []
    for row in rows[header_idx + 1 :]:
        record = _row_to_record(row, col_map)
        if record:
            records.append(record)

    meta = {
        "sheet": sheet_name,
        "header_row": header_idx,
        "column_map": col_map,
        "row_count": len(records),
    }
    return records, meta
