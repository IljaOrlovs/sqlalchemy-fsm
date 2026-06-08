"""Tests for `sqlalchemy_fsm.extras.alembic`.

Covers state extraction, CHECK constraint rendering, metadata attachment,
and end-to-end Alembic autogenerate against an in-memory SQLite database.
"""

import sqlalchemy
from alembic.autogenerate import produce_migrations
from alembic.migration import MigrationContext
from alembic.operations import ops
from sqlalchemy import CheckConstraint
from sqlalchemy.orm import declarative_base

from sqlalchemy_fsm import FSMField, transition
from sqlalchemy_fsm.extras.alembic import (
    _normalize_sqltext,
    attach_fsm_constraints,
    collect_states,
    fsm_check_name,
    register_autogenerate_comparator,
    render_check_constraint,
)

AlembicBase = declarative_base()


class Article(AlembicBase):
    __tablename__ = "AlembicArticle"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = sqlalchemy.Column(FSMField, nullable=False, default="draft")

    @transition(source="draft", target="published")
    def publish(self):
        pass

    @transition(source=["draft", "published"], target="archived")
    def archive(self):
        pass

    @transition(source="*", target="deleted")
    def delete(self):
        pass


@transition(target="republished")
class _Republish:
    @transition(source="archived")
    def from_archived(self, instance):
        pass


class WithClassGrouped(AlembicBase):
    __tablename__ = "AlembicWithClassGrouped"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = sqlalchemy.Column(FSMField, nullable=False, default="archived")

    republish = _Republish


class NoFsm(AlembicBase):
    __tablename__ = "AlembicNoFsm"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    name = sqlalchemy.Column(sqlalchemy.String)


class TestCollectStates:
    def test_unions_sources_and_targets(self):
        states = collect_states(Article)
        assert states == {"draft", "published", "archived", "deleted"}

    def test_excludes_wildcard(self):
        states = collect_states(Article)
        assert "*" not in states

    def test_includes_class_grouped_sub_handlers(self):
        states = collect_states(WithClassGrouped)
        assert states == {"archived", "republished"}


class TestRenderCheckConstraint:
    def test_returns_check_constraint(self):
        c = render_check_constraint(Article)
        assert isinstance(c, CheckConstraint)

    def test_has_deterministic_name(self):
        c = render_check_constraint(Article)
        assert c.name == fsm_check_name("AlembicArticle", "state")

    def test_sql_lists_all_states_alphabetically(self):
        c = render_check_constraint(Article)
        sql = str(c.sqltext)
        # Sort guarantee makes the rendered SQL stable across runs.
        assert "'archived'" in sql
        assert "'deleted'" in sql
        assert "'draft'" in sql
        assert "'published'" in sql


class TestAttachFsmConstraints:
    def _fresh_base(self):
        """Build an isolated Base+model so attachment is unit-testable."""
        Base = declarative_base()

        class M(Base):
            __tablename__ = "attach_target"
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
            state = sqlalchemy.Column(FSMField, nullable=False)

            @transition(source="a", target="b")
            def go(self):
                pass

        return Base, M

    def test_attaches_to_fsm_tables(self):
        Base, model = self._fresh_base()
        attached = attach_fsm_constraints(Base)
        assert len(attached) == 1
        names = {c.name for c in model.__table__.constraints}
        assert fsm_check_name("attach_target", "state") in names

    def test_skips_non_fsm_tables(self):
        attached = attach_fsm_constraints(AlembicBase)
        attached_tables = {
            c.table.name  # type: ignore[attr-defined]
            for c in attached
        }
        assert "AlembicNoFsm" not in attached_tables

    def test_accepts_iterable_of_classes(self):
        _, model = self._fresh_base()
        attached = attach_fsm_constraints([model])
        assert len(attached) == 1

    def test_idempotent(self):
        Base, model = self._fresh_base()
        attach_fsm_constraints(Base)
        before = sum(
            1 for c in model.__table__.constraints if isinstance(c, CheckConstraint)
        )
        attach_fsm_constraints(Base)
        after = sum(
            1 for c in model.__table__.constraints if isinstance(c, CheckConstraint)
        )
        assert before == after


class TestNormalizeSqltext:
    def test_collapses_whitespace_and_case(self):
        assert _normalize_sqltext("STATE  IN ('a', 'b')") == _normalize_sqltext(
            "state IN ('a', 'b')"
        )


class TestAutogenerateEndToEnd:
    """Spin up an in-memory SQLite, simulate model→DB drift, and verify
    Alembic autogenerate (with our comparator) produces the right ops."""

    def _build_world(self):
        """Returns (engine, metadata, model_class) with the comparator registered."""
        register_autogenerate_comparator()
        engine = sqlalchemy.create_engine("sqlite:///:memory:")
        # Use a fresh Base so other test models don't interfere.
        Base = declarative_base()
        md = Base.metadata

        class Doc(Base):
            __tablename__ = "AutogenDoc"
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
            state = sqlalchemy.Column(FSMField, nullable=False, default="draft")

            @transition(source="draft", target="published")
            def publish(self):
                pass

        attach_fsm_constraints(Base)
        return engine, md, Doc, Base

    def test_initial_create_includes_check_constraint(self):
        engine, md, _, _ = self._build_world()

        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts={"target_metadata": md})
            script = produce_migrations(ctx, md)

        # CreateTable op should carry our CHECK on the new table.
        assert script.upgrade_ops is not None
        create_ops = [
            op
            for op in _flatten(script.upgrade_ops.ops)
            if isinstance(op, ops.CreateTableOp) and op.table_name == "AutogenDoc"
        ]
        assert len(create_ops) == 1
        # CreateTableOp stores positional columns + constraints in `.columns`.
        constraint_names = [getattr(c, "name", None) for c in create_ops[0].columns]
        assert fsm_check_name("AutogenDoc", "state") in constraint_names

    def test_drift_detected_after_state_added(self):
        """Create the table from V1 of the model, then run autogenerate
        against a V2 model that has an extra state — expect drop+add ops
        for the FSM CHECK."""
        register_autogenerate_comparator()
        engine = sqlalchemy.create_engine("sqlite:///:memory:")

        BaseV1 = declarative_base()

        class DocV1(BaseV1):
            __tablename__ = "DriftDoc"
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
            state = sqlalchemy.Column(FSMField, nullable=False, default="draft")

            @transition(source="draft", target="published")
            def publish(self):
                pass

        attach_fsm_constraints(BaseV1)
        BaseV1.metadata.create_all(engine)

        # V2: same table, extra state via a new transition.
        BaseV2 = declarative_base()

        class DocV2(BaseV2):
            __tablename__ = "DriftDoc"
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
            state = sqlalchemy.Column(FSMField, nullable=False, default="draft")

            @transition(source="draft", target="published")
            def publish(self):
                pass

            @transition(source="published", target="archived")
            def archive(self):
                pass

        attach_fsm_constraints(BaseV2)

        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            script = produce_migrations(ctx, BaseV2.metadata)

        assert script.upgrade_ops is not None
        op_list = list(_flatten(script.upgrade_ops.ops))
        check_name = fsm_check_name("DriftDoc", "state")
        drop_ops = [
            o
            for o in op_list
            if isinstance(o, ops.DropConstraintOp) and o.constraint_name == check_name
        ]
        add_ops = [
            o
            for o in op_list
            if isinstance(o, ops.AddConstraintOp)
            and getattr(o.to_constraint(), "name", None) == check_name
        ]
        assert drop_ops, f"expected DropConstraintOp for {check_name!r}; got {op_list}"
        assert add_ops, f"expected AddConstraintOp for {check_name!r}; got {op_list}"


def _flatten(ops_iter):
    """Walk a tree of (Modify)TableOps yielding leaf ops."""
    for op in ops_iter:
        yield op
        sub = getattr(op, "ops", None)
        if sub:
            yield from _flatten(sub)
