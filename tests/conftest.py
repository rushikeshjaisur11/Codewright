import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    """Keep recent-repo state out of the real home folder."""
    monkeypatch.setenv("CODEWRIGHT_HOME", str(tmp_path_factory.mktemp("cwhome")))
    monkeypatch.setenv("CODEWRIGHT_CACHE_DIR", str(tmp_path_factory.mktemp("cwcache")))
    # A model key on the developer's machine would turn "no model" tests into real model calls.
    for var in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "VERTEXAI_PROJECT",
        "GOOGLE_CLOUD_PROJECT",
        "CODEWRIGHT_AI_PROVIDER",
        "CODEWRIGHT_AI_KEY",
        "CODEWRIGHT_AI_MODEL",
        "CODEWRIGHT_AI_BASE_URL",
        "CODEWRIGHT_MODEL",
        "CODEWRIGHT_OFFLINE",
    ):
        monkeypatch.delenv(var, raising=False)
