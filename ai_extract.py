"""
Extract structured vendor fields from uploaded documents via OpenRouter.

Uses a cost-effective multimodal model (Gemini Flash by default) that handles
PDF/image document understanding well.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

from column_keywords import VENDOR_DATA_COLUMNS

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Strong doc/OCR quality at low cost; override with OPENROUTER_MODEL in .env
DEFAULT_MODEL = "google/gemini-2.5-flash"

IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
PDF_TYPES = {".pdf"}

# Extra keys that map into vendor_data (aadhar is AI-only today).
AI_FIELD_KEYS = list(VENDOR_DATA_COLUMNS) + ["aadhar"]

DOC_PROMPTS = {
    "msme": """You are extracting data from an Indian MSME / Udyam registration document.
Return ONLY valid JSON with these keys (use null if not found):
{
  "name": string|null,
  "msme": string|null,
  "address": string|null,
  "city": string|null,
  "state": string|null,
  "pincode": string|null,
  "phone": string|null,
  "email": string|null,
  "pan": string|null,
  "gst": string|null
}
Prefer the Udyam / MSME registration number in "msme".""",
    "pan": """You are extracting data from an Indian PAN card or PAN document.
Return ONLY valid JSON with these keys (use null if not found):
{
  "name": string|null,
  "pan": string|null,
  "contact_person": string|null,
  "address": string|null,
  "city": string|null,
  "state": string|null,
  "pincode": string|null,
  "phone": string|null,
  "email": string|null
}
"pan" must be the 10-character PAN if visible. Put the name on the card in "name".""",
    "aadhar": """You are extracting data from an Indian Aadhaar card / Aadhaar document.
Return ONLY valid JSON with these keys (use null if not found):
{
  "name": string|null,
  "aadhar": string|null,
  "contact_person": string|null,
  "address": string|null,
  "city": string|null,
  "state": string|null,
  "pincode": string|null,
  "phone": string|null,
  "email": string|null
}
"aadhar" should be the 12-digit Aadhaar number if visible (digits only).
Put the cardholder name in both "name" and "contact_person" when appropriate.""",
    "other": """You are extracting vendor / company details from a business document
(quotation, invoice, letterhead, registration, bank letter, etc.).
Return ONLY valid JSON with these keys (use null if not found):
{
  "name": string|null,
  "contact_person": string|null,
  "address": string|null,
  "city": string|null,
  "state": string|null,
  "pincode": string|null,
  "phone": string|null,
  "email": string|null,
  "pan": string|null,
  "gst": string|null,
  "msme": string|null,
  "rfp_no": string|null,
  "bank_name": string|null,
  "beneficiary_name": string|null,
  "account_no": string|null,
  "ifsc_code": string|null
}
Prefer company/vendor legal name in "name". Prefer mobile in "phone".""",
}


def _model() -> str:
    return os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def _api_key() -> str:
    key = (os.getenv("OPENROUTER_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is missing in .env")
    return key


def _mime_for(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    if guessed:
        return guessed
    ext = path.suffix.lower()
    return {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(ext, "application/octet-stream")


def _file_to_content_parts(path: Path) -> List[Dict[str, Any]]:
    """Build OpenRouter multimodal content parts for a PDF or image."""
    data = path.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    mime = _mime_for(path)
    ext = path.suffix.lower()

    if ext in PDF_TYPES:
        return [
            {
                "type": "file",
                "file": {
                    "filename": path.name,
                    "file_data": f"data:{mime};base64,{b64}",
                },
            }
        ]

    if ext in IMAGE_TYPES:
        return [
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            }
        ]

    raise ValueError(f"Unsupported file type for AI extraction: {path.suffix}")


def _strip_json_fence(text: str) -> str:
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text, re.IGNORECASE)
    if fence:
        return fence.group(1).strip()
    return text


def _parse_json_response(text: str) -> Dict[str, Any]:
    cleaned = _strip_json_fence(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if not match:
            raise ValueError(f"Model did not return JSON: {text[:300]}")
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("Model JSON was not an object.")
    return data


def _normalize_extracted(data: Dict[str, Any]) -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {k: None for k in AI_FIELD_KEYS}
    for key in AI_FIELD_KEYS:
        if key not in data:
            continue
        value = data.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if not text or text.lower() in {"null", "none", "n/a", "na", "-"}:
            continue
        if key == "aadhar":
            digits = re.sub(r"\D", "", text)
            text = digits or text
        if key == "pan":
            text = re.sub(r"\s+", "", text).upper()
        if key == "phone":
            text = re.sub(r"[^\d+]", "", text) or text
        out[key] = text
    return out


def extract_from_document(path: Path, doc_type: str) -> Dict[str, Optional[str]]:
    """Call OpenRouter and return normalized vendor fields for one document."""
    prompt = DOC_PROMPTS.get(doc_type, DOC_PROMPTS["other"])
    parts: List[Dict[str, Any]] = [
        {"type": "text", "text": prompt},
        *_file_to_content_parts(path),
    ]

    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://localhost/vendor-data-detection",
        "X-Title": "Vendor Data Detection",
    }
    payload = {
        "model": _model(),
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": parts,
            }
        ],
    }

    response = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=120)
    if response.status_code >= 400:
        raise RuntimeError(
            f"OpenRouter error {response.status_code}: {response.text[:500]}"
        )

    body = response.json()
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected OpenRouter response: {body}") from exc

    if isinstance(content, list):
        # Some providers return content parts
        text_bits = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text_bits.append(part.get("text") or "")
            elif isinstance(part, str):
                text_bits.append(part)
        content = "\n".join(text_bits)

    if not isinstance(content, str):
        raise RuntimeError(f"Unexpected message content type: {type(content)}")

    return _normalize_extracted(_parse_json_response(content))


def _merge_fields(
    base: Dict[str, Optional[str]],
    incoming: Dict[str, Optional[str]],
) -> Dict[str, Optional[str]]:
    """Fill empty fields only — first non-empty value wins."""
    merged = dict(base)
    for key, value in incoming.items():
        if key not in merged:
            merged[key] = value
            continue
        if merged.get(key) in (None, "") and value not in (None, ""):
            merged[key] = value
    return merged


def extract_vendor_pack(
    saved_docs: Dict[str, List[Path]],
    vendor_name: str,
    rfp_no: str,
) -> tuple[Dict[str, Optional[str]], List[str]]:
    """
    Run type-specific extraction over saved vendor documents and merge results.

    saved_docs keys: msme, pan, aadhar, other  -> list of file paths

    Returns (merged_record, error_messages).
    Form vendor_name / rfp_no always win for those fields.
    """
    record: Dict[str, Optional[str]] = {k: None for k in AI_FIELD_KEYS}
    errors: List[str] = []

    for doc_type in ("msme", "pan", "aadhar", "other"):
        for path in saved_docs.get(doc_type, []):
            try:
                extracted = extract_from_document(path, doc_type)
                record = _merge_fields(record, extracted)
            except Exception as exc:  # keep processing other files
                errors.append(f"{path.name}: {exc}")

    if vendor_name:
        record["name"] = vendor_name
    if rfp_no:
        record["rfp_no"] = rfp_no

    return record, errors
