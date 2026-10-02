"""Snapshot checkpoints: the single place where collection timing is defined.

Post checkpoints are offsets from published_at. Account metrics use 'daily'
(one snapshot per UTC date). 'adhoc' is a manual pull at any time.

Catch-up rule (for when the PC was off):
  - pending:     not due yet.
  - due:         due, still inside the grace window -> collect, completeness as measured.
  - missed:      past the grace window but the NEXT checkpoint is not due yet ->
                 collect now, mark completeness 'delayed' (time_since_publish_seconds
                 records the real age).
  - unrecoverable: a later checkpoint is already due, so collecting now would just
                 duplicate that later value under an earlier label -> do not collect
                 metrics; record an 'unavailable' snapshot instead. Never guess.
  - done:        already collected.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

from .timeutil import to_utc

POST_CHECKPOINTS = {
    "1h": 3600,
    "24h": 24 * 3600,
    "48h": 48 * 3600,
    "7d": 7 * 24 * 3600,
    "28d": 28 * 24 * 3600,
}
ACCOUNT_CHECKPOINT = "daily"
ADHOC_CHECKPOINT = "adhoc"
ALL_CHECKPOINTS = tuple(POST_CHECKPOINTS) + (ACCOUNT_CHECKPOINT, ADHOC_CHECKPOINT)

GRACE_FRACTION = 0.10
GRACE_MIN_SECONDS = 15 * 60

PENDING, DUE, MISSED, UNRECOVERABLE, DONE = "pending", "due", "missed", "unrecoverable", "done"


def grace_seconds(checkpoint):
    return max(GRACE_MIN_SECONDS, int(POST_CHECKPOINTS[checkpoint] * GRACE_FRACTION))


@dataclass(frozen=True)
class CheckpointStatus:
    checkpoint: str
    due_at: datetime
    state: str
    lateness_seconds: int  # how far past due_at `now` is (0 if not yet due)

    @property
    def should_collect(self):
        return self.state in (DUE, MISSED)


def evaluate_checkpoints(published_at, now, collected=()):
    """Status of every post checkpoint for a publication published at `published_at`."""
    published_at, now = to_utc(published_at), to_utc(now)
    collected = set(collected)
    labels = list(POST_CHECKPOINTS)
    result = []
    for i, label in enumerate(labels):
        due_at = published_at + timedelta(seconds=POST_CHECKPOINTS[label])
        lateness = max(0, int((now - due_at).total_seconds()))
        if label in collected:
            state = DONE
        elif now < due_at:
            state = PENDING
        elif lateness <= grace_seconds(label):
            state = DUE
        else:
            next_label = labels[i + 1] if i + 1 < len(labels) else None
            next_due = next_label and published_at + timedelta(seconds=POST_CHECKPOINTS[next_label])
            state = UNRECOVERABLE if next_due and now >= next_due else MISSED
        result.append(CheckpointStatus(label, due_at, state, lateness))
    return result


def due_checkpoints(publication, now, collected=()):
    """Checkpoints to act on now for `publication` (anything with .published_at):
    due, missed (collect as delayed) and unrecoverable (record as unavailable)."""
    return [s for s in evaluate_checkpoints(publication.published_at, now, collected)
            if s.state in (DUE, MISSED, UNRECOVERABLE)]


def period_key_for(checkpoint, collected_at):
    if checkpoint in POST_CHECKPOINTS:
        return checkpoint
    collected_at = to_utc(collected_at)
    if checkpoint == ACCOUNT_CHECKPOINT:
        return collected_at.date().isoformat()
    if checkpoint == ADHOC_CHECKPOINT:
        return collected_at.isoformat()
    raise ValueError(f"unknown checkpoint {checkpoint!r}")
