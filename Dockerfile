# Build the application wheel, not a search index. Never copy corpus data or keys.
ARG PYTHON_IMAGE=python:3.13-slim
ARG TANTIVY_VERSION=0.26.2
FROM ${PYTHON_IMAGE} AS wheels
ARG TANTIVY_VERSION
WORKDIR /build
COPY pyproject.toml ./
COPY src/ ./src/
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels \
    ".[tantivy]" "tantivy==${TANTIVY_VERSION}"

FROM ${PYTHON_IMAGE} AS runtime
ARG TANTIVY_VERSION
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TMPDIR=/tmp
WORKDIR /app
COPY --from=wheels /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels \
    opensearch-local "tantivy==${TANTIVY_VERSION}" \
    && rm -r /wheels \
    && groupadd --gid 10001 search \
    && useradd --uid 10001 --gid search --create-home --shell /usr/sbin/nologin search \
    && mkdir /indexes
COPY config/ /app/config/
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import json, urllib.request; r = urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2); assert json.load(r)['status'] == 'ok'"]
# Arguments after the image name are serving options, never crawl/build commands.
ENTRYPOINT ["open-search", "serve", "/indexes", "--host", "0.0.0.0", "--port", "8080", "--config-root", "/app/config"]
CMD ["--profile", "laptop"]
