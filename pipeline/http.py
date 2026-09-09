"""Minimal JSON-over-HTTPS helper.

Deliberately stdlib-only: the pipeline installs no dependencies, so a GitHub
Action run cannot break on an upstream package release, and cold start is fast.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

USER_AGENT = "cfb-spreads/1.0 (+https://github.com/thanejohannsen/CFB_Spreads)"


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
        self.status = status


def get_json(url: str, params: Optional[dict] = None, headers: Optional[dict] = None,
             timeout: int = 60, retries: int = 4) -> dict:
    """GET a JSON document, retrying transient failures with backoff."""
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        url = f"{url}?{urllib.parse.urlencode(clean)}"

    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    hdrs.update(headers or {})

    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            # Client errors other than rate limiting will not fix themselves.
            if e.code < 500 and e.code != 429:
                raise HttpError(e.code, url, body) from e
            last = HttpError(e.code, url, body)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            last = e
        if attempt < retries - 1:
            time.sleep(2 ** attempt)
    raise last if last else RuntimeError(f"failed to GET {url}")
