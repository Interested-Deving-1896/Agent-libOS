from __future__ import annotations

import unittest

from agent_libos import Runtime
from agent_libos.api.cli import run_demo
from agent_libos.exceptions import CapabilityDenied, HumanApprovalRequired
from agent_libos.models import (
    ForkMode,
    MemoryViewSpec,
    ObjectMetadata,
    ObjectPatch,
    ObjectType,
    ProcessStatus,
    ViewMode,
)


class RuntimeMVPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = Runtime.open("local")

    def tearDown(self) -> None:
        self.runtime.close()

    def test_object_memory_materialization_and_fork_attenuation(self) -> None:
        root = self.runtime.process.spawn(goal="inspect failure")
        public = self.runtime.memory.create_object(
            root,
            ObjectType.ERROR_TRACE,
            {"log": "FAILED tests/test_math.py::test_add"},
            metadata=ObjectMetadata(title="public log"),
        )
        secret = self.runtime.memory.create_object(
            root,
            ObjectType.EVIDENCE,
            {"token": "secret"},
            metadata=ObjectMetadata(title="secret"),
        )
        root_proc = self.runtime.process.get(root)
        assert root_proc.memory_view is not None
        root_proc.memory_view.roots.extend([public, secret])
        self.runtime.store.update_process(root_proc)

        child = self.runtime.process.fork(
            root,
            goal="analyze log",
            memory_view=MemoryViewSpec(roots=[public], mode=ViewMode.READ_ONLY),
            mode=ForkMode.WORKER,
        )
        child_proc = self.runtime.process.get(child)
        assert child_proc.memory_view is not None
        materialized = self.runtime.memory.materialize_context(child, child_proc.memory_view, policy="error_debug")
        self.assertIn("FAILED", materialized.text)
        with self.assertRaises(CapabilityDenied):
            self.runtime.memory.get_object(child, secret)

    def test_human_approval_grants_tool_execute_capability(self) -> None:
        root = self.runtime.process.spawn(goal="run controlled tool")
        tool = self.runtime.tools.register_static("double", lambda args: args["value"] * 2)
        with self.assertRaises(HumanApprovalRequired) as raised:
            self.runtime.tools.call(root, tool, {"value": 4})
        self.assertEqual(self.runtime.process.get(root).status, ProcessStatus.WAITING_HUMAN)
        self.runtime.human.approve(raised.exception.request_id)
        result = self.runtime.tools.call(root, tool, {"value": 4})
        self.assertTrue(result.ok)
        self.assertEqual(result.payload, 8)
        self.assertEqual(self.runtime.process.get(root).status, ProcessStatus.RUNNABLE)

    def test_jit_tool_validation_registration_and_call(self) -> None:
        root = self.runtime.process.spawn(goal="make parser")
        candidate = self.runtime.tools.propose(
            root,
            {
                "name": "sum_values",
                "description": "Sum integer values.",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "number"},
            },
            source_code="def run(args):\n    return sum(args.get('values', []))\n",
            tests=[{"args": {"values": [1, 2, 3]}, "expected": 6}],
        )
        validation = self.runtime.tools.validate(candidate)
        self.assertTrue(validation.ok, validation.errors)
        handle = self.runtime.tools.register(root, candidate)
        result = self.runtime.tools.call(root, handle, {"values": [4, 5]})
        self.assertEqual(result.payload, 9)

    def test_checkpoint_rollback_restores_object_state(self) -> None:
        root = self.runtime.process.spawn(goal="checkpoint object")
        obj = self.runtime.memory.create_object(
            root,
            ObjectType.OBSERVATION,
            {"value": 1},
            immutable=False,
        )
        checkpoint = self.runtime.checkpoint.checkpoint(root, "before update")
        self.runtime.memory.update_object(root, obj, ObjectPatch(payload={"value": 2}))
        self.assertEqual(self.runtime.memory.get_object(root, obj).payload["value"], 2)
        self.runtime.checkpoint.rollback(root, checkpoint)
        self.assertEqual(self.runtime.memory.get_object(root, obj).payload["value"], 1)

    def test_cli_demo_flow(self) -> None:
        summary = run_demo(self.runtime)
        self.assertTrue(summary["jit_validation_ok"])
        self.assertIsNotNone(summary["approval_request"])
        self.assertGreater(summary["audit_records"], 10)


if __name__ == "__main__":
    unittest.main()

