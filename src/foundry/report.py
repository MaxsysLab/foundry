"""Find the report a worker was asked to end its answer with.

The verifier and the reviewer are told to answer with one JSON object. In practice
there may be prose around it, and the prose may hold braces of its own (`{port}`), or
an earlier attempt at the report, or a JSON example after it. Taking everything from
the first brace to the last breaks on all three. The report is the last JSON object in
the answer that can be read and that carries the key a report must have.
"""

from __future__ import annotations

import json
from typing import Any


def last_json_object(raw: str, key: str = "verdict") -> dict[str, Any] | None:
    """The last top-level JSON object in `raw` that has `key`. When none has it, the
    last object of any kind; None when there is no object at all."""
    decoder = json.JSONDecoder()
    found: dict[str, Any] | None = None
    other: dict[str, Any] | None = None
    start = raw.find("{")
    while start != -1:
        try:
            value, end = decoder.raw_decode(raw, start)
        except json.JSONDecodeError:
            start = raw.find("{", start + 1)
            continue
        if isinstance(value, dict):
            if key in value:
                found = value
            else:
                other = value
        start = raw.find("{", end)
    return found if found is not None else other
