"""Exceptions raised by the FSM machinery."""


class FSMException(Exception):
    """Base class for every exception this library raises."""


class PreconditionError(FSMException):
    """A `conditions=` callable returned falsy — the transition is blocked."""


class SetupError(FSMException):
    """The model or transition is misconfigured (e.g. missing/duplicate FSMField,
    incompatible parent/child source sets, handler/condition arg mismatch)."""


class NoFSMColumnError(SetupError):
    """The model has no FSMField column."""


class MultipleFSMColumnsError(SetupError):
    """The model has more than one FSMField column."""


class InvalidSourceStateError(FSMException):
    """The current state isn't in the transition's allowed source set."""


class PermissionDeniedError(FSMException):
    """A `permissions=` callable returned falsy — the caller is not allowed
    to execute this transition."""
