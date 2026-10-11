from decimal import Decimal

from app import __version__


def test_version_is_published(client):
    assert client.get("/api/config").json()["version"] == __version__
    assert client.get("/openapi.json").json()["info"]["version"] == __version__


def test_bump_rules(tmp_path, monkeypatch):
    import tools.bump_version as bv
    init, log = tmp_path / "__init__.py", tmp_path / "CHANGELOG.md"
    init.write_text('__version__ = "1.0"\n')
    log.write_text("# Changelog\n\n## 1.0 – 2026-10-11\n\n- first\n")
    monkeypatch.setattr(bv, "INIT", init)
    monkeypatch.setattr(bv, "CHANGELOG", log)
    assert bv.bump("small", "tweak", "2026-10-12") == "1.01"
    assert bv.bump("small", "", "2026-10-12") == "1.02"
    assert bv.bump("big", "feature", "2026-10-13") == "1.12"
    assert bv.fmt(Decimal("1.09") + Decimal("0.01")) == "1.1"
    text = log.read_text()
    assert text.index("## 1.12") < text.index("## 1.02") < text.index("## 1.0 ")
