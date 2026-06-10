"""Exceptions raised by the FSM machinery."""

from typing import Any


class FSMException(Exception):
    """Base class for every exception this library raises."""


class SetupError(FSMException):
    """The model or transition is misconfigured (e.g. missing/duplicate FSMField,
    incompatible parent/child source sets, handler/condition arg mismatch)."""


class NoFSMColumnError(SetupError):
    """The model has no FSMField column."""


class MultipleFSMColumnsError(SetupError):
    """The model has more than one FSMField column."""


class _TransitionFailure(FSMException):
    """Shared base for runtime transition failures.

    Carries structured context so callers can branch on the failure
    without parsing the message — set via ``.current_state``,
    ``.target_state``, and ``.transition_name`` attributes.
    """

    current_state: Any = None
    target_state: Any = None
    transition_name: str | None = None

    def __init__(
        self,
        message: str,
        *,
        current_state: Any = None,
        target_state: Any = None,
        transition_name: str | None = None,
    ) -> None:
        super().__init__(message)
        self.current_state = current_state
        self.target_state = target_state
        self.transition_name = transition_name


class PreconditionError(_TransitionFailure):
    """A `conditions=` callable returned falsy — the transition is blocked."""


class InvalidSourceStateError(_TransitionFailure):
    """The current state isn't in the transition's allowed source set."""


class PermissionDeniedError(_TransitionFailure):
    """A `permissions=` callable returned falsy — the caller is not allowed
    to execute this transition."""
