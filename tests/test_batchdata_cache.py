import skip_tracer.batchdata_cache as batchdata_cache


def test_load_batchdata_cache_missing_file_returns_empty_dict(tmp_path, monkeypatch):
    monkeypatch.setenv("BATCHDATA_CACHE_PATH", str(tmp_path / "batchdata_cache.json"))
    assert batchdata_cache.load_batchdata_cache() == {}


def test_save_then_load_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("BATCHDATA_CACHE_PATH", str(tmp_path / "batchdata_cache.json"))

    batchdata_cache.save_batchdata_cache({"1": {"skip_trace": {"a": 1}}})

    assert batchdata_cache.load_batchdata_cache() == {"1": {"skip_trace": {"a": 1}}}


def test_cached_call_with_none_cache_always_calls():
    calls = []
    result = batchdata_cache.cached_call(None, "1", "skip_trace", lambda: calls.append(1) or "fresh")
    assert result == "fresh"
    assert calls == [1]


def test_cached_call_stores_and_reuses_result():
    cache = {}
    calls = []

    def make_call():
        calls.append(1)
        return "result"

    first = batchdata_cache.cached_call(cache, "1", "skip_trace", make_call)
    second = batchdata_cache.cached_call(cache, "1", "skip_trace", make_call)

    assert first == "result"
    assert second == "result"
    assert calls == [1]  # make_call only ran once
    assert cache == {"1": {"skip_trace": "result"}}


def test_cached_call_keys_are_independent_per_acctid_and_call_type():
    cache = {}
    batchdata_cache.cached_call(cache, "1", "skip_trace", lambda: "trace-1")
    batchdata_cache.cached_call(cache, "1", "valuation", lambda: "val-1")
    batchdata_cache.cached_call(cache, "2", "skip_trace", lambda: "trace-2")

    assert cache == {
        "1": {"skip_trace": "trace-1", "valuation": "val-1"},
        "2": {"skip_trace": "trace-2"},
    }
