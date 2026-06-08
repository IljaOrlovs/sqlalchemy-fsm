"""Transition decorator."""

import inspect as py_inspect
import warnings
from collections.abc import Callable, Iterable
from typing import Any, overload

try:
    # SQLAlchemy 2.0+
    from sqlalchemy.ext.hybrid import HybridExtensionType

    HYBRID_METHOD = HybridExtensionType.HYBRID_METHOD
except ImportError:
    # SQLAlchemy 1.x
    from sqlalchemy.ext.hybrid import (
        HYBRID_METHOD,  # pyright: ignore[reportAttributeAccessIssue]
    )
from sqlalchemy.orm.interfaces import InspectionAttrInfo

from . import bound, cache, exc
from .meta import FSMMeta

SourceState = str | None | Iterable[str | None]


@cache.dict_cache
def sql_equality_cache(key):
    """It takes a bit of time for sqlalchemy to generate these.

    So I'm caching them.
    """
    (column, target) = key
    if not target:
        raise exc.SetupError("Target must be defined.")
    return column == target


class ClassBoundFsmTransition:
    __slots__ = (
        "_sa_fsm_meta",
        "_sa_fsm_owner_cls",
        "_sa_fsm_sqla_handle",
        "_sa_fsm_transition_fn",
    )

    def __init__(self, meta, sqla_handle, payload_func, owner_cls):
        self._sa_fsm_meta = meta
        self._sa_fsm_owner_cls = owner_cls
        self._sa_fsm_sqla_handle = sqla_handle
        self._sa_fsm_transition_fn = payload_func

    def __call__(self):
        """Return a SQLAlchemy filter for this particular state."""
        column = self._sa_fsm_sqla_handle.fsm_column
        target = self._sa_fsm_meta.target
        return sql_equality_cache.get_value((column, target))

    def is_(self, value):
        if isinstance(value, bool):
            out = self().is_(value)
        else:
            warnings.warn(f"Unexpected is_ argument: {value!r}", stacklevel=2)
            # Can be used as sqlalchemy filer. Won't match anything
            out = False
        return out


class InstanceBoundFsmTransition:
    __slots__ = (*ClassBoundFsmTransition.__slots__, "_sa_fsm_self", "_sa_fsm_bound_meta")

    def __init__(self, meta, sqla_handle, transition_fn, owner_cls, instance):
        self._sa_fsm_meta = meta
        self._sa_fsm_transition_fn = transition_fn
        self._sa_fsm_owner_cls = owner_cls
        self._sa_fsm_self = instance
        self._sa_fsm_bound_meta = meta.get_bound(sqla_handle, transition_fn, ())

    def __call__(self):
        """Check if this is the current state of the object."""
        bound_meta = self._sa_fsm_bound_meta
        return bound_meta.target_state == bound_meta.current_state

    def set(self, *args, **kwargs):
        """Transition the FSM to this new state."""
        bound_meta = self._sa_fsm_bound_meta
        func = self._sa_fsm_transition_fn

        if not bound_meta.transition_possible():
            raise exc.InvalidSourceStateError(
                f"Unable to switch from {bound_meta.current_state} "
                f"using method {func.__name__}"
            )
        if not bound_meta.conditions_met(args, kwargs):
            raise exc.PreconditionError("Preconditions are not satisfied.")
        return bound_meta.to_next_state(args, kwargs)

    def can_proceed(self, *args, **kwargs):
        bound_meta = self._sa_fsm_bound_meta
        return bound_meta.transition_possible() and bound_meta.conditions_met(
            args, kwargs
        )


class FsmTransition(InspectionAttrInfo):
    is_attribute = True
    extension_type = HYBRID_METHOD
    _sa_fsm_is_transition = True

    def __init__(self, meta, set_function):
        self.meta = meta
        self.set_fn = set_function

    @overload
    def __get__(self, instance: None, owner: type) -> ClassBoundFsmTransition: ...
    @overload
    def __get__(self, instance: object, owner: type) -> InstanceBoundFsmTransition: ...

    def __get__(self, instance, owner):
        try:
            sql_alchemy_handle = owner._sa_fsm_sqlalchemy_handle
        except AttributeError:
            # Owner class is not bound to sqlalchemy handle object
            sql_alchemy_handle = bound.SqlAlchemyHandle(owner, instance)

        if instance is None:
            return ClassBoundFsmTransition(
                self.meta, sql_alchemy_handle, self.set_fn, owner
            )
        else:
            return InstanceBoundFsmTransition(
                self.meta, sql_alchemy_handle, self.set_fn, owner, instance
            )


def transition(
    source: SourceState = "*",
    target: str | None = None,
    conditions: Iterable[Callable[..., Any]] = (),
) -> Callable[[Any], FsmTransition]:
    def inner_transition(subject):
        if py_inspect.isfunction(subject):
            meta = FSMMeta(source, target, conditions, (), bound.BoundFSMFunction)
        elif py_inspect.isclass(subject):
            # Assume a class with multiple handles for various source states
            meta = FSMMeta(source, target, conditions, (), bound.BoundFSMClass)
        else:
            raise NotImplementedError(f"Do not know how to {subject!r}")

        return FsmTransition(meta, subject)

    return inner_transition
