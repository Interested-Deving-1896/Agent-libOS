from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_libos.evidence.payload_retention import (
    PayloadRetentionTier,
    llm_call_payload_is_runtime_dependency,
    retain_llm_call_payload,
)
from agent_libos.models.llm import LLMCallRecord
from agent_libos.storage.sqlite import SQLiteStore
from agent_libos.tools.builtin.context import _read_child_summary_from_llm_calls


def test_retention_protects_actual_latest_responses_chain_and_tool_call_recovery() -> None:
    store = SQLiteStore(":memory:")
    try:
        store.insert_llm_call(
            LLMCallRecord(
                call_id="older-call",
                pid="pid-chain",
                image_id="base-agent:v0",
                purpose="action_selection",
                status="ok",
                api="responses",
                response_id="resp-older",
                messages=[],
                tools=[],
                request_options={"openai_provider_chain_eligible": True},
                response_content="older",
                tool_calls=[],
                created_at="2026-01-01T00:00:00+00:00",
                completed_at="2026-01-01T00:00:01+00:00",
            )
        )
        store.insert_llm_call(
            LLMCallRecord(
                call_id="live-chain-head",
                pid="pid-chain",
                image_id="base-agent:v0",
                purpose="action_selection",
                status="ok",
                api="responses",
                response_id="resp-live",
                messages=[],
                tools=[],
                request_options={"openai_provider_chain_eligible": True},
                response_content="latest",
                tool_calls=[],
                created_at="2026-01-01T00:00:02+00:00",
                completed_at="2026-01-01T00:00:03+00:00",
            )
        )

        latest = store.get_latest_llm_call(
            pid="pid-chain",
            purpose="action_selection",
        )

        assert latest is not None and latest.call_id == "live-chain-head"
        assert llm_call_payload_is_runtime_dependency(latest)
        with pytest.raises(ValueError, match="runtime-dependent"):
            retain_llm_call_payload(latest, PayloadRetentionTier.SUMMARY)
        older = store.get_llm_call("older-call")
        assert older is not None
        assert not llm_call_payload_is_runtime_dependency(
            older,
            provider_chain_head=False,
        )
        assert (
            retain_llm_call_payload(
                older,
                PayloadRetentionTier.SUMMARY,
                provider_chain_head=False,
            ).call_id
            == older.call_id
        )

        recovered_payload = {"summary": "durable child result"}
        store.insert_llm_call(
            LLMCallRecord(
                call_id="compressor-exit",
                pid="pid-compressor-child",
                image_id="context-compressor:v0",
                purpose="action_selection",
                status="ok",
                messages=[],
                tools=[],
                response_content="",
                tool_calls=[
                    {
                        "name": "process_exit",
                        "arguments": {"payload": recovered_payload},
                    }
                ],
                created_at="2026-01-01T00:00:04+00:00",
                completed_at="2026-01-01T00:00:05+00:00",
            )
        )
        runtime_view = SimpleNamespace(store=store)
        child_call = store.get_latest_llm_call(
            pid="pid-compressor-child",
            purpose="action_selection",
        )

        assert child_call is not None
        assert llm_call_payload_is_runtime_dependency(child_call)
        assert (
            _read_child_summary_from_llm_calls(
                runtime_view,
                "pid-compressor-child",
            )
            == recovered_payload
        )
        with pytest.raises(ValueError, match="runtime-dependent"):
            retain_llm_call_payload(child_call, PayloadRetentionTier.SUMMARY)
        assert (
            _read_child_summary_from_llm_calls(
                runtime_view,
                "pid-compressor-child",
            )
            == recovered_payload
        )
    finally:
        store.close()
