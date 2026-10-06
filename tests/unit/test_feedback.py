from pathlib import Path

import pytest

from search_engine.engine.feedback import (
    FeedbackEvent,
    FeedbackKind,
    LocalFeedbackAggregator,
    SqliteFeedbackAggregator,
    classify_query,
)


def test_feedback_uses_salted_query_classes_and_aggregate_thresholds() -> None:
    salt = b"local-only-salt"
    assert classify_query("  Python Crawler  ", local_salt=salt) == classify_query("python crawler", local_salt=salt)
    aggregator = LocalFeedbackAggregator()
    query_class = classify_query("python crawler", local_salt=salt)
    url_id = "a" * 32
    for _ in range(10):
        aggregator.record(FeedbackEvent(query_class, url_id, FeedbackKind.IMPRESSION))
    aggregator.record(FeedbackEvent(query_class, url_id, FeedbackKind.SELECT))
    assert aggregator.ranking_delta(query_class, url_id) == pytest.approx(0.01)
    assert aggregator.contribution(minimum_events=12) == {}
    assert aggregator.contribution(minimum_events=10)
    with pytest.raises(ValueError, match="UrlId"):
        aggregator.record(FeedbackEvent(query_class, "z" * 32, FeedbackKind.SELECT))


def test_sqlite_feedback_aggregates_survive_process_recreation(tmp_path: Path) -> None:
    salt = b"local-only-salt"
    query_class = classify_query("python crawler", local_salt=salt)
    url_id = "a" * 32
    database = tmp_path / "feedback.sqlite3"
    aggregator = SqliteFeedbackAggregator(database)
    for _ in range(10):
        aggregator.record(FeedbackEvent(query_class, url_id, FeedbackKind.IMPRESSION))
    aggregator.record(FeedbackEvent(query_class, url_id, FeedbackKind.SELECT))
    reloaded = SqliteFeedbackAggregator(database)
    assert reloaded.ranking_delta(query_class, url_id) == pytest.approx(0.01)
    assert reloaded.contribution(minimum_events=11)
