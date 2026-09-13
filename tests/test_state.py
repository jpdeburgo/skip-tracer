import skip_tracer.state as state


def test_load_seen_parcels_missing_file_returns_empty_set(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_FILE", tmp_path / "state.json")
    assert state.load_seen_parcels(env={}) == set()


def test_save_then_load_round_trips_local_file(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_FILE", tmp_path / "state.json")
    state.save_seen_parcels({"161002621465", "999"}, env={})
    assert state.load_seen_parcels(env={}) == {"161002621465", "999"}


def test_github_config_requires_both_token_and_repo():
    import pytest

    with pytest.raises(RuntimeError):
        state._github_state_config({"ST_GITHUB_TOKEN": "abc"})


def test_github_config_none_when_neither_set():
    assert state._github_state_config({}) is None


def test_github_config_returns_defaults_branch():
    config = state._github_state_config(
        {"ST_GITHUB_TOKEN": "abc", "ST_GITHUB_REPO": "me/repo"}
    )
    assert config == ("abc", "me/repo", "main")
