"""Flask entrypoint — routes only. Helpers live in sibling modules."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.utils import secure_filename

from ai_extract import (
    extract_entities_from_document,
    extract_vendor_pack,
    extract_vendors_from_quotation_document,
    link_docs_ai_cache_enabled,
    load_link_docs_ai_cache,
    save_link_docs_ai_cache_if_empty,
)
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
from db_dedupe import run_exact_dedupe_background
from document_links_service import (
    auto_insert_link_entities,
    cacheable_entity,
    entities_from_link_cache,
    link_entity_record,
    load_link_batch,
    save_link_batch,
)
from duplicate_check import (
    find_cross_document_links,
    find_document_vs_db_duplicates,
    find_duplicates,
    find_entities_vs_database,
    find_submission_duplicates,
)
from export_csv import vendors_to_csv
from master_parser import parse_master_file
from upload_utils import (
    MASTER_EXTENSIONS,
    QUOTATION_EXTENSIONS,
    UPLOAD_MASTER,
    UPLOAD_VENDOR,
    ensure_upload_dirs,
    save_file,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "vendor-data-detection-dev-key")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB

ensure_upload_dirs()
init_db(reset=False)
run_exact_dedupe_background("startup")


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

    run_exact_dedupe_background("after-master-upload")
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

        (vendor_dir / "meta.txt").write_text(
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
        results = find_document_vs_db_duplicates(doc_rows, fetch_all_vendors())
    except Exception as exc:
        flash(f"Database comparison failed: {exc}", "error")
        return redirect(url_for("index"))

    return render_template("quotation_db_compare.html", results=results)


@app.route("/document-links", methods=["GET", "POST"])
def document_links():
    """Multi-doc extract + cross-file links. Auto-inserts rows with name+phone+email."""
    if request.method == "GET":
        return render_template("document_links.html")

    files = [f for f in request.files.getlist("link_docs") if f and f.filename]
    if len(files) < 2:
        flash("Upload at least two documents to look for links between them.", "error")
        return render_template("document_links.html")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    batch_id = f"link_{stamp}"
    batch_dir = UPLOAD_VENDOR / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)

    entities = []
    errors = []
    saved_names = []
    accepted_uploads = []

    for upload in files:
        ext = upload.filename.rsplit(".", 1)[-1].lower() if "." in upload.filename else ""
        if ext not in QUOTATION_EXTENSIONS:
            errors.append(f"{upload.filename}: only PDF and images are accepted")
            continue
        saved = save_file(upload, batch_dir, prefix="doc_")
        if not saved:
            errors.append(f"{upload.filename}: could not save file")
            continue
        saved_names.append(saved)
        accepted_uploads.append((upload, saved))

    if len(saved_names) < 2:
        flash(
            "Need at least two readable documents. " + ("; ".join(errors[:3]) if errors else ""),
            "error",
        )
        return render_template("document_links.html")

    use_cache = link_docs_ai_cache_enabled()
    cached = load_link_docs_ai_cache() if use_cache else None
    cache_used = False
    cache_per_file = []

    if cached:
        cache_used = True
        entities = entities_from_link_cache([u for u, _ in accepted_uploads], cached)
        saved_by_name = {u.filename: saved for u, saved in accepted_uploads}
        for entity in entities:
            entity["saved_as"] = saved_by_name.get(entity.get("source_file"))
        logger.info(
            "Link Documents using AI cache (%s file entries, %s entities)",
            len(cached),
            len(entities),
        )
    else:
        for upload, saved in accepted_uploads:
            try:
                found = extract_entities_from_document(batch_dir / saved)
                clean_entities = []
                for item in found:
                    item["source_file"] = upload.filename
                    item["saved_as"] = saved
                    item["inserted_id"] = None
                    item["insert_status"] = None
                    entities.append(item)
                    clean_entities.append(cacheable_entity(item))
                cache_per_file.append(
                    {"filename": upload.filename, "entities": clean_entities}
                )
            except Exception as exc:
                errors.append(f"{upload.filename}: {exc}")
        if use_cache and cache_per_file:
            if save_link_docs_ai_cache_if_empty(cache_per_file):
                logger.info("Link Documents AI cache filled with %s files", len(cache_per_file))

    auto_count, insert_errors = auto_insert_link_entities(batch_id, entities)
    errors.extend(insert_errors)
    if auto_count:
        run_exact_dedupe_background("after-link-docs")

    save_link_batch(
        batch_id,
        {
            "batch_id": batch_id,
            "created_at": datetime.now().isoformat(),
            "files": saved_names,
            "entities": entities,
            "errors": errors,
            "auto_inserted": auto_count,
            "cache_used": cache_used,
        },
    )
    return redirect(url_for("document_link_results", batch_id=batch_id))


@app.route("/document-links/<batch_id>")
def document_link_results(batch_id: str):
    try:
        payload = load_link_batch(batch_id)
    except FileNotFoundError:
        flash("Document link results were not found.", "error")
        return redirect(url_for("document_links"))

    entities = payload.get("entities") or []
    links = find_cross_document_links(entities)
    auto_count = sum(1 for e in entities if e.get("insert_status") == "auto")
    if payload.get("auto_inserted") is not None and auto_count == 0:
        auto_count = int(payload.get("auto_inserted") or 0)
    skipped_count = sum(1 for e in entities if not e.get("inserted_id"))
    return render_template(
        "document_link_results.html",
        batch_id=batch_id,
        entities=entities,
        links=links,
        errors=payload.get("errors") or [],
        auto_inserted=auto_count,
        skipped_count=skipped_count,
        cache_used=bool(payload.get("cache_used")),
        show_all_rows=request.args.get("show_all") == "1",
    )


@app.route("/document-links/<batch_id>/add", methods=["POST"])
def document_links_add(batch_id: str):
    try:
        payload = load_link_batch(batch_id)
    except FileNotFoundError:
        flash("Document link batch was not found.", "error")
        return redirect(url_for("document_links"))

    try:
        index = int(request.form.get("index", "-1"))
    except ValueError:
        index = -1

    entities = payload.get("entities") or []
    results_url = url_for("document_link_results", batch_id=batch_id, show_all=1)
    if index < 0 or index >= len(entities):
        flash("That extracted row could not be found.", "error")
        return redirect(results_url)

    entity = entities[index]
    if entity.get("inserted_id"):
        flash(f"Already saved as row {entity['inserted_id']}.", "success")
        return redirect(results_url)

    record = link_entity_record(entity)
    if not record.get("name"):
        flash("This row has no name, so it was not added.", "error")
        return redirect(results_url)

    try:
        inserted_ids = insert_vendor_rows_returning_ids(
            [record],
            source_file=batch_id,
            source=SOURCE_AI,
        )
    except Exception as exc:
        flash(f"Could not add row: {exc}", "error")
        return redirect(results_url)

    entity["inserted_id"] = inserted_ids[0] if inserted_ids else None
    entity["insert_status"] = "manual" if entity["inserted_id"] else "skipped"
    entities[index] = entity
    payload["entities"] = entities
    save_link_batch(batch_id, payload)
    flash(f"Added {record['name']} as row {entity['inserted_id']}.", "success")
    return redirect(results_url)


@app.route("/document-links/<batch_id>/compare-db", methods=["POST"])
def document_links_compare_db(batch_id: str):
    try:
        payload = load_link_batch(batch_id)
    except FileNotFoundError:
        flash("Document link batch was not found.", "error")
        return redirect(url_for("document_links"))

    entities = payload.get("entities") or []
    try:
        results = find_entities_vs_database(entities, fetch_all_vendors())
    except Exception as exc:
        flash(f"Database comparison failed: {exc}", "error")
        return redirect(url_for("document_link_results", batch_id=batch_id))

    return render_template(
        "document_link_db.html",
        batch_id=batch_id,
        results=results,
    )


@app.route("/export/vendors.csv")
def export_vendors_csv():
    """Download every vendor_data row as CSV."""
    try:
        rows = fetch_all_vendors()
    except Exception as exc:
        flash(f"Export failed: {exc}", "error")
        return redirect(url_for("index"))

    if not rows:
        flash("No vendor rows in the database to export.", "error")
        return redirect(url_for("index"))

    csv_text, filename = vendors_to_csv(rows)
    return Response(
        csv_text,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/run-process", methods=["POST"])
def run_process():
    total = count_vendors()
    if total < 1:
        flash("No vendor rows in the database yet. Upload a master file or vendor docs first.", "error")
        return redirect(url_for("index"))

    try:
        results = find_duplicates(fetch_all_vendors())
    except Exception as exc:
        flash(f"Process failed: {exc}", "error")
        return redirect(url_for("index"))

    return render_template("process_results.html", results=results)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
