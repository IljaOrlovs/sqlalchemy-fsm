"""Caching tools/classes"""

import weakref
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


@dataclass(slots=True)
class DictCache(Generic[K, V]):
    """Generic object that uses dict-like object for caching."""

    cache: MutableMapping[K, V]
    get_default: Callable[[K], V]

    def get_value(self, key: K) -> V:
        """A method is faster than __getitem__"""
        try:
            return self.cache[key]
        except KeyError:
            out = self.get_default(key)
            self.cache[key] = out
            return out


def weak_value_cache(get_func: Callable[[K], V]) -> DictCache[K, V]:
    """A decorator that makes a new dict_cache using function provided as value getter"""
    return DictCache(weakref.WeakValueDictionary(), get_func)


def dict_cache(get_func: Callable[[K], V]) -> DictCache[K, V]:
    """Generic dict cache decorator"""
    return DictCache({}, get_func)
