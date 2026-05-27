from __future__ import annotations

from agent_libos.models import MaterializedContext


def format_context_message(context: MaterializedContext) -> str:
    omitted = ", ".join(context.omitted_objects) if context.omitted_objects else "none"
    return (
        f"Context policy: {context.policy_used}\n"
        f"Token estimate: {context.token_count}\n"
        f"Omitted objects: {omitted}\n\n"
        f"{context.text}"
    )

