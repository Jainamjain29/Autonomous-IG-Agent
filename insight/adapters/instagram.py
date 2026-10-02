"""Instagram Graph API platform adapter."""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

from ..dictionary import MetricDictionary
from ..http_client import GraphClient
from ..timeutil import UTC
from .base import Capabilities, CommentRecord, MetricResult, PlatformAdapter, PublicationRecord

logger = logging.getLogger("insight.adapters.instagram")


def is_reel(media_type: str | None, media_product_type: str | None) -> bool:
    """A post is collected as a Reel when media_product_type == 'REELS' (media_type is VIDEO).
    Also supports legacy/fake fixture records where media_type == 'reel'.
    """
    if media_product_type == "REELS" and media_type == "VIDEO":
        return True
    if media_product_type == "REELS":
        return True
    if media_type == "reel":
        return True
    return False


def _parse_utc_iso(ts_str: str) -> datetime:
    """Parse ISO8601 strings from Meta API like '2026-10-02T10:10:25+0000' to aware UTC datetime."""
    # Handle +0000 -> +00:00
    if ts_str.endswith("+0000"):
        ts_str = ts_str[:-5] + "+00:00"
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _extract_metric_value(item: dict) -> Any:
    tv = item.get("total_value")
    if isinstance(tv, dict) and "value" in tv:
        return tv["value"]
    vals = item.get("values", [])
    if vals and isinstance(vals[0], dict):
        return vals[0].get("value")
    return None


class InstagramAdapter(PlatformAdapter):
    platform = "instagram"

    def __init__(self, client: GraphClient, ig_user_id: str, dictionary: MetricDictionary | None = None):
        super().__init__(dictionary or MetricDictionary(self.platform))
        self.client = client
        self.ig_user_id = str(ig_user_id)

    def capabilities(self) -> Capabilities:
        return Capabilities(
            platform=self.platform,
            post_metrics=self.dictionary.canonical_metrics("reel"),
            account_metrics=self.dictionary.canonical_metrics("account"),
            features=frozenset({"comments", "comment_replies"}),
        )

    def list_publications(self, since: datetime | None = None) -> list[PublicationRecord]:
        """Fetch publications from /{ig_user_id}/media, following cursor pagination.
        Stops when paging ends or posts become older than `since`.
        """
        records: list[PublicationRecord] = []
        path = f"{self.ig_user_id}/media"
        params: dict[str, Any] = {
            "fields": "id,media_type,media_product_type,timestamp,permalink,caption",
            "limit": 50,
        }

        while path:
            status, payload = self.client.get(path, params=params, label="list_publications")
            if status >= 400 or not isinstance(payload, dict):
                logger.error(f"Failed to fetch publications: HTTP {status} {payload}")
                break

            items = payload.get("data", [])
            for item in items:
                pub_time = _parse_utc_iso(item["timestamp"])
                rec = PublicationRecord(
                    platform_post_id=item["id"],
                    platform_account_id=self.ig_user_id,
                    media_type=item.get("media_type", "UNKNOWN"),
                    published_at=pub_time,
                    caption=item.get("caption"),
                    permalink=item.get("permalink"),
                    media_product_type=item.get("media_product_type"),
                )
                records.append(rec)

            if since and items:
                oldest_in_page = _parse_utc_iso(items[-1]["timestamp"])
                if oldest_in_page < since:
                    break

            if self.client.usage_throttled:
                logger.warning("Usage throttled >80%; stopping publication pagination")
                break

            paging = payload.get("paging", {})
            next_url = paging.get("next")
            if next_url:
                path = next_url
                params = {}  # URL already contains params
            else:
                break

        return records

    def fetch_post_metrics(self, publication: Any) -> MetricResult:
        """Fetch metrics for a publication. Non-Reels return unsupported missing reasons."""
        m_type = getattr(publication, "media_type", None)
        m_prod = getattr(publication, "media_product_type", None)
        post_id = getattr(publication, "platform_post_id", str(publication))

        now = datetime.now(UTC)
        if not is_reel(m_type, m_prod):
            return MetricResult(
                endpoint=f"{post_id}/insights",
                fetched_at=now,
                raw_payload={"error": "unsupported media type", "media_type": m_type, "media_product_type": m_prod},
                values={},
                missing={m: "unsupported media type" for m in self.dictionary.canonical_metrics("reel")},
            )

        metric_names = [r.platform_metric_name for r in self.dictionary._rows if r.applies_to == "reel"]
        status, data = self.client.get(
            f"{post_id}/insights",
            params={"metric": ",".join(metric_names)},
            label=f"post_insights_{post_id}",
        )

        flat_metrics: dict[str, Any] = {}
        if status < 400 and isinstance(data, dict):
            for item in data.get("data", []):
                val = _extract_metric_value(item)
                if val is not None:
                    flat_metrics[item.get("name")] = val

        return self.build_result(f"{post_id}/insights", now, data if isinstance(data, dict) else {}, flat_metrics, "reel")

    def fetch_account_metrics(self, target_date: date) -> MetricResult:
        """Fetch day-bounded account insights (profile_views, reach, views) for a specific UTC date.
        Followers is deliberately NOT included here (it is an adhoc 'now' value).
        """
        now = datetime.now(UTC)
        start_dt = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0, tzinfo=timezone.utc)
        end_dt = datetime(target_date.year, target_date.month, target_date.day, 23, 59, 59, tzinfo=timezone.utc)

        since = int(start_dt.timestamp())
        until = int(end_dt.timestamp())

        # Daily account metrics from the dictionary (exclude followers_count which is a User field)
        account_metrics = [
            r.platform_metric_name for r in self.dictionary._rows
            if r.applies_to == "account" and r.platform_metric_name != "followers_count"
        ]

        status, data = self.client.get(
            f"{self.ig_user_id}/insights",
            params={
                "metric": ",".join(account_metrics),
                "period": "day",
                "metric_type": "total_value",
                "since": since,
                "until": until,
            },
            label=f"account_insights_{target_date.isoformat()}",
        )

        flat_metrics: dict[str, Any] = {}
        if status < 400 and isinstance(data, dict):
            for item in data.get("data", []):
                val = _extract_metric_value(item)
                if val is not None:
                    flat_metrics[item.get("name")] = val

        res = self.build_result(f"{self.ig_user_id}/insights", now, data if isinstance(data, dict) else {}, flat_metrics, "account")
        # Followers is a point-in-time value, not a day-bounded insight; remove it from missing for daily snapshots
        res.missing.pop("followers", None)
        return res

    def fetch_account_followers(self) -> tuple[int | None, MetricResult]:
        """Fetch current followers_count from User profile for account 'adhoc' snapshot."""
        now = datetime.now(UTC)
        status, data = self.client.get(
            self.ig_user_id,
            params={"fields": "id,username,followers_count"},
            label="account_followers",
        )

        count = None
        flat_metrics: dict[str, Any] = {}
        if status < 400 and isinstance(data, dict):
            count = data.get("followers_count")
            if count is not None:
                flat_metrics["followers_count"] = count

        res = self.build_result(self.ig_user_id, now, data if isinstance(data, dict) else {}, flat_metrics, "account")
        # For adhoc followers snapshot, we only expect followers
        res.missing = {}
        return count, res

    def fetch_comments(self, publication: Any, since: datetime) -> list[CommentRecord]:
        raise NotImplementedError("Step 5")
