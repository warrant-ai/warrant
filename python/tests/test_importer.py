import json
from pathlib import Path

import pytest

from warrant import SQLiteStore, validate, verify_records
from warrant.cli import main
from warrant.importer import ImportReport, Taxonomy, TaxonomyError, deterministic_ulid, import_traces, read_spans, reconstruct

ROOT = Path(__file__).resolve().parents[2]
TRACES = ROOT / "examples" / "import" / "traces.jsonl"
TAXONOMY = ROOT / "examples" / "import" / "taxonomy.yaml"
POLICIES = ROOT / "examples" / "policies"

TAX_JSON = {
    "tenant": "t", "stream": "s", "currency": "INR",
    "pricing": {"anthropic/claude-sonnet-5": {"input_per_1k": 0.25, "output_per_1k": 1.25}},
    "decisions": [{"class": "credit.approve", "match": {"tool": "approve_loan"}, "subject": "gen_ai.tool.call.arguments.loan_id", "action": "approve", "inputs": "gen_ai.tool.call.arguments"}],
}


def test_reads_otlp_jsonl_export():
    spans = list(read_spans([TRACES]))
    assert len(spans) == 24
    act = [s for s in spans if s.attributes.get("gen_ai.tool.name") == "approve_loan"][0]
    assert act.resource == {"service.name": "credit-underwriter", "service.version": "2.2.0"}
    assert act.parent_id and act.end_ns > act.start_ns
    chat = [s for s in spans if s.attributes.get("gen_ai.operation.name") == "chat"][0]
    assert chat.attributes["gen_ai.usage.input_tokens"] == 5000


def test_reads_python_console_exporter_json(tmp_path):
    doc = {
        "name": "execute_tool approve_loan",
        "context": {"trace_id": "0x" + "ab" * 16, "span_id": "0x" + "cd" * 8},
        "parent_id": "0x" + "ef" * 8,
        "start_time": "2026-09-16T10:00:00.000000Z", "end_time": "2026-09-16T10:00:01.500000Z",
        "status": {"status_code": "ERROR"},
        "attributes": {"gen_ai.tool.name": "approve_loan", "gen_ai.tool.call.arguments": {"loan_id": "LN-1", "amount": 5}},
        "resource": {"attributes": {"service.name": "svc"}},
    }
    f = tmp_path / "console.json"
    f.write_text(json.dumps([doc]), encoding="utf-8")
    (span,) = list(read_spans([f]))
    assert span.trace_id == "ab" * 16 and span.span_id == "cd" * 8 and span.parent_id == "ef" * 8
    assert span.end_ns - span.start_ns == 1_500_000_000 and span.status_error
    assert span.attributes["gen_ai.tool.call.arguments"] == {"loan_id": "LN-1", "amount": 5}
    report = reconstruct([span], Taxonomy.from_dict(TAX_JSON))
    (record,) = report.records
    assert record["decision"]["subject"] == "LN-1" and record["decision"]["status"] == "failed"
    assert record["actor"] == {"name": "svc", "version": "0"}


def test_reconstruct_builds_valid_imported_records_with_evidence_and_cost():
    report = reconstruct(read_spans([TRACES]), Taxonomy.from_dict(TAX_JSON), files=1)
    assert (report.spans, report.traces, len(report.records), report.skipped) == (24, 6, 6, [])
    r = report.records[0]
    validate(r)
    assert r["origin"] == "imported" and r["stream"] == "s" and r["tenant"] == "t"
    assert r["actor"] == {"name": "credit-underwriter", "version": "2.2.0"}
    assert r["decision"]["subject"] == "LN-30001" and r["decision"]["action"] == "approve" and r["decision"]["status"] == "acted"
    assert r["decision"]["inputs"] == {"loan_id": "LN-30001", "amount": 450000, "bureau_score": 748, "foir": 0.38}
    assert [(e["type"], e["name"]) for e in r["evidence"]] == [("tool_call", "bureau_pull"), ("model_call", "anthropic/claude-sonnet-5"), ("tool_call", "approve_loan")]
    assert r["evidence"][1]["uri"].startswith("otel://trace/") and len(r["evidence"][1]["content_hash"]) == 64
    assert r["cost"] == {"amount": pytest.approx(1.75), "currency": "INR", "breakdown": [{"kind": "model_call", "provider": "anthropic", "model": "claude-sonnet-5", "tokens_in": 5000, "tokens_out": 400, "amount": 1.75}]}
    assert r["mandate"] == {"result": "unchecked", "reason": "no policy bundle supplied"}
    assert r["timestamp"].endswith("Z")


def test_retrospective_policy_check_and_finding_report():
    pytest.importorskip("celpy")
    from warrant.policy import CelPolicyEngine, PolicyBundle

    engine = CelPolicyEngine(PolicyBundle.load(POLICIES))
    report = reconstruct(read_spans([TRACES]), Taxonomy.from_dict(TAX_JSON), engine, files=1)
    report.policy_label = "CR-07@2026.3"
    results = {r["decision"]["subject"]: r["mandate"]["result"] for r in report.records}
    assert results == {"LN-30001": "allow", "LN-30002": "escalate", "LN-30003": "deny", "LN-30004": "escalate", "LN-30005": "allow", "LN-30006": "allow"}
    assert report.by_class() == {"credit.approve": {"decisions": 6, "allow": 3, "deny": 1, "escalate": 2, "unchecked": 0, "with_inputs": 6, "with_model_calls": 6}}
    assert [r["decision"]["subject"] for r in report.outside_mandate()] == ["LN-30002", "LN-30003", "LN-30004"]
    text = report.summary()
    assert "read 24 span(s) in 6 trace(s) from 1 file(s)" in text
    assert "would write 6 record(s) (dry run)" in text
    assert "credit.approve: 6 decision(s), 6 with inputs, 6 with model calls checked against CR-07@2026.3" in text
    assert "allow 3   deny 1   escalate 2   unchecked 0" in text
    assert "3 decision(s) outside mandate:" in text and "LN-30003  deny  CR-07 clause 4.1  Decline below bureau floor" in text
    data = report.to_dict()
    assert data["outside_mandate"][1]["mandate"]["clause"] == "4.1" and json.dumps(data)


def test_missing_inputs_or_subject_fall_back_gracefully():
    tax = Taxonomy.from_dict({"decisions": [{"class": "x.y", "match": {"span_name": "execute_tool approve*"}}]})
    report = reconstruct(read_spans([TRACES]), tax, policy=_AlwaysAllow())
    r = report.records[0]
    assert r["decision"]["subject"].startswith("trace:") and r["decision"]["action"] == "act"
    assert "inputs" not in r["decision"] and r["mandate"] == {"result": "unchecked", "reason": "no inputs found on the decision span"}
    assert r["actor"]["name"] == "credit-underwriter"


class _AlwaysAllow:
    def evaluate(self, decision_class, inputs):
        from warrant import Verdict

        return Verdict("allow", policy_id="P", policy_version="1", clause="1")


def test_import_writes_sealed_records_and_is_idempotent(tmp_path):
    store = SQLiteStore(tmp_path / "r.db")
    tax = Taxonomy.from_dict(TAX_JSON)
    report = import_traces([TRACES], tax, store=store, stream="lending-import")
    assert (report.written, report.duplicates) == (6, 0)
    again = import_traces([TRACES], tax, store=store, stream="lending-import")
    assert (again.written, again.duplicates) == (0, 6) and "6 already present" in again.summary()
    records = list(store.iter_records("lending-import"))
    assert len(records) == 6 and all(r["origin"] == "imported" for r in records)
    (chain,) = verify_records(records)
    assert chain.ok
    assert store.find_decision("lending-import", "LN-30003") == records[2]["record_id"]
    store.close()


def test_deterministic_ulid_is_stable_and_valid():
    a = deterministic_ulid(1758000000000, "trace/span")
    assert a == deterministic_ulid(1758000000000, "trace/span") and a != deterministic_ulid(1758000000000, "other")
    import re

    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", a)


@pytest.mark.parametrize("raw,match", [
    ([], "top level"),
    ({}, "'decisions'"),
    ({"decisions": [{"match": {"tool": "x"}}]}, "needs a 'class'"),
    ({"decisions": [{"class": "a.b", "match": {}}]}, "tool, operation or span_name"),
    ({"decisions": [{"class": "a.b", "match": {"tool": "x"}, "action": 3}]}, "'action'"),
    ({"decisions": [{"class": "a.b", "match": {"tool": "x"}}], "pricing": []}, "'pricing'"),
])
def test_taxonomy_validation(raw, match):
    with pytest.raises(TaxonomyError, match=match):
        Taxonomy.from_dict(raw)


def test_taxonomy_loads_yaml_and_json(tmp_path):
    pytest.importorskip("yaml")
    tax = Taxonomy.load(TAXONOMY)
    assert tax.stream == "lending-import" and tax.currency == "INR" and tax.rules[0].match == {"tool": "approve_loan"}
    assert tax.pricing["anthropic/claude-sonnet-5"]["output_per_1k"] == 1.25
    j = tmp_path / "t.json"
    j.write_text(json.dumps(TAX_JSON), encoding="utf-8")
    assert Taxonomy.load(j).rules[0].decision_class == "credit.approve"
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    with pytest.raises(TaxonomyError, match="cannot parse"):
        Taxonomy.load(bad)


def test_bad_export_files(tmp_path):
    f = tmp_path / "broken.jsonl"
    f.write_text('{"resourceSpans": []}\n{not json\n', encoding="utf-8")
    with pytest.raises(ValueError, match="line 2"):
        list(read_spans([f]))
    empty = tmp_path / "empty.json"
    empty.write_text("", encoding="utf-8")
    assert list(read_spans([empty])) == []


def test_cli_import(tmp_path, capsys):
    pytest.importorskip("celpy")
    db = str(tmp_path / "r.db")
    assert main(["import", str(TRACES), "--taxonomy", str(TAXONOMY), "--store", db, "--policy", str(POLICIES), "--dry-run", "--report", str(tmp_path / "r.json")]) == 0
    out = capsys.readouterr().out
    assert "would write 6 record(s) (dry run) to stream 'lending-import' with origin: imported" in out
    assert "3 decision(s) outside mandate:" in out and "json report:" in out
    assert not Path(db).exists()
    assert main(["import", str(TRACES), "--taxonomy", str(TAXONOMY), "--store", db, "--policy", str(POLICIES)]) == 0
    assert "wrote 6 record(s)" in capsys.readouterr().out
    assert main(["import", str(TRACES), "--taxonomy", str(TAXONOMY), "--store", db]) == 0
    assert "6 already present" in capsys.readouterr().out
    assert main(["export", "--store", db, "-o", str(tmp_path / "e.jsonl")]) == 0 and main(["verify", str(tmp_path / "e.jsonl")]) == 0
    capsys.readouterr()
    assert main(["import", str(tmp_path / "missing.jsonl"), "--taxonomy", str(TAXONOMY), "--store", db, "--dry-run"]) == 1
    assert "file not found" in capsys.readouterr().err
    assert main(["import", str(TRACES), "--taxonomy", str(tmp_path / "missing.yaml"), "--store", db]) == 1
    (tmp_path / "none.json").write_text(json.dumps({"decisions": [{"class": "a.b", "match": {"tool": "nothing_here"}}]}), encoding="utf-8")
    assert main(["import", str(TRACES), "--taxonomy", str(tmp_path / "none.json"), "--store", db, "--dry-run"]) == 1
    assert "would write 0 record(s)" in capsys.readouterr().out
