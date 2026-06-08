"""Memoization helpers for keyed computations."""

import weakref
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


@dataclass(slots=True)
class DictCache(Generic[K, V]):
    """Lazy memoizer over an arbitrary mapping (dict, WeakValueDictionary…)."""

    cache: MutableMapping[K, V]
    get_default: Callable[[K], V]

    def get_value(self, key: K) -> V:
        # get/except is faster than `in` + lookup on the hot path.
        try:
            return self.cache[key]
        except KeyError:
            out = self.get_default(key)
            self.cache[key] = out
            return out


def weak_value_cache(get_func: Callable[[K], V]) -> DictCache[K, V]:
    """Cache decorator backed by a `WeakValueDictionary` — values may be GC'd."""
    return DictCache(weakref.WeakValueDictionary(), get_func)


def dict_cache(get_func: Callable[[K], V]) -> DictCache[K, V]:
    """Cache decorator backed by a plain `dict` — values persist for process lifetime."""
    return DictCache({}, get_func)
