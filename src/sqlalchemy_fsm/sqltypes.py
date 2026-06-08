"""The SA column type that marks a column as FSM-managed."""

from sqlalchemy import types


class FSMField(types.String):
    """A `String` column flagged so `@transition` can discover it."""
