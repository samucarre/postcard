from gi.repository import GObject

from .email import Email


class Conversation(GObject.Object):
    __gtype_name__ = "PostcardConversation"

    def __init__(self, emails: list[Email], id: int | None = None) -> None:
        super().__init__()
        self.emails: list[Email] = emails
        # With conversation grouping off, every message is its own row and must
        # not share the id of the thread it would otherwise belong to.
        self._id: int | None = id

    @property
    def id(self) -> int:
        if self._id is not None:
            return self._id
        return self.emails[0].conversation_id or self.emails[0].id

    @property
    def latest(self) -> Email:
        return self.emails[-1]

    @property
    def subject(self) -> str:
        return self.latest.subject

    @property
    def date(self) -> str:
        return self.latest.date

    @property
    def preview(self) -> str:
        return self.latest.preview

    @property
    def count(self) -> int:
        return len(self.emails)

    @property
    def is_unread(self) -> bool:
        return any(mail.is_unread for mail in self.emails)

    @property
    def is_starred(self) -> bool:
        return any(mail.is_starred for mail in self.emails)

    @property
    def participants(self) -> str:
        seen_senders: list[str] = []
        for mail in self.emails:
            if mail.sender not in seen_senders:
                seen_senders.append(mail.sender)
        return ", ".join(seen_senders)
