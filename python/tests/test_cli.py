import json
from pathlib import Path

from warrant.cli import main

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "loan-approval.json"


def test_schema_command_prints_schema(capsys):
    assert main(["schema"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["title"] == "Warrant decision record"


def test_validate_valid_file(capsys):
    assert main(["validate", str(EXAMPLE)]) == 0
    assert "valid" in capsys.readouterr().out


def test_validate_invalid_file(tmp_path, capsys):
    record = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    record["origin"] = "guessed"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(record), encoding="utf-8")
    assert main(["validate", str(bad)]) == 1
    err = capsys.readouterr().err
    assert "INVALID" in err and "origin" in err


def test_validate_missing_file(capsys):
    assert main(["validate", str(Path("/nonexistent/record.json"))]) == 1
    assert "file not found" in capsys.readouterr().err


def test_validate_malformed_json(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert main(["validate", str(bad)]) == 1
    assert "invalid JSON" in capsys.readouterr().err


def test_no_command_prints_help(capsys):
    assert main([]) == 2
    assert "usage: warrant" in capsys.readouterr().out
