"""Bring every account's whole mailbox down in the background.

Upstream Postcard keeps only the newest page of each folder and fetches the
rest on scroll, so search and the list only ever see the last few weeks. Here
a worker thread walks every folder of every account, oldest gaps included:
first every message's headers (cheap, and enough for the list and search),
then every body, newest first, so old mail opens at once and offline.

It runs on its own IMAP connection and its own SQLite connection, so neither
the window's syncs nor the main loop wait on it, and it never notifies: the
window's own sync stays the only thing that announces new mail. In the folders
the window looks after (the inbox and the other role folders) only messages older
than the newest one it holds are filled in, so a message that has just arrived is
still the window's to find and announce. A user's own folder has nobody else
keeping it fresh until it's opened, so its new mail comes down here too, unannounced.
"""

import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime

from gi.repository import GLib

from . import mail_sync
from .core import filing_rules, secrets
from .core.models.account import Account
from .core.net.imap_session import ATTR_NOSELECT, STATUS_OK, ImapError, ImapSession

logger = logging.getLogger(__name__)

# Gmail's virtual folders hold copies of mail that lives in a real folder too
# (All Mail is every message; Important and Starred are subsets): fetching them
# would store each message twice or three times.
SKIP_ATTRIBUTES = frozenset(
    {ATTR_NOSELECT, "\\NonExistent", "\\All", "\\Important", "\\Flagged"}
)

HEADER_FIELDS = (
    "(UID FLAGS BODY.PEEK[HEADER.FIELDS "
    "(DATE FROM TO CC SUBJECT MESSAGE-ID IN-REPLY-TO REFERENCES)])"
)
HEADERS_PER_FETCH = 500
SIZES_PER_FETCH = 2000
# A body fetch is sized by bytes, so a run of large attachments doesn't hold
# tens of megabytes in memory, nor a write transaction open for long.
BODY_BYTES_PER_FETCH = 8 * 1024 * 1024
BODIES_PER_FETCH = 50
# After a failure (network gone, server busy) wait this long before trying again.
RETRY_SECONDS = 5 * 60
# Every sync kicks the worker, and a pass lists every UID of every folder (a
# megabyte for a large inbox), so once caught up it looks again at most this often.
MIN_PASS_SECONDS = 10 * 60

STATE_PATH = os.path.join(GLib.get_user_state_dir(), "postcard", "descarga.json")


def _uid_set(uids: Sequence[int]) -> str:
    """Compress UIDs into an IMAP sequence set: [1,2,3,7] -> "1:3,7"."""
    parts: list[str] = []
    ordered = sorted(uids)
    start = prev = ordered[0]
    for uid in ordered[1:]:
        if uid == prev + 1:
            prev = uid
            continue
        parts.append(f"{start}:{prev}" if start != prev else str(start))
        start = prev = uid
    parts.append(f"{start}:{prev}" if start != prev else str(start))
    return ",".join(parts)


def _chunks[T](items: Sequence[T], size: int) -> Iterable[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class StoppedError(Exception):
    """The window is closing, or asked for a fresh start."""


class Backfill:
    """One worker thread for every account, run again on each kick()."""

    def __init__(
        self,
        db_path: str,
        on_progress: Callable[[int], None],
    ) -> None:
        self._db_path = db_path
        # Called on the worker thread with a folder id whenever that folder
        # gained rows; the window hops to the main loop itself.
        self._on_progress = on_progress
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._accounts: list[Account] = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state: dict[str, dict[str, dict[str, int]]] = {}
        self._phase = "esperando"

    # --- called from the main loop ---------------------------------------

    def kick(self, accounts: Iterable[Account]) -> None:
        with self._lock:
            self._accounts = [a for a in accounts if not a.is_graph]
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name="backfill", daemon=True
            )
            self._thread.start()
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # --- the worker -------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            if self._stop.is_set():
                return
            with self._lock:
                accounts = list(self._accounts)
            try:
                is_complete = self._pass(accounts)
            except StoppedError:
                return
            except Exception:
                logger.exception("mailbox download stopped")
                is_complete = False
            if self._stop.is_set():
                return
            if not is_complete:
                # Something failed (offline, a server hiccup): try again later
                # even if no sync kicks us before then.
                self._stop.wait(RETRY_SECONDS)
                self._wake.set()
            else:
                self._stop.wait(MIN_PASS_SECONDS)

    def _pass(self, accounts: list[Account]) -> bool:
        """Headers for every account, then bodies. True if nothing failed."""
        is_complete = True
        conn = sqlite3.connect(self._db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            for phase, work in (
                ("cabeceras", self._headers),
                ("cuerpos", self._bodies),
            ):
                self._phase = phase
                for account in accounts:
                    self._check_stop()
                    try:
                        self._with_session(account, conn, work)
                    except StoppedError:
                        raise
                    except Exception:
                        logger.warning(
                            "mailbox download (%s) failed for %s",
                            phase,
                            account.email,
                            exc_info=True,
                        )
                        is_complete = False
            self._phase = "hecho" if is_complete else "pendiente (reintenta en 5 min)"
            self._save_state()
        finally:
            conn.close()
        return is_complete

    def _check_stop(self) -> None:
        if self._stop.is_set():
            raise StoppedError()

    def _with_session(
        self,
        account: Account,
        conn: sqlite3.Connection,
        work: Callable[
            [Account, ImapSession, sqlite3.Connection, list[tuple[int, str]]], None
        ],
    ) -> None:
        credential = secrets.credential_for(account)
        if credential is None:
            raise ImapError(f"no credential for {account.email}")
        session = mail_sync._signed_in(account, credential)  # noqa: SLF001 -- reuses the sync's own session and parsing
        try:
            folders = self._folders(account, session, conn)
            work(account, session, conn, folders)
        finally:
            session.logout()

    def _folders(
        self, account: Account, session: ImapSession, conn: sqlite3.Connection
    ) -> list[tuple[int, str]]:
        """(folder id, mailbox name) of every real folder the window knows."""
        known = {
            row["name"]: row["id"]
            for row in conn.execute(
                "SELECT id, name FROM folders WHERE account_id = ?", (account.id,)
            )
        }
        folders = []
        for mailbox in session.list_folders():
            if mailbox.name in mail_sync.NAMESPACE_ROOTS:
                continue
            if SKIP_ATTRIBUTES & {flag.strip() for flag in mailbox.flags.split()}:
                continue
            folder_id = known.get(mailbox.name)
            if folder_id is not None:
                folders.append((folder_id, mailbox.name))
        return folders

    def _open(
        self, session: ImapSession, conn: sqlite3.Connection, folder_id: int, name: str
    ) -> bool:
        """Select a folder read-only, if its UIDs still mean what we stored."""
        exists = session.select(name, True)
        current = session.uidvalidity()
        row = conn.execute(
            "SELECT uidvalidity FROM folders WHERE id = ?", (folder_id,)
        ).fetchone()
        if row is None:
            return False
        stored = row["uidvalidity"]
        if stored is None:
            has_rows = conn.execute(
                "SELECT 1 FROM emails WHERE folder_id = ? AND server_id != '' LIMIT 1",
                (folder_id,),
            ).fetchone()
            if has_rows:
                # Rows cached before the value was recorded: the window's next
                # sync checks them first.
                return False
            if current is not None:
                with conn:
                    conn.execute(
                        "UPDATE folders SET uidvalidity = ?"
                        " WHERE id = ? AND uidvalidity IS NULL",
                        (current, folder_id),
                    )
        elif current is not None and current != stored:
            return False  # renumbered: the window's sync drops the cache first
        return exists > 0

    # --- phase 1: headers -------------------------------------------------

    def _headers(
        self,
        account: Account,
        session: ImapSession,
        conn: sqlite3.Connection,
        folders: list[tuple[int, str]],
    ) -> None:
        rules = filing_rules.load()
        failures: list[ImapError] = []
        for folder_id, name in folders:
            self._check_stop()
            if not self._open(session, conn, folder_id, name):
                continue
            is_own_folder = (
                mail_sync.role_for_folder(name) is mail_sync.FolderRole.OTHER
            )
            try:
                on_server = {int(uid) for uid in session.search_all_uids()}
            except ImapError as error:
                # Seen on iCloud: a folder selected as non-empty, then a search
                # with no answer. That mustn't cost the folders after it their
                # turn; raising at the end still gets the pass retried.
                logger.warning("could not list %s on %s", name, account.email)
                failures.append(error)
                continue
            local = {
                int(row[0])
                for row in conn.execute(
                    "SELECT server_id FROM emails"
                    " WHERE folder_id = ? AND server_id != ''",
                    (folder_id,),
                )
                if str(row[0]).isdigit()
            }
            newest_local = max(local) if local else None
            missing = sorted(
                (
                    uid
                    for uid in on_server - local
                    if is_own_folder or newest_local is None or uid < newest_local
                ),
                reverse=True,
            )
            self._note(
                account, name, servidor=len(on_server), cabeceras=len(on_server & local)
            )
            if not missing:
                continue
            logger.info("%s %s: fetching %d headers", account.email, name, len(missing))
            stored = len(on_server & local)
            for chunk in _chunks(missing, HEADERS_PER_FETCH):
                self._check_stop()
                headers = [
                    header
                    for header in self._fetch_headers(session, chunk)
                    if not rules.is_leaving(account.email, name, header)
                ]
                with conn:
                    conn.executemany(
                        """
                        INSERT INTO emails
                            (folder_id, server_id, sender, subject, preview, date,
                             unread, starred, message_id, in_reply_to, reference_ids,
                             sender_address, recipient, recipient_address)
                        VALUES (?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (folder_id, server_id) DO NOTHING
                        """,
                        [
                            (
                                folder_id,
                                h.uid,
                                h.sender,
                                h.subject,
                                h.date,
                                int(h.is_unread),
                                int(h.is_starred),
                                h.message_id,
                                h.in_reply_to,
                                h.references,
                                h.sender_address,
                                h.recipient,
                                h.recipient_address,
                            )
                            for h in headers
                        ],
                    )
                stored += len(headers)
                self._note(account, name, cabeceras=stored)
                self._on_progress(folder_id)
        if failures:
            raise failures[0]

    def _fetch_headers(self, session: ImapSession, uids: Sequence[int]) -> list:
        status, payload = session._require_imap().uid(  # noqa: SLF001
            "FETCH", _uid_set(uids), HEADER_FIELDS
        )
        if status != STATUS_OK:
            raise ImapError(f"header fetch failed: {payload}")
        headers = []
        for item in payload:
            if not isinstance(item, tuple):
                continue
            meta, header_bytes = item
            fetched = session._parse(meta.decode("utf-8", "replace"), header_bytes)  # noqa: SLF001 -- reuses the sync's own session and parsing
            if fetched.uid:
                headers.append(mail_sync._to_message_header(fetched))  # noqa: SLF001 -- reuses the sync's own session and parsing
        return headers

    # --- phase 2: bodies --------------------------------------------------

    def _bodies(
        self,
        account: Account,
        session: ImapSession,
        conn: sqlite3.Connection,
        folders: list[tuple[int, str]],
    ) -> None:
        for folder_id, name in folders:
            self._check_stop()
            wanted = {
                int(row["server_id"]): row["id"]
                for row in conn.execute(
                    """
                    SELECT e.id, e.server_id FROM emails e
                    LEFT JOIN bodies b ON b.email_id = e.id
                    WHERE e.folder_id = ? AND e.server_id != '' AND b.email_id IS NULL
                    """,
                    (folder_id,),
                )
                if str(row["server_id"]).isdigit()
            }
            total = conn.execute(
                "SELECT COUNT(*) FROM emails WHERE folder_id = ? AND server_id != ''",
                (folder_id,),
            ).fetchone()[0]
            self._note(account, name, cuerpos=total - len(wanted))
            if not wanted or not self._open(session, conn, folder_id, name):
                continue
            logger.info("%s %s: fetching %d bodies", account.email, name, len(wanted))
            done = total - len(wanted)
            for size_chunk in _chunks(sorted(wanted, reverse=True), SIZES_PER_FETCH):
                sizes = self._fetch_sizes(session, size_chunk)
                for batch in self._batches_by_size(size_chunk, sizes):
                    self._check_stop()
                    bodies = self._fetch_bodies(session, batch)
                    with conn:
                        conn.executemany(
                            """
                            INSERT OR IGNORE INTO bodies (email_id, raw)
                            SELECT id, ? FROM emails WHERE id = ?
                            """,
                            [
                                (raw, wanted[uid])
                                for uid, raw in bodies.items()
                                if uid in wanted
                            ],
                        )
                    done += len(bodies)
                    self._note(account, name, cuerpos=done)

    def _fetch_sizes(self, session: ImapSession, uids: Sequence[int]) -> dict[int, int]:
        status, payload = session._require_imap().uid(  # noqa: SLF001
            "FETCH", _uid_set(uids), "(UID RFC822.SIZE)"
        )
        if status != STATUS_OK:
            raise ImapError(f"size fetch failed: {payload}")
        sizes: dict[int, int] = {}
        for item in payload:
            text = item.decode("ascii", "replace") if isinstance(item, bytes) else ""
            uid = re.search(r"UID (\d+)", text)
            size = re.search(r"RFC822\.SIZE (\d+)", text)
            if uid and size:
                sizes[int(uid.group(1))] = int(size.group(1))
        return sizes

    @staticmethod
    def _batches_by_size(
        uids: Sequence[int], sizes: dict[int, int]
    ) -> Iterable[list[int]]:
        batch: list[int] = []
        batch_bytes = 0
        for uid in uids:
            if uid not in sizes:
                continue  # expunged since the list was read
            if batch and (
                batch_bytes + sizes[uid] > BODY_BYTES_PER_FETCH
                or len(batch) >= BODIES_PER_FETCH
            ):
                yield batch
                batch, batch_bytes = [], 0
            batch.append(uid)
            batch_bytes += sizes[uid]
        if batch:
            yield batch

    def _fetch_bodies(
        self, session: ImapSession, uids: Sequence[int]
    ) -> dict[int, bytes]:
        status, payload = session._require_imap().uid(  # noqa: SLF001
            "FETCH", _uid_set(uids), "(UID BODY.PEEK[])"
        )
        if status != STATUS_OK:
            raise ImapError(f"body fetch failed: {payload}")
        bodies: dict[int, bytes] = {}
        for item in payload:
            if not isinstance(item, tuple):
                continue
            meta, raw = item
            uid = re.search(rb"UID (\d+)", meta)
            if uid and isinstance(raw, bytes):
                bodies[int(uid.group(1))] = raw
        missing = [uid for uid in uids if uid not in bodies]
        for uid in missing:
            # A server that put the UID after the literal: one at a time.
            try:
                bodies[uid] = session.fetch_message(str(uid))
            except ImapError:
                logger.warning("no body for uid %s", uid, exc_info=True)
        return bodies

    # --- progress, for anyone asking how far it got -------------------------

    def _note(self, account: Account, folder: str, **counts: int) -> None:
        self._state.setdefault(account.email, {}).setdefault(folder, {}).update(counts)
        now = time.monotonic()
        if now - getattr(self, "_saved_at", 0.0) > 10:
            self._save_state()

    def _save_state(self) -> None:
        self._saved_at = time.monotonic()
        try:
            os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(
                    {
                        "fase": self._phase,
                        "actualizado": datetime.now().isoformat(timespec="seconds"),
                        "cuentas": self._state,
                    },
                    f,
                    ensure_ascii=False,
                    indent=1,
                )
            os.replace(tmp, STATE_PATH)
        except OSError:
            logger.warning("could not write %s", STATE_PATH, exc_info=True)
