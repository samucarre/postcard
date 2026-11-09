import json

import pytest

from postcard.core import filing_rules
from postcard.core.models.message_header import MessageHeader

ACCOUNT = "me@example.com"
BY_CONTENT = {
    "account": ACCOUNT,
    "from": "shop.example",
    "subject": "your order",
    "moved_to": "Shop",
}
BY_PREFIX = {
    "account": ACCOUNT,
    "from": "shop.example",
    "subject_starts": "news:",
    "moved_to": "Shop",
}


def header(subject: str, address: str = "no-reply@shop.example") -> MessageHeader:
    return MessageHeader(
        uid="1",
        sender="Shop",
        sender_address=address,
        recipient="",
        recipient_address="",
        subject=subject,
        date="",
        is_unread=True,
    )


@pytest.fixture
def rules(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps([BY_CONTENT, BY_PREFIX]))
    return filing_rules.load(path)


def test_matches_the_subject_anywhere_ignoring_case(rules):
    message = header("Re: YOUR Order has shipped")
    assert rules.is_leaving(ACCOUNT, "INBOX", message)


def test_matches_a_prefix_only_at_the_start(rules):
    assert rules.is_leaving(ACCOUNT, "INBOX", header("  News: autumn sale"))
    assert not rules.is_leaving(ACCOUNT, "INBOX", header("Fwd: news: autumn sale"))


def test_other_mail_from_that_sender_stays(rules):
    assert not rules.is_leaving(ACCOUNT, "INBOX", header("Rate your purchase"))


def test_same_subject_from_someone_else_stays(rules):
    message = header("your order", address="friend@elsewhere.example")
    assert not rules.is_leaving(ACCOUNT, "INBOX", message)


def test_sender_matches_on_the_display_name_too(rules):
    message = MessageHeader(
        uid="1",
        sender="shop.example support",
        sender_address="a@b.example",
        recipient="",
        recipient_address="",
        subject="your order",
        date="",
        is_unread=True,
    )
    assert rules.is_leaving(ACCOUNT, "INBOX", message)


def test_another_account_is_left_alone(rules):
    assert not rules.is_leaving("other@example.com", "INBOX", header("your order"))
    assert not rules.is_destination("other@example.com", "Shop")
    assert not rules.may_count_leaving_mail("other@example.com", "INBOX")


def test_account_ignores_case(rules):
    assert rules.is_leaving("ME@example.com", "INBOX", header("your order"))


def test_only_the_inbox_hides_mail(rules):
    assert not rules.is_leaving(ACCOUNT, "Shop", header("your order"))
    assert not rules.is_leaving(ACCOUNT, "Archive", header("your order"))
    assert rules.is_leaving(ACCOUNT, "Inbox", header("your order"))


def test_destination_is_quiet(rules):
    assert rules.is_destination(ACCOUNT, "Shop")
    assert not rules.is_destination(ACCOUNT, "INBOX")


def test_unread_count_of_the_inbox_may_include_leaving_mail(rules):
    assert rules.may_count_leaving_mail(ACCOUNT, "INBOX")
    assert not rules.may_count_leaving_mail(ACCOUNT, "Shop")


def test_no_file_hides_nothing(tmp_path):
    rules = filing_rules.load(tmp_path / "absent.json")
    assert not rules.is_leaving(ACCOUNT, "INBOX", header("your order"))


@pytest.mark.parametrize("content", ["not json", '{"a": 1}', '"text"', "3"])
def test_a_broken_file_hides_nothing(tmp_path, content):
    path = tmp_path / "rules.json"
    path.write_text(content)
    rules = filing_rules.load(path)
    assert not rules.is_leaving(ACCOUNT, "INBOX", header("your order"))


@pytest.mark.parametrize(
    "missing", ["account", "from", "moved_to", "subject"], ids=lambda key: f"no {key}"
)
def test_an_incomplete_rule_is_ignored_not_guessed_at(tmp_path, missing):
    entry = {key: value for key, value in BY_CONTENT.items() if key != missing}
    path = tmp_path / "rules.json"
    path.write_text(json.dumps([entry, "nonsense", 3]))
    rules = filing_rules.load(path)
    assert rules.rules == ()
    assert not rules.is_leaving(ACCOUNT, "INBOX", header("your order"))


def test_an_empty_sender_cannot_match_everything(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps([{**BY_CONTENT, "from": "  "}]))
    assert filing_rules.load(path).rules == ()


def test_default_path_follows_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    expected = tmp_path / "postcard" / "filed-elsewhere.json"
    assert filing_rules.default_path() == expected
