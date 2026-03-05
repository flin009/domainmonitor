import psycopg2
import psycopg2.extras
from typing import Any, Dict, List, Optional
try:
    from .config import get_config
except Exception:
    from config import get_config  # type: ignore


class Db:
    def __init__(self):
        cfg = get_config()
        self._dsn = {
            "host": cfg.db_host,
            "port": cfg.db_port,
            "user": cfg.db_user,
            "password": cfg.db_password,
            "dbname": cfg.db_name,
        }
        self._conn = None

    def connect(self) -> None:
        if self._conn is None:
            self._conn = psycopg2.connect(**self._dsn)
            self._conn.autocommit = False

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def create_task(self, platform: str, domain: str, proxy_server: Optional[str], headless: bool) -> int:
        self.connect()
        with self._conn.cursor() as cur:
            cur.execute(
                "insert into monitor_tasks(platform, domain, status, proxy_server, headless) values(%s,%s,%s,%s,%s) returning id",
                (platform, domain, "running", proxy_server, headless),
            )
            tid = cur.fetchone()[0]
            self._conn.commit()
            return int(tid)

    def update_task_metrics(
        self,
        task_id: int,
        status: str,
        count: int,
        browser_launch_ms: Optional[float],
        collect_ms: Optional[float],
        insert_ms: Optional[float],
        total_ms: Optional[float],
        error_type: Optional[str],
        error_message: Optional[str],
    ) -> None:
        self.connect()
        with self._conn.cursor() as cur:
            cur.execute(
                """
                update monitor_tasks
                set status=%s, count=%s, browser_launch_ms=%s, collect_ms=%s, insert_ms=%s, total_ms=%s, error_type=%s, error_message=%s
                where id=%s
                """,
                (
                    status,
                    count,
                    browser_launch_ms,
                    collect_ms,
                    insert_ms,
                    total_ms,
                    error_type,
                    error_message,
                    task_id,
                ),
            )
            self._conn.commit()

    def update_task_proxy(self, task_id: int, proxy_server: Optional[str]) -> None:
        if proxy_server is None:
            return
        self.connect()
        with self._conn.cursor() as cur:
            cur.execute(
                "update monitor_tasks set proxy_server=%s where id=%s",
                (proxy_server, task_id),
            )
            self._conn.commit()

    def insert_results(self, results: List[Dict[str, Any]]) -> int:
        if not results:
            return 0
        self.connect()
        cols = [
            "task_id",
            "operator",
            "region",
            "download_time",
            "connect_time",
            "dns_time",
            "total_time",
            "status_code",
            "ip_location",
            "response_ip",
            "raw",
            "ip_country",
            "ip_province",
            "ip_city",
            "ip_isp",
        ]
        vals = []
        for r in results:
            vals.append(
                (
                    r.get("task_id"),
                    r.get("operator"),
                    r.get("region"),
                    r.get("download_time"),
                    r.get("connect_time"),
                    r.get("dns_time"),
                    r.get("total_time"),
                    str(r.get("status_code") or "") if r.get("status_code") is not None else None,
                    r.get("ip_location"),
                    r.get("response_ip"),
                    psycopg2.extras.Json(r.get("raw") or {}),
                    r.get("ip_country") or "未知",
                    r.get("ip_province") or "未知",
                    r.get("ip_city") or "未知",
                    r.get("ip_isp") or "未知",
                )
            )
        with self._conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                f"insert into monitor_results({','.join(cols)}) values %s",
                vals,
            )
            self._conn.commit()
            return len(vals)
