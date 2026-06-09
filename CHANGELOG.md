# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Breaking changes
- `InvalidSourceStateError` no longer inherits from `NotImplementedError`. It
  inherits from `FSMException` only. Catch sites using
  `except NotImplementedError` to mean "invalid source state" must switch to
  `except InvalidSourceStateError` (or `FSMException`).
- Removed undocumented back-compat shims: `bound.column_cache`,
  `BoundFSMFunction.get_call_iface_error()`, and
  `AsyncBoundFSMFunction.transition_possible_async()`. Use
  `bound.single_fsm_column()`, the module-level `_call_iface_error`,
  and `transition_possible()` respectively.
- Removed `transition.sql_equality_cache` (tuple-keyed shim). Use
  `transition.sql_equality_for(column, target)` instead — same semantics.
- `events.BoundFSMDispatcher` only exposes `before_state_change` /
  `after_state_change`. Previously its `__getattr__` would silently
  return a `partial` for any SA `InstanceEvents` attribute; that
  surface is now closed.

### Added (cont.)
- `InvalidSourceStateError`, `PreconditionError`, and `PermissionDeniedError`
  now expose `.current_state`, `.target_state`, and `.transition_name`
  attributes, so callers can branch on the failure without parsing the
  error message.
- Async transitions: `await instance.<name>()` now returns the boolean
  predicate (was previously sync — `await` raised `TypeError`).
  `instance.<name>.aset(...)` and `instance.<name>.acan_proceed(...)`
  are unchanged.
- `FSMField["a","bb","ccc"]` now derives `length=` from the longest
  declared state (overridable via explicit `length=` kwarg). The plain
  unsubscripted `FSMField` remains unbounded.

### Fixed
- `SqlAlchemyHandle.__post_init__` no longer skips dispatcher creation
  when the mapped instance overrides `__bool__` to return falsy
  (`if self.record is not None:` instead of truthiness).
- Async condition / permission / handler evaluation awaits any
  `inspect.isawaitable` value (Task, Future, custom awaitables), not
  only bare coroutines. Previously a sync callable returning a `Task`
  was treated as truthy via object identity and silently passed.
- Class-grouped transitions now distinguish "no sub-handler's source
  set matches the current state" (`InvalidSourceStateError`) from
  "source state matches, but no single sub satisfies both permissions
  and conditions" (`PreconditionError` with a per-sub breakdown).
- `_make_transition` accepts any callable, not only `inspect.isfunction`
  — `functools.partial`, callable class instances, and other callables
  used to fall into the "Do not know how to" path.
- `is_valid_source_state` now gates the `"*"` comparison on
  `isinstance(value, str)` so a malicious `__eq__` can't sneak a
  non-string wildcard through validation.

### Fixed (continued)
- Alembic CHECK constraint now goes through SA's expression compiler
  rather than f-string interpolation. State strings containing
  single quotes (`O'Brien`) are escaped correctly, and column names
  that are SQL reserved words (`order`, `from`) are quoted per the
  active dialect at DDL emission time — preventing both broken DDL
  and spurious autogen drift on Postgres/MySQL.
- Alembic autogenerate now warns once per dialect when
  `get_check_constraints()` raises `NotImplementedError`, instead of
  silently disabling drift detection on backends that don't reflect
  CHECK constraints.
- `TransitionStateArithmetics.source_intersection()` returns
  `frozenset | None` (was `frozenset | bool`). The `False` sentinel
  shared truthiness with the empty frozenset and was fragile against
  refactors.
- `BoundFSMFunction._validate_handler_iface` no longer emits a
  redundant `warnings.warn` before raising `SetupError`; the
  diagnostic is folded into the error message.
- `_SIGNATURE_FALLBACK` now evicts in LRU order rather than wiping
  the entire cache on overflow.
- `ClassBoundFsmTransition.__call__` now raises `SetupError` with a
  clear message when invoked on the synthetic dispatcher class
  produced by class-based transition introspection (previously
  surfaced as a bare `AttributeError`).
- `fsm_columns_cache` and the per-column equality cache are now
  weak-keyed, so dynamically created model classes (test factories,
  parametrised fixtures) are no longer pinned for the process
  lifetime.

### Added
- `FSMCondition` Protocol exported from the package root; the
  `conditions=` / `permissions=` kwargs are typed against it so
  pyright catches non-callable mistakes at decoration sites.
- `cache.weak_key_cache` helper.

### Added
- `permissions=[...]` kwarg on `@transition` for RBAC checks, separate from
  `conditions`. Each callable receives `(instance, *args, **kwargs)` forwarded
  from `set()` / `can_proceed()`. A failing permission raises
  `PermissionDeniedError`; checks run after source-state and before conditions.
- `PermissionDeniedError` (in `sqlalchemy_fsm.exc`).
- `sqlalchemy_fsm.extras.graph` — render a model's transition graph as
  Mermaid / Graphviz DOT / PlantUML source via `to_mermaid()`, `to_dot()`,
  `to_plantuml()`. Class-grouped transitions are flattened to match
  runtime dispatch.
- `AsyncSession` support is now verified end-to-end (events, conditions,
  permissions, class-bound query helpers) via `tests/test_async.py`.
  No runtime change — `@transition` was already async-safe — but the
  README now documents the supported usage.

- `sqlalchemy_fsm.extras.alembic` — render and autogenerate `CHECK` constraints
  for FSM-managed columns. `attach_fsm_constraints(Base)` accepts a declarative
  base, a `registry`, or an iterable of mapped classes; `register_autogenerate_comparator()`
  hooks Alembic's autogenerate to emit drop/add ops when the legal state set
  changes. Available as an optional install: `pip install sqlalchemy-fsm[alembic]`.

- `FSMField["a", "b", "c"]` subscript syntax declares a closed set of legal
  states. When present, the package validates the model's transition graph
  at SA `mapper_configured` time and raises `SetupError` on:
  - unknown state (transition references a state not in the declared set),
  - incomplete coverage (declared state never referenced),
  - or an unreachable state (no forward path from the column's `default=`).
  Wildcards (`source="*"`) count as edges from every declared state.
  Plain `FSMField` (no subscript) remains supported and skips validation.
  `validate_fsm(Model)` is also exposed for explicit invocation.

### Internal
- Dev deps gain `pytest-asyncio`, `aiosqlite`, `greenlet`, and `alembic` for
  async + Alembic integration tests.
- New `sqlalchemy_fsm.introspection` module factors out the transition-graph
  walk shared by the validator and the optional extras.

## [2.2.0] - 2026-06-08

### Added
- PEP 561 `py.typed` marker — downstream type checkers (pyright, mypy) now
  pick up the bundled annotations automatically.
- Full type annotations across the public API; `DictCache` and `InstanceRef`
  are now generic (`DictCache[K, V]`, `InstanceRef[T]`).
- Property-based tests with Hypothesis covering predicates, `FSMMeta`
  normalization, and `DictCache` invariants.
- Border-case and misconfiguration tests (`tests/test_edge_cases.py`).
- CI: ruff (lint + format), pyright (type check), and an OIDC trusted-
  publishing release workflow with Sigstore signing.

### Changed
- **Build system:** migrated from `setup.py`/`setup.cfg` to PDM + `pdm-backend`
  with SCM-based dynamic versioning. `setup.py`, `setup.cfg`, `tox.ini`,
  `requirements/`, `bin/release.sh`, `.travis.yml`, and `.pyup.yml` removed.
- **Python:** dropped support for Python < 3.10; the `six` dependency is gone.
- **SQLAlchemy:** added compatibility shim for the `HYBRID_METHOD` rename in
  SQLAlchemy 2.0 (`HybridExtensionType.HYBRID_METHOD`). The package now works
  on both 1.4.x and 2.x.
- `SqlAlchemyHandle`, `DictCache`, `InstanceRef`, and `TransitionStateArithmetics`
  are now `@dataclass(slots=True)` classes.
- Replaced runtime `assert` statements with real exceptions (`SetupError`,
  `InvalidSourceStateError`) so they survive `python -O`.
- Renamed internal `TansitionStateArtithmetics` → `TransitionStateArithmetics`;
  fixed misspellings in error messages (`beteen`, `compatable`, `preconditons`).

### Removed
- `six` dependency.
- `setup.py`, `setup.cfg`, `tox.ini`, `requirements/`, `bin/release.sh`,
  `.travis.yml`, `.pyup.yml`, `sqlalchemy-fsm.sublime-project`.

## [2.0.13] — 2022-08

Last release on the legacy `setup.py` toolchain.
