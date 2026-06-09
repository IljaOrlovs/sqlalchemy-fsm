"""The `@transition` decorator and the descriptor it produces."""

import asyncio
import inspect as py_inspect
import warnings
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import (
    TYPE_CHECKING,
    Any,
    Concatenate,
    Generic,
    ParamSpec,
    Protocol,
    TypeVar,
    cast,
    overload,
)

if TYPE_CHECKING:
    from .column import FSMColumn

from sqlalchemy.ext.hybrid import HybridExtensionType
from sqlalchemy.orm.interfaces import InspectionAttrInfo
from sqlalchemy.sql.expression import false as sql_false

from . import bound, cache, exc
from .meta import FSMMeta

HYBRID_METHOD = HybridExtensionType.HYBRID_METHOD

SourceState = str | None | Iterable[str | None]

#: ParamSpec for the handler's user-facing signature (everything after ``self``).
P = ParamSpec("P")
#: Handler return type. Preserved on ``.fn`` so direct calls in tests keep it;
#: ``set()`` / ``aset()`` discard the value and return ``None`` regardless.
R = TypeVar("R")


class FSMCondition(Protocol):
    """Public-API shape for `conditions=` / `permissions=` callables.

    Each callable is invoked as `fn(instance, *args, **kwargs)` and must
    return something truthy to allow the transition.

    Not parameterized against the handler's `ParamSpec`: pyright can't
    keep the handler's `P` free at the outer `transition(...)` call
    *and* bind it from the decoratee, so we keep conditions loose and
    let the handler/`.fn`/`.set` carry the precision.
    """

    def __call__(self, instance: Any, /, *args: Any, **kwargs: Any) -> Any: ...


@cache.weak_key_cache
def _column_target_table(column: Any) -> dict[str, Any]:
    """Per-column dict of ``target_state → (column == target_state)``.

    A WeakKeyDictionary indexed by Column instance, so dynamically-built
    mapped classes (test factories) don't pin themselves forever via
    this cache. Inner dict is small (one entry per target) and lives
    alongside the column.
    """
    return {}


def sql_equality_for(column: Any, target: str | None) -> Any:
    """Memoize ``Column == target`` — building the SA expression is non-trivial."""
    if not target:
        raise exc.SetupError("Target must be defined.")
    by_target = _column_target_table.get_value(column)
    try:
        return by_target[target]
    except KeyError:
        expr = column == target
        by_target[target] = expr
        return expr


def _failure_context(bound_meta: Any, func: Callable[..., Any]) -> dict[str, Any]:
    """Common kwargs splatted into every runtime FSM-failure exception.

    Centralises the four pieces of context (current/target state plus
    the handler name) so the six raise sites in `set` / `aset` don't
    drift out of sync as new fields are added.
    """
    return {
        "current_state": bound_meta.current_state,
        "target_state": bound_meta.target_state,
        "transition_name": func.__name__,
    }


class ClassBoundFsmTransition(Generic[P, R]):
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
        payload_func: Callable[Concatenate[Any, P], R],
        owner_cls: type,
    ) -> None:
        self._sa_fsm_meta = meta
        self._sa_fsm_owner_cls = owner_cls
        self._sa_fsm_sqla_handle = sqla_handle
        self._sa_fsm_transition_fn = payload_func

    def __call__(self) -> Any:
        """SA filter expression matching rows whose state == this transition's target."""
        handle = self._sa_fsm_sqla_handle
        if handle is None:
            # This descriptor was reached via the synthetic dispatcher
            # subclass walked by `inherited_bound_classes` — there's no
            # backing column to compare against.
            raise exc.SetupError(
                "ClassBoundFsmTransition has no SqlAlchemyHandle; this handle "
                "was produced by introspection of a class-based transition's "
                "synthetic dispatcher subclass and is not meant to be invoked."
            )
        target = self._sa_fsm_meta.target
        return sql_equality_for(handle.fsm_column, target)

    @property
    def fn(self) -> Callable[Concatenate[Any, P], R]:
        """The raw handler the user decorated — for direct calling in tests.

        ``BlogPost.publish.fn(post)`` runs the body verbatim, skipping
        source-state, permission, and condition checks. Useful when the
        body has its own side effects worth testing in isolation. For
        class-grouped transitions this is the wrapper class; reach for
        its sub-handlers as `.fn.from_draft` etc.
        """
        return self._sa_fsm_transition_fn

    def is_(self, value: Any) -> Any:
        if isinstance(value, bool):
            return self().is_(value)
        # Non-bool argument: warn and return a SA `false()` literal so that
        # callers using this in `query.filter(...)` get a well-defined
        # "matches nothing" instead of Python's bare `False`, which SA can
        # mishandle depending on dialect/version.
        warnings.warn(f"Unexpected is_ argument: {value!r}", stacklevel=2)
        return sql_false()


class _InstanceBoundBase(Generic[P, R]):
    """Shared state + `is_current` for sync and async instance descriptors."""

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
        transition_fn: Callable[Concatenate[Any, P], R],
        owner_cls: type,
        instance: Any,
    ) -> None:
        self._sa_fsm_meta = meta
        self._sa_fsm_transition_fn = transition_fn
        self._sa_fsm_owner_cls = owner_cls
        self._sa_fsm_self = instance
        self._sa_fsm_sqla_handle = sqla_handle
        self._sa_fsm_bound_meta = meta.get_bound(sqla_handle, transition_fn, ())

    @property
    def is_current(self) -> bool:
        """True if this instance is currently in the transition's target state.

        Equivalent to ``instance.state == "<target>"`` but reads the
        target off the transition itself, so renaming a state in one
        place doesn't drift the check site.
        """
        bound_meta = self._sa_fsm_bound_meta
        return bound_meta.target_state == bound_meta.current_state

    @property
    def fn(self) -> Callable[Concatenate[Any, P], R]:
        """The raw handler the user decorated — for direct calling in tests.

        ``post.publish.fn(post)`` runs the body verbatim, skipping every
        guard. Equivalent to the class-bound `BlogPost.publish.fn` —
        provided here so tests don't have to reach back to the class.
        """
        return self._sa_fsm_transition_fn


class InstanceBoundFsmTransition(_InstanceBoundBase[P, R]):
    __slots__ = ()

    def set(self, *args: P.args, **kwargs: P.kwargs) -> None:
        """Execute the transition. Raises if the current state, permissions,
        or conditions don't allow it. Mutates the field in memory — commit
        the session yourself to persist.

        `*args` / `**kwargs` are forwarded to permissions, conditions,
        and the handler — typed to match the handler's signature
        (everything after ``self``).
        """
        bound_meta = self._sa_fsm_bound_meta
        func = self._sa_fsm_transition_fn

        ctx = _failure_context(bound_meta, func)
        if not bound_meta.transition_possible():
            raise exc.InvalidSourceStateError(
                f"Unable to switch from {bound_meta.current_state} "
                f"using method {func.__name__}",
                **ctx,
            )
        if not bound_meta.permissions_met(args, kwargs):
            raise exc.PermissionDeniedError(
                f"Permission denied for transition {func.__name__}.", **ctx
            )
        if not bound_meta.conditions_met(args, kwargs):
            raise exc.PreconditionError("Preconditions are not satisfied.", **ctx)
        return bound_meta.to_next_state(args, kwargs, transition_name=func.__name__)

    def can_proceed(self, *args: P.args, **kwargs: P.kwargs) -> bool:
        # Delegate to `would_succeed` so the result mirrors what `set()`
        # would actually do — including the "exactly one accepted sub-handler"
        # rule for class-based transitions.
        return self._sa_fsm_bound_meta.would_succeed(args, kwargs)


class AsyncInstanceBoundFsmTransition(_InstanceBoundBase[P, R]):
    """Async sibling of `InstanceBoundFsmTransition`. Exposes `aset` /
    `acan_proceed`. Calling `aset` / `acan_proceed` outside a running
    event loop raises. The `.is_current` predicate is sync (a plain
    attribute compare) on both sync and async transitions.
    """

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

    async def aset(self, *args: P.args, **kwargs: P.kwargs) -> None:
        """Execute an async transition. Awaits async handlers, conditions,
        and permissions. Mutates the field in memory — commit the session
        yourself to persist."""
        self._require_running_loop()
        bound_meta = self._sa_fsm_bound_meta
        func = self._sa_fsm_transition_fn

        ctx = _failure_context(bound_meta, func)
        if not bound_meta.transition_possible():
            raise exc.InvalidSourceStateError(
                f"Unable to switch from {bound_meta.current_state} "
                f"using method {func.__name__}",
                **ctx,
            )
        if not await bound_meta.apermissions_met(args, kwargs):
            raise exc.PermissionDeniedError(
                f"Permission denied for transition {func.__name__}.", **ctx
            )
        if not await bound_meta.aconditions_met(args, kwargs):
            raise exc.PreconditionError("Preconditions are not satisfied.", **ctx)
        return await bound_meta.ato_next_state(
            args, kwargs, transition_name=func.__name__
        )

    async def acan_proceed(self, *args: P.args, **kwargs: P.kwargs) -> bool:
        self._require_running_loop()
        return await self._sa_fsm_bound_meta.awould_succeed(args, kwargs)


class FsmTransition(Generic[P, R], InspectionAttrInfo):
    """Base descriptor for both sync and async transitions.

    Generic over the handler's ParamSpec (``P``, every arg after
    ``self``) and return type (``R``). The runtime ``__get__`` lives
    here; ``SyncFsmTransition`` / ``AsyncFsmTransition`` exist purely
    to give pyright a concrete instance-bound return type.
    """

    is_attribute = True
    extension_type = HYBRID_METHOD
    _sa_fsm_is_transition = True

    def __init__(
        self,
        meta: FSMMeta,
        set_function: Callable[Concatenate[Any, P], R],
        column_ref: "FSMColumn | None" = None,
    ) -> None:
        self.meta = meta
        self.set_fn = set_function
        # Set by `FSMColumn.transition`; `None` for the bare module-level
        # `@transition`, which resolves at call time to the model's sole
        # FSM column.
        self.column_ref: FSMColumn | None = column_ref

    @property
    def fn(self) -> Callable[Concatenate[Any, P], R]:
        """The raw handler — public alias of `set_fn`, for tests.

        Read it to call the body directly; assign to it to swap the
        handler (e.g. from inside a test via `monkeypatch.setattr`).
        The descriptor lives at ``MyModel.__dict__["<name>"]``; each
        attribute access on the model rebuilds the bound wrappers, so
        a patch on `fn` here propagates to subsequent `set()` calls
        through the model.
        """
        return self.set_fn

    @fn.setter
    def fn(self, value: Callable[Concatenate[Any, P], R]) -> None:
        self.set_fn = value

    def __get__(
        self, instance: Any, owner: type
    ) -> "ClassBoundFsmTransition[P, R] | InstanceBoundFsmTransition[P, R] | AsyncInstanceBoundFsmTransition[P, R]":  # noqa: E501
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


class SyncFsmTransition(FsmTransition[P, R]):
    """Descriptor produced by `@transition` — typed for sync handlers."""

    if TYPE_CHECKING:

        @overload  # type: ignore[override]
        def __get__(
            self, instance: None, owner: Any
        ) -> ClassBoundFsmTransition[P, R]: ...
        @overload
        def __get__(
            self, instance: object, owner: Any
        ) -> InstanceBoundFsmTransition[P, R]: ...
        def __get__(self, instance: Any, owner: Any) -> Any: ...


class AsyncFsmTransition(FsmTransition[P, R]):
    """Descriptor produced by `@async_transition` — typed for async handlers."""

    if TYPE_CHECKING:

        @overload  # type: ignore[override]
        def __get__(
            self, instance: None, owner: Any
        ) -> ClassBoundFsmTransition[P, R]: ...
        @overload
        def __get__(
            self, instance: object, owner: Any
        ) -> AsyncInstanceBoundFsmTransition[P, R]: ...
        def __get__(self, instance: Any, owner: Any) -> Any: ...


def _make_transition(
    is_async: bool,
    source: SourceState,
    target: str | None,
    conditions: Iterable[Callable[..., Any]],
    permissions: Iterable[Callable[..., Any]],
    custom: Mapping[str, Any] | None,
) -> Callable[[Any], FsmTransition[Any, Any]]:
    fn_cls = bound.AsyncBoundFSMFunction if is_async else bound.BoundFSMFunction
    cls_cls = bound.AsyncBoundFSMClass if is_async else bound.BoundFSMClass
    transition_cls: type[FsmTransition[Any, Any]] = (
        AsyncFsmTransition if is_async else SyncFsmTransition
    )

    def inner(subject: Any) -> FsmTransition[Any, Any]:
        # Classes go through the sub-handler dispatcher path; anything
        # else callable (function, lambda, functools.partial, callable
        # instance) is treated as a single handler. Non-callables are
        # a setup mistake — reject loudly.
        if py_inspect.isclass(subject):
            bound_cls = cls_cls
        elif callable(subject):
            bound_cls = fn_cls
        else:
            raise exc.SetupError(
                f"@transition expects a callable or class; got {subject!r}"
            )
        meta = FSMMeta(
            source,
            target,
            conditions,
            (),
            bound_cls,
            permissions,
            custom=custom,
        )
        return transition_cls(meta, subject)

    return inner


# Inner decorator returned by `transition(...)` / `async_transition(...)`.
# Two overload arms per Protocol: a real function gets full ParamSpec /
# return-type precision; a class-grouped transition falls back to `Any`
# because dispatch-by-source isn't representable in the type system.
# The Protocols themselves are NOT parameterized — pyright infers P and
# R at the decoration call site (where the user's function is bound),
# not at the outer `transition(...)` call.


class _SyncTransitionDecorator(Protocol):
    @overload
    def __call__(
        self, subject: Callable[Concatenate[Any, P], R]
    ) -> SyncFsmTransition[P, R]: ...
    @overload
    def __call__(self, subject: type) -> SyncFsmTransition[Any, Any]: ...


class _AsyncTransitionDecorator(Protocol):
    @overload
    def __call__(
        self, subject: Callable[Concatenate[Any, P], Awaitable[R]]
    ) -> AsyncFsmTransition[P, R]: ...
    @overload
    def __call__(
        self, subject: Callable[Concatenate[Any, P], R]
    ) -> AsyncFsmTransition[P, R]: ...
    @overload
    def __call__(self, subject: type) -> AsyncFsmTransition[Any, Any]: ...


def transition(
    source: SourceState = "*",
    target: str | None = None,
    conditions: Iterable[FSMCondition] = (),
    permissions: Iterable[FSMCondition] = (),
    custom: Mapping[str, Any] | None = None,
) -> _SyncTransitionDecorator:
    """Decorate a method as a state transition.

    `custom` is a free-form metadata dict — sqlalchemy-fsm ignores it,
    but it's available via `Model.attr.meta.custom` for tools to read
    (admin labels, button text, RBAC tags, etc.). The dict is frozen
    on decoration.

    For type checkers: `@transition` is generic over the handler's
    ``ParamSpec`` so `.fn`, `.set`, and `.can_proceed` reveal the
    handler's true signature in editors. Class-grouped transitions
    (`@transition class publish: ...`) fall back to `Any` because
    dispatch-by-source isn't representable in the type system.
    """
    return cast(
        "_SyncTransitionDecorator",
        _make_transition(False, source, target, conditions, permissions, custom),
    )


def async_transition(
    source: SourceState = "*",
    target: str | None = None,
    conditions: Iterable[FSMCondition] = (),
    permissions: Iterable[FSMCondition] = (),
    custom: Mapping[str, Any] | None = None,
) -> _AsyncTransitionDecorator:
    """Like `@transition`, but the handler — and any conditions/permissions —
    may be `async def`. Invoke via `await instance.<name>.aset(...)`; only
    works inside a running asyncio event loop. Sync callables remain valid.

    See `transition` for the `custom=` metadata bag and the generic
    typing model.
    """
    return cast(
        "_AsyncTransitionDecorator",
        _make_transition(True, source, target, conditions, permissions, custom),
    )
