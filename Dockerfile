FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4

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
# Regenerate with: uv pip compile requirements.txt -o requirements.lock \
#   --generate-hashes --python-version 3.13 --python-platform linux
# The platform flag matters: this lock is installed with --require-hashes in a
# Linux image, and resolving on macOS produces different transitive versions
# and wheel hashes.
COPY requirements.lock .
# pip is a build-time tool only: server.py and healthcheck.py never shell out to
# it. Purging it after the install removes pip's vendored third-party copies
# (pip/_vendor/msgpack, pip/_vendor/pkg_resources) which Trivy reports as
# msgpack and setuptools findings against the image even though nothing in the
# lockfile depends on them. Those copies are not separately upgradable, so
# deleting pip is the fix rather than a version bump.
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3.14/site-packages/pip \
       /usr/local/lib/python3.14/site-packages/pip-*.dist-info \
       /usr/local/lib/python3.14/ensurepip \
    && rm -f /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.*

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
