import pytest
from jobkit import prereqs


@pytest.fixture(autouse=True)
def no_real_tailscale(monkeypatch):
    """Tests never ask this machine's own Tailscale (it may be signed in): a test that needs one brings a fake."""
    monkeypatch.setattr(prereqs, "TAILSCALE_APP", "/nonexistent/Tailscale")
    monkeypatch.setattr(prereqs, "BREW_DIRS", [])
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
