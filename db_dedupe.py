"""
Remove near-exact duplicate rows from vendor_data.

Two rows are treated as the same vendor record only when:
  1. Vendor name is the same (required), and
  2. At least MIN_OTHER_MATCHING_FIELDS other identity fields also match exactly.

Keeps the lowest id in each cluster and deletes the rest.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from db import delete_vendors_by_ids, fetch_all_vendors
from duplicate_check import (
    normalize_email,
    normalize_gst,
    normalize_msme,
    normalize_pan,
    normalize_phone,
    normalize_vendor_name,
)

logger = logging.getLogger(__name__)

# Besides matching name, at least this many other fields must match.
MIN_OTHER_MATCHING_FIELDS = 3

OTHER_FIELDS = ("phone", "email", "pan", "gst", "msme", "aadhar")
DEDUPE_FIELDS = ("name",) + OTHER_FIELDS


def normalize_aadhar(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if len(digits) < 8:
        return None
    return digits


DEDUPE_NORMALIZERS = {
    "name": lambda v: normalize_vendor_name(v) or None,
    "phone": normalize_phone,
    "email": normalize_email,
    "pan": normalize_pan,
    "gst": normalize_gst,
    "msme": normalize_msme,
    "aadhar": normalize_aadhar,
}


def _field_values(row: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for field in DEDUPE_FIELDS:
        key = DEDUPE_NORMALIZERS[field](row.get(field))
        if key:
            out[field] = key
    return out


def is_exact_combo_duplicate(
    a: Dict[str, str],
    b: Dict[str, str],
    min_other: int = MIN_OTHER_MATCHING_FIELDS,
) -> bool:
    """True when names match and ≥ min_other other identity fields match."""
    name_a, name_b = a.get("name"), b.get("name")
    if not name_a or not name_b or name_a != name_b:
        return False

    other_shared = 0
    for field in OTHER_FIELDS:
        va, vb = a.get(field), b.get(field)
        if va and vb and va == vb:
            other_shared += 1
            if other_shared >= min_other:
                return True
    return False


class _UnionFind:
    def __init__(self, ids: List[int]):
        self.parent = {i: i for i in ids}

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            if ra < rb:
                self.parent[rb] = ra
            else:
                self.parent[ra] = rb


def find_exact_combo_duplicate_ids(
    rows: List[Dict[str, Any]],
    min_other: int = MIN_OTHER_MATCHING_FIELDS,
) -> Tuple[List[int], int]:
    """
    Return (ids_to_delete, clusters_with_dupes).

    Keeps the lowest id in each cluster; others are deleted.
    """
    if len(rows) < 2:
        return [], 0

    prepared: List[Tuple[int, Dict[str, str]]] = []
    for row in rows:
        rid = row.get("id")
        if rid is None:
            continue
        vals = _field_values(row)
        # Name is mandatory for cleanup — skip nameless rows as anchors
        if not vals.get("name"):
            continue
        prepared.append((int(rid), vals))

    ids = [rid for rid, _ in prepared]
    uf = _UnionFind(ids)
    id_to_vals = {rid: vals for rid, vals in prepared}

    # Only compare rows that already share the same name
    by_name: Dict[str, List[int]] = {}
    for rid, vals in prepared:
        by_name.setdefault(vals["name"], []).append(rid)

    for name_ids in by_name.values():
        if len(name_ids) < 2:
            continue
        for i in range(len(name_ids)):
            for j in range(i + 1, len(name_ids)):
                a, b = name_ids[i], name_ids[j]
                if is_exact_combo_duplicate(id_to_vals[a], id_to_vals[b], min_other=min_other):
                    uf.union(a, b)

    clusters: Dict[int, List[int]] = {}
    for rid in ids:
        root = uf.find(rid)
        clusters.setdefault(root, []).append(rid)

    to_delete: List[int] = []
    cluster_count = 0
    for members in clusters.values():
        if len(members) < 2:
            continue
        cluster_count += 1
        keep = min(members)
        to_delete.extend(mid for mid in members if mid != keep)

    return sorted(to_delete), cluster_count


def remove_exact_combo_duplicates(
    min_other: int = MIN_OTHER_MATCHING_FIELDS,
) -> Dict[str, int]:
    """
    Load all rows, find near-exact duplicates (same name + ≥3 other fields),
    delete extras. Returns stats: scanned, deleted, clusters.
    """
    rows = fetch_all_vendors()
    to_delete, clusters = find_exact_combo_duplicate_ids(rows, min_other=min_other)
    deleted = delete_vendors_by_ids(to_delete) if to_delete else 0
    stats = {
        "scanned": len(rows),
        "deleted": deleted,
        "clusters": clusters,
        "min_other_matching_fields": min_other,
        "require_same_name": True,
    }
    logger.info("Exact combo dedupe finished: %s", stats)
    return stats
