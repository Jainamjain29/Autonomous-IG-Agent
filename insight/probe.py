"""Live Instagram API probe: validate metric dictionary names against the real API.

CLI:  python -m insight.probe

READ-ONLY against Instagram.  Does NOT write to data/insight.db.
Saves raw API responses (tokens stripped via insight.privacy.redact_secrets)
to data/probe/<timestamp>/*.json.

The probe calls the Instagram Graph API with every metric name from the
Step 1 metric dictionary seed and reports which names are valid, invalid,
or unavailable.  This validates documentation-derived names before building
the real collector in Step 2b.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

import database as db
from .dictionary import load_seed
from .http_client import CallCapReached, GraphClient
from .privacy import redact_secrets

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CALL_CAP = 40


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ProbeError(Exception):
    """Fatal configuration error (missing credentials, etc.)."""


# ---------------------------------------------------------------------------
# Finding
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    canonical_name: str
    platform_name: str
    applies_to: str
    status: str          # OK | INVALID | NO_DATA | NOT_TRIED
    sample_value: object = None
    error: str = ""


# ---------------------------------------------------------------------------
# ProbeRunner
# ---------------------------------------------------------------------------

class ProbeRunner:

    def __init__(self):
        host = db.get_setting("GRAPH_HOST") or "graph.instagram.com"
        version = db.get_setting("GRAPH_VERSION") or "v26.0"
        token = db.get_setting("META_ACCESS_TOKEN")
        self.ig_user_id = db.get_setting("IG_USER_ID")
        if not token or not self.ig_user_id:
            raise ProbeError(
                "Missing META_ACCESS_TOKEN or IG_USER_ID.  "
                "Set them in .env or the settings dashboard."
            )
        self.client = GraphClient(f"https://{host}/{version}", token, call_cap=CALL_CAP)
        self._init_state()

    def _init_state(self):
        """Load the metric dictionary and partition Instagram mappings."""
        seed = load_seed()
        self.ig_mappings = [
            r for r in seed
            if r.platform == "instagram" and r.status == "active"
            and r.platform_metric_name
        ]
        self.reel_mappings = [m for m in self.ig_mappings if m.applies_to == "reel"]
        self.account_mappings = [m for m in self.ig_mappings if m.applies_to == "account"]
        self.findings: list[Finding] = []
        self.undiscovered: dict[str, object] = {}
        self.reel_count: int = 0

    # ------------------------------------------------------------------ run

    def run(self):
        print("[probe] Starting Instagram API probe...")
        account_data = self._probe_account()
        reels = self._probe_media_list()
        self._probe_reel_insights(reels)
        self._probe_account_insights(account_data)
        self._save_responses()
        self._print_report()

    # ------------------------------------------ step 1: account info

    def _probe_account(self) -> dict | None:
        try:
            status, data = self.client.get(
                self.ig_user_id,
                params={"fields": "id,username,media_count,followers_count"},
                label="account_info",
            )
        except CallCapReached:
            print("[probe] Call cap reached before account info")
            return None
        if status >= 400:
            print(f"[probe] Account info failed: HTTP {status}")
            return None
        print(
            f"[probe] Account: @{data.get('username', '?')}  "
            f"followers={data.get('followers_count', '?')}  "
            f"posts={data.get('media_count', '?')}"
        )
        return data

    # ------------------------------------------ step 2: media list

    def _probe_media_list(self) -> list[dict]:
        try:
            status, data = self.client.get(
                f"{self.ig_user_id}/media",
                params={
                    "fields": "id,media_type,media_product_type,"
                              "timestamp,permalink,caption",
                },
                label="media_list",
            )
        except CallCapReached:
            return []
        if status >= 400:
            print(f"[probe] Media list failed: HTTP {status}")
            return []
        items = data.get("data", [])
        reels = [m for m in items if m.get("media_product_type") == "REELS"]
        print(f"[probe] Media page: {len(items)} items, {len(reels)} Reel(s)")
        return reels[:2]

    # ------------------------------------------ step 3: reel insights

    def _probe_reel_insights(self, reels: list[dict]):
        if not reels:
            print("[probe] No Reels found on first page; skipping reel insights")
            for m in self.reel_mappings:
                self.findings.append(Finding(
                    m.canonical_name, m.platform_metric_name, "reel",
                    "NOT_TRIED", error="no reels on account"))
            return

        self.reel_count = len(reels)
        media_id = reels[0]["id"]
        metric_names = [m.platform_metric_name for m in self.reel_mappings]

        # ---- batch attempt
        try:
            status, data = self.client.get(
                f"{media_id}/insights",
                params={"metric": ",".join(metric_names)},
                label=f"reel_{media_id}_batch",
            )
        except CallCapReached:
            for m in self.reel_mappings:
                self.findings.append(Finding(
                    m.canonical_name, m.platform_metric_name, "reel",
                    "NOT_TRIED"))
            return

        if status < 400:
            self._parse_insights(data, self.reel_mappings, "reel")
            return

        # ---- batch failed -> one-by-one fallback
        print(f"[probe] Reel batch failed ({_extract_error(data)}); "
              f"trying one-by-one...")
        for m in self.reel_mappings:
            try:
                s, d = self.client.get(
                    f"{media_id}/insights",
                    params={"metric": m.platform_metric_name},
                    label=f"reel_{media_id}_{m.platform_metric_name}",
                )
            except CallCapReached:
                self.findings.append(Finding(
                    m.canonical_name, m.platform_metric_name, "reel",
                    "NOT_TRIED"))
                continue
            if s >= 400:
                self.findings.append(Finding(
                    m.canonical_name, m.platform_metric_name, "reel",
                    "INVALID", error=_extract_error(d)))
            else:
                self._parse_single(d, m, "reel")

    # ------------------------------------------ step 4: account insights

    def _probe_account_insights(self, account_data: dict | None):
        # followers_count is a User field, not an insights metric
        followers_maps = [m for m in self.account_mappings
                          if m.platform_metric_name == "followers_count"]
        insight_maps = [m for m in self.account_mappings
                        if m.platform_metric_name != "followers_count"]

        for m in followers_maps:
            val = account_data.get("followers_count") if account_data else None
            if val is not None:
                self.findings.append(Finding(
                    m.canonical_name, m.platform_metric_name, "account",
                    "OK", sample_value=val))
            else:
                self.findings.append(Finding(
                    m.canonical_name, m.platform_metric_name, "account",
                    "NO_DATA"))

        if not insight_maps:
            return

        names = [m.platform_metric_name for m in insight_maps]

        # ---- batch
        try:
            status, data = self.client.get(
                f"{self.ig_user_id}/insights",
                params={"metric": ",".join(names),
                        "period": "day", "metric_type": "total_value"},
                label="account_insights_batch",
            )
        except CallCapReached:
            for m in insight_maps:
                self.findings.append(Finding(
                    m.canonical_name, m.platform_metric_name, "account",
                    "NOT_TRIED"))
            return

        if status < 400:
            self._parse_insights(data, insight_maps, "account")
            return

        # ---- fallback
        print(f"[probe] Account batch failed ({_extract_error(data)}); "
              f"trying one-by-one...")
        for m in insight_maps:
            try:
                s, d = self.client.get(
                    f"{self.ig_user_id}/insights",
                    params={"metric": m.platform_metric_name,
                            "period": "day", "metric_type": "total_value"},
                    label=f"account_{m.platform_metric_name}",
                )
            except CallCapReached:
                self.findings.append(Finding(
                    m.canonical_name, m.platform_metric_name, "account",
                    "NOT_TRIED"))
                continue
            if s >= 400:
                self.findings.append(Finding(
                    m.canonical_name, m.platform_metric_name, "account",
                    "INVALID", error=_extract_error(d)))
            else:
                self._parse_single(d, m, "account")

    # ------------------------------------------ parsers

    def _parse_insights(self, data, mappings, applies_to):
        """Parse a multi-metric ``/{id}/insights`` response."""
        returned: dict[str, object] = {}
        for item in data.get("data", []):
            returned[item.get("name")] = _extract_value(item)

        known = {m.platform_metric_name for m in mappings}

        for m in mappings:
            pn = m.platform_metric_name
            if pn in returned:
                v = returned[pn]
                if v is not None:
                    self.findings.append(Finding(
                        m.canonical_name, pn, applies_to, "OK",
                        sample_value=v))
                else:
                    self.findings.append(Finding(
                        m.canonical_name, pn, applies_to, "NO_DATA"))
            else:
                self.findings.append(Finding(
                    m.canonical_name, pn, applies_to, "NO_DATA",
                    error="metric accepted but not in response"))

        for name, v in returned.items():
            if name not in known:
                self.undiscovered[name] = v

    def _parse_single(self, data, mapping, applies_to):
        """Parse a single-metric ``/{id}/insights`` response."""
        items = data.get("data", [])
        if items:
            v = _extract_value(items[0])
            if v is not None:
                self.findings.append(Finding(
                    mapping.canonical_name, mapping.platform_metric_name,
                    applies_to, "OK", sample_value=v))
            else:
                self.findings.append(Finding(
                    mapping.canonical_name, mapping.platform_metric_name,
                    applies_to, "NO_DATA"))
        else:
            self.findings.append(Finding(
                mapping.canonical_name, mapping.platform_metric_name,
                applies_to, "NO_DATA", error="empty data array"))

    # ------------------------------------------ save / report

    def _save_responses(self, base_dir=None):
        """Write every raw response to *base_dir*/<ts>/*.json, tokens stripped.

        Token stripping reuses ``insight.privacy.redact_secrets``, which
        removes dict keys like ``access_token`` and masks
        ``access_token=...`` inside URL strings (e.g. paging.next) at any
        nesting depth.
        """
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if base_dir is None:
            base_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "data", "probe",
            )
        probe_dir = os.path.join(base_dir, ts)
        os.makedirs(probe_dir, exist_ok=True)
        for i, (label, record) in enumerate(self.client.raw_responses):
            safe = "".join(
                c if c.isalnum() or c in "-_." else "_" for c in label)
            path = os.path.join(probe_dir, f"{i:02d}_{safe}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(redact_secrets(record), f, indent=2, default=str)
        print(f"[probe] Saved {len(self.client.raw_responses)} response(s)"
              f" to {probe_dir}")
        return probe_dir

    def _print_report(self):
        print(f"\n[probe] API calls used: {self.client.call_count}/{CALL_CAP}")
        if self.reel_count < 2:
            print(f"[probe] Note: only {self.reel_count} Reel(s) found"
                  f" (probed all available)")
        hdr = (f"{'canonical_name':<28} {'platform_name':<36} "
               f"{'applies_to':<10} {'status':<10} "
               f"{'sample_value':<15} {'error'}")
        print(f"\n{hdr}")
        print("-" * len(hdr))
        for f in self.findings:
            sv = str(f.sample_value) if f.sample_value is not None else ""
            print(f"{f.canonical_name:<28} {f.platform_name:<36} "
                  f"{f.applies_to:<10} {f.status:<10} "
                  f"{sv:<15} {f.error}")
        if self.undiscovered:
            print("\n-- Metrics returned by the API but NOT in the "
                  "dictionary --")
            for name, v in sorted(self.undiscovered.items()):
                print(f"  {name} = {v}")


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _extract_value(item: dict):
    """Pull a scalar from an insights item (handles both response shapes).

    - lifetime / default:  ``{"values": [{"value": 123}]}``
    - total_value:         ``{"total_value": {"value": 123}}``
    """
    tv = item.get("total_value")
    if isinstance(tv, dict) and "value" in tv:
        return tv["value"]
    vals = item.get("values", [])
    if vals and isinstance(vals[0], dict):
        return vals[0].get("value")
    return None


def _extract_error(data) -> str:
    """Pull a human-readable message from an API error response."""
    if isinstance(data, dict):
        err = data.get("error", {})
        if isinstance(err, dict):
            return err.get("message", str(err))
        return str(err)
    return str(data)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    try:
        ProbeRunner().run()
    except ProbeError as exc:
        print(f"[probe] FATAL: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[probe] Interrupted.")


if __name__ == "__main__":
    main()
