from __future__ import annotations

import contextlib
import io
import json
from typing import Any

from agent_libos.api.cli import main as cli_main
from agent_libos.models import (
    ExternalEffectClassification,
    ExternalEffectRollbackClass,
    ExternalEffectRollbackStatus,
)
from agent_libos.substrate import CommandResult


def checkpoint_cli_json(argv: list[str]) -> Any:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        cli_main(argv)
    return json.loads(buffer.getvalue())


class ClassifiedShellProvider:
    def run(self, argv: list[str], *, timeout: float = 30.0, cwd: str | None = None) -> CommandResult:
        return CommandResult(argv=list(argv), returncode=0, stdout="ok\n", stderr="")

    def classify_external_effect(self, operation: str, context: dict[str, Any], result: Any) -> ExternalEffectClassification:
        return ExternalEffectClassification(
            rollback_class=ExternalEffectRollbackClass.IRREVERSIBLE,
            rollback_status=ExternalEffectRollbackStatus.NOT_SUPPORTED,
            state_mutation=True,
            information_flow=True,
            metadata={"operation": operation},
        )
