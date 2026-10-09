import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from search_engine.cli import _main
from search_engine.engine.canonicalization import canonicalize_url
from search_engine.engine.identity import UrlId
from search_engine.engine.index import SearchDocument
from search_engine.engine.publisher import publish_lexical_generation

ROOT = Path(__file__).resolve().parents[2]


def test_docker_entrypoint_is_serve_only_and_untrusted_mode_is_not_default():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = next(line for line in dockerfile.splitlines() if line.startswith("ENTRYPOINT "))
    arguments = json.loads(entrypoint.removeprefix("ENTRYPOINT "))
    assert arguments[:3] == ["open-search", "serve", "/indexes"]
    assert "--allow-unsigned-local" not in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "COPY config/ /app/config/" in dockerfile
    assert 'CMD ["--profile", "laptop"]' in dockerfile
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "**\n" in ignored and "!src/**" in ignored and "**/*.pem" in ignored


def test_serving_command_searches_native_volume_without_modifying_artifacts(tmp_path, monkeypatch):
    pytest.importorskip("tantivy")
    root = tmp_path / "mounted-indexes"
    url = canonicalize_url("https://example.com/python")
    document = SearchDocument(UrlId.from_canonical_url(url), url.value, "Python", "search engine")
    publish_lexical_generation(root, "first", {"s1": (document,)}, backend="tantivy")
    before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    monkeypatch.setattr("sys.argv", ["open-search", "serve", str(root), "--host", "0.0.0.0",
                                    "--port", "8080", "--config-root", str(ROOT / "config"),
                                    "--profile", "laptop", "--allow-unsigned-local"])

    def run(app, **arguments):
        assert arguments == {"host": "0.0.0.0", "port": 8080}
        with TestClient(app) as client:
            assert client.get("/health").json() == {"status": "ok"}
            response = client.post("/search", json={"query": "python"})
            assert response.status_code == 200
            assert response.json()["results"][0]["title"] == "Python"
            assert client.get("/version").json()["generation"] == "first"

    monkeypatch.setattr("uvicorn.run", run)
    _main()
    after = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert after == before
