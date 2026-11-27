from app import __version__


def test_version_is_published(client):
    assert client.get("/api/config").json()["version"] == __version__
    assert client.get("/openapi.json").json()["info"]["version"] == __version__


def test_version_is_section_feature_fix_and_matches_VERSION():
    """2026-11-28: Section.Feature.Fix like SirisOS (scripts/bump-version.sh); app/__init__.py follows VERSION."""
    import re
    from pathlib import Path
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)
    assert (Path(__file__).resolve().parents[1] / "VERSION").read_text().strip() == __version__
