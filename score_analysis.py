"""Shared score-bucket analysis for /analysis and Sunday auto-tune.

Keeps dead-zone detection and min/max recommendations consistent across
webhook_server.score_analysis and weight_refitter.auto_tune_config.
"""

from __future__ import annotations

from typing import Any, Optional

# Labeled bucket floors. Keys use ASCII hyphen for storage/API stability.
BUCKET_BOUNDS: dict[str, float] = {
    "1.0-1.5": 1.0,
    "1.5-2.0": 1.5,
    "2.0-2.5": 2.0,
    "2.5-3.0": 2.5,
    "3.0+": 3.0,
}
BUCKET_ORDER: list[str] = list(BUCKET_BOUNDS.keys())

# Display labels (en-dash) for Discord/API readability — map both ways.
_DISPLAY = {k: k.replace("-", "–") for k in BUCKET_BOUNDS}
_DISPLAY["3.0+"] = "3.0+"
_FROM_DISPLAY = {v: k for k, v in _DISPLAY.items()}


def normalize_bucket_key(label: str) -> str:
    """Map en-dash or hyphen labels to the canonical hyphen key."""
    if label in BUCKET_BOUNDS:
        return label
    return _FROM_DISPLAY.get(label, label.replace("–", "-"))


def display_bucket(key: str) -> str:
    key = normalize_bucket_key(key)
    return _DISPLAY.get(key, key)


def bucket_for_score(score: float) -> str:
    """Assign a composite score to a labeled bucket.

    Scores below 1.0 are kept in their own bucket so they do not inflate
    the 1.0–1.5 hit-rate / avg stats.
    """
    if score < 1.0:
        return "<1.0"
    if score < 1.5:
        return "1.0-1.5"
    if score < 2.0:
        return "1.5-2.0"
    if score < 2.5:
        return "2.0-2.5"
    if score < 3.0:
        return "2.5-3.0"
    return "3.0+"


def sql_bucket_case(column: str = "composite_score") -> str:
    """SQL CASE expression matching bucket_for_score()."""
    return f"""
        CASE
            WHEN {column} < 1.0 THEN '<1.0'
            WHEN {column} < 1.5 THEN '1.0-1.5'
            WHEN {column} < 2.0 THEN '1.5-2.0'
            WHEN {column} < 2.5 THEN '2.0-2.5'
            WHEN {column} < 3.0 THEN '2.5-3.0'
            ELSE '3.0+'
        END
    """.strip()


def is_dead_zone(b: Optional[dict[str, Any]], *, min_n: int = 10) -> bool:
    """Weak band: low hit rate AND weak expectancy.

    Hit rate alone is a poor dead-zone signal for right-skewed crypto returns
    (a 33% hit rate with +7% avg 3d is still a good long band).
    """
    if not b or b.get("n", 0) < min_n:
        return False
    hit = b.get("hit_rate_3d", b.get("hit_rate", 0)) or 0
    avg = b.get("avg_3d", 0) or 0
    return hit < 40 and avg < 2.0


def is_good_bucket(
    b: Optional[dict[str, Any]],
    *,
    min_n: int = 5,
    min_avg_3d: float = 1.0,
) -> bool:
    """Profitable contiguous band for min_score recommendation.

    Uses expectancy (avg_3d) as the primary gate. Dead zones are excluded
    even if avg is slightly positive from a few outliers.
    """
    if not b or b.get("n", 0) < min_n:
        return False
    if is_dead_zone(b, min_n=min_n):
        return False
    return (b.get("avg_3d") or 0) > min_avg_3d


def recommend_score_bounds(
    buckets: list[dict[str, Any]],
    current_min: float,
    current_max: Optional[float],
    *,
    min_n_good: int = 5,
    min_avg_good: float = 1.0,
) -> tuple[float, Optional[float], list[str]]:
    """Return (suggested_min, suggested_max, dead_zone_notes).

    Walks DOWN from the top labeled bucket, extending the good range while
    buckets stay profitable. Stops at the first dead/non-good zone.
    """
    # Only labeled trading bands participate in min/max recommendation.
    bmap = {normalize_bucket_key(b["bucket"]): b for b in buckets}
    notes: list[str] = []

    for bk in BUCKET_ORDER:
        b = bmap.get(bk)
        if is_dead_zone(b):
            hit = b.get("hit_rate_3d", b.get("hit_rate", 0)) or 0
            notes.append(
                f"⚠ {display_bucket(bk)} is a dead zone — only {hit:.0f}% "
                f"hit rate and weak avg 3d on {b['n']} picks. Avoid."
            )

    suggested_max = current_max
    for bk in reversed(BUCKET_ORDER):
        b = bmap.get(bk)
        if b and b["n"] >= 3 and (b.get("avg_3d") or 0) < 0:
            suggested_max = BUCKET_BOUNDS[bk]
        else:
            break

    suggested_min = current_min
    contiguous: list[str] = []
    for bk in reversed(BUCKET_ORDER):
        if is_good_bucket(bmap.get(bk), min_n=min_n_good, min_avg_3d=min_avg_good):
            contiguous.append(bk)
        else:
            break
    if contiguous:
        suggested_min = BUCKET_BOUNDS[contiguous[-1]]

    return suggested_min, suggested_max, notes


def format_recommendation(
    buckets: list[dict[str, Any]],
    current_min: float,
    current_max: Optional[float],
    suggested_min: float,
    suggested_max: Optional[float],
    notes: list[str],
) -> str:
    """Human-readable recommendation string for /analysis / Discord."""
    total_n = sum(b.get("n", 0) for b in buckets)
    if not buckets or total_n < 20:
        return "Not enough data yet — check back after 30+ days of picks."

    changes: list[str] = []
    if suggested_min != current_min:
        direction = "Raise" if suggested_min > current_min else "Lower"
        changes.append(f"{direction} min_score {current_min} → {suggested_min}")
    if suggested_max != current_max:
        if suggested_max:
            changes.append(
                f"Set max_score cap at {suggested_max} "
                f"(scores above this average negative returns)"
            )
        else:
            changes.append("Remove max_score cap — high-score picks performing well")

    if changes:
        msg = " | ".join(changes) + "."
        if notes:
            msg += " Also: " + "; ".join(notes)
        return msg
    if notes:
        return " ".join(notes)
    return (
        f"Config looks well-calibrated — min_score={current_min}"
        + (f", max_score={current_max}" if current_max else "")
        + "."
    )
