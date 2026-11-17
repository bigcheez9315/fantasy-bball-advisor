import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

PERSONAL_SETTINGS = (
    "PROTECT_OWNED_PCT", "PROTECTED_PLAYERS", "STREAMERS", "PUNT_CATS", "RETURNING",
)


@pytest.fixture(autouse=True)
def _no_personal_env(monkeypatch):
    """Tests must not depend on the developer's own .env roster settings."""
    from fantasy import config

    monkeypatch.setattr(config, "load_env", lambda *a, **k: {})
    for key in PERSONAL_SETTINGS:
        monkeypatch.delenv(key, raising=False)
