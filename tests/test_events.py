import pytest
import sqlalchemy
import sqlalchemy.event

import sqlalchemy_fsm

from .conftest import Base


class EventModel(Base):
    __tablename__ = "event_model"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = sqlalchemy.Column(sqlalchemy_fsm.FSMField)

    def __init__(self, *args, **kwargs):
        self.state = "new"
        super(EventModel, self).__init__(*args, **kwargs)

    @sqlalchemy_fsm.transition(source="*", target="state_a")
    def state_a(self):
        pass

    @sqlalchemy_fsm.transition(source="*", target="state_b")
    def state_b(self):
        pass


class TestEventListener(object):
    @pytest.fixture
    def model(self):
        return EventModel()

    @pytest.mark.parametrize(
        "event_name",
        [
            "before_state_change",
            "after_state_change",
        ],
    )
    def test_events(self, model, event_name):

        listener_result = []

        def on_update(instance, source, target):
            listener_result.append((source, target))

        sqlalchemy.event.listen(EventModel, event_name, on_update)

        expected_result = []
        assert listener_result == expected_result

        for handle_name in ("state_a", "state_b", "state_a", "state_a", "state_b"):
            expected_result.append((model.state, handle_name))
            if handle_name == "state_a":
                handle = model.state_a
            else:
                handle = model.state_b
            handle.set()
            assert listener_result == expected_result

        # Remove the listener & check that it had an effect
        sqlalchemy.event.remove(EventModel, event_name, on_update)
        # Call the state handle & ensure that listener had not been called.
        model.state_a.set()
        assert listener_result == expected_result

    def test_standard_sqlalchemy_events_still_work(self, model, session):
        state_log = []
        insert_log = []

        @sqlalchemy.event.listens_for(EventModel, "after_state_change")
        def after_state_change(instance, source, target):
            state_log.append(target)

        @sqlalchemy.event.listens_for(EventModel, "before_insert")
        def before_insert(mapper, connection, target):
            insert_log.append(42)

        assert not state_log
        assert not insert_log

        model.state_a.set()
        assert len(state_log) == 1
        assert len(insert_log) == 0

        model.state_b.set()
        assert len(state_log) == 2
        assert len(insert_log) == 0

        session.add(model)
        session.flush()

        assert len(state_log) == 2
        assert len(insert_log) == 1

        model.state_b.set()
        assert len(state_log) == 3
        assert len(insert_log) == 1


class TransitionClassEventModel(Base):
    __tablename__ = "transition_class_event_model"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = sqlalchemy.Column(sqlalchemy_fsm.FSMField)
    side_effect = sqlalchemy.Column(sqlalchemy.String)

    def __init__(self, *args, **kwargs):
        self.state = "new"
        super(TransitionClassEventModel, self).__init__(*args, **kwargs)

    @sqlalchemy_fsm.transition(source="*", target="state_a")
    def state_a(self):
        pass

    @sqlalchemy_fsm.transition(source="*", target="state_b")
    def state_b(self):
        pass

    @sqlalchemy_fsm.transition(target="state_class")
    class StateClass(object):
        @sqlalchemy_fsm.transition(source="state_a")
        def from_a(self, instance):
            instance.side_effect = "from_a"

        @sqlalchemy_fsm.transition(source="state_b")
        def from_b(self, instance):
            instance.side_effect = "from_b"


class TestTransitionClassEvents(object):
    @pytest.fixture
    def model(self):
        return TransitionClassEventModel()

    @pytest.mark.parametrize(
        "event_name",
        [
            "before_state_change",
            "after_state_change",
        ],
    )
    def test_events(self, model, event_name):

        listener_result = []

        @sqlalchemy.event.listens_for(TransitionClassEventModel, event_name)
        def on_update(instance, source, target):
            listener_result.append(target)

        expected_result = []
        assert listener_result == expected_result

        for handle_name in ("state_a", "state_b", "state_a", "state_a", "state_b"):
            expected_result.append(handle_name)
            if handle_name == "state_a":
                handle = model.state_a
            else:
                handle = model.state_b
            handle.set()
            assert listener_result == expected_result
            model.StateClass.set()

            if handle_name == "state_a":
                expected_side = "from_a"
            else:
                expected_side = "from_b"

            expected_result.append("state_class")

            assert model.side_effect == expected_side
            assert listener_result == expected_result

        # Remove the listener & check that it had an effect
        sqlalchemy.event.remove(TransitionClassEventModel, event_name, on_update)
        # Call the state handle & ensure that listener had not been called.
        model.state_a.set()
        assert listener_result == expected_result


class TestEventsLeakage(object):
    """Ensure that multiple FSM models do not mix their events up."""

    @pytest.mark.parametrize(
        "event_name",
        [
            "before_state_change",
            "after_state_change",
        ],
    )
    def test_leakage(self, event_name):
        event_model = EventModel()
        tr_cls_model = TransitionClassEventModel()

        event_result = []
        tr_cls_result = []
        joint_result = []

        @sqlalchemy.event.listens_for(EventModel, event_name)
        def on_evt_update(instance, source, target):
            event_result.append(target)

        @sqlalchemy.event.listens_for(TransitionClassEventModel, event_name)
        def on_tr_update(instance, source, target):
            tr_cls_result.append(target)

        @sqlalchemy.event.listens_for(TransitionClassEventModel, event_name)
        @sqlalchemy.event.listens_for(EventModel, event_name)
        def on_both_update(instance, source, target):
            joint_result.append(target)

        assert len(event_result) == 0
        assert len(tr_cls_result) == 0
        assert len(joint_result) == 0

        event_model.state_a.set()
        assert len(event_result) == 1
        assert len(tr_cls_result) == 0
        assert len(joint_result) == 1

        event_model.state_b.set()
        assert len(event_result) == 2
        assert len(tr_cls_result) == 0
        assert len(joint_result) == 2

        tr_cls_model.state_a.set()
        assert len(event_result) == 2
        assert len(tr_cls_result) == 1
        assert len(joint_result) == 3

        tr_cls_model.state_a.set()
        assert len(event_result) == 2
        assert len(tr_cls_result) == 2
        assert len(joint_result) == 4
