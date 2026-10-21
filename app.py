import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.utils import secure_filename

from ai_extract import extract_vendor_pack, extract_vendors_from_quotation_document
from db import (
    SOURCE_AI,
    SOURCE_EXCEL,
    count_vendors,
    fetch_all_vendors,
    fetch_vendors_by_ids,
    init_db,
    insert_vendor_rows,
    insert_vendor_rows_returning_ids,
)
from db_dedupe import remove_exact_combo_duplicates
from duplicate_check import (
    find_document_vs_db_duplicates,
    find_duplicates,
    find_submission_duplicates,
)
from master_parser import parse_master_file

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "vendor-data-detection-dev-key")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB

UPLOAD_MASTER.mkdir(parents=True, exist_ok=True)
UPLOAD_VENDOR.mkdir(parents=True, exist_ok=True)

init_db(reset=False)

_dedupe_lock = threading.Lock()
_dedupe_running = False


def _run_exact_dedupe_background(reason: str = "startup") -> None:
    """Delete near-exact DB duplicates (same name + ≥3 other fields) in a daemon thread."""
    global _dedupe_running

    def worker():
        global _dedupe_running
        if not _dedupe_lock.acquire(blocking=False):
            logger.info("Exact combo dedupe skipped (%s) — already running", reason)
            return
        _dedupe_running = True
        try:
            stats = remove_exact_combo_duplicates()
            logger.info("Exact combo dedupe (%s): %s", reason, stats)
        except Exception:
            logger.exception("Exact combo dedupe failed (%s)", reason)
        finally:
            _dedupe_running = False
            _dedupe_lock.release()

    threading.Thread(target=worker, name=f"dedupe-{reason}", daemon=True).start()


# Clean near-exact duplicate rows when the app starts
_run_exact_dedupe_background("startup")


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


@app.route("/")
def index():
    master_files = sorted(UPLOAD_MASTER.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    master_ready = bool(master_files)
    return render_template(
        "index.html",
        master_ready=master_ready,
        master_filename=master_files[0].name if master_files else None,
    )


@app.route("/upload/master", methods=["POST"])
def upload_master():
    file = request.files.get("master_file")
    if not file or not file.filename:
        flash("Please select a master file to upload.", "error")
        return redirect(url_for("index"))

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in MASTER_EXTENSIONS:
        flash("Master file must be Excel (.xlsx / .xls) or CSV.", "error")
        return redirect(url_for("index"))

    saved = save_file(file, UPLOAD_MASTER, prefix="master_")
    if not saved:
        flash("Unsupported file type.", "error")
        return redirect(url_for("index"))

    saved_path = UPLOAD_MASTER / saved
    try:
        records, meta = parse_master_file(saved_path)
        inserted = insert_vendor_rows(
            records,
            source_file=saved,
            source=SOURCE_EXCEL,
        )
    except Exception as exc:
        flash(f"File saved as {saved}, but import failed: {exc}", "error")
        return redirect(url_for("index"))

    # Re-run near-exact cleanup after bulk excel import
    _run_exact_dedupe_background("after-master-upload")

    flash(
        f"Master uploaded ({saved}). Detected header row {meta['header_row'] + 1}, "
        f"mapped {len(meta['column_map'])} columns, inserted {inserted} row(s). "
        f"Background cleanup of near-exact duplicates started.",
        "success",
    )
    return redirect(url_for("index"))


def _handle_document_only_upload():
    """Extract multiple vendors from one quotation PDF/image; within-doc dupes only."""
    file = request.files.get("quotation_doc")
    if not file or not file.filename:
        flash("Please upload a single PDF or image quotation document.", "error")
        return render_template("vendor_docs.html", form_data=request.form)

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in QUOTATION_EXTENSIONS:
        flash("Document-only mode accepts PDF or image files only.", "error")
        return render_template("vendor_docs.html", form_data=request.form)

    rfp_no = (request.form.get("doc_rfp_no") or "").strip()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    vendor_dir = UPLOAD_VENDOR / f"quotation_doc_{stamp}"
    vendor_dir.mkdir(parents=True, exist_ok=True)

    saved = save_file(file, vendor_dir, prefix="quotation_")
    if not saved:
        flash("Could not save the uploaded document.", "error")
        return render_template("vendor_docs.html", form_data=request.form)

    saved_path = vendor_dir / saved
    (vendor_dir / "meta.txt").write_text(
        f"Mode: document_only\nFile: {saved}\nRFP No: {rfp_no or '-'}\n"
        f"Uploaded At: {datetime.now().isoformat()}\n",
        encoding="utf-8",
    )

    try:
        records = extract_vendors_from_quotation_document(saved_path)
        if rfp_no:
            for rec in records:
                if not rec.get("rfp_no"):
                    rec["rfp_no"] = rfp_no

        inserted_ids = insert_vendor_rows_returning_ids(
            records,
            source_file=vendor_dir.name,
            source=SOURCE_AI,
        )
        inserted_rows = fetch_vendors_by_ids(inserted_ids)
        # Within-document duplicates only — do not use any other DB rows
        results = find_duplicates(inserted_rows)
    except Exception as exc:
        flash(f"Document saved, but extraction/insert failed: {exc}", "error")
        return redirect(url_for("index"))

    return render_template(
        "quotation_results.html",
        results=results,
        inserted_rows=inserted_rows,
        inserted_ids_csv=",".join(str(i) for i in inserted_ids),
        source_file=saved,
    )


@app.route("/vendor-docs", methods=["GET", "POST"])
def vendor_docs():
    if request.method == "POST":
        upload_mode = (request.form.get("upload_mode") or "standard").strip()
        if upload_mode == "document_only":
            return _handle_document_only_upload()

        vendor_name = (request.form.get("vendor_name") or "").strip()
        rfp_no = (request.form.get("rfp_no") or "").strip()

        if not vendor_name:
            flash("Vendor name is required.", "error")
            return render_template("vendor_docs.html", form_data=request.form)

        vendor_slug = secure_filename(vendor_name) or "vendor"
        rfp_slug = secure_filename(rfp_no) if rfp_no else "no-rfp"
        vendor_dir = UPLOAD_VENDOR / f"{vendor_slug}_{rfp_slug}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        vendor_dir.mkdir(parents=True, exist_ok=True)

        meta_path = vendor_dir / "meta.txt"
        meta_path.write_text(
            f"Vendor Name: {vendor_name}\nRFP No: {rfp_no or '-'}\nUploaded At: {datetime.now().isoformat()}\n",
            encoding="utf-8",
        )

        optional_docs = {
            "msme": request.files.get("msme"),
            "pan": request.files.get("pan"),
            "aadhar": request.files.get("aadhar"),
        }
        other_docs = request.files.getlist("other_docs")

        has_any_doc = any(f and f.filename for f in optional_docs.values()) or any(
            f and f.filename for f in other_docs
        )
        if not has_any_doc:
            flash("Upload at least one document (MSME, PAN, Aadhaar, or Other docs).", "error")
            return render_template("vendor_docs.html", form_data=request.form)

        saved_docs = {"msme": [], "pan": [], "aadhar": [], "other": []}

        for label, file_storage in optional_docs.items():
            if not file_storage or not file_storage.filename:
                continue
            saved = save_file(file_storage, vendor_dir, prefix=f"{label}_")
            if not saved:
                flash(f"Invalid file type for {label.upper()}.", "error")
                return render_template("vendor_docs.html", form_data=request.form)
            saved_docs[label].append(vendor_dir / saved)

        for file_storage in other_docs:
            if file_storage and file_storage.filename:
                saved = save_file(file_storage, vendor_dir, prefix="other_")
                if not saved:
                    flash(f"Skipped unsupported file: {file_storage.filename}", "error")
                    continue
                saved_docs["other"].append(vendor_dir / saved)

        try:
            # Snapshot existing DB before insert so we don't match the new row
            existing_rows = fetch_all_vendors()

            record, extract_errors = extract_vendor_pack(
                saved_docs,
                vendor_name=vendor_name,
                rfp_no=rfp_no,
            )
            inserted_ids = insert_vendor_rows_returning_ids(
                [record],
                source_file=vendor_dir.name,
                source=SOURCE_AI,
            )
            if inserted_ids:
                record["id"] = inserted_ids[0]

            results = find_submission_duplicates(record, existing_rows)
        except Exception as exc:
            flash(
                f"Documents saved for {vendor_name}, but AI extraction/DB insert failed: {exc}",
                "error",
            )
            return redirect(url_for("index"))

        return render_template(
            "submission_results.html",
            results=results,
            submitted=record,
            extract_errors="; ".join(extract_errors[:3]) if extract_errors else None,
        )

    return render_template("vendor_docs.html", form_data={})


@app.route("/vendor-docs/compare-db", methods=["POST"])
def compare_quotation_with_db():
    """Optional step: compare quotation-document vendors against the rest of the DB."""
    raw_ids = (request.form.get("inserted_ids") or "").strip()
    if not raw_ids:
        flash("Missing document vendor ids for comparison.", "error")
        return redirect(url_for("index"))

    try:
        inserted_ids = [int(part) for part in raw_ids.split(",") if part.strip()]
    except ValueError:
        flash("Invalid vendor id list for comparison.", "error")
        return redirect(url_for("index"))

    doc_rows = fetch_vendors_by_ids(inserted_ids)
    if not doc_rows:
        flash("Could not load the extracted document vendors.", "error")
        return redirect(url_for("index"))

    try:
        all_rows = fetch_all_vendors()
        results = find_document_vs_db_duplicates(doc_rows, all_rows)
    except Exception as exc:
        flash(f"Database comparison failed: {exc}", "error")
        return redirect(url_for("index"))

    return render_template("quotation_db_compare.html", results=results)


@app.route("/run-process", methods=["POST"])
def run_process():
    total = count_vendors()
    if total < 1:
        flash("No vendor rows in the database yet. Upload a master file or vendor docs first.", "error")
        return redirect(url_for("index"))

    try:
        rows = fetch_all_vendors()
        results = find_duplicates(rows)
    except Exception as exc:
        flash(f"Process failed: {exc}", "error")
        return redirect(url_for("index"))

    return render_template("process_results.html", results=results)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
