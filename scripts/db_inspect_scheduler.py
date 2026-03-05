import psycopg2
import os
import sys
from datetime import datetime, timezone
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

def main():
    conn = connect()
    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("select count(*) from monitor_targets where enabled=true")
        enabled_count = cur.fetchone()[0]
        cur.execute("""
            select count(*)
            from monitor_targets t
            where t.enabled=true
              and (
                t.last_scheduled_at is null or
                now() >= t.last_scheduled_at + (t.schedule_interval_minutes || ' minutes')::interval
              )
        """)
        eligible_count = cur.fetchone()[0]
        cur.execute("""
            select status, count(*) from monitor_waiting_tasks group by status order by status
        """)
        status_counts = cur.fetchall()
        cur.execute("""
            select id, domain, status, lease_until, attempts, created_at, updated_at
            from monitor_waiting_tasks
            order by updated_at desc
            limit 10
        """)
        sample = cur.fetchall()
        cur.execute("""
            select id, domain, last_scheduled_at, schedule_interval_minutes
            from monitor_targets
            where enabled=true
            order by last_scheduled_at nulls first
            limit 10
        """)
        targets = cur.fetchall()
    conn.close()
    print("enabled_targets:", enabled_count)
    print("eligible_targets:", eligible_count)
    print("waiting_tasks_status_counts:", status_counts)
    print("waiting_tasks_sample:", sample)
    print("targets_sample:", targets)

if __name__ == "__main__":
    main()
