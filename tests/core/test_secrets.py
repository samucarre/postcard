import pytest
from gi.repository import GLib

from postcard.core import secrets


def _stale_name_error():
    # What libsecret raises on every call once the keyring daemon it first
    # talked to has been replaced: the cached proxy names a bus name nobody
    # owns any more.
    message = "The name :1.12 was not provided by any .service files"
    return GLib.Error(message, "g-dbus-error-quark", 2)


def test_a_keyring_call_is_retried_once_after_reconnecting(monkeypatch):
    calls = []
    disconnected = []

    def lookup():
        calls.append(1)
        if len(calls) == 1:
            raise _stale_name_error()
        return "secret"

    monkeypatch.setattr(
        secrets.Secret.Service, "disconnect", lambda: disconnected.append(1)
    )
    assert secrets._keyring(lookup) == "secret"
    assert len(calls) == 2
    assert disconnected == [1]


def test_a_keyring_still_down_after_reconnecting_raises(monkeypatch):
    monkeypatch.setattr(secrets.Secret.Service, "disconnect", lambda: None)

    def lookup():
        raise _stale_name_error()

    with pytest.raises(GLib.Error):
        secrets._keyring(lookup)


def test_an_error_that_is_not_dbus_is_not_retried(monkeypatch):
    calls = []
    monkeypatch.setattr(
        secrets.Secret.Service,
        "disconnect",
        lambda: pytest.fail("should not reconnect"),
    )

    def lookup():
        calls.append(1)
        raise GLib.Error("locked", "secret-error-quark", 1)

    with pytest.raises(GLib.Error):
        secrets._keyring(lookup)
    assert len(calls) == 1


def test_credential_for_gives_none_when_the_keyring_is_unreachable(monkeypatch):
    from postcard.core.models.account import Account

    monkeypatch.setattr(secrets.Secret.Service, "disconnect", lambda: None)

    def lookup(*_args):
        raise _stale_name_error()

    monkeypatch.setattr(secrets.Secret, "password_lookup_sync", lookup)
    account = Account(
        id=1,
        email="a@b.c",
        display_name="A",
        imap_host="imap.b.c",
        imap_port=993,
        smtp_host="smtp.b.c",
        smtp_port=465,
    )
    assert secrets.credential_for(account) is None
