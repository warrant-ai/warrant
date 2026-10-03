"""Circuit breakers: portfolio properties a per-decision policy clause cannot see.

A policy clause answers a question about one decision, from the inputs of that decision. "No more
than seventy per cent of alerts may be auto-closed" is a different kind of statement — it is about
a population over a window, and the CEL engine has no history handle by design. So it lives here,
outside the policy, and is evaluated after the clause has already spoken.

Two failures this is for, and they look nothing alike:

* **A decider that becomes confident about everything.** Auto-share climbs, nothing in any single
  decision looks wrong, and the first sign is a supervisor asking why volumes fell.
* **Escalations collapsing.** The queue that should be catching the hard cases goes quiet, which is
  the same event seen from the other side.

One rule holds absolutely: **a breaker may force a decision to a human and may never force one the
other way.** A breaker that is itself broken must fail towards a person, so ``allow`` is not a
permitted action and there is no configuration that makes it one.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from warrant.store import resolve_tenant

log = logging.getLogger("warrant.breaker")

METRICS = ("auto_share", "escalation_share")
#: A breaker may only ever send a decision to a person. See the module docstring.
ACTIONS = ("escalate",)
_WINDOW = re.compile(r"^(\d+)([hd])$")


class BreakerError(ValueError):
    """A breaker rule cannot be read or is not well formed. Raised at load, never at decision time."""


@dataclass(frozen=True)
class BreakerRule:
    """One ceiling or floor on a decision class, over a window."""

    decision_class: str
    metric: str
    window: str
    ceiling: Optional[float] = None
    floor: Optional[float] = None
    min_decisions: int = 50
    action: str = "escalate"

    @property
    def window_delta(self) -> timedelta:
        amount, unit = _WINDOW.match(self.window).groups()
        return timedelta(hours=int(amount)) if unit == "h" else timedelta(days=int(amount))

    def breached(self, value: float) -> bool:
        if self.ceiling is not None and value > self.ceiling:
            return True
        return self.floor is not None and value < self.floor

    def describe(self, value: float) -> str:
        bound = f"above {self.ceiling:g}" if self.ceiling is not None else f"below {self.floor:g}"
        return f"{self.metric} {value:.3f} is {bound} over {self.window}"


@dataclass(frozen=True)
class Trip:
    """A breached rule, with the numbers that breached it."""

    rule: BreakerRule
    value: float
    decisions: int

    @property
    def reason(self) -> str:
        return f"breaker: {self.rule.describe(self.value)} ({self.decisions} decisions)"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "class": self.rule.decision_class,
            "metric": self.rule.metric,
            "window": self.rule.window,
            "ceiling": self.rule.ceiling,
            "floor": self.rule.floor,
            "value": self.value,
            "decisions": self.decisions,
            "action": self.rule.action,
            "reason": self.reason,
        }


def load_rules(path: Union[str, Path]) -> List[BreakerRule]:
    """Read breaker rules from a YAML or JSON file: a list, or a mapping with ``breakers:``."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"breaker rules not found: {path}")
    text = path.read_text(encoding="utf-8")
    try:
        if path.suffix.lower() == ".json":
            raw = json.loads(text)
        else:
            import yaml

            raw = yaml.safe_load(text)
    except ImportError as exc:
        raise BreakerError(
            f'{path.name}: reading YAML needs the pyyaml package: pip install "warrantai[policy]"'
        ) from exc
    except Exception as exc:
        raise BreakerError(f"{path.name}: cannot parse: {exc}") from exc

    if isinstance(raw, dict):
        raw = raw.get("breakers")
    if not isinstance(raw, list) or not raw:
        raise BreakerError(f"{path.name}: expected a non-empty list of breaker rules")
    return [_rule(path.name, i, body) for i, body in enumerate(raw, start=1)]


def _rule(file_name: str, index: int, body: Any) -> BreakerRule:
    where = f"{file_name}: rule {index}"
    if not isinstance(body, dict):
        raise BreakerError(f"{where}: must be a mapping")
    decision_class = str(body.get("class", "")).strip()
    if not decision_class:
        raise BreakerError(f"{where}: 'class' must be a non-empty decision class")
    metric = str(body.get("metric", "")).strip()
    if metric not in METRICS:
        raise BreakerError(f"{where}: 'metric' must be one of {', '.join(METRICS)}, got {metric!r}")
    window = str(body.get("window", "")).strip()
    if not _WINDOW.match(window):
        raise BreakerError(f"{where}: 'window' must look like 24h or 7d, got {window!r}")

    ceiling, floor = body.get("ceiling"), body.get("floor")
    for name, value in (("ceiling", ceiling), ("floor", floor)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise BreakerError(f"{where}: '{name}' must be a number")
        if value is not None and not 0.0 <= float(value) <= 1.0:
            raise BreakerError(f"{where}: '{name}' is a share and must be between 0 and 1")
    if (ceiling is None) == (floor is None):
        raise BreakerError(f"{where}: give exactly one of 'ceiling' or 'floor'")

    action = str(body.get("action", "escalate")).strip()
    if action not in ACTIONS:
        raise BreakerError(
            f"{where}: 'action' must be 'escalate'. A breaker may send a decision to a person and "
            "may never force one through, so that a broken breaker fails towards a human"
        )
    min_decisions = body.get("min_decisions", 50)
    if isinstance(min_decisions, bool) or not isinstance(min_decisions, int) or min_decisions < 1:
        raise BreakerError(f"{where}: 'min_decisions' must be a positive integer")

    return BreakerRule(
        decision_class=decision_class,
        metric=metric,
        window=window,
        ceiling=None if ceiling is None else float(ceiling),
        floor=None if floor is None else float(floor),
        min_decisions=min_decisions,
        action=action,
    )


@dataclass
class Window:
    """What the store held for one class over one window."""

    decisions: int = 0
    auto: int = 0
    escalated: int = 0

    def value(self, metric: str) -> float:
        if not self.decisions:
            return 0.0
        return (self.auto if metric == "auto_share" else self.escalated) / self.decisions


class Breaker:
    """Evaluates breaker rules against what a store actually holds.

    Counts are read per call. A deployment doing real volume should pass a ``cache_seconds`` so a
    breaker does not scan the window on every decision; the default of 60 seconds is the balance
    between a breaker that reacts and one that is free.
    """

    def __init__(
        self,
        rules: Sequence[BreakerRule],
        store: Any,
        *,
        stream: Optional[str] = None,
        tenant: Optional[str] = None,
        cache_seconds: float = 60.0,
    ) -> None:
        self._rules: Dict[str, List[BreakerRule]] = {}
        for rule in rules:
            self._rules.setdefault(rule.decision_class, []).append(rule)
        self._store = store
        self._stream = stream
        self._tenant = tenant
        self._cache_seconds = cache_seconds
        self._cache: Dict[str, Any] = {}

    @classmethod
    def load(cls, path: Union[str, Path], store: Any, **kwargs: Any) -> "Breaker":
        return cls(load_rules(path), store, **kwargs)

    def check(self, decision_class: str, *, at: Optional[str] = None) -> Optional[Trip]:
        """The first rule this class breaches, or ``None``.

        ``None`` also covers "not enough decisions yet": a breaker that trips on the third decision
        of the morning is noise, and noise gets switched off.
        """
        rules = self._rules.get(decision_class)
        if not rules:
            return None
        now = _parse(at) if at else datetime.now(timezone.utc)
        for rule in rules:
            window = self._window(decision_class, rule, now)
            if window.decisions < rule.min_decisions:
                continue
            value = window.value(rule.metric)
            if rule.breached(value):
                trip = Trip(rule=rule, value=value, decisions=window.decisions)
                log.error("warrant breaker tripped: %s", trip.reason)
                return trip
        return None

    def _window(self, decision_class: str, rule: BreakerRule, now: datetime) -> Window:
        key = f"{decision_class}|{rule.metric}|{rule.window}"
        cached = self._cache.get(key)
        if cached and (now - cached[0]).total_seconds() < self._cache_seconds:
            return cached[1]
        since = (now - rule.window_delta).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        window = count_window(self._store, decision_class, since, stream=self._stream, tenant=self._tenant)
        self._cache[key] = (now, window)
        return window


def count_window(store: Any, decision_class: str, since: str, *, stream: Optional[str] = None, tenant: Optional[str] = None) -> Window:
    """Count decisions of one class written since ``since``, by how they were routed.

    Routing is read from ``decision.route`` where a record carries it and inferred from the mandate
    otherwise, so a breaker works on records written before routes existed.
    """
    window = Window()
    for record in store.iter_records(stream, resolve_tenant(store, stream, tenant)):
        if record.get("record_type") != "decision":
            continue
        decision = record.get("decision") or {}
        if decision.get("class") != decision_class or record.get("timestamp", "") < since:
            continue
        window.decisions += 1
        route = decision.get("route")
        if route is None:
            result = (record.get("mandate") or {}).get("result")
            route = "auto" if result == "allow" and decision.get("status") == "acted" else "human"
        if route == "auto":
            window.auto += 1
        elif route in ("human", "model"):
            window.escalated += 1
    return window


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
