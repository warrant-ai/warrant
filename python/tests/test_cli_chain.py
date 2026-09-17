import json

from warrant import AgentInfo, Warrant
from warrant.cli import main


def _populate(path):
    with Warrant("lending", store=path, agent=AgentInfo("a", "1"), flush_interval=0.02) as w:
        for i in range(3):
            with w.decide("credit.approve", subject=f"LN-{i}") as d:
                d.act("approve")
        w.outcome(subject="LN-0", label="performing")
        assert w.flush()


def test_export_then_verify_roundtrip(tmp_path, capsys):
    db = tmp_path / "r.db"
    _populate(db)
    out = tmp_path / "export.jsonl"
    assert main(["export", "--store", str(db), "-o", str(out)]) == 0
    assert "exported 4 record(s)" in capsys.readouterr().out
    assert main(["verify", str(out)]) == 0
    assert "local/lending: 4 record(s), chain OK" in capsys.readouterr().out


def test_export_to_stdout_filtered_by_stream(tmp_path, capsys):
    db = tmp_path / "r.db"
    _populate(db)
    assert main(["export", "--store", str(db), "--stream", "lending"]) == 0
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l]
    assert len(lines) == 4 and all(l["stream"] == "lending" for l in lines)
    assert main(["export", "--store", str(db), "--stream", "nothing"]) == 0
    assert "no records" in capsys.readouterr().err


def test_verify_detects_tampered_export(tmp_path, capsys):
    db = tmp_path / "r.db"
    _populate(db)
    out = tmp_path / "export.jsonl"
    assert main(["export", "--store", str(db), "-o", str(out)]) == 0
    lines = out.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[1])
    rec["decision"]["action"] = "decline"
    lines[1] = json.dumps(rec)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert main(["verify", str(out)]) == 1
    err = capsys.readouterr().err
    assert "FAILED" in err and "hash mismatch" in err


def test_verify_bad_inputs(tmp_path, capsys):
    assert main(["verify", str(tmp_path / "missing.jsonl")]) == 1
    assert "file not found" in capsys.readouterr().err
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json\n", encoding="utf-8")
    assert main(["verify", str(bad)]) == 1
    assert "invalid JSONL" in capsys.readouterr().err
    assert main(["export", "--store", str(tmp_path / "missing.db")]) == 1
    assert "not found" in capsys.readouterr().err
