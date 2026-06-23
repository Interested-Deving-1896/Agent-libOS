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


class ResourceLimitExceeded(ProcessError):
    pass


class ValidationError(LibOSError):
    pass


class SandboxError(LibOSError):
    pass
