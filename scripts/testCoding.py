import asyncio
from agent_libos import Runtime
from agent_libos.models import ProcessStatus

runtime = Runtime.open("local")
pid = runtime.process.spawn(image="coding-agent:v0", goal=input("Write your goal here:"))
asyncio.run(runtime.arun_until_idle())
process = runtime.process.get(pid)
if process.status != ProcessStatus.EXITED:
    raise RuntimeError(f"chat process did not exit; status={process.status.value}")
