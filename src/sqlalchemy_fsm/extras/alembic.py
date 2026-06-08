"""Alembic integration: render and autogenerate `CHECK` constraints for
FSM-managed columns.

A model with `@transition` declarations carries a closed set of legal
states (the union of `source`s and `target`s, minus wildcards). This
module turns that into a CHECK constraint on the underlying table and
hooks Alembic's autogenerate to detect drift between the model and the
database.

## Two ways to use it

**Attach at metadata setup time** (works with any Alembic configuration):

```python
from sqlalchemy_fsm.extras.alembic import attach_fsm_constraints
attach_fsm_constraints(Base.metadata)
# Now Base.metadata.tables carry the CHECK; standard Alembic autogenerate
# detects new tables/columns. For *changes* to existing CHECKs, also enable
# the comparator below.
```

**Register the autogenerate comparator** (detects state-set changes on
existing tables and emits drop/add ops):

```python
# env.py
from sqlalchemy_fsm.extras.alembic import (
    attach_fsm_constraints,
    register_autogenerate_comparator,
)
attach_fsm_constraints(target_metadata)
register_autogenerate_comparator()

context.configure(target_metadata=target_metadata, ...)
```

`alembic` is an optional dependency; install with
`pip install sqlalchemy-fsm[alembic]`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint
from sqlalchemy import inspect as sqla_inspect

from .. import bound as _bound
from ..sqltypes import FSMField
from ..transition import FsmTransition

if TYPE_CHECKING:
    from sqlalchemy import Table
    from sqlalchemy.engine import Inspector

try:
    from alembic.autogenerate import comparators as _comparators
    from alembic.operations import ops as _ops
except ImportError as _alembic_import_err:  # pragma: no cover
    _ALEMBIC_AVAILABLE = False
    _ALEMBIC_IMPORT_ERR: ImportError | None = _alembic_import_err
    _comparators: Any = None  # type: ignore[no-redef]
    _ops: Any = None  # type: ignore[no-redef]
else:
    _ALEMBIC_AVAILABLE = True
    _ALEMBIC_IMPORT_ERR = None


def _require_alembic() -> None:
    if not _ALEMBIC_AVAILABLE:
        raise RuntimeError(
            "alembic is not installed. Install with: pip install sqlalchemy-fsm[alembic]"
        ) from _ALEMBIC_IMPORT_ERR


# --- core state extraction --------------------------------------------------


def _iter_transitions(model_cls: type) -> list[tuple[str, FsmTransition]]:
    out: list[tuple[str, FsmTransition]] = []
    for name in dir(model_cls):
        for klass in model_cls.__mro__:
            if name in klass.__dict__:
                attr = klass.__dict__[name]
                if isinstance(attr, FsmTransition):
                    out.append((name, attr))
                break
    return out


def _states_from_meta(meta: Any) -> set[str]:
    """Collect concrete (non-wildcard, non-null) states from one `FSMMeta`."""
    states: set[str] = set()
    if meta.target is not None:
        states.add(meta.target)
    for src in meta.sources:
        if src is None or src == "*":
            continue
        states.add(src)
    return states


def collect_states(model_cls: type) -> set[str]:
    """Return the closed set of legal states declared by a model's transitions.

    Walks every `@transition` on `model_cls` (including sub-handlers of
    class-grouped transitions) and unions their concrete `source` / `target`
    states. Wildcards (`"*"`) and `None` sources are excluded — they aren't
    constants you can pin in a CHECK constraint.
    """
    states: set[str] = set()
    for _, fsm_t in _iter_transitions(model_cls):
        meta = fsm_t.meta
        states |= _states_from_meta(meta)
        if meta.bound_cls is _bound.BoundFSMClass and isinstance(fsm_t.set_fn, type):
            for _, sub in _iter_transitions(fsm_t.set_fn):
                states |= _states_from_meta(sub.meta)
    return states


def _fsm_column_name(model_cls: type) -> str:
    """Discover the name of the FSMField column on this mapped class."""
    for col in sqla_inspect(model_cls).columns:
        if isinstance(col.type, FSMField):
            return col.name
    raise ValueError(f"No FSMField column on {model_cls!r}")


def fsm_check_name(table_name: str, column_name: str) -> str:
    """Deterministic CHECK constraint name. Stable across runs so Alembic can
    match metadata and DB by name."""
    return f"ck_{table_name}_{column_name}_fsm"


def _check_sql(column_name: str, states: set[str]) -> str:
    quoted = ", ".join(f"'{s}'" for s in sorted(states))
    return f"{column_name} IN ({quoted})"


def render_check_constraint(model_cls: type) -> CheckConstraint:
    """Build (without attaching) the `CheckConstraint` for a model's FSM column."""
    states = collect_states(model_cls)
    column = _fsm_column_name(model_cls)
    table_name = sqla_inspect(model_cls).local_table.name
    return CheckConstraint(
        _check_sql(column, states), name=fsm_check_name(table_name, column)
    )


# --- metadata attachment ---------------------------------------------------


def _resolve_classes(source: Any) -> list[type]:
    """Normalize the various inputs `attach_fsm_constraints` accepts into a
    flat list of mapped classes:

    - a `registry` (`Base.registry`)            → its `.mappers`' classes
    - a declarative base class                  → `cls.registry`'s classes
    - an iterable of mapped classes             → as-is
    """
    # registry: has .mappers attr
    if hasattr(source, "mappers") and hasattr(source, "metadata"):
        return [m.class_ for m in source.mappers]
    # declarative base: has .registry
    if isinstance(source, type) and hasattr(source, "registry"):
        return [m.class_ for m in source.registry.mappers]
    # iterable of classes
    if isinstance(source, (list, tuple, set, frozenset)):
        return [c for c in source if isinstance(c, type)]
    raise TypeError(
        f"Expected a registry, declarative base, or iterable of mapped "
        f"classes; got {type(source).__name__}"
    )


def _table_has_fsm_column(table: Table) -> bool:
    return any(isinstance(c.type, FSMField) for c in table.columns)


def attach_fsm_constraints(source: Any) -> list[CheckConstraint]:
    """Attach a CHECK constraint to every FSM-managed table.

    `source` may be a SQLAlchemy `registry` (e.g. `Base.registry`), a
    declarative base class, or an iterable of mapped classes.

    Idempotent: re-running drops the previous FSM-named CHECK from each
    table before re-adding the freshly computed one. Returns the list of
    attached constraints.
    """
    attached: list[CheckConstraint] = []
    for cls in _resolve_classes(source):
        table = getattr(cls, "__table__", None)
        if table is None or not _table_has_fsm_column(table):
            continue
        column = _fsm_column_name(cls)
        name = fsm_check_name(table.name, column)
        existing = [c for c in list(table.constraints) if c.name == name]
        for c in existing:
            table.constraints.discard(c)
        constraint = render_check_constraint(cls)
        constraint._set_parent(table)  # type: ignore[attr-defined]
        attached.append(constraint)
    return attached


# --- alembic autogenerate comparator ---------------------------------------

_COMPARATOR_REGISTERED = False


def register_autogenerate_comparator() -> None:  # noqa: C901
    """Register a `comparators.dispatch_for('table')` hook that detects
    drift between the model's expected FSM CHECK constraint and what's in
    the database, emitting `DropConstraintOp` / `AddConstraintOp` directives.

    Safe to call multiple times — only the first call has any effect.
    """
    _require_alembic()

    global _COMPARATOR_REGISTERED
    if _COMPARATOR_REGISTERED:
        return
    _COMPARATOR_REGISTERED = True

    @_comparators.dispatch_for("table")
    def _compare_fsm_check(
        autogen_context: Any,
        modify_table_ops: Any,
        schema: Any,
        table_name: str,
        conn_table: Table | None,
        metadata_table: Table | None,
    ) -> None:
        # `metadata_table` is the model-side table (may be None if the table
        # only exists in the DB). `conn_table` is the DB-reflected table
        # (may be None if newly added). We act when the model side has an
        # FSM column.
        # For brand-new tables (conn_table is None) the constraint is
        # already inside the emitted CreateTableOp; nothing to compare.
        # For dropped tables (metadata_table is None) the DropTableOp
        # carries the cascade; also nothing to compare.
        if conn_table is None or metadata_table is None:
            return
        table = metadata_table
        if not _table_has_fsm_column(table):
            return

        column = next(c.name for c in table.columns if isinstance(c.type, FSMField))
        expected_name = fsm_check_name(table_name, column)

        expected = None
        if metadata_table is not None:
            expected = next(
                (
                    c
                    for c in metadata_table.constraints
                    if isinstance(c, CheckConstraint) and c.name == expected_name
                ),
                None,
            )

        # Look up the DB-side CHECK by name.
        insp: Inspector = autogen_context.inspector
        try:
            db_checks = insp.get_check_constraints(table_name)
        except NotImplementedError:
            return
        db = next((c for c in db_checks if c.get("name") == expected_name), None)

        if expected is None and db is None:
            return
        if (
            expected is not None
            and db is not None
            and _normalize_sqltext(str(expected.sqltext))
            == _normalize_sqltext(db.get("sqltext", ""))
        ):
            return  # in sync

        if db is not None:
            # Build a placeholder Constraint for the to-be-dropped CHECK so
            # alembic's reversibility helpers can render it. The SQL text
            # comes from the DB inspection, the name matches.
            old_constraint = CheckConstraint(db.get("sqltext", ""), name=expected_name)
            # Attach to a transient Table so the constraint has a table
            # reference (required by alembic's renderer).
            sqla_inspect_table = metadata_table
            old_constraint._set_parent(sqla_inspect_table)  # type: ignore[attr-defined]
            modify_table_ops.ops.append(
                _ops.DropConstraintOp.from_constraint(old_constraint)
            )
            # Detach so we don't leave the placeholder on the metadata table.
            sqla_inspect_table.constraints.discard(old_constraint)
        if expected is not None:
            modify_table_ops.ops.append(_ops.AddConstraintOp.from_constraint(expected))


def _normalize_sqltext(text: str) -> str:
    return " ".join(text.split()).strip().lower()
