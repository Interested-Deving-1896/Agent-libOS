from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from agent_libos import Runtime
from agent_libos.config import DEFAULT_CONFIG
from agent_libos.llm.client import LLMCompletion
from agent_libos.models import ProcessStatus
from agent_libos.serde import to_jsonable
from scripts.llm_context_probe import last_tool_result, recent_events

_RUNTIME_DEFAULTS = DEFAULT_CONFIG.runtime
_SCRIPT_DEFAULTS = DEFAULT_CONFIG.scripts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ask the human which workspace file to view, then show that file's content through HumanObject output."
    )
    parser.add_argument(
        "--db",
        default=_RUNTIME_DEFAULTS.local_store_target,
        help=f"Runtime SQLite database path, or '{_RUNTIME_DEFAULTS.local_store_target}' for in-memory.",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=_SCRIPT_DEFAULTS.ask_file_max_bytes,
        help="Maximum bytes to read from the selected file.",
    )
    parser.add_argument(
        "--max-quanta",
        type=int,
        default=_SCRIPT_DEFAULTS.ask_file_max_quanta,
        help="Maximum Agent execution quanta to run.",
    )
    parser.add_argument(
        "--auto-answer",
        default=None,
        help="Non-interactive answer to the file-name question, for example README.md.",
    )
    args = parser.parse_args()
    report = asyncio.run(
        run_file_viewer(
            db=args.db,
            max_bytes=args.max_bytes,
            max_quanta=args.max_quanta,
            auto_answer=args.auto_answer,
        )
    )
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))


async def run_file_viewer(
    *,
    db: str = _RUNTIME_DEFAULTS.local_store_target,
    max_bytes: int = _SCRIPT_DEFAULTS.ask_file_max_bytes,
    max_quanta: int = _SCRIPT_DEFAULTS.ask_file_max_quanta,
    auto_answer: str | None = None,
    echo: bool = True,
) -> dict[str, Any]:
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    runtime = Runtime.open(db)
    outputs: list[str] = []
    client = AskFileViewerClient(max_bytes=max_bytes)
    runtime.llm.client = client

    def output_sink(message: str) -> None:
        outputs.append(message)
        if echo:
            print(message, flush=True)

    runtime.human.output_sink = output_sink
    try:
        pid = runtime.process.spawn(
            image=_RUNTIME_DEFAULTS.coding_image_id,
            goal=(
                "Ask the human which workspace file they want to view. Read that file and show its content "
                "to the human. If reading fails, show the failure reason to the human. Then exit."
            ),
        )
        results = await runtime.arun_until_idle(
            max_quanta=max_quanta,
            human_auto_answer=auto_answer,
        )
        process = runtime.process.get(pid)
        report = {
            "pid": pid,
            "selected_path": client.selected_path,
            "displayed": client.displayed,
            "error": client.error,
            "process_status": process.status.value,
            "actions": [_action_name(result) for result in results],
            "outputs": outputs,
            "model_calls": client.calls,
            "results": to_jsonable(results),
        }
        if process.status != ProcessStatus.EXITED:
            raise RuntimeError(f"process did not exit after {max_quanta} quanta; status={process.status.value}")
        return report
    finally:
        runtime.close()


class AskFileViewerClient:
    def __init__(self, *, max_bytes: int):
        self.max_bytes = max_bytes
        self.calls = 0
        self.step = 0
        self.selected_path: str | None = None
        self.displayed = False
        self.error: str | None = None

    def complete_action(self, messages: list[dict[str, str]], tools: list[dict[str, object]]) -> LLMCompletion:
        self.calls += 1
        if self.step == 0:
            # Drive the process through real human/file primitives while keeping
            # this script deterministic and testable without a model call.
            self.step = 1
            return self._completion(
                "ask_human",
                {
                    "question": "Which workspace file do you want to view?",
                    "context": {"path_rule": "Use a path under the runtime workspace root."},
                },
            )
        if self.step == 1:
            answer = self._last_tool_result(messages, "ask_human").get("answer")
            if not isinstance(answer, str) or not answer.strip():
                raise AssertionError("ask_human result did not include a non-empty answer")
            self.selected_path = answer.strip()
            self.step = 2
            return self._completion(
                "read_text_file",
                {"path": self.selected_path, "max_bytes": self.max_bytes},
            )
        if self.step == 2:
            read_result = self._last_tool_result(messages, "read_text_file", required=False)
            if read_result is None:
                self.error = self._last_tool_error(messages) or "read_text_file failed without a visible error"
                message = f"Could not read {self.selected_path!r}: {self.error}"
            else:
                content = str(read_result.get("content", ""))
                truncated = bool(read_result.get("truncated", False))
                suffix = "\n\n[content truncated]" if truncated else ""
                message = f"----- {self.selected_path} -----\n{content}{suffix}"
                self.displayed = True
            self.step = 3
            return self._completion("human_output", {"message": message})
        if self.step == 3:
            self.step = 4
            return self._completion(
                "process_exit",
                {
                    "payload": {
                        "selected_path": self.selected_path,
                        "displayed": self.displayed,
                        "error": self.error,
                    }
                },
            )
        raise AssertionError("file viewer action plan is already complete")

    def _completion(self, name: str, args: dict[str, Any]) -> LLMCompletion:
        return LLMCompletion(
            content="",
            tool_calls=[{"id": f"file_viewer_{self.calls}", "name": name, "arguments": json.dumps(args)}],
        )

    def _last_tool_result(
        self,
        messages: list[dict[str, str]],
        tool_name: str,
        *,
        required: bool = True,
    ) -> dict[str, Any] | None:
        result = last_tool_result(messages, tool_name)
        if result is not None:
            return result
        if required:
            raise AssertionError(f"no visible result for {tool_name}")
        return None

    def _last_tool_error(self, messages: list[dict[str, str]]) -> str | None:
        for event in reversed(recent_events(messages)):
            if event.get("type") != "tool_failed":
                continue
            payload = event.get("payload")
            if isinstance(payload, dict) and isinstance(payload.get("error"), str):
                return payload["error"]
        return None


def _action_name(result: object) -> str | None:
    if not isinstance(result, dict):
        return None
    action = result.get("action")
    if isinstance(action, dict):
        return action.get("action")
    return None


if __name__ == "__main__":
    main()
