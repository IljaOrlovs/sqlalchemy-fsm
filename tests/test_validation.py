"""Tests for `FSMField[...]` literal syntax and the startup validator."""

import pytest
import sqlalchemy
from sqlalchemy.orm import declarative_base

from sqlalchemy_fsm import FSMField, transition, validate_fsm
from sqlalchemy_fsm.exc import SetupError


class TestFSMFieldSubscript:
    def test_returns_subclass_of_fsmfield(self):
        T = FSMField["draft", "published"]
        assert issubclass(T, FSMField)

    def test_subclass_is_cached(self):
        assert FSMField["a", "b"] is FSMField["a", "b"]

    def test_order_independent_cache(self):
        # Same set of states → same subclass regardless of order.
        assert FSMField["a", "b"] is FSMField["b", "a"]

    def test_single_state(self):
        T = FSMField["only"]
        assert T._allowed_states == frozenset({"only"})

    def test_states_stored_as_frozenset(self):
        T = FSMField["draft", "published", "archived"]
        assert T._allowed_states == frozenset({"draft", "published", "archived"})

    def test_plain_fsmfield_has_no_allowed_states(self):
        assert FSMField._allowed_states is None

    def test_rejects_non_string_arg(self):
        with pytest.raises(TypeError):
            FSMField[42]  # type: ignore[misc]

    def test_rejects_empty(self):
        with pytest.raises(TypeError):
            FSMField[()]  # type: ignore[misc]

    def test_instantiable_as_sa_type(self):
        T = FSMField["a", "b"]
        # Must be usable in a Column without arguments.
        col = sqlalchemy.Column(T)
        assert isinstance(col.type, FSMField)
        assert col.type._allowed_states == frozenset({"a", "b"})


# --- validator: correct/complete/reachable -------------------------------


class TestValidateFsmCorrect:
    def _make(
        self,
        *,
        with_extra_target: bool = False,
        with_unknown_source: bool = False,
    ):
        Base = declarative_base()

        class M(Base):
            __tablename__ = "validate_correct"
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
            state = sqlalchemy.Column(
                FSMField["draft", "published"],
                nullable=False,
                default="draft",
            )

            @transition(source="draft", target="published")
            def publish(self):
                pass

        if with_extra_target:
            # Add a transition with a target outside the declared set.
            M.archive = transition(source="published", target="archived")(
                lambda self: None
            )
        if with_unknown_source:
            M.from_nowhere = transition(source="ghost", target="published")(
                lambda self: None
            )

        return M

    def test_clean_graph_passes(self):
        validate_fsm(self._make())

    def test_unknown_target_raises(self):
        with pytest.raises(SetupError, match="not in the declared"):
            validate_fsm(self._make(with_extra_target=True))

    def test_unknown_source_raises(self):
        with pytest.raises(SetupError, match="not in the declared"):
            validate_fsm(self._make(with_unknown_source=True))


class TestValidateFsmComplete:
    def test_unused_allowed_state_raises(self):
        Base = declarative_base()

        class M(Base):
            __tablename__ = "validate_complete"
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
            state = sqlalchemy.Column(
                FSMField["draft", "published", "frozen"],
                nullable=False,
                default="draft",
            )

            @transition(source="draft", target="published")
            def publish(self):
                pass

        with pytest.raises(SetupError, match="never referenced"):
            validate_fsm(M)


class TestValidateFsmReachable:
    def test_island_raises(self):
        Base = declarative_base()

        class M(Base):
            __tablename__ = "validate_island"
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
            state = sqlalchemy.Column(
                FSMField["a", "b", "c", "d"],
                nullable=False,
                default="a",
            )

            @transition(source="a", target="b")
            def ab(self):
                pass

            # c↔d is disconnected from {a, b}.
            @transition(source="c", target="d")
            def cd(self):
                pass

            @transition(source="d", target="c")
            def dc(self):
                pass

        with pytest.raises(SetupError, match="unreachable"):
            validate_fsm(M)

    def test_wildcard_source_makes_all_states_reachable(self):
        Base = declarative_base()

        class M(Base):
            __tablename__ = "validate_wildcard"
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
            state = sqlalchemy.Column(
                FSMField["a", "deleted"],
                nullable=False,
                default="a",
            )

            @transition(source="*", target="deleted")
            def delete(self):
                pass

        validate_fsm(M)

    def test_missing_default_raises(self):
        Base = declarative_base()

        class M(Base):
            __tablename__ = "validate_no_default"
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
            state = sqlalchemy.Column(FSMField["a", "b"], nullable=False)

            @transition(source="a", target="b")
            def go(self):
                pass

        with pytest.raises(SetupError, match="default="):
            validate_fsm(M)

    def test_default_outside_declared_set_raises(self):
        Base = declarative_base()

        class M(Base):
            __tablename__ = "validate_bad_default"
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
            state = sqlalchemy.Column(
                FSMField["a", "b"], nullable=False, default="elsewhere"
            )

            @transition(source="a", target="b")
            def go(self):
                pass

        with pytest.raises(SetupError, match="default="):
            validate_fsm(M)


class TestNoOpForPlainFsmField:
    """Plain FSMField (no subscript) must skip validation — backwards
    compatibility for existing models."""

    def test_plain_field_skips_validation(self):
        Base = declarative_base()

        class M(Base):
            __tablename__ = "validate_plain"
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
            state = sqlalchemy.Column(FSMField, nullable=False)

            @transition(source="anything", target="goes")
            def go(self):
                pass

        # No declared states → no checks → no error.
        validate_fsm(M)


class TestStartupAutoValidation:
    """The SA mapper_configured event listener should fire validation
    automatically when models are configured."""

    def test_bad_graph_raises_at_mapper_configuration(self):
        Base = declarative_base()

        class M(Base):
            __tablename__ = "validate_auto"
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
            state = sqlalchemy.Column(
                FSMField["a", "b", "c"],
                nullable=False,
                default="a",
            )

            @transition(source="a", target="b")
            def ab(self):
                pass

            # 'c' is declared but unused → completeness failure.

        # Configure only this registry's mappers, so leaked bad models
        # from earlier tests don't poison this assertion.
        with pytest.raises(SetupError, match="never referenced"):
            Base.registry.configure()
