"""
Standalone PDF utilities.

Current:
  - extract_pages: build a new PDF from a page range

Edit INPUT_PDF / PAGE_RANGE / OUTPUT_DIR below, then run:
  python pdf-page-extractor.py

Page numbers are 1-based (first page = 1).
Ranges are inclusive. Comma-separated lists and ranges can be mixed.
Examples: "1-5" | "1,3,8" | "2-" | "-4"

Add further PDF helpers in the FUNCTIONS section below.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List, Sequence, Union

from pypdf import PdfReader, PdfWriter

PathLike = Union[str, Path]


# ---------------------------------------------------------------------------
# CONFIG — set these before running the script
# ---------------------------------------------------------------------------

INPUT_PDF = r"C:\Divyesh\Vendor Data Detection\data\Additonal QCS\WO-126-Galia-Assam-Porta Cabin Foundation.pdf"   # full path to the source PDF
PAGE_RANGE = "32-35"                    # e.g. "1-5", "1,3,8", "2-", "-4"
OUTPUT_DIR = r"C:\Divyesh\Vendor Data Detection\Standalone-Utilities\generated-pdfs"  # all generated PDFs go here


# ---------------------------------------------------------------------------
# Shared helpers (reuse across PDF functions)
# ---------------------------------------------------------------------------

def _as_path(path: PathLike) -> Path:
    return Path(path).expanduser().resolve()


def load_reader(pdf_path: PathLike) -> PdfReader:
    path = _as_path(pdf_path)
    if not path.is_file():
        raise FileNotFoundError(f"PDF not found: {path}")
    if path.suffix.lower() != ".pdf":
        raise ValueError(f"Expected a .pdf file, got: {path.name}")
    return PdfReader(str(path))


def page_count(pdf_path: PathLike) -> int:
    return len(load_reader(pdf_path).pages)


def parse_page_range(spec: str, total_pages: int) -> List[int]:
    """
    Parse a 1-based page specification into a list of 0-based page indexes.

    Accepted forms:
      "3"          -> [2]
      "1-5"        -> [0, 1, 2, 3, 4]
      "1,3,5-7"    -> [0, 2, 4, 5, 6]
      "7-"         -> from 7 through last page
      "-3"         -> from first page through 3
    """
    if total_pages < 1:
        raise ValueError("PDF has no pages.")

    text = (spec or "").strip()
    if not text:
        raise ValueError("Page range is empty.")

    indexes: List[int] = []
    seen = set()

    for part in re.split(r"\s*,\s*", text):
        if not part:
            continue

        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start = 1 if start_s.strip() == "" else int(start_s)
            end = total_pages if end_s.strip() == "" else int(end_s)
            if start < 1 or end < 1:
                raise ValueError(f"Page numbers must be >= 1 (got {part!r}).")
            if start > end:
                raise ValueError(f"Invalid range {part!r}: start > end.")
            if end > total_pages:
                raise ValueError(
                    f"Page {end} is out of range (PDF has {total_pages} page(s))."
                )
            page_nums = range(start, end + 1)
        else:
            page_num = int(part)
            if page_num < 1 or page_num > total_pages:
                raise ValueError(
                    f"Page {page_num} is out of range (PDF has {total_pages} page(s))."
                )
            page_nums = [page_num]

        for page_num in page_nums:
            idx = page_num - 1
            if idx not in seen:
                seen.add(idx)
                indexes.append(idx)

    if not indexes:
        raise ValueError("No pages selected from the given range.")
    return indexes


def _output_filename(src: Path, indexes: Sequence[int]) -> str:
    first = indexes[0] + 1
    last = indexes[-1] + 1
    if len(indexes) == 1:
        suffix = f"_p{first}"
    elif indexes == list(range(indexes[0], indexes[-1] + 1)):
        suffix = f"_p{first}-{last}"
    else:
        suffix = f"_p{first}-plus"
    return f"{src.stem}{suffix}{src.suffix}"


def resolve_output_path(
    src: Path,
    indexes: Sequence[int],
    output_dir: PathLike | None = None,
) -> Path:
    """Build the output PDF path inside ``output_dir`` (created if needed)."""
    filename = _output_filename(src, indexes)
    if output_dir:
        folder = _as_path(output_dir)
        folder.mkdir(parents=True, exist_ok=True)
        return folder / filename
    return src.with_name(filename)


def write_pdf(writer: PdfWriter, output_path: PathLike) -> Path:
    out = _as_path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as fh:
        writer.write(fh)
    return out


# ---------------------------------------------------------------------------
# PDF FUNCTIONS — add new capabilities here
# ---------------------------------------------------------------------------

def extract_pages(
    pdf_path: PathLike,
    page_range: str,
    output_dir: PathLike | None = None,
) -> Path:
    """
    Create a new PDF containing only the pages in ``page_range``.

    Writes into ``output_dir`` when provided; otherwise beside the source PDF.
    Returns the path of the generated PDF.
    """
    src = _as_path(pdf_path)
    reader = load_reader(src)
    indexes = parse_page_range(page_range, len(reader.pages))

    writer = PdfWriter()
    for idx in indexes:
        writer.add_page(reader.pages[idx])

    out = resolve_output_path(src, indexes, output_dir)
    return write_pdf(writer, out)


# Example stubs for later expansion (implement when needed):
#
# def merge_pdfs(pdf_paths: Iterable[PathLike], output_path: PathLike) -> Path:
#     ...
#
# def split_pdf(pdf_path: PathLike, output_dir: PathLike) -> List[Path]:
#     ...


# ---------------------------------------------------------------------------
# Run from config variables
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        out = extract_pages(INPUT_PDF, PAGE_RANGE, OUTPUT_DIR)
        print(f"Wrote: {out}")
        return 0
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
