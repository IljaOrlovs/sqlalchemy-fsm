from dataclasses import dataclass
from functools import partial
from typing import Any, Generic, TypeVar

import sqlalchemy.event
import sqlalchemy.orm.events
from sqlalchemy.orm.instrumentation import register_class

T = TypeVar("T")


@sqlalchemy.event.dispatcher
class FSMSchemaEvents(sqlalchemy.orm.events.InstanceEvents):
    """Define event listeners for FSM Schema (table) objects."""

    def before_state_change(self, source: str | None, target: str | None) -> None:
        """Event that is fired before the model changes
        form `source` to `target` state."""

    def after_state_change(self, source: str | None, target: str | None) -> None:
        """Event that is fired after the model changes
        form `source` to `target` state."""


@dataclass(slots=True)
class InstanceRef(Generic[T]):
    """This class has to be passed to the dispatch call as instance.

    No idea why it is required.
    """

    target: T

    def obj(self) -> T:
        return self.target


FSM_EVENT_DISPATCHER_CACHE: dict[type, Any] = {}


def get_class_bound_dispatcher(target_cls: type) -> Any:
    """Python class-bound FSM dispatcher class."""
    try:
        out_val = FSM_EVENT_DISPATCHER_CACHE[target_cls]
    except KeyError:
        out_val = register_class(target_cls).dispatch
        FSM_EVENT_DISPATCHER_CACHE[target_cls] = out_val
    return out_val


class BoundFSMDispatcher:
    """Utility method that simplifies sqlalchemy event dispatch."""

    def __init__(self, instance: Any) -> None:
        self.__ref = InstanceRef(instance)
        self.__cls_dispatcher = get_class_bound_dispatcher(type(instance))
        for fsm_handle in ("before_state_change", "after_state_change"):
            # Precompute fsm handles
            getattr(self, fsm_handle)

    def __getattr__(self, name: str) -> Any:
        handle = partial(getattr(self.__cls_dispatcher, name), self.__ref)
        setattr(self, name, handle)
        return handle
