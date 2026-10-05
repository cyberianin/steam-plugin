from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from steam_companion.errors import ServiceError


class SnapshotCursor:
    """Versioned HMAC cursor bound to a principal, filter set, collection and snapshot."""

    VERSION = 3

    @staticmethod
    def _filter_digest(filters: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(filters, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]

    @staticmethod
    def _encode(part: bytes) -> str:
        return base64.urlsafe_b64encode(part).decode().rstrip("=")

    @staticmethod
    def _decode(part: str) -> bytes:
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))

    @classmethod
    def encode(
        cls, offset: int, principal: str, filters: dict[str, Any], secret: bytes,
        snapshot_id: str, collection: str,
    ) -> str:
        payload = json.dumps({
            "v": cls.VERSION, "offset": offset, "principal": principal,
            "filters": cls._filter_digest(filters), "snapshot": snapshot_id,
            "collection": collection,
        }, sort_keys=True, separators=(",", ":")).encode()
        signature = hmac.new(secret, payload, hashlib.sha256).digest()[:16]
        return f"{cls._encode(payload)}.{cls._encode(signature)}"

    @classmethod
    def decode(
        cls, cursor: str, principal: str, filters: dict[str, Any], secret: bytes,
        snapshot_id: str, collection: str,
    ) -> int:
        try:
            encoded_payload, encoded_signature = cursor.split(".", 1)
            payload, signature = cls._decode(encoded_payload), cls._decode(encoded_signature)
            expected = hmac.new(secret, payload, hashlib.sha256).digest()[:16]
            document = json.loads(payload)
            if (
                not hmac.compare_digest(signature, expected)
                or not isinstance(document, dict)
                or set(document) != {"v", "offset", "principal", "filters", "snapshot", "collection"}
                or document["v"] != cls.VERSION
                or isinstance(document["offset"], bool)
                or not isinstance(document["offset"], int)
                or document["offset"] < 0
                or document["principal"] != principal
                or document["filters"] != cls._filter_digest(filters)
                or document["collection"] != collection
            ):
                raise ValueError
            if document["snapshot"] != snapshot_id:
                raise ServiceError("cursor_stale", "The collection changed after this cursor was issued; restart pagination.", 409)
            return document["offset"]
        except ServiceError:
            raise
        except (ValueError, TypeError, KeyError, json.JSONDecodeError, base64.binascii.Error):
            raise ServiceError("invalid_cursor", "The cursor is invalid or belongs to another principal or filter.", 400) from None
