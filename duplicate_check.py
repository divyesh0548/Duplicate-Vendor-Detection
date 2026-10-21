"""Find duplicate vendor_data rows by phone, email, GST, and MSME.

Exact-same vendor names are not flagged.
Fuzzy name matching can collapse near-identical vendor names and remove those groups.
"""

from __future__ import annotations

import re
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Sequence


COMPARE_FIELDS = ("phone", "email", "gst", "msme", "pan")

FIELD_LABELS = {
    "phone": "Mobile / Phone",
    "email": "Email",
    "gst": "GST",
    "msme": "MSME",
    "pan": "PAN",
}

# Tunable — covers CSPDCL / Chhattisgarh / AV TECH style near-matches
FUZZY_RATIO_THRESHOLD = 0.72
FUZZY_COMPACT_RATIO_THRESHOLD = 0.88


def normalize_phone(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    digits = re.sub(r"\D", "", str(value))
    if not digits:
        return None
    if len(digits) > 10:
        digits = digits[-10:]
    return digits if len(digits) >= 8 else None


def normalize_email(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip().lower()
    if not text or "@" not in text:
        return None
    return text


def normalize_gst(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = re.sub(r"\s+", "", str(value)).upper()
    if not text or text in {"NULL", "NONE", "NILL", "NIL", "N/A", "-"}:
        return None
    return text


def normalize_msme(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = re.sub(r"\s+", "", str(value)).upper()
    if not text or text in {"NULL", "NONE", "NILL", "NIL", "N/A", "-"}:
        return None
    return text


def normalize_pan(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = re.sub(r"\s+", "", str(value)).upper()
    if not text or text in {"NULL", "NONE", "NILL", "NIL", "N/A", "-"}:
        return None
    return text


NORMALIZERS = {
    "phone": normalize_phone,
    "email": normalize_email,
    "gst": normalize_gst,
    "msme": normalize_msme,
    "pan": normalize_pan,
}


def normalize_vendor_name(value: Optional[str]) -> str:
    """Exact-compare key for vendor names (case/space insensitive)."""
    if not value:
        return ""
    text = str(value).strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def _name_for_fuzzy(value: Optional[str]) -> str:
    """Stronger normalization used only for fuzzy comparisons."""
    if not value:
        return ""
    text = str(value).lower()
    # Drop parenthetical person / site notes: "(Diksha kumari)"
    text = re.sub(r"\([^)]*\)", " ", text)
    text = text.replace("&", " and ")
    # Keep letters/digits/spaces; turn other separators into spaces
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _compact_name(value: str) -> str:
    """Remove spaces so 'A V TECH' ~= 'AV TECH'."""
    return re.sub(r"\s+", "", value)


def _significant_tokens(value: str) -> List[str]:
    stop = {
        "ltd",
        "limited",
        "pvt",
        "private",
        "co",
        "company",
        "and",
        "the",
        "of",
        "division",
        "div",
        "unit",
        "branch",
    }
    return [t for t in value.split() if len(t) > 1 and t not in stop]


def names_fuzzy_match(a: Optional[str], b: Optional[str]) -> bool:
    """
    True when two vendor names likely refer to the same organisation.

    Handles cases like:
      - Chhattisgarh State Power…-Bhojpuri vs …-Mudipar
      - CSPDCL Siltara division vs CSPDCL Tatibandh division
      - A V TECH SERVICES vs AV TECH Services (Diksha kumari)
    """
    na = _name_for_fuzzy(a)
    nb = _name_for_fuzzy(b)
    if not na or not nb:
        return False
    if na == nb:
        return True

    ca, cb = _compact_name(na), _compact_name(nb)
    if ca and cb and (ca == cb or ca in cb or cb in ca):
        return True

    ratio = SequenceMatcher(None, na, nb).ratio()
    if ratio >= FUZZY_RATIO_THRESHOLD:
        return True

    compact_ratio = SequenceMatcher(None, ca, cb).ratio()
    if compact_ratio >= FUZZY_COMPACT_RATIO_THRESHOLD:
        return True

    ta, tb = set(_significant_tokens(na)), set(_significant_tokens(nb))
    if ta and tb:
        overlap = len(ta & tb) / max(len(ta), len(tb))
        # Shared org core with a site/division suffix difference
        if overlap >= 0.6 and len(ta & tb) >= 2:
            return True
        # Short brand codes like CSPDCL + different locality
        shared = ta & tb
        if shared and any(len(t) >= 4 and t.isalpha() for t in shared):
            if overlap >= 0.4:
                return True

    # Shared long prefix before last differing segment (hyphen/site suffix)
    prefix_a = re.split(r"\s*-\s*", na)[0].strip()
    prefix_b = re.split(r"\s*-\s*", nb)[0].strip()
    if prefix_a and prefix_b and len(prefix_a) >= 12:
        if prefix_a == prefix_b or SequenceMatcher(None, prefix_a, prefix_b).ratio() >= 0.9:
            return True

    return False


def _all_names_fuzzy_same(names: Sequence[str]) -> bool:
    """True if every non-empty name fuzzy-matches into one family."""
    cleaned = [n for n in names if n and n.strip()]
    if len(cleaned) <= 1:
        return True
    anchor = cleaned[0]
    return all(names_fuzzy_match(anchor, other) for other in cleaned[1:])


def _distinct_exact_names(rows: Sequence[Dict[str, Any]]) -> List[str]:
    seen = []
    seen_keys = set()
    for row in rows:
        key = normalize_vendor_name(row.get("name"))
        display = (row.get("name") or "").strip() or "—"
        if key not in seen_keys:
            seen_keys.add(key)
            seen.append(display)
    return seen


def _summarize_groups(groups: List[Dict[str, Any]]) -> Dict[str, int]:
    flagged_ids = set()
    counts = {field: 0 for field in COMPARE_FIELDS}
    for group in groups:
        counts[group["field"]] += 1
        for row in group["rows"]:
            if row.get("id") is not None:
                flagged_ids.add(row["id"])
    return {
        "phone": counts.get("phone", 0),
        "email": counts.get("email", 0),
        "gst": counts.get("gst", 0),
        "msme": counts.get("msme", 0),
        "pan": counts.get("pan", 0),
        "group_count": len(groups),
        "flagged_rows": len(flagged_ids),
    }


def find_duplicates(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Flag rows that share phone / email / GST / MSME but do NOT share the same vendor name.

    Each group includes ``fuzzy_name_match`` — True when all names in the group
    are fuzzy-similar (UI can hide these when fuzzy matching is enabled).
    """
    buckets: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
        field: defaultdict(list) for field in COMPARE_FIELDS
    }

    for row in rows:
        for field in COMPARE_FIELDS:
            key = NORMALIZERS[field](row.get(field))
            if key:
                buckets[field][key].append(row)

    groups: List[Dict[str, Any]] = []

    for field in COMPARE_FIELDS:
        for matched_value, matched_rows in sorted(
            buckets[field].items(),
            key=lambda item: (-len(item[1]), item[0]),
        ):
            if len(matched_rows) < 2:
                continue

            # Skip when every row has the same exact vendor name
            exact_names = {
                normalize_vendor_name(r.get("name"))
                for r in matched_rows
                if normalize_vendor_name(r.get("name"))
            }
            # All empty names → treat as same unknown; don't flag
            if len(exact_names) <= 1:
                continue

            ordered = sorted(matched_rows, key=lambda r: r.get("id") or 0)
            name_list = [r.get("name") for r in ordered]
            fuzzy_same = _all_names_fuzzy_same(name_list)

            groups.append(
                {
                    "field": field,
                    "field_label": FIELD_LABELS[field],
                    "matched_value": matched_value,
                    "count": len(ordered),
                    "rows": ordered,
                    "distinct_names": _distinct_exact_names(ordered),
                    "fuzzy_name_match": fuzzy_same,
                }
            )

    default_summary = _summarize_groups(groups)
    fuzzy_filtered = [g for g in groups if not g["fuzzy_name_match"]]
    fuzzy_summary = _summarize_groups(fuzzy_filtered)

    removed_groups = default_summary["group_count"] - fuzzy_summary["group_count"]
    removed_rows = default_summary["flagged_rows"] - fuzzy_summary["flagged_rows"]

    return {
        "total_rows": len(rows),
        "groups": groups,
        "summary": default_summary,
        "fuzzy_summary": fuzzy_summary,
        "fuzzy_removed": {
            "groups": max(0, removed_groups),
            "rows": max(0, removed_rows),
        },
    }


def find_document_vs_db_duplicates(
    doc_rows: List[Dict[str, Any]],
    db_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Compare vendors extracted from one document against other DB rows.

    Flags when phone / email / GST / MSME / PAN match between a doc row and a
    DB row, but only when vendor names differ (case-insensitive by default).
    Groups where all names are fuzzy-similar are marked for the UI toggle.
    """
    doc_ids = {r.get("id") for r in doc_rows if r.get("id") is not None}
    external = [r for r in db_rows if r.get("id") not in doc_ids]

    groups: List[Dict[str, Any]] = []

    for field in COMPARE_FIELDS:
        doc_buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for row in doc_rows:
            key = NORMALIZERS[field](row.get(field))
            if key:
                doc_buckets[key].append(row)

        for matched_value, from_doc in doc_buckets.items():
            from_db = [
                r
                for r in external
                if NORMALIZERS[field](r.get(field)) == matched_value
            ]
            if not from_db:
                continue

            tagged_doc = [{**r, "_origin": "document"} for r in from_doc]
            tagged_db = [{**r, "_origin": "database"} for r in from_db]
            ordered = sorted(
                tagged_doc + tagged_db,
                key=lambda r: (0 if r.get("_origin") == "document" else 1, r.get("id") or 0),
            )

            # Case-insensitive name equality — same vendor name => not a flag
            exact_names = {
                normalize_vendor_name(r.get("name"))
                for r in ordered
                if normalize_vendor_name(r.get("name"))
            }
            if len(exact_names) <= 1:
                continue

            name_list = [r.get("name") for r in ordered]
            fuzzy_same = _all_names_fuzzy_same(name_list)

            groups.append(
                {
                    "field": field,
                    "field_label": FIELD_LABELS[field],
                    "matched_value": matched_value,
                    "count": len(ordered),
                    "rows": ordered,
                    "distinct_names": _distinct_exact_names(ordered),
                    "fuzzy_name_match": fuzzy_same,
                }
            )

    default_summary = _summarize_groups(groups)
    fuzzy_filtered = [g for g in groups if not g["fuzzy_name_match"]]
    fuzzy_summary = _summarize_groups(fuzzy_filtered)
    removed_groups = default_summary["group_count"] - fuzzy_summary["group_count"]
    removed_rows = default_summary["flagged_rows"] - fuzzy_summary["flagged_rows"]

    return {
        "mode": "document_vs_db",
        "total_rows": len(external),
        "doc_row_count": len(doc_rows),
        "groups": groups,
        "summary": default_summary,
        "fuzzy_summary": fuzzy_summary,
        "fuzzy_removed": {
            "groups": max(0, removed_groups),
            "rows": max(0, removed_rows),
        },
    }


def find_submission_duplicates(
    submitted: Dict[str, Any],
    existing_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Compare one vendor-document submission against existing DB rows.

    Each match group includes the AI-extracted submission row plus existing
    DB rows that share phone / email / GST / MSME / PAN.
    """
    groups: List[Dict[str, Any]] = []
    flagged_ids = set()
    summary_counts = {field: 0 for field in COMPARE_FIELDS}

    submitted_row = {
        **submitted,
        "_origin": "submission",
        "source": submitted.get("source") or "AI-Extracted",
    }

    for field in COMPARE_FIELDS:
        submitted_key = NORMALIZERS[field](submitted.get(field))
        if not submitted_key:
            continue

        matches = []
        for row in existing_rows:
            row_key = NORMALIZERS[field](row.get(field))
            if row_key and row_key == submitted_key:
                matches.append({**row, "_origin": "database"})

        if not matches:
            continue

        ordered = [submitted_row] + sorted(matches, key=lambda r: r.get("id") or 0)
        groups.append(
            {
                "field": field,
                "field_label": FIELD_LABELS[field],
                "matched_value": submitted_key,
                "count": len(ordered),
                "rows": ordered,
                "fuzzy_name_match": False,
            }
        )
        summary_counts[field] += 1
        if submitted.get("id") is not None:
            flagged_ids.add(submitted["id"])
        for r in matches:
            if r.get("id") is not None:
                flagged_ids.add(r["id"])

    return {
        "mode": "submission",
        "submitted": submitted,
        "total_rows": len(existing_rows),
        "groups": groups,
        "summary": {
            **summary_counts,
            "group_count": len(groups),
            "flagged_rows": len(flagged_ids),
        },
        "fuzzy_summary": {
            **summary_counts,
            "group_count": len(groups),
            "flagged_rows": len(flagged_ids),
        },
        "fuzzy_removed": {"groups": 0, "rows": 0},
    }
