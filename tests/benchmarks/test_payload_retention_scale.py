from __future__ import annotations

import pytest

from agent_libos.evidence.payload_retention import PayloadRetentionCursor
from agent_libos.storage import SQLiteStore


_OLD = "2020-01-01T00:00:00+00:00"
_ELIGIBLE = "2021-01-01T00:00:00+00:00"
_CUTOFF = "2022-01-01T00:00:00+00:00"
_HISTORY_ROWS_PER_CLASS = 5_000
_DEEP_ELIGIBLE_ROWS = 10_000
_LLM_INSERT = """
    INSERT INTO llm_calls (
        call_id, purpose, status, messages_json, tools_json,
        request_options_json, response_content, tool_calls_json,
        usage_json, observability_json, created_at, completed_at,
        payload_retention_tier
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
_EFFECT_INSERT = """
    INSERT INTO external_effects (
        effect_id, pid, provider, operation, rollback_class,
        rollback_status, state_mutation, information_flow,
        provider_metadata_json, created_at, effect_state,
        transaction_state, provider_receipt_json,
        payload_retention_tier, payload_retention_sha256
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
_EMPTY_JSON = "{}"


def test_retention_query_work_ignores_large_nonterminal_and_hash_only_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteStore(":memory:")
    try:
        _seed_ineligible_history(store)
        captured: list[tuple[str, tuple[object, ...], int]] = []
        original_query = store._query

        def tracked_query(sql: str, params: object = ()) -> list[object]:
            selected_params = tuple(params)  # type: ignore[arg-type]
            rows = original_query(sql, selected_params)
            if "payload_retention_tier IN" in sql and "LIMIT" in sql:
                captured.append((sql, selected_params, len(rows)))
            return rows

        monkeypatch.setattr(store, "_query", tracked_query)

        llm_page = store.scan_llm_call_payloads_for_retention(
            older_than=_CUTOFF,
            after=None,
            limit=2,
        )
        effect_page = store.scan_external_effect_payloads_for_retention(
            older_than=_CUTOFF,
            after=None,
            limit=2,
        )

        assert [record.call_id for record in llm_page.records] == [
            "eligible-call-0",
            "eligible-call-1",
        ]
        assert [record.effect_id for record in effect_page.records] == [
            "eligible-effect-0",
            "eligible-effect-1",
        ]
        assert llm_page.next_cursor is not None
        assert effect_page.next_cursor is not None
        assert [raw_rows for _sql, _params, raw_rows in captured] == [3, 3]

        expected_indexes = (
            "idx_llm_calls_retention_eligible",
            "idx_external_effects_retention_eligible",
        )
        for (sql, params, _raw_rows), expected_index in zip(
            captured,
            expected_indexes,
            strict=True,
        ):
            plan_rows = list(
                store.conn.execute(f"EXPLAIN QUERY PLAN {sql}", params)
            )
            plan = "\n".join(str(row["detail"]) for row in plan_rows)
            assert expected_index in plan
    finally:
        store.close()


def test_resumed_retention_pages_seek_to_deep_composite_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteStore(":memory:")
    try:
        _seed_deep_eligible_history(store)
        captured: list[tuple[str, tuple[object, ...], int]] = []
        progress_callbacks: list[int] = []
        original_query = store._query

        def tracked_query(sql: str, params: object = ()) -> list[object]:
            selected_params = tuple(params)  # type: ignore[arg-type]
            if "payload_retention_tier IN" not in sql or "LIMIT" not in sql:
                return original_query(sql, selected_params)
            callbacks = 0

            def count_work() -> int:
                nonlocal callbacks
                callbacks += 1
                return 0

            store.conn.set_progress_handler(count_work, 100)
            try:
                rows = original_query(sql, selected_params)
            finally:
                store.conn.set_progress_handler(None, 0)
            captured.append((sql, selected_params, len(rows)))
            progress_callbacks.append(callbacks)
            return rows

        monkeypatch.setattr(store, "_query", tracked_query)
        after_index = _DEEP_ELIGIBLE_ROWS - 11
        llm_page = store.scan_llm_call_payloads_for_retention(
            older_than=_CUTOFF,
            after=PayloadRetentionCursor(
                _ELIGIBLE,
                f"deep-call-{after_index:05d}",
            ),
            limit=2,
        )
        effect_page = store.scan_external_effect_payloads_for_retention(
            older_than=_CUTOFF,
            after=PayloadRetentionCursor(
                _ELIGIBLE,
                f"deep-effect-{after_index:05d}",
            ),
            limit=2,
        )

        assert [record.call_id for record in llm_page.records] == [
            f"deep-call-{after_index + 1:05d}",
            f"deep-call-{after_index + 2:05d}",
        ]
        assert [record.effect_id for record in effect_page.records] == [
            f"deep-effect-{after_index + 1:05d}",
            f"deep-effect-{after_index + 2:05d}",
        ]
        assert [raw_rows for _sql, _params, raw_rows in captured] == [3, 3]
        # A proper composite seek executes only the candidate lookup and three
        # primary-key materializations. The old OR-expanded cursor filtered a
        # nearly 10k-row prefix on every resumed page and exceeds this budget by
        # orders of magnitude.
        assert progress_callbacks and max(progress_callbacks) < 20

        expected_ranges = (
            (
                "idx_llm_calls_retention_eligible",
                "(created_at,call_id)>(?,?)",
            ),
            (
                "idx_external_effects_retention_eligible",
                "(created_at,effect_id)>(?,?)",
            ),
        )
        for (sql, params, _raw_rows), (expected_index, expected_range) in zip(
            captured,
            expected_ranges,
            strict=True,
        ):
            plan_rows = list(
                store.conn.execute(f"EXPLAIN QUERY PLAN {sql}", params)
            )
            plan = "\n".join(str(row["detail"]) for row in plan_rows)
            normalized_plan = plan.replace(" ", "")
            assert expected_index in plan
            assert expected_range in normalized_plan
    finally:
        store.close()


def _seed_ineligible_history(store: SQLiteStore) -> None:
    with store.transaction() as cursor:
        cursor.executemany(
            _LLM_INSERT,
            [
                (
                    f"nonterminal-call-{index:05d}",
                    "provider_observation",
                    "streaming",
                    _EMPTY_JSON,
                    _EMPTY_JSON,
                    _EMPTY_JSON,
                    "",
                    _EMPTY_JSON,
                    _EMPTY_JSON,
                    _EMPTY_JSON,
                    _OLD,
                    None,
                    "full",
                )
                for index in range(_HISTORY_ROWS_PER_CLASS)
            ]
            + [
                (
                    f"hash-call-{index:05d}",
                    "provider_observation",
                    "ok",
                    _EMPTY_JSON,
                    _EMPTY_JSON,
                    _EMPTY_JSON,
                    "",
                    _EMPTY_JSON,
                    _EMPTY_JSON,
                    _EMPTY_JSON,
                    _OLD,
                    _OLD,
                    "hash_only",
                )
                for index in range(_HISTORY_ROWS_PER_CLASS)
            ]
            + [
                (
                    f"eligible-call-{index}",
                    "provider_observation",
                    "ok",
                    _EMPTY_JSON,
                    _EMPTY_JSON,
                    _EMPTY_JSON,
                    "",
                    _EMPTY_JSON,
                    _EMPTY_JSON,
                    _EMPTY_JSON,
                    _ELIGIBLE,
                    _ELIGIBLE,
                    "full",
                )
                for index in range(3)
            ],
        )
        cursor.executemany(
            _EFFECT_INSERT,
            [
                (
                    f"pending-effect-{index:05d}",
                    "pid-retention-scale",
                    "scale-provider",
                    "write",
                    "unknown",
                    "unknown",
                    0,
                    0,
                    _EMPTY_JSON,
                    _OLD,
                    "pending",
                    "unknown",
                    _EMPTY_JSON,
                    "full",
                    None,
                )
                for index in range(_HISTORY_ROWS_PER_CLASS)
            ]
            + [
                (
                    f"hash-effect-{index:05d}",
                    "pid-retention-scale",
                    "scale-provider",
                    "write",
                    "no_rollback_required",
                    "not_required",
                    1,
                    0,
                    _EMPTY_JSON,
                    _OLD,
                    "finalized",
                    "committed",
                    _EMPTY_JSON,
                    "hash_only",
                    "a" * 64,
                )
                for index in range(_HISTORY_ROWS_PER_CLASS)
            ]
            + [
                (
                    f"eligible-effect-{index}",
                    "pid-retention-scale",
                    "scale-provider",
                    "write",
                    "no_rollback_required",
                    "not_required",
                    1,
                    0,
                    _EMPTY_JSON,
                    _ELIGIBLE,
                    "finalized",
                    "committed",
                    _EMPTY_JSON,
                    "full",
                    None,
                )
                for index in range(3)
            ],
        )


def _seed_deep_eligible_history(store: SQLiteStore) -> None:
    with store.transaction() as cursor:
        cursor.executemany(
            _LLM_INSERT,
            [
                (
                    f"deep-call-{index:05d}",
                    "provider_observation",
                    "ok",
                    _EMPTY_JSON,
                    _EMPTY_JSON,
                    _EMPTY_JSON,
                    "",
                    _EMPTY_JSON,
                    _EMPTY_JSON,
                    _EMPTY_JSON,
                    _ELIGIBLE,
                    _ELIGIBLE,
                    "full",
                )
                for index in range(_DEEP_ELIGIBLE_ROWS)
            ],
        )
        cursor.executemany(
            _EFFECT_INSERT,
            [
                (
                    f"deep-effect-{index:05d}",
                    "pid-retention-scale",
                    "scale-provider",
                    "write",
                    "no_rollback_required",
                    "not_required",
                    1,
                    0,
                    _EMPTY_JSON,
                    _ELIGIBLE,
                    "finalized",
                    "committed",
                    _EMPTY_JSON,
                    "full",
                    None,
                )
                for index in range(_DEEP_ELIGIBLE_ROWS)
            ],
        )
