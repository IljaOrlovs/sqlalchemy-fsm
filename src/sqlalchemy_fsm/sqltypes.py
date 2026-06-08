"""The SA column type that marks a column as FSM-managed."""

from __future__ import annotations

from typing import ClassVar

from sqlalchemy import types


class FSMField(types.String):
    # Subscripted subclasses (`FSMField["a","b"]`) are pure tag classes —
    # the SA cache key from `String` applies unchanged. Without this,
    # SA emits a SAWarning for every dynamic subclass.
    cache_ok = True

    """A `String` column flagged so `@transition` can discover it.

    Subscript syntax declares the closed set of legal states. When present,
    every mapped class using the typed form is validated at SA mapper
    configuration time (see `sqlalchemy_fsm.validation`):

        state = sa.Column(
            FSMField["draft", "published", "archived"],
            nullable=False,
            default="draft",
        )

    The plain `FSMField` form (no subscript) remains supported and skips
    the startup check.
    """

    _allowed_states: frozenset[str] | None = None

    # Cache of (states-tuple → subclass) so `FSMField["a","b"]` returns the
    # same class on repeated lookups — important for `isinstance` checks.
    _subscript_cache: ClassVar[dict[tuple[str, ...], type[FSMField]]] = {}

    def __class_getitem__(cls, item: object) -> type[FSMField]:
        if isinstance(item, str):
            states = (item,)
        elif isinstance(item, tuple):
            states = item  # type: ignore[assignment]
        else:
            raise TypeError(f"FSMField[...] expects strings; got {type(item).__name__}")

        bad = [s for s in states if not isinstance(s, str)]
        if bad:
            raise TypeError(f"FSMField[...] expects strings; got {bad!r}")
        if not states:
            raise TypeError("FSMField[...] requires at least one state")

        key = tuple(sorted(set(states)))
        cached = cls._subscript_cache.get(key)
        if cached is not None:
            return cached

        new_cls = type(
            f"FSMField[{', '.join(repr(s) for s in key)}]",
            (cls,),
            {"_allowed_states": frozenset(key)},
        )
        cls._subscript_cache[key] = new_cls
        return new_cls
