FROM python:3.14-slim@sha256:5b3879b6f3cb77e712644d50262d05a7c146b7312d784a18eff7ff5462e77033

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
