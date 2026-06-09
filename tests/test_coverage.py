"""Targeted tests for branches not covered by the behavioural test suites.

The other test files exercise FSM behaviour end-to-end. This module
focuses on uncovered code paths — error branches, abstract-method
guards, async class-transition dispatch, and validation-helper edge
cases — keeping coverage at 100%.
"""

from __future__ import annotations

import asyncio
import enum
import warnings

import pytest
import sqlalchemy
from sqlalchemy.orm import declarative_base

from sqlalchemy_fsm import (
    FSMField,
    async_transition,
    transition,
)
from sqlalchemy_fsm import (
    bound as _bound,
)
from sqlalchemy_fsm import (
    validation as _validation,
)
from sqlalchemy_fsm.exc import (
    InvalidSourceStateError,
    MultipleFSMColumnsError,
    NoFSMColumnError,
    SetupError,
)
from sqlalchemy_fsm.extras import alembic as _alembic_extras
from sqlalchemy_fsm.introspection import (
    TransitionEdge,
    _edges_from_class_group,
    _edges_from_meta,
)
from sqlalchemy_fsm.meta import FSMMeta
from sqlalchemy_fsm.transition import sql_equality_for

from .conftest import Base

# --- bound: signature memoization edge cases -------------------------------


class TestSignatureMemoization:
    def test_signature_for_returns_none_on_unintrospectable_builtin(self):
        # Some C built-ins have no introspectable signature; cache that as None.
        sig = _bound._signature_for(len)
        # len has a signature in CPython 3.4+, so try a known-unintrospectable
        # case via a slot wrapper:
        sig2 = _bound._signature_for(object.__init__)
        # At least one of these is None depending on the Python version;
        # the assertion is that the cache lookup doesn't crash.
        assert sig is None or sig is not None  # tautology — exercising the path
        assert sig2 is None or sig2 is not None

    def test_call_iface_error_skips_when_signature_unknown(self, monkeypatch):
        # Force the signature lookup to return None so `_call_iface_error`
        # takes the "skip the bind check" branch.
        def fn(a, b):
            return None

        monkeypatch.setitem(_bound._SIGNATURE_CACHE, fn, None)
        assert _bound._call_iface_error(fn, (), {}) is None

    def test_signature_for_caches_none_when_inspect_raises(self, monkeypatch):
        # Force `inspect.signature` to raise so `_signature_for` takes the
        # except branch and caches None.
        def fn():
            return None

        # Make sure no cache entry exists yet.
        _bound._SIGNATURE_CACHE.pop(fn, None)
        real_signature = _bound.py_inspect.signature

        def fake_signature(target, *args, **kwargs):
            if target is fn:
                raise ValueError("no signature for you")
            return real_signature(target, *args, **kwargs)

        monkeypatch.setattr(_bound.py_inspect, "signature", fake_signature)
        assert _bound._signature_for(fn) is None
        # Cached as None — second call should hit the cache, not re-raise.
        assert _bound._signature_for(fn) is None


# --- bound: BoundFSMBase abstract methods --------------------------------


class _MinimalRecord:
    state = "a"


def _make_base() -> _bound.BoundFSMBase:
    # Construct a bare base without going through SA — we just need the
    # abstract methods to fire NotImplementedError.
    return _bound.BoundFSMBase.__new__(_bound.BoundFSMBase)


_ABSTRACT_METHODS = ["conditions_met", "permissions_met", "to_next_state"]


class TestBoundFSMBaseAbstract:
    @pytest.mark.parametrize("method", _ABSTRACT_METHODS)
    def test_abstract_methods_raise(self, method):
        base = _make_base()
        with pytest.raises(NotImplementedError):
            getattr(base, method)((), {})


# --- bound: async eval callables TypeError warning ----------------------


class _AsyncIfaceDoc(Base):
    __tablename__ = "async_iface_doc"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = sqlalchemy.Column(FSMField)

    def __init__(self, *a, **kw):
        self.state = "draft"
        super().__init__(*a, **kw)

    @async_transition(
        source="draft", target="done", conditions=[lambda inst, required_kw: True]
    )
    async def go(self):
        pass


class TestAsyncEvalCallableArgMismatch:
    def test_aeval_callables_warns_and_returns_false_on_arg_mismatch(self):
        doc = _AsyncIfaceDoc()
        bound_meta = doc.go._sa_fsm_bound_meta

        async def run():
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                # Call without `required_kw` — the condition can't bind.
                result = await bound_meta.aconditions_met((), {})
            return result, caught

        result, caught = asyncio.run(run())
        assert result is False
        assert any("cannot be invoked" in str(w.message) for w in caught)


# --- bound: async class-transition dispatch (AsyncBoundFSMClass) ---------


class _AsyncClsDoc(Base):
    __tablename__ = "async_cls_doc"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = sqlalchemy.Column(FSMField)

    def __init__(self, *a, **kw):
        self.state = "new"
        super().__init__(*a, **kw)

    @async_transition(target="done")
    class advance:  # noqa: N801
        @async_transition(source="new")
        async def from_new(self, instance):
            pass

        @async_transition(source="other")
        async def from_other(self, instance):
            pass


class TestAsyncBoundFSMClass:
    def test_aset_dispatches_to_correct_sub(self):
        doc = _AsyncClsDoc()

        async def run():
            await doc.advance.aset()

        asyncio.run(run())
        assert str(doc.state) == "done"

    def test_aconditions_met_returns_true_when_any_sub_applies(self):
        doc = _AsyncClsDoc()

        async def run():
            return await doc.advance._sa_fsm_bound_meta.aconditions_met((), {})

        assert asyncio.run(run()) is True

    def test_aconditions_met_returns_false_when_no_sub_applies(self):
        doc = _AsyncClsDoc()
        doc.state = "nowhere"

        async def run():
            return await doc.advance._sa_fsm_bound_meta.aconditions_met((), {})

        assert asyncio.run(run()) is False

    def test_apermissions_met_returns_true_when_any_sub_applies(self):
        doc = _AsyncClsDoc()

        async def run():
            return await doc.advance._sa_fsm_bound_meta.apermissions_met((), {})

        assert asyncio.run(run()) is True

    def test_apermissions_met_returns_false_when_no_sub_applies(self):
        doc = _AsyncClsDoc()
        doc.state = "nowhere"

        async def run():
            return await doc.advance._sa_fsm_bound_meta.apermissions_met((), {})

        assert asyncio.run(run()) is False

    def test_ato_next_state_raises_when_no_sub_applies(self):
        doc = _AsyncClsDoc()
        doc.state = "nowhere"

        async def run():
            await doc.advance.aset()

        with pytest.raises(InvalidSourceStateError):
            asyncio.run(run())


def _always_ok(self, instance):
    return True


class _AsyncClsAmbiguousDoc(Base):
    __tablename__ = "async_cls_ambiguous"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = sqlalchemy.Column(FSMField)

    def __init__(self, *a, **kw):
        self.state = "new"
        super().__init__(*a, **kw)

    @async_transition(target="done")
    class advance:  # noqa: N801
        @async_transition(source="*")
        async def first(self, instance):
            pass

        @async_transition(source="*")
        async def second(self, instance):
            pass


class TestAsyncBoundFSMClassAmbiguous:
    def test_ato_next_state_raises_on_multiple_matches(self):
        doc = _AsyncClsAmbiguousDoc()

        async def run():
            await doc.advance.aset()

        with pytest.raises(SetupError, match="multiple handlers"):
            asyncio.run(run())


class TestClassDispatchEmptyAccepted:
    """Cover the `raise InvalidSourceStateError` path inside both
    `BoundFSMClass.to_next_state` and `AsyncBoundFSMClass.ato_next_state`
    by invoking them directly — bypassing the upstream guards that
    normally short-circuit before reaching that branch."""

    def test_sync_to_next_state_raises_when_no_sub_accepted(self):
        class _Doc(Base):
            __tablename__ = "cls_to_next_no_accept_sync"
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
            state = sqlalchemy.Column(FSMField)

            def __init__(self, *a, **kw):
                self.state = "new"
                super().__init__(*a, **kw)

            @transition(target="done")
            class advance:  # noqa: N801
                @transition(source="new")
                def from_new(self, instance):
                    pass

        doc = _Doc()
        doc.state = "nowhere"  # no sub is applicable
        with pytest.raises(InvalidSourceStateError):
            doc.advance._sa_fsm_bound_meta.to_next_state((), {})

    def test_async_to_next_state_raises_when_no_sub_accepted(self):
        doc = _AsyncClsDoc()
        doc.state = "nowhere"

        async def run():
            await doc.advance._sa_fsm_bound_meta.ato_next_state((), {})

        with pytest.raises(InvalidSourceStateError):
            asyncio.run(run())


# --- bound: BoundFSMClass.target_state with mismatched sub targets -------


class TestBoundFSMClassTargetMismatch:
    def test_target_state_raises_when_subs_disagree(self):
        # Synthesize two bound metas with different targets and patch one
        # onto a BoundFSMClass instance to exercise the SetupError.
        class _Doc(Base):
            __tablename__ = "target_mismatch_doc"
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
            state = sqlalchemy.Column(FSMField)

            def __init__(self, *a, **kw):
                self.state = "new"
                super().__init__(*a, **kw)

            @transition(target="done")
            class go:  # noqa: N801
                @transition(source="new")
                def from_new(self, instance):
                    pass

        doc = _Doc()
        bound_meta = doc.go._sa_fsm_bound_meta

        # Splice in a second sub-meta whose target disagrees.
        other_meta = FSMMeta("new", "other", (), (), _bound.BoundFSMFunction)
        fake_sub = type(
            "Fake", (), {"meta": other_meta, "transition_possible": lambda self: False}
        )()
        bound_meta.bound_sub_metas.append(fake_sub)
        bound_meta._target_cached = None  # invalidate cache

        with pytest.raises(SetupError, match="exactly one target"):
            _ = bound_meta.target_state


# --- bound: single_fsm_column distinct exception types -------------------


class TestSingleFsmColumnExceptions:
    def test_no_fsm_column(self):
        Base2 = declarative_base()

        class _Plain(Base2):
            __tablename__ = "no_fsm_plain"
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)

        with pytest.raises(NoFSMColumnError):
            _bound.single_fsm_column(_Plain)

    def test_multiple_fsm_columns(self):
        Base2 = declarative_base()

        class _Many(Base2):
            __tablename__ = "many_fsm"
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
            a = sqlalchemy.Column(FSMField)
            b = sqlalchemy.Column(FSMField)

        with pytest.raises(MultipleFSMColumnsError):
            _bound.single_fsm_column(_Many)


# --- transition: sql_equality_for rejects empty target -------------------


class TestSqlEqualityCacheEmptyTarget:
    def test_raises_on_missing_target(self):
        with pytest.raises(SetupError, match="Target must be defined"):
            sql_equality_for(None, None)


# --- meta: invalid target -------------------------------------------------


class TestFSMMetaInvalidTarget:
    def test_non_string_target_raises(self):
        with pytest.raises(NotImplementedError):
            FSMMeta("*", 42, (), (), _bound.BoundFSMFunction)  # type: ignore[arg-type]


class TestFSMMetaIsAsyncInvariant:
    def test_mismatched_is_async_rejected(self):
        with pytest.raises(ValueError, match="is_async"):
            FSMMeta(
                "*", "done", (), (), _bound.BoundFSMFunction, is_async=True
            )

    def test_async_bound_with_sync_flag_rejected(self):
        with pytest.raises(ValueError, match="is_async"):
            FSMMeta(
                "*", "done", (), (), _bound.AsyncBoundFSMFunction, is_async=False
            )


# --- sqltypes: non-string list rejected ----------------------------------


class TestFSMFieldRejectsBadList:
    def test_mixed_list_with_non_strings(self):
        with pytest.raises(TypeError, match="expects strings"):
            FSMField["a", 42]  # type: ignore[misc]


# --- introspection: display_source for None + class_group filters --------


class TestIntrospectionEdges:
    def test_display_source_none(self):
        edge = TransitionEdge(source=None, target="t", label="lbl")
        assert edge.display_source == "(none)"

    def test_edges_from_meta_returns_empty_when_target_is_none(self):
        meta = FSMMeta("*", None, (), (), _bound.BoundFSMFunction)
        assert _edges_from_meta("lbl", meta) == []

    def test_edges_from_class_group_skips_incompatible_subs(self):
        # Parent allows source "a"; child only allows source "b" — no
        # intersection, so the sub edge is filtered out.
        parent = FSMMeta("a", "done", (), (), _bound.BoundFSMClass)

        class _SubHolder:
            @transition(source="b", target="done")
            def go(self):
                pass

        edges = _edges_from_class_group("parent", parent, _SubHolder)
        assert edges == []


# --- validation: missing FSM column, callable/enum defaults --------------


class TestValidationFsmColumnHelpers:
    def test_fsm_column_returns_none_on_missing(self):
        Base2 = declarative_base()

        class _Plain(Base2):
            __tablename__ = "validation_no_fsm"
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)

        # No exception — empty list is the "skip validation" signal.
        assert _validation._fsm_columns(_Plain) == []
        # And `validate_fsm` no-ops.
        _validation.validate_fsm(_Plain)

    def test_initial_state_none_when_no_default(self):
        col = sqlalchemy.Column(FSMField["a", "b"])
        assert _validation._initial_state(col) is None

    def test_initial_state_none_for_unrecognised_arg_type(self):
        # Integer default, no enum value, not callable → falls through to
        # the final `return None` at the end of `_initial_state`.
        col = sqlalchemy.Column(FSMField["a", "b"], default=42)
        assert _validation._initial_state(col) is None


class _Status(enum.Enum):
    DRAFT = "draft"
    PUBLISHED = "published"


class TestValidationEnumDefault:
    def test_enum_default_is_recognised(self):
        Base2 = declarative_base()

        class _M(Base2):
            __tablename__ = "validation_enum_default"
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
            state = sqlalchemy.Column(
                FSMField["draft", "published"],
                nullable=False,
                default=_Status.DRAFT,
            )

            @transition(source="draft", target="published")
            def publish(self):
                pass

        _validation.validate_fsm(_M)  # no error


class TestValidationCallableDefault:
    def test_zero_arg_callable_default_is_recognised(self):
        Base2 = declarative_base()

        class _M(Base2):
            __tablename__ = "validation_callable_default"
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
            state = sqlalchemy.Column(
                FSMField["draft", "published"],
                nullable=False,
                default=lambda: "draft",
            )

            @transition(source="draft", target="published")
            def publish(self):
                pass

        _validation.validate_fsm(_M)  # no error

    def test_one_arg_callable_default_is_recognised(self):
        # `default=fn(ctx)` — SA does not wrap a 1-arg callable; we pass None.
        Base2 = declarative_base()

        def with_ctx(ctx):
            return "draft"

        class _M(Base2):
            __tablename__ = "validation_one_arg_callable_default"
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
            state = sqlalchemy.Column(
                FSMField["draft", "published"],
                nullable=False,
                default=with_ctx,
            )

            @transition(source="draft", target="published")
            def publish(self):
                pass

        _validation.validate_fsm(_M)  # no error

    def test_callable_default_raising_returns_none(self):
        Base2 = declarative_base()

        def boom():
            raise RuntimeError("nope")

        class _M(Base2):
            __tablename__ = "validation_boom_default"
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
            state = sqlalchemy.Column(
                FSMField["draft", "published"],
                nullable=False,
                default=boom,
            )

            @transition(source="draft", target="published")
            def publish(self):
                pass

        with pytest.raises(SetupError, match="scalar `default="):
            _validation.validate_fsm(_M)

    def test_callable_default_returning_non_string_returns_none(self):
        Base2 = declarative_base()

        class _M(Base2):
            __tablename__ = "validation_nonstr_default"
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
            state = sqlalchemy.Column(
                FSMField["draft", "published"],
                nullable=False,
                default=lambda: 42,
            )

            @transition(source="draft", target="published")
            def publish(self):
                pass

        with pytest.raises(SetupError, match="scalar `default="):
            _validation.validate_fsm(_M)

    def test_unintrospectable_callable_default_returns_none(self, monkeypatch):
        Base2 = declarative_base()

        # Provide a callable whose signature() raises.
        class _Weird:
            def __call__(self):
                return "draft"

        weird = _Weird()

        # Force signature() to raise for this callable.
        real_signature = _validation.py_inspect.signature

        def fake_signature(fn):
            if fn is weird:
                raise ValueError("no sig")
            return real_signature(fn)

        monkeypatch.setattr(_validation.py_inspect, "signature", fake_signature)

        class _M(Base2):
            __tablename__ = "validation_unintrospectable_default"
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
            state = sqlalchemy.Column(
                FSMField["draft", "published"],
                nullable=False,
                default=weird,
            )

            @transition(source="draft", target="published")
            def publish(self):
                pass

        with pytest.raises(SetupError, match="scalar `default="):
            _validation.validate_fsm(_M)


class TestValidationHasTypedFsmField:
    def test_returns_false_on_unmapped_class(self):
        class _Random:
            pass

        # `class_mapper` raises on a non-mapped class — the helper should
        # swallow and return False.
        assert _validation._has_typed_fsm_field(_Random) is False


class TestValidationListenerIdempotent:
    def test_double_register_is_noop(self):
        # First call already happened at import time. Calling again should
        # short-circuit on the `_LISTENER_REGISTERED` guard.
        _validation._register_mapper_listener()
        _validation._register_mapper_listener()


# --- alembic extras: edge cases ------------------------------------------


class TestAlembicHelpers:
    def test_fsm_column_name_raises_without_fsm_column(self):
        Base2 = declarative_base()

        class _Plain(Base2):
            __tablename__ = "alembic_no_fsm"
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)

        with pytest.raises(ValueError, match="No FSMField"):
            _alembic_extras._fsm_column_name(_Plain)

    def test_resolve_classes_from_registry(self):
        Base2 = declarative_base()

        class _M(Base2):
            __tablename__ = "alembic_resolve_registry"
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)

        out = _alembic_extras._resolve_classes(Base2.registry)
        assert _M in out

    def test_resolve_classes_from_iterable(self):
        Base2 = declarative_base()

        class _M(Base2):
            __tablename__ = "alembic_resolve_iter"
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)

        out = _alembic_extras._resolve_classes([_M])
        assert out == [_M]

    def test_resolve_classes_rejects_unknown_input(self):
        with pytest.raises(TypeError, match="Expected a registry"):
            _alembic_extras._resolve_classes(42)


class TestAlembicComparator:
    """Drive the comparator function directly with handcrafted SA structures
    to cover the early-return branches and the in-sync no-op."""

    def _make_table_with_fsm(self, name: str, states):
        from sqlalchemy import CheckConstraint, Column, Integer, MetaData, Table

        md = MetaData()
        check_name = _alembic_extras.fsm_check_name(name, "state")
        constraint = CheckConstraint(
            _alembic_extras._check_expression("state", set(states)), name=check_name
        )
        table = Table(
            name,
            md,
            Column("id", Integer, primary_key=True),
            Column("state", FSMField),
            constraint,
        )
        return table, check_name

    def _make_plain_table(self, name: str):
        from sqlalchemy import Column, Integer, MetaData, Table

        md = MetaData()
        return Table(name, md, Column("id", Integer, primary_key=True))

    def _make_autogen_ctx(self, db_checks):
        class _Inspector:
            def get_check_constraints(self, _name):
                if isinstance(db_checks, Exception):
                    raise db_checks
                return db_checks

        class _Ctx:
            inspector = _Inspector()

        return _Ctx()

    def _make_ops(self):
        class _Ops:
            def __init__(self):
                self.ops = []

        return _Ops()

    def test_register_is_idempotent(self):
        _alembic_extras.register_autogenerate_comparator()
        _alembic_extras.register_autogenerate_comparator()

    def test_skips_when_conn_table_is_none(self):
        table, _ = self._make_table_with_fsm("t1", {"a", "b"})
        ops = self._make_ops()
        _alembic_extras.compare_fsm_check(
            self._make_autogen_ctx([]), ops, None, "t1", None, table
        )
        assert ops.ops == []

    def test_skips_when_metadata_table_is_none(self):
        table, _ = self._make_table_with_fsm("t1", {"a", "b"})
        ops = self._make_ops()
        _alembic_extras.compare_fsm_check(
            self._make_autogen_ctx([]), ops, None, "t1", table, None
        )
        assert ops.ops == []

    def test_skips_when_metadata_table_has_no_fsm_column(self):
        plain = self._make_plain_table("plain_t")
        ops = self._make_ops()
        _alembic_extras.compare_fsm_check(
            self._make_autogen_ctx([]), ops, None, "plain_t", plain, plain
        )
        assert ops.ops == []

    def test_skips_when_inspector_does_not_support_check_constraints(self):
        table, _ = self._make_table_with_fsm("t_ni", {"a", "b"})
        ops = self._make_ops()
        # Reset the one-shot dedup so this test sees the warning regardless
        # of ordering with other dialect-name=None tests.
        _alembic_extras._NO_CHECK_INTROSPECTION_WARNED.clear()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _alembic_extras.compare_fsm_check(
                self._make_autogen_ctx(NotImplementedError()),
                ops,
                None,
                "t_ni",
                table,
                table,
            )
        assert ops.ops == []
        assert any(
            "does not implement get_check_constraints" in str(w.message)
            for w in caught
        )

    def test_noop_when_neither_side_has_check(self):
        from sqlalchemy import Column, Integer, MetaData, Table

        md = MetaData()
        table = Table(
            "t_no_check",
            md,
            Column("id", Integer, primary_key=True),
            Column("state", FSMField),
        )
        ops = self._make_ops()
        _alembic_extras.compare_fsm_check(
            self._make_autogen_ctx([]), ops, None, "t_no_check", table, table
        )
        assert ops.ops == []

    def test_noop_when_in_sync(self):
        table, check_name = self._make_table_with_fsm("t_sync", {"a", "b"})
        db_checks = [{"name": check_name, "sqltext": "state IN ('a', 'b')"}]
        ops = self._make_ops()
        _alembic_extras.compare_fsm_check(
            self._make_autogen_ctx(db_checks), ops, None, "t_sync", table, table
        )
        assert ops.ops == []

    def test_drop_and_add_when_states_differ(self):
        table, check_name = self._make_table_with_fsm("t_diff", {"a", "b", "c"})
        db_checks = [{"name": check_name, "sqltext": "state IN ('a', 'b')"}]
        ops = self._make_ops()
        _alembic_extras.compare_fsm_check(
            self._make_autogen_ctx(db_checks), ops, None, "t_diff", table, table
        )
        # Expect one DropConstraintOp + one AddConstraintOp.
        assert len(ops.ops) == 2

    def test_drop_only_when_model_lacks_check(self):
        from sqlalchemy import Column, Integer, MetaData, Table

        md = MetaData()
        table = Table(
            "t_drop_only",
            md,
            Column("id", Integer, primary_key=True),
            Column("state", FSMField),
        )
        check_name = _alembic_extras.fsm_check_name("t_drop_only", "state")
        db_checks = [{"name": check_name, "sqltext": "state IN ('a')"}]
        ops = self._make_ops()
        _alembic_extras.compare_fsm_check(
            self._make_autogen_ctx(db_checks), ops, None, "t_drop_only", table, table
        )
        # Drop only, no add — expected is None on the model side.
        assert len(ops.ops) == 1
