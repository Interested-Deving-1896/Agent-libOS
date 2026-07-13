from __future__ import annotations

from pathlib import Path

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


def test_contract_registry_matches_explainable_external_primitive_boundaries() -> None:
    with temporary_runtime() as runtime:
        contracts = {contract.name for contract in runtime.protected_operations.contracts()}
        assert contracts == set(runtime.external_primitive_boundary_names)
        assert contracts <= set(runtime.explainable_boundary_names)
        assert all(
            set(contract.evidence_roles) == {"audit", "event", "effect"}
            for contract in runtime.protected_operations.contracts()
        )
