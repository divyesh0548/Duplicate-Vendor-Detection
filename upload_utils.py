"""Upload paths, allowed extensions, and file save helpers."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_MASTER = BASE_DIR / "uploads" / "master"
UPLOAD_VENDOR = BASE_DIR / "uploads" / "vendor"

ALLOWED_EXTENSIONS = {
    "pdf",
    "png",
    "jpg",
    "jpeg",
    "gif",
    "webp",
    "xlsx",
    "xls",
    "csv",
    "doc",
    "docx",
}
MASTER_EXTENSIONS = {"xlsx", "xls", "csv"}
QUOTATION_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "gif", "webp"}


def ensure_upload_dirs() -> None:
    UPLOAD_MASTER.mkdir(parents=True, exist_ok=True)
    UPLOAD_VENDOR.mkdir(parents=True, exist_ok=True)


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def save_file(file_storage, destination_dir: Path, prefix: str = "") -> Optional[str]:
    if not file_storage or not file_storage.filename:
        return None
    if not allowed_file(file_storage.filename):
        return None
    safe_name = secure_filename(file_storage.filename)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    final_name = f"{prefix}{stamp}_{safe_name}" if prefix else f"{stamp}_{safe_name}"
    file_storage.save(destination_dir / final_name)
    return final_name
