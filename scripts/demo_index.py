"""Build and query a small deterministic index through the real publication path."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from fastapi.testclient import TestClient

from search_engine.api import create_generation_app
from search_engine.engine.canonicalization import canonicalize_url
from search_engine.engine.crawl_processing import CrawlResultProcessor
from search_engine.engine.crawl_store import CrawledDocumentRepository
from search_engine.engine.index_builder import LocalIndexBuilder
from search_engine.engine.state import CandidateRepository, CrawlStateDatabase, UrlState


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="new directory for this synthetic demo")
    parser.add_argument("--backend", choices=("reference", "tantivy"), default="tantivy")
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=False)
    database = CrawlStateDatabase(args.directory / "crawl.sqlite3")
    database.initialize()
    candidates = CandidateRepository(database)
    documents = CrawledDocumentRepository(database)
    processor = CrawlResultProcessor(candidates)
    pages = (
        ("https://demo.example/python", "<title>Python crawler</title><p>Build a Python web crawler.</p><a href='/search'>Search index</a>"),
        ("https://demo.example/search", "<title>Search index</title><p>Python search uses lexical retrieval and PageRank.</p>"),
        ("https://other.example/gardening", "<title>Gardening</title><p>Grow flowers in a garden.</p>"),
    )
    for url, html in pages:
        url_id = candidates.upsert(canonicalize_url(url), "synthetic_demo")
        candidates.transition(url_id, UrlState.LIVENESS_PENDING)
        candidates.transition(url_id, UrlState.LIVE)
        documents.save(processor.process_html(url_id, html))
    root = args.directory / "artifacts"
    built = LocalIndexBuilder(documents, candidates).build(root, "demo-g1", max_documents=10, shard_count=2, backend=args.backend)
    app, _service = create_generation_app(root, require_signatures=False)
    with TestClient(app) as client:
        response = client.post("/search", json={"query": "python", "freshness": "day", "explain": True})
        response.raise_for_status()
        result = response.json()
        if not result["results"]:
            raise RuntimeError("demo index returned no Python results")
        print(json.dumps({"documents": built.document_count, "shards": built.shard_count, "search": result}, indent=2))


if __name__ == "__main__":
    main()
