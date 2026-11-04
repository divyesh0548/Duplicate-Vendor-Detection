"""Helpers for the Link Documents flow (batch JSON, auto-insert, AI cache replay)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

from werkzeug.utils import secure_filename

from db import SOURCE_AI, insert_vendor_rows_returning_ids
from upload_utils import UPLOAD_VENDOR

LINK_ENTITY_INSERT_FIELDS = (
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
    "aadhar",
    "rfp_no",
    "bank_name",
    "beneficiary_name",
    "account_no",
    "ifsc_code",
)


def link_batch_path(batch_id: str) -> Path:
    safe = secure_filename(batch_id)
    return UPLOAD_VENDOR / safe / "batch.json"


def load_link_batch(batch_id: str) -> Dict[str, Any]:
    path = link_batch_path(batch_id)
    if not path.is_file():
        raise FileNotFoundError("This document-link batch was not found.")
    return json.loads(path.read_text(encoding="utf-8"))


def save_link_batch(batch_id: str, payload: Dict[str, Any]) -> None:
    path = link_batch_path(batch_id)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def link_entity_record(entity: Dict[str, Any]) -> Dict[str, Any]:
    return {key: entity.get(key) for key in LINK_ENTITY_INSERT_FIELDS}


def qualifies_for_auto_insert(entity: Dict[str, Any]) -> bool:
    """Auto-insert when name, phone, and email are all present."""
    return all(
        str(entity.get(field) or "").strip() for field in ("name", "phone", "email")
    )


def auto_insert_link_entities(
    batch_id: str, entities: List[Dict[str, Any]]
) -> Tuple[int, List[str]]:
    """
    Insert rows that have name + phone + email. Always inserts (dedupe is background).
    Sets inserted_id and insert_status: auto | skipped.
    """
    auto_count = 0
    insert_errors: List[str] = []
    for entity in entities:
        if entity.get("inserted_id"):
            entity.setdefault("insert_status", "auto")
            continue
        if not qualifies_for_auto_insert(entity):
            entity["inserted_id"] = None
            entity["insert_status"] = "skipped"
            continue
        record = link_entity_record(entity)
        try:
            inserted_ids = insert_vendor_rows_returning_ids(
                [record],
                source_file=batch_id,
                source=SOURCE_AI,
            )
            entity["inserted_id"] = inserted_ids[0] if inserted_ids else None
            entity["insert_status"] = "auto" if entity["inserted_id"] else "skipped"
            if entity["inserted_id"]:
                auto_count += 1
            else:
                entity["insert_status"] = "skipped"
        except Exception as exc:
            entity["inserted_id"] = None
            entity["insert_status"] = "skipped"
            insert_errors.append(f"{record.get('name') or 'row'}: {exc}")
    return auto_count, insert_errors


def entities_from_link_cache(
    uploads: List[Any],
    cached_per_file: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Replay cached Link Documents entities onto the current upload filenames."""
    entities: List[Dict[str, Any]] = []
    by_name = {
        str(item.get("filename") or ""): item
        for item in cached_per_file
        if isinstance(item, dict)
    }

    for idx, upload in enumerate(uploads):
        entry = by_name.get(upload.filename)
        if entry is None and idx < len(cached_per_file):
            entry = cached_per_file[idx]
        if not isinstance(entry, dict):
            continue
        for item in entry.get("entities") or []:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            row["source_file"] = upload.filename
            row["inserted_id"] = None
            row["insert_status"] = None
            entities.append(row)

    return entities


def cacheable_entity(item: Dict[str, Any]) -> Dict[str, Any]:
    """Strip batch-only keys before writing the Link Documents AI cache."""
    skip = {"inserted_id", "insert_status", "saved_as", "source_file"}
    return {k: v for k, v in item.items() if k not in skip}
