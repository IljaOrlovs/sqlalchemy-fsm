"""State-name predicates and subscript helpers."""

from collections.abc import Mapping
from typing import Any, TypeVar

_C = TypeVar("_C", bound=type)


def is_valid_fsm_state(value: Any) -> bool:
    """A target/state name: any non-empty string."""
    return bool(isinstance(value, str) and value)


def is_valid_source_state(value: Any) -> bool:
    """A transition source: a state name, `"*"` (any), or `None` (NULL column).

    The `"*"` comparison is gated on `isinstance(value, str)` so a foreign
    object whose `__eq__` happens to return truthy against `"*"` can't sneak
    through validation.
    """
    if value is None:
        return True
    if isinstance(value, str) and value == "*":
        return True
    return is_valid_fsm_state(value)


def normalize_subscript_states(cls_name: str, item: object) -> tuple[str, ...]:
    """Validate a `Cls[...]` subscript argument and return the canonical
    sorted-unique tuple of state names.

    Shared by `FSMField.__class_getitem__` and `FSMColumn.__class_getitem__`
    so the two stay in sync. Raises `TypeError` with a message that names
    `cls_name` so the user sees which class rejected the argument.
    """
    if isinstance(item, str):
        states: tuple[str, ...] = (item,)
    elif isinstance(item, tuple):
        states = item  # type: ignore[assignment]
    else:
        raise TypeError(f"{cls_name}[...] expects strings; got {type(item).__name__}")

    bad = [s for s in states if not isinstance(s, str)]
    if bad:
        raise TypeError(f"{cls_name}[...] expects strings; got {bad!r}")
    if not states:
        raise TypeError(f"{cls_name}[...] requires at least one state")

    return tuple(sorted(set(states)))


def get_or_build_subscript_subclass(
    parent: _C,
    label: str,
    item: object,
    cache: dict[tuple[str, ...], _C],
    extra_attrs: Mapping[str, Any] | None = None,
) -> _C:
    """Memoised `Parent[...]` factory shared by `FSMField` and `FSMColumn`.

    Returns the subclass cached under the normalised state-tuple key,
    building it on first lookup. `extra_attrs` is merged into the
    synthesized class body so each parent can inject what only applies
    to it (e.g. SA's `inherit_cache` for column types).
    """
    key = normalize_subscript_states(label, item)
    cached = cache.get(key)
    if cached is not None:
        return cached
    attrs: dict[str, Any] = {"_allowed_states": frozenset(key)}
    if extra_attrs:
        attrs.update(extra_attrs)
    new_cls = type(
        f"{label}[{', '.join(repr(s) for s in key)}]",
        (parent,),
        attrs,
    )
    cache[key] = new_cls  # type: ignore[assignment]
    return new_cls  # type: ignore[return-value]
