import json
import subprocess
import sys
from pathlib import Path

import pytest

from warrant.questions import (
    ADDITIVE,
    BREAKING,
    PATCH,
    SEMANTIC,
    QuestionSetError,
    Registry,
    check_answers,
    compare,
    lint,
    load_question_set,
)

pytest.importorskip("yaml", reason="question set files are YAML")

GALLERY = Path(__file__).resolve().parents[2] / "examples" / "gallery" / "aml" / "question-sets"

BASE = """
id: aml.alert
version: "{version}"
title: Alert adjudication
owner: fiu-ops@bank.example
questions:
  structuring_pattern:
    primitive: noul
    instructions: Is there a pattern of structuring below the threshold?
  disposition:
    primitive: choice
    instructions: Close the alert or escalate it?
    criteria: [close, escalate]
"""


def _write(tmp_path, text, name):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _registry(tmp_path, *versions):
    for text, name in versions:
        _write(tmp_path, text, name)
    return Registry.load(tmp_path)


# --- reading -----------------------------------------------------------------


def test_the_gallery_set_loads_and_is_internally_consistent():
    registry = Registry.load(GALLERY)
    question_set = registry.get("aml.alert", "3.1.0")
    assert question_set.ref == "aml.alert@3.1.0"
    assert question_set.questions["disposition"].criteria == ("close", "escalate")
    assert question_set.questions["structuring_pattern"].primitive == "noul"
    assert set(question_set.questions) == {
        "profile_consistent", "structuring_pattern", "counterparty_risk",
        "explanation_on_file", "behaviour_change", "disposition",
    }


@pytest.mark.parametrize(
    "body, message",
    [
        ("id: Bad.ID\nversion: '1.0.0'\nquestions: {a: {primitive: noul, instructions: x}}", "'id' must look like"),
        ("id: a.b\nversion: '1.0'\nquestions: {a: {primitive: noul, instructions: x}}", "must be semantic"),
        ("id: a.b\nversion: '1.0.0'\nquestions: {}", "non-empty mapping"),
        ("id: a.b\nversion: '1.0.0'\nquestions: {A: {primitive: noul, instructions: x}}", "lower_snake_case"),
        ("id: a.b\nversion: '1.0.0'\nquestions: {a: {primitive: vector, instructions: x}}", "'primitive' must be one of"),
        ("id: a.b\nversion: '1.0.0'\nquestions: {a: {primitive: noul, instructions: ''}}", "non-empty string"),
        ("id: a.b\nversion: '1.0.0'\nquestions: {a: {primitive: choice, instructions: x}}", "at least two permitted"),
        ("id: a.b\nversion: '1.0.0'\nquestions: {a: {primitive: choice, instructions: x, criteria: [one]}}", "at least two permitted"),
        ("id: a.b\nversion: '1.0.0'\nquestions: {a: {primitive: choice, instructions: x, criteria: [a, a]}}", "duplicate"),
        ("id: a.b\nversion: '1.0.0'\nquestions: {a: {primitive: noul, instructions: x, criteria: [a, b]}}", "takes no 'criteria'"),
        ("- not a mapping", "top level must be a mapping"),
    ],
)
def test_malformed_sets_are_refused_with_the_reason(tmp_path, body, message):
    with pytest.raises(QuestionSetError) as exc:
        load_question_set(_write(tmp_path, body, "s.yaml"))
    assert message in str(exc.value)


def test_two_files_claiming_the_same_version_is_refused(tmp_path):
    with pytest.raises(QuestionSetError) as exc:
        _registry(tmp_path, (BASE.format(version="1.0.0"), "a.yaml"), (BASE.format(version="1.0.0"), "b.yaml"))
    assert "already defined" in str(exc.value)


def test_a_missing_registry_and_an_empty_one_are_distinguished(tmp_path):
    with pytest.raises(FileNotFoundError):
        Registry.load(tmp_path / "absent")
    (tmp_path / "empty").mkdir()
    with pytest.raises(QuestionSetError) as exc:
        Registry.load(tmp_path / "empty")
    assert "no question set files" in str(exc.value)


# --- pinning -----------------------------------------------------------------


def test_an_unknown_set_or_version_names_what_the_registry_does_hold(tmp_path):
    registry = _registry(tmp_path, (BASE.format(version="1.0.0"), "a.yaml"))
    with pytest.raises(QuestionSetError) as exc:
        registry.get("nope", "1.0.0")
    assert "the registry holds: aml.alert" in str(exc.value)
    with pytest.raises(QuestionSetError) as exc:
        registry.get("aml.alert", "9.9.9")
    assert "it holds: 1.0.0" in str(exc.value)


def test_versions_order_numerically_not_lexically(tmp_path):
    registry = _registry(
        tmp_path,
        (BASE.format(version="1.9.0"), "a.yaml"),
        (BASE.format(version="1.10.0"), "b.yaml"),
        (BASE.format(version="1.2.0"), "c.yaml"),
    )
    assert [s.version for s in registry.history("aml.alert")] == ["1.2.0", "1.9.0", "1.10.0"]
    assert registry.latest("aml.alert").version == "1.10.0"


# --- classifying change ------------------------------------------------------


def _pair(tmp_path, after_body, after_version="1.0.1"):
    before = load_question_set(_write(tmp_path, BASE.format(version="1.0.0"), "before.yaml"))
    after = load_question_set(_write(tmp_path, after_body.format(version=after_version), "after.yaml"))
    return compare(before, after)


def test_removing_a_question_is_breaking(tmp_path):
    body = BASE.replace(
        "  structuring_pattern:\n    primitive: noul\n    instructions: Is there a pattern of structuring below the threshold?\n",
        "",
    )
    c = _pair(tmp_path, body)
    assert c.severity == BREAKING and c.required_bump == "major"
    assert any("question removed" in str(x) for x in c.changes)
    assert not c.sufficient  # a patch bump cannot carry it


def test_narrowing_permitted_answers_is_breaking(tmp_path):
    body = BASE.replace("criteria: [close, escalate]", "criteria: [close, escalate_l1]")
    c = _pair(tmp_path, body)
    assert c.severity == BREAKING
    assert any("permitted answers removed: escalate" in str(x) for x in c.changes)


def test_changing_a_primitive_is_breaking(tmp_path):
    body = BASE.replace(
        "  structuring_pattern:\n    primitive: noul",
        "  structuring_pattern:\n    primitive: choice\n    criteria: [yes_, no_]",
    )
    c = _pair(tmp_path, body)
    assert c.severity == BREAKING
    assert any("primitive changed" in str(x) for x in c.changes)


def test_editing_instructions_is_semantic_and_needs_a_minor_bump(tmp_path):
    """The change this module exists for: nothing breaks, but answers stop being comparable."""
    body = BASE.replace("Close the alert or escalate it?", "Close the alert, or escalate it for review?")
    c = _pair(tmp_path, body)
    assert c.severity == SEMANTIC and c.required_bump == "minor"
    assert not c.sufficient  # a patch bump is not enough
    assert any("no longer comparable" in str(x) for x in c.changes)
    assert _pair(tmp_path, body, after_version="1.1.0").sufficient


def test_adding_a_question_or_an_answer_is_additive(tmp_path):
    added_question = BASE + "  pep_exposure:\n    primitive: noul\n    instructions: Is a politically exposed person involved?\n"
    c = _pair(tmp_path, added_question, after_version="1.1.0")
    assert c.severity == ADDITIVE and c.sufficient
    widened = BASE.replace("criteria: [close, escalate]", "criteria: [close, escalate, defer]")
    assert _pair(tmp_path, widened, after_version="1.1.0").severity == ADDITIVE


def test_an_owner_change_alone_is_a_patch(tmp_path):
    body = BASE.replace("owner: fiu-ops@bank.example", "owner: mlro@bank.example")
    c = _pair(tmp_path, body)
    assert c.severity == PATCH and c.sufficient


def test_no_change_at_all_is_reported_as_such(tmp_path):
    c = _pair(tmp_path, BASE, after_version="1.0.1")
    assert c.changes == [] and c.sufficient
    assert "no difference" in c.summary()


def test_comparing_different_sets_is_refused(tmp_path):
    a = load_question_set(_write(tmp_path, BASE.format(version="1.0.0"), "a.yaml"))
    b = load_question_set(_write(tmp_path, BASE.replace("id: aml.alert", "id: kyc.review").format(version="1.0.0"), "b.yaml"))
    with pytest.raises(QuestionSetError):
        compare(a, b)


# --- the gate ----------------------------------------------------------------


def test_lint_passes_a_registry_whose_bumps_are_big_enough(tmp_path):
    widened = BASE.replace("criteria: [close, escalate]", "criteria: [close, escalate, defer]")
    report = lint(_registry(tmp_path, (BASE.format(version="1.0.0"), "a.yaml"), (widened.format(version="1.1.0"), "b.yaml")))
    assert report.ok and "no problems" in report.summary()


def test_lint_fails_a_question_removed_in_a_patch_release(tmp_path):
    """The failure this exists to stop."""
    trimmed = BASE.replace(
        "  structuring_pattern:\n    primitive: noul\n    instructions: Is there a pattern of structuring below the threshold?\n",
        "",
    )
    report = lint(_registry(tmp_path, (BASE.format(version="1.0.0"), "a.yaml"), (trimmed.format(version="1.0.1"), "b.yaml")))
    assert not report.ok
    assert "breaking change carried by a patch bump" in report.problems[0]
    assert "needs major" in report.problems[0]


def test_lint_walks_every_consecutive_pair(tmp_path):
    edited = BASE.replace("Close the alert or escalate it?", "Different wording entirely?")
    report = lint(_registry(
        tmp_path,
        (BASE.format(version="1.0.0"), "a.yaml"),
        (BASE.format(version="1.0.1"), "b.yaml"),
        (edited.format(version="1.0.2"), "c.yaml"),
    ))
    assert len(report.comparisons) == 2
    assert len(report.problems) == 1 and "1.0.1 -> aml.alert@1.0.2" in report.problems[0]


def test_the_gallery_registry_lints_clean():
    assert lint(Registry.load(GALLERY)).ok


# --- answers against the questions asked -------------------------------------


def test_answers_are_checked_against_the_questions(tmp_path):
    question_set = load_question_set(_write(tmp_path, BASE.format(version="1.0.0"), "a.yaml"))
    assert check_answers(question_set, {"structuring_pattern": False, "disposition": "close"}) == []

    problems = check_answers(question_set, {"structuring_pattern": False, "disposition": "shred"})
    assert problems == ["disposition: 'shred' is not one of the permitted answers (close, escalate)"]

    assert "not answered" in check_answers(question_set, {"disposition": "close"})[0]
    assert "not in aml.alert@1.0.0" in check_answers(
        question_set, {"structuring_pattern": False, "disposition": "close", "extra": 1}
    )[0]
    assert "must answer true or false" in check_answers(
        question_set, {"structuring_pattern": "maybe", "disposition": "close"}
    )[0]


# --- CLI ---------------------------------------------------------------------


def _cli(*args):
    return subprocess.run([sys.executable, "-m", "warrant.cli", *args], capture_output=True, text=True)


def test_cli_lint_exits_non_zero_on_an_insufficient_bump(tmp_path):
    trimmed = BASE.replace(
        "  structuring_pattern:\n    primitive: noul\n    instructions: Is there a pattern of structuring below the threshold?\n",
        "",
    )
    _write(tmp_path, BASE.format(version="1.0.0"), "a.yaml")
    _write(tmp_path, trimmed.format(version="1.0.1"), "b.yaml")

    result = _cli("questions", "lint", str(tmp_path))
    assert result.returncode == 1
    assert "breaking" in result.stdout

    as_json = _cli("questions", "lint", str(tmp_path), "--json")
    assert json.loads(as_json.stdout)["ok"] is False


def test_cli_lint_passes_the_gallery():
    result = _cli("questions", "lint", str(GALLERY))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "aml.alert: 3.1.0" in result.stdout


def test_cli_diff_accepts_a_bare_version_for_the_second_side(tmp_path):
    widened = BASE.replace("criteria: [close, escalate]", "criteria: [close, escalate, defer]")
    _write(tmp_path, BASE.format(version="1.0.0"), "a.yaml")
    _write(tmp_path, widened.format(version="1.1.0"), "b.yaml")
    result = _cli("questions", "diff", str(tmp_path), "aml.alert@1.0.0", "1.1.0")
    assert result.returncode == 0
    assert "permitted answers added: defer" in result.stdout


def test_cli_diff_without_a_set_id_says_so(tmp_path):
    _write(tmp_path, BASE.format(version="1.0.0"), "a.yaml")
    result = _cli("questions", "diff", str(tmp_path), "1.0.0", "1.0.0")
    assert result.returncode == 1 and "needs a set id" in result.stderr


def test_cli_show_prints_the_form_that_lands_in_a_pack():
    result = _cli("questions", "show", str(GALLERY), "aml.alert")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["version"] == "3.1.0"
    assert payload["questions"]["disposition"]["criteria"] == ["close", "escalate"]
