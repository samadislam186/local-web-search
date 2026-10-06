import json
import logging
from io import StringIO

import pytest
from search_engine.test_support import service_for_tests

from search_engine.engine.observability import (
    JsonEventFormatter,
    MetricsRegistry,
    NoopMetricsRegistry,
    correlation_context,
    log_event,
)


def test_metrics_registry_and_search_service_report_execution() -> None:
    registry = MetricsRegistry()
    registry.increment("queries")
    registry.observe("latency", 2.0)
    registry.observe("latency", 4.0)
    snapshot = registry.snapshot()
    assert snapshot.counters["queries"] == 1
    assert snapshot.observations["latency"]["mean"] == 3.0
    service = service_for_tests()
    service.search("python")
    assert service.stats()["query_count"] == 1


def test_json_events_include_correlation_and_reject_sensitive_fields() -> None:
    stream = StringIO()
    logger = logging.getLogger("search-engine-test-events")
    logger.handlers = []
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonEventFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    with correlation_context("request-123"):
        log_event(logger, "query_completed", result_count=2)
    payload = json.loads(stream.getvalue())
    assert payload["correlation_id"] == "request-123"
    assert payload["fields"] == {"result_count": 2}
    with pytest.raises(ValueError, match="sensitive"):
        log_event(logger, "query_completed", url="https://example.com/")
    assert NoopMetricsRegistry().snapshot().counters == {}
