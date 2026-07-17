from __future__ import annotations

import threading
from typing import Any

from agent_libos.models.exceptions import ValidationError


class HumanPresentationService:
    """Own per-provider presentation delivery receipts."""

    def __init__(self, *, receipt_limit: int) -> None:
        self._lock = threading.RLock()
        self._receipts: dict[tuple[str, str, int], tuple[str, Any]] = {}
        self._receipt_limit = max(1, receipt_limit)

    @staticmethod
    def normalize(value: str) -> str:
        selected = str(value).strip().lower()
        if selected != "gui":
            raise ValidationError(f"unsupported Human presentation: {value}")
        return selected

    def was_delivered(
        self,
        *,
        presentation: str,
        request_id: str,
        provider: Any,
        view_sha256: str,
    ) -> bool:
        key = (presentation, request_id, id(provider))
        with self._lock:
            receipt = self._receipts.get(key)
            delivered = bool(
                receipt is not None
                and receipt[0] == view_sha256
                and receipt[1] is provider
            )
            if delivered:
                self._receipts[key] = self._receipts.pop(key)
            return delivered

    def mark_delivered(
        self,
        *,
        presentation: str,
        request_id: str,
        provider: Any,
        view_sha256: str,
    ) -> None:
        key = (presentation, request_id, id(provider))
        with self._lock:
            self._receipts.pop(key, None)
            self._receipts[key] = (view_sha256, provider)
            while len(self._receipts) > self._receipt_limit:
                oldest = next(iter(self._receipts))
                self._receipts.pop(oldest, None)
