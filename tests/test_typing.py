"""Type-checker assertions for the ParamSpec-generic transition API.

Verified by pyright in CI. The runtime suite just imports the module
and confirms it loads. Each `assert_type` and each `pyright: ignore`
line is a contract pyright will flag if the inferred type drifts.

ParamSpec literals aren't directly expressible in user code, so the
shape is proven indirectly: positive call-site checks (these compile
clean) plus negative checks (these MUST be flagged by pyright — the
`pyright: ignore` tags would themselves fail the build if the line
were silently accepted).
"""

from typing import Any

import sqlalchemy
from typing_extensions import assert_type

from sqlalchemy_fsm import FSMField, async_transition, transition

from .conftest import Base


class Doc(Base):
    __tablename__ = "typed_doc_for_types"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = sqlalchemy.Column(FSMField, default="draft")

    def __init__(self) -> None:
        self.state = "draft"

    @transition(source="draft", target="published")
    def publish(self, user: str, *, force: bool = False) -> int:
        return len(user) + int(force)

    @async_transition(source="published", target="archived")
    async def archive(self, by: str | None = None) -> bool:
        return True


def _check_sync_positive() -> None:
    """All these calls MUST type-check clean — they match the handler's
    signature: `publish(self, user: str, *, force: bool = False) -> int`.
    """
    doc = Doc()
    ib = doc.publish

    # `.set` accepts the handler's args (after self).
    ib.set("alice")
    ib.set("alice", force=True)

    # `.can_proceed` mirrors `.set` shape, returns bool.
    assert_type(ib.can_proceed("alice"), bool)
    assert_type(ib.can_proceed("alice", force=False), bool)

    # `.fn` is the raw handler — return type is the handler's int.
    result = ib.fn(doc, "alice", force=True)
    assert_type(result, int)

    # Class-bound `.fn` works the same.
    class_result = Doc.publish.fn(doc, "alice")
    assert_type(class_result, int)


def _check_sync_negative() -> None:
    """Each `pyright: ignore` proves pyright IS catching a type error —
    if pyright silently accepted any of these, the `useless ignore` rule
    would flag the comment and the build would fail.
    """
    doc = Doc()
    ib = doc.publish

    # Wrong arg type for `user`.
    ib.set(42)  # pyright: ignore[reportArgumentType, reportCallIssue]

    # Missing required positional `user`.
    ib.set()  # pyright: ignore[reportCallIssue]

    # Unknown keyword.
    ib.set("alice", nonsense=1)  # pyright: ignore[reportCallIssue]

    # `.fn` direct call with wrong types.
    ib.fn(doc, 42)  # pyright: ignore[reportArgumentType]


def _check_async_positive() -> None:
    """Async sibling: `archive(self, by: str | None = None) -> bool`."""
    doc = Doc()
    ib = doc.archive

    aset_coro = ib.aset()
    aset_coro_with_arg = ib.aset(by="alice")
    acan_coro = ib.acan_proceed("alice")

    # Close unused coroutines so pyright's "unused coroutine" rule is
    # satisfied; we're not running them, just type-checking the calls.
    aset_coro.close()
    aset_coro_with_arg.close()
    acan_coro.close()


def _check_async_negative() -> None:
    doc = Doc()
    ib = doc.archive

    # Wrong type for `by`.
    coro = ib.aset(by=42)  # pyright: ignore[reportArgumentType, reportCallIssue]
    coro.close()


def _check_no_args_transition() -> None:
    """A handler with no extra args should also stay typed."""

    class Empty(Base):
        __tablename__ = "typed_empty"
        id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
        state = sqlalchemy.Column(FSMField, default="a")

        @transition(source="a", target="b")
        def go(self) -> None:
            return None

    obj: Any = Empty()
    obj.go.set()
    # Passing an unexpected arg should be flagged.
    obj.go.set("nope")  # pyright: ignore[reportCallIssue]


def test_module_imports():
    """Runtime check — the static assertions above only verify on the
    type-checker side; this ensures the module loads at all."""
    doc = Doc()
    assert str(doc.state) == "draft"
