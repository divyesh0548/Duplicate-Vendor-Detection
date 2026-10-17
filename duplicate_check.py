"""Find duplicate vendor_data rows by phone, email, and GST.

Exact-same vendor names are not flagged.
Fuzzy name matching can collapse near-identical vendor names and remove those groups.
"""

from __future__ import annotations

import re
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Sequence, Tuple


COMPARE_FIELDS = ("phone", "email", "gst")

FIELD_LABELS = {
    "phone": "Mobile / Phone",
    "email": "Email",
    "gst": "GST",
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


NORMALIZERS = {
    "phone": normalize_phone,
    "email": normalize_email,
    "gst": normalize_gst,
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
        "phone": counts["phone"],
        "email": counts["email"],
        "gst": counts["gst"],
        "group_count": len(groups),
        "flagged_rows": len(flagged_ids),
    }


def find_duplicates(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Flag rows that share phone / email / GST but do NOT share the same vendor name.

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
