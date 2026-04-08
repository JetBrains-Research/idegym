USER root
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
    python3=3.11.2* \
    python-is-python3=3.11.2*; \
    rm -rf /var/lib/apt/lists/*
USER appuser
