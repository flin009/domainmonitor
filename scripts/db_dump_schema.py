import os
import sys
import psycopg2
from typing import List
try:
    from ..config import get_config
except Exception:
    sys.path.append(os.path.dirname(os.path.dirname(__file__)))
    from config import get_config  # type: ignore


def connect():
    cfg = get_config()
    return psycopg2.connect(
        host=cfg.db_host,
        port=cfg.db_port,
        user=cfg.db_user,
        password=cfg.db_password,
        dbname=cfg.db_name,
    )


def dump_table(conn, table: str) -> str:
    parts: List[str] = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name=%s
            ORDER BY ordinal_position
            """,
            (table,),
        )
        cols = cur.fetchall()
        if not cols:
            return f"-- table {table} not found\n"
        col_lines = []
        for name, dtype, nullable, default in cols:
            # Map data_type to typical PostgreSQL type names
            t = str(dtype)
            # Use text as-is; information_schema already gives types like integer, bigint, timestamp with time zone, jsonb
            line = f"  {name} {t}"
            if nullable == "NO":
                line += " NOT NULL"
            if default:
                line += f" DEFAULT {default}"
            col_lines.append(line)
        parts.append(f"CREATE TABLE {table} (\n" + ",\n".join(col_lines) + "\n);")
        # Constraints
        cur.execute(
            """
            SELECT conname, contype, pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conrelid = %s::regclass
            ORDER BY contype, conname
            """,
            (f"public.{table}",),
        )
        cons = cur.fetchall()
        for name, contype, defn in cons:
            if contype in ("p", "u", "f", "c"):
                parts.append(f"ALTER TABLE {table} ADD CONSTRAINT {name} {defn};")
        # Indexes
        cur.execute(
            """
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE schemaname='public' AND tablename=%s
            ORDER BY indexname
            """,
            (table,),
        )
        idxs = cur.fetchall()
        for idxname, idxdef in idxs:
            parts.append(idxdef + ";")
    return "\n".join(parts) + "\n"


def main():
    tables = [
        "monitor_targets",
        "monitor_waiting_tasks",
        "monitor_tasks",
        "monitor_results",
    ]
    conn = connect()
    try:
        for t in tables:
            print(dump_table(conn, t))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
