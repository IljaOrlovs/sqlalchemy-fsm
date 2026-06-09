"""State-name predicates and subscript helpers."""

from typing import Any


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
        raise TypeError(
            f"{cls_name}[...] expects strings; got {type(item).__name__}"
        )

    bad = [s for s in states if not isinstance(s, str)]
    if bad:
        raise TypeError(f"{cls_name}[...] expects strings; got {bad!r}")
    if not states:
        raise TypeError(f"{cls_name}[...] requires at least one state")

    return tuple(sorted(set(states)))
