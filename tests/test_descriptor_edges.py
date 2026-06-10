"""Descriptor-level contracts that are easy to break in a refactor.

Pins behaviour at the seams between the FSM descriptor and SQLAlchemy's
ORM that the rest of the suite doesn't explicitly exercise: the SA 2.x
``select(...)`` path, transition mutation on detached / cross-session
instances, mapped classes with custom ``__bool__``, and the structured
fields surfaced on FSM exceptions.
"""

from typing import cast

import pytest
import sqlalchemy
from sqlalchemy import orm

from sqlalchemy_fsm import FSMField, transition

from .conftest import Base, SessionGen, engine


class Article(Base):
    __tablename__ = "descriptor_edges_article"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = sqlalchemy.Column(FSMField)

    def __init__(self, *args, **kwargs):
        self.state = "draft"
        super().__init__(*args, **kwargs)

    @transition(source="draft", target="published")
    def publish(self):
        pass


@pytest.fixture(scope="module", autouse=True)
def _create_tables():
    Base.metadata.create_all(engine)


def test_select_with_class_bound_transition_filter():
    """``select(Article).where(Article.publish())`` round-trips.

    The hybrid descriptor's class-side ``__get__`` produces a
    ``ClassBoundFsmTransition`` whose ``__call__`` returns a SA filter
    expression. Pin that this path works under SA 2.x's ``select(...)``
    in addition to the historical ``query.filter(...)`` form.
    """
    session = SessionGen()
    try:
        a, b = Article(), Article()
        b.publish.set()
        session.add_all([a, b])
        session.commit()
        ids = [a.id, b.id]

        stmt = sqlalchemy.select(Article).where(Article.publish(), Article.id.in_(ids))
        rows = session.execute(stmt).scalars().all()
        assert rows == [b]

        neg = sqlalchemy.select(Article).where(~Article.publish(), Article.id.in_(ids))
        rows = session.execute(neg).scalars().all()
        assert rows == [a]
    finally:
        session.close()


def test_set_after_expunge_mutates_in_memory():
    """``instance.publish.set()`` on a detached row updates the attribute.

    The library promises in-memory mutation, nothing more — persistence
    is the caller's. That contract holds whether or not the row is
    currently attached to a session.
    """
    session = orm.sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        a = Article()
        session.add(a)
        session.commit()
        session.expunge_all()

        assert cast("str", a.state) == "draft"
        a.publish.set()
        assert cast("str", a.state) == "published"
    finally:
        session.close()


class Falsy(Base):
    __tablename__ = "descriptor_edges_falsy"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = sqlalchemy.Column(FSMField)

    def __init__(self, *args, **kwargs):
        self.state = "draft"
        super().__init__(*args, **kwargs)

    def __bool__(self) -> bool:
        # Value-object-style mapped class whose truthiness depends on
        # domain state rather than session attachment. The FSM dispatcher
        # must use `is not None`, not truthiness, when deciding whether
        # to wire up event dispatch.
        return False

    @transition(source="draft", target="published")
    def publish(self):
        pass


def test_set_works_on_mapped_instance_overriding_bool():
    """A mapped class with `__bool__ → False` still transitions."""
    f = Falsy()
    assert not bool(f)
    f.publish.set()
    assert cast("str", f.state) == "published"


def test_exception_carries_structured_fields():
    """`InvalidSourceStateError` exposes `current_state`, `target_state`,
    and `transition_name` so callers don't have to parse the message."""
    from sqlalchemy_fsm.exc import InvalidSourceStateError

    a = Article()  # state="draft"
    a.publish.set()  # now "published"

    # No transition from "published" back to "draft" — `publish` requires draft.
    with pytest.raises(InvalidSourceStateError) as info:
        a.publish.set()
    err = info.value
    assert err.current_state == "published"
    assert err.target_state == "published"
    assert err.transition_name == "publish"


def test_set_works_across_sessions():
    """A row moved between sessions can still transition.

    The transition machinery operates on Python attributes only — it
    doesn't consult the row's ``InstanceState`` for session membership —
    so re-attaching to a different session is fine.
    """
    mk = orm.sessionmaker(bind=engine, expire_on_commit=False)
    s1 = mk()
    s2 = mk()
    try:
        a = Article()
        s1.add(a)
        s1.commit()
        s1.expunge(a)

        s2.add(a)  # re-attach to a different session
        a.publish.set()
        s2.commit()

        assert cast("str", a.state) == "published"
    finally:
        s1.close()
        s2.close()
