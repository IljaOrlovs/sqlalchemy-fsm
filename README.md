[![PyPI version](https://badge.fury.io/py/sqlalchemy-fsm.svg)](https://badge.fury.io/py/sqlalchemy-fsm)
[![CI](https://github.com/IljaOrlovs/sqlalchemy-fsm/actions/workflows/main.yml/badge.svg)](https://github.com/IljaOrlovs/sqlalchemy-fsm/actions/workflows/main.yml)

Finite state machine field for sqlalchemy
==============================================================

sqlalchemy-fsm adds declarative states management for sqlalchemy models.
Instead of adding some state field to a model and managing its values by
hand, you can use `FSMField` and mark model methods with the `transition`
decorator. Your method body contains the side effects of the state change.

The decorator also takes a list of conditions, all of which must be met
before a transition is allowed.

Requirements
------------

* Python 3.10+
* SQLAlchemy 1.4+ (2.x supported)

Installation
------------

```bash
pip install sqlalchemy-fsm
```

For local development:

```bash
pdm install                    # installs the project + dev dependencies
pdm run pytest                 # run the test suite
pdm run ruff check ./src ./tests
pdm run ruff format --check ./src ./tests
pdm run pyright                # static type check
```

Usage
-----

Add an `FSMField` to your model

```python
from sqlalchemy_fsm import FSMField, transition

class BlogPost(db.Model):
    state = db.Column(FSMField, nullable = False)
```

Use the `transition` decorator to annotate model methods

```python
@transition(source='new', target='published')
def published(self):
    """
    This function may contain side-effects,
    like updating caches, notifying users, etc.
    The return value will be discarded.
    """
```

`source` parameter accepts a list of states, or an individual state.
You can use `*` for source, to allow switching to `target` from any state.

`@transition`- annotated methods have the following API:
1. `<SqlAlchemy table class>.method()` - returns an SqlAlchemy filter condition that can be used for querying the database (e.g. `session.query(BlogPost).filter(BlogPost.published())`)
1. `<SqlAlchemy table class>.method.is_(<bool>)` - same as `<SqlAlchemy table class>.method() == <bool>`
1. `<SqlAlchemy record>.method()` - returns boolean value that tells if this particular record is in the target state for that method() (e.g. `if not blog.published():`)
1. `<SqlAlchemy record>.method.set(*args, **kwargs)` - changes the state of the record object to the transitions' target state (or raises an exception if it is not able to do so)
1. `<SqlAlchemy record>.method.can_proceed(*args, **kwargs)` - returns `True` if calling `.method.set(*args, **kwargs)` (with same `*args, **kwargs`) should succeed.

You can also use `None` as source state for (e.g. in case when the state column in nullable).
However, it is _not possible_ to create transition with `None` as target state due to religious reasons.

Transition can be also used on a class object to create a group of handlers
for same target state.

```python
@transition(target='published')
class PublishHandler(object):

    @transition(source='new')
    def do_one(self, instance, value):
        instance.side_effect = "published from new"

    @transition(source='draft')
    def do_two(self, instance, value):
        instance.side_effect = "published from draft"


class BlogPost(db.Model):
    ...
    published = PublishHandler
```

The transition is still to be invoked by calling the model's `published.set()` method.

An alternative inline class syntax is supported too:

```python
@transition(target='published')
class published(object):

    @transition(source='new')
    def do_one(self, instance, value):
        instance.side_effect = "published from new"

    @transition(source='draft')
    def do_two(self, instance, value):
        instance.side_effect = "published from draft"
```

If calling `published.set()` succeeds without raising an exception, the state field
will be changed, but not written to the database.

```python
def publish_view(request, post_id):
    post = get_object__or_404(BlogPost, pk=post_id)
    if not post.published.can_proceed():
         raise Http404;

    post.published.set()
    post.save()
    return redirect('/')
```

If your given function requires arguments to validate, you need to include them
when calling `can_proceed` as well as including them when you call the function
normally. Say `publish.set()` required a date for some reason:

```python
if not post.published.can_proceed(the_date):
    raise Http404
else:
    post.publish(the_date)
```

If your code needs to know the state model is currently in, you can just call
the main function function.

```python
if post.deleted():
    raise Http404
```

If you require some conditions to be met before changing state, use the
`conditions` argument to `transition`. `conditions` must be a list of functions
that take one argument, the model instance.  The function must return either
`True` or `False` or a value that evaluates to `True` or `False`. If all
functions return `True`, all conditions are considered to be met and transition
is allowed to happen. If one of the functions return `False`, the transition
will not happen. These functions should not have any side effects.

You can use ordinary functions

```python
def can_publish(instance):
    # No publishing after 17 hours
    if datetime.datetime.now().hour > 17:
       return False
    return True
```

Or model methods

```python
def can_destroy(self):
    return not self.is_under_investigation()
```

Use the conditions like this:

```python
@transition(source='new', target='published', conditions=[can_publish])
def publish(self):
    """
    Side effects galore
    """

@transition(source='*', target='destroyed', conditions=[can_destroy])
def destroy(self):
    """
    Side effects galore
    """
```

You can also use FSM handlers to query the database. E.g.

```python
session.query(BlogCls).filter(BlogCls.publish())
```

will return all "Blog" objects whose current state matches "publish"'es target state.

Events
------

Sqlalchemy-fsm integrates with sqlalchemy's event system.
The library exposes two events `before_state_change` and `after_state_change` that are fired up
at the expected points of state's lifecycle.

You can subscribe event listeners via standard SQLAlchemy interface of
`listens_for` or `listen`.

```python
from sqlalchemy.event import listens_for

@listens_for(Blog, 'before_state_change')
def on_state_change(instance, source, target):
    ...
```

Or

```python
from sqlalchemy import event

def on_state_change(instance, source, target):
    ...

event.listen(Blog, 'after_state_change', on_state_change)
```

It is possible to de-register an event listener call with `sqlalchemy.event.remove()` method.

Type checking
-------------

The package ships type information (PEP 561 `py.typed` marker). Pyright,
mypy, and other type checkers will pick up annotations automatically when
the package is installed.

Releasing
---------

Releases are driven by git tags:

```bash
git tag v2.1.0
git push --follow-tags
```

The release workflow runs the full CI matrix, builds the wheel + sdist
with `pdm build` (version derived from the tag via `pdm-backend`'s SCM
versioning), signs the artifacts with Sigstore, and publishes to TestPyPI
then PyPI via OIDC trusted publishing. A GitHub Release is created with
notes extracted from `CHANGELOG.md`.

How does sqlalchemy-fsm diverge from django-fsm?
------------------------------------------------

* Can't commit data from within transition-decorated functions

* Does support arguments to conditions functions
