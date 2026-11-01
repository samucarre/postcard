"""Show the pictures a message carries inside itself.

A multipart/related message ships its images as sibling parts and points at
them from the HTML with `cid:` URLs (RFC 2392) -- some senders, banks and
public bodies among them, send nothing but one such image. The reader's
WebView cannot resolve `cid:`, and its CSP only lets `data:` images through, so
every reference that names an image part is rewritten into a `data:` URI built
from that part.
"""

import base64
import re
from email.message import EmailMessage
from urllib.parse import unquote

# A cid: URL inside an attribute or a CSS url(): src="cid:x", background=cid:x,
# url('cid:x'). Plain text that happens to read "cid:" is left alone.
_CID_REF = re.compile(r"""(?P<lead>["'(=]\s*)cid:(?P<cid>[^"'\s)>]+)""", re.IGNORECASE)


def content_id(part: EmailMessage) -> str | None:
    """The part's Content-ID without its angle brackets, or None."""
    raw = part.get("Content-ID")
    if raw is None:
        return None
    cid = str(raw).strip().removeprefix("<").removesuffix(">").strip()
    return cid or None


def inline_cid_images(html: str, parts: list[EmailMessage]) -> tuple[str, set[int]]:
    """Rewrite the html's cid: references to the image parts they name.

    Returns the new html and the ids (`id()`) of the parts it inlined, so the
    caller can keep them out of the attachment list.
    """
    by_cid: dict[str, EmailMessage] = {}
    for part in parts:
        cid = content_id(part)
        if cid and part.get_content_maintype() == "image":
            by_cid.setdefault(cid, part)
            by_cid.setdefault(cid.lower(), part)
    if not by_cid:
        return html, set()

    used: set[int] = set()
    uris: dict[int, str] = {}

    def replace(match: re.Match[str]) -> str:
        # The URL form is url-encoded (RFC 2392); senders are not always exact
        # about case either, so an exact match wins and a folded one follows.
        cid = unquote(match["cid"])
        part = by_cid.get(cid) or by_cid.get(cid.lower())
        if part is None:
            return match[0]
        key = id(part)
        if key not in uris:
            uris[key] = _data_uri(part)
        used.add(key)
        return match["lead"] + uris[key]

    return _CID_REF.sub(replace, html), used


def _data_uri(part: EmailMessage) -> str:
    content = part.get_payload(decode=True)
    data = content if isinstance(content, bytes) else b""
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{part.get_content_type()};base64,{encoded}"
