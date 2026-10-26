"""Mint a Google access token from a stored refresh token.

Postcard normally gets XOAUTH2 credentials from GNOME Online Accounts. This
adds a second source: a refresh token kept in the keyring, exchanged for a
short-lived access token at sign-in. It lets a Google account be used without
GOA installed -- the refresh token is obtained once (e.g. imported from another
mail client the user already authorised) and stored like a password.

The client_id/secret below are Thunderbird's built-in, public OAuth client
(shipped in its open-source tree); Google treats "installed app" secrets as
non-confidential, so they identify the client, they do not protect anything.
"""

import json
import urllib.parse
import urllib.request

# Thunderbird's built-in Google OAuth client (public, from its source tree).
TB_CLIENT_ID = "406964657835-aq8lmia8j95dhl1a2bvharmfk3t1hgqj.apps.googleusercontent.com"
TB_CLIENT_SECRET = "kSmqreRr0qwBWJgbf5Y-PjSU"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"


def access_token(refresh_token: str) -> str:
    """Exchange a refresh token for a fresh access token. Blocks on one HTTPS
    round trip; call it from the worker thread, as credential_for already is."""
    data = urllib.parse.urlencode(
        {
            "client_id": TB_CLIENT_ID,
            "client_secret": TB_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode()
    with urllib.request.urlopen(_TOKEN_ENDPOINT, data=data, timeout=30) as response:
        return json.load(response)["access_token"]
