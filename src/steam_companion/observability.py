from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any


request_id: ContextVar[str] = ContextVar("request_id", default="-")
logger = logging.getLogger("steam_companion")


def emit(level: int, **fields: Any) -> None:
    record = {
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "level": logging.getLevelName(level).lower(),
        "request_id": request_id.get(),
        **fields,
    }
    logger.log(level, json.dumps(record, separators=(",", ":"), ensure_ascii=True))
