"""Health check script for Docker HEALTHCHECK.

The SSE endpoint streams text/event-stream responses, so we just check the
TCP-level HTTP response is 200.
"""

import sys
import urllib.request


def check():
    try:
        resp = urllib.request.urlopen("http://localhost:3713/sse", timeout=5)
        if resp.status == 200:
            sys.exit(0)
    except Exception:
        pass
    sys.exit(1)


if __name__ == "__main__":
    check()
