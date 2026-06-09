"""The SA column type that marks a column as FSM-managed."""

from __future__ import annotations

from typing import ClassVar

from sqlalchemy import types

from .util import normalize_subscript_states


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

    The subscripted form also derives a column `length=` equal to the
    longest declared state name, so dialects that care about ``VARCHAR``
    sizing (Postgres warns, MySQL forbids unsized varchar in some
    configs) get a sensible bound. Override by passing ``length=`` to
    the constructor explicitly: ``FSMField["a","b"](length=64)``.

    The plain `FSMField` form (no subscript) remains supported, skips
    the startup check, and uses SA's default (unbounded) ``String``.
    """

    _allowed_states: frozenset[str] | None = None

    # Cache of (states-tuple → subclass) so `FSMField["a","b"]` returns the
    # same class on repeated lookups — important for `isinstance` checks.
    _subscript_cache: ClassVar[dict[tuple[str, ...], type[FSMField]]] = {}

    def __init__(self, *args: object, **kwargs: object) -> None:
        if (
            self._allowed_states is not None
            and "length" not in kwargs
            and not args
        ):
            kwargs["length"] = max(len(s) for s in self._allowed_states)
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    def __class_getitem__(cls, item: object) -> type[FSMField]:
        key = normalize_subscript_states("FSMField", item)
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
