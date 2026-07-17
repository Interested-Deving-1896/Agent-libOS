from agent_libos.models import DataFlowDirection
from agent_libos.sdk import AuthorityMode, ResourcePolicy
from agent_libos.sdk.descriptors import protected_operation_descriptor as operation


PROTECTED_OPERATION_DESCRIPTORS = (
    operation(
        "primitive.pty.spawn",
        "pty",
        "spawn",
        resource_policy=ResourcePolicy.NONE,
        state_mutation=True,
        information_flow=True,
        data_flow_direction=DataFlowDirection.BIDIRECTIONAL,
    ),
    operation(
        "primitive.pty.read",
        "pty",
        "read",
        resource_policy=ResourcePolicy.NONE,
        information_flow=True,
        data_flow_direction=DataFlowDirection.INGRESS,
    ),
    operation(
        "primitive.pty.ingest",
        "pty",
        "ingest",
        resource_policy=ResourcePolicy.NONE,
        authority_mode=AuthorityMode.RUNTIME_INTERNAL,
        information_flow=True,
        data_flow_direction=DataFlowDirection.INGRESS,
        internal_reason=(
            "runtime continuously drains an already-authorized PTY session so the "
            "child process cannot block on its output buffer"
        ),
    ),
    operation(
        "primitive.pty.write",
        "pty",
        "write",
        resource_policy=ResourcePolicy.NONE,
        state_mutation=True,
        information_flow=True,
        data_flow_direction=DataFlowDirection.EGRESS,
    ),
    operation(
        "primitive.pty.resize",
        "pty",
        "resize",
        resource_policy=ResourcePolicy.NONE,
        state_mutation=True,
        information_flow=True,
        data_flow_direction=DataFlowDirection.EGRESS,
    ),
    operation(
        "primitive.pty.close",
        "pty",
        "close",
        resource_policy=ResourcePolicy.NONE,
        state_mutation=True,
        information_flow=True,
        data_flow_direction=DataFlowDirection.EGRESS,
    ),
    operation(
        "primitive.pty.close.internal",
        "pty",
        "close",
        resource_policy=ResourcePolicy.NONE,
        authority_mode=AuthorityMode.RUNTIME_INTERNAL,
        state_mutation=True,
        information_flow=True,
        internal_reason="runtime lifecycle finalizer owns the PTY session being closed",
    ),
)


__all__ = ["PROTECTED_OPERATION_DESCRIPTORS"]
