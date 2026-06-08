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


def _fsm_columns(model_cls: type) -> list[Column]:
    """Every FSMField-typed column on the model, in declaration order."""
    return list(bound.fsm_columns_cache.get_value(model_cls))


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
    # Callable default. Two SA-side shapes we handle:
    #
    # 1. `default=lambda: "draft"` — SA wraps it as `lambda ctx: fn()` so the
    #    actual `.arg` requires one positional argument. We probe with
    #    `follow_wrapped=False` to see the wrapper, then call with `None`.
    # 2. `default=lambda ctx=None: "draft"` — already accepts a ctx itself.
    #
    # Any callable that raises, returns non-str, or refuses our probe is
    # treated as unresolvable (returns `None`).
    if callable(arg):
        try:
            sig = py_inspect.signature(arg, follow_wrapped=False)
        except (TypeError, ValueError):
            return None
        required = [
            p
            for p in sig.parameters.values()
            if p.kind
            in (
                py_inspect.Parameter.POSITIONAL_ONLY,
                py_inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
            and p.default is py_inspect.Parameter.empty
        ]
        # SA only ever wraps 0- or 1-arg callables (it raises on more), so
        # `required` is always [] or [<ctx>] here.
        try:
            result = arg(None) if required else arg()
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
    """Validate the transition graph against every typed `FSMField` column.

    Each FSM column is validated independently (correctness, completeness,
    reachability). Untyped `FSMField` columns are skipped. Raises
    `SetupError` on any violation.
    """
    columns = _fsm_columns(model_cls)
    for column in columns:
        _validate_fsm_column(model_cls, column, multi=len(columns) > 1)


def _validate_fsm_column(model_cls: type, column: Column, multi: bool) -> None:
    allowed = _declared_states(column)
    if not allowed:
        return

    where = f"{model_cls.__name__}.{column.name}" if multi else model_cls.__name__

    # 0. The column's default= must be present and a declared state. We
    # check this first so the error is specific instead of getting masked
    # by the unknown-state rule below.
    start = _initial_state(column)
    if start is None:
        raise exc.SetupError(
            f"{where}: typed FSMField columns must declare a "
            f"scalar `default=<state>` so reachability can be validated."
        )
    if start not in allowed:
        raise exc.SetupError(
            f"{where}: column default={start!r} is not in the "
            f"declared FSMField allowed set {sorted(allowed)!r}."
        )

    # Per-column transition graph. In single-column models we keep the
    # legacy behavior of treating every `@transition` as belonging to the
    # column; in multi-column models we filter strictly by `column_ref`.
    filter_col = column if multi else None
    used = collect_transition_states(model_cls, column=filter_col) | {start}
    edges = collect_edges(model_cls, column=filter_col)

    # 1. Correct: every used state is allowed.
    unknown = used - allowed
    if unknown:
        raise exc.SetupError(
            f"{where}: transition references states "
            f"{sorted(unknown)!r} not in the declared FSMField allowed set "
            f"{sorted(allowed)!r}."
        )

    # 2. Complete: every allowed state is used.
    unused = allowed - used
    if unused:
        raise exc.SetupError(
            f"{where}: declared FSMField states "
            f"{sorted(unused)!r} are never referenced by any @transition."
        )

    # 3. Reachable from the column's default=.
    reachable = _reachable_from(start, edges, allowed)
    unreachable = allowed - reachable
    if unreachable:
        raise exc.SetupError(
            f"{where}: states {sorted(unreachable)!r} are "
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
