"""Tests for the `permissions=` kwarg on `@transition`."""

import pytest
import sqlalchemy

from sqlalchemy_fsm import FSMField, transition
from sqlalchemy_fsm.exc import PermissionDeniedError, PreconditionError

from .conftest import Base


def is_editor(instance, user=None, **_):
    return getattr(user, "role", None) == "editor"


def is_owner(instance, user=None, **_):
    return getattr(user, "id", None) == getattr(instance, "owner_id", object())


class User:
    def __init__(self, id_, role):
        self.id = id_
        self.role = role


class Doc(Base):
    __tablename__ = "PermissionDoc"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    owner_id = sqlalchemy.Column(sqlalchemy.Integer, default=1)
    state = sqlalchemy.Column(FSMField)

    def __init__(self, *args, **kwargs):
        self.state = "draft"
        self.owner_id = kwargs.pop("owner_id", 1)
        super().__init__(*args, **kwargs)

    @transition(source="draft", target="published", permissions=[is_editor])
    def publish(self, user=None):
        pass

    @transition(
        source="draft",
        target="archived",
        permissions=[is_editor, is_owner],
    )
    def archive(self, user=None):
        pass


class TestPermissions:
    @pytest.fixture
    def doc(self):
        return Doc(owner_id=1)

    def test_no_user_denied(self, doc):
        assert not doc.publish.can_proceed()
        with pytest.raises(PermissionDeniedError):
            doc.publish.set()
        assert doc.state == "draft"

    def test_wrong_role_denied(self, doc):
        viewer = User(1, "viewer")
        assert not doc.publish.can_proceed(user=viewer)
        with pytest.raises(PermissionDeniedError):
            doc.publish.set(user=viewer)

    def test_correct_role_allowed(self, doc):
        editor = User(2, "editor")
        assert doc.publish.can_proceed(user=editor)
        doc.publish.set(user=editor)
        assert doc.state == "published"

    def test_all_permissions_must_pass(self, doc):
        # Editor but not owner — second permission rejects.
        other_editor = User(99, "editor")
        assert not doc.archive.can_proceed(user=other_editor)
        with pytest.raises(PermissionDeniedError):
            doc.archive.set(user=other_editor)

        owning_editor = User(1, "editor")
        assert doc.archive.can_proceed(user=owning_editor)
        doc.archive.set(user=owning_editor)
        assert doc.state == "archived"


# --- permissions checked before conditions (denial precedence) ---


def fail_condition(instance, **_):
    return False


def fail_permission(instance, **_):
    return False


class OrderingDoc(Base):
    __tablename__ = "PermissionOrderingDoc"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = sqlalchemy.Column(FSMField)

    def __init__(self, *a, **kw):
        self.state = "draft"
        super().__init__(*a, **kw)

    @transition(
        source="draft",
        target="done",
        permissions=[fail_permission],
        conditions=[fail_condition],
    )
    def finish(self):
        pass


def test_permissions_checked_before_conditions():
    """When both fail, PermissionDeniedError is raised — not PreconditionError."""
    doc = OrderingDoc()
    with pytest.raises(PermissionDeniedError):
        doc.finish.set()


# --- class-based transitions inherit permissions from the parent ---


def parent_perm(handler_self, instance, allow=False, **_):
    return allow


class MultiHandler(Base):
    __tablename__ = "PermissionMultiHandler"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = sqlalchemy.Column(FSMField)

    def __init__(self, *a, **kw):
        self.state = "a"
        super().__init__(*a, **kw)

    @transition(target="b", permissions=[parent_perm])
    class go:  # noqa: N801
        @transition(source="a")
        def from_a(self, instance, allow=False):
            pass


class TestClassBasedPermissionInheritance:
    @pytest.fixture
    def m(self):
        return MultiHandler()

    def test_inherits(self, m):
        assert not m.go.can_proceed(allow=False)
        with pytest.raises(PermissionDeniedError):
            m.go.set(allow=False)
        assert m.state == "a"

        assert m.go.can_proceed(allow=True)
        m.go.set(allow=True)
        assert m.state == "b"


# --- no permissions kwarg = unchanged behavior ---


class LegacyDoc(Base):
    __tablename__ = "PermissionLegacyDoc"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = sqlalchemy.Column(FSMField)

    def __init__(self, *a, **kw):
        self.state = "draft"
        super().__init__(*a, **kw)

    @transition(source="draft", target="done")
    def finish(self):
        pass


class TestLegacy:
    @pytest.fixture
    def d(self):
        return LegacyDoc()

    def test_transition_without_permissions_works_as_before(self, d):
        assert d.finish.can_proceed()
        d.finish.set()
        assert d.state == "done"


# Silence unused import warning under strict configs.
_ = PreconditionError
