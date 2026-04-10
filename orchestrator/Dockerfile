FROM python:3.14.3-slim-bookworm

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
            bash \
            dumb-init \
            netcat-openbsd \
            supervisor; \
    rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.10.11 /uv /uvx /bin/

SHELL ["/bin/bash", "-c"]

ENV PATH="/home/orchestrator/.local/bin:$PATH" \
    PYTHONUNBUFFERED=1

RUN set -eux; \
    groupadd -g 1000 orchestrator; \
    useradd -u 1000 \
            -g orchestrator \
            -s /bin/bash \
            -m orchestrator

WORKDIR /opt/orchestrator

COPY .python-version pyproject.toml uv.lock ./
COPY api api/
COPY backend-utils backend-utils/
COPY common-utils common-utils/
COPY image-builder image-builder/
COPY orchestrator orchestrator/
COPY scripts/kill_supervisor.py /usr/local/bin/kill-supervisor

RUN set -eux; \
    chown -R orchestrator:orchestrator /opt/orchestrator; \
    chown orchestrator:orchestrator /usr/local/bin/kill-supervisor

USER orchestrator

RUN set -eux; \
    uv python install; \
    uv sync --project orchestrator \
            --frozen \
            --no-cache \
            --no-dev

ENTRYPOINT ["dumb-init", "--"]
CMD ["supervisord", "-c", "orchestrator/supervisord.conf"]

EXPOSE 8000

HEALTHCHECK \
    --start-period=10s \
    --interval=60s \
    --timeout=30s \
    --retries=5 \
CMD nc -z 127.0.0.1 8000 || exit 1
