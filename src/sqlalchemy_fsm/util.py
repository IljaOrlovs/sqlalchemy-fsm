"""State-name predicates."""

from typing import Any


def is_valid_fsm_state(value: Any) -> bool:
    """A target/state name: any non-empty string."""
    return bool(isinstance(value, str) and value)


def is_valid_source_state(value: Any) -> bool:
    """A transition source: a state name, `"*"` (any), or `None` (NULL column)."""
    return (value == "*") or (value is None) or is_valid_fsm_state(value)
