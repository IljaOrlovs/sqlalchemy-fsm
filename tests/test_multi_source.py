import pytest
import sqlalchemy

from sqlalchemy_fsm import FSMField, transition
from sqlalchemy_fsm.exc import (
    InvalidSourceStateError,
    PreconditionError,
)

from .conftest import Base


def val_eq_condition(expected_value):
    def bound_val_eq_condition(self, instance, actual_value):
        return expected_value == actual_value

    return bound_val_eq_condition


def val_contains_condition(expected_values):
    def bound_val_contains_condition(self, instance, actual_value):
        return actual_value in expected_values

    return bound_val_contains_condition


def three_argument_condition(expected1, expected2, expected3):
    def bound_three_argument_condition(self, instance, arg1, arg2, arg3):
        return (arg1, arg2, arg3) == (expected1, expected2, expected3)

    return bound_three_argument_condition


class MultiSourceBlogPost(Base):
    __tablename__ = "MultiSourceBlogPost"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = sqlalchemy.Column(FSMField)
    side_effect = sqlalchemy.Column(sqlalchemy.String)

    def __init__(self, *args, **kwargs):
        self.state = "new"
        self.side_effect = "default"
        super().__init__(*args, **kwargs)

    @transition(source="new", target="hidden")
    def hide(self):
        self.side_effect = "did_hide"

    @transition(source="*", target="deleted")
    def delete(self):
        self.side_effect = "deleted"

    @transition(target="published", conditions=[val_contains_condition([1, 2])])
    class publish:  # noqa: N801
        @transition(source="new", conditions=[val_eq_condition(1)])
        def do_one(self, instance, value):
            instance.side_effect = "did_one"

        @transition(source="new", conditions=[val_contains_condition([2, 42])])
        def do_two(self, instance, value):
            instance.side_effect = "did_two"

        @transition(source="hidden")
        def do_unhide(self, instance, value):
            instance.side_effect = f"did_unhide: {value}"

        @transition(source="published")
        def do_publish_loop(self, instance, value):
            instance.side_effect = f"do_publish_loop: {value}"

    @transition(target="published", source=["new", "something"])
    class noPreFilterPublish:  # noqa: N801
        @transition(source="*", conditions=[three_argument_condition(1, 2, 3)])
        def do_three_arg_mk1(self, instance, val1, val2, val3):
            instance.side_effect = f"did_three_arg_mk1::{[val1, val2, val3]}"

        @transition(source="new", conditions=[three_argument_condition("str", -1, 42)])
        def do_three_arg_mk2(self, instance, val1, val2, val3):
            instance.side_effect = f"did_three_arg_mk2::{[val1, val2, val3]}"


class TestMultiSourceBlogPost:
    @pytest.fixture
    def model(self):
        return MultiSourceBlogPost()

    def test_transition_one(self, model):
        assert model.publish.can_proceed(1)

        model.publish.set(1)
        assert model.state == "published"
        assert model.side_effect == "did_one"

    def test_transition_two(self, model):
        assert model.publish.can_proceed(2)

        model.publish.set(2)
        assert model.state == "published"
        assert model.side_effect == "did_two"

    def test_three_arg_transition_mk1(self, model):
        assert model.noPreFilterPublish.can_proceed(1, 2, 3)
        model.noPreFilterPublish.set(1, 2, 3)
        assert model.state == "published"
        assert model.side_effect == "did_three_arg_mk1::[1, 2, 3]"

    def test_three_arg_transition_mk2(self, model):
        assert model.noPreFilterPublish.can_proceed("str", -1, 42)
        model.noPreFilterPublish.set("str", -1, 42)
        assert model.state == "published"
        assert model.side_effect == "did_three_arg_mk2::['str', -1, 42]"

    def unable_to_proceed_with_invalid_kwargs(self, model):
        assert not model.noPreFilterPublish.can_proceed("str", -1, tomato="potato")

    def test_transition_two_incorrect_arg(self, model):
        # Transition should be rejected because of
        # top-level `val_contains_condition([1,2])` constraint
        with pytest.raises(PreconditionError) as err:
            model.publish.set(42)
        assert "Preconditions are not satisfied" in str(err)
        assert model.state == "new"
        assert model.side_effect == "default"

        # Verify that the exception can still be avoided
        # with can_proceed() call
        assert not model.publish.can_proceed(42)
        assert not model.publish.can_proceed(4242)

    def test_hide(self, model):
        model.hide.set()
        assert model.state == "hidden"
        assert model.side_effect == "did_hide"

        model.publish.set(2)
        assert model.state == "published"
        assert model.side_effect == "did_unhide: 2"

    def test_publish_loop(self, model):
        model.publish.set(1)
        assert model.state == "published"
        assert model.side_effect == "did_one"

        for arg in (1, 2, 1, 1, 2):
            model.publish.set(arg)
            assert model.state == "published"
            assert model.side_effect == f"do_publish_loop: {arg}"

    def test_delete_new(self, model):
        model.delete.set()
        assert model.state == "deleted"

        # Can not switch from deleted to published
        assert not model.publish.can_proceed(2)
        with pytest.raises(InvalidSourceStateError) as err:
            model.publish.set(2)
        assert "Unable to switch" in str(err)
        assert model.state == "deleted"
