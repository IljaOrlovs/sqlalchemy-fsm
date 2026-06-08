"""Explicit border-case tests.

Locks in current behavior for surprising or under-specified inputs. Some
tests document quirks (e.g. whitespace-only state names being accepted) so
that any future change is intentional, not silent.
"""

# `Column == "..."` returns SA's ColumnElement[bool] at the type level but
# evaluates as a normal bool at runtime — pervasive in these assertions.
# pyright: reportGeneralTypeIssues=false

import pytest
import sqlalchemy

from sqlalchemy_fsm import FSMField, bound, transition
from sqlalchemy_fsm.exc import (
    InvalidSourceStateError,
    PreconditionError,
    SetupError,
)
from sqlalchemy_fsm.meta import FSMMeta
from sqlalchemy_fsm.util import is_valid_fsm_state

from .conftest import Base

# --- Predicate quirks -------------------------------------------------------


def test_whitespace_only_state_is_currently_accepted():
    """Documents existing behavior: the predicate only checks for non-empty
    string, so `" "` and `"\\t"` pass. If we ever tighten this, update the
    test and bump a version."""
    assert is_valid_fsm_state(" ")
    assert is_valid_fsm_state("\t")
    assert is_valid_fsm_state("\n")


def test_very_long_state_name_is_accepted():
    long_name = "x" * 10_000
    meta = FSMMeta(long_name, "target", (), (), bound.BoundFSMFunction)
    assert long_name in meta.sources


def test_unicode_state_name_is_accepted():
    meta = FSMMeta("état_initial", "état_final", (), (), bound.BoundFSMFunction)
    assert meta.target == "état_final"
    assert "état_initial" in meta.sources


# --- FSMMeta source iterable edge cases ------------------------------------


def test_empty_source_iterable_yields_empty_frozenset():
    """An empty source list produces an FSMMeta with no sources. The
    transition is effectively unreachable — documenting that the library
    does not reject this at construction time."""
    meta = FSMMeta([], "target", (), (), bound.BoundFSMFunction)
    assert meta.sources == frozenset()


def test_generator_source_is_consumed_correctly():
    def gen():
        yield "a"
        yield "b"
        yield "c"

    meta = FSMMeta(gen(), "target", (), (), bound.BoundFSMFunction)
    assert meta.sources == frozenset({"a", "b", "c"})


def test_tuple_source_is_accepted():
    meta = FSMMeta(("a", "b"), "target", (), (), bound.BoundFSMFunction)
    assert meta.sources == frozenset({"a", "b"})


def test_set_source_is_accepted():
    meta = FSMMeta({"a", "b"}, "target", (), (), bound.BoundFSMFunction)
    assert meta.sources == frozenset({"a", "b"})


def test_mixing_star_with_concrete_states_in_source_is_allowed():
    """`"*"` mixed with concrete states is currently accepted at meta
    construction. Documenting behavior."""
    meta = FSMMeta(["*", "concrete"], "target", (), (), bound.BoundFSMFunction)
    assert "*" in meta.sources
    assert "concrete" in meta.sources


# --- FSMMeta target edge cases ---------------------------------------------


def test_target_empty_string_rejected():
    with pytest.raises(NotImplementedError):
        FSMMeta("*", "", (), (), bound.BoundFSMFunction)


def test_target_none_is_accepted_and_stored():
    """target=None creates an FSMMeta with no fixed target — used for
    classes-as-transitions which dispatch targets per source."""
    meta = FSMMeta("*", None, (), (), bound.BoundFSMFunction)
    assert meta.target is None


# --- Conditions edge cases --------------------------------------------------


def test_empty_conditions_tuple():
    meta = FSMMeta("*", "target", (), (), bound.BoundFSMFunction)
    assert meta.conditions == ()


def test_conditions_as_generator_is_consumed():
    def cond_gen():
        yield lambda inst: True
        yield lambda inst: False

    meta = FSMMeta("*", "target", cond_gen(), (), bound.BoundFSMFunction)
    assert len(meta.conditions) == 2


# --- Live FSM end-to-end edge tests ----------------------------------------


class BorderCaseModel(Base):
    __tablename__ = "edge_border_model"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    status = sqlalchemy.Column(FSMField)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.status is None:
            self.status = "new"

    @transition(source="new", target="active")
    def activate(self):
        pass

    @transition(source="active", target="done")
    def finish(self):
        pass

    @transition(source=["new", "active"], target="cancelled")
    def cancel(self):
        pass


def test_invalid_transition_from_terminal_state(session):
    model = BorderCaseModel()
    model.activate.set()
    model.finish.set()
    assert model.status == "done"
    with pytest.raises(InvalidSourceStateError):
        model.activate.set()


def test_can_proceed_returns_false_after_invalid_transition_attempt(session):
    model = BorderCaseModel()
    model.activate.set()
    model.finish.set()
    assert model.activate.can_proceed() is False
    assert model.cancel.can_proceed() is False


def test_multi_source_transition_works_from_each_source(session):
    m1 = BorderCaseModel()
    assert m1.cancel.can_proceed()
    m1.cancel.set()
    assert m1.status == "cancelled"

    m2 = BorderCaseModel()
    m2.activate.set()
    assert m2.cancel.can_proceed()
    m2.cancel.set()
    assert m2.status == "cancelled"


def test_chained_transitions_preserve_invariants(session):
    """Drive a model through every valid path and assert state at each step."""
    model = BorderCaseModel()
    assert model.status == "new"
    assert model.activate.can_proceed()
    assert not model.finish.can_proceed()

    model.activate.set()
    assert model.status == "active"
    assert not model.activate.can_proceed()
    assert model.finish.can_proceed()
    assert model.cancel.can_proceed()

    model.finish.set()
    assert model.status == "done"
    assert not model.finish.can_proceed()
    assert not model.cancel.can_proceed()


# --- Conditions failure surface --------------------------------------------


class ConditionalModel(Base):
    __tablename__ = "edge_conditional_model"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    status = sqlalchemy.Column(FSMField)
    allow = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.status is None:
            self.status = "ready"

    @transition(source="ready", target="go", conditions=[lambda self: self.allow])
    def proceed(self):
        pass


def test_precondition_failure_raises(session):
    model = ConditionalModel()
    model.allow = False
    with pytest.raises(PreconditionError):
        model.proceed.set()
    assert model.status == "ready"


def test_precondition_success_transitions(session):
    model = ConditionalModel()
    model.allow = True
    model.proceed.set()
    assert model.status == "go"


# --- Misconfiguration detection --------------------------------------------


def test_model_without_fsm_field_fails_at_use_time(session):
    class NoFsm(Base):
        __tablename__ = "edge_no_fsm"
        id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)

        @transition(source="*", target="anywhere")
        def go(self):
            pass

    Base.metadata.create_all(session.bind)
    with pytest.raises(SetupError, match="No FSMField"):
        NoFsm().go.set()


def test_model_with_two_fsm_fields_fails(session):
    class TwoFsm(Base):
        __tablename__ = "edge_two_fsm"
        id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
        state_a = sqlalchemy.Column(FSMField, default="a")
        state_b = sqlalchemy.Column(FSMField, default="b")

        @transition(source="*", target="x")
        def go(self):
            pass

    Base.metadata.create_all(session.bind)
    with pytest.raises(SetupError):
        TwoFsm().go.set()
