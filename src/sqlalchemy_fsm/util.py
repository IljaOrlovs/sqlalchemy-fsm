"""Utility functions and consts."""


def is_valid_fsm_state(value):
    return isinstance(value, str) and value


def is_valid_source_state(value):
    """This function makes exeptions for special source states.

    E.g. It explicitly allows '*' (for any state)
        and `None` (as this is default  value for sqlalchemy colums)
    """
    return (value == "*") or (value is None) or is_valid_fsm_state(value)
