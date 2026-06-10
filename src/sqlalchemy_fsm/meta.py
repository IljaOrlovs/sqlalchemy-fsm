"""`FSMMeta` — the validated descriptor attached to every `@transition`."""

import collections.abc
import types
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from . import util

_EMPTY_CUSTOM: Mapping[str, Any] = types.MappingProxyType({})


class FSMMeta:
    __slots__ = (
        "bound_cls",
        "conditions",
        "custom",
        "extra_call_args",
        "is_async",
        "permissions",
        "sources",
        "target",
    )

    bound_cls: type
    conditions: tuple[Callable[..., Any], ...]
    permissions: tuple[Callable[..., Any], ...]
    extra_call_args: tuple[Any, ...]
    sources: frozenset[str | None]
    target: str | None
    is_async: bool
    custom: Mapping[str, Any]

    def __init__(
        self,
        source: Any,
        target: str | None,
        conditions: Iterable[Callable[..., Any]],
        extra_args: Iterable[Any],
        bound_cls: type,
        permissions: Iterable[Callable[..., Any]] = (),
        custom: Mapping[str, Any] | None = None,
    ) -> None:
        # `is_async` is fully derived from `bound_cls`.
        from . import bound as _bound  # local import to avoid cycle

        self.bound_cls = bound_cls
        self.is_async = issubclass(
            bound_cls, (_bound.AsyncBoundFSMFunction, _bound.AsyncBoundFSMClass)
        )
        self.conditions = tuple(conditions)
        self.permissions = tuple(permissions)
        self.extra_call_args = tuple(extra_args)
        # Freeze caller-supplied dict so listeners can't mutate it across
        # invocations and leak state between transitions.
        self.custom = (
            _EMPTY_CUSTOM if not custom else types.MappingProxyType(dict(custom))
        )

        if target is not None:
            if not util.is_valid_fsm_state(target):
                raise NotImplementedError(target)
            self.target = target
        else:
            self.target = None

        if util.is_valid_source_state(source):
            all_sources: tuple[Any, ...] = (source,)
        elif isinstance(source, collections.abc.Iterable):
            all_sources = tuple(source)

            if not all(util.is_valid_source_state(el) for el in all_sources):
                raise NotImplementedError(all_sources)
        else:
            raise NotImplementedError(source)

        self.sources = frozenset(all_sources)

    def get_bound(
        self,
        sqlalchemy_handle: Any,
        set_func: Callable[..., Any],
        extra_args: tuple[Any, ...],
    ) -> Any:
        return self.bound_cls(self, sqlalchemy_handle, set_func, extra_args)

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} "
            f"sources={self.sources!r} "
            f"target={self.target!r} "
            f"conditions={self.conditions!r} "
            f"permissions={self.permissions!r} "
            f"extra call args={self.extra_call_args!r} "
            f"custom={dict(self.custom)!r}>"
        )
