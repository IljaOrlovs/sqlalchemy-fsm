import pytest
import sqlalchemy

from sqlalchemy_fsm import FSMField, exc, transition

from .conftest import Base


class NotFsm(Base):
    __tablename__ = "NotFsm"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)

    @transition(source="*", target="blah")
    def change_state(self):
        pass

    def not_transition(self):
        pass


def test_not_fsm():
    with pytest.raises(exc.SetupError) as err:
        NotFsm().change_state.set()
    assert "No FSMField found in model" in str(err)


def test_not_transition():
    with pytest.raises(AttributeError):
        NotFsm.not_transition.can_proceed()  # pyright: ignore[reportFunctionMemberAccess]


class TooMuchFsm(Base):
    __tablename__ = "TooMuchFsm"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state1 = sqlalchemy.Column(FSMField)
    state2 = sqlalchemy.Column(FSMField)

    @transition(source="*", target="blah")
    def change_state(self):
        pass


def test_too_much_fsm():
    with pytest.raises(exc.SetupError) as err:
        TooMuchFsm().change_state.set()
    assert "2 FSMField columns" in str(err)
    assert "FSMColumn.transition" in str(err)


def test_transition_accepts_callable_instance():
    """A callable class instance is a valid handler (treated like a function)."""

    class MyCallable:
        def __call__(self, instance):
            instance.side_effect = "called"

    wrapper = transition(source="*", target="blah")
    # Should NOT raise — callable instances are valid handlers.
    fsm_t = wrapper(MyCallable())
    assert fsm_t.meta.target == "blah"


def test_transition_rejects_non_callable():
    """Non-callables (ints, strings, etc.) are a setup error."""
    wrapper = transition(source="*", target="blah")
    with pytest.raises(exc.SetupError, match="expects a callable"):
        wrapper(42)  # pyright: ignore[reportArgumentType, reportCallIssue]


def test_transition_raises_on_invalid_state():
    with pytest.raises(NotImplementedError) as err:

        @transition(source=42, target="blah")  # pyright: ignore[reportArgumentType, reportCallIssue]
        def func1():
            pass

    assert "42" in str(err)

    with pytest.raises(NotImplementedError) as err:

        @transition(source="*", target=42)  # pyright: ignore[reportArgumentType, reportCallIssue]
        def func2():
            pass

    assert "42" in str(err)

    with pytest.raises(NotImplementedError) as err:

        @transition(source=["str", 42], target="blah")  # pyright: ignore[reportArgumentType, reportCallIssue]
        def func3():
            pass

    assert "42" in str(err)


def one_arg_condition():
    def one_arg_condition(instance, arg1):
        return True

    return one_arg_condition


class MisconfiguredTransitions(Base):
    __tablename__ = "MisconfiguredTransitions"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = sqlalchemy.Column(FSMField)

    @transition(source="*", target="blah", conditions=[one_arg_condition()])
    def change_state(self):
        """Condition accepts one arg, state handler doesn't -> exception."""

    @transition(source="*", target="blah")
    class MultiHandlerTransition:
        """The system won't know which transition{1,2} handler to chose."""

        @transition()
        def transition1(self, instance):
            pass

        @transition()
        def transition2(self, instance):
            pass

    @transition(source="*", target="blah")
    class IncompatibleTargets:
        """The system won't know which transition{1,2} handler to chose."""

        @transition(target="not-blah")
        def transition1(self, instance):
            pass

    @transition(source=["src1", "src2"], target="blah")
    class IncompatibleSources:
        """The system won't know which transition{1,2} handler to chose."""

        @transition(source=["src3", "src4"])
        def transition1(self, instance):
            pass

    @transition(source="*", target="blah")
    class NoConflictDueToPreconditionArgCount:
        @transition(conditions=[lambda self, instance, arg1: True])
        def change_state(self, instance, arg1):
            pass

        @transition()
        def no_arg_condition(self, instance):
            pass


class TestMisconfiguredTransitions:
    @pytest.fixture
    def model(self):
        return MisconfiguredTransitions()

    def test_misconfigured_transitions(self, model):
        with pytest.raises(exc.SetupError) as err:
            model.change_state.set(42)
        assert "Mismatch between args accepted" in str(err)

    def test_multi_transition_handlers(self, model):
        with pytest.raises(exc.SetupError) as err:
            model.MultiHandlerTransition.set()
        assert "Can transition with multiple handlers" in str(err)

    def test_incompatible_targets(self, model):
        with pytest.raises(exc.SetupError) as err:
            model.IncompatibleTargets.set()
        assert "are not compatible" in str(err)

    def test_incompatable_sources(self, model):
        with pytest.raises(exc.SetupError) as err:
            model.IncompatibleSources.set()
        assert "are not compatible" in str(err)

    def test_no_conflict_due_to_precondition_arg_count(self, model):
        assert model.NoConflictDueToPreconditionArgCount.can_proceed()


def test_unexpected_is__type(session):
    model = MisconfiguredTransitions()
    session.add(model)
    session.commit()
    with pytest.warns(UserWarning, match="Unexpected is_ argument") as warn:
        result = session.scalars(
            sqlalchemy.select(MisconfiguredTransitions).where(
                MisconfiguredTransitions.change_state.is_("hello world")
            )
        ).all()
    assert not result
    assert "Unexpected is_ argument: 'hello world'" in str(warn.list[0].message)
