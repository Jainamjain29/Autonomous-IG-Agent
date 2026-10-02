"""FakeAdapter: implements PlatformAdapter over an insight.fixtures Dataset.

Responses are shaped like Instagram Graph API insights so the mapping path is
exercised for real. Set `now` to move the clock; `drop_metrics` / `extra_metrics`
simulate missing and unknown metrics.
"""
from ..privacy import hash_author
from .base import Capabilities, CommentRecord, PlatformAdapter, PublicationRecord

FAKE_TOKEN = "FAKE_ACCESS_TOKEN_SHOULD_BE_STRIPPED"


class FakeAdapter(PlatformAdapter):
    platform = "instagram"

    def __init__(self, dataset, now=None, salt=None, dictionary=None):
        super().__init__(dictionary)
        from ..fixtures import FIXTURE_SALT
        self.dataset = dataset
        self.now = now or dataset.now
        self.salt = salt or FIXTURE_SALT
        self.drop_metrics = set()
        self.extra_metrics = {}
        self._reels = {r.post_id: r for r in dataset.reels}

    def capabilities(self):
        return Capabilities(
            platform=self.platform,
            post_metrics=self.dictionary.canonical_metrics("reel"),
            account_metrics=self.dictionary.canonical_metrics("account"),
            features=frozenset({"comments", "comment_replies"}),
        )

    def list_publications(self, since):
        acc = self.dataset.account
        return [
            PublicationRecord(
                platform_post_id=r.post_id,
                platform_account_id=acc.account_id,
                media_type="reel",
                published_at=r.published_at,
                caption=r.caption,
                permalink=f"https://www.instagram.com/reel/FAKE{r.post_id[-6:]}/",
            )
            for r in self.dataset.reels
            if since <= r.published_at <= self.now
        ]

    def _payload(self, endpoint, flat, period):
        flat = {k: v for k, v in flat.items() if k not in self.drop_metrics}
        flat.update(self.extra_metrics)
        payload = {
            "data": [{"name": k, "period": period, "values": [{"value": v}]} for k, v in flat.items()],
            "paging": {"next": f"https://graph.facebook.com/v26.0/{endpoint}?after=X&access_token={FAKE_TOKEN}"},
        }
        return payload, flat

    def fetch_post_metrics(self, publication):
        reel = self._reels[publication.platform_post_id]
        elapsed = (self.now - reel.published_at).total_seconds()
        if elapsed < 0:
            raise ValueError("cannot fetch metrics before the post was published")
        endpoint = f"{reel.post_id}/insights"
        payload, flat = self._payload(endpoint, reel.metrics_at(elapsed), "lifetime")
        return self.build_result(endpoint, self.now, payload, flat, "reel")

    def fetch_account_metrics(self):
        acc = self.dataset.account
        endpoint = f"{acc.account_id}/insights"
        payload, flat = self._payload(endpoint, acc.metrics_on(self.now.date()), "day")
        return self.build_result(endpoint, self.now, payload, flat, "account")

    def fetch_comments(self, publication, since):
        reel = self._reels[publication.platform_post_id]
        return [
            CommentRecord(
                platform_comment_id=c.comment_id,
                text=c.text,
                created_at=c.created_at,
                author_hash=hash_author(c.username, self.salt),
                like_count=c.like_count,
                parent_platform_comment_id=c.parent_id,
            )
            for c in reel.comments
            if since <= c.created_at <= self.now
        ]
