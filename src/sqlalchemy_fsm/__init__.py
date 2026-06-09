from importlib.metadata import PackageNotFoundError, version

from . import events, exc, introspection
from .column import FSMColumn
from .introspection import aavailable_transitions, available_transitions
from .sqltypes import FSMField
from .transition import FSMCondition, async_transition, transition
from .validation import (
    _register_mapper_listener as _register_fsm_validator,
)
from .validation import (
    validate_fsm,
)

_register_fsm_validator()

try:
    __version__ = version("sqlalchemy-fsm")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0.dev0"
