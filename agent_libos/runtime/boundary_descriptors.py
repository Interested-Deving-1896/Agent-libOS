from __future__ import annotations

from agent_libos.ports import ExplainBoundaryDescriptor


def boundary(
    component: str,
    method: str,
    kind: str,
    name: str,
    actor_arg: str = "",
    pid_arg: str = "",
    *,
    result_pid: bool = False,
    preflight_method: str = "",
) -> ExplainBoundaryDescriptor:
    return ExplainBoundaryDescriptor(
        component=component,
        method=method,
        kind=kind,
        name=name,
        actor_arg=actor_arg,
        pid_arg=pid_arg,
        result_pid=result_pid,
        preflight_method=preflight_method,
    )


PRIMITIVE_BOUNDARIES = (
    boundary("filesystem", "read_text", "primitive", "primitive.filesystem.read_text", "pid", "pid"),
    boundary("filesystem", "read_bytes", "primitive", "primitive.filesystem.read_bytes", "pid", "pid"),
    boundary("filesystem", "write_text", "primitive", "primitive.filesystem.write_text", "pid", "pid"),
    boundary("filesystem", "read_directory", "primitive", "primitive.filesystem.read_directory", "pid", "pid"),
    boundary("filesystem", "write_directory", "primitive", "primitive.filesystem.write_directory", "pid", "pid"),
    boundary("filesystem", "delete_file", "primitive", "primitive.filesystem.delete_file", "pid", "pid"),
    boundary("filesystem", "delete_directory", "primitive", "primitive.filesystem.delete_directory", "pid", "pid"),
    boundary("shell", "run", "primitive", "primitive.shell.run", "pid", "pid"),
    boundary("jsonrpc", "call", "primitive", "primitive.jsonrpc.call", "pid", "pid"),
    boundary("mcp", "list_tools", "primitive", "primitive.mcp.list_tools", "actor", "actor"),
    boundary("mcp", "call_tool", "primitive", "primitive.mcp.call", "pid", "pid"),
    boundary("clock", "now", "primitive", "primitive.clock.now", "pid", "pid"),
    boundary("clock", "sleep", "primitive", "primitive.clock.sleep", "pid", "pid"),
)

PROCESS_BOUNDARIES = (
    boundary(
        "process", "spawn", "runtime", "process.spawn",
        result_pid=True, preflight_method="preflight_spawn",
    ),
    boundary(
        "process", "fork", "runtime", "process.fork", "parent", "parent",
        preflight_method="preflight_fork",
    ),
    boundary(
        "process", "spawn_child", "runtime", "process.spawn_child", "parent", "parent",
        preflight_method="preflight_spawn_child",
    ),
    boundary(
        "process", "exec", "runtime", "process.exec", "pid", "pid",
        preflight_method="preflight_exec",
    ),
    boundary("process", "set_working_directory", "runtime", "process.chdir", "pid", "pid"),
    boundary("process", "signal_child", "runtime", "process.signal_child", "pid", "pid"),
    boundary("process", "signal", "runtime", "process.signal", "target", "target"),
    boundary("process", "wait", "runtime", "process.wait", "pid", "pid"),
    boundary("process", "pause", "runtime", "process.pause", "pid", "pid"),
    boundary("process", "resume", "runtime", "process.resume", "pid", "pid"),
    boundary("process", "cancel", "runtime", "process.cancel", "pid", "pid"),
    boundary("process", "exit", "runtime", "process.exit", "pid", "pid"),
)

MEMORY_BOUNDARIES = (
    boundary("memory", "create_object", "runtime", "memory.create_object", "pid", "pid"),
    boundary("memory", "update_object", "runtime", "memory.update_object", "pid", "pid"),
    boundary("memory", "append_object_by_name", "runtime", "memory.append_object", "pid", "pid"),
    boundary("memory", "link_objects", "runtime", "memory.link_objects", "pid", "pid"),
    boundary("memory", "create_view", "runtime", "memory.create_view", "pid", "pid"),
    boundary("memory", "fork_view", "runtime", "memory.fork_view", "parent_pid", "parent_pid"),
    boundary("memory", "merge_view", "runtime", "memory.merge_view", "parent_pid", "parent_pid"),
    boundary("memory", "snapshot_view", "runtime", "memory.snapshot_view", "pid", "pid"),
    boundary("memory", "materialize_context", "runtime", "memory.materialize_context", "pid", "pid"),
)

CHECKPOINT_BOUNDARIES = (
    boundary("checkpoint", "create", "runtime", "checkpoint.create", "actor", "pid"),
    boundary(
        "checkpoint", "inspect", "runtime", "checkpoint.inspect", "actor", "actor",
        preflight_method="preflight_checkpoint",
    ),
    boundary(
        "checkpoint", "diff", "runtime", "checkpoint.diff", "actor", "actor",
        preflight_method="preflight_checkpoint",
    ),
    boundary(
        "checkpoint", "restore", "runtime", "checkpoint.restore", "actor", "actor",
        preflight_method="preflight_checkpoint",
    ),
    boundary(
        "checkpoint", "fork_from_checkpoint", "runtime", "checkpoint.fork", "actor", "actor",
        preflight_method="preflight_checkpoint",
    ),
    boundary(
        "checkpoint", "replay_to_event", "runtime", "checkpoint.replay", "actor", "actor",
        preflight_method="preflight_checkpoint",
    ),
)

HUMAN_BOUNDARIES = (
    boundary("human", "query", "runtime", "human.query", "pid", "pid"),
    boundary("human", "request_permission", "runtime", "human.request_permission", "pid", "pid"),
    boundary("human", "ask", "runtime", "human.ask", "pid", "pid"),
    boundary("human", "output", "runtime", "human.output", "pid", "pid"),
    boundary("human", "interrupt", "runtime", "human.interrupt", "pid", "pid"),
    boundary("human", "send_process_message", "runtime", "human.process_message", "human", "recipient_pid"),
)

MESSAGE_BOUNDARIES = (
    boundary("messages", "post", "runtime", "process.message.post", "sender", "recipient_pid"),
    boundary("messages", "send_from_process", "runtime", "process.message.send", "sender_pid", "sender_pid"),
    boundary("messages", "receive", "runtime", "process.message.receive", "pid", "pid"),
    boundary("messages", "ack", "runtime", "process.message.ack", "pid", "pid"),
)

OBJECT_TASK_BOUNDARIES = (
    boundary("object_tasks", "start", "runtime", "object_task.start", "pid", "pid"),
    boundary("object_tasks", "watch_owner", "runtime", "object_task.watch", "actor_pid", "actor_pid"),
    boundary("object_tasks", "cancel", "runtime", "object_task.cancel", "actor_pid", "actor_pid"),
    boundary("object_tasks", "wait", "runtime", "object_task.wait", "actor_pid", "actor_pid"),
)

AUTHORITY_BOUNDARIES = (
    boundary("authority_manifests", "prepare_launch", "runtime", "authority_manifest.bind", "issued_by", "pid"),
    boundary("capability", "issue", "runtime", "capability.issue", "actor", "subject"),
    boundary("capability", "delegate", "runtime", "capability.delegate", "parent", "parent"),
    boundary("capability", "derive_authority", "runtime", "capability.derive_authority", "source_subject", "source_subject"),
    boundary("capability", "revoke", "runtime", "capability.revoke", "revoked_by", "revoked_by"),
)

EXTENSION_BOUNDARIES = (
    boundary("tools", "activate_tool_group", "runtime", "tool_group.activate", "pid", "pid"),
    boundary("skills", "register_skill_package", "runtime", "skill.register", "actor", "actor"),
    boundary("skills", "activate_skill", "runtime", "skill.activate", "pid", "pid"),
    boundary("skills", "unload_skill", "runtime", "skill.unload", "pid", "pid"),
    boundary("skills", "trust_skill_source", "runtime", "skill.trust", "actor", "actor"),
    boundary("skills", "untrust_skill_source", "runtime", "skill.untrust", "actor", "actor"),
    boundary("image_registry", "register", "runtime", "image.register", "actor", "actor"),
    boundary("image_registry", "register_from_package_files", "runtime", "image.register_package", "actor", "actor"),
    boundary(
        "image_registry", "commit_from_checkpoint", "runtime", "image.commit", "actor", "actor",
        preflight_method="preflight_checkpoint_commit",
    ),
    boundary("jsonrpc", "register_endpoint", "runtime", "jsonrpc.register", "actor", "actor"),
    boundary("jsonrpc", "unregister_endpoint", "runtime", "jsonrpc.unregister", "actor", "actor"),
    boundary("mcp", "register_server", "runtime", "mcp.register", "actor", "actor"),
    boundary("mcp", "unregister_server", "runtime", "mcp.unregister", "actor", "actor"),
)

EXPLAIN_BOUNDARY_DESCRIPTORS = (
    *PRIMITIVE_BOUNDARIES,
    *PROCESS_BOUNDARIES,
    *MEMORY_BOUNDARIES,
    *CHECKPOINT_BOUNDARIES,
    *HUMAN_BOUNDARIES,
    *MESSAGE_BOUNDARIES,
    *OBJECT_TASK_BOUNDARIES,
    *AUTHORITY_BOUNDARIES,
    *EXTENSION_BOUNDARIES,
)


def validate_explain_descriptors() -> None:
    names = [descriptor.name for descriptor in EXPLAIN_BOUNDARY_DESCRIPTORS]
    if len(names) != len(set(names)):
        raise ValueError("duplicate explain boundary descriptor")


validate_explain_descriptors()


__all__ = ["EXPLAIN_BOUNDARY_DESCRIPTORS"]
