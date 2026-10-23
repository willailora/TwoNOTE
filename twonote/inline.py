"""Portable inline references inside the existing encrypted content field.

No HTML execution or external resources. Older clients retain the readable tokens.
"""
import re

HEADER = "<!--TwoNote:inline:v1-->\n"
PATTERN = re.compile(r"!\[TwoNote\]\(twonote-embed://([0-9a-f]{64})\)")
BLOCK_MIME = "application/vnd.twonote.block+json"


def supported(item):
    mime = item.get("mime", "")
    return mime == BLOCK_MIME or (mime.startswith("image/") and mime != "image/svg+xml")


def token(identifier):
    if not re.fullmatch(r"[0-9a-f]{64}", identifier):
        raise ValueError("invalid inline attachment ID")
    return f"![TwoNote](twonote-embed://{identifier})"


def body(content, attachments=()):
    if content.startswith(HEADER):
        return content[len(HEADER):]
    # Old imports discarded positions. Never invent positions within the text.
    tail = [token(a["id"]) for a in attachments if supported(a)]
    return content + (("\n\n" if content else "") + "\n".join(tail) if tail else "")


def encode(text):
    return HEADER + text


def parts(text, attachments):
    allowed = {a["id"] for a in attachments if supported(a)}
    offset = 0
    for match in PATTERN.finditer(text):
        if match[1] not in allowed:
            continue
        if match.start() > offset:
            yield "text", text[offset:match.start()]
        yield "embed", match[1]
        offset = match.end()
    if offset < len(text):
        yield "text", text[offset:]


def merge(notes):
    return encode("\n\n--- 別の編集版 ---\n\n".join(dict.fromkeys(body(n["content"], n["attachments"]) for n in notes)))
