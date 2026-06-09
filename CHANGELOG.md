# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Breaking changes
- `InvalidSourceStateError` no longer inherits from `NotImplementedError`.
  Catch with `InvalidSourceStateError` or `FSMException`.
- `events.BoundFSMDispatcher` exposes only `before_state_change` and
  `after_state_change`. Any other attribute now raises `AttributeError`
  instead of silently producing a `partial` for an SA `InstanceEvents`
  handle.
- Removed undocumented helpers from `bound`: `column_cache`,
  `BoundFSMFunction.get_call_iface_error()`, and
  `AsyncBoundFSMFunction.transition_possible_async()`. Replace with
  `bound.single_fsm_column()`, the module-level `_call_iface_error`,
  and `transition_possible()`.
- Removed `transition.sql_equality_cache` (tuple-keyed shim).
  Replace `sql_equality_cache.get_value((col, target))` with
  `sql_equality_for(col, target)`.

### Added
- `permissions=[...]` kwarg on `@transition` and `@async_transition`
  for RBAC checks, separate from `conditions`. Permissions run after
  the source-state check and before conditions; a failing permission
  raises `PermissionDeniedError`. Each callable receives the instance
  plus any `*args` / `**kwargs` forwarded from `set()` / `can_proceed()`.
- `@async_transition` decorator for handlers that need to `await`. The
  descriptor exposes `aset(...)` / `acan_proceed(...)` and an awaitable
  predicate (`await instance.<name>()`). Awaitable conditions and
  permissions are resolved with `inspect.isawaitable`, so Tasks and
  Futures work, not only bare coroutines.
- `InvalidSourceStateError`, `PreconditionError`, and
  `PermissionDeniedError` carry `.current_state`, `.target_state`, and
  `.transition_name` attributes, so callers can branch on the failure
  without parsing the message.
- `FSMField["a","b","c"]` subscript declares a closed set of legal
  states. With the typed form the package validates the transition
  graph at SA `mapper_configured` time (correctness, completeness,
  reachability from the column's `default=`) and `validate_fsm(Model)`
  is exposed for explicit invocation. The subscripted form also derives
  `length=` from the longest declared state (override via explicit
  `length=`). Bare `FSMField` skips validation and is unbounded.
- `FSMColumn` — `sa.Column` subclass that doubles as a per-column
  `@transition` namespace, so models with more than one FSM column can
  bind each transition explicitly.
- `FSMCondition` Protocol exported from the package root; the
  `conditions=` / `permissions=` kwargs use it so pyright flags
  non-callable arguments at decoration sites.
- `sqlalchemy_fsm.extras.alembic` — emit a CHECK constraint for the
  legal state set and hook Alembic autogenerate to detect drift.
  `attach_fsm_constraints(Base)` accepts a declarative base, a
  `registry`, or an iterable of mapped classes;
  `register_autogenerate_comparator()` wires up the comparator.
  Optional install: `pip install sqlalchemy-fsm[alembic]`.
- `sqlalchemy_fsm.extras.graph` — render the transition graph as
  Mermaid / Graphviz DOT / PlantUML source via `to_mermaid()`,
  `to_dot()`, `to_plantuml()`. Class-grouped transitions are flattened
  to match runtime dispatch.
- `cache.weak_key_cache` helper backing the class- and column-keyed
  caches.

### Changed
- Class-grouped transitions now distinguish "the current state matches
  no sub-handler's source set" (still `InvalidSourceStateError`) from
  "the source state matches, but no single sub-handler satisfies both
  permissions and conditions" (now `PreconditionError` with a per-sub
  breakdown naming each handler).
- `@transition` accepts any callable, not just `inspect.isfunction` —
  `functools.partial`, callable class instances, and other callables
  are valid handlers.
- `TransitionStateArithmetics.source_intersection()` returns
  `frozenset | None`; the `False` sentinel that shared truthiness with
  the empty frozenset is gone.
- `_SIGNATURE_FALLBACK` evicts in LRU order. A burst of one-off
  callables no longer wipes signatures still hot at another callsite.
- Class- and column-keyed caches are now weak-keyed, so dynamically
  built mapped classes (test factories, parametrised fixtures) aren't
  pinned for the process lifetime.

### Fixed
- `SqlAlchemyHandle` builds the event dispatcher whenever a record is
  attached, not only when it's truthy. Mapped classes overriding
  `__bool__` to return falsy now transition correctly.
- The Alembic CHECK goes through SA's expression compiler instead of
  f-string interpolation. States containing `'` (e.g. `O'Brien`) are
  escaped per the SQL standard, and reserved-word column names
  (`order`, `from`) are quoted per the active dialect — preventing
  both invalid DDL and spurious autogen drift on Postgres/MySQL.
- Alembic autogenerate emits a one-shot per-dialect warning when
  `get_check_constraints()` raises `NotImplementedError`, instead of
  silently skipping drift detection.
- `is_valid_source_state` gates the `"*"` check on
  `isinstance(value, str)`, so a non-string object whose `__eq__`
  happens to match `"*"` can't sneak through validation.
- `ClassBoundFsmTransition.__call__` raises `SetupError` with a clear
  message when invoked on the synthetic dispatcher subclass produced
  during class-based transition introspection. (It used to fall
  through to a bare `AttributeError`.)
- `BoundFSMFunction._validate_handler_iface` no longer emits a stray
  `warnings.warn` before raising `SetupError`; the diagnostic is in
  the error message.

### Internal
- New `sqlalchemy_fsm.introspection` module factors out the
  transition-graph walk shared by the validator and the extras.
- Dev deps add `pytest-asyncio`, `aiosqlite`, `greenlet`, `alembic`,
  and `hypothesis`.

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
