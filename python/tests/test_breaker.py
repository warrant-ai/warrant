"""Circuit breakers: the portfolio properties a per-decision clause cannot see."""

from datetime import datetime, timedelta, timezone

import pytest

from warrant import SQLiteStore
from warrant.breaker import Breaker, BreakerError, BreakerRule, count_window, load_rules

pytest.importorskip("yaml")

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def _stamp(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _decision(i, *, route="auto", hours_ago=1.0, cls="aml.alert.disposition"):
    return {
        "record_id": f"01K5A3Q7Z2XV8M9N4B6C1D{i:04d}",
        "record_type": "decision",
        "tenant": "demo-bank",
        "stream": "aml",
        "timestamp": _stamp(hours_ago),
        "schema_version": "0",
        "origin": "live",
        "actor": {"name": "adjudicator", "version": "1.4.0"},
        "decision": {"class": cls, "action": "close", "subject": f"alert:{i}", "status": "acted", "route": route},
        "mandate": {"result": "allow" if route == "auto" else "escalate"},
    }


def _store(tmp_path, records, name="r.db"):
    s = SQLiteStore(tmp_path / name)
    s.write(records)
    return s


CEILING = BreakerRule("aml.alert.disposition", "auto_share", "24h", ceiling=0.70, min_decisions=10)


# --- reading rules -----------------------------------------------------------


def test_rules_load_from_a_list_or_a_breakers_mapping(tmp_path):
    body = """
breakers:
  - class: aml.alert.disposition
    metric: auto_share
    window: 24h
    ceiling: 0.70
    min_decisions: 100
"""
    (tmp_path / "b.yaml").write_text(body, encoding="utf-8")
    (rule,) = load_rules(tmp_path / "b.yaml")
    assert rule.metric == "auto_share" and rule.ceiling == 0.70 and rule.min_decisions == 100
    assert rule.window_delta == timedelta(hours=24)


@pytest.mark.parametrize(
    "body, message",
    [
        ("- {metric: auto_share, window: 24h, ceiling: 0.7}", "'class' must be"),
        ("- {class: a.b, metric: vibes, window: 24h, ceiling: 0.7}", "'metric' must be one of"),
        ("- {class: a.b, metric: auto_share, window: soon, ceiling: 0.7}", "'window' must look like"),
        ("- {class: a.b, metric: auto_share, window: 24h}", "exactly one of 'ceiling' or 'floor'"),
        ("- {class: a.b, metric: auto_share, window: 24h, ceiling: 0.7, floor: 0.2}", "exactly one of"),
        ("- {class: a.b, metric: auto_share, window: 24h, ceiling: 1.7}", "between 0 and 1"),
        ("- {class: a.b, metric: auto_share, window: 24h, ceiling: 0.7, min_decisions: 0}", "positive integer"),
        ("[]", "non-empty list"),
    ],
)
def test_malformed_rules_are_refused_at_load(tmp_path, body, message):
    (tmp_path / "b.yaml").write_text(body, encoding="utf-8")
    with pytest.raises(BreakerError) as exc:
        load_rules(tmp_path / "b.yaml")
    assert message in str(exc.value)


def test_a_breaker_may_never_force_a_decision_through(tmp_path):
    """The rule that has no configuration: a broken breaker must fail towards a person."""
    (tmp_path / "b.yaml").write_text(
        "- {class: a.b, metric: auto_share, window: 24h, floor: 0.2, action: allow}", encoding="utf-8"
    )
    with pytest.raises(BreakerError) as exc:
        load_rules(tmp_path / "b.yaml")
    assert "may never force one through" in str(exc.value)


# --- counting ----------------------------------------------------------------


def test_the_window_counts_only_this_class_inside_the_window(tmp_path):
    store = _store(tmp_path, [
        *[_decision(i, route="auto", hours_ago=1) for i in range(8)],
        *[_decision(100 + i, route="human", hours_ago=1) for i in range(2)],
        _decision(200, route="auto", hours_ago=48),                        # outside the window
        _decision(300, route="auto", hours_ago=1, cls="kyc.review"),       # a different class
    ])
    window = count_window(store, "aml.alert.disposition", _stamp(24), stream="aml")
    store.close()
    assert (window.decisions, window.auto, window.escalated) == (10, 8, 2)
    assert window.value("auto_share") == pytest.approx(0.8)
    assert window.value("escalation_share") == pytest.approx(0.2)


def test_routing_is_inferred_for_records_written_before_routes_existed(tmp_path):
    older = _decision(1)
    del older["decision"]["route"]           # allow + acted reads as auto
    referred = _decision(2, route="human")
    del referred["decision"]["route"]        # escalate reads as human
    store = _store(tmp_path, [older, referred])
    window = count_window(store, "aml.alert.disposition", _stamp(24), stream="aml")
    store.close()
    assert (window.auto, window.escalated) == (1, 1)


# --- tripping ----------------------------------------------------------------


def test_a_ceiling_trips_when_auto_share_climbs(tmp_path):
    store = _store(tmp_path, [
        *[_decision(i, route="auto") for i in range(9)],
        _decision(100, route="human"),
    ])
    trip = Breaker([CEILING], store, stream="aml", cache_seconds=0).check(
        "aml.alert.disposition", at=NOW.isoformat()
    )
    store.close()
    assert trip is not None
    assert trip.value == pytest.approx(0.9) and trip.decisions == 10
    assert "auto_share 0.900 is above 0.7 over 24h" in trip.reason


def test_a_floor_catches_escalations_collapsing(tmp_path):
    """The same failure seen from the other side: the queue that should catch hard cases goes quiet."""
    rule = BreakerRule("aml.alert.disposition", "escalation_share", "24h", floor=0.10, min_decisions=10)
    store = _store(tmp_path, [
        *[_decision(i, route="auto") for i in range(19)],
        _decision(100, route="human"),
    ])
    trip = Breaker([rule], store, stream="aml", cache_seconds=0).check("aml.alert.disposition", at=NOW.isoformat())
    store.close()
    assert trip is not None and "is below 0.1" in trip.reason


def test_a_healthy_population_does_not_trip(tmp_path):
    store = _store(tmp_path, [
        *[_decision(i, route="auto") for i in range(6)],
        *[_decision(100 + i, route="human") for i in range(6)],
    ])
    assert Breaker([CEILING], store, stream="aml", cache_seconds=0).check(
        "aml.alert.disposition", at=NOW.isoformat()
    ) is None
    store.close()


def test_too_few_decisions_never_trips(tmp_path):
    """A breaker that fires on the third decision of the morning is noise, and noise gets ignored."""
    store = _store(tmp_path, [_decision(i, route="auto") for i in range(9)])
    assert Breaker([CEILING], store, stream="aml", cache_seconds=0).check(
        "aml.alert.disposition", at=NOW.isoformat()
    ) is None
    store.close()


def test_a_class_with_no_rule_is_never_checked(tmp_path):
    store = _store(tmp_path, [_decision(i, route="auto") for i in range(20)])
    assert Breaker([CEILING], store, stream="aml", cache_seconds=0).check("kyc.review", at=NOW.isoformat()) is None
    store.close()


def test_counts_are_cached_so_a_breaker_is_not_a_scan_per_decision(tmp_path):
    class _Counting:
        def __init__(self, inner):
            self.inner, self.scans = inner, 0

        def iter_records(self, stream=None):
            self.scans += 1
            return self.inner.iter_records(stream)

    inner = _store(tmp_path, [_decision(i, route="auto") for i in range(20)])
    counting = _Counting(inner)
    breaker = Breaker([CEILING], counting, stream="aml", cache_seconds=300)
    for _ in range(5):
        breaker.check("aml.alert.disposition", at=NOW.isoformat())
    inner.close()
    assert counting.scans == 1


# --- through the adapter -----------------------------------------------------


def test_the_breaker_takes_a_decision_away_from_the_machine(tmp_path):
    """The meaningful case: the clause said allow, and the population took it back."""
    from warrant import AgentInfo, Warrant
    from warrant.adapters.base import DecisionModel, ModelAnswer, ModelResult
    from warrant.adapters.model import DecisionAdapter

    class _Model(DecisionModel):
        provider, endpoint, region = "fake", "https://fake.invalid", "global"

        def evaluate(self, state, questions):
            return ModelResult(
                answers={"disposition": ModelAnswer("disposition", "close", 0.99)},
                model="fake-1",
            )

    policies = tmp_path / "policies"
    policies.mkdir()
    (policies / "AML-01.yaml").write_text(
        "policy_id: AML-01\nversion: '2026.1'\nclasses: [aml.alert.disposition]\n"
        "default: escalate\nclauses:\n"
        "  - {id: '1', title: Auto-close at 0.90, when: 'double(confidence) >= 0.90', result: allow}\n",
        encoding="utf-8",
    )

    db = tmp_path / "live.db"
    seed = SQLiteStore(db)
    seed.write([_decision(i, route="auto") for i in range(20)])  # auto-share already at 1.0
    seed.close()

    def run(with_breaker):
        w = Warrant("aml", tenant="demo-bank", store=db, agent=AgentInfo("adjudicator", "1.4.0"),
                    policy_bundle=policies, flush_interval=0.02)
        try:
            read_only = SQLiteStore(db, read_only=True)
            breaker = Breaker([CEILING], read_only, stream="aml", cache_seconds=0) if with_breaker else None
            adapter = DecisionAdapter(w, _Model(), breaker=breaker)
            result = adapter.decide(
                decision_class="aml.alert.disposition",
                subject=f"alert:{'broken' if with_breaker else 'clear'}",
                state={"segment": "retail"}, questions={"disposition": object()},
            )
            assert w.flush(timeout=5)
            read_only.close()
            written = [
                r for r in SQLiteStore(db, read_only=True).iter_records("aml")
                if r["record_type"] == "decision" and r["decision"]["subject"] == result_subject(with_breaker)
            ][0]
            return result, written
        finally:
            w.close()

    def result_subject(with_breaker):
        return f"alert:{'broken' if with_breaker else 'clear'}"

    # without the breaker the clause allows it outright
    clear, clear_record = run(False)
    assert clear.route == "auto"
    assert clear_record["mandate"]["result"] == "allow"

    # with it, the same decision is taken away from the machine
    broken, broken_record = run(True)
    assert broken.route == "human"
    assert broken_record["decision"]["route"] == "human"
    assert broken_record["mandate"]["result"] == "escalate"
    assert broken_record["mandate"]["flagged"] is True
    assert "breaker: auto_share" in broken_record["mandate"]["reason"]
    assert broken_record["human"]["required"] is True
    # the clause that allowed it is still named, so the record says what happened and why
    assert broken_record["mandate"]["policy_id"] == "AML-01"


def test_a_breaker_never_widens_a_deny_or_an_escalate(tmp_path):
    """A tripped breaker must not turn a human decision into an automatic one."""
    from warrant import AgentInfo, Warrant
    from warrant.adapters.base import DecisionModel, ModelAnswer, ModelResult
    from warrant.adapters.model import DecisionAdapter

    class _Model(DecisionModel):
        provider, endpoint, region = "fake", "https://fake.invalid", "global"

        def evaluate(self, state, questions):
            return ModelResult(
                answers={"disposition": ModelAnswer("disposition", "close", 0.40)},
                model="fake-1",
            )

    policies = tmp_path / "policies"
    policies.mkdir()
    (policies / "AML-01.yaml").write_text(
        "policy_id: AML-01\nversion: '2026.1'\nclasses: [aml.alert.disposition]\n"
        "default: escalate\nclauses:\n"
        "  - {id: '1', title: Auto-close at 0.90, when: 'double(confidence) >= 0.90', result: allow}\n",
        encoding="utf-8",
    )
    db = tmp_path / "low.db"
    seed = SQLiteStore(db)
    seed.write([_decision(i, route="human") for i in range(20)])  # auto-share 0, ceiling not breached
    seed.close()

    w = Warrant("aml", tenant="demo-bank", store=db, agent=AgentInfo("adjudicator", "1.4.0"),
                policy_bundle=policies, flush_interval=0.02)
    try:
        read_only = SQLiteStore(db, read_only=True)
        adapter = DecisionAdapter(
            w, _Model(), breaker=Breaker([CEILING], read_only, stream="aml", cache_seconds=0)
        )
        result = adapter.decide(
            decision_class="aml.alert.disposition", subject="alert:low",
            state={"segment": "retail"}, questions={"disposition": object()},
        )
        read_only.close()
    finally:
        w.close()
    # 0.40 is below the clause floor, so it escalates; the breaker leaves it escalated
    assert result.route == "human" and result.verdict.result == "escalate"
