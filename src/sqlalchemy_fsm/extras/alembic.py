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
from sqlalchemy import column as sa_column
from sqlalchemy import inspect as sqla_inspect

from ..introspection import collect_transition_states
from ..sqltypes import FSMField

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy import Table
    from sqlalchemy.engine import Inspector
    from sqlalchemy.sql.elements import ColumnElement

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
    if not _ALEMBIC_AVAILABLE:  # pragma: no cover - alembic is a test dependency
        raise RuntimeError(
            "alembic is not installed. Install with: pip install sqlalchemy-fsm[alembic]"
        ) from _ALEMBIC_IMPORT_ERR


# --- core state extraction --------------------------------------------------


def collect_states(model_cls: type, column: Any = None) -> set[str]:
    """Return the closed set of legal states declared by a model's transitions.

    Walks every `@transition` on `model_cls` (including sub-handlers of
    class-grouped transitions) and unions their concrete `source` / `target`
    states. Wildcards (`"*"`) and `None` sources are excluded — they aren't
    constants you can pin in a CHECK constraint.

    If `column` is given, only consider transitions bound to it.
    """
    return collect_transition_states(model_cls, column=column)


def _fsm_columns(model_cls: type) -> list[Any]:
    """Every FSMField column on this mapped class, in declaration order."""
    return [c for c in sqla_inspect(model_cls).columns if isinstance(c.type, FSMField)]


def _fsm_column_name(model_cls: type) -> str:
    """Discover the name of the (sole) FSMField column on this mapped class."""
    cols = _fsm_columns(model_cls)
    if not cols:
        raise ValueError(f"No FSMField column on {model_cls!r}")
    return cols[0].name


def fsm_check_name(table_name: str, column_name: str) -> str:
    """Deterministic CHECK constraint name. Stable across runs so Alembic can
    match metadata and DB by name."""
    return f"ck_{table_name}_{column_name}_fsm"


def _check_expression(column_name: str, states: Iterable[str]) -> ColumnElement:
    """Build the CHECK body as a SA expression.

    Hands literal escaping and identifier quoting off to SA's compiler —
    so a state like ``"O'Brien"`` round-trips safely, and a column named
    ``order`` is quoted per the active dialect at DDL emission time. The
    sort is purely so the rendered SQL is deterministic across runs.
    """
    return sa_column(column_name).in_(sorted(states))


def render_check_constraint(model_cls: type, column: Any = None) -> CheckConstraint:
    """Build (without attaching) the `CheckConstraint` for one FSM column.

    `column` may be a `Column` instance or `None` (uses the sole FSM
    column on the model).
    """
    cols = _fsm_columns(model_cls)
    if not cols:
        raise ValueError(f"No FSMField column on {model_cls!r}")
    if column is None:
        column = cols[0]
    states = collect_states(model_cls, column=column if len(cols) > 1 else None)
    table_name = sqla_inspect(model_cls).local_table.name
    return CheckConstraint(
        _check_expression(column.name, states),
        name=fsm_check_name(table_name, column.name),
    )


def render_check_constraints(model_cls: type) -> list[CheckConstraint]:
    """One `CheckConstraint` per FSM column on the model."""
    return [render_check_constraint(model_cls, col) for col in _fsm_columns(model_cls)]


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
        for col in _fsm_columns(cls):
            name = fsm_check_name(table.name, col.name)
            existing = [c for c in list(table.constraints) if c.name == name]
            for c in existing:
                table.constraints.discard(c)
            constraint = render_check_constraint(cls, col)
            constraint._set_parent(table)  # type: ignore[attr-defined]
            attached.append(constraint)
    return attached


# --- alembic autogenerate comparator ---------------------------------------

def compare_fsm_check(
    autogen_context: Any,
    modify_table_ops: Any,
    schema: Any,
    table_name: str,
    conn_table: Table | None,
    metadata_table: Table | None,
) -> None:
    """Comparator body — exposed so it can be unit-tested directly.

    `metadata_table` is the model-side table (may be None if the table only
    exists in the DB). `conn_table` is the DB-reflected table (may be None
    if newly added). For brand-new tables (`conn_table is None`) the
    constraint is already inside the emitted `CreateTableOp`; nothing to
    compare. For dropped tables (`metadata_table is None`) the
    `DropTableOp` carries the cascade; also nothing to compare.
    """
    if conn_table is None or metadata_table is None:
        return
    if not _table_has_fsm_column(metadata_table):
        return

    insp: Inspector = autogen_context.inspector
    try:
        db_checks = insp.get_check_constraints(table_name)
    except NotImplementedError:
        return

    dialect = _resolve_dialect(autogen_context, insp)

    for col in (c for c in metadata_table.columns if isinstance(c.type, FSMField)):
        expected_name = fsm_check_name(table_name, col.name)
        expected = next(
            (
                c
                for c in metadata_table.constraints
                if isinstance(c, CheckConstraint) and c.name == expected_name
            ),
            None,
        )
        db = next((c for c in db_checks if c.get("name") == expected_name), None)

        if expected is None and db is None:
            continue
        if (
            expected is not None
            and db is not None
            and _normalize_sqltext(_render_sqltext(expected.sqltext, dialect))
            == _normalize_sqltext(db.get("sqltext", ""))
        ):
            continue  # in sync

        if db is not None:
            old_constraint = CheckConstraint(db.get("sqltext", ""), name=expected_name)
            old_constraint._set_parent(metadata_table)  # type: ignore[attr-defined]
            modify_table_ops.ops.append(
                _ops.DropConstraintOp.from_constraint(old_constraint)
            )
            metadata_table.constraints.discard(old_constraint)
        if expected is not None:
            modify_table_ops.ops.append(_ops.AddConstraintOp.from_constraint(expected))


_COMPARATOR_REGISTERED = False


def register_autogenerate_comparator() -> None:
    """Register `compare_fsm_check` with alembic's `'table'` dispatch.

    Safe to call multiple times — only the first call has any effect.
    """
    _require_alembic()

    global _COMPARATOR_REGISTERED
    if _COMPARATOR_REGISTERED:
        return
    _COMPARATOR_REGISTERED = True

    _comparators.dispatch_for("table")(compare_fsm_check)


def _normalize_sqltext(text: str) -> str:
    return " ".join(text.split()).strip().lower()


def _resolve_dialect(autogen_context: Any, insp: Any) -> Any:
    """Best-effort dialect lookup for compiling the model-side sqltext.

    Alembic's ``MigrationContext`` exposes ``dialect`` directly; older
    or mocked contexts may not. Fall back to ``insp.bind.dialect``, then
    to a default dialect — at worst we lose dialect-specific identifier
    quoting in the comparison, which only matters for reserved-word
    column names.
    """
    dialect = getattr(autogen_context, "dialect", None)
    if dialect is not None:
        return dialect
    bind = getattr(insp, "bind", None)
    if bind is not None:
        return bind.dialect
    from sqlalchemy.engine import default

    return default.DefaultDialect()


def _render_sqltext(sqltext: Any, dialect: Any) -> str:
    """Compile a CHECK body to dialect-specific SQL with literals inlined.

    Falls back to ``str()`` if the value isn't a SA expression (legacy
    callers that built CHECKs from raw strings) — those stay subject to
    whatever escaping they applied themselves.
    """
    compile_fn = getattr(sqltext, "compile", None)
    if compile_fn is None:
        return str(sqltext)
    return str(compile_fn(dialect=dialect, compile_kwargs={"literal_binds": True}))
