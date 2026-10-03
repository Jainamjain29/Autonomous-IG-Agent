"""Insight Agent storage schema (SQLAlchemy 2.x).

Portable on purpose: only types and constraints that behave the same on SQLite
and Postgres. All timestamps are UTCDateTime (UTC in, UTC out).
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .checkpoints import ALL_CHECKPOINTS
from .timeutil import UTCDateTime

COMPLETENESS = ("complete", "partial", "delayed", "unavailable")
SUBJECT_TYPES = ("publication", "account")


def _in(column, values):
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("platform", "platform_account_id", name="uq_account_platform_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32))
    platform_account_id: Mapped[str] = mapped_column(String(128))
    handle: Mapped[Optional[str]] = mapped_column(String(255))
    connected_at: Mapped[datetime] = mapped_column(UTCDateTime())

    publications: Mapped[list["Publication"]] = relationship(back_populates="account")


class Publication(Base):
    __tablename__ = "publications"
    __table_args__ = (UniqueConstraint("platform", "platform_post_id", name="uq_publication_platform_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32))
    platform_post_id: Mapped[str] = mapped_column(String(128))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    # Links to the content pipeline later; no FK because that lives in another DB.
    content_id: Mapped[Optional[str]] = mapped_column(String(128))
    media_type: Mapped[str] = mapped_column(String(32))
    media_product_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    caption: Mapped[Optional[str]] = mapped_column(Text)
    permalink: Mapped[Optional[str]] = mapped_column(String(1024))
    published_at: Mapped[datetime] = mapped_column(UTCDateTime())

    account: Mapped[Account] = relationship(back_populates="publications")


class MetricDefinition(Base):
    """The metric dictionary. Loaded from insight/seeds/metric_dictionary.v*.json."""

    __tablename__ = "metric_definitions"
    __table_args__ = (
        UniqueConstraint("platform", "canonical_name", "applies_to", name="uq_metric_definition"),
        CheckConstraint(_in("status", ("active", "todo")), name="ck_metric_definition_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical_name: Mapped[str] = mapped_column(String(64))
    platform: Mapped[str] = mapped_column(String(32))
    # NULL while status == 'todo' (name not confirmed yet).
    platform_metric_name: Mapped[Optional[str]] = mapped_column(String(128))
    unit: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text)
    applies_to: Mapped[str] = mapped_column(String(32))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    # Multiplier from the platform's unit to `unit` (e.g. 0.001 for ms -> seconds).
    scale: Mapped[float] = mapped_column(Float, default=1.0)
    status: Mapped[str] = mapped_column(String(16), default="active")
    seed_version: Mapped[int] = mapped_column(Integer)


class RawResponse(Base):
    __tablename__ = "raw_responses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32))
    endpoint: Mapped[str] = mapped_column(String(512))
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime())
    payload: Mapped[dict] = mapped_column(JSON)


class MetricSnapshot(Base):
    """One collection of metrics for one subject at one checkpoint.

    subject_type/subject_id are NOT NULL so the unique key works (NULLs are
    distinct in unique constraints on both SQLite and Postgres). The FK columns
    are kept for joins and referential integrity; the check constraints keep
    them consistent with the subject columns.
    """

    __tablename__ = "metric_snapshots"
    __table_args__ = (
        UniqueConstraint("subject_type", "subject_id", "checkpoint", "period_key", name="uq_snapshot_subject_period"),
        CheckConstraint(
            "(publication_id IS NULL) <> (account_id IS NULL)", name="ck_snapshot_exactly_one_subject"
        ),
        CheckConstraint(
            # Explicit IS NOT NULL: a CHECK that evaluates to NULL counts as passing.
            "(subject_type = 'publication' AND publication_id IS NOT NULL AND publication_id = subject_id)"
            " OR (subject_type = 'account' AND account_id IS NOT NULL AND account_id = subject_id)",
            name="ck_snapshot_subject_matches_fk",
        ),
        CheckConstraint(_in("subject_type", SUBJECT_TYPES), name="ck_snapshot_subject_type"),
        CheckConstraint(_in("checkpoint", ALL_CHECKPOINTS), name="ck_snapshot_checkpoint"),
        CheckConstraint(_in("completeness", COMPLETENESS), name="ck_snapshot_completeness"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_type: Mapped[str] = mapped_column(String(16))
    subject_id: Mapped[int] = mapped_column(Integer)
    publication_id: Mapped[Optional[int]] = mapped_column(ForeignKey("publications.id"))
    account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("accounts.id"))
    checkpoint: Mapped[str] = mapped_column(String(16))
    # Post checkpoints: the label; 'daily': UTC date; 'adhoc': collected_at ISO.
    period_key: Mapped[str] = mapped_column(String(64))
    collected_at: Mapped[datetime] = mapped_column(UTCDateTime())
    # NULL for account snapshots.
    time_since_publish_seconds: Mapped[Optional[int]] = mapped_column(BigInteger)
    completeness: Mapped[str] = mapped_column(String(16))
    raw_response_id: Mapped[Optional[int]] = mapped_column(ForeignKey("raw_responses.id"))

    values: Mapped[list["MetricValue"]] = relationship(back_populates="snapshot", cascade="all, delete-orphan")
    raw_response: Mapped[Optional[RawResponse]] = relationship()


class MetricValue(Base):
    """Long format: one row per canonical metric per snapshot.

    value is NULL when the metric was expected but missing; missing_reason says why.
    """

    __tablename__ = "metric_values"
    __table_args__ = (UniqueConstraint("snapshot_id", "canonical_metric", name="uq_value_snapshot_metric"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("metric_snapshots.id"))
    canonical_metric: Mapped[str] = mapped_column(String(64))
    value: Mapped[Optional[float]] = mapped_column(Float)
    missing_reason: Mapped[Optional[str]] = mapped_column(String(255))

    snapshot: Mapped[MetricSnapshot] = relationship(back_populates="values")


class Comment(Base):
    __tablename__ = "comments"
    __table_args__ = (UniqueConstraint("platform", "platform_comment_id", name="uq_comment_platform_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32))
    platform_comment_id: Mapped[str] = mapped_column(String(128))
    publication_id: Mapped[int] = mapped_column(ForeignKey("publications.id"))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    like_count: Mapped[Optional[int]] = mapped_column(Integer)
    # SHA-256(salt + username). Raw usernames are never stored.
    author_hash: Mapped[Optional[str]] = mapped_column(String(64))
    parent_comment_id: Mapped[Optional[int]] = mapped_column(ForeignKey("comments.id"))
    is_own_account: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    labels: Mapped[list["CommentLabel"]] = relationship(back_populates="comment", cascade="all, delete-orphan")


class CommentLabel(Base):
    """Audience intelligence classification (category, sentiment, needs_reply, theme) for a comment."""

    __tablename__ = "comment_labels"
    __table_args__ = (
        UniqueConstraint("comment_id", "prompt_version", name="uq_comment_labels_comment_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    comment_id: Mapped[int] = mapped_column(ForeignKey("comments.id", ondelete="CASCADE"))
    category: Mapped[str] = mapped_column(String(32))
    sentiment: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[float] = mapped_column(Float)
    needs_reply: Mapped[bool] = mapped_column(Boolean)
    needs_reply_reason: Mapped[Optional[str]] = mapped_column(String(255))
    theme: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(32), default="ai")
    model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(16))
    input_hash: Mapped[str] = mapped_column(String(64))
    labelled_at: Mapped[datetime] = mapped_column(UTCDateTime())

    comment: Mapped["Comment"] = relationship(back_populates="labels")


class PostTag(Base):
    """Categorization tags for a publication (topic, format, hook) from AI analysis."""

    __tablename__ = "post_tags"
    __table_args__ = (
        UniqueConstraint("publication_id", "dimension", "prompt_version", name="uq_post_tags_pub_dim_ver"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    publication_id: Mapped[int] = mapped_column(ForeignKey("publications.id", ondelete="CASCADE"))
    dimension: Mapped[str] = mapped_column(String(32))  # 'topic', 'format', 'hook'
    value: Mapped[str] = mapped_column(String(64))
    confidence: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(32), default="ai")
    model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(16))
    input_hash: Mapped[str] = mapped_column(String(64))
    tagged_at: Mapped[datetime] = mapped_column(UTCDateTime())

