import csv
import io
import os
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values

from column_keywords import VENDOR_DATA_COLUMNS

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TABLE_NAME = "vendor_data"

SOURCE_EXCEL = "excel"
SOURCE_AI = "AI-Extracted"

# DB insert columns = excel-mapped fields + AI-only extras (order matters for INSERT).
INSERT_COLUMNS = list(VENDOR_DATA_COLUMNS)
if "aadhar" not in INSERT_COLUMNS:
    INSERT_COLUMNS.append("aadhar")


def get_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT", "5432"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        dbname=os.getenv("DB_NAME"),
        connect_timeout=15,
    )


# No UNIQUE constraints on business columns — duplicates are allowed.
CREATE_VENDOR_DATA_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    id                SERIAL PRIMARY KEY,
    sr_no             TEXT,
    name              TEXT,
    contact_person    TEXT,
    address           TEXT,
    city              TEXT,
    state             TEXT,
    pincode           TEXT,
    phone             TEXT,
    email             TEXT,
    pan               TEXT,
    gst               TEXT,
    msme              TEXT,
    rfp_no            TEXT,
    bank_name         TEXT,
    beneficiary_name  TEXT,
    account_no        TEXT,
    ifsc_code         TEXT,
    aadhar            TEXT,
    source_file       TEXT,
    source            TEXT DEFAULT '{SOURCE_EXCEL}',
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

RESET_VENDOR_DATA_SQL = f"""
DROP TABLE IF EXISTS {TABLE_NAME};
DROP TABLE IF EXISTS vendor_date;
""" + CREATE_VENDOR_DATA_SQL

# Columns to ensure on existing deployments (nullable, no unique).
ENSURE_COLUMNS = {
    "rfp_no": "TEXT",
    "aadhar": "TEXT",
    "source": f"TEXT DEFAULT '{SOURCE_EXCEL}'",
}


def _table_exists(cur, name: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = %s
        """,
        (name,),
    )
    return cur.fetchone() is not None


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s AND column_name = %s
        """,
        (table, column),
    )
    return cur.fetchone() is not None


def _migrate_rename_legacy_table(cur):
    """Rename vendor_date -> vendor_data if the old table still exists."""
    has_new = _table_exists(cur, TABLE_NAME)
    has_old = _table_exists(cur, "vendor_date")
    if has_old and not has_new:
        cur.execute("ALTER TABLE vendor_date RENAME TO vendor_data")
    elif has_old and has_new:
        cur.execute("DROP TABLE vendor_date")


def _ensure_columns(cur):
    for column, col_type in ENSURE_COLUMNS.items():
        if not _column_exists(cur, TABLE_NAME, column):
            cur.execute(
                f"ALTER TABLE {TABLE_NAME} ADD COLUMN {column} {col_type}"
            )
    # Backfill null source values on older excel rows
    if _column_exists(cur, TABLE_NAME, "source"):
        cur.execute(
            f"UPDATE {TABLE_NAME} SET source = %s WHERE source IS NULL",
            (SOURCE_EXCEL,),
        )


def init_db(reset: bool = False):
    with get_connection() as conn:
        with conn.cursor() as cur:
            if reset:
                cur.execute(RESET_VENDOR_DATA_SQL)
            else:
                _migrate_rename_legacy_table(cur)
                cur.execute(CREATE_VENDOR_DATA_SQL)
                _ensure_columns(cur)
        conn.commit()


def _records_to_value_tuples(
    records: Iterable[Dict[str, Optional[str]]],
    source_file: Optional[str],
    source: str,
) -> List[tuple]:
    rows: List[tuple] = []
    for record in records:
        rows.append(
            tuple(record.get(col) for col in INSERT_COLUMNS)
            + (
                source_file if source_file is not None else record.get("source_file"),
                record.get("source") or source,
            )
        )
    return rows


def insert_vendor_rows(
    records: Iterable[Dict[str, Optional[str]]],
    source_file: Optional[str] = None,
    source: str = SOURCE_EXCEL,
) -> int:
    """
    Bulk-insert vendor rows (fast path for master Excel/CSV).

    Large batches use PostgreSQL COPY (stdin CSV). Smaller batches use
    multi-row INSERT via execute_values — both avoid per-row round trips.
    """
    rows = _records_to_value_tuples(records, source_file, source)
    if not rows:
        return 0

    columns = list(INSERT_COLUMNS) + ["source_file", "source"]
    col_sql = ", ".join(columns)

    with get_connection() as conn:
        with conn.cursor() as cur:
            if len(rows) >= 50:
                buf = io.StringIO()
                writer = csv.writer(buf, lineterminator="\n")
                for values in rows:
                    # Empty string -> SQL NULL via COPY NULL ''
                    writer.writerow(["" if v is None else str(v) for v in values])
                buf.seek(0)
                cur.copy_expert(
                    f"COPY {TABLE_NAME} ({col_sql}) FROM STDIN WITH (FORMAT csv, NULL '')",
                    buf,
                )
            else:
                execute_values(
                    cur,
                    f"INSERT INTO {TABLE_NAME} ({col_sql}) VALUES %s",
                    rows,
                    page_size=1000,
                )
        conn.commit()

    return len(rows)


def insert_vendor_rows_returning_ids(
    records: Iterable[Dict[str, Optional[str]]],
    source_file: Optional[str] = None,
    source: str = SOURCE_EXCEL,
) -> List[int]:
    """Bulk-insert vendor rows and return their new ids (batched RETURNING)."""
    rows = _records_to_value_tuples(records, source_file, source)
    if not rows:
        return []

    columns = list(INSERT_COLUMNS) + ["source_file", "source"]
    col_sql = ", ".join(columns)
    sql = f"INSERT INTO {TABLE_NAME} ({col_sql}) VALUES %s RETURNING id"

    with get_connection() as conn:
        with conn.cursor() as cur:
            # fetch=True returns all RETURNING rows from batched inserts
            result = execute_values(cur, sql, rows, page_size=500, fetch=True)
            ids = [int(r[0]) for r in result] if result else []
        conn.commit()

    return ids


def delete_vendors_by_ids(ids: Iterable[int]) -> int:
    """Delete vendor_data rows by id. Returns number deleted."""
    id_list = [int(i) for i in ids]
    if not id_list:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {TABLE_NAME} WHERE id = ANY(%s)",
                (id_list,),
            )
            deleted = cur.rowcount
        conn.commit()
    return deleted


def fetch_all_vendors() -> List[Dict]:
    """Return all vendor_data rows as dicts (for process / duplicate checks)."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    id, sr_no, name, contact_person, address, city, state, pincode,
                    phone, email, pan, gst, msme, rfp_no, bank_name, beneficiary_name,
                    account_no, ifsc_code, aadhar, source_file, source,
                    created_at, updated_at
                FROM {TABLE_NAME}
                ORDER BY id
                """
            )
            return [dict(row) for row in cur.fetchall()]


def count_vendors() -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}")
            return int(cur.fetchone()[0])


def fetch_vendors_by_ids(ids: Iterable[int]) -> List[Dict]:
    """Return vendor_data rows for the given ids, ordered by id."""
    id_list = [int(i) for i in ids]
    if not id_list:
        return []
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    id, sr_no, name, contact_person, address, city, state, pincode,
                    phone, email, pan, gst, msme, rfp_no, bank_name, beneficiary_name,
                    account_no, ifsc_code, aadhar, source_file, source,
                    created_at, updated_at
                FROM {TABLE_NAME}
                WHERE id = ANY(%s)
                ORDER BY id
                """,
                (id_list,),
            )
            return [dict(row) for row in cur.fetchall()]


def describe_vendor_data_table() -> List[tuple]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                ORDER BY ordinal_position
                """,
                (TABLE_NAME,),
            )
            return cur.fetchall()


if __name__ == "__main__":
    init_db(reset=True)
    print(f"Table {TABLE_NAME} reset. Columns:")
    for name, dtype, nullable in describe_vendor_data_table():
        print(f"  - {name}: {dtype} (nullable={nullable})")
