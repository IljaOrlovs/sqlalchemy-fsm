"""Regression tests for descriptor-level edge cases.

Covers two narrow but easily-broken paths:

1. SA 2.x ``select(Model.transition)`` — the hybrid descriptor protocol
   exposes the class-bound handle via ``__get__(None, owner)`` even when
   reached through ``select()``. The descriptor must still produce a
   usable SQL expression (via the no-arg ``__call__``) so ``where()`` /
   ``filter_by()`` keep working.

2. Detached / cross-session ``set()`` — the library never validates the
   session attachment state of the row; documenting the contract here
   so the behaviour is fixed (in-memory mutation works, persistence is
   the caller's job) and a regression elsewhere would be caught.
"""

import pytest
import sqlalchemy

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
    expression. We want to make sure that path is still wired through
    SA 2.x's ``select()`` API, not just legacy ``query.filter``.
    """
    session = SessionGen()
    try:
        a, b = Article(), Article()
        b.publish.set()
        session.add_all([a, b])
        session.commit()
        ids = [a.id, b.id]

        stmt = sqlalchemy.select(Article).where(
            Article.publish(), Article.id.in_(ids)
        )
        rows = session.execute(stmt).scalars().all()
        assert rows == [b]

        neg = sqlalchemy.select(Article).where(
            ~Article.publish(), Article.id.in_(ids)
        )
        rows = session.execute(neg).scalars().all()
        assert rows == [a]
    finally:
        session.close()


def test_set_after_expunge_mutates_in_memory():
    """``instance.publish.set()`` on a detached row mutates the attribute.

    Persistence is the caller's responsibility. We don't promise anything
    beyond "the column attribute is updated" — but that contract must
    hold even after the row is detached from its session.
    """
    session = sqlalchemy.orm.sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        a = Article()
        session.add(a)
        session.commit()
        session.expunge_all()

        assert a.state == "draft"
        a.publish.set()
        assert a.state == "published"
    finally:
        session.close()


def test_set_works_across_sessions():
    """A row moved between sessions can still transition.

    The transition machinery operates on Python attributes; it doesn't
    consult the row's ``InstanceState`` for session membership. Verify
    that explicitly so anyone refactoring the dispatch stays honest.
    """
    mk = sqlalchemy.orm.sessionmaker(bind=engine, expire_on_commit=False)
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

        assert a.state == "published"
    finally:
        s1.close()
        s2.close()
