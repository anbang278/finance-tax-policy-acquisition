"""Default regressions are offline and keep update caches in isolated directories."""

import pytest


@pytest.fixture(autouse=True)
def isolated_update_check(tmp_path, monkeypatch):
    from ftr import update_check

    # Subprocess CLI tests inherit the opt-out; dedicated update tests enable explicitly.
    monkeypatch.setenv("FTR_UPDATE_CHECK__ENABLED", "false")
    monkeypatch.setattr(
        update_check, "cache_path", lambda *_: tmp_path / "update-cache/status.json"
    )

    def offline(*_args, **_kwargs):
        raise update_check._CheckError("OFFLINE_TEST")

    monkeypatch.setattr(update_check, "_compare", offline)
