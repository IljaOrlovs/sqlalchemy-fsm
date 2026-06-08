"""Utility functions and consts."""

from typing import Any


def is_valid_fsm_state(value: Any) -> bool:
    return bool(isinstance(value, str) and value)


def is_valid_source_state(value: Any) -> bool:
    """This function makes exceptions for special source states.

    E.g. It explicitly allows '*' (for any state)
        and `None` (as this is the default value for sqlalchemy columns).
    """
    return (value == "*") or (value is None) or is_valid_fsm_state(value)
