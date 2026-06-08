"""Exceptions raised by the FSM machinery."""


class FSMException(Exception):
    """Base class for every exception this library raises."""


class PreconditionError(FSMException):
    """A `conditions=` callable returned falsy — the transition is blocked."""


class SetupError(FSMException):
    """The model or transition is misconfigured (e.g. missing/duplicate FSMField,
    incompatible parent/child source sets, handler/condition arg mismatch)."""


class InvalidSourceStateError(FSMException, NotImplementedError):
    """The current state isn't in the transition's allowed source set."""
