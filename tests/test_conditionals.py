import unittest

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
        super(BlogPostWithConditions, self).__init__(*args, **kwargs)

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


class TestConditional(unittest.TestCase):
    def setUp(self):
        self.model = BlogPostWithConditions()

    def test_initial_staet(self):
        self.assertEqual(self.model.state, "new")

    def test_known_transition_should_succeed(self):
        self.assertTrue(self.model.published.can_proceed())
        self.model.published.set()
        self.assertEqual(self.model.state, "published")

    def test_unmet_condition(self):
        self.model.published.set()
        self.assertEqual(self.model.state, "published")
        self.assertFalse(self.model.destroyed.can_proceed())
        self.assertRaises(PreconditionError, self.model.destroyed.set)
        self.assertEqual(self.model.state, "published")
