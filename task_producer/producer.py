import argparse
import logging
import os
import sys
import time
import psycopg2
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


def schedule_once(conn, batch_size: int) -> int:
    sql = """
    WITH eligible AS (
      SELECT t.id AS target_id, t.domain
      FROM monitor_targets t
      WHERE t.enabled = TRUE
        AND (
          t.last_scheduled_at IS NULL OR
          NOW() >= t.last_scheduled_at + (t.schedule_interval_minutes || ' minutes')::interval
        )
      ORDER BY t.priority DESC NULLS LAST, t.last_scheduled_at NULLS FIRST
      LIMIT %s
    ),
    ins AS (
      INSERT INTO monitor_waiting_tasks(target_id, domain, status, attempts)
      SELECT e.target_id, e.domain, 'waiting', 0
      FROM eligible e
      WHERE NOT EXISTS (
        SELECT 1 FROM monitor_waiting_tasks w
        WHERE w.target_id = e.target_id
          AND w.status IN ('waiting','leased')
      )
      RETURNING target_id
    ),
    upd AS (
      UPDATE monitor_targets t
      SET last_scheduled_at = NOW()
      WHERE t.id IN (SELECT target_id FROM ins)
      RETURNING t.id
    )
    SELECT (SELECT count(*) FROM ins) AS scheduled;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (batch_size,))
        row = cur.fetchone()
    conn.commit()
    logging.info(
        f"调度完成: 成功插入 {int(row[0]) if row and row[0] is not None else 0} 条等待任务, "
        f"batch_size={batch_size}, SQL 影响的 CTE: eligible/ins/upd"
    )
    return int(row[0] if row and row[0] is not None else 0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--loop-seconds", type=int, default=120)
    args = parser.parse_args()
    root = logging.getLogger()
    if not root.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root.addHandler(h)
    root.setLevel(logging.INFO)
    conn = connect()
    logging.info(f"启动参数: batch_size={args.batch_size}, loop_seconds={args.loop_seconds}")
    try:
        if args.loop_seconds > 0:
            while True:
                n = schedule_once(conn, args.batch_size)
                logging.info(f"scheduled {n} tasks")
                time.sleep(args.loop_seconds)
        else:
            n = schedule_once(conn, args.batch_size)
            logging.info(f"scheduled {n} tasks")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
