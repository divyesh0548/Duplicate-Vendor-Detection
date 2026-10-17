import os
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

from ai_extract import extract_vendor_pack
from db import SOURCE_AI, SOURCE_EXCEL, count_vendors, fetch_all_vendors, init_db, insert_vendor_rows
from duplicate_check import find_duplicates
from master_parser import parse_master_file

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

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

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "vendor-data-detection-dev-key")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB

UPLOAD_MASTER.mkdir(parents=True, exist_ok=True)
UPLOAD_VENDOR.mkdir(parents=True, exist_ok=True)

init_db(reset=False)


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

    flash(
        f"Master uploaded ({saved}). Detected header row {meta['header_row'] + 1}, "
        f"mapped {len(meta['column_map'])} columns, inserted {inserted} row(s).",
        "success",
    )
    return redirect(url_for("index"))


@app.route("/vendor-docs", methods=["GET", "POST"])
def vendor_docs():
    if request.method == "POST":
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
            record, extract_errors = extract_vendor_pack(
                saved_docs,
                vendor_name=vendor_name,
                rfp_no=rfp_no,
            )
            inserted = insert_vendor_rows(
                [record],
                source_file=vendor_dir.name,
                source=SOURCE_AI,
            )
        except Exception as exc:
            flash(
                f"Documents saved for {vendor_name}, but AI extraction/DB insert failed: {exc}",
                "error",
            )
            return redirect(url_for("index"))

        rfp_label = f" (RFP {rfp_no})" if rfp_no else ""
        msg = (
            f"Vendor documents saved for {vendor_name}{rfp_label}. "
            f"AI extracted and inserted {inserted} row(s) (source={SOURCE_AI})."
        )
        if extract_errors:
            msg += " Warnings: " + "; ".join(extract_errors[:3])
            flash(msg, "error")
        else:
            flash(msg, "success")
        return redirect(url_for("index"))

    return render_template("vendor_docs.html", form_data={})


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
