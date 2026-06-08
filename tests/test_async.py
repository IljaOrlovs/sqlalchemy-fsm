"""Verify FSM transitions work under SQLAlchemy 2.x AsyncSession.

The FSM machinery doesn't touch the session itself — it only mutates an
attribute on a mapped instance — so the runtime is async-safe out of
the box. These tests pin that behaviour: state changes, persistence,
events, conditions, and permissions all need to work end-to-end through
an async engine.

The whole module skips on SQLAlchemy 1.4 — `async_sessionmaker` was
added in 2.0.
"""

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

from sqlalchemy_fsm import FSMField, transition  # noqa: E402
from sqlalchemy_fsm.exc import (  # noqa: E402
    InvalidSourceStateError,
    PermissionDeniedError,
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

        @listens_for(AsyncDoc, "before_state_change")
        def _before(instance, source, target):
            seen.append(("before", source, target))

        @listens_for(AsyncDoc, "after_state_change")
        def _after(instance, source, target):
            seen.append(("after", source, target))

        try:
            doc = AsyncDoc()
            async_session.add(doc)
            doc.publish.set()
            await async_session.commit()
        finally:
            remove(AsyncDoc, "before_state_change", _before)
            remove(AsyncDoc, "after_state_change", _after)

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
