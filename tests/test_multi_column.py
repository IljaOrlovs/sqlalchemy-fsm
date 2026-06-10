"""Tests for `FSMColumn` and multiple FSM columns per model."""

from __future__ import annotations

import pytest
import sqlalchemy

from sqlalchemy_fsm import FSMColumn, FSMField, transition
from sqlalchemy_fsm.exc import (
    InvalidSourceStateError,
    MultipleFSMColumnsError,
    SetupError,
)

from .conftest import Base


class BlogPost(Base):
    __tablename__ = "blog_post_multi"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = FSMColumn["draft", "published", "archived"](nullable=False, default="draft")
    ad_mode = FSMColumn["no-ads", "inline-ads", "images", "popups"](
        nullable=False, default="popups"
    )

    @state.transition(source="draft", target="published")
    def publish(self):
        pass

    @state.transition(source=["draft", "published"], target="archived")
    def archive(self):
        pass

    @ad_mode.transition(source=["popups"], target="images")
    def image_ads(self):
        pass

    @ad_mode.transition(source="*", target="inline-ads")
    def inline_ads(self):
        pass

    @ad_mode.transition(source="*", target="no-ads")
    def no_ads(self):
        pass


class TestFSMColumnBasic:
    def test_column_construction(self):
        col = BlogPost.__table__.c.state
        assert isinstance(col.type, FSMField)
        assert col.type._allowed_states == frozenset({"draft", "published", "archived"})

    def test_initial_states(self):
        post = BlogPost()
        post.state = "draft"
        post.ad_mode = "popups"
        assert post.state == "draft"
        assert post.ad_mode == "popups"

    def test_state_transition_writes_only_state(self):
        post = BlogPost()
        post.state = "draft"
        post.ad_mode = "popups"
        post.publish.set()
        assert post.state == "published"
        assert post.ad_mode == "popups"  # untouched

    def test_ad_mode_transition_writes_only_ad_mode(self):
        post = BlogPost()
        post.state = "draft"
        post.ad_mode = "popups"
        post.image_ads.set()
        assert post.state == "draft"  # untouched
        assert post.ad_mode == "images"

    def test_wildcard_source_per_column(self):
        post = BlogPost()
        post.state = "published"
        post.ad_mode = "no-ads"
        post.inline_ads.set()
        assert post.ad_mode == "inline-ads"
        assert post.state == "published"

    def test_archive_from_draft(self):
        post = BlogPost()
        post.state = "draft"
        post.ad_mode = "popups"
        post.archive.set()
        assert post.state == "archived"

    def test_invalid_source_rejected(self):
        post = BlogPost()
        post.state = "archived"
        post.ad_mode = "popups"
        with pytest.raises(InvalidSourceStateError):
            post.publish.set()

    def test_query_filter_targets_correct_column(self, session):
        a = BlogPost()
        a.state = "draft"
        a.ad_mode = "popups"
        b = BlogPost()
        b.state = "published"
        b.ad_mode = "images"
        session.add_all([a, b])
        session.commit()

        published = session.scalars(
            sqlalchemy.select(BlogPost).where(BlogPost.publish())
        ).all()
        assert [p.state for p in published] == ["published"]

        images = session.scalars(
            sqlalchemy.select(BlogPost).where(BlogPost.image_ads())
        ).all()
        assert [p.ad_mode for p in images] == ["images"]


class TestEagerStateValidation:
    def test_unknown_target_rejected_at_decoration(self):
        col = FSMColumn["a", "b"](nullable=False, default="a")
        with pytest.raises(SetupError) as err:

            @col.transition(source="a", target="bogus")
            def go(self):
                pass

        assert "bogus" in str(err.value)

    def test_unknown_source_rejected_at_decoration(self):
        col = FSMColumn["a", "b"](nullable=False, default="a")
        with pytest.raises(SetupError) as err:

            @col.transition(source="nope", target="b")
            def go(self):
                pass

        assert "nope" in str(err.value)

    def test_unknown_source_in_list_rejected(self):
        col = FSMColumn["a", "b"](nullable=False, default="a")
        with pytest.raises(SetupError):

            @col.transition(source=["a", "ghost"], target="b")
            def go(self):
                pass

    def test_wildcard_and_none_allowed(self):
        col = FSMColumn["a", "b"](nullable=False, default="a")

        @col.transition(source="*", target="b")
        def from_anywhere(self):
            pass

        @col.transition(source=None, target="a")
        def from_null(self):
            pass

    def test_untyped_column_skips_validation(self):
        col = FSMColumn()  # no subscript → no allowed_states

        # Any state name is fine.
        @col.transition(source="anywhere", target="anywhere_else")
        def go(self):
            pass


class TestLegacyTransitionRaisesOnMultipleColumns:
    def test_module_level_transition_on_multi_column_model_raises(self):
        # A model with two FSM columns where a bare `@transition` is used
        # should raise `MultipleFSMColumnsError` at first `__get__`.
        from sqlalchemy.orm import DeclarativeBase

        class Base2(DeclarativeBase):
            pass

        class TwoColModel(Base2):
            __tablename__ = "two_col_legacy"
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
            a = sqlalchemy.Column(FSMField, default="x")
            b = sqlalchemy.Column(FSMField, default="y")

            @transition(source="x", target="y")
            def move(self):
                pass

        with pytest.raises(MultipleFSMColumnsError):
            TwoColModel().move.set()


class TestValidatorPerColumn:
    def test_each_column_validated_independently(self):
        # state-graph problem on one column should mention that column.
        from sqlalchemy.orm import DeclarativeBase

        class Base2(DeclarativeBase):
            pass

        class Broken(Base2):
            __tablename__ = "broken_multi_col"
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
            state = FSMColumn["a", "b"](nullable=False, default="a")
            mode = FSMColumn["x", "y"](nullable=False, default="x")

            @state.transition(source="a", target="b")
            def go(self):
                pass

            # `mode` has no transitions → "y" is unused & unreachable

        from sqlalchemy_fsm import validate_fsm

        with pytest.raises(SetupError) as err:
            validate_fsm(Broken)
        assert "mode" in str(err.value)


class TestGraphPerColumn:
    def test_mermaid_filters_by_column(self):
        from sqlalchemy_fsm.extras.graph import to_mermaid

        state_graph = to_mermaid(BlogPost, column=BlogPost.__table__.c.state)
        assert "published" in state_graph
        assert "images" not in state_graph

        ad_graph = to_mermaid(BlogPost, column=BlogPost.__table__.c.ad_mode)
        assert "images" in ad_graph
        assert "published" not in ad_graph


class TestAlembicConstraintsPerColumn:
    def test_constraint_per_column(self):
        from sqlalchemy_fsm.extras.alembic import (
            fsm_check_name,
            render_check_constraints,
        )

        constraints = render_check_constraints(BlogPost)
        names = {c.name for c in constraints}
        table = BlogPost.__tablename__
        assert fsm_check_name(table, "state") in names
        assert fsm_check_name(table, "ad_mode") in names
        assert len(constraints) == 2
