from skip_tracer.imap_client import fetch_jurisdiction_leads


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    """Serves two pages, then a final page with no exceededTransferLimit."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append(params)
        page = self.pages[len(self.calls) - 1]
        return _FakeResponse(page)


def _feature(acctid):
    return {"attributes": {"ACCTID": acctid}}


def test_fetch_jurisdiction_leads_paginates_past_transfer_limit():
    pages = [
        {"features": [_feature("1"), _feature("2")], "exceededTransferLimit": True},
        {"features": [_feature("3")], "exceededTransferLimit": False},
    ]
    session = _FakeSession(pages)

    records = fetch_jurisdiction_leads("MONT", session=session)

    assert [r["ACCTID"] for r in records] == ["1", "2", "3"]
    assert session.calls[0]["resultOffset"] == 0
    assert session.calls[1]["resultOffset"] == 2


def test_fetch_jurisdiction_leads_stops_on_empty_features():
    pages = [{"features": [], "exceededTransferLimit": True}]
    session = _FakeSession(pages)

    records = fetch_jurisdiction_leads("MONT", session=session)

    assert records == []


def test_fetch_jurisdiction_leads_excludes_owner_occupied_by_default():
    session = _FakeSession([{"features": [], "exceededTransferLimit": False}])

    fetch_jurisdiction_leads("MONT", session=session)

    assert "OOI<>'H'" in session.calls[0]["where"]


def test_fetch_jurisdiction_leads_includes_owner_occupied_when_requested():
    session = _FakeSession([{"features": [], "exceededTransferLimit": False}])

    fetch_jurisdiction_leads("MONT", session=session, include_owner_occupied=True)

    assert "OOI" not in session.calls[0]["where"]
