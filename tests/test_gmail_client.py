import base64
import email
import errno
from pathlib import Path

import pytest

import skip_tracer.gmail_client as gmail_client


class _FakeCredentials:
    def __init__(self, valid=True, expired=False, refresh_token=None, has_scope=True):
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self._has_scope = has_scope
        self.refreshed = False

    def has_scopes(self, scopes):
        return self._has_scope

    def refresh(self, request):
        self.refreshed = True
        self.valid = True

    def to_json(self):
        return '{"fake": "token"}'


class _FakeExecute:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeMessages:
    def __init__(self):
        self.sent = None

    def send(self, userId, body):
        self.sent = {"userId": userId, "body": body}
        return _FakeExecute({"id": "msg123"})


class _FakeUsers:
    def __init__(self):
        self._messages = _FakeMessages()

    def messages(self):
        return self._messages


class _FakeService:
    def __init__(self):
        self._users = _FakeUsers()

    def users(self):
        return self._users


def _decoded_message(service):
    raw = service._users._messages.sent["body"]["raw"]
    mime_bytes = base64.urlsafe_b64decode(raw.encode("ascii"))
    return email.message_from_bytes(mime_bytes)


def test_send_email_plain_text_defaults():
    service = _FakeService()

    msg_id = gmail_client.send_email(service, "Subject line", "Body text", "to@example.com")

    assert msg_id == "msg123"
    sent = service._users._messages.sent
    assert sent["userId"] == "me"
    message = _decoded_message(service)
    assert message.get_content_type() == "text/plain"
    assert message["Subject"] == "Subject line"
    assert message["To"] == "to@example.com"
    assert message.get_payload(decode=True).decode("utf-8") == "Body text"


def test_send_email_html():
    service = _FakeService()

    gmail_client.send_email(service, "Subj", "<p>hi</p>", "to@example.com", html=True)

    message = _decoded_message(service)
    assert message.get_content_type() == "text/html"
    assert message.get_payload(decode=True).decode("utf-8") == "<p>hi</p>"


def test_get_gmail_service_raises_when_no_token_or_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_TOKEN_PATH", str(tmp_path / "token.json"))
    monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))

    with pytest.raises(FileNotFoundError):
        gmail_client.get_gmail_service()


def test_get_gmail_service_uses_valid_cached_token(tmp_path, monkeypatch):
    token_path = tmp_path / "token.json"
    token_path.write_text("{}")
    monkeypatch.setenv("GMAIL_TOKEN_PATH", str(token_path))
    monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))

    fake_creds = _FakeCredentials(valid=True)
    monkeypatch.setattr(
        gmail_client.Credentials,
        "from_authorized_user_file",
        staticmethod(lambda path, scopes: fake_creds),
    )
    built = {}

    def fake_build(*args, **kwargs):
        built["credentials"] = kwargs.get("credentials")
        return "the-service"

    monkeypatch.setattr(gmail_client, "build", fake_build)

    service = gmail_client.get_gmail_service()

    assert service == "the-service"
    assert built["credentials"] is fake_creds
    assert fake_creds.refreshed is False


def test_get_gmail_service_refreshes_expired_token_and_saves(tmp_path, monkeypatch):
    token_path = tmp_path / "token.json"
    token_path.write_text("{}")
    monkeypatch.setenv("GMAIL_TOKEN_PATH", str(token_path))
    monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))

    fake_creds = _FakeCredentials(valid=False, expired=True, refresh_token="rt")
    monkeypatch.setattr(
        gmail_client.Credentials,
        "from_authorized_user_file",
        staticmethod(lambda path, scopes: fake_creds),
    )
    monkeypatch.setattr(gmail_client, "build", lambda *args, **kwargs: "the-service")

    service = gmail_client.get_gmail_service()

    assert service == "the-service"
    assert fake_creds.refreshed is True
    assert token_path.read_text() == '{"fake": "token"}'


def test_get_gmail_service_ignores_cached_token_missing_scope(tmp_path, monkeypatch):
    token_path = tmp_path / "token.json"
    token_path.write_text("{}")
    monkeypatch.setenv("GMAIL_TOKEN_PATH", str(token_path))
    monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))

    fake_creds = _FakeCredentials(valid=True, has_scope=False)
    monkeypatch.setattr(
        gmail_client.Credentials,
        "from_authorized_user_file",
        staticmethod(lambda path, scopes: fake_creds),
    )

    # Missing scope drops the cached credentials, and no credentials.json
    # exists to bootstrap a fresh consent flow.
    with pytest.raises(FileNotFoundError):
        gmail_client.get_gmail_service()


def test_save_token_swallows_read_only_filesystem_error(tmp_path, monkeypatch, capsys):
    token_path = tmp_path / "token.json"

    def fake_write_text(self, content, encoding=None):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(Path, "write_text", fake_write_text)

    gmail_client._save_token(token_path, _FakeCredentials())

    assert "skipping write to read-only" in capsys.readouterr().out


def test_save_token_reraises_other_os_errors(tmp_path, monkeypatch):
    token_path = tmp_path / "token.json"

    def fake_write_text(self, content, encoding=None):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(Path, "write_text", fake_write_text)

    with pytest.raises(OSError):
        gmail_client._save_token(token_path, _FakeCredentials())
