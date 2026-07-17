from agent_libos.models import DataFlowDirection
from agent_libos.sdk import (
    AuthorityMode,
    PostProviderFailureMode,
    ResourcePolicy,
)
from agent_libos.sdk.descriptors import protected_operation_descriptor as operation


PROTECTED_OPERATION_DESCRIPTORS = (
    operation(
        "primitive.human.read",
        "human",
        "read",
        resource_policy=ResourcePolicy.NONE,
        authority_mode=AuthorityMode.RUNTIME_INTERNAL,
        information_flow=True,
        data_flow_direction=DataFlowDirection.BIDIRECTIONAL,
        post_provider_failure_mode=PostProviderFailureMode.PRESERVE_RESULT,
        internal_reason="terminal queue already owns an authorized Human request",
    ),
    operation(
        "primitive.human.write",
        "human",
        "write",
        resource_policy=ResourcePolicy.NONE,
        authority_mode=AuthorityMode.RUNTIME_INTERNAL,
        state_mutation=True,
        information_flow=True,
        data_flow_direction=DataFlowDirection.EGRESS,
        post_provider_failure_mode=PostProviderFailureMode.PRESERVE_RESULT,
        internal_reason="terminal queue already owns an authorized Human request",
        prepared_recovery="human_output_delivery",
    ),
)


__all__ = ["PROTECTED_OPERATION_DESCRIPTORS"]
