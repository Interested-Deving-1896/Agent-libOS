from __future__ import annotations


class LibOSError(Exception):
    """Base exception for Agent libOS runtime errors."""


class NotFound(LibOSError):
    pass


class CapabilityDenied(LibOSError):
    pass


class HumanApprovalRequired(LibOSError):
    def __init__(self, request_id: str, message: str):
        super().__init__(message)
        self.request_id = request_id


class HumanResponseRequired(HumanApprovalRequired):
    pass


class ProcessWaitRequired(LibOSError):
    def __init__(self, child_pid: str, message: str, resume_action: dict | None = None):
        super().__init__(message)
        self.child_pid = child_pid
        self.resume_action = dict(resume_action) if resume_action is not None else None


class ProcessMessageWaitRequired(LibOSError):
    def __init__(self, recipient_pid: str, filters: dict, message: str):
        super().__init__(message)
        self.recipient_pid = recipient_pid
        self.filters = dict(filters)


class PolicyDenied(LibOSError):
    pass


class ProcessError(LibOSError):
    pass


class ProcessRevisionConflict(ProcessError):
    """A process mutation lost its compare-and-swap race."""

    pass


class RuntimePublicationPending(ProcessError):
    """A linked runtime publication still owns an operation's final outcome."""

    def __init__(
        self,
        *,
        publication_id: str,
        operation_id: str,
        state: str,
        phase: str,
    ) -> None:
        self.publication_id = str(publication_id)
        self.operation_id = str(operation_id)
        self.state = str(state)
        self.phase = str(phase)
        super().__init__(
            "runtime publication outcome remains pending: "
            f"{self.publication_id} ({self.state}/{self.phase})"
        )


class RuntimeRecoveryRequired(ProcessError):
    """A failed exec compensation requires reopen before further mutation."""

    def __init__(
        self,
        *,
        publication_id: str,
        operation_id: str,
        pid: str,
        state: str,
        phase: str,
        _issuer_token: object | None = None,
    ) -> None:
        self.publication_id = str(publication_id)
        self.operation_id = str(operation_id)
        self.pid = str(pid)
        self.state = str(state)
        self.phase = str(phase)
        self._issuer_token = _issuer_token
        super().__init__(
            "runtime recovery is required before further mutation: "
            f"{self.publication_id} ({self.state}/{self.phase})"
        )


class ResourceLimitExceeded(ProcessError):
    pass


class ValidationError(LibOSError):
    pass


class DurableObjectFinalizerUnavailable(ValidationError):
    """A durable cleanup intent has no restart-stable handler."""

    pass


class ProviderHostError(LibOSError, RuntimeError):
    """Stable public replacement for an exception raised by a Host provider."""

    def __init__(self, *, code: str, error_type: str, correlation_id: str):
        self.code = str(code)
        self.error_type = str(error_type)
        self.correlation_id = str(correlation_id)
        super().__init__(
            f"{self.code}: {self.error_type} (correlation_id={self.correlation_id})"
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "error_type": self.error_type,
            "correlation_id": self.correlation_id,
        }


class UnsupportedStoreVersion(ValidationError):
    """The runtime store belongs to an unsupported on-disk schema generation."""

    pass


class SandboxError(LibOSError):
    pass
