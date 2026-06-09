"""Tests for the three features borrowed from django-fsm:

- `custom={}` metadata bag on `@transition`
- Richer `before_transition` / `after_transition` events (name + args)
- `available_transitions(instance, ...)` introspection helper
"""

from typing import ClassVar, cast

import pytest
import sqlalchemy
import sqlalchemy.event

import sqlalchemy_fsm
from sqlalchemy_fsm import (
    FSMField,
    available_transitions,
    transition,
)
from sqlalchemy_fsm.introspection import iter_transitions

from .conftest import Base

# ---------------------------------------------------------------------------
# FSMField default length padding
# ---------------------------------------------------------------------------


class TestFsmFieldDefaultLength:
    def test_default_length_is_padded(self):
        # `published` is the longest state at 9 chars; with the 3x padding
        # factor that's a VARCHAR(27).
        field = FSMField["draft", "published", "archived"]()
        assert field.length == 9 * 3

    def test_explicit_length_overrides_default(self):
        field = FSMField["draft", "published"](length=64)
        assert field.length == 64

    def test_plain_fsmfield_has_no_default_length(self):
        # Without a declared state set we still defer to SA's String default.
        assert FSMField().length is None


# ---------------------------------------------------------------------------
# custom={} metadata bag
# ---------------------------------------------------------------------------


class WithCustom(Base):
    __tablename__ = "borrowed_with_custom"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = sqlalchemy.Column(FSMField, default="draft")

    def __init__(self):
        self.state = "draft"

    @transition(
        source="draft",
        target="published",
        custom={"label": "Publish post", "icon": "rocket"},
    )
    def publish(self):
        pass

    @transition(source="draft", target="archived")
    def archive(self):
        pass


class TestCustomMetadata:
    def test_custom_is_exposed_on_meta(self):
        for name, fsm_t in iter_transitions(WithCustom):
            if name == "publish":
                assert dict(fsm_t.meta.custom) == {
                    "label": "Publish post",
                    "icon": "rocket",
                }
            elif name == "archive":
                assert dict(fsm_t.meta.custom) == {}

    def test_custom_is_frozen(self):
        for name, fsm_t in iter_transitions(WithCustom):
            if name == "publish":
                with pytest.raises(TypeError):
                    fsm_t.meta.custom["label"] = "Mutated"  # type: ignore[index]

    def test_caller_dict_is_copied_not_referenced(self):
        # Mutating the dict the caller handed in must not affect the
        # frozen view stored on the meta.
        shared = {"k": "v1"}

        class Doc(Base):
            __tablename__ = "borrowed_custom_isolation"
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
            state = sqlalchemy.Column(FSMField, default="a")

            @transition(source="a", target="b", custom=shared)
            def go(self):
                pass

        shared["k"] = "v2"
        for name, fsm_t in iter_transitions(Doc):
            if name == "go":
                assert fsm_t.meta.custom["k"] == "v1"


# ---------------------------------------------------------------------------
# Richer transition events
# ---------------------------------------------------------------------------


class RichEventModel(Base):
    __tablename__ = "borrowed_rich_events"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = sqlalchemy.Column(FSMField, default="new")

    def __init__(self):
        self.state = "new"

    @transition(source="new", target="ready")
    def make_ready(self, by=None):
        pass


class TestRichEvents:
    def test_before_transition_carries_full_payload(self):
        seen = []

        def listener(instance, transition_name, source, target, args, kwargs):
            seen.append(
                {
                    "instance": instance,
                    "transition_name": transition_name,
                    "source": source,
                    "target": target,
                    "args": args,
                    "kwargs": kwargs,
                }
            )

        sqlalchemy.event.listen(RichEventModel, "before_transition", listener)
        try:
            obj = RichEventModel()
            obj.make_ready.set(by="alice")
        finally:
            sqlalchemy.event.remove(RichEventModel, "before_transition", listener)

        assert len(seen) == 1
        payload = seen[0]
        assert payload["instance"] is obj
        assert payload["transition_name"] == "make_ready"
        assert payload["source"] == "new"
        assert payload["target"] == "ready"
        assert payload["args"] == ()
        assert payload["kwargs"] == {"by": "alice"}

    def test_after_transition_fires_post_mutation(self):
        seen_states = []

        def listener(instance, transition_name, source, target, args, kwargs):
            seen_states.append(cast("str", instance.state))

        sqlalchemy.event.listen(RichEventModel, "after_transition", listener)
        try:
            obj = RichEventModel()
            obj.make_ready.set()
        finally:
            sqlalchemy.event.remove(RichEventModel, "after_transition", listener)

        assert seen_states == ["ready"]


# ---------------------------------------------------------------------------
# Class-grouped transitions emit the public name, not the sub-handler name
# ---------------------------------------------------------------------------


class GroupedModel(Base):
    __tablename__ = "borrowed_grouped"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = sqlalchemy.Column(FSMField, default="draft")

    def __init__(self):
        self.state = "draft"

    @transition(target="published")
    class publish:  # noqa: N801 — lowercase is the user-facing transition name
        @transition(source="draft")
        def from_draft(self, instance):
            pass

        @transition(source="archived")
        def from_archive(self, instance):
            pass

    @transition(source=["draft", "published"], target="archived")
    def archive(self):
        pass


class TestGroupedTransitionEventName:
    def test_event_reports_outer_class_name(self):
        names = []

        def listener(instance, transition_name, source, target, args, kwargs):
            names.append(transition_name)

        sqlalchemy.event.listen(GroupedModel, "after_transition", listener)
        try:
            obj = GroupedModel()
            obj.publish.set()
        finally:
            sqlalchemy.event.remove(GroupedModel, "after_transition", listener)

        # Not "from_draft" — the sub-handler — but the user-facing class
        # name `publish`. This is the whole point of plumbing
        # `transition_name` down.
        assert names == ["publish"]


# ---------------------------------------------------------------------------
# available_transitions() helper
# ---------------------------------------------------------------------------


def _is_owner(instance, user=None, **_):
    return user == "owner"


class AvailModel(Base):
    __tablename__ = "borrowed_avail"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = sqlalchemy.Column(FSMField, default="draft")

    def __init__(self):
        self.state = "draft"

    @transition(source="draft", target="published", permissions=[_is_owner])
    def publish(self, user=None):
        pass

    @transition(source=["draft", "published"], target="archived")
    def archive(self):
        pass

    @transition(source="archived", target="draft")
    def restore(self):
        pass


class TestAvailableTransitions:
    def test_filters_by_current_state(self):
        obj = AvailModel()
        # In 'draft': publish (with owner) and archive are reachable.
        names = [n for n, _ in available_transitions(obj, user="owner")]
        assert set(names) == {"publish", "archive"}

    def test_filters_by_permission(self):
        obj = AvailModel()
        # Without the user kwarg, publish's permission check fails.
        names = [n for n, _ in available_transitions(obj)]
        assert set(names) == {"archive"}

    def test_respects_current_state(self):
        obj = AvailModel()
        obj.archive.set()
        # Only restore is reachable from 'archived'.
        names = [n for n, _ in available_transitions(obj, user="owner")]
        assert names == ["restore"]

    def test_returns_descriptor(self):
        obj = AvailModel()
        for name, fsm_t in available_transitions(obj, user="owner"):
            assert hasattr(fsm_t, "meta")
            assert fsm_t.meta.target in {"published", "archived"}
            assert name in {"publish", "archive"}


# ---------------------------------------------------------------------------
# aavailable_transitions() — async sibling
# ---------------------------------------------------------------------------


async def _async_is_owner(instance, user=None, **_):
    return user == "owner"


class AvailAsyncModel(Base):
    __tablename__ = "borrowed_avail_async"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = sqlalchemy.Column(FSMField, default="draft")

    def __init__(self):
        self.state = "draft"

    @sqlalchemy_fsm.async_transition(
        source="draft", target="published", permissions=[_async_is_owner]
    )
    async def publish(self, user=None):
        pass

    @transition(source="draft", target="archived")
    def archive(self):
        pass


# ---------------------------------------------------------------------------
# ParamSpec typing — runtime smoke + type-checker assertions
# ---------------------------------------------------------------------------


class TypedDoc(Base):
    __tablename__ = "borrowed_typed_doc"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = sqlalchemy.Column(FSMField, default="draft")

    def __init__(self):
        self.state = "draft"

    @transition(source="draft", target="published")
    def publish(self, user: str, *, force: bool = False) -> int:
        return len(user) + int(force)


class TestParamSpecRuntime:
    """Runtime parity for the ParamSpec-typed surface. The actual type
    precision is exercised by pyright in CI — these tests just pin the
    runtime behavior that the types describe."""

    def test_fn_returns_handler_value(self):
        # `.fn` is the raw handler — direct call returns whatever the
        # handler returns. Guards do NOT run; column NOT mutated.
        obj = TypedDoc()
        result = TypedDoc.publish.fn(obj, "alice", force=True)
        assert result == 6
        assert cast("str", obj.state) == "draft"

    def test_set_forwards_args_to_handler(self):
        obj = TypedDoc()
        obj.publish.set("alice", force=False)
        assert cast("str", obj.state) == "published"

    def test_can_proceed_accepts_handler_args(self):
        obj = TypedDoc()
        assert obj.publish.can_proceed("alice", force=True)


# ---------------------------------------------------------------------------
# .fn exposes the raw handler for unit tests / mocking
# ---------------------------------------------------------------------------


class HandlerExposure(Base):
    __tablename__ = "borrowed_handler_exposure"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = sqlalchemy.Column(FSMField, default="draft")
    side_effects: ClassVar[list[str]] = []

    def __init__(self):
        self.state = "draft"

    @transition(source="draft", target="published")
    def publish(self):
        type(self).side_effects.append(f"published-{id(self)}")


class TestHandlerExposure:
    def setup_method(self):
        HandlerExposure.side_effects.clear()

    def test_class_bound_fn_calls_handler_directly(self):
        # Bypasses source-state, permission, condition checks. The
        # column is NOT mutated — the handler just runs.
        obj = HandlerExposure()
        HandlerExposure.publish.fn(obj)
        assert HandlerExposure.side_effects == [f"published-{id(obj)}"]
        assert cast("str", obj.state) == "draft"  # guard not run

    def test_instance_bound_fn_returns_same_callable(self):
        obj = HandlerExposure()
        assert obj.publish.fn is HandlerExposure.publish.fn

    def test_get_transition_returns_descriptor(self):
        from sqlalchemy_fsm import get_transition

        descriptor = get_transition(HandlerExposure, "publish")
        assert descriptor.fn is HandlerExposure.publish.fn
        assert descriptor.set_fn is descriptor.fn  # back-compat alias

    def test_get_transition_raises_on_unknown_name(self):
        from sqlalchemy_fsm import get_transition

        with pytest.raises(AttributeError, match="no @transition attribute"):
            get_transition(HandlerExposure, "nope")

    def test_descriptor_fn_is_settable_for_mocking(self, monkeypatch):
        # Mock the handler via the descriptor; subsequent set() calls
        # see the replacement because the bound wrapper is rebuilt on
        # every attribute access.
        from sqlalchemy_fsm import get_transition

        descriptor = get_transition(HandlerExposure, "publish")
        calls = []

        def stub(instance):
            calls.append(instance)

        monkeypatch.setattr(descriptor, "fn", stub)

        obj = HandlerExposure()
        obj.publish.set()

        assert calls == [obj]
        assert cast("str", obj.state) == "published"  # guards + mutation still ran
        # original side effect did NOT fire — the stub replaced the body.
        assert HandlerExposure.side_effects == []


@pytest.mark.asyncio
async def test_aavailable_transitions_handles_mixed():
    from sqlalchemy_fsm import aavailable_transitions

    obj = AvailAsyncModel()
    names = [n for n, _ in await aavailable_transitions(obj, user="owner")]
    assert set(names) == {"publish", "archive"}

    # Wrong user — async permission denies publish, sync archive still available.
    names = [n for n, _ in await aavailable_transitions(obj)]
    assert set(names) == {"archive"}
