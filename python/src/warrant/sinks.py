"""Sinks that deliver record batches somewhere other than a local store."""

from __future__ import annotations

import gzip
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Sequence

from warrant import __version__
from warrant.emit import PermanentSinkError, SinkError

log = logging.getLogger("warrant.sinks")


class HttpSink:
    """POST batches to a Warrant collector. 5xx and connection errors are transient (the
    emitter spills and retries); 4xx responses are permanent (dead-lettered)."""

    def __init__(self, url: str, token: Optional[str] = None, *, timeout: float = 10.0, compress: bool = True) -> None:
        if not url.startswith(("http://", "https://")):
            raise ValueError("collector url must start with http:// or https://")
        self.url = url.rstrip("/") + "/v1/records"
        self._token = token if token is not None else os.environ.get("WARRANT_TOKEN")
        self._timeout = timeout
        self._compress = compress
        if url.startswith("http://") and not url.startswith("http://localhost") and not url.startswith("http://127."):
            log.warning("warrant: collector url %s is not https; records will travel in clear", url)

    def write(self, records: Sequence[Dict[str, Any]]) -> None:
        body = json.dumps({"records": list(records)}, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": f"warrantai-python/{__version__}"}
        if self._compress and len(body) > 1024:
            body = gzip.compress(body)
            headers["Content-Encoding"] = "gzip"
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                if response.status not in (200, 202):
                    raise SinkError(f"collector returned {response.status}")
        except urllib.error.HTTPError as exc:
            detail = _read_error(exc)
            if 400 <= exc.code < 500 and exc.code not in (408, 429):
                raise PermanentSinkError(f"collector rejected batch ({exc.code}): {detail}") from exc
            raise SinkError(f"collector error {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SinkError(f"collector unreachable: {exc}") from exc


def _read_error(exc: urllib.error.HTTPError) -> str:
    try:
        text = exc.read().decode("utf-8", "replace")
    except Exception:
        return exc.reason or ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text[:300]
    detail = str(data.get("detail") or data.get("error") or text)
    problems = data.get("problems")
    if isinstance(problems, list) and problems and isinstance(problems[0], dict):
        detail += f"; first: {problems[0].get('error')}"
    return detail[:300]
