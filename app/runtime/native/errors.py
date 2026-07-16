class NativeRuntimeError(Exception):
    """Base error for the native workflow runtime."""


class WorkflowNotFoundError(NativeRuntimeError):
    """Raised when a workflow cannot be found."""


class ExecutionNotFoundError(NativeRuntimeError):
    """Raised when an execution cannot be found."""


class ExecutionPausedError(NativeRuntimeError):
    """Raised when execution is paused."""


class ExecutionApprovalSuspendedError(ExecutionPausedError):
    """Raised after an approval checkpoint is durable so the worker can exit."""


class ExecutionCancelledError(NativeRuntimeError):
    """Raised when execution is cancelled."""


class ApprovalRequiredError(NativeRuntimeError):
    """Raised when a tool call requires approval and approval is not granted."""


class ToolExecutionError(NativeRuntimeError):
    """Raised when tool execution fails."""


class MaxIterationsReachedError(NativeRuntimeError):
    """Raised when the agent loop reaches its maximum iteration count."""
