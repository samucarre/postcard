"""Mail the user has something else file away the moment it lands.

Postcard has no filters, so a user may run their own: a server rule, or a script
that moves mail over IMAP. Until that acts, a message still passes through the
inbox as a row in the list, one more unread and a notification. Naming those
messages in ~/.config/postcard/filed-elsewhere.json keeps them out of all three,
and keeps their destination folder from announcing them either:

    [{"account": "me@example.com", "from": "shop.example", "subject": "your order",
      "moved_to": "Shop"},
     {"account": "me@example.com", "from": "shop.example", "subject_starts": "news:",
      "moved_to": "Shop"}]

Matching ignores case, and "from" looks in the display name and the address.
Postcard only looks away and never moves anything, so a rule with nothing behind
it hides that mail from the inbox for good: whatever does the moving should read
this file too. An incomplete rule is ignored rather than guessed at, so a typo
can't hide more than was asked for.
"""

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .models.message_header import MessageHeader

logger = logging.getLogger(__name__)

_INBOX = "inbox"


def default_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(config_home) / "postcard" / "filed-elsewhere.json"


@dataclass(frozen=True)
class _Rule:
    account: str
    sender: str
    subject: str
    subject_starts: str
    moved_to: str

    def matches(self, message: MessageHeader) -> bool:
        if self.sender not in f"{message.sender} {message.sender_address}".lower():
            return False
        subject = message.subject.strip().lower()
        return bool(
            (self.subject and self.subject in subject)
            or (self.subject_starts and subject.startswith(self.subject_starts))
        )


@dataclass(frozen=True)
class FilingRules:
    rules: tuple[_Rule, ...] = ()

    def _for(self, account: str) -> list[_Rule]:
        return [rule for rule in self.rules if rule.account == account.lower()]

    def is_leaving(self, account: str, mailbox: str, message: MessageHeader) -> bool:
        """Whether this message is in the inbox only until it gets filed away."""
        return mailbox.lower() == _INBOX and any(
            rule.matches(message) for rule in self._for(account)
        )

    def may_count_leaving_mail(self, account: str, mailbox: str) -> bool:
        """Whether the server's unread count here can include mail on its way out."""
        return mailbox.lower() == _INBOX and bool(self._for(account))

    def is_destination(self, account: str, mailbox: str) -> bool:
        """Whether mail is filed into this folder, so its arrivals aren't news."""
        return any(rule.moved_to == mailbox for rule in self._for(account))


def _parse(entry: object) -> _Rule | None:
    if not isinstance(entry, dict):
        return None
    rule = _Rule(
        account=str(entry.get("account", "")).strip().lower(),
        sender=str(entry.get("from", "")).strip().lower(),
        subject=str(entry.get("subject", "")).strip().lower(),
        subject_starts=str(entry.get("subject_starts", "")).strip().lower(),
        moved_to=str(entry.get("moved_to", "")).strip(),
    )
    is_complete = (
        rule.account
        and rule.sender
        and rule.moved_to
        and (rule.subject or rule.subject_starts)
    )
    return rule if is_complete else None


def load(path: Path | None = None) -> FilingRules:
    """The rules on disk now: read every time, so an edit needs no restart."""
    source = path or default_path()
    try:
        entries = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return FilingRules()
    except (OSError, ValueError):
        logger.warning("could not read the filing rules in %s", source, exc_info=True)
        return FilingRules()
    if not isinstance(entries, list):
        logger.warning("the filing rules in %s are not a list", source)
        return FilingRules()
    rules = []
    for entry in entries:
        rule = _parse(entry)
        if rule is None:
            logger.warning("ignoring an incomplete rule in %s: %r", source, entry)
        else:
            rules.append(rule)
    return FilingRules(tuple(rules))
