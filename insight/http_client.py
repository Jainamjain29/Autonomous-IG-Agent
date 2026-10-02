"""Reusable HTTP client for Instagram Graph API with retry, backoff, call cap, and usage throttling."""
import json
import logging
import time
import requests

logger = logging.getLogger("insight.http_client")

DEFAULT_CALL_CAP = 200
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
BACKOFF_BASE = 1.0


class CallCapReached(Exception):
    """Hard call cap hit."""


class GraphClient:
    """Read-only HTTP client for the Instagram Graph API.

    * Token is never logged, printed, or saved.
    * Every HTTP attempt -- including retries -- counts toward call_cap.
    * 5xx and network timeouts are retried up to MAX_RETRIES times with
      exponential backoff (1 s, 2 s, 4 s).
    * 4xx errors are NOT retried; they are returned for the caller to record.
    * Parses X-App-Usage and X-Business-Use-Case-Usage headers.
      If usage > 80%, sets usage_throttled = True.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        call_cap: int = DEFAULT_CALL_CAP,
        timeout: int = REQUEST_TIMEOUT,
        max_retries: int = MAX_RETRIES,
        backoff_base: float = BACKOFF_BASE,
    ):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.call_cap = call_cap
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.call_count: int = 0
        self.usage_throttled: bool = False
        self.max_usage_observed: float = 0.0
        self.raw_responses: list[tuple[str, dict]] = []

    def _check_usage_headers(self, headers):
        if not headers:
            return
        # 1. X-App-Usage: {"call_count": 28, "total_cputime": 25, "total_time": 25}
        app_usage = headers.get("X-App-Usage") or headers.get("x-app-usage")
        if app_usage:
            try:
                data = json.loads(app_usage) if isinstance(app_usage, str) else app_usage
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, (int, float)):
                            self.max_usage_observed = max(self.max_usage_observed, float(v))
                            if float(v) > 80.0:
                                self.usage_throttled = True
                                logger.warning(f"Meta X-App-Usage exceeded 80%: {k}={v}%")
            except Exception:
                pass

        # 2. X-Business-Use-Case-Usage
        biz_usage = headers.get("X-Business-Use-Case-Usage") or headers.get("x-business-use-case-usage")
        if biz_usage:
            try:
                data = json.loads(biz_usage) if isinstance(biz_usage, str) else biz_usage
                if isinstance(data, dict):
                    for items in data.values():
                        if isinstance(items, list):
                            for entry in items:
                                if isinstance(entry, dict):
                                    for k in ("call_count", "total_cputime", "total_time"):
                                        v = entry.get(k)
                                        if isinstance(v, (int, float)):
                                            self.max_usage_observed = max(self.max_usage_observed, float(v))
                                            if float(v) > 80.0:
                                                self.usage_throttled = True
                                                logger.warning(f"Meta X-Business-Use-Case-Usage exceeded 80%: {k}={v}%")
            except Exception:
                pass

    def get(
        self,
        path: str,
        params: dict | None = None,
        label: str | None = None,
    ) -> tuple[int, dict]:
        """GET with retry. Returns (status_code, parsed_json)."""
        params = dict(params or {})
        params["access_token"] = self._token
        url = path if path.startswith("http://") or path.startswith("https://") else f"{self.base_url}/{path}"
        label = label or path

        resp = None
        for attempt in range(self.max_retries + 1):
            if self.call_count >= self.call_cap:
                if resp is not None:
                    break
                raise CallCapReached(f"Hit {self.call_cap}-call cap")
            self.call_count += 1

            try:
                resp = requests.get(url, params=params, timeout=self.timeout)
            except (requests.Timeout, requests.ConnectionError):
                if attempt < self.max_retries:
                    time.sleep(self.backoff_base * (2 ** attempt))
                    continue
                raise

            self._check_usage_headers(resp.headers)

            if resp.status_code < 500:
                break

            if attempt < self.max_retries:
                time.sleep(self.backoff_base * (2 ** attempt))

        try:
            data = resp.json()
        except ValueError:
            data = {"raw_body": resp.text[:2000]}

        self.raw_responses.append((label, {
            "status_code": resp.status_code,
            "path": path,
            "data": data,
        }))
        return resp.status_code, data
