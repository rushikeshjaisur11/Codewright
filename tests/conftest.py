import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    """Keep recent-repo state out of the real home folder."""
    monkeypatch.setenv("CODEWRIGHT_HOME", str(tmp_path_factory.mktemp("cwhome")))
    monkeypatch.setenv("CODEWRIGHT_CACHE_DIR", str(tmp_path_factory.mktemp("cwcache")))
