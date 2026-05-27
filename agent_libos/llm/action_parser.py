from __future__ import annotations

import json
from typing import Any


def parse_json_action(text: str) -> dict[str, Any]:
    return json.loads(text)

