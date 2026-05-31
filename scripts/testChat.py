import asyncio
from agent_libos import Runtime
from agent_libos.models import AgentImage, ProcessStatus, ResourceBudget

CHAT_IMAGE_ID = "chat-image:v0"
CHAT_IMAGE_NAME = "ChatImage"
def chat_image() -> AgentImage:
    return AgentImage(
        image_id=CHAT_IMAGE_ID,
        name=CHAT_IMAGE_NAME,
        version="v0",
        system_prompt="Traditional human/LLM chat image with only human I/O and process exit tools.",
        default_tools=["ask_human", "human_output", "process_exit"],
        context_policy="recency_first",
        required_capabilities=[{"resource": "human:owner", "rights": ["write"]}],
    )

runtime = Runtime.open("local")
runtime.register_image(chat_image())
pid = runtime.process.spawn(image=CHAT_IMAGE_ID,
    goal=("You are an AI assistant interacting via a terminal interface. To ensure a smooth and efficient conversation, please adhere to the following rules:"
          "1. Do not repeat the same text, greetings, or explanations in one turn. Keep responses concise and strictly relevant to the new context.",
          "2. Every turn MUST conclude with a tool call to either `ask_human` (to receive the next user message) or `process_exit` (when the user wants to exit or the task is done). Never end a turn without calling one of these tools."),
    resource_budget=ResourceBudget(max_materialized_tokens=64_000), )
asyncio.run(runtime.arun_until_idle())
process = runtime.process.get(pid)
if process.status != ProcessStatus.EXITED:
    raise RuntimeError(f"chat process did not exit; status={process.status.value}")
