import json

import pytest

from flightdeck import submit


def _payload(tmp_path):
    p = tmp_path / "cc.json"
    p.write_text(json.dumps({
        "daily": [{"date": "2026-07-28", "totalTokens": 10, "totalCost": 1.0}],
        "totals": {"totalTokens": 10, "totalCost": 1.0},
    }))
    return p


def test_user_is_required_and_never_guessed(tmp_path):
    """viberank-cli defaults to git config user.name — a real name, not a
    handle. Publishing under the wrong identity cannot be undone."""
    with pytest.raises(SystemExit) as e:
        submit.run(_payload(tmp_path), user=None)
    assert "--user is required" in str(e.value)


def test_dry_run_sends_nothing(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(submit, "post", lambda *a, **k: called.append(a))
    s = submit.run(_payload(tmp_path), user="someone")
    assert s["sent"] is False and called == []
    assert s["tokens"] == 10


def test_confirmed_run_posts_once(tmp_path, monkeypatch):
    seen = {}
    def fake_post(payload, user, endpoint=submit.ENDPOINT):
        seen["user"] = user
        seen["tokens"] = payload["totals"]["totalTokens"]
        return {"success": True, "profileUrl": "https://viberank.app/profile/someone"}
    monkeypatch.setattr(submit, "post", fake_post)
    s = submit.run(_payload(tmp_path), user="someone", confirmed=True)
    assert s["sent"] is True
    assert seen == {"user": "someone", "tokens": 10}


def test_machine_id_is_a_stable_uuid():
    a, b = submit.machine_id(), submit.machine_id()
    assert a == b and len(a) == 36
