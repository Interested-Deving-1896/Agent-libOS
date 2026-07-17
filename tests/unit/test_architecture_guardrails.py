from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import check_architecture as checker


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ALLOWLIST = PROJECT_ROOT / "scripts" / "architecture_allowlist.json"


def _write_source(root: Path, relative: str, source: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _write_current_baseline(root: Path) -> Path:
    path = root / "architecture_allowlist.json"
    path.write_text(
        json.dumps(checker.allowlist_for(checker.scan_architecture(root)), indent=2),
        encoding="utf-8",
    )
    return path


def _long_function(statement_count: int) -> str:
    return "def legacy_hotspot():\n" + "".join(
        f"    value_{index} = {index}\n" for index in range(statement_count)
    )


def _complex_function(branch_count: int) -> str:
    branches = "".join(
        f"    if value == {index}:\n        result += {index}\n"
        for index in range(branch_count)
    )
    return f"def legacy_hotspot(value):\n    result = 0\n{branches}    return result\n"


class TestArchitectureGuardrails:

    def test_checked_in_ratchet_accepts_the_current_tree(self) -> None:
        assert checker.check_architecture(PROJECT_ROOT, PROJECT_ALLOWLIST) == []

    @pytest.mark.parametrize(
        "package",
        [
            "models",
            "capability",
            "images",
            "primitives",
            "sdk",
            "human",
            "llm",
            "tools",
        ],
    )
    @pytest.mark.parametrize(
        "statement",
        [
            "from agent_libos.runtime.runtime import Runtime",
            "from ..runtime.runtime import Runtime",
            "from agent_libos import runtime",
        ],
    )
    def test_new_reverse_runtime_import_fails(
        self,
        tmp_path: Path,
        package: str,
        statement: str,
    ) -> None:
        relative = f"agent_libos/{package}/sample.py"
        _write_source(tmp_path, relative, "VALUE = 1\n")
        allowlist = _write_current_baseline(tmp_path)

        _write_source(
            tmp_path,
            relative,
            f"{statement}\n\nVALUE = 2\n",
        )

        errors = checker.check_architecture(tmp_path, allowlist)

        assert any("concrete-runtime-import" in error for error in errors)

    def test_lower_layer_api_import_fails(self, tmp_path: Path) -> None:
        relative = "agent_libos/tools/sample.py"
        _write_source(tmp_path, relative, "VALUE = 1\n")
        allowlist = _write_current_baseline(tmp_path)

        _write_source(
            tmp_path,
            relative,
            "from agent_libos.api.cli import main\n",
        )

        errors = checker.check_architecture(tmp_path, allowlist)

        assert any("concrete-api-import" in error for error in errors)

    def test_runtime_layer_api_import_fails(self, tmp_path: Path) -> None:
        relative = "agent_libos/runtime/service.py"
        _write_source(tmp_path, relative, "VALUE = 1\n")
        allowlist = _write_current_baseline(tmp_path)

        _write_source(
            tmp_path,
            relative,
            "from agent_libos.api.cli import main\n",
        )

        errors = checker.check_architecture(tmp_path, allowlist)

        assert any("concrete-api-import" in error for error in errors)

    @pytest.mark.parametrize(
        "statement",
        [
            "store.update_process(process)",
            "getattr(store, 'update_process')(process)",
        ],
    )
    def test_whole_process_write_fails(self, tmp_path: Path, statement: str) -> None:
        relative = "agent_libos/runtime/sample.py"
        _write_source(tmp_path, relative, "VALUE = 1\n")
        allowlist = _write_current_baseline(tmp_path)

        _write_source(
            tmp_path,
            relative,
            f"def mutate(store, process):\n    {statement}\n",
        )

        errors = checker.check_architecture(tmp_path, allowlist)

        assert any("whole-process-write" in error for error in errors)

    def test_runtime_module_concrete_runtime_import_fails(
        self,
        tmp_path: Path,
    ) -> None:
        relative = "modules/example/module.py"
        _write_source(tmp_path, relative, "VALUE = 1\n")
        allowlist = _write_current_baseline(tmp_path)

        _write_source(
            tmp_path,
            relative,
            "from agent_libos.runtime.runtime import Runtime\n",
        )

        errors = checker.check_architecture(tmp_path, allowlist)

        assert any("concrete-runtime-import" in error for error in errors)

    def test_runtime_module_private_component_access_fails(
        self,
        tmp_path: Path,
    ) -> None:
        relative = "modules/example/module.py"
        _write_source(tmp_path, relative, "VALUE = 1\n")
        allowlist = _write_current_baseline(tmp_path)

        _write_source(
            tmp_path,
            relative,
            """
class ModuleAdapter:
    def register(self):
        return self.shell._authorize_operation()
""".lstrip(),
        )

        errors = checker.check_architecture(tmp_path, allowlist)

        assert any("cross-component-private-access" in error for error in errors)

    def test_new_composition_late_binding_fails(self, tmp_path: Path) -> None:
        relative = "agent_libos/runtime/builder.py"
        _write_source(tmp_path, relative, "VALUE = 1\n")
        allowlist = _write_current_baseline(tmp_path)

        _write_source(
            tmp_path,
            relative,
            """
class RuntimeBuilder:
    def assemble(self, host):
        host.new_service.bind_runtime(host)
""".lstrip(),
        )

        errors = checker.check_architecture(tmp_path, allowlist)

        assert any("composition-late-binding" in error for error in errors)

    def test_builder_component_requires_runtime_declaration(self, tmp_path: Path) -> None:
        _write_source(
            tmp_path,
            "agent_libos/runtime/runtime.py",
            """
class Runtime:
    declared: object
""".lstrip(),
        )
        relative = "agent_libos/runtime/builder.py"
        _write_source(
            tmp_path,
            relative,
            """
class RuntimeBuilder:
    def assemble(self, host):
        host.declared = object()
""".lstrip(),
        )
        allowlist = _write_current_baseline(tmp_path)

        _write_source(
            tmp_path,
            relative,
            """
class RuntimeBuilder:
    def assemble(self, host):
        host.declared = object()
        host.implicit = object()
""".lstrip(),
        )

        errors = checker.check_architecture(tmp_path, allowlist)

        assert any("undeclared-runtime-component" in error for error in errors)

    def test_existing_component_coupling_passes_but_net_growth_fails(
        self, tmp_path: Path
    ) -> None:
        relative = "agent_libos/services/legacy.py"
        original = """
class LegacyService:
    def run(self):
        self.runtime.store.get_process("pid")
        self.checkpoint._insert_row("row")
""".lstrip()
        _write_source(tmp_path, relative, original)
        allowlist = _write_current_baseline(tmp_path)

        assert checker.check_architecture(tmp_path, allowlist) == []

        grown = original.replace(
            '        self.checkpoint._insert_row("row")\n',
            '        self.checkpoint._insert_row("row")\n'
            '        self.runtime.store.get_process("other")\n'
            '        self.checkpoint._insert_row("other")\n',
        )
        _write_source(tmp_path, relative, grown)

        errors = checker.check_architecture(tmp_path, allowlist)

        assert any("runtime-service-locator" in error for error in errors)
        assert any("cross-component-private-access" in error for error in errors)

    @pytest.mark.parametrize("runtime_name", ["runtime", "_runtime"])
    def test_runtime_alias_does_not_bypass_service_locator_guard(
        self,
        tmp_path: Path,
        runtime_name: str,
    ) -> None:
        relative = "agent_libos/services/alias.py"
        _write_source(tmp_path, relative, "VALUE = 1\n")
        allowlist = _write_current_baseline(tmp_path)

        _write_source(
            tmp_path,
            relative,
            f"""
class Service:
    def run(self):
        runtime = self.{runtime_name}
        return runtime.store.get_process("pid")
""".lstrip(),
        )

        errors = checker.check_architecture(tmp_path, allowlist)

        assert any("runtime-service-locator" in error for error in errors)

    def test_dynamic_runtime_alias_does_not_bypass_service_locator_guard(
        self,
        tmp_path: Path,
    ) -> None:
        relative = "agent_libos/services/dynamic_alias.py"
        _write_source(tmp_path, relative, "VALUE = 1\n")
        allowlist = _write_current_baseline(tmp_path)

        _write_source(
            tmp_path,
            relative,
            """
class Service:
    def run(self):
        runtime = getattr(self, "runtime", None)
        host = runtime
        return host.store.get_process("pid")
""".lstrip(),
        )

        errors = checker.check_architecture(tmp_path, allowlist)

        assert any("runtime-service-locator" in error for error in errors)

    def test_dynamic_runtime_service_access_fails(self, tmp_path: Path) -> None:
        relative = "agent_libos/services/dynamic_service.py"
        _write_source(tmp_path, relative, "VALUE = 1\n")
        allowlist = _write_current_baseline(tmp_path)

        _write_source(
            tmp_path,
            relative,
            """
class Service:
    def run(self):
        return getattr(getattr(self, "runtime", None), "store", None)
""".lstrip(),
        )

        errors = checker.check_architecture(tmp_path, allowlist)

        assert any("runtime-service-locator" in error for error in errors)

    def test_cross_component_private_field_access_fails(self, tmp_path: Path) -> None:
        relative = "agent_libos/services/private_state.py"
        _write_source(tmp_path, relative, "VALUE = 1\n")
        allowlist = _write_current_baseline(tmp_path)

        _write_source(
            tmp_path,
            relative,
            """
class Service:
    def run(self):
        return self.registry._entries
""".lstrip(),
        )

        errors = checker.check_architecture(tmp_path, allowlist)

        assert any("cross-component-private-access" in error for error in errors)

    def test_underscored_dependency_does_not_bypass_private_access_guard(
        self,
        tmp_path: Path,
    ) -> None:
        relative = "agent_libos/services/private_dependency.py"
        _write_source(tmp_path, relative, "VALUE = 1\n")
        allowlist = _write_current_baseline(tmp_path)

        _write_source(
            tmp_path,
            relative,
            """
class Service:
    def run(self):
        return self._registry._entries
""".lstrip(),
        )

        errors = checker.check_architecture(tmp_path, allowlist)

        assert any("cross-component-private-access" in error for error in errors)

    def test_local_alias_does_not_bypass_private_access_guard(
        self,
        tmp_path: Path,
    ) -> None:
        relative = "agent_libos/services/private_alias.py"
        _write_source(tmp_path, relative, "VALUE = 1\n")
        allowlist = _write_current_baseline(tmp_path)

        _write_source(
            tmp_path,
            relative,
            """
class Service:
    def run(self):
        registry = self._registry
        selected = registry
        return selected._entries
""".lstrip(),
        )

        errors = checker.check_architecture(tmp_path, allowlist)

        assert any("cross-component-private-access" in error for error in errors)

    def test_dynamic_private_access_does_not_bypass_guard(self, tmp_path: Path) -> None:
        relative = "agent_libos/services/dynamic_private.py"
        _write_source(tmp_path, relative, "VALUE = 1\n")
        allowlist = _write_current_baseline(tmp_path)

        _write_source(
            tmp_path,
            relative,
            """
class Service:
    def run(self):
        return getattr(self._registry, "_entries", None)
""".lstrip(),
        )

        errors = checker.check_architecture(tmp_path, allowlist)

        assert any("cross-component-private-access" in error for error in errors)

    def test_removed_component_debt_requires_budget_update(self, tmp_path: Path) -> None:
        relative = "agent_libos/services/private_dependency.py"
        _write_source(
            tmp_path,
            relative,
            """
class Service:
    def run(self):
        return self._registry._entries
""".lstrip(),
        )
        allowlist = _write_current_baseline(tmp_path)

        _write_source(tmp_path, relative, "VALUE = 1\n")

        errors = checker.check_architecture(tmp_path, allowlist)
        assert any("stale cross-component-private-access budget" in error for error in errors)

        _write_current_baseline(tmp_path)
        assert checker.check_architecture(tmp_path, allowlist) == []

    def test_component_ratchet_survives_method_renames(self, tmp_path: Path) -> None:
        relative = "agent_libos/services/legacy.py"
        original = """
class LegacyService:
    def old_name(self):
        self.runtime.store.get_process("pid")
        self.checkpoint._insert_row("row")
""".lstrip()
        _write_source(tmp_path, relative, original)
        allowlist = _write_current_baseline(tmp_path)

        _write_source(tmp_path, relative, original.replace("old_name", "new_name"))

        assert checker.check_architecture(tmp_path, allowlist) == []

    def test_direct_runtime_facade_call_is_not_a_service_locator(self, tmp_path: Path) -> None:
        _write_source(
            tmp_path,
            "agent_libos/api/sample.py",
            """
class Api:
    def open(self):
        return self.runtime.open()
""".lstrip(),
        )
        allowlist = _write_current_baseline(tmp_path)

        assert allowlist.is_file()
        assert checker.scan_architecture(tmp_path).violations == ()

    def test_existing_long_function_requires_budget_update_after_shrink(
        self,
        tmp_path: Path,
    ) -> None:
        relative = "agent_libos/services/long_function.py"
        _write_source(tmp_path, relative, _long_function(checker.MAX_FUNCTION_LINES))
        allowlist = _write_current_baseline(tmp_path)

        _write_source(
            tmp_path,
            relative,
            _long_function(checker.MAX_FUNCTION_LINES + 1),
        )
        errors = checker.check_architecture(tmp_path, allowlist)

        assert any("long-function" in error for error in errors)

        _write_source(
            tmp_path,
            relative,
            _long_function(checker.MAX_FUNCTION_LINES - 1),
        )
        errors = checker.check_architecture(tmp_path, allowlist)
        assert any("stale long-function budget" in error for error in errors)

        _write_current_baseline(tmp_path)
        assert checker.check_architecture(tmp_path, allowlist) == []

    def test_new_long_function_fails_without_an_allowlist_entry(self, tmp_path: Path) -> None:
        _write_source(tmp_path, "agent_libos/__init__.py", "")
        allowlist = _write_current_baseline(tmp_path)

        _write_source(
            tmp_path,
            "agent_libos/services/new_long_function.py",
            _long_function(checker.MAX_FUNCTION_LINES),
        )

        errors = checker.check_architecture(tmp_path, allowlist)

        assert any("long-function" in error for error in errors)

    def test_complexity_hotspot_requires_budget_update_after_shrink(
        self,
        tmp_path: Path,
    ) -> None:
        relative = "agent_libos/services/complexity.py"
        _write_source(
            tmp_path,
            relative,
            _complex_function(checker.COMPLEXITY_HOTSPOT_THRESHOLD),
        )
        allowlist = _write_current_baseline(tmp_path)

        _write_source(
            tmp_path,
            relative,
            _complex_function(checker.COMPLEXITY_HOTSPOT_THRESHOLD + 1),
        )
        errors = checker.check_architecture(tmp_path, allowlist)

        assert any("complexity-hotspot" in error for error in errors)

        _write_source(
            tmp_path,
            relative,
            _complex_function(checker.COMPLEXITY_HOTSPOT_THRESHOLD - 1),
        )
        errors = checker.check_architecture(tmp_path, allowlist)
        assert any("stale complexity-hotspot budget" in error for error in errors)

        _write_current_baseline(tmp_path)
        assert checker.check_architecture(tmp_path, allowlist) == []
