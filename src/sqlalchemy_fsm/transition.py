"""The `@transition` decorator and the descriptor it produces."""

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
def sql_equality_cache(key: tuple[Any, str | None]) -> Any:
    """Memoize `Column == target` — building the SA expression is non-trivial."""
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

    def __init__(
        self,
        meta: FSMMeta,
        sqla_handle: "bound.SqlAlchemyHandle",
        payload_func: Callable[..., Any],
        owner_cls: type,
    ) -> None:
        self._sa_fsm_meta = meta
        self._sa_fsm_owner_cls = owner_cls
        self._sa_fsm_sqla_handle = sqla_handle
        self._sa_fsm_transition_fn = payload_func

    def __call__(self) -> Any:
        """SA filter expression matching rows whose state == this transition's target."""
        column = self._sa_fsm_sqla_handle.fsm_column
        target = self._sa_fsm_meta.target
        return sql_equality_cache.get_value((column, target))

    def is_(self, value: Any) -> Any:
        if isinstance(value, bool):
            return self().is_(value)
        # Non-bool argument: warn and return a sentinel False that, used as a
        # SA filter, matches nothing.
        warnings.warn(f"Unexpected is_ argument: {value!r}", stacklevel=2)
        return False


class InstanceBoundFsmTransition:
    __slots__ = (*ClassBoundFsmTransition.__slots__, "_sa_fsm_self", "_sa_fsm_bound_meta")

    def __init__(
        self,
        meta: FSMMeta,
        sqla_handle: "bound.SqlAlchemyHandle",
        transition_fn: Callable[..., Any],
        owner_cls: type,
        instance: Any,
    ) -> None:
        self._sa_fsm_meta = meta
        self._sa_fsm_transition_fn = transition_fn
        self._sa_fsm_owner_cls = owner_cls
        self._sa_fsm_self = instance
        self._sa_fsm_bound_meta = meta.get_bound(sqla_handle, transition_fn, ())

    def __call__(self) -> bool:
        """True if this instance is currently in the transition's target state."""
        bound_meta = self._sa_fsm_bound_meta
        return bound_meta.target_state == bound_meta.current_state

    def set(self, *args: Any, **kwargs: Any) -> None:
        """Execute the transition. Raises if the current state, permissions,
        or conditions don't allow it. Mutates the field in memory — commit
        the session yourself to persist."""
        bound_meta = self._sa_fsm_bound_meta
        func = self._sa_fsm_transition_fn

        if not bound_meta.transition_possible():
            raise exc.InvalidSourceStateError(
                f"Unable to switch from {bound_meta.current_state} "
                f"using method {func.__name__}"
            )
        if not bound_meta.permissions_met(args, kwargs):
            raise exc.PermissionDeniedError(
                f"Permission denied for transition {func.__name__}."
            )
        if not bound_meta.conditions_met(args, kwargs):
            raise exc.PreconditionError("Preconditions are not satisfied.")
        return bound_meta.to_next_state(args, kwargs)

    def can_proceed(self, *args: Any, **kwargs: Any) -> bool:
        bound_meta = self._sa_fsm_bound_meta
        return (
            bound_meta.transition_possible()
            and bound_meta.permissions_met(args, kwargs)
            and bound_meta.conditions_met(args, kwargs)
        )


class FsmTransition(InspectionAttrInfo):
    is_attribute = True
    extension_type = HYBRID_METHOD
    _sa_fsm_is_transition = True

    def __init__(self, meta: FSMMeta, set_function: Callable[..., Any]) -> None:
        self.meta = meta
        self.set_fn = set_function

    @overload
    def __get__(self, instance: None, owner: type) -> ClassBoundFsmTransition: ...
    @overload
    def __get__(self, instance: object, owner: type) -> InstanceBoundFsmTransition: ...

    def __get__(
        self, instance: Any, owner: type
    ) -> "ClassBoundFsmTransition | InstanceBoundFsmTransition":
        try:
            sql_alchemy_handle = owner._sa_fsm_sqlalchemy_handle
        except AttributeError:
            # Owner class is not bound to sqlalchemy handle object
            sql_alchemy_handle = bound.SqlAlchemyHandle(owner, instance)

        if instance is None:
            return ClassBoundFsmTransition(
                self.meta, sql_alchemy_handle, self.set_fn, owner
            )
        return InstanceBoundFsmTransition(
            self.meta, sql_alchemy_handle, self.set_fn, owner, instance
        )


def transition(
    source: SourceState = "*",
    target: str | None = None,
    conditions: Iterable[Callable[..., Any]] = (),
    permissions: Iterable[Callable[..., Any]] = (),
) -> Callable[[Any], FsmTransition]:
    def inner_transition(subject: Any) -> FsmTransition:
        if py_inspect.isfunction(subject):
            meta = FSMMeta(
                source, target, conditions, (), bound.BoundFSMFunction, permissions
            )
        elif py_inspect.isclass(subject):
            # Assume a class with multiple handles for various source states
            meta = FSMMeta(
                source, target, conditions, (), bound.BoundFSMClass, permissions
            )
        else:
            raise NotImplementedError(f"Do not know how to {subject!r}")

        return FsmTransition(meta, subject)

    return inner_transition
