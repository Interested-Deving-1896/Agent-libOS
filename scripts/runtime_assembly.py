from __future__ import annotations

from pathlib import Path

from agent_libos import Runtime
from agent_libos.config import AgentLibOSConfig
from agent_libos.runtime import RuntimeAssemblyCleanupRequired
from agent_libos.substrate import ResourceProviderSubstrate


async def aopen_runtime(
    target: str | Path | None = None,
    substrate: ResourceProviderSubstrate | None = None,
    config: AgentLibOSConfig | None = None,
    module_manifests: list[str | Path] | tuple[str | Path, ...] | None = None,
    trusted_modules: list[str] | tuple[str, ...] | None = None,
    trusted_module_sha256: list[str] | tuple[str, ...] | None = None,
) -> Runtime:
    """Open a script-owned Runtime and discharge failed-assembly ownership."""

    try:
        return await Runtime.aopen(
            target,
            substrate=substrate,
            config=config,
            module_manifests=module_manifests,
            trusted_modules=trusted_modules,
            trusted_module_sha256=trusted_module_sha256,
        )
    except BaseException as assembly_error:
        cleanup_errors: list[BaseException] = []
        for handle in RuntimeAssemblyCleanupRequired.extract(assembly_error):
            try:
                await handle.arelease()
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if cleanup_errors:
            raise BaseExceptionGroup(
                "runtime assembly failed and script cleanup did not complete",
                [assembly_error, *cleanup_errors],
            ) from assembly_error
        raise


__all__ = ["aopen_runtime"]
