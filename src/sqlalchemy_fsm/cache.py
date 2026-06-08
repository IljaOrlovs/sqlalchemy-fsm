"""Caching tools/classes"""

import weakref
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class DictCache:
    """Generic object that uses dict-like object for caching."""

    cache: MutableMapping[Any, Any]
    get_default: Callable[[Any], Any]

    def get_value(self, key: Any) -> Any:
        """A method is faster than __getitem__"""
        try:
            return self.cache[key]
        except KeyError:
            out = self.get_default(key)
            self.cache[key] = out
            return out


def weak_value_cache(get_func: Callable[[Any], Any]) -> DictCache:
    """A decorator that makes a new dict_cache using function provided as value getter"""
    return DictCache(weakref.WeakValueDictionary(), get_func)


def dict_cache(get_func: Callable[[Any], Any]) -> DictCache:
    """Generic dict cache decorator"""
    return DictCache({}, get_func)
