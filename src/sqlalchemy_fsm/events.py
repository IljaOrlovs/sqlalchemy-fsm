import weakref
from dataclasses import dataclass
from functools import partial
from typing import Any, Generic, TypeVar

import sqlalchemy.event
import sqlalchemy.orm.events
from sqlalchemy.orm.instrumentation import register_class

T = TypeVar("T")


@sqlalchemy.event.dispatcher
class FSMSchemaEvents(sqlalchemy.orm.events.InstanceEvents):
    """SQLAlchemy event hooks fired around every FSM transition."""

    def before_state_change(self, source: str | None, target: str | None) -> None:
        """Fires immediately before the transition handler runs."""

    def after_state_change(self, source: str | None, target: str | None) -> None:
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
    """Per-instance fan-out to SQLAlchemy's class-level event dispatcher."""

    def __init__(self, instance: Any) -> None:
        self.__ref = InstanceRef(instance)
        self.__cls_dispatcher = get_class_bound_dispatcher(type(instance))
        # Eagerly materialise the two FSM events so the hot path skips __getattr__.
        for fsm_handle in ("before_state_change", "after_state_change"):
            getattr(self, fsm_handle)

    def __getattr__(self, name: str) -> Any:
        handle = partial(getattr(self.__cls_dispatcher, name), self.__ref)
        setattr(self, name, handle)
        return handle
