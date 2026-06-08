import pytest
import sqlalchemy

from sqlalchemy_fsm import FSMField, transition
from sqlalchemy_fsm.exc import (
    PreconditionError,
)

from .conftest import Base


def condition_func(instance):
    return True


class BlogPostWithConditions(Base):
    __tablename__ = "BlogPostWithConditions"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = sqlalchemy.Column(FSMField)

    def __init__(self, *args, **kwargs):
        self.state = "new"
        super().__init__(*args, **kwargs)

    def model_condition(self):
        return True

    def unmet_condition(self):
        return False

    @transition(
        source="new", target="published", conditions=[condition_func, model_condition]
    )
    def published(self):
        pass

    @transition(
        source="published",
        target="destroyed",
        conditions=[condition_func, unmet_condition],
    )
    def destroyed(self):
        pass


class TestConditional:
    @pytest.fixture
    def model(self):
        return BlogPostWithConditions()

    def test_initial_state(self, model):
        assert model.state == "new"

    def test_known_transition_should_succeed(self, model):
        assert model.published.can_proceed()
        model.published.set()
        assert model.state == "published"

    def test_unmet_condition(self, model):
        model.published.set()
        assert model.state == "published"
        assert not model.destroyed.can_proceed()
        with pytest.raises(PreconditionError):
            model.destroyed.set()
        assert model.state == "published"
