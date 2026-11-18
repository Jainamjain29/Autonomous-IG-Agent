"""Model pricing lookup and cost calculation."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Any
import yaml

_PRICES_CACHE: Dict[str, Dict[str, float]] | None = None


def load_model_prices(config_path: str | Path = "config/model_prices.yaml") -> Dict[str, Dict[str, float]]:
    """Load model pricing from YAML file."""
    global _PRICES_CACHE
    if _PRICES_CACHE is not None:
        return _PRICES_CACHE

    path = Path(config_path)
    if not path.exists():
        # Fallback defaults if file is not found
        _PRICES_CACHE = {
            "default": {"input_per_m": 0.15, "output_per_m": 0.60}
        }
        return _PRICES_CACHE

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    prices: Dict[str, Dict[str, float]] = {}
    for model, p in data.items():
        if isinstance(p, dict):
            prices[model.lower()] = {
                "input_per_m": float(p.get("input_per_m", 0.15)),
                "output_per_m": float(p.get("output_per_m", 0.60)),
            }
    _PRICES_CACHE = prices
    return _PRICES_CACHE


def calculate_cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    """Calculate the total cost in USD for the given input and output tokens."""
    prices = load_model_prices()
    model_key = model.lower()
    pricing = prices.get(model_key)
    if pricing is None:
        # Check partial match or fallback
        for k in prices:
            if k in model_key or model_key in k:
                pricing = prices[k]
                break
    if pricing is None:
        pricing = prices.get("default", {"input_per_m": 0.15, "output_per_m": 0.60})

    cost_in = (tokens_in * pricing["input_per_m"]) / 1_000_000.0
    cost_out = (tokens_out * pricing["output_per_m"]) / 1_000_000.0
    return round(cost_in + cost_out, 8)
