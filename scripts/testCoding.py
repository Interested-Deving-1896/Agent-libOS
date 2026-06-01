import asyncio
from agent_libos import Runtime
from agent_libos.config import DEFAULT_CONFIG
from agent_libos.models import ProcessStatus

_RUNTIME_DEFAULTS = DEFAULT_CONFIG.runtime

runtime = Runtime.open(_RUNTIME_DEFAULTS.local_store_target)
pid = runtime.process.spawn(image=_RUNTIME_DEFAULTS.coding_image_id, goal=input("Write your goal here:"))
asyncio.run(runtime.arun_until_idle())
process = runtime.process.get(pid)
if process.status != ProcessStatus.EXITED:
    raise RuntimeError(f"chat process did not exit; status={process.status.value}")
