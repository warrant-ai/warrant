"""Reconstruct decision records from existing OpenTelemetry trace exports.

``warrant import`` reads OTLP JSON (the collector's file exporter, one document or
JSONL) or the Python SDK's console exporter JSON, finds the side-effecting spans a
taxonomy names as decisions, and writes one record per span with ``origin:
imported``. Other generative-AI spans in the same trace become evidence. With a
policy bundle, each decision is checked retrospectively and the report says which
fell outside mandate.

Taxonomy (YAML or JSON)::

    tenant: demo-bank
    stream: lending-import
    currency: INR
    agent: {name_attr: service.name, version_attr: service.version, name: underwriter, version: "0"}
    pricing: {"anthropic/claude-sonnet-5": {input_per_1k: 0.3, output_per_1k: 1.5}}
    decisions:
      - class: credit.approve
        match: {tool: approve_loan}            # tool | operation | span_name (glob)
        subject: gen_ai.tool.call.arguments.loan_id
        action: approve                        # constant, or attr:<path>
        inputs: gen_ai.tool.call.arguments     # attribute holding the inputs (JSON string or map)
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple, Union

from warrant.client import PolicyEngine, Verdict
from warrant.hashing import content_hash
from warrant.ids import _ALPHABET
from warrant.otel_attrs import classify_attrs, first_attr, to_int
from warrant.schema import SCHEMA_VERSION, ValidationError, validate
from warrant.store import SQLiteStore

log = logging.getLogger("warrant.import")


class TaxonomyError(ValueError):
    """The taxonomy file is malformed."""


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_id: Optional[str]
    name: str
    start_ns: int
    end_ns: int
    attributes: Dict[str, Any]
    resource: Dict[str, Any] = field(default_factory=dict)
    status_error: bool = False


@dataclass
class DecisionRule:
    decision_class: str
    match: Dict[str, str]
    subject: Optional[str]
    action: str
    inputs: Optional[str]

    def matches(self, span: Span) -> bool:
        attrs = span.attributes
        if "tool" in self.match and attrs.get("gen_ai.tool.name") != self.match["tool"]:
            return False
        if "operation" in self.match and attrs.get("gen_ai.operation.name") != self.match["operation"]:
            return False
        if "span_name" in self.match and not fnmatch.fnmatchcase(span.name, self.match["span_name"]):
            return False
        return True


@dataclass
class Taxonomy:
    rules: List[DecisionRule]
    tenant: str = "imported"
    stream: str = "imported"
    currency: str = "USD"
    agent_name_attr: str = "service.name"
    agent_version_attr: str = "service.version"
    agent_name: str = "unknown-agent"
    agent_version: str = "0"
    pricing: Dict[str, Dict[str, float]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Union[str, Path]) -> "Taxonomy":
        path = Path(path)
        text = path.read_text(encoding="utf-8")
        try:
            if path.suffix.lower() == ".json":
                raw = json.loads(text)
            else:
                try:
                    import yaml
                except ImportError as exc:
                    raise ImportError('YAML taxonomies need pyyaml (installed with "warrantai[policy]"); or write the taxonomy as JSON') from exc
                raw = yaml.safe_load(text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise TaxonomyError(f"{path.name}: cannot parse: {exc}") from exc
        except Exception as exc:
            raise TaxonomyError(f"{path.name}: cannot parse: {exc}") from exc
        return cls.from_dict(raw, source=path.name)

    @classmethod
    def from_dict(cls, raw: Any, source: str = "taxonomy") -> "Taxonomy":
        if not isinstance(raw, dict):
            raise TaxonomyError(f"{source}: top level must be a mapping")
        rules_raw = raw.get("decisions")
        if not isinstance(rules_raw, list) or not rules_raw:
            raise TaxonomyError(f"{source}: 'decisions' must be a non-empty list")
        rules: List[DecisionRule] = []
        for i, r in enumerate(rules_raw, start=1):
            if not isinstance(r, dict):
                raise TaxonomyError(f"{source}: decision #{i} must be a mapping")
            cls_name = r.get("class")
            if not isinstance(cls_name, str) or not cls_name:
                raise TaxonomyError(f"{source}: decision #{i} needs a 'class'")
            match = r.get("match")
            if not isinstance(match, dict) or not any(k in match for k in ("tool", "operation", "span_name")):
                raise TaxonomyError(f"{source}: decision #{i} ({cls_name}): 'match' needs tool, operation or span_name")
            action = r.get("action", "act")
            if not isinstance(action, str) or not action:
                raise TaxonomyError(f"{source}: decision #{i} ({cls_name}): 'action' must be a string")
            rules.append(DecisionRule(cls_name, {k: str(v) for k, v in match.items()}, r.get("subject"), action, r.get("inputs")))
        agent = raw.get("agent") or {}
        pricing = raw.get("pricing")
        if pricing is None:
            pricing = {}
        if not isinstance(pricing, dict):
            raise TaxonomyError(f"{source}: 'pricing' must be a mapping of provider/model to rates")
        return cls(
            rules=rules,
            tenant=str(raw.get("tenant") or "imported"),
            stream=str(raw.get("stream") or "imported"),
            currency=str(raw.get("currency") or "USD"),
            agent_name_attr=str(agent.get("name_attr") or "service.name"),
            agent_version_attr=str(agent.get("version_attr") or "service.version"),
            agent_name=str(agent.get("name") or "unknown-agent"),
            agent_version=str(agent.get("version") or "0"),
            pricing={str(k): {"input_per_1k": float(v.get("input_per_1k", 0)), "output_per_1k": float(v.get("output_per_1k", 0))} for k, v in pricing.items()},
        )


# -- readers -------------------------------------------------------------------


def read_spans(paths: Sequence[Union[str, Path]]) -> Iterator[Span]:
    """Yield spans from OTLP JSON / JSONL files or Python SDK console-exporter JSON."""
    for path in paths:
        path = Path(path)
        text = path.read_text(encoding="utf-8")
        for doc in _documents(text, path.name):
            yield from _spans_from_document(doc)


def _documents(text: str, name: str) -> Iterator[Any]:
    stripped = text.strip()
    if not stripped:
        return
    try:
        yield json.loads(stripped)
        return
    except json.JSONDecodeError:
        pass
    for lineno, line in enumerate(stripped.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{name}: line {lineno}: {exc.msg}") from exc


def _spans_from_document(doc: Any) -> Iterator[Span]:
    if isinstance(doc, list):
        for item in doc:
            yield from _spans_from_document(item)
        return
    if not isinstance(doc, dict):
        return
    if "resourceSpans" in doc:
        for rs in doc.get("resourceSpans") or []:
            resource = _otlp_attrs((rs.get("resource") or {}).get("attributes") or [])
            for ss in rs.get("scopeSpans") or rs.get("instrumentationLibrarySpans") or []:
                for sp in ss.get("spans") or []:
                    yield _otlp_span(sp, resource)
        return
    if "context" in doc and "name" in doc:
        yield _console_span(doc)


def _otlp_attrs(items: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for item in items:
        key = item.get("key")
        if key is not None:
            out[key] = _otlp_value(item.get("value") or {})
    return out


def _otlp_value(value: Dict[str, Any]) -> Any:
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        return to_int(value["intValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "boolValue" in value:
        return bool(value["boolValue"])
    if "arrayValue" in value:
        return [_otlp_value(v) for v in (value["arrayValue"] or {}).get("values") or []]
    if "kvlistValue" in value:
        return _otlp_attrs((value["kvlistValue"] or {}).get("values") or [])
    return None


def _otlp_span(sp: Dict[str, Any], resource: Dict[str, Any]) -> Span:
    status = sp.get("status") or {}
    return Span(
        trace_id=str(sp.get("traceId", "")).lower(),
        span_id=str(sp.get("spanId", "")).lower(),
        parent_id=(str(sp["parentSpanId"]).lower() if sp.get("parentSpanId") else None),
        name=str(sp.get("name", "")),
        start_ns=to_int(sp.get("startTimeUnixNano")),
        end_ns=to_int(sp.get("endTimeUnixNano")),
        attributes=_otlp_attrs(sp.get("attributes") or []),
        resource=resource,
        status_error=str(status.get("code", "")).upper() in ("STATUS_CODE_ERROR", "2", "ERROR"),
    )


def _console_span(doc: Dict[str, Any]) -> Span:
    ctx = doc.get("context") or {}
    resource = (doc.get("resource") or {}).get("attributes") or {}
    status = doc.get("status") or {}
    return Span(
        trace_id=str(ctx.get("trace_id", "")).replace("0x", "").lower(),
        span_id=str(ctx.get("span_id", "")).replace("0x", "").lower(),
        parent_id=(str(doc["parent_id"]).replace("0x", "").lower() if doc.get("parent_id") else None),
        name=str(doc.get("name", "")),
        start_ns=_iso_to_ns(doc.get("start_time")),
        end_ns=_iso_to_ns(doc.get("end_time")),
        attributes=dict(doc.get("attributes") or {}),
        resource=dict(resource),
        status_error=str(status.get("status_code", "")).upper() == "ERROR",
    )


def _iso_to_ns(value: Any) -> int:
    if not value:
        return 0
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1_000_000_000)


# -- reconstruction --------------------------------------------------------------


@dataclass
class ImportReport:
    files: int
    spans: int
    traces: int
    stream: str
    records: List[Dict[str, Any]]
    skipped: List[str] = field(default_factory=list)
    written: int = 0
    duplicates: int = 0
    policy_label: Optional[str] = None
    dry_run: bool = True

    def by_class(self) -> Dict[str, Dict[str, int]]:
        out: Dict[str, Dict[str, int]] = {}
        for r in self.records:
            cls = r["decision"]["class"]
            bucket = out.setdefault(cls, {"decisions": 0, "allow": 0, "deny": 0, "escalate": 0, "unchecked": 0, "with_inputs": 0, "with_model_calls": 0})
            bucket["decisions"] += 1
            bucket[r["mandate"]["result"]] += 1
            if "inputs" in r["decision"]:
                bucket["with_inputs"] += 1
            if any(e["type"] == "model_call" for e in r["evidence"]):
                bucket["with_model_calls"] += 1
        return out

    def outside_mandate(self) -> List[Dict[str, Any]]:
        return [r for r in self.records if r["mandate"]["result"] in ("deny", "escalate")]

    def summary(self, max_listed: int = 20) -> str:
        lines = [f"read {self.spans} span(s) in {self.traces} trace(s) from {self.files} file(s)"]
        verb = f"would write {len(self.records)} record(s) (dry run)" if self.dry_run else f"wrote {self.written} record(s)"
        dup = f", {self.duplicates} already present" if self.duplicates else ""
        lines.append(f"{verb} to stream {self.stream!r} with origin: imported{dup}")
        if self.skipped:
            lines.append(f"skipped {len(self.skipped)} matched span(s): {self.skipped[0]}" + (" ..." if len(self.skipped) > 1 else ""))
        for cls, b in sorted(self.by_class().items()):
            checked = b["allow"] + b["deny"] + b["escalate"]
            against = f" checked against {self.policy_label}" if self.policy_label and checked else ""
            lines.append(f"  {cls}: {b['decisions']} decision(s), {b['with_inputs']} with inputs, {b['with_model_calls']} with model calls{against}")
            lines.append(f"    allow {b['allow']}   deny {b['deny']}   escalate {b['escalate']}   unchecked {b['unchecked']}")
        outside = self.outside_mandate()
        if outside:
            lines.append(f"  {len(outside)} decision(s) outside mandate:")
            for r in outside[:max_listed]:
                m = r["mandate"]
                where = f"{m.get('policy_id', '')} clause {m['clause']}" if m.get("clause") else f"{m.get('policy_id', '')} default"
                lines.append(f"    {r['decision']['subject']}  {m['result']}  {where}  {m.get('reason', '')}".rstrip())
            if len(outside) > max_listed:
                lines.append(f"    ... and {len(outside) - max_listed} more")
        elif self.policy_label:
            lines.append("  no decisions outside mandate")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "files": self.files, "spans": self.spans, "traces": self.traces, "stream": self.stream,
            "written": self.written, "duplicates": self.duplicates, "skipped": self.skipped, "policy": self.policy_label,
            "by_class": self.by_class(),
            "outside_mandate": [{"record_id": r["record_id"], "subject": r["decision"]["subject"], "class": r["decision"]["class"], "mandate": r["mandate"]} for r in self.outside_mandate()],
        }


def reconstruct(spans: Iterable[Span], taxonomy: Taxonomy, policy: Optional[PolicyEngine] = None, *, stream: Optional[str] = None, files: int = 0) -> ImportReport:
    """Turn spans into unsealed decision records according to the taxonomy."""
    by_trace: Dict[str, List[Span]] = defaultdict(list)
    count = 0
    for span in spans:
        count += 1
        by_trace[span.trace_id].append(span)
    stream = stream or taxonomy.stream
    records: List[Dict[str, Any]] = []
    skipped: List[str] = []
    for trace_id, trace in by_trace.items():
        trace.sort(key=lambda s: (s.start_ns, s.span_id))
        for span in trace:
            rule = next((r for r in taxonomy.rules if r.matches(span)), None)
            if rule is None:
                continue
            try:
                record = _record_for(span, trace, rule, taxonomy, policy, stream)
                validate(record)
            except (ValidationError, ValueError) as exc:
                skipped.append(f"{span.name} in trace {trace_id[:8]}: {exc}")
                continue
            records.append(record)
    return ImportReport(files=files, spans=count, traces=len(by_trace), stream=stream, records=records, skipped=skipped)


def _record_for(span: Span, trace: List[Span], rule: DecisionRule, tax: Taxonomy, policy: Optional[PolicyEngine], stream: str) -> Dict[str, Any]:
    subject = _resolve(span.attributes, rule.subject) if rule.subject else None
    subject = str(subject) if subject not in (None, "") else f"trace:{span.trace_id}"
    action = rule.action
    if action.startswith("attr:"):
        resolved = _resolve(span.attributes, action[5:])
        action = str(resolved) if resolved not in (None, "") else "unknown"
    inputs = _resolve(span.attributes, rule.inputs) if rule.inputs else None
    if inputs is not None and not isinstance(inputs, dict):
        inputs = None

    evidence: List[Dict[str, Any]] = []
    breakdown: List[Dict[str, Any]] = []
    total = 0.0
    for other in trace:
        if other.span_id == span.span_id or other.end_ns > span.end_ns:
            continue
        kind = classify_attrs(other.attributes)
        if kind is None:
            continue
        uri = f"otel://trace/{other.trace_id}/span/{other.span_id}"
        digest = content_hash({"name": other.name, "attributes": other.attributes})
        if kind == "model_call":
            provider = str(first_attr(other.attributes, "gen_ai.provider.name", "gen_ai.system") or "unknown")
            model = str(first_attr(other.attributes, "gen_ai.response.model", "gen_ai.request.model") or "unknown")
            tin = to_int(first_attr(other.attributes, "gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens"))
            tout = to_int(first_attr(other.attributes, "gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens"))
            rate = tax.pricing.get(f"{provider}/{model}")
            amount = round(tin / 1000 * rate["input_per_1k"] + tout / 1000 * rate["output_per_1k"], 6) if rate else 0.0
            total += amount
            evidence.append({"name": f"{provider}/{model}", "type": "model_call", "uri": uri, "content_hash": digest})
            breakdown.append({"kind": "model_call", "provider": provider, "model": model, "tokens_in": tin, "tokens_out": tout, "amount": amount})
        else:
            name = str(first_attr(other.attributes, "gen_ai.tool.name") or other.name)
            evidence.append({"name": name, "type": "tool_call", "uri": uri, "content_hash": digest})
    # the decision span itself is evidence of the action taken
    evidence.append({"name": str(first_attr(span.attributes, "gen_ai.tool.name") or span.name), "type": "tool_call",
                     "uri": f"otel://trace/{span.trace_id}/span/{span.span_id}", "content_hash": content_hash({"name": span.name, "attributes": span.attributes})})

    if policy is not None and inputs is not None:
        verdict = policy.evaluate(rule.decision_class, inputs)
    elif policy is not None:
        verdict = Verdict("unchecked", reason="no inputs found on the decision span")
    else:
        verdict = Verdict("unchecked", reason="no policy bundle supplied")
    mandate: Dict[str, Any] = {"result": verdict.result}
    for key in ("policy_id", "policy_version", "clause", "reason"):
        if getattr(verdict, key):
            mandate[key] = getattr(verdict, key)
    if verdict.flagged:
        mandate["flagged"] = True

    decision: Dict[str, Any] = {"class": rule.decision_class, "action": action, "subject": subject, "status": "failed" if span.status_error else "acted", "summary": f"imported from span {span.name}"}
    if inputs is not None:
        decision["inputs"] = inputs
    cost: Dict[str, Any] = {"amount": round(total, 6), "currency": tax.currency}
    if breakdown:
        cost["breakdown"] = breakdown
    return {
        "record_id": deterministic_ulid(span.end_ns // 1_000_000, f"{span.trace_id}/{span.span_id}"),
        "record_type": "decision",
        "tenant": tax.tenant,
        "stream": stream,
        "timestamp": _ns_to_iso(span.end_ns),
        "schema_version": SCHEMA_VERSION,
        "origin": "imported",
        "actor": {"name": str(span.resource.get(tax.agent_name_attr) or tax.agent_name), "version": str(span.resource.get(tax.agent_version_attr) or tax.agent_version)},
        "decision": decision,
        "mandate": mandate,
        "evidence": evidence,
        "human": {"required": False},
        "cost": cost,
        "outcome": {"status": "pending"},
    }


def _resolve(attrs: Dict[str, Any], path: Optional[str]) -> Any:
    """Attribute lookup that descends into JSON-string or mapping attributes: ``a.b.c`` tries
    the exact key first, then the longest key prefix followed by keys inside its value."""
    if not path:
        return None
    if path in attrs:
        return _maybe_json(attrs[path])
    parts = path.split(".")
    for i in range(len(parts) - 1, 0, -1):
        key = ".".join(parts[:i])
        if key in attrs:
            value = _maybe_json(attrs[key])
            for p in parts[i:]:
                if isinstance(value, dict) and p in value:
                    value = value[p]
                else:
                    return None
            return value
    return None


def _maybe_json(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in ("{", "["):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def deterministic_ulid(ts_ms: int, key: str) -> str:
    """ULID whose random part is derived from ``key``, so re-importing the same span yields the same id."""
    ts_ms = max(0, min(ts_ms, (1 << 48) - 1))
    rand = int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:10], "big")
    value = (ts_ms << 80) | rand
    chars = []
    for _ in range(26):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def _ns_to_iso(ns: int) -> str:
    dt = datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def import_traces(
    paths: Sequence[Union[str, Path]],
    taxonomy: Taxonomy,
    *,
    store: Optional[SQLiteStore] = None,
    policy: Optional[PolicyEngine] = None,
    policy_label: Optional[str] = None,
    stream: Optional[str] = None,
) -> ImportReport:
    """Read, reconstruct, and (unless ``store`` is None) write. Records already in the store are skipped."""
    report = reconstruct(read_spans(paths), taxonomy, policy, stream=stream, files=len(paths))
    report.policy_label = policy_label
    report.dry_run = store is None
    if store is not None and report.records:
        fresh = [r for r in report.records if store.get(r["record_id"]) is None]
        report.duplicates = len(report.records) - len(fresh)
        if fresh:
            store.write(fresh)
        report.written = len(fresh)
        log.info("warrant import: %d record(s) written to %s, %d duplicate(s) skipped", report.written, report.stream, report.duplicates)
    return report
