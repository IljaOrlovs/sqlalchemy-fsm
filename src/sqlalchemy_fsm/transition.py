"""The `@transition` decorator and the descriptor it produces."""

import asyncio
import inspect as py_inspect
import warnings
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, overload

try:
    # SQLAlchemy 2.0+
    from sqlalchemy.ext.hybrid import HybridExtensionType

    HYBRID_METHOD = HybridExtensionType.HYBRID_METHOD
except ImportError:  # pragma: no cover
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


class _InstanceBoundBase:
    """Shared state + `__call__` for sync and async instance descriptors."""

    __slots__ = (
        "_sa_fsm_bound_meta",
        "_sa_fsm_meta",
        "_sa_fsm_owner_cls",
        "_sa_fsm_self",
        "_sa_fsm_sqla_handle",
        "_sa_fsm_transition_fn",
    )

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
        self._sa_fsm_sqla_handle = sqla_handle
        self._sa_fsm_bound_meta = meta.get_bound(sqla_handle, transition_fn, ())

    def __call__(self) -> bool:
        """True if this instance is currently in the transition's target state."""
        bound_meta = self._sa_fsm_bound_meta
        return bound_meta.target_state == bound_meta.current_state


class InstanceBoundFsmTransition(_InstanceBoundBase):
    __slots__ = ()

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


class AsyncInstanceBoundFsmTransition(_InstanceBoundBase):
    """Async sibling of `InstanceBoundFsmTransition`. Exposes only `aset`
    and `acan_proceed` — calling them outside a running event loop raises."""

    __slots__ = ()

    @staticmethod
    def _require_running_loop() -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError as err:
            raise exc.SetupError(
                "async transitions require a running asyncio event loop; "
                "call `aset()`/`acan_proceed()` from inside an async function"
            ) from err

    async def aset(self, *args: Any, **kwargs: Any) -> None:
        """Execute an async transition. Awaits async handlers, conditions,
        and permissions. Mutates the field in memory — commit the session
        yourself to persist."""
        self._require_running_loop()
        bound_meta = self._sa_fsm_bound_meta
        func = self._sa_fsm_transition_fn

        if not bound_meta.transition_possible():
            raise exc.InvalidSourceStateError(
                f"Unable to switch from {bound_meta.current_state} "
                f"using method {func.__name__}"
            )
        if not await bound_meta.apermissions_met(args, kwargs):
            raise exc.PermissionDeniedError(
                f"Permission denied for transition {func.__name__}."
            )
        if not await bound_meta.aconditions_met(args, kwargs):
            raise exc.PreconditionError("Preconditions are not satisfied.")
        return await bound_meta.ato_next_state(args, kwargs)

    async def acan_proceed(self, *args: Any, **kwargs: Any) -> bool:
        self._require_running_loop()
        bound_meta = self._sa_fsm_bound_meta
        return (
            bound_meta.transition_possible()
            and await bound_meta.apermissions_met(args, kwargs)
            and await bound_meta.aconditions_met(args, kwargs)
        )


class FsmTransition(InspectionAttrInfo):
    """Base descriptor for both sync and async transitions.

    The sync vs async distinction is a runtime property of `meta.is_async`,
    but static checkers can't see through that — they need a concrete
    return type on `__get__`. We therefore expose two thin subclasses
    (`SyncFsmTransition` / `AsyncFsmTransition`) whose only purpose is to
    override the `__get__` overloads with the concrete instance-bound
    type. The runtime `__get__` lives here.
    """

    is_attribute = True
    extension_type = HYBRID_METHOD
    _sa_fsm_is_transition = True

    def __init__(
        self,
        meta: FSMMeta,
        set_function: Callable[..., Any],
        column_ref: Any = None,
    ) -> None:
        self.meta = meta
        self.set_fn = set_function
        # Set by `FSMColumn.transition`; `None` for the legacy module-level
        # `@transition`, which resolves to the model's single FSM column.
        self.column_ref = column_ref

    def __get__(  # noqa: E501
        self, instance: Any, owner: type
    ) -> "ClassBoundFsmTransition | InstanceBoundFsmTransition | AsyncInstanceBoundFsmTransition":
        try:
            sql_alchemy_handle = owner._sa_fsm_sqlalchemy_handle
        except AttributeError:
            sql_alchemy_handle = bound.resolve_handle(owner, instance, self.column_ref)

        if instance is None:
            return ClassBoundFsmTransition(
                self.meta, sql_alchemy_handle, self.set_fn, owner
            )
        if self.meta.is_async:
            return AsyncInstanceBoundFsmTransition(
                self.meta, sql_alchemy_handle, self.set_fn, owner, instance
            )
        return InstanceBoundFsmTransition(
            self.meta, sql_alchemy_handle, self.set_fn, owner, instance
        )


class SyncFsmTransition(FsmTransition):
    """Descriptor produced by `@transition` — typed for sync handlers."""

    if TYPE_CHECKING:
        @overload  # type: ignore[override]
        def __get__(self, instance: None, owner: Any) -> ClassBoundFsmTransition: ...
        @overload
        def __get__(self, instance: object, owner: Any) -> InstanceBoundFsmTransition: ...
        def __get__(self, instance: Any, owner: Any) -> Any: ...


class AsyncFsmTransition(FsmTransition):
    """Descriptor produced by `@async_transition` — typed for async handlers."""

    if TYPE_CHECKING:
        @overload  # type: ignore[override]
        def __get__(self, instance: None, owner: Any) -> ClassBoundFsmTransition: ...
        @overload
        def __get__(
            self, instance: object, owner: Any
        ) -> AsyncInstanceBoundFsmTransition: ...
        def __get__(self, instance: Any, owner: Any) -> Any: ...


if TYPE_CHECKING:
    from typing import TypeVar

    _T = TypeVar("_T", bound=FsmTransition)


def _make_transition(
    is_async: bool,
    source: SourceState,
    target: str | None,
    conditions: Iterable[Callable[..., Any]],
    permissions: Iterable[Callable[..., Any]],
) -> Callable[[Any], FsmTransition]:
    fn_cls = bound.AsyncBoundFSMFunction if is_async else bound.BoundFSMFunction
    cls_cls = bound.AsyncBoundFSMClass if is_async else bound.BoundFSMClass
    transition_cls: type[FsmTransition] = (
        AsyncFsmTransition if is_async else SyncFsmTransition
    )

    def inner(subject: Any) -> FsmTransition:
        if py_inspect.isfunction(subject):
            bound_cls = fn_cls
        elif py_inspect.isclass(subject):
            bound_cls = cls_cls
        else:
            raise NotImplementedError(f"Do not know how to {subject!r}")
        meta = FSMMeta(
            source, target, conditions, (), bound_cls, permissions, is_async=is_async
        )
        return transition_cls(meta, subject)

    return inner


def transition(
    source: SourceState = "*",
    target: str | None = None,
    conditions: Iterable[Callable[..., Any]] = (),
    permissions: Iterable[Callable[..., Any]] = (),
) -> Callable[[Any], SyncFsmTransition]:
    return _make_transition(False, source, target, conditions, permissions)  # type: ignore[return-value]


def async_transition(
    source: SourceState = "*",
    target: str | None = None,
    conditions: Iterable[Callable[..., Any]] = (),
    permissions: Iterable[Callable[..., Any]] = (),
) -> Callable[[Any], AsyncFsmTransition]:
    """Like `@transition`, but the handler — and any conditions/permissions —
    may be `async def`. Invoke via `await instance.<name>.aset(...)`; only
    works inside a running asyncio event loop. Sync callables remain valid."""
    return _make_transition(True, source, target, conditions, permissions)  # type: ignore[return-value]
