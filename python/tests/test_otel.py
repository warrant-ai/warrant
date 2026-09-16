import logging

import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry import trace as otel_trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.trace import StatusCode  # noqa: E402

from warrant import AgentInfo, Warrant  # noqa: E402
from warrant.otel import WarrantSpanProcessor, classify  # noqa: E402


@pytest.fixture
def tracer_and_client(tmp_path):
    provider = TracerProvider()
    processor = WarrantSpanProcessor(pricer=lambda p, m, i, o: i * 0.001 + o * 0.002)
    provider.add_span_processor(processor)
    tracer = provider.get_tracer("test")
    w = Warrant("lending", store=tmp_path / "r.db", agent=AgentInfo("a", "1"), currency="INR", flush_interval=0.02)
    yield tracer, w, processor
    w.close()
    provider.shutdown()


def _records(w):
    assert w.flush()
    return list(w.store.iter_records("lending"))


def test_model_and_tool_spans_inside_scope_become_evidence_and_cost(tracer_and_client):
    tracer, w, _ = tracer_and_client
    with w.decide("credit.approve", subject="LN-1") as d:
        with tracer.start_as_current_span("chat claude-sonnet-5", attributes={
            "gen_ai.operation.name": "chat", "gen_ai.provider.name": "anthropic",
            "gen_ai.request.model": "claude-sonnet-5", "gen_ai.response.model": "claude-sonnet-5-20260401",
            "gen_ai.usage.input_tokens": 1000, "gen_ai.usage.output_tokens": 100,
        }) as span:
            trace_id, span_id = span.get_span_context().trace_id, span.get_span_context().span_id
        with tracer.start_as_current_span("execute_tool bureau_pull", attributes={
            "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "bureau_pull", "gen_ai.tool.call.id": "call_1",
        }):
            pass
        with tracer.start_as_current_span("db query", attributes={"db.system": "postgresql"}):
            pass
        d.act("approve")
    (record,) = _records(w)
    ev = record["evidence"]
    assert [(e["type"], e["name"]) for e in ev] == [("model_call", "anthropic/claude-sonnet-5-20260401"), ("tool_call", "bureau_pull")]
    assert ev[0]["uri"] == f"otel://trace/{trace_id:032x}/span/{span_id:016x}"
    assert "excerpt" not in ev[0]
    assert record["cost"]["amount"] == pytest.approx(1.2)
    assert record["cost"]["breakdown"][0] == {"kind": "model_call", "provider": "anthropic", "model": "claude-sonnet-5-20260401", "tokens_in": 1000, "tokens_out": 100, "amount": 1.2}


def test_spans_outside_scope_or_ending_after_close_are_ignored(tracer_and_client, caplog):
    tracer, w, processor = tracer_and_client
    with tracer.start_as_current_span("chat", attributes={"gen_ai.request.model": "m"}):
        pass
    late = None
    with w.decide("credit.approve", subject="LN-2") as d:
        late = tracer.start_span("chat late", attributes={"gen_ai.request.model": "m"})
        d.act("approve")
    with caplog.at_level(logging.DEBUG, logger="warrant.otel"):
        late.end()
    (record,) = _records(w)
    assert record["evidence"] == []
    assert any("ended after its decision closed" in r.message for r in caplog.records)
    assert processor._open == {}


def test_legacy_token_names_and_missing_provider(tracer_and_client):
    tracer, w, _ = tracer_and_client
    with w.decide("credit.approve", subject="LN-3") as d:
        with tracer.start_as_current_span("completion", attributes={
            "gen_ai.system": "openai", "gen_ai.request.model": "gpt-x",
            "gen_ai.usage.prompt_tokens": 10, "gen_ai.usage.completion_tokens": "5",
        }) as span:
            span.set_status(StatusCode.ERROR, "rate limited")
        with tracer.start_as_current_span("embed", attributes={"gen_ai.operation.name": "embeddings"}):
            pass
        d.act("approve")
    (record,) = _records(w)
    b = record["cost"]["breakdown"]
    assert b[0]["provider"] == "openai" and b[0]["tokens_in"] == 10 and b[0]["tokens_out"] == 5
    assert b[1]["provider"] == "unknown" and b[1]["model"] == "unknown" and b[1]["tokens_in"] == 0


def test_pricer_failure_records_zero_cost_and_keeps_evidence(tmp_path, caplog):
    provider = TracerProvider()
    provider.add_span_processor(WarrantSpanProcessor(pricer=lambda *a: 1 / 0))
    tracer = provider.get_tracer("t")
    with Warrant("lending", store=tmp_path / "r.db", agent=AgentInfo("a", "1"), flush_interval=0.02) as w:
        with caplog.at_level(logging.WARNING, logger="warrant.otel"):
            with w.decide("x.y", subject="1") as d:
                with tracer.start_as_current_span("chat", attributes={"gen_ai.request.model": "m"}):
                    pass
                d.act("go")
        (record,) = _records(w)
    assert len(record["evidence"]) == 1 and record["cost"]["amount"] == 0
    assert any("pricer failed" in r.message for r in caplog.records)
    provider.shutdown()


def test_classify():
    assert classify({"gen_ai.operation.name": "chat"}) == "model_call"
    assert classify({"gen_ai.response.model": "m"}) == "model_call"
    assert classify({"gen_ai.tool.name": "t"}) == "tool_call"
    assert classify({"gen_ai.operation.name": "execute_tool"}) == "tool_call"
    assert classify({"http.method": "GET"}) is None
    assert classify({}) is None


def test_processor_lifecycle():
    p = WarrantSpanProcessor()
    assert p.force_flush() is True
    p.shutdown()
