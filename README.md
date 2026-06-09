[![PyPI version](https://img.shields.io/pypi/v/sqlalchemy-fsm.svg)](https://pypi.org/project/sqlalchemy-fsm/)
[![Python versions](https://img.shields.io/pypi/pyversions/sqlalchemy-fsm.svg)](https://pypi.org/project/sqlalchemy-fsm/)
[![CI](https://github.com/IljaOrlovs/sqlalchemy-fsm/actions/workflows/main.yml/badge.svg)](https://github.com/IljaOrlovs/sqlalchemy-fsm/actions/workflows/main.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Downloads](https://static.pepy.tech/badge/sqlalchemy-fsm/month)](https://pepy.tech/project/sqlalchemy-fsm)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Checked with pyright](https://microsoft.github.io/pyright/img/pyright_badge.svg)](https://microsoft.github.io/pyright/)

# sqlalchemy-fsm

Declarative finite state machine for SQLAlchemy models. Add an `FSMField`
column, decorate methods with `@transition`, and let the library enforce
which transitions are reachable from which states.

## Requirements

Python 3.10+, SQLAlchemy 1.4+ (2.x supported).

## Install

```bash
pip install sqlalchemy-fsm
```

## Quickstart

```python
import sqlalchemy as sa
from sqlalchemy.orm import declarative_base
from sqlalchemy_fsm import FSMField, transition

Base = declarative_base()


class BlogPost(Base):
    __tablename__ = "blog_post"
    id = sa.Column(sa.Integer, primary_key=True)
    state = sa.Column(FSMField, nullable=False, default="draft")

    @transition(source="draft", target="published")
    def publish(self):
        """Side effects of publishing go here (notifications, cache busts, …).
        The return value is discarded."""

    @transition(source=["draft", "published"], target="archived")
    def archive(self):
        ...


post = BlogPost()
post.publish.can_proceed()   # True — we're in 'draft'
post.publish.set()           # state is now 'published'
post.publish.set()           # raises InvalidSourceStateError
```

`source` accepts a single state, a list of states, `"*"` (any state), or
`None` (matches a nullable column's NULL).

## Transition API

For a transition decorated as `BlogPost.publish`:

| Expression | Returns |
|---|---|
| `BlogPost.publish()` | SQLAlchemy filter for rows in the transition's target state — use in `.filter(...)`. |
| `BlogPost.publish.is_(True)` | Equivalent to `BlogPost.publish() == True`. |
| `post.publish()` | `bool` — is this instance currently in the target state? |
| `post.publish.set(*args, **kwargs)` | Execute the transition. Raises `InvalidSourceStateError` if the current state isn't allowed, or `PreconditionError` if any condition returns falsy. |
| `post.publish.can_proceed(*args, **kwargs)` | `bool` — would `set()` succeed right now? |

`set()` mutates the field in memory; commit the session yourself to persist.

Top-level `@transition` must declare an explicit `target=` state.
Sub-handlers inside a class-grouped transition may omit `target=` to
inherit it from the enclosing class.

## Conditions

Pass callables to `conditions` to gate the transition. Each is called with
the instance (plus any args forwarded from `set()` / `can_proceed()`) and
must return truthy.

```python
def can_publish(instance) -> bool:
    return datetime.now().hour <= 17

class BlogPost(Base):
    ...
    @transition(source="draft", target="published", conditions=[can_publish])
    def publish(self):
        ...

# can_proceed() must receive the same args you'd pass to set():
post.publish.can_proceed()   # checks conditions without mutating
post.publish.set()
```

Conditions must be side-effect-free — `can_proceed()` evaluates them too.

## Declared states & startup validation

The subscript form `FSMField["a", "b", "c"]` declares the closed set of
legal states. When present, the package validates the transition graph
at SA mapper-configuration time and raises `SetupError` if it doesn't
match:

```python
class BlogPost(Base):
    __tablename__ = "blog_post"
    id = sa.Column(sa.Integer, primary_key=True)
    state = sa.Column(
        FSMField["draft", "published", "archived"],
        nullable=False,
        default="draft",
    )

    @transition(source="draft", target="published")
    def publish(self): ...

    @transition(source=["draft", "published"], target="archived")
    def archive(self): ...
```

Three properties are checked:

- **Correct** — every state referenced by a transition is in the
  declared set. (Catches typos like `target="publsihed"`.)
- **Complete** — every declared state is used somewhere (the column's
  `default=` counts as a use).
- **Reachable** — every declared state is reachable along forward
  edges from the column's `default=`. (`source="*"` wildcards count as
  edges from every declared state.)

A typed `FSMField[...]` column must declare a scalar `default=<state>`
so reachability can be evaluated. If your FSM genuinely starts from
NULL (the row is inserted with no state set, and the first transition
assigns one), either declare a sentinel state like
`"uninitialized"` and use it as the `default=`, or drop to the plain
`FSMField` (no subscript), which skips validation entirely.

Call `sqlalchemy_fsm.validate_fsm(MyModel)` explicitly if you want to
run the check yourself (e.g. from a unit test).

## Permissions (RBAC)

`permissions=` accepts callables that gate the transition for authorization,
separately from `conditions`. They run **after** the source-state check and
**before** `conditions`. A failing permission raises `PermissionDeniedError`
from `set()`; `can_proceed()` returns `False`.

```python
from sqlalchemy_fsm.exc import PermissionDeniedError

def is_editor(instance, user=None, **_):
    return getattr(user, "role", None) == "editor"

class Doc(Base):
    ...
    @transition(source="draft", target="published", permissions=[is_editor])
    def publish(self, user=None):
        ...

doc.publish.can_proceed(user=current_user)
doc.publish.set(user=current_user)   # raises PermissionDeniedError if not allowed
```

Each callable receives the instance plus any args forwarded from
`set()` / `can_proceed()` — pass `user=` (or whatever you need) explicitly.
All listed permissions must pass.

## Class-grouped transitions

To branch on the source state with different handlers, decorate a class:

```python
@transition(target="published")
class publish:
    @transition(source="draft")
    def from_draft(self, instance):
        instance.published_via = "fresh"

    @transition(source="archived")
    def from_archive(self, instance):
        instance.published_via = "republish"
```

Invocation is still `post.publish.set()` — the right sub-handler is picked
by the current state.

## Query helpers

Use the class-bound form inside `.filter()`:

```python
session.query(BlogPost).filter(BlogPost.publish())          # currently 'published'
session.query(BlogPost).filter(~BlogPost.publish())         # everything else
```

## Events

The library hooks into SQLAlchemy's event system and emits
`before_state_change` and `after_state_change` per transition:

```python
from sqlalchemy.event import listens_for

@listens_for(BlogPost, "after_state_change")
def on_change(instance, source, target):
    ...
```

Remove with `sqlalchemy.event.remove(...)`.

**Listeners must be plain (non-async) functions.** SQLAlchemy's
`InstanceEvents` dispatch is synchronous; an `async def` listener
returns a coroutine that nothing awaits, so its body silently doesn't
run. Wrap async work in `asyncio.create_task(...)` if you need it.

**`before_state_change` runs before the handler and before the column
is mutated.** Raising from it cleanly aborts the transition — state is
unchanged. **`after_state_change` runs *after* the handler has
returned and *after* the column has been mutated.** If an after-listener
raises, the in-memory state has already been overwritten and won't be
rolled back; the exception still propagates to the caller. Treat
`after_state_change` as best-effort notification, not a transactional
gate.

## Async (SQLAlchemy 2.x `AsyncSession`)

Two modes, used together or separately:

**Sync `@transition` under `AsyncSession`.** A sync transition mutates an
attribute — it does not touch the session — so it works unchanged under
an async engine. Call `.set()` as usual; await the commit yourself.

```python
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

engine = create_async_engine("postgresql+asyncpg://…")

async with AsyncSession(engine) as session:
    doc = AsyncDoc()
    session.add(doc)
    doc.publish.set()           # synchronous mutation
    await session.commit()      # async persistence
```

**`@async_transition` for awaiting inside the handler.** Use it when the
handler, a condition, or a permission needs to `await` something. The
descriptor exposes `aset(...)` and `acan_proceed(...)`, and the
predicate `instance.<name>()` is also a coroutine, so the async surface
mirrors the sync one:

```python
from sqlalchemy_fsm import async_transition

async def is_editor(instance, user=None, **_):
    return await user.has_role("editor")

class AsyncDoc(Base):
    ...
    @async_transition(source="draft", target="published",
                      permissions=[is_editor])
    async def publish(self, user=None):
        await notify_subscribers(self)

await doc.publish.acan_proceed(user=u)   # bool
await doc.publish.aset(user=u)           # executes
await doc.publish()                      # bool: is the row in 'published'?
```

Sync callables stay valid inside `@async_transition` — anything
awaitable (coroutine, Task, Future) is resolved, anything else is taken
as a value. Mixing sync and async **sub-handlers** under one
class-grouped transition is rejected at decoration time.

The class-bound query helper (`AsyncDoc.publish()` at the class level)
is a plain SA expression and composes with
`select(...).where(...)` against an `AsyncSession` identically to the
sync case. Events (`before_state_change` / `after_state_change`) fire
normally; their listeners must still be sync (see Events above).

## Alembic integration

`sqlalchemy_fsm.extras.alembic` renders the set of legal states as a
CHECK constraint on the underlying column, and registers an Alembic
comparator that detects drift between the model and the database. Install
with the optional extra: `pip install sqlalchemy-fsm[alembic]`.

In your `env.py`:

```python
from sqlalchemy_fsm.extras.alembic import (
    attach_fsm_constraints,
    register_autogenerate_comparator,
)

attach_fsm_constraints(Base)         # accepts a Base / registry / list of classes
register_autogenerate_comparator()   # hook into `alembic revision --autogenerate`

context.configure(target_metadata=Base.metadata, ...)
```

After this, adding or removing a `@transition` that changes the state set
will show up in the next autogenerated migration as a paired
`drop_constraint` + `create_check_constraint` for the `ck_<table>_<col>_fsm`
constraint.

If you only want the constraint and not the comparator, call
`attach_fsm_constraints(Base)` alone — new tables will be created with the
CHECK, but changes to the state list on existing tables won't be detected
automatically.

## Diagram export

`sqlalchemy_fsm.extras.graph` renders a model's transition graph as
Mermaid / Graphviz DOT / PlantUML source — useful for embedding in docs
or generating an SVG with the respective tool.

```python
from sqlalchemy_fsm.extras.graph import to_mermaid, to_dot, to_plantuml

print(to_mermaid(BlogPost))   # stateDiagram-v2 ... (renders on GitHub)
print(to_dot(BlogPost))       # pipe through `dot -Tsvg`
print(to_plantuml(BlogPost))
```

`source="*"` is emitted as a synthetic `(any)` node (or `[*]` in PlantUML).
Class-grouped transitions are flattened so the rendered edges match
runtime dispatch.

## Type checking

The package ships type information (PEP 561 `py.typed`). pyright / mypy
pick up annotations automatically once installed.

## Development

```bash
pdm install                                 # project + dev deps
pdm run pytest                              # tests
pdm run ruff check ./src ./tests            # lint
pdm run ruff format --check ./src ./tests   # format check
pdm run pyright                             # type check
```

## Releasing

Tagged commits drive releases:

```bash
git tag v2.1.0
git push --follow-tags
```

CI runs the matrix, `pdm-backend` derives the version from the tag, the
artifacts are Sigstore-signed and published to TestPyPI then PyPI via
OIDC trusted publishing. A GitHub Release is created with notes from
[CHANGELOG.md](CHANGELOG.md).

## How does this differ from django-fsm?

- The transition handler does not own the database transaction — the
  caller commits the session after `set()` returns. No implicit
  `transaction.atomic` wrapping.
- Conditions and permissions receive the same `*args` / `**kwargs` you
  pass to `set()` / `can_proceed()` (after the instance), so you can
  thread caller-supplied context like `user=` through every check.
- States can be declared up-front as a closed set
  (`FSMField["a","b","c"]`) and the transition graph is validated at
  SA mapper-configuration time. Alembic autogenerate can emit and
  diff a matching CHECK constraint.
