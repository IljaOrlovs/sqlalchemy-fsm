# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
