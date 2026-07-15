from __future__ import annotations

from pathlib import Path

import pytest

from agent_libos.models import DataFlowDirection
from agent_libos.models.exceptions import ValidationError
from agent_libos.sdk import ProtectedOperationInvocation
from scripts.check_protected_operations import check_tree, scan_source
from tests.support.runtime import temporary_runtime


def test_provider_subsystems_do_not_call_effect_lifecycle_directly() -> None:
    root = Path(__file__).resolve().parents[2]
    assert check_tree(root) == []


def test_static_check_rejects_direct_effect_lifecycle(tmp_path: Path) -> None:
    source = tmp_path / "bad_provider.py"
    source.write_text(
        "from agent_libos.runtime.external_effects import record_external_effect\n"
        "def unsafe(store):\n"
        "    return record_external_effect(store)\n",
        encoding="utf-8",
    )
    errors = scan_source(source, relative=Path("agent_libos/primitives/bad_provider.py"))
    assert any("direct import" in error for error in errors)
    assert any("direct record_external_effect call" in error for error in errors)


def test_static_check_rejects_provider_call_outside_sdk_phase(tmp_path: Path) -> None:
    source = tmp_path / "bad_provider.py"
    source.write_text(
        "class UnsafePrimitive:\n"
        "    def unsafe(self):\n"
        "        return self.provider.call()\n"
        "    def disguise(self, client):\n"
        "        return client.call(None, self.unsafe)\n",
        encoding="utf-8",
    )
    errors = scan_source(source, relative=Path("agent_libos/primitives/bad_provider.py"))
    assert any("outside an active ProtectedOperation phase" in error for error in errors)


def test_static_check_rejects_protected_provider_helper_called_directly(tmp_path: Path) -> None:
    source = tmp_path / "bad_helper.py"
    source.write_text(
        "class UnsafePrimitive:\n"
        "    def provider_phase(self):\n"
        "        return self.provider.call()\n"
        "    def protected(self, operation):\n"
        "        return operation.call(ProviderPhase('call'), self.provider_phase)\n"
        "    def unsafe(self):\n"
        "        return self.provider_phase()\n",
        encoding="utf-8",
    )
    errors = scan_source(source, relative=Path("agent_libos/primitives/bad_helper.py"))
    assert any("provider helper provider_phase is called outside" in error for error in errors)


def test_static_check_rejects_provider_handle_call_outside_sdk_phase(tmp_path: Path) -> None:
    source = tmp_path / "bad_handle.py"
    source.write_text(
        "class UnsafePrimitive:\n"
        "    def unsafe(self, session):\n"
        "        return session.handle.read()\n",
        encoding="utf-8",
    )
    errors = scan_source(source, relative=Path("modules/bad_handle.py"))
    assert any("provider handle method read" in error for error in errors)


def test_static_check_rejects_egress_without_sink_and_source_descriptors(tmp_path: Path) -> None:
    source = tmp_path / "bad_egress.py"
    source.write_text(
        "class UnsafePrimitive:\n"
        "    def call(self):\n"
        "        invocation = ProtectedOperationInvocation(pid='p', actor='p', target='llm:x')\n"
        "        return self.protected.start('primitive.llm.complete', invocation, provider=self.provider)\n",
        encoding="utf-8",
    )

    errors = scan_source(source, relative=Path("agent_libos/primitives/bad_egress.py"))

    assert any("missing data-flow descriptor fields" in error for error in errors)


def test_static_check_rejects_ingress_without_trusted_context(tmp_path: Path) -> None:
    source = tmp_path / "bad_ingress.py"
    source.write_text(
        "class UnsafePrimitive:\n"
        "    def call(self):\n"
        "        invocation = ProtectedOperationInvocation(pid='p', actor='p', target='file:x')\n"
        "        return self.protected.start('primitive.filesystem.read_text', invocation, provider=self.provider)\n",
        encoding="utf-8",
    )

    errors = scan_source(source, relative=Path("agent_libos/primitives/bad_ingress.py"))

    assert any("missing ingress data-flow descriptor field" in error for error in errors)


def test_contract_registry_matches_explainable_external_primitive_boundaries() -> None:
    with temporary_runtime() as runtime:
        contracts = {contract.name for contract in runtime.protected_operations.contracts()}
        assert contracts == set(runtime.external_primitive_boundary_names)
        assert contracts <= set(runtime.explainable_boundary_names)
        assert all(
            set(contract.evidence_roles) == {"audit", "event", "effect"}
            for contract in runtime.protected_operations.contracts()
        )


def test_contract_registry_declares_explicit_data_flow_directions() -> None:
    expected = {
        "primitive.filesystem.read_text": DataFlowDirection.INGRESS,
        "primitive.filesystem.read_bytes": DataFlowDirection.INGRESS,
        "primitive.filesystem.write_text": DataFlowDirection.EGRESS,
        "primitive.filesystem.read_directory": DataFlowDirection.INGRESS,
        "primitive.filesystem.write_directory": DataFlowDirection.EGRESS,
        "primitive.filesystem.delete_file": DataFlowDirection.EGRESS,
        "primitive.filesystem.delete_directory": DataFlowDirection.EGRESS,
        "primitive.shell.run": DataFlowDirection.BIDIRECTIONAL,
        "primitive.jsonrpc.call": DataFlowDirection.BIDIRECTIONAL,
        "primitive.mcp.list_tools": DataFlowDirection.BIDIRECTIONAL,
        "primitive.mcp.list_tools.internal": DataFlowDirection.BIDIRECTIONAL,
        "primitive.mcp.call": DataFlowDirection.BIDIRECTIONAL,
        "primitive.llm.complete": DataFlowDirection.BIDIRECTIONAL,
        "primitive.human.read": DataFlowDirection.BIDIRECTIONAL,
        "primitive.human.write": DataFlowDirection.EGRESS,
        "primitive.pty.spawn": DataFlowDirection.BIDIRECTIONAL,
        "primitive.pty.read": DataFlowDirection.INGRESS,
        "primitive.pty.ingest": DataFlowDirection.INGRESS,
        "primitive.pty.write": DataFlowDirection.EGRESS,
        "primitive.pty.resize": DataFlowDirection.EGRESS,
        "primitive.pty.close": DataFlowDirection.EGRESS,
    }
    with temporary_runtime() as runtime:
        actual = {
            contract.name: contract.data_flow_direction
            for contract in runtime.protected_operations.contracts()
            if contract.data_flow_direction is not DataFlowDirection.NONE
        }
        assert actual == expected


def test_sdk_rejects_egress_without_concrete_descriptors_before_effect_intent() -> None:
    with temporary_runtime() as runtime:
        pid = runtime.process.spawn(goal="reject missing egress descriptor")
        invocation = ProtectedOperationInvocation(
            pid=pid,
            actor=pid,
            target="llm:default",
            data_flow_ingress_context=runtime.data_flow.current_context(),
        )

        with pytest.raises(ValidationError, match="concrete DataSink"):
            with runtime.protected_operations.start(
                "primitive.llm.complete",
                invocation,
                provider=runtime.llm.client,
            ):
                pass

        assert runtime.store.list_external_effects(pid=pid) == []
