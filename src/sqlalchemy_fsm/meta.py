"""FSM meta object."""

import collections.abc

from . import util


class FSMMeta(object):
    __slots__ = (
        "target",
        "conditions",
        "sources",
        "bound_cls",
        "extra_call_args",
    )

    def __init__(self, source, target, conditions, extra_args, bound_cls):
        self.bound_cls = bound_cls
        self.conditions = tuple(conditions)
        self.extra_call_args = tuple(extra_args)

        if target is not None:
            if not util.is_valid_fsm_state(target):
                raise NotImplementedError(target)
            self.target = target
        else:
            self.target = None

        if util.is_valid_source_state(source):
            all_sources = (source,)
        elif isinstance(source, collections.abc.Iterable):
            all_sources = tuple(source)

            if not all(util.is_valid_source_state(el) for el in all_sources):
                raise NotImplementedError(all_sources)
        else:
            raise NotImplementedError(source)

        self.sources = frozenset(all_sources)

    def get_bound(self, sqlalchemy_handle, set_func, extra_args):
        return self.bound_cls(self, sqlalchemy_handle, set_func, extra_args)

    def __repr__(self):
        return (
            "<{} sources={!r} target={!r} conditions={!r} extra call args={!r}>".format(
                self.__class__.__name__,
                self.sources,
                self.target,
                self.conditions,
                self.extra_call_args,
            )
        )
