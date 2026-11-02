"""Where an account's sign-in comes from: the system keyring for an account
typed in by hand, GNOME Online Accounts for one imported from Settings."""

import logging
from collections.abc import Callable

import gi

gi.require_version("Secret", "1")

from gi.repository import GLib, Secret

from . import goa
from .models.account import Account
from .net import google_oauth
from .net.auth import MECHANISM_XOAUTH2, Credential

logger = logging.getLogger(__name__)

_SCHEMA = Secret.Schema.new(
    "in.gxanshu.postcard.Account",
    Secret.SchemaFlags.NONE,
    {"account-id": Secret.SchemaAttributeType.INTEGER},
)

# A Google refresh token for an account signed in with XOAUTH2 (imported rather
# than handed over by GOA). Kept separate from the password schema so an account
# is never both at once.
_OAUTH_SCHEMA = Secret.Schema.new(
    "in.gxanshu.postcard.OAuthRefresh",
    Secret.SchemaFlags.NONE,
    {"account-id": Secret.SchemaAttributeType.INTEGER},
)


def _keyring[T](call: Callable[[], T]) -> T:
    """Run one libsecret call, reconnecting once if the keyring went away.

    libsecret keeps a proxy to the Secret Service it first talked to. Once
    that daemon is replaced (gnome-keyring-daemon --replace, a crash, a
    restart of the user session's keyring), every call on the cached proxy
    fails with a D-Bus error naming a bus name nobody owns any more, for as
    long as the process lives. Dropping the proxy makes the next call look
    the service up again.
    """
    try:
        return call()
    except GLib.Error as error:
        if error.domain != "g-dbus-error-quark":
            raise
        logger.warning(
            "the keyring stopped answering (%s); reconnecting to it", error.message
        )
        Secret.Service.disconnect()
        return call()


def store_refresh_token(account_id: int, refresh_token: str) -> None:
    _keyring(
        lambda: Secret.password_store_sync(
            _OAUTH_SCHEMA,
            {"account-id": str(account_id)},
            Secret.COLLECTION_DEFAULT,
            f"Postcard account {account_id} (OAuth)",
            refresh_token,
            None,
        )
    )


def lookup_refresh_token(account_id: int) -> str | None:
    return _keyring(
        lambda: Secret.password_lookup_sync(
            _OAUTH_SCHEMA, {"account-id": str(account_id)}, None
        )
    )


def store_password(account_id: int, password: str) -> None:
    _keyring(
        lambda: Secret.password_store_sync(
            _SCHEMA,
            {"account-id": str(account_id)},
            Secret.COLLECTION_DEFAULT,
            f"Postcard account {account_id}",
            password,
            None,
        )
    )


def lookup_password(account_id: int) -> str | None:
    return _keyring(
        lambda: Secret.password_lookup_sync(
            _SCHEMA, {"account-id": str(account_id)}, None
        )
    )


def clear_password(account_id: int) -> bool:
    return _keyring(
        lambda: Secret.password_clear_sync(
            _SCHEMA, {"account-id": str(account_id)}, None
        )
    )


def credential_for(account: Account) -> Credential | None:
    """How to sign this account in, or None when we cannot.

    Called from the worker thread: both branches block on IPC, and the Online
    Accounts one can spend a network round trip refreshing an expired token.
    """
    if account.goa_id:
        return goa.credential(account.goa_id)

    try:
        refresh_token = lookup_refresh_token(account.id)
        password = None if refresh_token is not None else lookup_password(account.id)
    except GLib.Error:
        # The keyring is unreachable even after a reconnect. None is what every
        # worker already handles; an exception here used to kill the thread,
        # leaving its account marked as busy for the rest of the session.
        logger.exception("could not read the keyring for %s", account.email)
        return None

    if refresh_token is not None:
        access_token = google_oauth.access_token(refresh_token)
        return Credential(account.login_name, access_token, MECHANISM_XOAUTH2)

    return Credential(account.login_name, password) if password else None
