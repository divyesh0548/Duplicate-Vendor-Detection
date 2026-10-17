"""Create the database (if needed) and reset/create the vendor_data table."""
import os
from pathlib import Path

from dotenv import load_dotenv
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

from db import TABLE_NAME, describe_vendor_data_table, init_db

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def ensure_database():
    dbname = os.getenv("DB_NAME")
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT", "5432"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        dbname="postgres",
        connect_timeout=15,
    )
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
        if cur.fetchone():
            print(f"Database already exists: {dbname}")
        else:
            cur.execute(f'CREATE DATABASE "{dbname}"')
            print(f"Created database: {dbname}")
    conn.close()


if __name__ == "__main__":
    ensure_database()
    init_db(reset=True)
    print(f"Table {TABLE_NAME} is ready. Columns:")
    for name, dtype, nullable in describe_vendor_data_table():
        print(f"  - {name}: {dtype} (nullable={nullable})")
