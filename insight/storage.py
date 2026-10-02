"""Idempotent writers. Re-collecting the same snapshot or comment is a no-op."""
from sqlalchemy import select

from .checkpoints import POST_CHECKPOINTS, period_key_for
from .models import Account, Comment, MetricSnapshot, MetricValue, Publication, RawResponse
from .privacy import redact_secrets
from .timeutil import to_utc


def get_or_create_account(session, platform, platform_account_id, handle, connected_at):
    account = session.scalar(select(Account).filter_by(platform=platform, platform_account_id=platform_account_id))
    if account is None:
        account = Account(platform=platform, platform_account_id=platform_account_id,
                          handle=handle, connected_at=connected_at)
        session.add(account)
        session.flush()
    return account


def upsert_publication(session, account, record, content_id=None):
    """Insert a PublicationRecord, or refresh caption/permalink on an existing row."""
    pub = session.scalar(select(Publication).filter_by(
        platform=account.platform, platform_post_id=record.platform_post_id))
    if pub is None:
        pub = Publication(platform=account.platform, platform_post_id=record.platform_post_id,
                          account_id=account.id, media_type=record.media_type,
                          media_product_type=record.media_product_type,
                          published_at=record.published_at, content_id=content_id)
        session.add(pub)
    pub.caption = record.caption
    pub.permalink = record.permalink
    if record.media_product_type is not None:
        pub.media_product_type = record.media_product_type
    if content_id is not None:
        pub.content_id = content_id
    session.flush()
    return pub


def save_raw_response(session, platform, endpoint, payload, fetched_at):
    raw = RawResponse(platform=platform, endpoint=endpoint, fetched_at=fetched_at,
                      payload=redact_secrets(payload))
    session.add(raw)
    session.flush()
    return raw


def _completeness(result, delayed):
    if result is None or not result.values:
        return "unavailable"
    if result.missing:
        return "partial"
    return "delayed" if delayed else "complete"


def save_snapshot(session, *, checkpoint, collected_at, result=None, publication=None, account=None,
                  delayed=False, platform=None, period_key=None):
    """Store one snapshot + its long-format values. Returns (snapshot, created).

    result=None records an 'unavailable' snapshot (e.g. an unrecoverable checkpoint).
    If a snapshot with the same (subject, checkpoint, period_key) exists, nothing is written.
    """
    if (publication is None) == (account is None):
        raise ValueError("pass exactly one of publication or account")
    if checkpoint in POST_CHECKPOINTS and publication is None:
        raise ValueError(f"checkpoint {checkpoint!r} is for publications only")
    collected_at = to_utc(collected_at)
    subject_type, subject = ("publication", publication) if publication is not None else ("account", account)
    period_key = period_key or period_key_for(checkpoint, collected_at)

    existing = session.scalar(select(MetricSnapshot).filter_by(
        subject_type=subject_type, subject_id=subject.id, checkpoint=checkpoint, period_key=period_key))
    if existing is not None:
        return existing, False

    raw = None
    if result is not None:
        raw = save_raw_response(session, platform or subject.platform, result.endpoint,
                                result.raw_payload, result.fetched_at)
    snap = MetricSnapshot(
        subject_type=subject_type,
        subject_id=subject.id,
        publication_id=publication.id if publication is not None else None,
        account_id=account.id if account is not None else None,
        checkpoint=checkpoint,
        period_key=period_key,
        collected_at=collected_at,
        time_since_publish_seconds=(
            int((collected_at - publication.published_at).total_seconds()) if publication is not None else None),
        completeness=_completeness(result, delayed),
        raw_response=raw,
    )
    if result is not None:
        for name, value in sorted(result.values.items()):
            snap.values.append(MetricValue(canonical_metric=name, value=value))
        for name, reason in sorted(result.missing.items()):
            snap.values.append(MetricValue(canonical_metric=name, value=None, missing_reason=reason))
    session.add(snap)
    session.flush()
    return snap, True


def save_comment(session, publication, record):
    """Store a CommentRecord. Returns (comment, created). Parents must be saved first."""
    existing = session.scalar(select(Comment).filter_by(
        platform=publication.platform, platform_comment_id=record.platform_comment_id))
    if existing is not None:
        return existing, False
    parent_id = None
    if record.parent_platform_comment_id:
        parent = session.scalar(select(Comment).filter_by(
            platform=publication.platform, platform_comment_id=record.parent_platform_comment_id))
        if parent is None:
            raise ValueError(f"parent comment {record.parent_platform_comment_id} not stored yet")
        parent_id = parent.id
    comment = Comment(
        platform=publication.platform,
        platform_comment_id=record.platform_comment_id,
        publication_id=publication.id,
        text=record.text,
        created_at=record.created_at,
        like_count=record.like_count,
        author_hash=record.author_hash,
        parent_comment_id=parent_id,
    )
    session.add(comment)
    session.flush()
    return comment, True
