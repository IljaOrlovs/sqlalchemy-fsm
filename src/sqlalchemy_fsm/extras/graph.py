"""Render FSM transitions as Mermaid / Graphviz DOT / PlantUML diagrams.

The renderers walk a SQLAlchemy model class for `@transition`-decorated
attributes and emit a textual diagram. Output is deterministic (edges
sorted by name) so the result is safe to snapshot or commit.

```python
from sqlalchemy_fsm.extras.graph import to_mermaid

print(to_mermaid(BlogPost))
```

The renderers do not depend on Graphviz / PlantUML being installed —
they produce the textual source format only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .. import bound as _bound
from ..transition import FsmTransition

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ..meta import FSMMeta

# Synthetic node label used to render `source="*"` (wildcard) edges.
WILDCARD_NODE = "(any)"


@dataclass(frozen=True)
class Edge:
    """One drawable transition: a single (source, target) pair plus label."""

    source: str | None
    target: str
    label: str

    @property
    def display_source(self) -> str:
        if self.source == "*":
            return WILDCARD_NODE
        if self.source is None:
            return "(none)"
        return self.source


def collect_edges(model_cls: type) -> list[Edge]:
    """Discover every transition declared on `model_cls` and flatten to edges.

    For class-grouped transitions (`@transition(...) class foo: ...`), sources
    from sub-handlers are merged with the parent meta via the same logic the
    runtime uses, so the rendered edges match dispatch behaviour.
    """
    edges: list[Edge] = []
    for name in dir(model_cls):
        attr = _get_class_attr(model_cls, name)
        if not isinstance(attr, FsmTransition):
            continue
        meta = attr.meta
        if meta.bound_cls is _bound.BoundFSMClass:
            edges.extend(_edges_from_class_transition(name, meta, attr.set_fn))
        else:
            edges.extend(_edges_from_meta(name, meta))
    edges.sort(key=lambda e: (e.label, e.display_source, e.target))
    return edges


def _get_class_attr(cls: type, name: str) -> object:
    """Read a class attribute without triggering descriptors (`__get__`).

    `FsmTransition` is a descriptor — a plain `getattr(cls, name)` would
    return a `ClassBoundFsmTransition` wrapper, hiding the meta.
    """
    for klass in cls.__mro__:
        if name in klass.__dict__:
            return klass.__dict__[name]
    return None


def _edges_from_meta(label: str, meta: FSMMeta) -> list[Edge]:
    target = meta.target
    if target is None:
        return []
    return [Edge(source=src, target=target, label=label) for src in meta.sources]


def _edges_from_class_transition(
    label: str, parent_meta: FSMMeta, wrapped_cls: object
) -> list[Edge]:
    """Merge a class-grouped transition's parent meta with each sub-handler."""
    if not isinstance(wrapped_cls, type):
        return []
    out: list[Edge] = []
    for sub_name in dir(wrapped_cls):
        sub_attr = _get_class_attr(wrapped_cls, sub_name)
        if not isinstance(sub_attr, FsmTransition):
            continue
        sub_meta = sub_attr.meta
        arithmetics = _bound.TransitionStateArithmetics(parent_meta, sub_meta)
        sources = arithmetics.source_intersection()
        target = arithmetics.target_intersection()
        if not sources or not target:
            continue
        if isinstance(sources, bool):
            continue
        out.extend(Edge(source=s, target=target, label=label) for s in sources)
    return out


# --- renderers --------------------------------------------------------------


def _all_nodes(edges: Iterable[Edge]) -> list[str]:
    seen: dict[str, None] = {}
    for e in edges:
        seen[e.display_source] = None
        seen[e.target] = None
    return list(seen)


def to_mermaid(model_cls: type) -> str:
    """Render as a Mermaid `stateDiagram-v2` block. Renders natively on
    GitHub-flavoured markdown."""
    edges = collect_edges(model_cls)
    lines = ["stateDiagram-v2"]
    lines.extend(f"    {e.display_source} --> {e.target}: {e.label}" for e in edges)
    return "\n".join(lines)


def to_dot(model_cls: type) -> str:
    """Render as Graphviz DOT. Pipe through `dot -Tpng` to rasterise."""
    edges = collect_edges(model_cls)
    name = model_cls.__name__
    lines = [f"digraph {name} {{", "    rankdir=LR;"]
    for node in sorted(_all_nodes(edges)):
        shape = "ellipse" if node == WILDCARD_NODE else "box"
        lines.append(f'    "{node}" [shape={shape}];')
    lines.extend(
        f'    "{e.display_source}" -> "{e.target}" [label="{e.label}"];' for e in edges
    )
    lines.append("}")
    return "\n".join(lines)


def to_plantuml(model_cls: type) -> str:
    """Render as PlantUML state diagram source."""
    edges = collect_edges(model_cls)
    lines = ["@startuml"]
    for e in edges:
        src = "[*]" if e.source == "*" else f'"{e.display_source}"'
        lines.append(f'{src} --> "{e.target}" : {e.label}')
    lines.append("@enduml")
    return "\n".join(lines)
