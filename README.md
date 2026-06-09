[![PyPI version](https://img.shields.io/pypi/v/sqlalchemy-fsm.svg)](https://pypi.org/project/sqlalchemy-fsm/)
[![Python versions](https://img.shields.io/pypi/pyversions/sqlalchemy-fsm.svg)](https://pypi.org/project/sqlalchemy-fsm/)
[![CI](https://github.com/IljaOrlovs/sqlalchemy-fsm/actions/workflows/main.yml/badge.svg)](https://github.com/IljaOrlovs/sqlalchemy-fsm/actions/workflows/main.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Downloads](https://static.pepy.tech/badge/sqlalchemy-fsm/month)](https://pepy.tech/project/sqlalchemy-fsm)

# sqlalchemy-fsm

Declarative finite state machine for SQLAlchemy models. Add an `FSMField`
column, decorate methods with `@transition`, and let the library enforce
which transitions are reachable from which states.

## Requirements

Python 3.10+, SQLAlchemy 2.0+.

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
| `post.publish.is_current` | `bool` — is this instance currently in the target state? Equivalent to `post.state == "<target>"` but reads the target off the transition itself. |
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

The subscripted form derives `length=` as `longest_state * 3` so the
column has headroom for a renamed or longer state later without a
column-width migration. Override with an explicit `length=` if you
want a tighter or wider bound.

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
`before_transition` and `after_transition` around every transition.
Listeners receive the row, the transition method name, the source
and target states, and the `*args` / `**kwargs` passed to `set()` /
`aset()`:

```python
from sqlalchemy.event import listens_for

@listens_for(BlogPost, "after_transition")
def audit(instance, transition_name, source, target, args, kwargs):
    log.info(
        "%s: %s -> %s via %s by %s",
        instance.id, source, target, transition_name, kwargs.get("user"),
    )
```

For class-grouped transitions, `transition_name` is the outer (public)
name — `"publish"`, not the sub-handler method like `"from_draft"`.

Remove with `sqlalchemy.event.remove(...)`.

**Listeners must be plain (non-async) functions.** SQLAlchemy's
`InstanceEvents` dispatch is synchronous; an `async def` listener
returns a coroutine that nothing awaits, so its body silently doesn't
run. Wrap async work in `asyncio.create_task(...)` if you need it.

**`before_transition` runs before the handler and before the column
is mutated.** Raising from it cleanly aborts the transition — state
is unchanged. **`after_transition` runs *after* the handler has
returned and *after* the column has been mutated.** If an
after-listener raises, the in-memory state has already been
overwritten and won't be rolled back; the exception still propagates
to the caller. Treat `after_transition` as best-effort notification,
not a transactional gate.

## Transition metadata

`@transition(custom={...})` attaches a free-form dict to the
transition — sqlalchemy-fsm ignores it, but admin UIs, RBAC layers,
docs generators, etc. can read it via `Model.attr.meta.custom`:

```python
class BlogPost(Base):
    ...
    @transition(
        source="draft", target="published",
        custom={"label": "Publish post", "icon": "rocket", "groups": ["editor"]},
    )
    def publish(self): ...

BlogPost.publish.meta.custom["label"]   # "Publish post"
```

The dict is copied and frozen on decoration, so callers can't mutate
it after the fact.

## Available transitions

`available_transitions(instance, *args, **kwargs)` returns the
transitions whose source matches the instance's current state AND
whose permissions and conditions accept these args — useful for
rendering "what can this user do with this row right now?" action
lists in a UI:

```python
from sqlalchemy_fsm import available_transitions

for name, fsm_t in available_transitions(post, user=current_user):
    print(name, fsm_t.meta.target, fsm_t.meta.custom.get("label"))
```

`aavailable_transitions(...)` is the async sibling — it awaits
`acan_proceed` on `@async_transition` decorators and stays sync for
the rest. Pass `column=` on multi-column models to filter to one
state machine.

## Testing transitions

Every `@transition`-decorated attribute exposes the raw handler as
`.fn` for tests that want to call or mock it without going through
the state machinery.

**Call the handler directly.** Bypasses source-state, permission,
and condition checks — useful when the body has its own side effects
worth testing in isolation:

```python
def test_publish_sends_notification(mocker):
    post = BlogPost()
    spy = mocker.spy(notifications, "send")
    BlogPost.publish.fn(post)             # runs body, no guards, no mutation
    spy.assert_called_once_with(post)
```

`.fn` is the same callable on the class-bound (`BlogPost.publish.fn`)
and instance-bound (`post.publish.fn`) wrappers.

**Replace the handler with a mock.** Reach the underlying descriptor
via `sqlalchemy_fsm.testing.get_transition(Model, name)` and assign
`.fn`:

```python
from sqlalchemy_fsm.testing import get_transition

def test_publish_runs_through_caller(monkeypatch):
    descriptor = get_transition(BlogPost, "publish")
    monkeypatch.setattr(descriptor, "fn", lambda self: None)

    post = BlogPost()
    Service(post).do_publish()
    assert post.state == "published"   # guards + state mutation still ran
```

The descriptor is the stable target — `BlogPost.publish` itself
rebuilds a thin wrapper on every attribute access (so SA filter
expressions stay clean), but the wrapper reads `fn` from the
descriptor each time, so the patch propagates.

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
doc.publish.is_current                   # bool: is the row in 'published'? (sync, no await)
```

Sync callables stay valid inside `@async_transition` — anything
awaitable (coroutine, Task, Future) is resolved, anything else is taken
as a value. Mixing sync and async **sub-handlers** under one
class-grouped transition is rejected at decoration time.

The class-bound query helper (`AsyncDoc.publish()` at the class level)
is a plain SA expression and composes with
`select(...).where(...)` against an `AsyncSession` identically to the
sync case. Events (`before_transition` / `after_transition`) fire
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

## Comparison with django-fsm

Same shape — state column plus `@transition` methods — applied to
SQLAlchemy instead of Django. ([django-fsm] is archived since 2024;
[django-fsm-2] is the maintained drop-in fork.)

[django-fsm]: https://github.com/viewflow/django-fsm
[django-fsm-2]: https://github.com/django-commons/django-fsm-2

| | sqlalchemy-fsm | django-fsm |
|---|---|---|
| ORM | SQLAlchemy 2.x | Django |
| State types | String | String, int, FK |
| Declared state set | `FSMField["a","b","c"]` | Free-form |
| Startup graph validation | Correctness, completeness, reachability — `SetupError` at import | None — typo'd `target=` silently assigns |
| DB constraint | `CHECK (col IN (...))` via Alembic extra, autogen diff | None |
| `@transition` kwargs | `source`, `target`, `conditions`, `permissions`, `custom` | + `on_error`, `permission` (singular) |
| Condition signature | `(instance, *args, **kwargs)` forwarded from `set()` | `(instance)` |
| Permissions | List of callables; receive `set()` kwargs | One: Django perm string or `(instance, user) -> bool` |
| Optimistic locking | Use SA `version_id_col` | `ConcurrentTransitionMixin` filters UPDATE by loaded state |
| Async | `@async_transition`, `aset` / `acan_proceed`, mixed sync/async checks | None |
| Events | SA `before_transition` / `after_transition` (instance, name, source, target, args, kwargs) | Django signals `pre_transition` / `post_transition` (same payload) |
| Available-transition helper | `available_transitions(instance, *args, **kwargs)` and async sibling | `get_available_<field>_transitions(user)` |
| Dynamic target | Class-grouped transitions (dispatch by source) | `RETURN_VALUE(...)` / `GET_STATE(...)` |
| Proxy class per state | None | `state_choices=` swaps `__class__` |
| Block direct writes | No (`obj.state = "x"` always works) | `protected=True` |
| Graph export | Pure Python: `to_mermaid` / `to_dot` / `to_plantuml` | `manage.py graph_transitions` (graphviz extra) |
| Admin | n/a | `FSMAdminMixin`, unfold contrib (django-fsm-2) |
| Introspection helpers | `iter_transitions`, `collect_edges` | `get_available_*_transitions(user)` on instance |

Neither library wraps the handler in a transaction — the caller
commits.

### Notes on the bigger differences

- **`FSMField["a","b","c"]`** declares the legal set. At
  `mapper_configured` time, every `source=` / `target=` must be in
  it, every declared state must be used, every state must be
  reachable from `default=`. `target="publsihed"` fails at import.
  Plain `FSMField` skips validation.
- **Alembic extra** emits and diffs `ck_<table>_<col>_fsm`. django-fsm
  state lives only in Python; a stray `UPDATE` from psql can write
  anything.
- **Kwargs threaded through checks.** `post.publish.set(user=u)`
  reaches every permission and condition. django-fsm conditions get
  only the instance; threading context means closures.
- **No `permission=` string.** No auth framework to defer to — pass
  callables and decide what to check.
- **No `on_error=`.** Model failures as an explicit transition you
  call, not a magic side-effect of a raise.
- **Async transitions work under `AsyncSession`.** Sync `.set()` too
  — it just mutates an attribute, so it composes with `await
  session.commit()`.

### What this doesn't have

- `RETURN_VALUE` / `GET_STATE` — use class-grouped transitions, or
  set the attribute in the handler.
- `state_choices=` proxy classes.
- Integer or FK state columns. An enum with `__str__` works; ints
  need a custom SA type.
- `protected=True`. Bare attribute writes aren't gated; the CHECK
  constraint catches the bad value at commit.
- Admin integration.

### Migrating from django-fsm

| django-fsm | sqlalchemy-fsm |
|---|---|
| `FSMField(default="draft", protected=True)` | `sa.Column(FSMField["draft", …], default="draft", nullable=False)` |
| `@transition(field=state, source="x", target="y")` | `@transition(source="x", target="y")` (`column=` only if >1 FSMField) |
| `permission="app.publish"` | `permissions=[lambda inst, user=None, **_: user and user.has_perm("app.publish")]` |
| `condition(instance)` | `condition(instance, *args, **kwargs)` |
| `instance.do_x(); instance.save()` | `instance.do_x.set(); session.commit()` |
| `pre_transition` / `post_transition` | `event.listen(Model, "before_transition" \| "after_transition", fn)` — listener gets `(instance, transition_name, source, target, args, kwargs)` |
| `get_available_<field>_transitions(user)` | `available_transitions(instance, user=user)` (or `aavailable_transitions` for async) |
| `custom={"label": …}` | `custom={"label": …}` — read via `Model.attr.meta.custom` |
| `ConcurrentTransitionMixin` | `version_id_col` on the mapper, or `SELECT … FOR UPDATE` |
| `RETURN_VALUE("a", "b")` | Class-grouped transition with sub-handlers |
| `manage.py graph_transitions` | `print(to_mermaid(Model))` |
