"""Realistic fake Instagram data for testing analysis code.

    python -m insight.fixtures            # (re)build data/insight_sample.db
    python -m insight.fixtures --path X   # somewhere else

Writes only to a sample DB; refuses to touch the real Insight DB.
Deterministic: the same seed always produces the same data.
"""
import argparse
import math
import os
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .checkpoints import ACCOUNT_CHECKPOINT, POST_CHECKPOINTS, grace_seconds
from .db import REAL_DB_PATH, SAMPLE_DB_PATH, init_db, make_engine, real_db_url, session_factory, sqlite_url
from .timeutil import UTC

PLATFORM = "instagram"
DEFAULT_NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
FIXTURE_SALT = "insight-fixture-salt-not-a-secret"

_COMMENT_TEXTS = [
    "This is so useful, thanks!", "Saving this for later", "Wait, how did you do the transition?",
    "Can you make a part 2?", "Not sure I agree with the second point", "First!", "Love this format",
    "Where can I find the full version?", "This didn't work for me", "Underrated content",
    "Too fast, could you slow it down next time?", "The music choice is perfect", "Followed!",
    "What app do you use for editing?", "Sharing this with my team", "Meh, seen this before",
]
_TOPICS = ["morning routine", "productivity hack", "AI tools", "travel tip", "budget recipe",
           "study method", "desk setup", "fitness myth", "coding shortcut", "book summary"]


@dataclass
class FakeComment:
    comment_id: str
    username: str
    text: str
    created_at: datetime
    like_count: int
    parent_id: str | None = None


@dataclass
class FakeReel:
    post_id: str
    published_at: datetime
    caption: str
    profile: str               # "normal" | "viral" | "flop" (fixture metadata only)
    final_views: int
    tau_hours: float           # growth time-constant
    rates: dict                # per-view ratios for reach/likes/...
    avg_watch_ms: int
    comments: list = field(default_factory=list)

    def metrics_at(self, elapsed_seconds):
        """IG-named metrics at `elapsed_seconds` after publish (cumulative)."""
        hours = max(0.0, elapsed_seconds / 3600)
        views = int(self.final_views * (1 - math.exp(-hours / self.tau_hours)))
        m = {
            "views": views,
            "reach": int(views * self.rates["reach"]),
            "likes": int(views * self.rates["likes"]),
            "shares": int(views * self.rates["shares"]),
            "saved": int(views * self.rates["saves"]),
        }
        m["comments"] = int(m["likes"] * self.rates["comments"])
        m["total_interactions"] = m["likes"] + m["comments"] + m["shares"] + m["saved"]
        m["ig_reels_avg_watch_time"] = self.avg_watch_ms if views else 0
        m["ig_reels_video_view_total_time"] = views * self.avg_watch_ms
        return m


@dataclass
class FakeAccountData:
    account_id: str
    handle: str
    connected_at: datetime
    followers_start: int
    seed: int

    def metrics_on(self, day):
        """IG-named account metrics for a UTC date (deterministic per day)."""
        days = (day - self.connected_at.date()).days
        rng = random.Random(f"{self.seed}-{day.isoformat()}")
        return {
            "followers_count": self.followers_start + max(0, days) * 11 + rng.randint(-5, 20),
            "profile_views": rng.randint(40, 260),
            "reach": rng.randint(1500, 9000),
            "views": rng.randint(4000, 25000),
        }


@dataclass
class Dataset:
    now: datetime
    account: FakeAccountData
    reels: list
    special: dict  # which reels exercise delayed/partial/unrecoverable paths


def generate_dataset(seed=42, n_reels=30, days=60, now=DEFAULT_NOW):
    rng = random.Random(seed)
    start = now - timedelta(days=days)
    account = FakeAccountData("17841400000000000", "sample_creator", start, 4800, seed)

    profiles = ["normal"] * n_reels
    picks = rng.sample(range(n_reels), min(5, n_reels))
    for i in picks[:3]:
        profiles[i] = "viral"
    for i in picks[3:]:
        profiles[i] = "flop"

    publish_times = sorted(start + timedelta(seconds=rng.randint(0, days * 86400 - 2 * 3600))
                           for _ in range(n_reels))
    reels = []
    for i, (published_at, profile) in enumerate(zip(publish_times, profiles)):
        base = rng.lognormvariate(math.log(2500), 0.55)
        if profile == "viral":
            base = max(base, 2500)  # outliers should stand out from a typical reel, not a weak one
        mult = {"viral": rng.uniform(8, 20), "flop": rng.uniform(0.08, 0.2)}.get(profile, 1.0)
        reel = FakeReel(
            post_id=f"1790000000000{i:04d}",
            published_at=published_at.replace(microsecond=0),
            caption=f"Quick {rng.choice(_TOPICS)} #{i + 1}",
            profile=profile,
            final_views=max(30, int(base * mult)),
            tau_hours=rng.uniform(14, 40) * (1.6 if profile == "viral" else 1.0),
            rates={
                "reach": rng.uniform(0.55, 0.8),
                "likes": rng.uniform(0.03, 0.08) * (1.3 if profile == "viral" else 1.0),
                "shares": rng.uniform(0.002, 0.012) * (2.5 if profile == "viral" else 1.0),
                "saves": rng.uniform(0.004, 0.02),
                "comments": rng.uniform(0.02, 0.07),
            },
            avg_watch_ms=int(rng.uniform(4.0, 14.0) * 1000),
        )
        n_comments = min(reel.metrics_at(28 * 86400)["comments"], rng.randint(0, 6) + (10 if profile == "viral" else 0))
        for c in range(n_comments):
            parent = None
            if reel.comments and rng.random() < 0.25:
                parent = rng.choice([x for x in reel.comments if x.parent_id is None]).comment_id
            created = reel.published_at + timedelta(seconds=int(rng.expovariate(1 / (reel.tau_hours * 3600))) + 60)
            reel.comments.append(FakeComment(
                comment_id=f"{reel.post_id}_{c:03d}",
                username=f"user_{rng.randint(1, 400)}",
                text=rng.choice(_COMMENT_TEXTS),
                created_at=created,
                like_count=rng.choice([0, 0, 0, 1, 2, 3, 5, 12]),
                parent_id=parent,
            ))
        # Parents must precede replies; replies must not predate their parent.
        reel.comments.sort(key=lambda x: (x.created_at, x.comment_id))
        by_id = {x.comment_id: x for x in reel.comments}
        for x in reel.comments:
            if x.parent_id and by_id[x.parent_id].created_at >= x.created_at:
                x.parent_id = None
        reels.append(reel)

    old = [r for r in reels if (now - r.published_at).days >= 30]
    special = {
        "delayed_24h": old[0].post_id,         # PC was off: 24h collected late, before 48h
        "unrecoverable_1h": old[1].post_id,    # PC off until after 24h -> 1h unavailable
        "partial_7d": old[2].post_id,          # avg watch time missing at 7d
    }
    return Dataset(now=now, account=account, reels=reels, special=special)


def _same_path(url, path):
    return url.startswith("sqlite:///") and os.path.normcase(os.path.abspath(url[10:])) == os.path.normcase(os.path.abspath(path))


def load_sample_db(url=None, dataset=None):
    """Build a sample DB from a Dataset via FakeAdapter. Idempotent. Returns counts."""
    from sqlalchemy import func, select

    from . import storage
    from .adapters.fake import FakeAdapter
    from .models import Comment, MetricSnapshot, Publication

    url = url or sqlite_url(SAMPLE_DB_PATH)
    if url == real_db_url() or _same_path(url, REAL_DB_PATH):
        raise RuntimeError("refusing to load sample data into the real Insight DB")
    dataset = dataset or generate_dataset()
    engine = init_db(make_engine(url))
    adapter = FakeAdapter(dataset)
    special = {v: k for k, v in dataset.special.items()}

    with session_factory(engine).begin() as s:
        acc = dataset.account
        account = storage.get_or_create_account(s, PLATFORM, acc.account_id, acc.handle, acc.connected_at)
        for record in adapter.list_publications(since=acc.connected_at):
            pub = storage.upsert_publication(s, account, record)
            tag = special.get(record.platform_post_id)
            for label, offset in POST_CHECKPOINTS.items():
                due_at = pub.published_at + timedelta(seconds=offset)
                if due_at > dataset.now:
                    continue
                if tag == "unrecoverable_1h" and label == "1h":
                    storage.save_snapshot(s, publication=pub, checkpoint=label,
                                          collected_at=pub.published_at + timedelta(hours=25))
                    continue
                delayed = tag == "delayed_24h" and label == "24h"
                jitter = grace_seconds(label) + 6 * 3600 if delayed else 60 + int(pub.id * 37 % 400)
                adapter.now = due_at + timedelta(seconds=jitter)
                adapter.drop_metrics = {"ig_reels_avg_watch_time"} if tag == "partial_7d" and label == "7d" else set()
                result = adapter.fetch_post_metrics(pub)
                storage.save_snapshot(s, publication=pub, checkpoint=label, collected_at=adapter.now,
                                      result=result, delayed=delayed)
            adapter.now, adapter.drop_metrics = dataset.now, set()
            for comment in adapter.fetch_comments(pub, since=pub.published_at):
                storage.save_comment(s, pub, comment)

        day = acc.connected_at.replace(hour=23, minute=30, second=0, microsecond=0)
        while day <= dataset.now:
            adapter.now = day
            storage.save_snapshot(s, account=account, checkpoint=ACCOUNT_CHECKPOINT, collected_at=day,
                                  result=adapter.fetch_account_metrics())
            day += timedelta(days=1)
        adapter.now = dataset.now

        counts = {
            "publications": s.scalar(select(func.count()).select_from(Publication)),
            "snapshots": s.scalar(select(func.count()).select_from(MetricSnapshot)),
            "comments": s.scalar(select(func.count()).select_from(Comment)),
        }
    engine.dispose()
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the Insight sample database.")
    parser.add_argument("--path", default=SAMPLE_DB_PATH, help="SQLite file to write (default: %(default)s)")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)
    counts = load_sample_db(sqlite_url(args.path), generate_dataset(seed=args.seed))
    print(f"Sample data written to {args.path}: {counts}")


if __name__ == "__main__":
    main()
