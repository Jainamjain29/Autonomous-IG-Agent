"""Instagram metric collector and snapshot scheduler.

CLI:
    python -m insight.collect [--dry-run] [--call-cap N]

Runs as an idempotent single command, triggered by Windows Task Scheduler
every 30 minutes.
"""
from __future__ import annotations

import argparse
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select

import database as db
from .adapters.instagram import InstagramAdapter, is_reel
from .checkpoints import DUE, MISSED, POST_CHECKPOINTS, UNRECOVERABLE, due_checkpoints, evaluate_checkpoints
from .db import REPO_ROOT, make_engine, session_factory, upgrade_db
from .http_client import CallCapReached, GraphClient
from .models import Account, MetricSnapshot, Publication
from .storage import get_or_create_account, save_comment, save_snapshot, upsert_publication
from .timeutil import UTC

# Config: observed Meta API limit for account insights since parameter is 729 days (2 years)
ACCOUNT_INSIGHTS_MAX_LOOKBACK_DAYS = 729
LOCK_STALE_SECONDS = 25 * 60  # 25 minutes
LOCK_PATH = os.path.join(REPO_ROOT, "data", "insight.lock")
LOG_PATH = os.path.join(REPO_ROOT, "data", "logs", "collect.log")


class _StdoutStream:
    def write(self, s: str) -> int:
        return sys.stdout.write(s)

    def flush(self) -> None:
        sys.stdout.flush()


def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("insight.collect")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    has_file = any(
        isinstance(h, RotatingFileHandler)
        or (isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", None) == os.path.abspath(LOG_PATH))
        for h in logger.handlers
    )
    has_stream = any(
        isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
        for h in logger.handlers
    )

    if not has_file:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        fh = RotatingFileHandler(LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        formatter = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s")
        fh.setFormatter(formatter)
        logger.addHandler(fh)

    if not has_stream:
        sh = logging.StreamHandler(_StdoutStream())
        sh.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(sh)
    return logger


@contextmanager
def acquire_lock(lock_path: str = LOCK_PATH):
    """File lock ensuring only one collector runs at a time. Clears stale locks older than 25 min."""
    logger = logging.getLogger("insight.collect")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    now = time.time()

    if os.path.exists(lock_path):
        try:
            mtime = os.path.getmtime(lock_path)
            if now - mtime > LOCK_STALE_SECONDS:
                logger.warning(f"[collect] Found stale lock (age {int(now - mtime)}s > {LOCK_STALE_SECONDS}s). Clearing.")
                os.remove(lock_path)
            else:
                with open(lock_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                logger.warning(f"[collect] Another collector process is active (lock: {content}). Exiting.")
                sys.exit(0)
        except OSError as e:
            logger.warning(f"[collect] Error checking lock file: {e}")

    try:
        with open(lock_path, "w", encoding="utf-8") as f:
            f.write(f"pid={os.getpid()} started_at={datetime.now(UTC).isoformat()}\n")
        yield
    finally:
        if os.path.exists(lock_path):
            try:
                os.remove(lock_path)
            except OSError:
                pass


class Collector:
    def __init__(self, dry_run: bool = False, call_cap: int = 200, engine=None, collect_comments: bool = True):
        self.dry_run = dry_run
        self.call_cap = call_cap
        self.engine = engine or make_engine()
        self.collect_comments = collect_comments
        self.logger = _setup_logger()

        host = db.get_setting("GRAPH_HOST") or "graph.instagram.com"
        version = db.get_setting("GRAPH_VERSION") or "v26.0"
        token = db.get_setting("META_ACCESS_TOKEN")
        self.ig_user_id = db.get_setting("IG_USER_ID")

        if not token or not self.ig_user_id:
            raise RuntimeError("Missing META_ACCESS_TOKEN or IG_USER_ID in settings/.env")

        self.client = GraphClient(
            base_url=f"https://{host}/{version}",
            token=token,
            call_cap=self.call_cap,
        )
        self.adapter = InstagramAdapter(client=self.client, ig_user_id=self.ig_user_id)

    def collect(self) -> dict[str, Any]:
        start_time = time.time()
        now = datetime.now(UTC)
        stats = {
            "posts_synced": 0,
            "tagged": 0,
            "comments_new": 0,
            "labelled": 0,
            "snap_new": 0,
            "snap_unavail": 0,
            "errors": 0,
            "calls": 0,
            "elapsed": 0.0,
        }

        # 1. Ensure DB schema is up to date
        if not self.dry_run:
            upgrade_db(self.engine)

        session = session_factory(self.engine)()
        try:
            # 2. Get or create Account
            status, profile = self.client.get(
                self.ig_user_id,
                params={"fields": "id,username,followers_count"},
                label="account_profile",
            )
            handle = profile.get("username", "unknown") if status < 400 else "unknown"
            self.adapter.own_handle = handle
            account = get_or_create_account(
                session=session,
                platform="instagram",
                platform_account_id=self.ig_user_id,
                handle=handle,
                connected_at=now,
            )

            # 3. Sync all publications
            pubs = self.adapter.list_publications(since=None)
            for prec in pubs:
                upsert_publication(session, account, prec)
                stats["posts_synced"] += 1
            session.flush()

            # 3b. AI Tagging for untagged publications (max 10 per run)
            if not self.dry_run:
                try:
                    from .tagging import tag_untagged_publications
                    all_pubs = session.scalars(
                        select(Publication).filter_by(account_id=account.id)
                    ).all()
                    stats["tagged"] = tag_untagged_publications(session, all_pubs, max_posts=10)
                    session.flush()
                except Exception as tag_err:
                    self.logger.warning(f"[collect] Tagging error (non-fatal): {tag_err}")

            # 4. Process post checkpoints
            db_pubs = session.scalars(
                select(Publication).filter_by(account_id=account.id)
            ).all()

            for pub in db_pubs:
                if not is_reel(pub.media_type, pub.media_product_type):
                    continue

                age = now - pub.published_at
                existing_snaps = session.scalars(
                    select(MetricSnapshot).filter_by(
                        subject_type="publication",
                        subject_id=pub.id,
                    )
                ).all()
                existing_checkpoints = {s.checkpoint for s in existing_snaps}

                # Backfill check: post first seen when already older than checkpoints
                if not existing_checkpoints and age > timedelta(seconds=POST_CHECKPOINTS["1h"]):
                    chk_statuses = evaluate_checkpoints(pub.published_at, now, collected=())
                    for s in chk_statuses:
                        if s.state == UNRECOVERABLE:
                            _, created = save_snapshot(
                                session,
                                checkpoint=s.checkpoint,
                                collected_at=now,
                                result=None,
                                publication=pub,
                            )
                            if created:
                                stats["snap_unavail"] += 1

                    # Take one 'adhoc' backfill snapshot of current values
                    if "adhoc" not in existing_checkpoints:
                        res = self.adapter.fetch_post_metrics(pub)
                        _, created = save_snapshot(
                            session,
                            checkpoint="adhoc",
                            collected_at=now,
                            result=res,
                            publication=pub,
                        )
                        if created:
                            stats["snap_new"] += 1

                # Normal checkpoints for posts < 28 days old
                if age <= timedelta(days=28):
                    due = due_checkpoints(pub, now, collected=existing_checkpoints)
                    for s in due:
                        if s.state in (DUE, MISSED):
                            res = self.adapter.fetch_post_metrics(pub)
                            _, created = save_snapshot(
                                session,
                                checkpoint=s.checkpoint,
                                collected_at=now,
                                result=res,
                                publication=pub,
                                delayed=(s.state == MISSED),
                            )
                            if created:
                                stats["snap_new"] += 1
                        elif s.state == UNRECOVERABLE:
                            _, created = save_snapshot(
                                session,
                                checkpoint=s.checkpoint,
                                collected_at=now,
                                result=None,
                                publication=pub,
                            )
                            if created:
                                stats["snap_unavail"] += 1

                # Comments collection for posts <= 28 days old
                if self.collect_comments and age <= timedelta(days=28):
                    if not self.client.usage_throttled and self.client.call_count < self.client.call_cap:
                        try:
                            comment_records = self.adapter.fetch_comments(pub)
                            for crec in comment_records:
                                _, created = save_comment(session, pub, crec)
                                if created:
                                    stats["comments_new"] += 1
                            session.flush()
                        except Exception as c_err:
                            self.logger.warning(f"[collect] Error fetching comments for {pub.platform_post_id}: {c_err}")

                if self.client.usage_throttled:
                    self.logger.warning("[collect] Usage throttled >80%; pausing post checkpoint collection")
                    break

            # 5. Account daily snapshots
            if not self.client.usage_throttled and self.client.call_count < self.client.call_cap:
                yesterday = now.date() - timedelta(days=1)
                d_connected = account.connected_at.date()
                d_max_lookback = now.date() - timedelta(days=ACCOUNT_INSIGHTS_MAX_LOOKBACK_DAYS)
                earliest_pub_dt = session.scalar(
                    select(func.min(Publication.published_at)).filter_by(account_id=account.id)
                )
                candidates = [d_connected, d_max_lookback]
                if earliest_pub_dt is not None:
                    candidates.append(earliest_pub_dt.date())
                start_bound = max(candidates)

                latest_daily = session.scalar(
                    select(MetricSnapshot).filter_by(
                        subject_type="account",
                        subject_id=account.id,
                        checkpoint="daily",
                    ).order_by(MetricSnapshot.period_key.desc()).limit(1)
                )

                if latest_daily is not None:
                    last_date = date.fromisoformat(latest_daily.period_key)
                    start_date = max(last_date + timedelta(days=1), d_max_lookback)
                else:
                    start_date = min(start_bound, yesterday)

                days_to_collect: list[date] = []
                cur = start_date
                while cur <= yesterday:
                    days_to_collect.append(cur)
                    cur += timedelta(days=1)

                # Never more days per run than fit in the call cap; continue on next run
                calls_remaining = max(0, self.client.call_cap - self.client.call_count)
                today_str = now.date().isoformat()
                existing_today_adhoc = session.scalar(
                    select(MetricSnapshot).filter(
                        MetricSnapshot.subject_type == "account",
                        MetricSnapshot.account_id == account.id,
                        MetricSnapshot.checkpoint == "adhoc",
                        func.date(MetricSnapshot.collected_at) == today_str,
                    )
                )
                followers_needed = (existing_today_adhoc is None)
                if followers_needed and calls_remaining > 0:
                    daily_budget = max(0, calls_remaining - 1)
                else:
                    daily_budget = calls_remaining

                days_to_collect = days_to_collect[:daily_budget]

                for target_date in days_to_collect:
                    if self.client.usage_throttled or self.client.call_count >= self.client.call_cap:
                        self.logger.warning("[collect] Call cap or usage throttled >80%; pausing daily account collection")
                        break
                    date_key = target_date.isoformat()
                    days_ago = (now.date() - target_date).days
                    if days_ago <= ACCOUNT_INSIGHTS_MAX_LOOKBACK_DAYS:
                        res = self.adapter.fetch_account_metrics(target_date)
                        _, created = save_snapshot(
                            session,
                            checkpoint="daily",
                            collected_at=now,
                            result=res,
                            account=account,
                            period_key=date_key,
                        )
                    else:
                        _, created = save_snapshot(
                            session,
                            checkpoint="daily",
                            collected_at=now,
                            result=None,
                            account=account,
                            period_key=date_key,
                        )
                        stats["snap_unavail"] += 1

                    if created:
                        stats["snap_new"] += 1

            # 6. Followers snapshot: account 'adhoc' snapshot at most once per UTC day
            today_str = now.date().isoformat()
            existing_today_adhoc = session.scalar(
                select(MetricSnapshot).filter(
                    MetricSnapshot.subject_type == "account",
                    MetricSnapshot.account_id == account.id,
                    MetricSnapshot.checkpoint == "adhoc",
                    func.date(MetricSnapshot.collected_at) == today_str,
                )
            )
            if existing_today_adhoc is None and not self.client.usage_throttled and self.client.call_count < self.client.call_cap:
                count, followers_res = self.adapter.fetch_account_followers()
                if count is not None:
                    _, created = save_snapshot(
                        session,
                        checkpoint="adhoc",
                        collected_at=now,
                        result=followers_res,
                        account=account,
                    )
                    if created:
                        stats["snap_new"] += 1

            # 7. AI Audience Labeling for unlabelled comments (max 100 per run)
            if not self.dry_run and self.collect_comments:
                try:
                    from .audience import tag_unlabelled_comments
                    stats["labelled"] = tag_unlabelled_comments(session, max_comments=100)
                    session.flush()
                except Exception as aud_err:
                    self.logger.warning(f"[collect] Audience tagging error (non-fatal): {aud_err}")

            if self.dry_run:
                session.rollback()
                self.logger.info("[collect] DRY-RUN mode: rolled back all changes.")
            else:
                session.commit()

        except Exception as e:
            stats["errors"] += 1
            session.rollback()
            self.logger.error(f"[collect] Error during collection: {e}", exc_info=True)
            raise
        finally:
            session.close()
            stats["calls"] = self.client.call_count
            stats["elapsed"] = round(time.time() - start_time, 2)

        summary_line = (
            f"[collect] OK: synced={stats['posts_synced']} tagged={stats['tagged']} "
            f"comments={stats['comments_new']} labelled={stats['labelled']} "
            f"snap_new={stats['snap_new']} snap_unavail={stats['snap_unavail']} errors={stats['errors']} "
            f"calls={stats['calls']}/{self.call_cap} elapsed={stats['elapsed']}s"
        )
        self.logger.info(summary_line)
        return stats


def main():
    parser = argparse.ArgumentParser(description="StreamOvate Insight Collector")
    parser.add_argument("--dry-run", action="store_true", help="Fetch from API but write nothing to database")
    parser.add_argument("--call-cap", type=int, default=200, help="Per-run API call limit (default 200)")
    args = parser.parse_args()

    _setup_logger()
    with acquire_lock():
        try:
            collector = Collector(dry_run=args.dry_run, call_cap=args.call_cap)
            collector.collect()
        except CallCapReached as e:
            logging.getLogger("insight.collect").warning(f"[collect] Call cap reached: {e}")
            sys.exit(0)
        except Exception as exc:
            logging.getLogger("insight.collect").error(f"[collect] FATAL: {exc}")
            sys.exit(1)


if __name__ == "__main__":
    main()
