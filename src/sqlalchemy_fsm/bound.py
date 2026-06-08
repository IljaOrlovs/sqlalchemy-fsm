"""
Non-meta objects that are bound to a particular table & sqlalchemy instance.
"""

import inspect as py_inspect
import warnings
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import inspect as sqla_inspect

from . import cache, events, exc, meta
from .sqltypes import FSMField


@cache.weak_value_cache
def column_cache(table_class):
    fsm_fields = [
        col for col in sqla_inspect(table_class).columns if isinstance(col.type, FSMField)
    ]

    if len(fsm_fields) == 0:
        raise exc.SetupError("No FSMField found in model")
    elif len(fsm_fields) > 1:
        raise exc.SetupError(f"More than one FSMField found in model ({fsm_fields})")
    return fsm_fields[0]


@dataclass(slots=True)
class SqlAlchemyHandle:
    table_class: type
    record: Any = None
    fsm_column: Any = field(init=False)
    column_name: str = field(init=False)
    dispatch: Any = field(init=False, default=None)

    def __post_init__(self):
        self.fsm_column = column_cache.get_value(self.table_class)
        self.column_name = self.fsm_column.name
        if self.record:
            self.dispatch = events.BoundFSMDispatcher(self.record)


class BoundFSMBase:
    __slots__ = ("extra_call_args", "meta", "sqla_handle")

    def __init__(self, meta, sqla_handle, extra_call_args):
        self.meta = meta
        self.sqla_handle = sqla_handle
        self.extra_call_args = extra_call_args

    @property
    def target_state(self):
        return self.meta.target

    @property
    def current_state(self):
        return getattr(self.sqla_handle.record, self.sqla_handle.column_name)

    def transition_possible(self):
        return ("*" in self.meta.sources) or (self.current_state in self.meta.sources)


class BoundFSMFunction(BoundFSMBase):
    __slots__ = (*BoundFSMBase.__slots__, "set_func", "my_args")

    def __init__(self, meta, sqla_handle, set_func, extra_call_args):
        super().__init__(meta, sqla_handle, extra_call_args)
        self.set_func = set_func
        self.my_args = (
            self.meta.extra_call_args + self.extra_call_args + (self.sqla_handle.record,)
        )

    def get_call_iface_error(self, fn, args, kwargs):
        """Returns 'Type' error describing function's api mismatch (if one exists)

        or None
        """
        try:
            py_inspect.getcallargs(fn, *args, **kwargs)
        except TypeError as err:
            return err
        return None

    def conditions_met(self, args, kwargs):
        conditions = self.meta.conditions
        if not conditions:
            # Performance - skip the check
            return True

        args = self.my_args + tuple(args)

        kwargs = dict(kwargs)

        out = True
        for condition in conditions:
            # Check that condition is call-able with args provided
            if self.get_call_iface_error(condition, args, kwargs):
                out = False
            else:
                out = condition(*args, **kwargs)

            if not out:
                # Preconditions failed
                break

        if out:
            # Check that the function itself can be called with these args
            err = self.get_call_iface_error(self.set_func, args, kwargs)
            if err:
                warnings.warn(
                    f"Failure to validate handler call args: {err}",
                    stacklevel=2,
                )
                # Can not map call args to handler's
                out = False
                if conditions:
                    raise exc.SetupError(
                        "Mismatch between args accepted by preconditions "
                        f"({self.meta.conditions!r}) & handler ({self.set_func!r})"
                    )
        return out

    def to_next_state(self, args, kwargs):
        old_state = self.current_state
        new_state = self.target_state

        sqla_target = self.sqla_handle.record

        args = self.my_args + tuple(args)

        self.sqla_handle.dispatch.before_state_change(source=old_state, target=new_state)

        self.set_func(*args, **kwargs)
        setattr(sqla_target, self.sqla_handle.column_name, new_state)
        self.sqla_handle.dispatch.after_state_change(source=old_state, target=new_state)

    def __repr__(self):
        return (
            f"<{self.__class__.__name__} meta={self.meta!r} "
            f"instance={self.sqla_handle!r} function={self.set_func!r}>"
        )


@dataclass(slots=True)
class TransitionStateArithmetics:
    """Helper class aiding in merging transition state params."""

    meta_a: "meta.FSMMeta"
    meta_b: "meta.FSMMeta"

    def source_intersection(self):
        """Returns intersected sources meta sources."""
        sources_a = self.meta_a.sources
        sources_b = self.meta_b.sources

        if "*" in sources_a:
            return sources_b
        elif "*" in sources_b:
            return sources_a
        elif sources_a.issuperset(sources_b):
            return sources_a.intersection(sources_b)
        else:
            return False

    def target_intersection(self):
        target_a = self.meta_a.target
        target_b = self.meta_b.target
        if target_a == target_b:
            # Also covers the case when both are None
            out = target_a
        elif None in (target_a, target_b):
            # Return value that is not None
            out = target_a or target_b
        else:
            # Both are non-equal strings
            out = None
        return out

    def joint_conditions(self):
        """Returns union of both conditions."""
        return self.meta_a.conditions + self.meta_b.conditions

    def joint_args(self):
        return self.meta_a.extra_call_args + self.meta_b.extra_call_args


@cache.dict_cache
def inherited_bound_classes(key):

    (child_cls, parent_meta) = key

    def _get_sub_transitions(child_cls):
        sub_handlers = []
        for name in dir(child_cls):
            try:
                attr = getattr(child_cls, name)
                if attr._sa_fsm_meta:
                    sub_handlers.append((name, attr))
            except AttributeError:
                # Skip non-fsm methods
                continue
        return sub_handlers

    def _get_bound_sub_metas(child_cls, sub_transitions, parent_meta):
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

    def __init__(self, meta, sqlalchemy_handle, child_cls, extra_call_args):
        super().__init__(meta, sqlalchemy_handle, extra_call_args)
        child_cls = inherited_bound_classes.get_value((child_cls, meta))
        child_object = child_cls()
        child_object._sa_fsm_sqlalchemy_handle = sqlalchemy_handle
        self.bound_sub_metas = [
            meta.get_bound(sqlalchemy_handle, set_fn, (child_object,))
            for (meta, set_fn) in child_object._sa_fsm_sqlalchemy_metas
        ]
        self._target_cached = None

    @property
    def target_state(self):
        if self._target_cached is None:
            targets = tuple({meta.meta.target for meta in self.bound_sub_metas})
            if len(targets) != 1:
                raise exc.SetupError(
                    f"Expected exactly one target across sub-transitions, got {targets!r}"
                )
            self._target_cached = targets[0]
        return self._target_cached

    def transition_possible(self):
        return any(sub.transition_possible() for sub in self.bound_sub_metas)

    def conditions_met(self, args, kwargs):
        return any(
            sub.transition_possible() and sub.conditions_met(args, kwargs)
            for sub in self.bound_sub_metas
        )

    def to_next_state(self, args, kwargs):
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
