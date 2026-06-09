"""Read-only introspection helpers over `@transition`-decorated classes.

Used by the validator (`sqlalchemy_fsm.validation`) and the optional
extras (`sqlalchemy_fsm.extras.graph` / `.alembic`). Keep this module
dependency-free so anything in the package can import it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from . import bound as _bound
from .transition import FsmTransition

if TYPE_CHECKING:
    from .meta import FSMMeta


@dataclass(frozen=True)
class TransitionEdge:
    """One outgoing edge of the FSM: a source state (or `"*"` / `None`)
    and the target it leads to. `label` is the method name."""

    source: str | None
    target: str
    label: str

    @property
    def display_source(self) -> str:
        """Render-friendly source label: wildcard `"*"` becomes `"(any)"`,
        `None` becomes `"(none)"`. Used by the graph renderers."""
        if self.source == "*":
            return "(any)"
        if self.source is None:
            return "(none)"
        return self.source


def iter_transitions(
    model_cls: type, column: Any = None
) -> list[tuple[str, FsmTransition]]:
    """Yield (attribute name, descriptor) for every `@transition` on `cls`.

    Walks the MRO without triggering descriptor `__get__`, so we see the
    `FsmTransition` itself rather than a bound wrapper. `dir()` already
    deduplicates names, and the inner loop stops at the first MRO hit,
    so overrides win and each name appears at most once.

    If `column` is given, only return transitions bound to that column
    (`column_ref is column`). Transitions decorated with the bare
    `@transition(...)` have no `column_ref` and are returned for every
    `column` query: that's correct on single-column models (the only place
    the bare form is valid), and `validate_fsm` rejects bare transitions
    on multi-column models up front, so the over-attribution never reaches
    a user-visible report.
    """
    out: list[tuple[str, FsmTransition]] = []
    for name in dir(model_cls):
        for klass in model_cls.__mro__:
            if name in klass.__dict__:
                attr = klass.__dict__[name]
                if isinstance(attr, FsmTransition) and (
                    column is None or attr.column_ref is None or attr.column_ref is column
                ):
                    out.append((name, attr))
                break
    return out


def _is_class_group(meta: FSMMeta, set_fn: object) -> bool:
    """True if this transition wraps a class of sub-handlers (sync or async)."""
    return issubclass(meta.bound_cls, _bound.BoundFSMClass) and isinstance(set_fn, type)


def _concrete_states_from_meta(meta: FSMMeta) -> set[str]:
    out: set[str] = set()
    if meta.target is not None:
        out.add(meta.target)
    for src in meta.sources:
        if src is None or src == "*":
            continue
        out.add(src)
    return out


def collect_transition_states(model_cls: type, column: Any = None) -> set[str]:
    """All concrete (non-wildcard, non-None) states referenced by any
    transition on `model_cls`, including class-grouped sub-handlers.

    If `column` is given, only consider transitions bound to it.
    """
    states: set[str] = set()
    for _, fsm_t in iter_transitions(model_cls, column=column):
        meta = fsm_t.meta
        states |= _concrete_states_from_meta(meta)
        if _is_class_group(meta, fsm_t.set_fn):
            # `_is_class_group` already confirmed `set_fn` is a class.
            for _, sub in iter_transitions(cast("type", fsm_t.set_fn)):
                states |= _concrete_states_from_meta(sub.meta)
    return states


def collect_edges(model_cls: type, column: Any = None) -> list[TransitionEdge]:
    """Every (source, target, method-name) edge declared by `model_cls`.

    Class-grouped transitions are flattened the same way the runtime
    dispatches: each sub-handler contributes edges merged with its
    parent meta's target.

    If `column` is given, only return edges from transitions bound to it.
    """
    edges: list[TransitionEdge] = []
    for name, fsm_t in iter_transitions(model_cls, column=column):
        meta = fsm_t.meta
        if _is_class_group(meta, fsm_t.set_fn):
            edges.extend(_edges_from_class_group(name, meta, cast("type", fsm_t.set_fn)))
        else:
            edges.extend(_edges_from_meta(name, meta))
    edges.sort(key=lambda e: (e.label, e.source or "", e.target))
    return edges


def _edges_from_meta(label: str, meta: FSMMeta) -> list[TransitionEdge]:
    if meta.target is None:
        return []
    return [TransitionEdge(s, meta.target, label) for s in meta.sources]


def _edges_from_class_group(
    label: str, parent_meta: FSMMeta, wrapped_cls: type
) -> list[TransitionEdge]:
    out: list[TransitionEdge] = []
    for _, sub_attr in iter_transitions(wrapped_cls):
        sub_meta = sub_attr.meta
        arithmetic = _bound.TransitionStateArithmetics(parent_meta, sub_meta)
        sources = arithmetic.source_intersection()
        target = arithmetic.target_intersection()
        if not sources or not target:
            continue
        out.extend(TransitionEdge(s, target, label) for s in sources)
    return out
