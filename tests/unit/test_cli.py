import io
import json

import pytest

from mendarr import cli

SRC = 'META = {"backend": "camoufox", "notes": "n"}\n\n\nasync def solve(page, ctx):\n    return None\n'


@pytest.fixture
def run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MENDARR_DATA_DIR", str(tmp_path / "data"))

    def go(*argv):
        capsys.readouterr()
        code = cli.main(list(argv))
        cap = capsys.readouterr()
        return code, cap.out, cap.err

    return go


@pytest.fixture
def src(tmp_path):
    p = tmp_path / "p.py"
    p.write_text(SRC)
    return str(p)


def test_import_list_show(run, src):
    code, out, _ = run("plugins", "import", src, "--scope", "example.org", "--activate")
    assert (code, out.strip()) == (0, "imported example.org@v1 (active)")
    _, out, _ = run("plugins", "list", "--json")
    assert {(r["scope"], r["version"]) for r in json.loads(out)} == {("_default", 1), ("example.org", 1)}
    assert run("plugins", "list")[1].startswith("scope\tversion")
    assert run("plugins", "show", "example.org")[1] == SRC


def test_stdin_import(run, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(SRC))
    code, out, _ = run("plugins", "import", "-", "--scope", "a.org")
    assert code == 0 and "a.org@v1 (candidate)" in out


def test_missing_scope_and_guard(run, src, tmp_path):
    assert run("plugins", "import", src)[0] == 1
    bad = tmp_path / "bad.py"
    bad.write_text("import os\n" + SRC)
    code, _, err = run("plugins", "import", str(bad), "--scope", "a.org")
    assert code == 1 and err.startswith("guard:")


def test_promote_rollback_retire(run, src):
    run("plugins", "import", src, "--scope", "a.org", "--activate")
    run("plugins", "import", src, "--scope", "a.org")
    assert run("plugins", "promote", "a.org", "2")[1].strip() == "active: a.org@v2"
    assert run("plugins", "rollback", "a.org")[1].strip() == "active: a.org@v1"
    assert run("plugins", "retire", "a.org")[1].strip() == "retired: a.org@v1"
    assert run("plugins", "retire", "a.org")[1].strip() == "nothing active for a.org"
    assert "no active plugin" in run("plugins", "rollback", "a.org")[2]


def test_export_import_roundtrip(run, src, tmp_path, monkeypatch):
    run("plugins", "import", src, "--scope", "a.org", "--activate")
    run("plugins", "import", src, "--scope", "a.org", "--activate")
    out_dir = tmp_path / "exp"
    code, out, _ = run("plugins", "export", str(out_dir))
    assert code == 0 and "exported 2 plugins" in out
    assert (out_dir / "a.org" / "v2.py").read_text() == SRC
    monkeypatch.setenv("MENDARR_DATA_DIR", str(tmp_path / "fresh"))
    assert run("plugins", "import", str(out_dir), "--activate")[0] == 0
    recs = {(r["scope"], r["version"]): r for r in json.loads(run("plugins", "list", "--json")[1])}
    assert recs[("a.org", 2)]["status"] == "active" and recs[("a.org", 2)]["parent"] == 1
    assert recs[("a.org", 1)]["status"] == "candidate"


def test_incidents_empty(run):
    code, out, _ = run("incidents", "list", "--json")
    assert (code, json.loads(out)) == (0, [])
