"""Safe serialization for published offline authority/ranking signals."""

from __future__ import annotations

import json
import math

from .ranking import RankingSignals
from .storage import ArtifactValidationError


def authority_to_payload(scores: dict[str, float]) -> bytes:
    _validate_scores(scores)
    return json.dumps(scores, sort_keys=True, separators=(",", ":")).encode("utf-8")


def authority_from_payload(payload: bytes) -> dict[str, RankingSignals]:
    try:
        raw = json.loads(payload)
        scores = {str(url_id): float(score) for url_id, score in raw.items()}
        _validate_scores(scores)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ArtifactValidationError("invalid authority payload") from exc
    return {url_id: RankingSignals(authority=score) for url_id, score in scores.items()}


def _validate_scores(scores: dict[str, float]) -> None:
    for url_id, score in scores.items():
        if len(url_id) != 32 or not math.isfinite(score) or score < 0:
            raise ValueError("authority scores must use UrlId hex keys and finite non-negative values")
