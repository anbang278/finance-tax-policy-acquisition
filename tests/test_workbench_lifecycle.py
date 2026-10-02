import json
import socket

import pytest

from ftr.repository import Repository
from ftr.workbench import manage


def test_missing_db_does_not_initialize(tmp_path):
    root = tmp_path / "absent"
    with pytest.raises(ValueError, match="尚无资料库"):
        manage(root, "start")
    assert not root.exists()


def test_real_start_reuse_conflict_stop_readonly(tmp_path, monkeypatch):
    repo = Repository(tmp_path)
    repo.close()
    before = (tmp_path / "database.sqlite3").read_bytes()
    monkeypatch.setattr("ftr.workbench.webbrowser.open", lambda _: False)
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 8765))
        occupied.listen()
        try:
            first = manage(tmp_path, "start", open_browser=True)
            assert first["state"] == "RUNNING"
            assert first["url"] != "http://127.0.0.1:8765"
            assert first["browser_opened"] is False
            again = manage(tmp_path, "start")
            assert again["pid"] == first["pid"]
            assert "token" not in again
            # Wrong identity is never allowed to stop this child or an unrelated PID.
            state_path = tmp_path / ".workbench.json"
            original = state_path.read_text()
            state = json.loads(original)
            state["pid"] += 1
            state_path.write_text(json.dumps(state))
            assert manage(tmp_path, "status")["state"] == "STOPPED"
            state_path.write_text(original)
            assert manage(tmp_path, "status")["state"] == "RUNNING"
        finally:
            assert manage(tmp_path, "stop")["state"] == "STOPPED"
    assert (tmp_path / "database.sqlite3").read_bytes() == before
    assert manage(tmp_path, "stop")["state"] == "STOPPED"
