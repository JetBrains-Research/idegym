FROM python:3.15.0b4-slim-bookworm

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

ENV PATH="/home/watcher/.local/bin:$PATH" \
    PYTHONUNBUFFERED=1

RUN set -eux; \
    groupadd -g 1000 watcher; \
    useradd -u 1000 \
            -g watcher \
            -s /bin/bash \
            -m watcher

WORKDIR /opt/watcher

COPY .python-version pyproject.toml uv.lock ./
COPY api api/
COPY backend-utils backend-utils/
COPY common-utils common-utils/
COPY image-builder image-builder/
COPY orchestrator orchestrator/
COPY plugins plugins/
COPY rewards rewards/
COPY tools tools/
COPY watcher watcher/
COPY scripts/kill_supervisor.py /usr/local/bin/kill-supervisor

RUN set -eux; \
    chown -R watcher:watcher /opt/watcher; \
    chown watcher:watcher /usr/local/bin/kill-supervisor

USER watcher

RUN set -eux; \
    uv python install; \
    uv sync --project watcher \
            --frozen \
            --no-cache \
            --no-dev

ENTRYPOINT ["dumb-init", "--"]
CMD ["supervisord", "-c", "watcher/supervisord.conf"]

EXPOSE 8000

HEALTHCHECK \
    --start-period=10s \
    --interval=60s \
    --timeout=30s \
    --retries=5 \
CMD nc -z 127.0.0.1 8000 || exit 1
