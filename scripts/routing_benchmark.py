"""Evaluate query-class routing on published indexes, or a labelled synthetic challenge."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from search_engine.engine.canonicalization import canonicalize_url
from search_engine.engine.identity import UrlId
from search_engine.engine.index import SearchDocument
from search_engine.engine.publisher import publish_lexical_generation
from search_engine.engine.routing_evaluation import (
    RoutingQueryCase,
    evaluate_published_routing,
    load_query_cases,
)
from search_engine.engine.security import PinnedKeyVerifier


def build_fixture(directory: Path, backend: str = "tantivy") -> tuple[Path, tuple[RoutingQueryCase, ...]]:
    directory.mkdir(parents=True, exist_ok=False)
    now = datetime.now(UTC)

    def document(host, path, title, body, *, common=True, age=0):
        url = canonicalize_url(f"https://{host}/{path}")
        return SearchDocument(UrlId.from_canonical_url(url), url.value, title,
                              body + (" search tutorial guide" if common else ""),
                              crawled_at=(now - timedelta(days=age)).isoformat())

    python = document("python.example", "async", "Python async crawler", "Concurrency networking")
    rare = document("rare.example", "calibration", "Quasarx", "Hypergraph calibration", common=False)
    phrase = document("energy.example", "battery", "Renewable energy storage", "Battery technology")
    nav = document("docs.example", "start", "Documentation portal", "Official documentation")
    code = document("code.example", "snippet", "C++ systems guide", "C language foo_bar function")
    accented = document("cafe.example", "coffee", "Café guide", "Coffee recipes")
    chinese = document("cn.example", "ml", "机器学习", "中文 技术")
    hindi = document("hi.example", "computer", "कंप्यूटर", "तकनीक")
    japanese = document("例え.テスト", "guide", "日本語 検索", "資料")
    science = document("science.example", "water", "H2O chemistry", "Water molecules")
    literal = document("literal.example", "syntax", "Literal syntax", "Text site:literal instruction")
    broad = document("search.example", "engine", "Search platform", "Retrieval systems")
    widget = document("widget.example", "new", "Widget release", "Current widget software")
    howto = document("howto.example", "build", "Build a crawler guide", "How to build a crawler")
    segments = {
        "z-python": (python,), "z-rare": (rare, *(document("rare.example", f"background/{number}", "Background", "Unrelated content", common=False) for number in range(100))),
        "z-phrase": (phrase,), "a-phrase": (document("wrong-energy.example", "battery", "Renewable storage energy", "Reordered phrase"),),
        "z-nav": (nav,), "a-nav": (document("mirror.example", "link", "Mirror", "Reference https://docs.example/start"),),
        "z-code": (code,), "z-accent": (accented,), "z-chinese": (chinese,), "z-hindi": (hindi,),
        "z-japanese": (japanese,), "z-science": (science,), "z-literal": (literal,), "z-search": (broad,),
        "z-widget": (widget,), "a-widget": (document("old-widget.example", "old", "Widget release", "Old widget software", age=60),),
        "a-ruby": (document("ruby.example", "language", "Language", "Ruby programming"),),
        "a-body": (document("body.example", "text", "Unrelated title", "async crawler"),), "z-howto": (howto,),
    }
    for number in range(12):
        segments[f"b-distractor-{number:02d}"] = (document(f"distractor{number}.example", "page", "Background article", "General content"),)
    cases = (
        RoutingQueryCase("rare", "rare_entity", "quasarx search tutorial guide", expected_url_ids=(rare.url_id.hex,)),
        RoutingQueryCase("multi", "multiword", "async crawler", expected_url_ids=(python.url_id.hex,)),
        RoutingQueryCase("phrase", "phrase", '"renewable energy storage"', expected_url_ids=(phrase.url_id.hex,)),
        RoutingQueryCase("url", "navigation", nav.url, expected_url_ids=(nav.url_id.hex,)),
        RoutingQueryCase("hostname", "navigation", "docs.example", expected_url_ids=(nav.url_id.hex,)),
        RoutingQueryCase("site", "domain_filter", "site:python.example search", expected_url_ids=(python.url_id.hex,)),
        RoutingQueryCase("api-domain", "domain_filter", "search", domain_filter="PYTHON.EXAMPLE.", expected_url_ids=(python.url_id.hex,)),
        RoutingQueryCase("site-only", "domain_filter", "site:python.example", expected_url_ids=(python.url_id.hex,)),
        RoutingQueryCase("idn", "domain_filter", "site:例え.テスト 検索", expected_url_ids=(japanese.url_id.hex,)),
        RoutingQueryCase("fresh", "freshness", "widget freshness:day", expected_url_ids=(widget.url_id.hex,)),
        RoutingQueryCase("fuzzy", "explicit_typo", "pyhton~", expected_url_ids=(python.url_id.hex,)),
        RoutingQueryCase("single", "code_identifier", "C++", expected_url_ids=(code.url_id.hex,)),
        RoutingQueryCase("underscore", "code_identifier", "foo_bar", expected_url_ids=(code.url_id.hex,)),
        RoutingQueryCase("science", "code_identifier", "H2O", expected_url_ids=(science.url_id.hex,)),
        RoutingQueryCase("accent", "multilingual", "café", expected_url_ids=(accented.url_id.hex,)),
        RoutingQueryCase("chinese", "multilingual", "机器学习", expected_url_ids=(chinese.url_id.hex,)),
        RoutingQueryCase("hindi", "multilingual", "कंप्यूटर", expected_url_ids=(hindi.url_id.hex,)),
        RoutingQueryCase("literal", "literal_phrase", '"site:literal"', expected_url_ids=(literal.url_id.hex,)),
        RoutingQueryCase("field", "field_query", "title:python", expected_url_ids=(python.url_id.hex,)),
        RoutingQueryCase("boolean", "boolean", "python OR ruby", expected_url_ids=(python.url_id.hex,)),
        RoutingQueryCase("negative", "boolean", "python NOT ruby", expected_url_ids=(python.url_id.hex,)),
        RoutingQueryCase("longtail", "natural_language", "how to build a crawler", expected_url_ids=(howto.url_id.hex,)),
        RoutingQueryCase("broad", "broad", "search", expected_url_ids=(broad.url_id.hex,)),
        RoutingQueryCase("missing", "no_results", "zxqnothere", allow_empty_oracle=True),
    )
    root = directory / "artifacts"
    manifest = publish_lexical_generation(root, "challenge", segments, backend=backend)
    (directory / "queries.jsonl").write_text("".join(json.dumps(asdict(case), ensure_ascii=False) + "\n" for case in cases), encoding="utf-8")
    (directory / "corpus.json").write_text(json.dumps({"kind": "synthetic_challenge", "backend": backend,
                                                       "documents": sum(len(items) for items in segments.values()),
                                                       "shards": len(segments), "generation": manifest.generation,
                                                       "rule": "hand-authored adversarial query classes plus deterministic distractors"}, indent=2), encoding="utf-8")
    return root, cases


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--synthetic", type=Path, help="new directory for a labelled synthetic challenge")
    group.add_argument("--root", type=Path, help="existing published artifact root")
    parser.add_argument("--queries", type=Path, help="JSONL query cases for supplied artifacts")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--backend", choices=("reference", "tantivy"), default="tantivy")
    parser.add_argument("--budgets", type=int, nargs="+", default=[1, 2, 5, 10])
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--byte-budget", type=int, default=1 << 30)
    parser.add_argument("--minimum-recall", type=float, default=0.95)
    parser.add_argument("--allow-unsigned-local", action="store_true")
    parser.add_argument("--pinned-key", action="append", default=[], help="KEY_ID=BASE64_PUBLIC_KEY")
    args = parser.parse_args()
    if args.report.exists():
        parser.error("report already exists; use a new output path")
    if args.root and not args.queries:
        parser.error("supplied artifacts require --queries")
    if args.synthetic:
        root, cases = build_fixture(args.synthetic, args.backend)
        provenance = json.loads((args.synthetic / "corpus.json").read_text(encoding="utf-8"))
    else:
        root, cases = args.root, load_query_cases(args.queries)
        provenance = {"kind": "supplied_artifacts", "note": "representativeness/provenance requires separate review"}
    keys = dict(item.split("=", 1) for item in args.pinned_key)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    report = evaluate_published_routing(root, cases, shard_budgets=tuple(args.budgets), top_k=args.top_k,
                                       byte_budget=args.byte_budget, minimum_recall=args.minimum_recall,
                                       require_signatures=not (args.synthetic or args.allow_unsigned_local),
                                       verifier=PinnedKeyVerifier(keys) if keys else None, scratch_directory=args.report.parent)
    report["corpus"] = provenance
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"decision_by_budget": report["decision_by_budget"], "summaries": report["summaries"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
