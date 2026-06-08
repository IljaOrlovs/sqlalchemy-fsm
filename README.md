[![PyPI version](https://badge.fury.io/py/sqlalchemy-fsm.svg)](https://badge.fury.io/py/sqlalchemy-fsm)
[![CI](https://github.com/IljaOrlovs/sqlalchemy-fsm/actions/workflows/main.yml/badge.svg)](https://github.com/IljaOrlovs/sqlalchemy-fsm/actions/workflows/main.yml)

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

> Note: `target=None` is not supported — every transition must declare an
> explicit target state.

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

- Cannot commit data from inside a transition handler.
- Condition callables accept arguments forwarded from `set()`.
