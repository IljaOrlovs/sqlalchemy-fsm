"""`FSMColumn` — a `sa.Column` subclass that doubles as a per-column
namespace for `@transition` decorators.

    state = FSMColumn["draft", "published", "archived"](
        nullable=False, default="draft",
    )

    @state.transition(source="draft", target="published")
    def publish(self): ...

Multiple `FSMColumn`s on the same model are fully supported — each
transition is scoped to the column it was declared on. Source/target
state names are validated at *decoration time* against the column's
declared allowed states, so typos crash at import rather than at
runtime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, cast, overload

import sqlalchemy as sa

from . import exc
from .sqltypes import FSMField
from .util import get_or_build_subscript_subclass

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from .transition import (
        FSMCondition,
        FsmTransition,
        SourceState,
        _AsyncTransitionDecorator,
        _SyncTransitionDecorator,
    )


class FSMColumn(sa.Column):
    """A `sa.Column` subclass parameterised by its FSM state set.

    `FSMColumn["a", "b"]` returns a cached subclass whose `_allowed_states`
    is `frozenset({"a", "b"})`. Instantiating it builds a `sa.Column` whose
    SA type is `FSMField[<states>]` — so all existing FSMField discovery
    (validation, alembic CHECKs, query filtering) keeps working.

    The plain `FSMColumn(...)` form (no subscript) builds a column whose
    type is the bare `FSMField` — no state validation, matching the
    equivalent `sa.Column(FSMField, ...)` declaration.
    """

    _allowed_states: ClassVar[frozenset[str] | None] = None
    _subscript_cache: ClassVar[dict[tuple[str, ...], type[FSMColumn]]] = {}

    inherit_cache = True

    if TYPE_CHECKING:
        # Tell static checkers that an `FSMColumn` attribute on an
        # instance behaves like a plain `str` (matching SA's runtime
        # instrumentation), while class-level access still returns the
        # `FSMColumn` itself so `@state.transition(...)` keeps type-
        # checking. Mirrors the trick SA's own stubs use for `Mapped[T]`.
        @overload
        def __get__(self, instance: None, owner: Any) -> FSMColumn: ...
        @overload
        def __get__(self, instance: object, owner: Any) -> str: ...
        def __get__(self, instance: Any, owner: Any) -> FSMColumn | str: ...
        def __set__(self, instance: object, value: str) -> None: ...

    def __class_getitem__(cls, item: object) -> type[FSMColumn]:
        return get_or_build_subscript_subclass(
            cls,
            "FSMColumn",
            item,
            cls._subscript_cache,
            extra_attrs={"inherit_cache": True},
        )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if "type_" not in kwargs and not _args_have_type(args):
            kwargs["type_"] = self._build_fsm_type()
        super().__init__(*args, **kwargs)

    def _build_fsm_type(self) -> FSMField:
        if self._allowed_states is None:
            return FSMField()
        # Defer to FSMField's own subscript machinery so the cached
        # FSMField subclass matches what a user would write by hand.
        states_tuple = tuple(sorted(self._allowed_states))
        return FSMField[states_tuple]()

    # --- decorator factory --------------------------------------------------

    def transition(
        self,
        source: SourceState = "*",
        target: str | None = None,
        conditions: Iterable[FSMCondition] = (),
        permissions: Iterable[FSMCondition] = (),
        custom: Mapping[str, Any] | None = None,
    ) -> _SyncTransitionDecorator:
        """Like the module-level `@transition`, but scoped to this column.

        Validates `source`/`target` state names against this column's
        declared allowed states at decoration time. Mixing transitions
        from different columns on the same model is fine — each transition
        only writes back to the column it was declared on.
        """
        return cast(
            "_SyncTransitionDecorator",
            self._make_decorator(False, source, target, conditions, permissions, custom),
        )

    def async_transition(
        self,
        source: SourceState = "*",
        target: str | None = None,
        conditions: Iterable[FSMCondition] = (),
        permissions: Iterable[FSMCondition] = (),
        custom: Mapping[str, Any] | None = None,
    ) -> _AsyncTransitionDecorator:
        """Async sibling of `.transition`. See `sqlalchemy_fsm.async_transition`."""
        return cast(
            "_AsyncTransitionDecorator",
            self._make_decorator(True, source, target, conditions, permissions, custom),
        )

    def _make_decorator(
        self,
        is_async: bool,
        source: SourceState,
        target: str | None,
        conditions: Iterable[Callable[..., Any]],
        permissions: Iterable[Callable[..., Any]],
        custom: Mapping[str, Any] | None,
    ) -> Callable[[Any], FsmTransition[Any, Any]]:
        from .transition import _make_transition  # local import to avoid cycle

        self._validate_states(source, target)
        inner = _make_transition(
            is_async, source, target, conditions, permissions, custom
        )
        column_ref = self

        def wrapper(subject: Any) -> FsmTransition[Any, Any]:
            fsm_t = inner(subject)
            fsm_t.column_ref = column_ref
            return fsm_t

        return wrapper

    def _validate_states(self, source: Any, target: str | None) -> None:
        """Reject state names not in this column's declared allowed set.

        No-op for untyped (`FSMColumn` with no subscript) columns; their
        state set is open.
        """
        allowed = self._allowed_states
        if allowed is None:
            return

        if target is not None and target not in allowed:
            raise exc.SetupError(
                f"FSMColumn transition target {target!r} is not in declared "
                f"states {sorted(allowed)!r}"
            )

        if isinstance(source, str) or source is None:
            sources: tuple[Any, ...] = (source,)
        else:
            try:
                sources = tuple(source)
            except TypeError:
                return  # let FSMMeta raise the real error

        for s in sources:
            if s is None or s == "*" or not isinstance(s, str):
                continue
            if s not in allowed:
                raise exc.SetupError(
                    f"FSMColumn transition source {s!r} is not in declared "
                    f"states {sorted(allowed)!r}"
                )


def _args_have_type(args: tuple[Any, ...]) -> bool:
    """True if any positional arg looks like a SA column type — either an
    instance or a class. Used to detect a user-supplied type so we don't
    clobber it with the auto-injected `FSMField`."""
    for a in args:
        if isinstance(a, sa.types.TypeEngine):
            return True
        if isinstance(a, type) and issubclass(a, sa.types.TypeEngine):
            return True
    return False
