"""Metric dictionary: load the versioned seed file, map platform names -> canonical.

Mapping never guesses. Expected-but-absent metrics are reported as missing;
platform names the dictionary does not know are reported as unmapped and left alone.
"""
import json
import os
from dataclasses import dataclass, field

from sqlalchemy import select

from .models import MetricDefinition

SEED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seeds")
DEFAULT_SEED = os.path.join(SEED_DIR, "metric_dictionary.v1.json")


@dataclass(frozen=True)
class MetricMapping:
    canonical_name: str
    platform: str
    applies_to: str
    platform_metric_name: str | None
    unit: str
    description: str
    notes: str | None
    scale: float
    status: str
    seed_version: int


@dataclass
class MappedMetrics:
    values: dict = field(default_factory=dict)    # canonical -> float
    missing: dict = field(default_factory=dict)   # canonical -> reason
    unmapped: dict = field(default_factory=dict)  # platform name -> raw value (kept, not interpreted)


def load_seed(path=DEFAULT_SEED):
    with open(path, encoding="utf-8") as f:
        seed = json.load(f)
    canon = seed["canonical_metrics"]
    rows = []
    for m in seed["mappings"]:
        c = canon[m["canonical_name"]]
        rows.append(MetricMapping(
            canonical_name=m["canonical_name"],
            platform=m["platform"],
            applies_to=m["applies_to"],
            platform_metric_name=m.get("platform_metric_name"),
            unit=c["unit"],
            description=c["description"],
            notes=m.get("notes"),
            scale=float(m.get("scale", 1)),
            status=m.get("status", "active"),
            seed_version=seed["version"],
        ))
    return rows


def seed_metric_definitions(session, path=DEFAULT_SEED):
    """Upsert the seed into metric_definitions. Safe to run repeatedly."""
    for row in load_seed(path):
        existing = session.scalar(select(MetricDefinition).filter_by(
            platform=row.platform, canonical_name=row.canonical_name, applies_to=row.applies_to))
        target = existing or MetricDefinition(
            platform=row.platform, canonical_name=row.canonical_name, applies_to=row.applies_to)
        for attr in ("platform_metric_name", "unit", "description", "notes", "scale", "status", "seed_version"):
            setattr(target, attr, getattr(row, attr))
        if existing is None:
            session.add(target)
    session.flush()


class MetricDictionary:
    """Active mappings for one platform."""

    def __init__(self, platform, rows=None):
        rows = load_seed() if rows is None else rows
        self.platform = platform
        self._rows = [r for r in rows if r.platform == platform and r.status == "active" and r.platform_metric_name]

    def canonical_metrics(self, applies_to):
        return frozenset(r.canonical_name for r in self._rows if r.applies_to == applies_to)

    def map(self, raw, applies_to):
        """raw: {platform_metric_name: value}. Returns MappedMetrics for `applies_to`."""
        rows = [r for r in self._rows if r.applies_to == applies_to]
        known = {r.platform_metric_name for r in rows}
        out = MappedMetrics()
        for r in rows:
            if r.platform_metric_name not in raw:
                out.missing[r.canonical_name] = "not in response"
                continue
            value = raw[r.platform_metric_name]
            if value is None:
                out.missing[r.canonical_name] = "null in response"
            elif isinstance(value, bool) or not isinstance(value, (int, float)):
                out.missing[r.canonical_name] = f"non-numeric value {value!r}"
            else:
                out.values[r.canonical_name] = float(value) * r.scale
        for name, value in raw.items():
            if name not in known:
                out.unmapped[name] = value
        return out
