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


class PolicyDenied(LibOSError):
    pass


class ProcessError(LibOSError):
    pass


class ValidationError(LibOSError):
    pass


class SandboxError(LibOSError):
    pass
