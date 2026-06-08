"""Instance-bound FSM machinery: handles, conditions, and transition execution."""

import inspect as py_inspect
import warnings
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import inspect as sqla_inspect

from . import cache, events, exc, meta
from .sqltypes import FSMField


@cache.weak_value_cache
def column_cache(table_class: type) -> Any:
    fsm_fields = [
        col for col in sqla_inspect(table_class).columns if isinstance(col.type, FSMField)
    ]

    if len(fsm_fields) == 0:
        raise exc.SetupError("No FSMField found in model")
    if len(fsm_fields) > 1:
        raise exc.SetupError(f"More than one FSMField found in model ({fsm_fields})")
    return fsm_fields[0]


@dataclass(slots=True)
class SqlAlchemyHandle:
    table_class: type
    record: Any = None
    fsm_column: Any = field(init=False)
    column_name: str = field(init=False)
    dispatch: Any = field(init=False, default=None)

    def __post_init__(self) -> None:
        self.fsm_column = column_cache.get_value(self.table_class)
        self.column_name = self.fsm_column.name
        if self.record:
            self.dispatch = events.BoundFSMDispatcher(self.record)


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

    def to_next_state(self, args: Iterable[Any], kwargs: Mapping[str, Any]) -> None:
        raise NotImplementedError


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

    def get_call_iface_error(
        self,
        fn: Callable[..., Any],
        args: Iterable[Any],
        kwargs: Mapping[str, Any],
    ) -> TypeError | None:
        """`None` if `fn(*args, **kwargs)` would bind cleanly; else the `TypeError`."""
        try:
            py_inspect.getcallargs(fn, *args, **kwargs)
        except TypeError as err:
            return err
        return None

    def conditions_met(self, args: Iterable[Any], kwargs: Mapping[str, Any]) -> bool:
        conditions = self.meta.conditions
        if not conditions:
            return True

        args = self.my_args + tuple(args)
        kwargs = dict(kwargs)

        out = True
        for condition in conditions:
            if self.get_call_iface_error(condition, args, kwargs):
                out = False
            else:
                out = condition(*args, **kwargs)
            if not out:
                break

        if out:
            # If conditions accept these args, the handler must too — otherwise
            # set() would pass conditions and then crash inside the handler.
            err = self.get_call_iface_error(self.set_func, args, kwargs)
            if err:
                warnings.warn(
                    f"Failure to validate handler call args: {err}",
                    stacklevel=2,
                )
                out = False
                if conditions:
                    raise exc.SetupError(
                        "Mismatch between args accepted by preconditions "
                        f"({self.meta.conditions!r}) & handler ({self.set_func!r})"
                    )
        return out

    def to_next_state(self, args: Iterable[Any], kwargs: Mapping[str, Any]) -> None:
        old_state = self.current_state
        new_state = self.target_state

        sqla_target = self.sqla_handle.record

        args = self.my_args + tuple(args)

        self.sqla_handle.dispatch.before_state_change(source=old_state, target=new_state)

        self.set_func(*args, **kwargs)
        setattr(sqla_target, self.sqla_handle.column_name, new_state)
        self.sqla_handle.dispatch.after_state_change(source=old_state, target=new_state)

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} meta={self.meta!r} "
            f"instance={self.sqla_handle!r} function={self.set_func!r}>"
        )


@dataclass(slots=True)
class TransitionStateArithmetics:
    """Merge a parent class-transition meta with a child handler meta.

    Used to resolve which sub-handler covers which source state and to
    detect incompatible declarations at setup time.
    """

    meta_a: "meta.FSMMeta"
    meta_b: "meta.FSMMeta"

    def source_intersection(self) -> frozenset[str | None] | bool:
        """Sources reachable by both; `"*"` on either side widens to the other.
        Returns `False` if there is no overlap."""
        sources_a = self.meta_a.sources
        sources_b = self.meta_b.sources

        if "*" in sources_a:
            return sources_b
        if "*" in sources_b:
            return sources_a
        if sources_a.issuperset(sources_b):
            return sources_a.intersection(sources_b)
        return False

    def target_intersection(self) -> str | None:
        """The single agreed target, or `None` if the two targets conflict."""
        target_a = self.meta_a.target
        target_b = self.meta_b.target
        if target_a == target_b:
            return target_a  # covers both-None too
        if None in (target_a, target_b):
            return target_a or target_b  # the non-None one wins
        return None  # two distinct concrete targets — incompatible

    def joint_conditions(self) -> tuple[Callable[..., Any], ...]:
        return self.meta_a.conditions + self.meta_b.conditions

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
            if not sub_sources:
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

            merged_sub_meta = meta.FSMMeta(
                sub_sources,
                sub_target,
                arithmetics.joint_conditions(),
                arithmetics.joint_args(),
                sub_meta.bound_cls,
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

    def transition_possible(self) -> bool:
        return any(sub.transition_possible() for sub in self.bound_sub_metas)

    def conditions_met(self, args: Iterable[Any], kwargs: Mapping[str, Any]) -> bool:
        return any(
            sub.transition_possible() and sub.conditions_met(args, kwargs)
            for sub in self.bound_sub_metas
        )

    def to_next_state(self, args: Iterable[Any], kwargs: Mapping[str, Any]) -> None:
        can_transition_with = [
            sub
            for sub in self.bound_sub_metas
            if sub.transition_possible() and sub.conditions_met(args, kwargs)
        ]
        if len(can_transition_with) > 1:
            raise exc.SetupError(
                f"Can transition with multiple handlers ({can_transition_with})"
            )
        if not can_transition_with:
            raise exc.InvalidSourceStateError(
                "No sub-transition is currently applicable."
            )
        return can_transition_with[0].to_next_state(args, kwargs)
