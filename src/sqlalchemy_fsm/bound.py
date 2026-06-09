"""Instance-bound FSM machinery: handles, conditions, and transition execution."""

import inspect as py_inspect
import warnings
import weakref
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import inspect as sqla_inspect

from . import cache, events, exc, meta
from .sqltypes import FSMField


@cache.dict_cache
def fsm_columns_cache(table_class: type) -> tuple[Any, ...]:
    """All FSMField-typed columns on `table_class`, in column declaration order."""
    return tuple(
        col
        for col in sqla_inspect(table_class).columns
        if isinstance(col.type, FSMField)
    )


def single_fsm_column(table_class: type) -> Any:
    """Return the model's sole FSMField column. Raises if 0 or >1.

    Used by the legacy bare-`@transition(...)` path; multi-column models
    must use `FSMColumn.transition(...)` so the binding is explicit.
    """
    cols = fsm_columns_cache.get_value(table_class)
    if len(cols) == 0:
        raise exc.NoFSMColumnError("No FSMField found in model")
    if len(cols) > 1:
        raise exc.MultipleFSMColumnsError(
            f"{table_class.__name__} has {len(cols)} FSMField columns "
            f"({[c.name for c in cols]!r}); use FSMColumn.transition(...) "
            f"to bind each @transition to a specific column."
        )
    return cols[0]


def resolve_handle(
    table_class: type, record: Any, column_ref: Any | None
) -> "SqlAlchemyHandle":
    """Build the handle for a transition.

    `column_ref` is the `FSMColumn` instance the transition was declared
    on. When `None` (legacy module-level `@transition`), fall back to the
    single-column rule.
    """
    if column_ref is None:
        column_ref = single_fsm_column(table_class)
    return SqlAlchemyHandle(table_class, column_ref, record)


# Back-compat shim. External code (and tests) sometimes reach for
# `column_cache` directly — keep the import working but route it through
# the new single-column resolver.
class _ColumnCacheShim:
    def get_value(self, table_class: type) -> Any:
        return single_fsm_column(table_class)


column_cache = _ColumnCacheShim()


@dataclass(slots=True)
class SqlAlchemyHandle:
    table_class: type
    fsm_column: Any
    record: Any = None
    column_name: str = field(init=False)
    dispatch: Any = field(init=False, default=None)

    def __post_init__(self) -> None:
        self.column_name = self.fsm_column.name
        if self.record:
            self.dispatch = events.BoundFSMDispatcher(self.record)


# --- callable-signature memoization ----------------------------------------
#
# `inspect.getcallargs` was previously called on the hot path for every
# condition/permission/handler check. Building the Signature dominates the
# benchmark. We cache the Signature per-callable and use `.bind()` instead.

# WeakKeyDictionary so transient lambdas (common as inline conditions) don't
# pin themselves alive for the process lifetime. Built-ins and other objects
# that can't be weakref'd fall back to a small bounded LRU.
_SigCache = "weakref.WeakKeyDictionary[Callable[..., Any], py_inspect.Signature | None]"
_SIGNATURE_CACHE: _SigCache = weakref.WeakKeyDictionary()  # type: ignore[assignment]
# Bounded LRU keyed by `id(fn)` for callables that can't be weakref'd
# (e.g. built-ins). Wholesale clear() would lose entries belonging to other
# callsites; LRU eviction keeps the hot set warm.
_SIGNATURE_FALLBACK: OrderedDict[int, py_inspect.Signature | None] = OrderedDict()
_SIGNATURE_FALLBACK_MAX = 256


def _fallback_get(key: int) -> Any:
    try:
        sig = _SIGNATURE_FALLBACK[key]
    except KeyError:
        return _MISSING
    _SIGNATURE_FALLBACK.move_to_end(key)
    return sig


def _fallback_put(key: int, sig: py_inspect.Signature | None) -> None:
    _SIGNATURE_FALLBACK[key] = sig
    _SIGNATURE_FALLBACK.move_to_end(key)
    while len(_SIGNATURE_FALLBACK) > _SIGNATURE_FALLBACK_MAX:
        _SIGNATURE_FALLBACK.popitem(last=False)


def _signature_for(fn: Callable[..., Any]) -> py_inspect.Signature | None:
    """Cached `inspect.signature(fn)`. `None` means signature is unknowable
    (e.g. built-in with no introspectable params) — we skip the bind check
    in that case and let the call itself raise."""
    try:
        return _SIGNATURE_CACHE[fn]
    except KeyError:
        pass
    except TypeError:
        # Object isn't weakref-able (e.g. some built-ins) — fall through to
        # the id-keyed fallback cache below.
        key = id(fn)
        cached = _fallback_get(key)
        if cached is not _MISSING:
            return cached
        sig = _compute_signature(fn)
        _fallback_put(key, sig)
        return sig

    sig = _compute_signature(fn)
    try:
        _SIGNATURE_CACHE[fn] = sig
    except TypeError:
        # Race: fn is weakref-able on getitem path but not setitem. Cache
        # in the fallback instead.
        _fallback_put(id(fn), sig)
    return sig


_MISSING: Any = object()


def _compute_signature(fn: Callable[..., Any]) -> py_inspect.Signature | None:
    try:
        return py_inspect.signature(fn)
    except (ValueError, TypeError):
        return None


def _call_iface_error(
    fn: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
) -> TypeError | None:
    """`None` if `fn(*args, **kwargs)` would bind cleanly; else the `TypeError`."""
    sig = _signature_for(fn)
    if sig is None:
        return None
    try:
        sig.bind(*args, **kwargs)
    except TypeError as err:
        return err
    return None


class BoundFSMBase:
    __slots__ = ("extra_call_args", "meta", "sqla_handle")

    def __init__(
        self,
        meta: "meta.FSMMeta",
        sqla_handle: SqlAlchemyHandle,
        extra_call_args: tuple[Any, ...],
    ) -> None:
        self.meta = meta
        self.sqla_handle = sqla_handle
        self.extra_call_args = extra_call_args

    @property
    def target_state(self) -> str | None:
        return self.meta.target

    @property
    def current_state(self) -> str | None:
        return getattr(self.sqla_handle.record, self.sqla_handle.column_name)

    def transition_possible(self) -> bool:
        return ("*" in self.meta.sources) or (self.current_state in self.meta.sources)

    def conditions_met(self, args: Iterable[Any], kwargs: Mapping[str, Any]) -> bool:
        raise NotImplementedError

    def permissions_met(self, args: Iterable[Any], kwargs: Mapping[str, Any]) -> bool:
        raise NotImplementedError

    def to_next_state(self, args: Iterable[Any], kwargs: Mapping[str, Any]) -> None:
        raise NotImplementedError

    def would_succeed(self, args: Iterable[Any], kwargs: Mapping[str, Any]) -> bool:
        """True iff `to_next_state(args, kwargs)` would not raise.

        Subclasses override when the equivalence isn't a plain conjunction
        of `transition_possible`/`permissions_met`/`conditions_met` —
        notably `BoundFSMClass`, where dispatch requires *exactly one*
        applicable sub-handler.
        """
        return (
            self.transition_possible()
            and self.permissions_met(args, kwargs)
            and self.conditions_met(args, kwargs)
        )


class BoundFSMFunction(BoundFSMBase):
    __slots__ = (*BoundFSMBase.__slots__, "set_func", "my_args")

    def __init__(
        self,
        meta: "meta.FSMMeta",
        sqla_handle: SqlAlchemyHandle,
        set_func: Callable[..., Any],
        extra_call_args: tuple[Any, ...],
    ) -> None:
        super().__init__(meta, sqla_handle, extra_call_args)
        self.set_func = set_func
        self.my_args = (
            self.meta.extra_call_args + self.extra_call_args + (self.sqla_handle.record,)
        )

    # Back-compat for callers (e.g. subclasses or tests) that referenced the
    # old method name. New code should use the module-level `_call_iface_error`.
    def get_call_iface_error(
        self,
        fn: Callable[..., Any],
        args: Iterable[Any],
        kwargs: Mapping[str, Any],
    ) -> TypeError | None:
        return _call_iface_error(fn, tuple(args), kwargs)

    def _merged_args(self, args: Iterable[Any]) -> tuple[Any, ...]:
        return self.my_args if not args else self.my_args + tuple(args)

    def _eval_callables(
        self,
        callables: tuple[Callable[..., Any], ...],
        args: tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> bool:
        """Run each callable with the merged args; short-circuit on first falsy.

        A callable that can't be bound with these args raises a warning and
        causes the check to return False — same outcome the callable would
        get if invoked with mismatched args, but with a helpful warning.
        """
        for fn in callables:
            err = _call_iface_error(fn, args, kwargs)
            if err is not None:
                warnings.warn(
                    f"Callable {fn!r} cannot be invoked with these args: {err}",
                    stacklevel=2,
                )
                return False
            if not fn(*args, **kwargs):
                return False
        return True

    def _validate_handler_iface(
        self, merged_args: tuple[Any, ...], merged_kwargs: Mapping[str, Any]
    ) -> None:
        """Raise SetupError if conditions accept these args but the handler
        wouldn't — otherwise `set()` would pass conditions and then crash
        inside the handler."""
        err = _call_iface_error(self.set_func, merged_args, merged_kwargs)
        if err is None:
            return
        raise exc.SetupError(
            "Mismatch between args accepted by preconditions "
            f"({self.meta.conditions!r}) & handler ({self.set_func!r}): {err}"
        )

    def conditions_met(self, args: Iterable[Any], kwargs: Mapping[str, Any]) -> bool:
        conditions = self.meta.conditions
        if not conditions:
            return True
        return self._eval_callables(conditions, self._merged_args(args), kwargs)

    def permissions_met(self, args: Iterable[Any], kwargs: Mapping[str, Any]) -> bool:
        permissions = self.meta.permissions
        if not permissions:
            return True
        return self._eval_callables(permissions, self._merged_args(args), kwargs)

    def to_next_state(self, args: Iterable[Any], kwargs: Mapping[str, Any]) -> None:
        old_state = self.current_state
        new_state = self.target_state
        sqla_target = self.sqla_handle.record
        merged = self._merged_args(args)

        # Surface condition/handler arg-shape mismatches before we mutate.
        # Predicates already passed; if the handler can't bind the same
        # args, that's a setup bug, not a runtime "condition failed".
        if self.meta.conditions:
            self._validate_handler_iface(merged, kwargs)

        self.sqla_handle.dispatch.before_state_change(source=old_state, target=new_state)
        self.set_func(*merged, **kwargs)
        setattr(sqla_target, self.sqla_handle.column_name, new_state)
        self.sqla_handle.dispatch.after_state_change(source=old_state, target=new_state)

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} meta={self.meta!r} "
            f"instance={self.sqla_handle!r} function={self.set_func!r}>"
        )


class AsyncBoundFSMFunction(BoundFSMFunction):
    """Async-aware variant: callables may be `async def` and are awaited."""

    __slots__ = ()

    async def _aeval_callables(
        self,
        callables: tuple[Callable[..., Any], ...],
        args: tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> bool:
        for fn in callables:
            err = _call_iface_error(fn, args, kwargs)
            if err is not None:
                warnings.warn(
                    f"Callable {fn!r} cannot be invoked with these args: {err}",
                    stacklevel=2,
                )
                return False
            result = fn(*args, **kwargs)
            if py_inspect.iscoroutine(result):
                result = await result
            if not result:
                return False
        return True

    async def aconditions_met(
        self, args: Iterable[Any], kwargs: Mapping[str, Any]
    ) -> bool:
        conditions = self.meta.conditions
        if not conditions:
            return True
        return await self._aeval_callables(conditions, self._merged_args(args), kwargs)

    async def apermissions_met(
        self, args: Iterable[Any], kwargs: Mapping[str, Any]
    ) -> bool:
        permissions = self.meta.permissions
        if not permissions:
            return True
        return await self._aeval_callables(permissions, self._merged_args(args), kwargs)

    async def ato_next_state(
        self, args: Iterable[Any], kwargs: Mapping[str, Any]
    ) -> None:
        old_state = self.current_state
        new_state = self.target_state
        sqla_target = self.sqla_handle.record
        merged = self._merged_args(args)

        if self.meta.conditions:
            self._validate_handler_iface(merged, kwargs)

        self.sqla_handle.dispatch.before_state_change(source=old_state, target=new_state)
        result = self.set_func(*merged, **kwargs)
        if py_inspect.iscoroutine(result):
            await result
        setattr(sqla_target, self.sqla_handle.column_name, new_state)
        self.sqla_handle.dispatch.after_state_change(source=old_state, target=new_state)

    def transition_possible_async(self) -> bool:
        return self.transition_possible()

    async def awould_succeed(
        self, args: Iterable[Any], kwargs: Mapping[str, Any]
    ) -> bool:
        """Async sibling of `would_succeed`. Function-bound default: AND of
        the three checks. `AsyncBoundFSMClass` overrides for exactly-one
        semantics."""
        return (
            self.transition_possible()
            and await self.apermissions_met(args, kwargs)
            and await self.aconditions_met(args, kwargs)
        )


@dataclass(slots=True)
class TransitionStateArithmetics:
    """Merge a parent class-transition meta with a child handler meta.

    Used to resolve which sub-handler covers which source state and to
    detect incompatible declarations at setup time.
    """

    meta_a: "meta.FSMMeta"
    meta_b: "meta.FSMMeta"

    def source_intersection(self) -> frozenset[str | None] | None:
        """Sources reachable by both; `"*"` on either side widens to the
        other. Returns `None` if there is no overlap (i.e. the sub-handler
        declares a source the parent does not cover).

        Note the asymmetry: when neither side is wildcard we *require*
        `meta_a.sources` (parent / class-transition) to be a superset of
        `meta_b.sources` (sub-handler). A sub-handler that declares a
        source not covered by its parent is a setup error — the parent's
        source set is the contract the outer dispatcher promises, and a
        wider sub-handler would silently shadow that promise.
        """
        sources_a = self.meta_a.sources
        sources_b = self.meta_b.sources

        if "*" in sources_a:
            return sources_b
        if "*" in sources_b:
            return sources_a
        if sources_a.issuperset(sources_b):
            return sources_a.intersection(sources_b)
        return None

    def target_intersection(self) -> str | None:
        """The agreed target, or `None` if the two targets conflict.

        Sub-handlers under a class-based transition may legitimately
        declare `target=None`, inheriting the parent's target. So the
        rule is: non-None wins; equal wins; otherwise incompatible.

        Callers must distinguish "both were None" (caller bug — never
        produced by `@transition` on a sub-handler without a parent
        target) from "two distinct concrete targets" themselves — both
        cases return `None` here. The downstream callers (`_get_bound_sub_metas`,
        `_edges_from_class_group`) treat both as "incompatible", which
        is the right behavior in practice.
        """
        target_a = self.meta_a.target
        target_b = self.meta_b.target
        if target_a == target_b:
            return target_a
        if target_a is None:
            return target_b
        if target_b is None:
            return target_a
        return None  # two distinct concrete targets — incompatible

    def joint_conditions(self) -> tuple[Callable[..., Any], ...]:
        return self.meta_a.conditions + self.meta_b.conditions

    def joint_permissions(self) -> tuple[Callable[..., Any], ...]:
        return self.meta_a.permissions + self.meta_b.permissions

    def joint_args(self) -> tuple[Any, ...]:
        return self.meta_a.extra_call_args + self.meta_b.extra_call_args


@cache.dict_cache
def inherited_bound_classes(key: tuple[type, "meta.FSMMeta"]) -> type:
    (child_cls, parent_meta) = key

    def _get_sub_transitions(child_cls: type) -> list[tuple[str, Any]]:
        sub_handlers: list[tuple[str, Any]] = []
        for name in dir(child_cls):
            try:
                attr = getattr(child_cls, name)
                if attr._sa_fsm_meta:
                    sub_handlers.append((name, attr))
            except AttributeError:  # noqa: PERF203
                # Skip non-fsm methods — try/except is the most natural way
                # to filter for the `_sa_fsm_meta` attribute over a dir() walk.
                continue
        return sub_handlers

    def _get_bound_sub_metas(
        child_cls: type,
        sub_transitions: list[tuple[str, Any]],
        parent_meta: "meta.FSMMeta",
    ) -> list[tuple["meta.FSMMeta", Callable[..., Any]]]:
        out = []

        for _name, transition in sub_transitions:
            sub_meta = transition._sa_fsm_meta
            arithmetics = TransitionStateArithmetics(parent_meta, sub_meta)

            sub_sources = arithmetics.source_intersection()
            if sub_sources is None or not sub_sources:
                raise exc.SetupError(
                    f"Source state superset {parent_meta.sources} "
                    f"and subset {sub_meta.sources} are not compatible"
                )

            sub_target = arithmetics.target_intersection()
            if not sub_target:
                raise exc.SetupError(
                    f"Targets {parent_meta.target} and "
                    f"{sub_meta.target} are not compatible"
                )

            if parent_meta.is_async != sub_meta.is_async:
                raise exc.SetupError(
                    f"Cannot mix sync and async sub-handlers under a "
                    f"{'async' if parent_meta.is_async else 'sync'} class transition "
                    f"(sub={transition._sa_fsm_transition_fn!r})"
                )
            merged_sub_meta = meta.FSMMeta(
                sub_sources,
                sub_target,
                arithmetics.joint_conditions(),
                arithmetics.joint_args(),
                sub_meta.bound_cls,
                arithmetics.joint_permissions(),
                is_async=sub_meta.is_async,
            )
            out.append((merged_sub_meta, transition._sa_fsm_transition_fn))

        return out

    out_cls = type(
        f"{child_cls.__name__}::sqlalchemy_handle",
        (child_cls,),
        {
            "_sa_fsm_sqlalchemy_handle": None,
            "_sa_fsm_sqlalchemy_metas": (),
        },
    )
    sub_transitions = _get_sub_transitions(out_cls)
    out_cls._sa_fsm_sqlalchemy_metas = tuple(
        _get_bound_sub_metas(out_cls, sub_transitions, parent_meta)
    )

    return out_cls


class BoundFSMClass(BoundFSMBase):
    """Runtime binding for a class-grouped `@transition`.

    A class-based transition wraps a small dispatcher class whose methods
    are themselves `@transition`-decorated sub-handlers. We synthesize one
    instance of that dispatcher class per binding and pass it as the
    sub-handler's first argument.

    Note for users defining class-based transitions: `self` inside a
    sub-handler is **the dispatcher instance, not the mapped row**. The
    row is reachable via `self._sa_fsm_sqlalchemy_handle.record`. The
    dispatcher class is instantiated with no arguments, so its `__init__`
    must accept zero positional args (or be omitted).
    """

    __slots__ = (*BoundFSMBase.__slots__, "bound_sub_metas", "_target_cached")

    def __init__(
        self,
        meta: "meta.FSMMeta",
        sqlalchemy_handle: SqlAlchemyHandle,
        child_cls: type,
        extra_call_args: tuple[Any, ...],
    ) -> None:
        super().__init__(meta, sqlalchemy_handle, extra_call_args)
        child_cls = inherited_bound_classes.get_value((child_cls, meta))
        child_object = child_cls()
        child_object._sa_fsm_sqlalchemy_handle = sqlalchemy_handle
        self.bound_sub_metas: list[BoundFSMBase] = [
            meta.get_bound(sqlalchemy_handle, set_fn, (child_object,))
            for (meta, set_fn) in child_object._sa_fsm_sqlalchemy_metas
        ]
        self._target_cached: str | None = None

    @property
    def target_state(self) -> str | None:
        if self._target_cached is None:
            targets = tuple({meta.meta.target for meta in self.bound_sub_metas})
            if len(targets) != 1:
                raise exc.SetupError(
                    f"Expected exactly one target across sub-transitions, got {targets!r}"
                )
            self._target_cached = targets[0]
        return self._target_cached

    def _applicable_subs(self) -> list[BoundFSMBase]:
        """Sub-handlers whose source state matches the current state.

        Walked fresh on each call — `current_state` is read from the
        record at call time, so memoization here would be incorrect.
        The walk is O(#sub-handlers), which is small in practice.
        """
        return [sub for sub in self.bound_sub_metas if sub.transition_possible()]

    def transition_possible(self) -> bool:
        return any(sub.transition_possible() for sub in self.bound_sub_metas)

    def conditions_met(self, args: Iterable[Any], kwargs: Mapping[str, Any]) -> bool:
        return any(sub.conditions_met(args, kwargs) for sub in self._applicable_subs())

    def permissions_met(self, args: Iterable[Any], kwargs: Mapping[str, Any]) -> bool:
        # A class-based transition is allowed if any applicable sub-handler
        # accepts the caller — mirrors the dispatch in to_next_state().
        return any(sub.permissions_met(args, kwargs) for sub in self._applicable_subs())

    def _accepted_subs(
        self, args: Iterable[Any], kwargs: Mapping[str, Any]
    ) -> list[BoundFSMBase]:
        """Sub-handlers that pass source, permissions, AND conditions —
        the exact set `to_next_state` will pick a winner from."""
        return [
            sub
            for sub in self._applicable_subs()
            if sub.permissions_met(args, kwargs) and sub.conditions_met(args, kwargs)
        ]

    def would_succeed(self, args: Iterable[Any], kwargs: Mapping[str, Any]) -> bool:
        # Class transitions need *exactly one* accepted sub-handler. A bare
        # conjunction of permissions_met/conditions_met can over-approximate
        # (different subs satisfying each), causing `can_proceed → True` but
        # `set() → InvalidSourceStateError`.
        return len(self._accepted_subs(args, kwargs)) == 1

    def to_next_state(self, args: Iterable[Any], kwargs: Mapping[str, Any]) -> None:
        accepted = self._accepted_subs(args, kwargs)
        if len(accepted) > 1:
            raise exc.SetupError(
                f"Can transition with multiple handlers ({accepted})"
            )
        if not accepted:
            raise exc.InvalidSourceStateError(
                "No sub-transition is currently applicable."
            )
        return accepted[0].to_next_state(args, kwargs)


class AsyncBoundFSMClass(BoundFSMClass):
    """Class-transition variant where every sub-handler is async."""

    __slots__ = ()

    def _applicable_async_subs(self) -> list["AsyncBoundFSMFunction"]:
        """Same as `_applicable_subs`, but narrowed to the async type.

        By construction the parent transition is `AsyncBoundFSMClass`, so
        every sub-handler is `AsyncBoundFSMFunction`; the base class just
        can't express that statically.
        """
        from typing import cast

        return cast("list[AsyncBoundFSMFunction]", self._applicable_subs())

    async def aconditions_met(
        self, args: Iterable[Any], kwargs: Mapping[str, Any]
    ) -> bool:
        for sub in self._applicable_async_subs():
            if await sub.aconditions_met(args, kwargs):
                return True
        return False

    async def apermissions_met(
        self, args: Iterable[Any], kwargs: Mapping[str, Any]
    ) -> bool:
        for sub in self._applicable_async_subs():
            if await sub.apermissions_met(args, kwargs):
                return True
        return False

    async def _aaccepted_subs(
        self, args: Iterable[Any], kwargs: Mapping[str, Any]
    ) -> list["AsyncBoundFSMFunction"]:
        # Plain loop (not a comprehension) because each `await` is a real
        # suspension point — sequential evaluation is intentional so we
        # don't fan out coroutines we may not need (and so any side-effect
        # ordering matches the sync version).
        out: list[AsyncBoundFSMFunction] = []
        for sub in self._applicable_async_subs():
            if await sub.apermissions_met(args, kwargs) and await sub.aconditions_met(
                args, kwargs
            ):
                out.append(sub)  # noqa: PERF401
        return out

    async def awould_succeed(
        self, args: Iterable[Any], kwargs: Mapping[str, Any]
    ) -> bool:
        # See `BoundFSMClass.would_succeed` for why this isn't an AND.
        return len(await self._aaccepted_subs(args, kwargs)) == 1

    async def ato_next_state(
        self, args: Iterable[Any], kwargs: Mapping[str, Any]
    ) -> None:
        accepted = await self._aaccepted_subs(args, kwargs)
        if len(accepted) > 1:
            raise exc.SetupError(
                f"Can transition with multiple handlers ({accepted})"
            )
        if not accepted:
            raise exc.InvalidSourceStateError(
                "No sub-transition is currently applicable."
            )
        return await accepted[0].ato_next_state(args, kwargs)
