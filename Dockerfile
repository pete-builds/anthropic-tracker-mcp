FROM python:3.14-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6

# Apply Debian security patches on top of the pinned base. Keeps the digest
# pin for reproducibility while picking up CVE fixes between base rebuilds.
# CACHE_BUST forces this layer to re-run against current Debian mirrors so a
# stale cached apt layer can't pin us to an unpatched libssl
# (e.g. CVE-2026-45447 fixed in libssl 3.5.6-1~deb13u2). Bump the date to
# refresh. Build with: --build-arg CACHE_BUST=$(date +%Y-%m-%d)
ARG CACHE_BUST=2026-06-15
RUN echo "cache-bust: ${CACHE_BUST}" \
    && apt-get update \
    && apt-get -y upgrade \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install from the hash-pinned lockfile. --require-hashes refuses any package
# whose hash isn't in the file. Reproducible byte-for-byte.
# Regenerate with: uv pip compile requirements.txt -o requirements.lock --generate-hashes
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY clients/ ./clients/
COPY server.py .
COPY healthcheck.py .

# Non-root user with pinned UID for predictable bind-mount ownership.
RUN useradd --create-home --uid 1000 --shell /bin/bash mcp \
    && chown -R mcp:mcp /app
USER mcp

EXPOSE 3713

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python healthcheck.py || exit 1

CMD ["python", "server.py"]
