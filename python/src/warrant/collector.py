"""The collector: a stateless HTTP service that receives record batches, seals them
into the store, and answers health and metrics. Any number of instances can run
against one PostgreSQL store.

    warrant collector --store postgresql://... --listen 0.0.0.0:8787

Authentication is a bearer token per tenant, from ``WARRANT_COLLECTOR_TOKENS``
(``tenant:token,tenant2:token2``) or ``--token tenant:token``. A batch may only
contain records for the tenant its token belongs to. ``--insecure`` disables auth for
local development and says so loudly.

Protocol: ``POST /v1/records`` with ``{"records": [...]}`` (optionally gzip-encoded),
at most 1000 records and 8 MiB per batch. Responses: 202 ``{"accepted": n,
"duplicates": m}``; 400 invalid body or schema (per-record detail); 401/403 auth;
413 too large; 503 store unavailable. ``GET /healthz``, ``GET /readyz``, ``GET /metrics``.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from warrant import __version__
from warrant.emit import PermanentSinkError
from warrant.schema import ValidationError, validate

try:
    from fastapi import FastAPI, Request, Response
    from fastapi.responses import JSONResponse, PlainTextResponse
except ImportError as exc:  # pragma: no cover
    raise ImportError('the collector needs fastapi and uvicorn: pip install "warrantai[collector]"') from exc

log = logging.getLogger("warrant.collector")

MAX_RECORDS = 1000
MAX_BYTES = 8 * 1024 * 1024


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.batches = 0
        self.accepted = 0
        self.duplicates = 0
        self.rejected = 0
        self.store_errors = 0
        self.auth_failures = 0

    def bump(self, **counts: int) -> None:
        with self._lock:
            for key, n in counts.items():
                setattr(self, key, getattr(self, key) + n)

    def render(self) -> str:
        lines = []
        for key in ("batches", "accepted", "duplicates", "rejected", "store_errors", "auth_failures"):
            lines.append(f"# TYPE warrant_collector_{key}_total counter")
            lines.append(f"warrant_collector_{key}_total {getattr(self, key)}")
        return "\n".join(lines) + "\n"


def parse_tokens(spec: Optional[str]) -> Dict[str, str]:
    """``tenant:token,tenant2:token2`` -> {token: tenant}."""
    tokens: Dict[str, str] = {}
    for item in (spec or "").split(","):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            raise ValueError(f"token entry must be tenant:token, got {item!r}")
        tenant, token = item.split(":", 1)
        if not tenant or len(token) < 16:
            raise ValueError(f"token for tenant {tenant!r} must be at least 16 characters")
        tokens[token] = tenant
    return tokens


def create_app(store, *, tokens: Optional[Dict[str, str]] = None, insecure: bool = False) -> FastAPI:
    """Build the collector app around any store with ``write(records)`` and ``get(record_id)``."""
    if not tokens and not insecure:
        raise ValueError("no tokens configured; pass tokens or insecure=True for local development")
    if insecure:
        log.warning("warrant collector running WITHOUT authentication; only for local development")
    tokens = tokens or {}
    metrics = Metrics()
    app = FastAPI(title="warrant collector", version=__version__, docs_url=None, redoc_url=None)
    app.state.store = store
    app.state.metrics = metrics

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        started = time.perf_counter()
        response: Response = await call_next(request)
        response.headers["x-request-id"] = rid
        if request.url.path == "/v1/records":
            log.info("warrant collector %s %s -> %d in %.1f ms rid=%s", request.method, request.url.path, response.status_code, (time.perf_counter() - started) * 1000, rid)
        return response

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "version": __version__}

    @app.get("/readyz")
    def readyz():
        ping = getattr(store, "ping", None)
        ready = ping() if callable(ping) else True
        return JSONResponse({"status": "ok" if ready else "store unavailable"}, status_code=200 if ready else 503)

    @app.get("/metrics")
    def metrics_endpoint():
        return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")

    @app.post("/v1/records")
    async def ingest(request: Request):
        tenant: Optional[str] = None
        if not insecure:
            auth = request.headers.get("authorization", "")
            token = auth[7:] if auth.lower().startswith("bearer ") else ""
            tenant = tokens.get(token)
            if tenant is None:
                metrics.bump(auth_failures=1)
                return JSONResponse({"detail": "missing or invalid bearer token"}, status_code=401)
        raw = await request.body()
        if len(raw) > MAX_BYTES:
            metrics.bump(rejected=1)
            return JSONResponse({"detail": f"batch exceeds {MAX_BYTES} bytes"}, status_code=413)
        if request.headers.get("content-encoding", "").lower() == "gzip":
            try:
                raw = gzip.decompress(raw)
            except (OSError, EOFError) as exc:
                metrics.bump(rejected=1)
                return JSONResponse({"detail": f"invalid gzip body: {exc}"}, status_code=400)
            if len(raw) > MAX_BYTES:
                metrics.bump(rejected=1)
                return JSONResponse({"detail": f"batch exceeds {MAX_BYTES} bytes"}, status_code=413)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            metrics.bump(rejected=1)
            return JSONResponse({"detail": f"invalid JSON: {exc.msg}"}, status_code=400)
        records = payload.get("records") if isinstance(payload, dict) else None
        if not isinstance(records, list) or not records:
            metrics.bump(rejected=1)
            return JSONResponse({"detail": "body must be {\"records\": [...]} with at least one record"}, status_code=400)
        if len(records) > MAX_RECORDS:
            metrics.bump(rejected=1)
            return JSONResponse({"detail": f"batch has {len(records)} records; the limit is {MAX_RECORDS}"}, status_code=413)

        problems: List[Dict[str, Any]] = []
        for i, record in enumerate(records):
            if not isinstance(record, dict):
                problems.append({"index": i, "error": "not an object"})
                continue
            if "seal" in record:
                problems.append({"index": i, "record_id": record.get("record_id"), "error": "record is already sealed"})
                continue
            if tenant is not None and record.get("tenant") != tenant:
                problems.append({"index": i, "record_id": record.get("record_id"), "error": f"tenant {record.get('tenant')!r} does not match the token's tenant"})
                continue
            try:
                validate({k: v for k, v in record.items() if k != "_blobs"})
            except ValidationError as exc:
                problems.append({"index": i, "record_id": record.get("record_id"), "error": exc.errors[0]})
        if problems:
            metrics.bump(rejected=1)
            status = 403 if any("tenant" in p["error"] for p in problems) else 400
            return JSONResponse({"detail": f"{len(problems)} record(s) rejected", "problems": problems[:20]}, status_code=status)

        ids = [r["record_id"] for r in records]
        existing = sum(1 for rid in ids if store.get(rid) is not None)
        try:
            store.write(records)
        except PermanentSinkError as exc:
            metrics.bump(rejected=1)
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except Exception as exc:
            metrics.bump(store_errors=1)
            log.error("warrant collector store write failed: %s", exc)
            return JSONResponse({"detail": "store unavailable"}, status_code=503)
        accepted = len(records) - existing
        metrics.bump(batches=1, accepted=accepted, duplicates=existing)
        return JSONResponse({"accepted": accepted, "duplicates": existing}, status_code=202)

    return app


def serve(store_url: str, *, listen: str = "127.0.0.1:8787", tokens: Optional[Dict[str, str]] = None, insecure: bool = False) -> None:
    """Run the collector with uvicorn until interrupted."""
    import uvicorn

    from warrant.store import open_store

    store = open_store(store_url)
    app = create_app(store, tokens=tokens, insecure=insecure)
    host, _, port = listen.rpartition(":")
    log.info("warrant collector %s listening on %s, store %s", __version__, listen, getattr(store, "path", store_url))
    try:
        uvicorn.run(app, host=host or "127.0.0.1", port=int(port), log_level="warning")
    finally:
        store.close()
