"""Small shared helpers for compressed JSON responses and traffic accounting."""

from __future__ import annotations

import gzip
import json
import threading
from typing import Any, BinaryIO


_response_bytes = 0
_lock = threading.Lock()


def read_json(response: BinaryIO) -> Any:
    """Read one HTTP JSON response and count its on-wire response body bytes."""
    raw = response.read()
    global _response_bytes
    with _lock:
        _response_bytes += len(raw)
    encoding = str(response.headers.get("Content-Encoding", "")).lower()
    if "gzip" in encoding:
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def total_response_bytes() -> int:
    with _lock:
        return _response_bytes
