from __future__ import annotations

import asyncio
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from enum import StrEnum
import sys
from typing import Any, Awaitable, Callable, Iterable, Mapping, TypeVar

from agent_libos.models import (
    AuditRecord,
    CapabilityDecision,
    Event,
    EventPriority,
    EventType,
    ExternalEffectClassification,
    ExternalEffectRollbackClass,
    ExternalEffectRollbackStatus,
    OperationKind,
    ResourceUsage,
)
from agent_libos.models.exceptions import CapabilityDenied, ValidationError
from agent_libos.runtime.external_effects import (
    abandon_external_effect_intent,
    classify_external_effect,
    mark_external_effect_dispatched,
    prepare_external_effect_intent,
    record_external_effect,
    require_external_effect_classifier,
)
from agent_libos.substrate import ProviderEffectNotStarted
from agent_libos.utils.ids import utc_now
from agent_libos.utils.serde import to_jsonable


class AuthorityMode(StrEnum):
    CAPABILITY = "capability"
    RUNTIME_INTERNAL = "runtime_internal"


class PostProviderFailureMode(StrEnum):
    PROPAGATE = "propagate"
    PRESERVE_RESULT = "preserve_result"


class ResourcePolicy(StrEnum):
    NONE = "none"
    OPTIONAL = "optional"
    REQUIRED = "required"


@dataclass(frozen=True)
class ProtectedOperationContract:
    name: str
    provider: str
    operation: str
    evidence_roles: tuple[str, ...]
    resource_policy: ResourcePolicy
    authority_mode: AuthorityMode = AuthorityMode.CAPABILITY
    state_mutation: bool = False
    information_flow: bool = False
    post_provider_failure_mode: PostProviderFailureMode = PostProviderFailureMode.PROPAGATE
    internal_reason: str | None = None
    require_classifier: bool = True
    preflight_classifier: bool = False
    classifier_failure_rollback_class: ExternalEffectRollbackClass = ExternalEffectRollbackClass.UNKNOWN
    classifier_failure_rollback_status: ExternalEffectRollbackStatus = ExternalEffectRollbackStatus.UNKNOWN
    classifier_failure_label: str = "post_effect_failure"
    prepared_recovery: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.provider or not self.operation:
            raise ValueError("protected operation contract names must be non-empty")
        if set(self.evidence_roles) != {"audit", "effect", "event"}:
            raise ValueError(
                "protected operation contracts must declare audit, event, and effect evidence"
            )
        if self.authority_mode == AuthorityMode.RUNTIME_INTERNAL and not str(
            self.internal_reason or ""
        ).strip():
            raise ValueError("runtime-internal protected operations require an explicit reason")
        if self.prepared_recovery is not None and not str(self.prepared_recovery).strip():
            raise ValueError("prepared recovery policy names must be non-empty")


@dataclass(frozen=True)
class ProviderPhase:
    name: str
    state_mutation: bool = False
    information_flow: bool = False
    commits_authority: bool = True

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("provider phase name must be non-empty")


@dataclass(frozen=True)
class ResourceSettlement:
    usage: ResourceUsage | Mapping[str, Any]
    source: str
    context: Mapping[str, Any] = field(default_factory=dict)
    allow_overage: bool = True
    kill_on_exceed: bool = True


@dataclass(frozen=True)
class ProviderEffectNotStartedResult:
    """A structured provider result backed by a not-started certificate.

    Some primitives preserve their public result type instead of propagating
    :class:`ProviderEffectNotStarted`.  Returning this marker from the provider
    callable lets the SDK settle the phase as not-started without recording the
    current phase as an observed mutation.
    """

    error: ProviderEffectNotStarted
    result: Any
    outcome: str = "partial_not_started_after_prior_provider_effect"

    def __post_init__(self) -> None:
        if not isinstance(self.error, ProviderEffectNotStarted):
            raise TypeError("not-started result requires ProviderEffectNotStarted")
        if not self.outcome:
            raise ValueError("not-started result outcome must be non-empty")


@dataclass(frozen=True)
class ProtectedOperationEvidence:
    event_type: EventType | str
    event_source: str
    audit_action: str
    audit_actor: str
    event_target: str | None = None
    event_payload: Mapping[str, Any] = field(default_factory=dict)
    event_priority: EventPriority | str = EventPriority.NORMAL
    audit_target: str | None = None
    audit_decision: Mapping[str, Any] = field(default_factory=dict)
    input_refs: tuple[str, ...] = ()
    output_refs: tuple[str, ...] = ()
    capability_refs: tuple[str, ...] = ()
    correlation_id: str | None = None
    parent_record_id: str | None = None
    effect_metadata: Mapping[str, Any] = field(default_factory=dict)
    provider_receipt: Mapping[str, Any] = field(default_factory=dict)


Hook = Callable[[], None]
FailureEvidenceFactory = Callable[[BaseException, str], ProtectedOperationEvidence]
PreparedRecoveryHandler = Callable[[Any], None]
FailureResourceFactory = Callable[[BaseException, str], ResourceSettlement | None]


@dataclass(frozen=True)
class ProtectedOperationInvocation:
    pid: str
    actor: str
    target: str | None
    decisions: tuple[CapabilityDecision, ...] = ()
    canonical_args: Mapping[str, Any] = field(default_factory=dict)
    observation: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None
    preflight_usage: ResourceUsage | Mapping[str, Any] | None = None
    resource_source: str | None = None
    resource_context: Mapping[str, Any] = field(default_factory=dict)
    prepare: Hook | None = None
    restore_not_started: Hook | None = None
    failure_evidence: FailureEvidenceFactory | None = None
    failure_resource: ResourceSettlement | FailureResourceFactory | None = None


class ProtectedOperationProtocolError(ValidationError):
    pass


@dataclass(frozen=True)
class _ActiveBoundary:
    sdk_identity: int
    contract_name: str
    phase_name: str
    effect_id: str


_CURRENT_BOUNDARY: ContextVar[_ActiveBoundary | None] = ContextVar(
    "agent_libos_protected_provider_boundary",
    default=None,
)

T = TypeVar("T")


class ProtectedOperationSDK:
    """One fail-closed lifecycle for trusted provider operations."""

    def __init__(
        self,
        *,
        store: Any,
        capabilities: Any,
        audit: Any,
        events: Any,
        resources: Any | None,
        operations: Any,
    ) -> None:
        self.store = store
        self.capabilities = capabilities
        self.audit = audit
        self.events = events
        self.resources = resources
        self.operations = operations
        self._contracts: dict[str, ProtectedOperationContract] = {}
        self._prepared_recovery_handlers: dict[str, PreparedRecoveryHandler] = {}
        self._identity = id(self)

    def register_contract(self, contract: ProtectedOperationContract) -> ProtectedOperationContract:
        existing = self._contracts.get(contract.name)
        if existing is not None and existing != contract:
            raise ValidationError(f"protected operation contract conflict: {contract.name}")
        self._contracts[contract.name] = contract
        return contract

    def contracts(self) -> tuple[ProtectedOperationContract, ...]:
        return tuple(self._contracts[name] for name in sorted(self._contracts))

    def register_prepared_recovery(
        self,
        name: str,
        handler: PreparedRecoveryHandler,
    ) -> None:
        selected = str(name).strip()
        if not selected or not callable(handler):
            raise ValidationError("prepared recovery requires a name and callable handler")
        existing = self._prepared_recovery_handlers.get(selected)
        if existing is not None and existing != handler:
            raise ValidationError(f"prepared recovery handler conflict: {selected}")
        self._prepared_recovery_handlers[selected] = handler

    def recover_prepared(self) -> list[str]:
        """Restore local prepare state that never reached a provider phase."""

        recovered: list[str] = []
        for effect in self.store.list_external_effects():
            if effect.effect_state != "pending" or effect.transaction_state != "prepared":
                continue
            protected = effect.provider_metadata.get("protected_operation")
            if not isinstance(protected, Mapping):
                continue
            raw_reservations = protected.get("reservation_ids") or ()
            if not isinstance(raw_reservations, (list, tuple)) or any(
                not isinstance(item, str) or not item for item in raw_reservations
            ):
                raise ValidationError(
                    f"prepared protected operation has invalid reservation links: {effect.effect_id}"
                )
            contract_name = protected.get("contract_name")
            actor = protected.get("actor")
            if not isinstance(contract_name, str) or not contract_name:
                raise ValidationError(
                    f"prepared protected operation has invalid contract identity: {effect.effect_id}"
                )
            if not isinstance(actor, str) or not actor:
                raise ValidationError(
                    f"prepared protected operation has invalid actor identity: {effect.effect_id}"
                )
            expected_reservation_reason = (
                f"protected operation reserved authority for {contract_name}"
            )
            for reservation_id in raw_reservations:
                reservation = self.store.get_capability_use_reservation(reservation_id)
                if reservation is None or reservation.get("status") != "reserved":
                    continue
                if (
                    reservation.get("reserved_by") != actor
                    or reservation.get("reason") != expected_reservation_reason
                ):
                    raise ValidationError(
                        f"prepared protected operation reservation binding mismatch: {effect.effect_id}"
                    )
            recovery_name = protected.get("prepared_recovery")
            handler = None
            if recovery_name is not None:
                if not isinstance(recovery_name, str) or not recovery_name:
                    raise ValidationError(
                        f"prepared protected operation has invalid recovery policy: {effect.effect_id}"
                    )
                handler = self._prepared_recovery_handlers.get(recovery_name)
                if handler is None:
                    raise ValidationError(
                        f"prepared recovery handler is not registered: {recovery_name}"
                    )
            with self.store.transaction():
                if handler is not None:
                    handler(effect)
                for reservation_id in reversed(tuple(raw_reservations)):
                    self.capabilities.restore_reserved_use(
                        reservation_id,
                        restored_by="runtime.recovery",
                        reason=(
                            "protected operation recovered before provider dispatch: "
                            f"{contract_name}"
                        ),
                    )
                abandon_external_effect_intent(self.store, effect.effect_id)
            recovered.append(effect.effect_id)
        return recovered

    def current_boundary(self) -> tuple[str, str, str] | None:
        current = _CURRENT_BOUNDARY.get()
        if current is None or current.sdk_identity != self._identity:
            return None
        return current.contract_name, current.phase_name, current.effect_id

    def start(
        self,
        contract: ProtectedOperationContract | str,
        invocation: ProtectedOperationInvocation,
        *,
        provider: Any,
    ) -> "ProtectedOperation":
        name = contract if isinstance(contract, str) else contract.name
        registered = self._contracts.get(name)
        if registered is None:
            raise ValidationError(f"protected operation contract is not registered: {name}")
        if not isinstance(contract, str) and registered != contract:
            raise ValidationError(f"protected operation contract does not match registry: {name}")
        return ProtectedOperation(self, registered, invocation, provider)


class ProtectedOperation:
    def __init__(
        self,
        sdk: ProtectedOperationSDK,
        contract: ProtectedOperationContract,
        invocation: ProtectedOperationInvocation,
        provider: Any,
    ) -> None:
        self.sdk = sdk
        self.contract = contract
        self.invocation = invocation
        self.provider = provider
        self.effect_id: str | None = None
        self._reservation_ids: list[str] = []
        self._reservations_committed = False
        self._dispatched = False
        self._terminal = False
        self._completed_phases: list[ProviderPhase] = []
        self._operation_cm: Any | None = None

    @property
    def terminal(self) -> bool:
        return self._terminal

    def __enter__(self) -> "ProtectedOperation":
        current = self.sdk.operations.current()
        if (
            current is None
            or current.name != self.contract.name
            or current.pid != self.invocation.pid
            or current.actor != self.invocation.actor
        ):
            self._operation_cm = self.sdk.operations.scope(
                kind=OperationKind.PRIMITIVE,
                name=self.contract.name,
                actor=self.invocation.actor,
                pid=self.invocation.pid,
                expected_roles=(),
            )
            self._operation_cm.__enter__()
        try:
            self._prepare()
        except BaseException:
            if self._operation_cm is not None:
                operation_cm = self._operation_cm
                self._operation_cm = None
                operation_cm.__exit__(*sys.exc_info())
            raise
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> bool:
        protocol_error: BaseException | None = None
        settlement_error: BaseException | None = None
        try:
            if not self._terminal:
                if not self._dispatched:
                    self._abort_not_started("provider_not_dispatched")
                    if exc is None:
                        protocol_error = ProtectedOperationProtocolError(
                            f"protected operation exited without provider phase: {self.contract.name}"
                        )
                elif exc is None:
                    protocol_error = ProtectedOperationProtocolError(
                        f"protected operation exited without complete(): {self.contract.name}"
                    )
                    self._finalize_unknown(protocol_error, "protocol_incomplete")
                else:
                    self._finalize_unknown(exc, "caller_failed_after_provider")
        except BaseException as error:
            settlement_error = error

        selected_type = exc_type
        selected_exc = exc
        selected_tb = tb
        if settlement_error is not None:
            selected_type = type(settlement_error)
            selected_exc = settlement_error
            selected_tb = settlement_error.__traceback__
        elif protocol_error is not None:
            selected_type = type(protocol_error)
            selected_exc = protocol_error
            selected_tb = protocol_error.__traceback__
        operation_exit_error: BaseException | None = None
        if self._operation_cm is not None:
            operation_cm = self._operation_cm
            self._operation_cm = None
            try:
                operation_cm.__exit__(selected_type, selected_exc, selected_tb)
            except BaseException as error:
                operation_exit_error = error
        if settlement_error is not None:
            raise settlement_error.with_traceback(settlement_error.__traceback__)
        if protocol_error is not None:
            raise protocol_error
        if operation_exit_error is not None:
            raise operation_exit_error.with_traceback(operation_exit_error.__traceback__)
        return False

    def call(self, phase: ProviderPhase, function: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        self._require_active()
        self._dispatch(phase)
        token = self._activate_boundary(phase)
        try:
            result = function(*args, **kwargs)
        except ProviderEffectNotStarted as error:
            self._handle_not_started(error, phase)
            raise
        except BaseException as error:
            self._expect_settlement_evidence()
            self._commit_reservations_best_effort()
            self._finalize_unknown(error, phase.name)
            raise
        finally:
            _CURRENT_BOUNDARY.reset(token)
        if isinstance(result, ProviderEffectNotStartedResult):
            self._handle_not_started(result.error, phase, outcome=result.outcome)
            return result  # type: ignore[return-value]
        self._record_completed_phase(phase)
        self._expect_settlement_evidence()
        self._completed_phases.append(phase)
        if phase.commits_authority:
            try:
                self._commit_reservations()
            except BaseException as error:
                self._finalize_unknown(error, "capability_commit")
                raise
        return result

    async def acall(
        self,
        phase: ProviderPhase,
        function: Callable[..., Awaitable[T]],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> T:
        self._require_active()
        self._dispatch(phase)
        token = self._activate_boundary(phase)
        try:
            result = await function(*args, **kwargs)
        except ProviderEffectNotStarted as error:
            self._handle_not_started(error, phase)
            raise
        except BaseException as error:
            self._expect_settlement_evidence()
            self._commit_reservations_best_effort()
            self._finalize_unknown(error, phase.name)
            raise
        finally:
            _CURRENT_BOUNDARY.reset(token)
        if isinstance(result, ProviderEffectNotStartedResult):
            self._handle_not_started(result.error, phase, outcome=result.outcome)
            return result  # type: ignore[return-value]
        self._record_completed_phase(phase)
        self._expect_settlement_evidence()
        self._completed_phases.append(phase)
        if phase.commits_authority:
            try:
                self._commit_reservations()
            except BaseException as error:
                self._finalize_unknown(error, "capability_commit")
                raise
        return result

    def complete(
        self,
        result: T,
        evidence: ProtectedOperationEvidence,
        *,
        classification_context: Mapping[str, Any] | None = None,
        classification_result: Any | None = None,
        classification_override: ExternalEffectClassification | None = None,
        settle_success: Hook | None = None,
        resource: ResourceSettlement | None = None,
    ) -> T:
        self._require_active()
        if not self._dispatched:
            raise ProtectedOperationProtocolError(
                f"protected operation completed without provider dispatch: {self.contract.name}"
            )
        self._validate_resource_settlement(resource)
        try:
            classification = self._classification_with_phase_floor(
                classification_override
                if classification_override is not None
                else self._classification(
                    classification_context,
                    result if classification_result is None else classification_result,
                )
            )
            effect_metadata, flattened_metadata = self._safe_effect_metadata(
                evidence,
                classification_keys=classification.metadata.keys(),
            )
            classified_receipt = classification.metadata.get("provider_receipt")
            provider_receipt = (
                dict(evidence.provider_receipt)
                if evidence.provider_receipt
                else dict(classified_receipt)
                if isinstance(classified_receipt, Mapping)
                else {}
            )
            with self.sdk.store.transaction():
                if settle_success is not None:
                    settle_success()
                event, audit_record = self._persist_evidence(evidence)
                record_external_effect(
                    self.sdk.store,
                    pid=self.invocation.pid,
                    provider=self.contract.provider,
                    operation=self.contract.operation,
                    target=self.invocation.target,
                    classification=classification,
                    audit_record=audit_record,
                    event=event,
                    metadata={
                        "context": dict(self.invocation.observation),
                        "provider_phases": self._phase_metadata(),
                        "result": effect_metadata,
                        **flattened_metadata,
                        "provider_receipt": provider_receipt,
                    },
                    intent_effect_id=self.effect_id,
                )
            self._terminal = True
        except BaseException as error:
            self._terminal = True
            if self.contract.post_provider_failure_mode == PostProviderFailureMode.PRESERVE_RESULT:
                return result
            raise

        self._charge_resource(resource)
        return result

    def _prepare(self) -> None:
        if self.contract.require_classifier:
            require_external_effect_classifier(self.provider, self.contract.operation)
        self._validate_authority()
        manifests = getattr(self.sdk.store, "authority_manifest_manager", None)
        if manifests is not None:
            manifests.assert_effect(
                self.invocation.pid,
                f"{self.contract.provider}.{self.contract.operation}",
            )
        if self.contract.preflight_classifier:
            # Capability/manifest gates run before inspecting provider-specific
            # operation support. The second classification after completion can
            # still fail independently and uses the conservative ceiling.
            classify_external_effect(
                self.provider,
                self.contract.operation,
                dict(self.invocation.observation),
                {"preflight": True},
            )
        if self.invocation.failure_resource is not None:
            if self.contract.resource_policy == ResourcePolicy.NONE:
                raise ValidationError(
                    f"protected operation contract forbids failure resource settlement: {self.contract.name}"
                )
            if self.sdk.resources is None:
                raise ValidationError(
                    "protected operation failure resource settlement requires ResourceManager"
                )
        if self.invocation.preflight_usage is not None:
            if self.contract.resource_policy == ResourcePolicy.NONE:
                raise ValidationError(
                    f"protected operation contract forbids resource preflight: {self.contract.name}"
                )
            if self.sdk.resources is None:
                raise ValidationError("protected operation resource preflight requires ResourceManager")
            source = self.invocation.resource_source or self.contract.name
            self.sdk.resources.preflight(
                self.invocation.pid,
                self.invocation.preflight_usage,
                source=source,
                context=dict(self.invocation.resource_context),
            )
        elif self.contract.resource_policy == ResourcePolicy.REQUIRED:
            raise ValidationError(
                f"protected operation requires resource preflight: {self.contract.name}"
            )
        observation = to_jsonable(dict(self.invocation.observation))
        canonical_args = to_jsonable(dict(self.invocation.canonical_args))
        if not isinstance(observation, dict) or not isinstance(canonical_args, dict):
            raise ValidationError("protected operation contexts must serialize to objects")
        with self.sdk.store.transaction():
            self._reserve_decisions()
            if self.invocation.prepare is not None:
                self.invocation.prepare()
            effect = prepare_external_effect_intent(
                self.sdk.store,
                pid=self.invocation.pid,
                provider=self.contract.provider,
                operation=self.contract.operation,
                target=self.invocation.target,
                state_mutation=self.contract.state_mutation,
                information_flow=self.contract.information_flow,
                metadata={
                    "context": observation,
                    "protected_operation": {
                        "contract_name": self.contract.name,
                        "actor": self.invocation.actor,
                        "reservation_ids": list(self._reservation_ids),
                        "prepared_recovery": self.contract.prepared_recovery,
                    },
                },
                idempotency_key=self.invocation.idempotency_key,
                canonical_args=canonical_args,
            )
            self.effect_id = effect.effect_id

    def _validate_authority(self) -> None:
        if self.contract.authority_mode == AuthorityMode.RUNTIME_INTERNAL:
            return
        if not self.invocation.decisions:
            raise CapabilityDenied(
                f"protected provider operation requires an explicit capability decision: {self.contract.name}"
            )
        for decision in self.invocation.decisions:
            if not decision.allowed:
                raise CapabilityDenied(
                    f"protected provider operation received a denied capability decision: {decision.reason}"
                )
            if decision.subject != self.invocation.pid:
                raise CapabilityDenied(
                    "protected provider operation capability subject does not match the acting process"
                )

    def _reserve_decisions(self) -> None:
        seen: set[str] = set()
        for decision in self.invocation.decisions:
            cap_id = decision.consume_capability_id
            if cap_id is None or str(cap_id) in seen:
                continue
            seen.add(str(cap_id))
            reservation_id = self.sdk.capabilities.reserve_decision_use(
                decision,
                used_by=self.invocation.actor,
                reason=f"protected operation reserved authority for {self.contract.name}",
            )
            if reservation_id is not None:
                self._reservation_ids.append(reservation_id)

    def _dispatch(self, phase: ProviderPhase) -> None:
        if self.effect_id is None:
            raise ProtectedOperationProtocolError("protected operation has no prepared effect")
        mark_external_effect_dispatched(self.sdk.store, self.effect_id)
        current = self.sdk.store.get_external_effect(self.effect_id)
        if current is None:
            raise ProtectedOperationProtocolError("protected operation effect disappeared during dispatch")
        metadata = {
            **dict(current.provider_metadata),
            "active_provider_phase": {
                "name": phase.name,
                "state_mutation": phase.state_mutation,
                "information_flow": phase.information_flow,
            },
        }
        if not self.sdk.store.transition_external_effect(
            self.effect_id,
            expected_states=("dispatched",),
            transaction_state="dispatched",
            provider_metadata=metadata,
            updated_at=self._now(),
        ):
            raise ProtectedOperationProtocolError(
                f"protected operation phase dispatch cannot be persisted: {self.contract.name}:{phase.name}"
            )
        self._dispatched = True

    def _record_completed_phase(self, phase: ProviderPhase) -> None:
        if self.effect_id is None:
            raise ProtectedOperationProtocolError("protected operation has no effect for phase completion")
        current = self.sdk.store.get_external_effect(self.effect_id)
        if current is None:
            raise ProtectedOperationProtocolError("protected operation effect disappeared after provider call")
        completed = list(current.provider_metadata.get("completed_provider_phases") or [])
        completed.append(
            {
                "name": phase.name,
                "state_mutation": phase.state_mutation,
                "information_flow": phase.information_flow,
            }
        )
        metadata = {
            **dict(current.provider_metadata),
            "active_provider_phase": None,
            "completed_provider_phases": completed,
            "observed_state_mutation": any(bool(item.get("state_mutation")) for item in completed),
            "observed_information_flow": any(bool(item.get("information_flow")) for item in completed),
        }
        if not self.sdk.store.transition_external_effect(
            self.effect_id,
            expected_states=("dispatched",),
            transaction_state="dispatched",
            provider_metadata=metadata,
            updated_at=self._now(),
        ):
            error = ProtectedOperationProtocolError(
                f"protected operation phase completion cannot be persisted: {self.contract.name}:{phase.name}"
            )
            self._expect_settlement_evidence()
            self._commit_reservations_best_effort()
            self._finalize_unknown(error, f"{phase.name}_phase_evidence")
            raise error

    def _activate_boundary(self, phase: ProviderPhase) -> Token[_ActiveBoundary | None]:
        return _CURRENT_BOUNDARY.set(
            _ActiveBoundary(
                sdk_identity=self.sdk._identity,
                contract_name=self.contract.name,
                phase_name=phase.name,
                effect_id=str(self.effect_id),
            )
        )

    def _commit_reservations(self) -> None:
        if self._reservations_committed:
            return
        with self.sdk.store.transaction():
            for reservation_id in self._reservation_ids:
                self.sdk.capabilities.commit_reserved_use(
                    reservation_id,
                    committed_by=self.invocation.actor,
                    reason=f"protected operation crossed provider boundary: {self.contract.name}",
                )
        self._reservations_committed = True

    def _expect_settlement_evidence(self) -> None:
        self.sdk.operations.expect("audit", "event")

    def _commit_reservations_best_effort(self) -> None:
        try:
            self._commit_reservations()
        except Exception:
            pass

    def _restore_reservations(self) -> None:
        for reservation_id in reversed(self._reservation_ids):
            self.sdk.capabilities.restore_reserved_use(
                reservation_id,
                restored_by=self.invocation.actor,
                reason=f"protected operation certified not started: {self.contract.name}",
            )

    def _abort_not_started(self, reason: str) -> None:
        if self._terminal:
            return
        with self.sdk.store.transaction():
            if self.invocation.restore_not_started is not None:
                self.invocation.restore_not_started()
            self._restore_reservations()
            abandon_external_effect_intent(self.sdk.store, self.effect_id)
        self._terminal = True

    def _handle_not_started(
        self,
        error: ProviderEffectNotStarted,
        phase: ProviderPhase,
        *,
        outcome: str = "partial_not_started_after_prior_provider_effect",
    ) -> None:
        effectful_phases = [
            item
            for item in self._completed_phases
            if item.state_mutation or item.information_flow or item.commits_authority
        ]
        if not effectful_phases:
            self._abort_not_started("provider_certified_not_started")
            return
        self._commit_reservations_best_effort()
        evidence = self._failure_evidence(error, phase.name)
        state_mutation = any(item.state_mutation for item in effectful_phases)
        information_flow = any(item.information_flow for item in effectful_phases)
        classification = ExternalEffectClassification(
            rollback_class=ExternalEffectRollbackClass.UNKNOWN,
            rollback_status=ExternalEffectRollbackStatus.UNKNOWN,
            state_mutation=state_mutation,
            information_flow=information_flow,
            metadata={
                "outcome": outcome,
                "phase": phase.name,
                "error_type": type(error).__name__,
            },
        )
        self._settle_failure(
            classification,
            evidence,
            error=error,
            phase=phase.name,
        )

    def _finalize_unknown(self, error: BaseException, phase: str) -> None:
        if self._terminal or self.effect_id is None:
            return
        evidence = self._failure_evidence(error, phase)
        outcome = (
            "unknown_after_provider_success"
            if phase in {"caller_failed_after_provider", "protocol_incomplete"}
            else "unknown_after_provider_exception"
        )
        classification = ExternalEffectClassification(
            rollback_class=ExternalEffectRollbackClass.UNKNOWN,
            rollback_status=ExternalEffectRollbackStatus.UNKNOWN,
            state_mutation=self.contract.state_mutation,
            information_flow=self.contract.information_flow,
            metadata={
                "outcome": outcome,
                "phase": phase,
                "error_type": type(error).__name__,
            },
        )
        self._settle_failure(
            classification,
            evidence,
            error=error,
            phase=phase,
        )

    def _settle_failure(
        self,
        classification: ExternalEffectClassification,
        evidence: ProtectedOperationEvidence,
        *,
        error: BaseException,
        phase: str,
    ) -> None:
        settled = False
        try:
            effect_metadata, flattened_metadata = self._safe_effect_metadata(
                evidence,
                classification_keys=classification.metadata.keys(),
            )
            with self.sdk.store.transaction():
                event, audit_record = self._persist_evidence(evidence)
                record_external_effect(
                    self.sdk.store,
                    pid=self.invocation.pid,
                    provider=self.contract.provider,
                    operation=self.contract.operation,
                    target=self.invocation.target,
                    classification=classification,
                    audit_record=audit_record,
                    event=event,
                    metadata={
                        "context": dict(self.invocation.observation),
                        "provider_phases": self._phase_metadata(),
                        "result": effect_metadata,
                        **flattened_metadata,
                        "error_type": classification.metadata.get("error_type"),
                    },
                    intent_effect_id=self.effect_id,
                )
            settled = True
        except Exception:
            # The prepared/dispatched intent is the durable unknown evidence.
            pass
        self._terminal = True
        if settled:
            self._charge_resource(self._failure_resource_settlement(error, phase))

    def _failure_resource_settlement(
        self,
        error: BaseException,
        phase: str,
    ) -> ResourceSettlement | None:
        selected = self.invocation.failure_resource
        settlement = selected(error, phase) if callable(selected) else selected
        if settlement is not None:
            self._validate_resource_settlement(settlement, required=False)
            return settlement
        if self.contract.resource_policy != ResourcePolicy.REQUIRED:
            return None
        usage = self.invocation.preflight_usage
        if usage is None:
            raise ProtectedOperationProtocolError(
                f"protected operation requires failure resource settlement: {self.contract.name}"
            )
        return ResourceSettlement(
            usage=usage,
            source=self.invocation.resource_source or self.contract.name,
            context={
                **dict(self.invocation.resource_context),
                "failure_phase": phase,
                "error_type": type(error).__name__,
            },
        )

    def _validate_resource_settlement(
        self,
        resource: ResourceSettlement | None,
        *,
        required: bool = True,
    ) -> None:
        if (
            resource is None
            and required
            and self.contract.resource_policy == ResourcePolicy.REQUIRED
        ):
            raise ProtectedOperationProtocolError(
                f"protected operation requires resource settlement: {self.contract.name}"
            )
        if resource is not None and self.contract.resource_policy == ResourcePolicy.NONE:
            raise ProtectedOperationProtocolError(
                f"protected operation contract forbids resource settlement: {self.contract.name}"
            )
        if resource is not None and self.sdk.resources is None:
            raise ValidationError("protected operation resource settlement requires ResourceManager")

    def _charge_resource(self, resource: ResourceSettlement | None) -> None:
        if resource is None:
            return
        self.sdk.operations.expect("resource_charge")
        assert self.sdk.resources is not None
        self.sdk.resources.charge(
            self.invocation.pid,
            resource.usage,
            source=resource.source,
            context=dict(resource.context),
            allow_overage=resource.allow_overage,
            kill_on_exceed=resource.kill_on_exceed,
        )

    def _phase_metadata(self) -> list[dict[str, Any]]:
        return [
            {
                "name": phase.name,
                "state_mutation": phase.state_mutation,
                "information_flow": phase.information_flow,
            }
            for phase in self._completed_phases
        ]

    @staticmethod
    def _now() -> str:
        return utc_now()

    def _safe_effect_metadata(
        self,
        evidence: ProtectedOperationEvidence,
        *,
        classification_keys: Iterable[str] = (),
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        metadata = dict(evidence.effect_metadata)
        # These keys are owned by the SDK/effect ledger. Extension evidence may
        # add safe domain fields but cannot replace canonical lifecycle data.
        reserved = {
            "context",
            "effect_state",
            "provider_receipt",
            "result",
            "transaction_state",
            *classification_keys,
        }
        flattened = {key: value for key, value in metadata.items() if key not in reserved}
        return metadata, flattened

    def _failure_evidence(self, error: BaseException, phase: str) -> ProtectedOperationEvidence:
        if self.invocation.failure_evidence is not None:
            return self.invocation.failure_evidence(error, phase)
        event_type = (
            EventType.EXTERNAL_WRITE if self.contract.state_mutation else EventType.EXTERNAL_READ
        )
        return ProtectedOperationEvidence(
            event_type=event_type,
            event_source=self.invocation.actor,
            event_target=self.invocation.target,
            event_payload={
                "provider": self.contract.provider,
                "operation": self.contract.operation,
                "outcome": "unknown",
                "phase": phase,
                "error_type": type(error).__name__,
            },
            audit_action=f"{self.contract.name}.failed",
            audit_actor=self.invocation.actor,
            audit_target=self.invocation.target,
            audit_decision={
                "provider": self.contract.provider,
                "operation": self.contract.operation,
                "effect_outcome": "unknown",
                "phase": phase,
                "error_type": type(error).__name__,
            },
        )

    def _persist_evidence(
        self,
        evidence: ProtectedOperationEvidence,
    ) -> tuple[Event, AuditRecord]:
        event = self.sdk.events.emit(
            evidence.event_type,
            source=evidence.event_source,
            target=evidence.event_target,
            payload=dict(evidence.event_payload),
            priority=evidence.event_priority,
            correlation_id=evidence.correlation_id,
            causality=(
                {"audit_parent_record_id": evidence.parent_record_id}
                if evidence.parent_record_id is not None
                else None
            ),
        )
        audit_record = self.sdk.audit.record(
            actor=evidence.audit_actor,
            action=evidence.audit_action,
            target=evidence.audit_target,
            input_refs=list(evidence.input_refs),
            output_refs=list(evidence.output_refs),
            capability_refs=list(evidence.capability_refs),
            decision=dict(evidence.audit_decision),
            correlation_id=evidence.correlation_id,
            parent_record_id=evidence.parent_record_id,
        )
        return event, audit_record

    def _classification(
        self,
        context: Mapping[str, Any] | None,
        result: Any,
    ) -> ExternalEffectClassification:
        try:
            classification = classify_external_effect(
                self.provider,
                self.contract.operation,
                dict(context or self.invocation.observation),
                result,
            )
        except Exception as error:
            classification = ExternalEffectClassification(
                rollback_class=self.contract.classifier_failure_rollback_class,
                rollback_status=self.contract.classifier_failure_rollback_status,
                state_mutation=self.contract.state_mutation,
                information_flow=self.contract.information_flow,
                metadata={
                    "classification_fallback": self.contract.classifier_failure_label,
                    "classification_error_type": type(error).__name__,
                },
            )
        return classification

    def _classification_with_phase_floor(
        self,
        classification: ExternalEffectClassification,
    ) -> ExternalEffectClassification:
        """Never let a classifier erase an effect already declared by a phase."""
        phase_mutation = any(item.state_mutation for item in self._completed_phases)
        phase_flow = any(item.information_flow for item in self._completed_phases)
        return ExternalEffectClassification(
            rollback_class=classification.rollback_class,
            rollback_status=classification.rollback_status,
            state_mutation=bool(classification.state_mutation or phase_mutation),
            information_flow=bool(classification.information_flow or phase_flow),
            metadata=dict(classification.metadata),
        )

    def _require_active(self) -> None:
        if self._terminal:
            raise ProtectedOperationProtocolError(
                f"protected operation is already terminal: {self.contract.name}"
            )
        if self.effect_id is None:
            raise ProtectedOperationProtocolError(
                f"protected operation has not been entered: {self.contract.name}"
            )


__all__ = [
    "AuthorityMode",
    "PostProviderFailureMode",
    "ProtectedOperation",
    "ProtectedOperationContract",
    "ProtectedOperationEvidence",
    "ProtectedOperationInvocation",
    "ProtectedOperationProtocolError",
    "ProtectedOperationSDK",
    "ProviderEffectNotStartedResult",
    "ProviderPhase",
    "ResourcePolicy",
    "ResourceSettlement",
]
