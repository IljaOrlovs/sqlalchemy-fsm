"""Verify FSM transitions work under SQLAlchemy 2.x AsyncSession.

The FSM machinery doesn't touch the session itself — it only mutates an
attribute on a mapped instance — so the runtime is async-safe out of
the box. These tests pin that behaviour: state changes, persistence,
events, conditions, and permissions all need to work end-to-end through
an async engine.

The whole module skips on SQLAlchemy 1.4 — `async_sessionmaker` was
added in 2.0.
"""

from typing import ClassVar

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")
if sqlalchemy.__version__.startswith("1."):  # pragma: no cover
    pytest.skip("AsyncSession tests require SQLAlchemy 2.x", allow_module_level=True)

import pytest_asyncio  # noqa: E402
from sqlalchemy.event import listens_for, remove  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import declarative_base  # noqa: E402

from sqlalchemy_fsm import FSMField, async_transition, transition  # noqa: E402
from sqlalchemy_fsm.exc import (  # noqa: E402
    InvalidSourceStateError,
    PermissionDeniedError,
    PreconditionError,
    SetupError,
)

AsyncBase = declarative_base()


def is_editor(instance, user=None, **_):
    return getattr(user, "role", None) == "editor"


def can_publish(instance, **_):
    return True


class AsyncDoc(AsyncBase):
    __tablename__ = "AsyncDoc"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = sqlalchemy.Column(FSMField)

    def __init__(self, *a, **kw):
        self.state = "draft"
        super().__init__(*a, **kw)

    @transition(source="draft", target="published", conditions=[can_publish])
    def publish(self):
        pass

    @transition(source="draft", target="archived", permissions=[is_editor])
    def archive(self, user=None):
        pass


@pytest_asyncio.fixture
async def async_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(AsyncBase.metadata.create_all)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with session_factory() as session:
        yield session
    await engine.dispose()


class TestAsyncBasic:
    async def test_transition_persists_through_async_session(self, async_session):
        doc = AsyncDoc()
        async_session.add(doc)
        await async_session.commit()

        doc.publish.set()
        assert str(doc.state) == "published"
        await async_session.commit()

        # Re-fetch and confirm the new state survived the round-trip.
        await async_session.refresh(doc)
        assert str(doc.state) == "published"

    async def test_invalid_transition_raises(self, async_session):
        doc = AsyncDoc()
        async_session.add(doc)
        doc.publish.set()
        await async_session.commit()
        # Already published — no longer in the 'draft' source.
        with pytest.raises(InvalidSourceStateError):
            doc.publish.set()

    async def test_class_bound_filter_works_through_async_session(self, async_session):
        d1, d2 = AsyncDoc(), AsyncDoc()
        async_session.add_all([d1, d2])
        await async_session.commit()
        d1.publish.set()
        await async_session.commit()

        result = await async_session.execute(
            sqlalchemy.select(AsyncDoc).filter(AsyncDoc.publish())
        )
        published = result.scalars().all()
        assert {d.id for d in published} == {d1.id}


class TestAsyncEvents:
    async def test_events_fire_under_async_session(self, async_session):
        seen: list[tuple] = []

        @listens_for(AsyncDoc, "before_transition")
        def _before(instance, transition_name, source, target, args, kwargs):
            seen.append(("before", source, target))

        @listens_for(AsyncDoc, "after_transition")
        def _after(instance, transition_name, source, target, args, kwargs):
            seen.append(("after", source, target))

        try:
            doc = AsyncDoc()
            async_session.add(doc)
            doc.publish.set()
            await async_session.commit()
        finally:
            remove(AsyncDoc, "before_transition", _before)
            remove(AsyncDoc, "after_transition", _after)

        assert seen == [
            ("before", "draft", "published"),
            ("after", "draft", "published"),
        ]


class TestAsyncPermissions:
    async def test_permission_denied_propagates(self, async_session):
        doc = AsyncDoc()
        async_session.add(doc)
        await async_session.commit()
        with pytest.raises(PermissionDeniedError):
            doc.archive.set()
        # State unchanged after a denied transition.
        await async_session.refresh(doc)
        assert str(doc.state) == "draft"

    async def test_permission_allowed_passes(self, async_session):
        class _Editor:
            role = "editor"

        doc = AsyncDoc()
        async_session.add(doc)
        doc.archive.set(user=_Editor())
        await async_session.commit()
        await async_session.refresh(doc)
        assert str(doc.state) == "archived"


# --- async_transition coverage -------------------------------------------------


async def _async_can_publish(instance, **_):
    return True


async def _async_is_editor(instance, user=None, **_):
    return getattr(user, "role", None) == "editor"


class AsyncHandlerDoc(AsyncBase):
    __tablename__ = "AsyncHandlerDoc"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = sqlalchemy.Column(FSMField)
    side_effect: ClassVar[list] = []

    def __init__(self, *a, **kw):
        self.state = "draft"
        super().__init__(*a, **kw)

    @async_transition(source="draft", target="published", conditions=[_async_can_publish])
    async def publish(self):
        type(self).side_effect.append("published")

    @async_transition(source="draft", target="archived", permissions=[_async_is_editor])
    async def archive(self, user=None):
        type(self).side_effect.append("archived")

    # Mixing sync callables in an async transition is allowed.
    @async_transition(source="published", target="retracted", conditions=[can_publish])
    def retract(self):  # sync handler under async_transition is fine
        type(self).side_effect.append("retracted")


@pytest_asyncio.fixture
async def async_handler_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(AsyncBase.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with sf() as session:
        AsyncHandlerDoc.side_effect = []
        yield session
    await engine.dispose()


class TestAsyncTransition:
    async def test_async_handler_runs_and_state_persists(self, async_handler_session):
        doc = AsyncHandlerDoc()
        async_handler_session.add(doc)
        await async_handler_session.commit()

        await doc.publish.aset()
        assert AsyncHandlerDoc.side_effect == ["published"]
        assert str(doc.state) == "published"

        await async_handler_session.commit()
        await async_handler_session.refresh(doc)
        assert str(doc.state) == "published"

    async def test_async_permission_denied_propagates(self, async_handler_session):
        doc = AsyncHandlerDoc()
        async_handler_session.add(doc)
        with pytest.raises(PermissionDeniedError):
            await doc.archive.aset()
        assert str(doc.state) == "draft"

    async def test_async_permission_allowed(self, async_handler_session):
        class _Editor:
            role = "editor"

        doc = AsyncHandlerDoc()
        async_handler_session.add(doc)
        await doc.archive.aset(user=_Editor())
        assert str(doc.state) == "archived"

    async def test_async_invalid_source_raises(self, async_handler_session):
        doc = AsyncHandlerDoc()
        async_handler_session.add(doc)
        await doc.publish.aset()
        with pytest.raises(InvalidSourceStateError):
            await doc.publish.aset()

    async def test_async_can_proceed(self, async_handler_session):
        doc = AsyncHandlerDoc()
        async_handler_session.add(doc)
        assert await doc.publish.acan_proceed() is True
        await doc.publish.aset()
        assert await doc.publish.acan_proceed() is False

    async def test_sync_callable_under_async_transition(self, async_handler_session):
        doc = AsyncHandlerDoc()
        async_handler_session.add(doc)
        await doc.publish.aset()
        await doc.retract.aset()
        assert str(doc.state) == "retracted"
        assert AsyncHandlerDoc.side_effect == ["published", "retracted"]

    async def test_failed_async_condition_raises(self, async_handler_session):
        async def _never(instance, **_):
            return False

        class _Doc(AsyncBase):
            __tablename__ = "AsyncCondDoc"
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
            state = sqlalchemy.Column(FSMField)

            def __init__(self, *a, **kw):
                self.state = "draft"
                super().__init__(*a, **kw)

            @async_transition(source="draft", target="published", conditions=[_never])
            async def publish(self):
                pass

        async with async_handler_session.bind.begin() as conn:
            await conn.run_sync(_Doc.__table__.create)

        doc = _Doc()
        async_handler_session.add(doc)
        with pytest.raises(PreconditionError):
            await doc.publish.aset()


class TestAsyncTransitionGuards:
    def test_aset_outside_loop_raises(self):
        doc = AsyncHandlerDoc()
        # No running loop → SetupError. The descriptor itself is reached
        # synchronously; calling aset() must build a coroutine, so we
        # invoke it via send() to exercise the guard without awaiting.
        coro = doc.publish.aset()
        try:
            with pytest.raises(SetupError):
                coro.send(None)
        finally:
            coro.close()

    def test_set_on_async_transition_missing(self):
        doc = AsyncHandlerDoc()
        # AsyncInstanceBoundFsmTransition has no `.set`.
        assert not hasattr(doc.publish, "set")


class TestAsyncClassTransitionMixingForbidden:
    def test_mixing_sync_and_async_subhandlers_errors(self):
        class _Bad(AsyncBase):
            __tablename__ = "AsyncMixedDoc"
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
            state = sqlalchemy.Column(FSMField)

            def __init__(self, *a, **kw):
                self.state = "draft"
                super().__init__(*a, **kw)

            @async_transition(source="*", target="done")
            class go:  # noqa: N801
                @transition(source="draft")  # sync sub under async parent
                def from_draft(self):
                    pass

        with pytest.raises(SetupError):
            # Touching the descriptor triggers the merge.
            _ = _Bad().go.acan_proceed


# --- awaitable predicate + awaitable-condition resolution ----------------


class AwaitableDoc(AsyncBase):
    __tablename__ = "AwaitableDoc"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = sqlalchemy.Column(FSMField)

    def __init__(self, *a, **kw):
        self.state = "draft"
        super().__init__(*a, **kw)

    @async_transition(source="draft", target="published")
    async def publish(self):
        pass


async def test_async_predicate_is_sync_property():
    """`.is_current` is a plain attribute compare — no await needed on
    sync or async transitions, so the same predicate shape works
    everywhere."""
    doc = AwaitableDoc()
    assert doc.publish.is_current is False  # current state is "draft"
    await doc.publish.aset()
    assert doc.publish.is_current is True


async def test_async_condition_returning_task_is_awaited():
    """A sync condition returning a `Task` (or any non-coroutine
    awaitable) is awaited like a coroutine — the object-truth shortcut
    would silently pass these as truthy, which is the wrong answer when
    the underlying value is meant to gate the transition."""
    import asyncio

    async def _async_helper():
        return False  # condition should resolve to False, blocking the transition

    def returns_task(instance):
        return asyncio.ensure_future(_async_helper())

    class _Doc(AsyncBase):
        __tablename__ = "AwaitableCondDoc"
        id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
        state = sqlalchemy.Column(FSMField)

        def __init__(self, *a, **kw):
            self.state = "draft"
            super().__init__(*a, **kw)

        @async_transition(source="draft", target="published", conditions=[returns_task])
        async def publish(self):
            pass

    doc = _Doc()
    with pytest.raises(PreconditionError):
        await doc.publish.aset()
    assert doc.state == "draft"
