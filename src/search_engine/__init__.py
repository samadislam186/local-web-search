"""OpenSearch Local: CPU-first, local web search infrastructure."""

import importlib
import sys

from .engine.identity import UrlId

# Temporary compatibility aliases while callers migrate to search_engine.engine.
_ENGINE_MODULES = (
    "authority", "cache", "cache_benchmark", "canonicalization", "contracts", "crawl_processing", "crawl_store", "crawler_runner", "crawler_worker", "documents", "errors", "evaluation", "evaluation_corpus", "feedback", "fetch", "filesystem", "fingerprints",
    "graph", "identity", "index", "index_builder", "liveness", "liveness_runner", "liveness_worker", "manifest", "pagerank", "parser",
    "publisher", "query_analysis", "ranking", "robots", "robots_manager", "routing", "routing_experiment", "routing_store", "security", "service", "sitemap", "state", "storage", "tantivy_adapter", "test_support", "typed_payloads",
)
for _name in _ENGINE_MODULES:
    sys.modules.setdefault(f"{__name__}.{_name}", importlib.import_module(f"{__name__}.engine.{_name}"))
sys.modules.setdefault(f"{__name__}.agent", importlib.import_module(f"{__name__}.api.agent"))
sys.modules.setdefault(f"{__name__}.common_crawl", importlib.import_module(f"{__name__}.engine.common_crawl"))

__all__ = ["UrlId"]
