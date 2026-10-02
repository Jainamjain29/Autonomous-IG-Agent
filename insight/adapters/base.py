"""Platform adapter interface.

An adapter talks to one platform and returns plain records. It never writes to
the DB (that is insight.storage's job) and never invents values: metrics that
are absent from the API response come back in MetricResult.missing.
Raw usernames never leave the adapter; it hashes them via insight.privacy.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from ..dictionary import MetricDictionary
from ..privacy import redact_secrets


@dataclass(frozen=True)
class Capabilities:
    platform: str
    post_metrics: frozenset          # canonical metric names available per post
    account_metrics: frozenset       # canonical metric names available per account
    features: frozenset = frozenset()  # e.g. {"comments", "comment_replies"}


@dataclass(frozen=True)
class PublicationRecord:
    platform_post_id: str
    platform_account_id: str
    media_type: str
    published_at: datetime
    caption: str | None = None
    permalink: str | None = None


@dataclass
class MetricResult:
    endpoint: str
    fetched_at: datetime
    raw_payload: dict                              # tokens already stripped
    values: dict = field(default_factory=dict)     # canonical -> float
    missing: dict = field(default_factory=dict)    # canonical -> reason
    unmapped: dict = field(default_factory=dict)   # platform name -> raw value


@dataclass(frozen=True)
class CommentRecord:
    platform_comment_id: str
    text: str
    created_at: datetime
    author_hash: str | None
    like_count: int | None = None
    parent_platform_comment_id: str | None = None


class PlatformAdapter(ABC):
    platform: str = ""

    def __init__(self, dictionary=None):
        self.dictionary = dictionary or MetricDictionary(self.platform)

    @abstractmethod
    def capabilities(self) -> Capabilities: ...

    @abstractmethod
    def list_publications(self, since: datetime) -> list[PublicationRecord]: ...

    @abstractmethod
    def fetch_post_metrics(self, publication) -> MetricResult: ...

    @abstractmethod
    def fetch_account_metrics(self) -> MetricResult: ...

    @abstractmethod
    def fetch_comments(self, publication, since: datetime) -> list[CommentRecord]: ...

    def build_result(self, endpoint, fetched_at, raw_payload, flat_metrics, applies_to):
        """Shared helper: map {platform_name: value} through the dictionary."""
        mapped = self.dictionary.map(flat_metrics, applies_to)
        return MetricResult(
            endpoint=endpoint,
            fetched_at=fetched_at,
            raw_payload=redact_secrets(raw_payload),
            values=mapped.values,
            missing=mapped.missing,
            unmapped=mapped.unmapped,
        )
