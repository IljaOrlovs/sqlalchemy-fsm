import weakref
from collections.abc import Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any, Generic, TypeVar

import sqlalchemy.event
import sqlalchemy.orm.events
from sqlalchemy.orm.instrumentation import register_class

T = TypeVar("T")


@sqlalchemy.event.dispatcher
class FSMSchemaEvents(sqlalchemy.orm.events.InstanceEvents):
    """SQLAlchemy event hooks fired around every FSM transition.

    `before_transition` / `after_transition` carry `(instance,
    transition_name, source, target, args, kwargs)`. `instance` is
    injected by SA's `InstanceEvents` as the first positional;
    `transition_name`, `source`, `target`, `args`, `kwargs` are the
    kwargs sqlalchemy-fsm sends.
    """

    def before_transition(
        self,
        transition_name: str,
        source: str | None,
        target: str | None,
        args: tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> None:
        """Fires immediately before the transition handler runs."""

    def after_transition(
        self,
        transition_name: str,
        source: str | None,
        target: str | None,
        args: tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> None:
        """Fires after the handler and after the state field has been updated."""


@dataclass(slots=True)
class InstanceRef(Generic[T]):
    """Wrapper accepted by SQLAlchemy's dispatch as the `instance` argument."""

    target: T

    def obj(self) -> T:
        return self.target


# WeakKeyDictionary so dynamically-built model classes (test fixtures,
# factory patterns) don't leak. Most real-world classes outlive the process
# anyway, but the weak ref costs us nothing.
FSM_EVENT_DISPATCHER_CACHE: "weakref.WeakKeyDictionary[type, Any]" = (
    weakref.WeakKeyDictionary()
)


def get_class_bound_dispatcher(target_cls: type) -> Any:
    """Lazily register `target_cls` with SQLAlchemy's instrumentation and
    cache the resulting dispatcher."""
    try:
        return FSM_EVENT_DISPATCHER_CACHE[target_cls]
    except KeyError:
        out_val = register_class(target_cls).dispatch
        FSM_EVENT_DISPATCHER_CACHE[target_cls] = out_val
        return out_val


class BoundFSMDispatcher:
    """Per-instance fan-out to SQLAlchemy's class-level event dispatcher.

    Only the two FSM events (``before_transition`` / ``after_transition``)
    are exposed. Any other attribute access raises ``AttributeError`` so
    we don't accidentally fan out unrelated SA InstanceEvents (load,
    refresh, expire, …) through this object.
    """

    __slots__ = (
        "_cls_dispatcher",
        "_ref",
        "after_transition",
        "before_transition",
    )

    def __init__(self, instance: Any) -> None:
        self._ref = InstanceRef(instance)
        self._cls_dispatcher = get_class_bound_dispatcher(type(instance))
        # Eagerly bind the FSM events; the hot path then hits a slot,
        # not a descriptor lookup.
        self.before_transition = partial(
            self._cls_dispatcher.before_transition, self._ref
        )
        self.after_transition = partial(
            self._cls_dispatcher.after_transition, self._ref
        )
