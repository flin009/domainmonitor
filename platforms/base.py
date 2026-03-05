from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple


class MonitorPlatform(ABC):
    @abstractmethod
    def run(
        self,
        domain: str,
        proxy_server: Optional[str],
        headless: bool,
        user_agent: Optional[str] = None,
        referer: Optional[str] = None,
        cookie: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], str, Dict[str, float]]:
        ...

