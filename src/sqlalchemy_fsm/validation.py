"""Validate that a model's FSM transition graph matches the states the
column promises (via `FSMField["a", "b", ...]`).

The validator runs automatically at SA `mapper_configured` time for any
mapped class whose FSM column declares allowed states. It checks three
properties:

1. **Correct** — every state used by a transition is in `allowed_states`.
2. **Complete** — every state in `allowed_states` is used by some
   transition (as source, target, or sub-handler source/target).
3. **Reachable** — starting from the column's `default=`, every state in
   `allowed_states` is reachable along forward edges.

`"*"` wildcard sources count as "applicable from every allowed state"
for reachability — they edge from every allowed state to their target.
"""

from __future__ import annotations

import inspect as py_inspect
from typing import TYPE_CHECKING

import sqlalchemy.orm
from sqlalchemy import event

from . import bound, exc
from .introspection import collect_edges, collect_transition_states
from .sqltypes import FSMField

if TYPE_CHECKING:
    from sqlalchemy import Column


def _fsm_column(model_cls: type) -> Column | None:
    """The model's FSM-managed column, or `None` if it has none.

    Re-raises `MultipleFSMColumnsError` — that's a misconfiguration that
    should fail at mapper-config time, not be silently skipped.
    """
    try:
        return bound.column_cache.get_value(model_cls)
    except exc.NoFSMColumnError:
        return None


def _declared_states(column: Column) -> frozenset[str] | None:
    """The `_allowed_states` from a typed `FSMField["a","b"]`, or None."""
    states = getattr(column.type, "_allowed_states", None)
    return states if states else None


def _initial_state(column: Column) -> str | None:
    """The column's `default=` if it can be statically determined.

    Handles three common forms:
    - scalar string default: `default="draft"`
    - callable default that takes no args and returns a string: `default=lambda: "draft"`
    - enum value with a `.value` of type `str`

    Returns `None` if we can't pin down the default; the validator then
    surfaces a clear error instead of silently using the wrong start state.
    """
    default = column.default
    if default is None:
        return None
    arg = getattr(default, "arg", None)
    if isinstance(arg, str):
        return arg
    # Enum-like default (e.g. `default=Status.DRAFT`)
    enum_value = getattr(arg, "value", None)
    if isinstance(enum_value, str):
        return enum_value
    # Zero-arg callable default (`default=lambda: "draft"`). Only call if it
    # takes no parameters — SA passes a context to callables that accept one,
    # and we don't have one here.
    if callable(arg):
        try:
            sig = py_inspect.signature(arg)
        except (TypeError, ValueError):
            return None
        if not any(
            p.kind
            in (py_inspect.Parameter.POSITIONAL_ONLY, py_inspect.Parameter.POSITIONAL_OR_KEYWORD)
            and p.default is py_inspect.Parameter.empty
            for p in sig.parameters.values()
        ):
            try:
                result = arg()
            except Exception:
                return None
            return result if isinstance(result, str) else None
    return None


def _reachable_from(start: str, edges, allowed: frozenset[str]) -> set[str]:
    """BFS along forward edges from `start`. Wildcard sources (`"*"`) act
    as edges leaving every allowed state."""
    by_source: dict[str | None, list[str]] = {}
    wildcard_targets: list[str] = []
    for edge in edges:
        if edge.source == "*":
            wildcard_targets.append(edge.target)
        else:
            by_source.setdefault(edge.source, []).append(edge.target)

    reached: set[str] = {start}
    frontier: list[str] = [start]
    while frontier:
        node = frontier.pop()
        candidates = list(by_source.get(node, ()))
        if node in allowed:
            candidates.extend(wildcard_targets)
        for nxt in candidates:
            if nxt not in reached:
                reached.add(nxt)
                frontier.append(nxt)
    return reached


def validate_fsm(model_cls: type) -> None:
    """Validate the transition graph against the typed `FSMField` column.

    No-ops if the column is plain `FSMField` (no declared states). Raises
    `SetupError` on any violation.
    """
    column = _fsm_column(model_cls)
    if column is None:
        return
    allowed = _declared_states(column)
    if not allowed:
        return

    # 0. The column's default= must be present and a declared state. We
    # check this first so the error is specific instead of getting masked
    # by the unknown-state rule below.
    start = _initial_state(column)
    if start is None:
        raise exc.SetupError(
            f"{model_cls.__name__}: typed FSMField columns must declare a "
            f"scalar `default=<state>` so reachability can be validated."
        )
    if start not in allowed:
        raise exc.SetupError(
            f"{model_cls.__name__}: column default={start!r} is not in the "
            f"declared FSMField allowed set {sorted(allowed)!r}."
        )

    # The initial state is implicitly "used" — it's the entry point.
    used = collect_transition_states(model_cls) | {start}
    edges = collect_edges(model_cls)

    # 1. Correct: every used state is allowed.
    unknown = used - allowed
    if unknown:
        raise exc.SetupError(
            f"{model_cls.__name__}: transition references states "
            f"{sorted(unknown)!r} not in the declared FSMField allowed set "
            f"{sorted(allowed)!r}."
        )

    # 2. Complete: every allowed state is used.
    unused = allowed - used
    if unused:
        raise exc.SetupError(
            f"{model_cls.__name__}: declared FSMField states "
            f"{sorted(unused)!r} are never referenced by any @transition."
        )

    # 3. Reachable from the column's default=.
    reachable = _reachable_from(start, edges, allowed)
    unreachable = allowed - reachable
    if unreachable:
        raise exc.SetupError(
            f"{model_cls.__name__}: states {sorted(unreachable)!r} are "
            f"unreachable from initial state {start!r}."
        )


def _has_typed_fsm_field(model_cls: type) -> bool:
    """Cheap pre-check before calling the full validator."""
    try:
        for col in sqlalchemy.orm.class_mapper(model_cls).columns:
            if isinstance(col.type, FSMField) and getattr(
                col.type, "_allowed_states", None
            ):
                return True
    except Exception:
        return False
    return False


_LISTENER_REGISTERED = False


def _register_mapper_listener() -> None:
    """Install a once-per-process SA listener that validates FSMs at
    mapper configuration time. Called from package `__init__`."""
    global _LISTENER_REGISTERED
    if _LISTENER_REGISTERED:
        return
    _LISTENER_REGISTERED = True

    @event.listens_for(sqlalchemy.orm.Mapper, "mapper_configured")
    def _on_mapper_configured(mapper, cls) -> None:
        if _has_typed_fsm_field(cls):
            validate_fsm(cls)
