"""Helpers for testing `@transition`-decorated models.

Kept separate from `sqlalchemy_fsm.introspection` (which is for
runtime use) so the test-only target is signposted in import paths
and IDE autocomplete.
"""

from __future__ import annotations

from .transition import FsmTransition


def get_transition(model_cls: type, name: str) -> FsmTransition:
    """Return the underlying `FsmTransition` descriptor by attribute name.

    `Model.publish` returns the per-call class-bound wrapper used for
    SA filter expressions, so it's not a stable target for tests that
    want to inspect or patch the handler. `get_transition(Model,
    "publish")` walks the MRO and hands back the descriptor — its
    ``.fn`` is the raw handler, settable for monkey-patching:

        monkeypatch.setattr(get_transition(BlogPost, "publish"), "fn", mock)

    Raises `AttributeError` if no `@transition` with that name exists.
    """
    for klass in model_cls.__mro__:
        attr = klass.__dict__.get(name)
        if isinstance(attr, FsmTransition):
            return attr
    raise AttributeError(
        f"{model_cls.__name__!r} has no @transition attribute named {name!r}"
    )
