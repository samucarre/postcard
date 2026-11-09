import json
from types import SimpleNamespace

import pytest

import postcard.backfill as backfill
from postcard.core.models.message_header import MessageHeader
from postcard.core.net.imap_session import ImapError
from postcard.core.store.database import Database

EMAIL = "me@example.com"
UIDVALIDITY = 7


def header(uid: int, subject: str = "hello") -> MessageHeader:
    return MessageHeader(
        uid=str(uid),
        sender="Shop",
        sender_address="no-reply@shop.example",
        recipient="",
        recipient_address="",
        subject=subject,
        date="2026-01-01T00:00:00+00:00",
        is_unread=True,
    )


class FakeSession:
    """A server whose folders hold these UIDs; None answers the search with an error."""

    def __init__(self, uids_by_folder: dict[str, list[int] | None]) -> None:
        self._uids_by_folder = uids_by_folder
        self._selected = ""

    def select(self, name: str, _is_readonly: bool) -> int:
        self._selected = name
        return len(self._uids_by_folder[name] or [0])

    def uidvalidity(self) -> int:
        return UIDVALIDITY

    def search_all_uids(self) -> set[str]:
        uids = self._uids_by_folder[self._selected]
        if uids is None:
            raise ImapError("search returned a non-bytes item: None")
        return {str(uid) for uid in uids}


@pytest.fixture
def run(monkeypatch, tmp_path):
    """Run one headers phase over a folder; returns the UIDs stored in it after."""
    monkeypatch.setattr(backfill, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    rules = tmp_path / "postcard" / "filed-elsewhere.json"
    rules.parent.mkdir()
    rules.write_text(
        json.dumps(
            [
                {
                    "account": EMAIL,
                    "from": "shop.example",
                    "subject": "your order",
                    "moved_to": "Shop",
                }
            ]
        )
    )
    database = Database(":memory:")
    account = database.save_account(
        EMAIL, "Me", "imap.example.com", 993, "smtp.example.com", 587
    )

    def stored_in(folder_id: int) -> set[int]:
        rows = database._conn.execute(  # noqa: SLF001
            "SELECT server_id FROM emails WHERE folder_id = ?", (folder_id,)
        )
        return {int(row[0]) for row in rows}

    def folder_with(name: str, local: list[int]) -> int:
        folder = database.get_or_create_folder(account.id, name, "folder-symbolic")
        database.set_folder_uidvalidity(folder.id, UIDVALIDITY)
        for uid in local:
            database.save_incoming_email(folder.id, header(uid))
        return folder.id

    def walk(session: FakeSession, folders: list[tuple[int, str]], subjects) -> None:
        worker = backfill.Backfill(":memory:", lambda _folder_id: None)
        worker._fetch_headers = lambda _session, uids: [  # noqa: SLF001
            header(uid, subjects.get(uid, "hello")) for uid in uids
        ]
        worker._headers(  # noqa: SLF001
            SimpleNamespace(email=EMAIL),
            session,
            database._conn,  # noqa: SLF001
            folders,
        )

    def _run(name: str, local: list[int], server: dict[int, str]) -> set[int]:
        folder_id = folder_with(name, local)
        session = FakeSession({name: sorted({*local, *server})})
        walk(session, [(folder_id, name)], server)
        return stored_in(folder_id)

    _run.folder_with = folder_with
    _run.walk = walk
    _run.stored_in = stored_in
    yield _run
    database.close()


def test_new_mail_in_a_users_own_folder_comes_down(run):
    assert run("Shop", [1, 2], {3: "your order", 4: "hello"}) == {1, 2, 3, 4}


def test_a_gap_in_the_inbox_is_filled_but_new_mail_is_left_to_the_window(run):
    assert run("INBOX", [1, 5], {3: "hello", 9: "hello"}) == {1, 3, 5}


def test_new_mail_in_a_role_folder_is_left_to_the_window(run):
    assert run("Sent", [1, 5], {3: "hello", 9: "hello"}) == {1, 3, 5}


def test_inbox_mail_about_to_be_filed_away_is_not_stored(run):
    assert run("INBOX", [1, 5], {3: "Your order is here", 4: "hello"}) == {1, 4, 5}


def test_a_folder_that_will_not_list_does_not_cost_the_ones_after_it(run):
    broken = run.folder_with("Broken", [])
    shop = run.folder_with("Shop", [1])
    session = FakeSession({"Broken": None, "Shop": [1, 2]})

    with pytest.raises(ImapError):
        run.walk(session, [(broken, "Broken"), (shop, "Shop")], {})

    assert run.stored_in(shop) == {1, 2}
