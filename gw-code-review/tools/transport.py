"""Hard MCP-envelope bounds with deterministic overflow recovery."""

from __future__ import annotations

import hashlib
import json

MAX_MCP_RESPONSE_BYTES = 64_000
PAGE_CONTENT_BYTES = 20_000

_outputs: dict[tuple[str, str], dict] = {}
_cursors: dict[str, tuple[str, str, int]] = {}


def json_text(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def envelope_bytes(text: str) -> int:
    """Size of the MCP SDK envelope produced for a string tool result."""
    envelope = {
        "content": [{"type": "text", "text": text}],
        "structuredContent": {"result": text},
        "isError": False,
        "resultType": "complete",
    }
    return len(json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _chunks(text: str) -> list[str]:
    chunks: list[str] = []
    remaining = text
    while remaining:
        raw = remaining.encode("utf-8")
        if len(raw) <= PAGE_CONTENT_BYTES:
            chunks.append(remaining)
            break
        piece = raw[:PAGE_CONTENT_BYTES].decode("utf-8", errors="ignore")
        chunks.append(piece)
        remaining = remaining[len(piece):]
    return chunks or [""]


def _cursor(review_id: str, item_id: str, index: int) -> str:
    token = "page_" + hashlib.sha256(f"{review_id}\0{item_id}\0{index}".encode()).hexdigest()[:20]
    _cursors[token] = (review_id, item_id, index)
    return token


def respond(value: object, endpoint: str, review_id: str = "global") -> str:
    """Serialize a result or replace it with a bounded retrievable descriptor."""
    text = value if isinstance(value, str) else json_text(value)
    measured = envelope_bytes(text)
    if measured <= MAX_MCP_RESPONSE_BYTES:
        return text

    item_id = "output_" + hashlib.sha256(
        f"{review_id}\0{endpoint}\0".encode() + text.encode("utf-8")
    ).hexdigest()[:20]
    pages = _chunks(text)
    _outputs[(review_id, item_id)] = {
        "endpoint": endpoint,
        "pages": pages,
        "payload_bytes": len(text.encode("utf-8")),
        "envelope_bytes": measured,
    }
    descriptor = {
        "code": "RESPONSE_COMPACTED",
        "endpoint": endpoint,
        "item_id": item_id,
        "payload_bytes": len(text.encode("utf-8")),
        "envelope_bytes": measured,
        "response_cap_bytes": MAX_MCP_RESPONSE_BYTES,
        "returned_items": 0,
        "total_items": len(pages),
        "omitted_reason": "serialized MCP response exceeded the hard transport cap",
        "cursor": _cursor(review_id, item_id, 0),
    }
    bounded = json_text(descriptor)
    if envelope_bytes(bounded) > MAX_MCP_RESPONSE_BYTES:  # pragma: no cover - fixed-size invariant
        raise RuntimeError("overflow descriptor exceeds MCP response cap")
    return bounded


def get_page(review_id: str, cursor: str) -> dict:
    location = _cursors.get(cursor)
    if not location or location[0] != review_id:
        return {"code": "UNKNOWN_CURSOR", "reason": "cursor is unknown for this review"}
    _, item_id, index = location
    record = _outputs.get((review_id, item_id))
    if record is None or not 0 <= index < len(record["pages"]):
        return {"code": "UNKNOWN_CURSOR", "reason": "cursor target is unavailable"}
    next_cursor = _cursor(review_id, item_id, index + 1) if index + 1 < len(record["pages"]) else None
    result = {
        "item_id": item_id,
        "endpoint": record["endpoint"],
        "page_index": index,
        "returned_items": 1,
        "total_items": len(record["pages"]),
        "content": record["pages"][index],
        "next_cursor": next_cursor,
        "payload_bytes": len(record["pages"][index].encode("utf-8")),
    }
    return result


def discard_review(review_id: str) -> None:
    for key in [key for key in _outputs if key[0] == review_id]:
        _outputs.pop(key, None)
    for cursor, location in list(_cursors.items()):
        if location[0] == review_id:
            _cursors.pop(cursor, None)
