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

from typing import TYPE_CHECKING

from ..introspection import TransitionEdge, collect_edges

if TYPE_CHECKING:
    from collections.abc import Iterable

# Synthetic node label used to render `source="*"` (wildcard) edges.
WILDCARD_NODE = "(any)"

# Backwards-compat alias for callers that imported `Edge` from this module.
Edge = TransitionEdge


def _all_nodes(edges: Iterable[TransitionEdge]) -> list[str]:
    seen: dict[str, None] = {}
    for e in edges:
        seen[e.display_source] = None
        seen[e.target] = None
    return list(seen)


def _sorted_for_display(edges: list[TransitionEdge]) -> list[TransitionEdge]:
    """Re-sort using `display_source` so wildcards/Nones cluster predictably
    regardless of how the raw `source` field sorted."""
    return sorted(edges, key=lambda e: (e.label, e.display_source, e.target))


def to_mermaid(model_cls: type) -> str:
    """Render as a Mermaid `stateDiagram-v2` block. Renders natively on
    GitHub-flavoured markdown."""
    edges = _sorted_for_display(collect_edges(model_cls))
    lines = ["stateDiagram-v2"]
    lines.extend(f"    {e.display_source} --> {e.target}: {e.label}" for e in edges)
    return "\n".join(lines)


def to_dot(model_cls: type) -> str:
    """Render as Graphviz DOT. Pipe through `dot -Tpng` to rasterise."""
    edges = _sorted_for_display(collect_edges(model_cls))
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
    edges = _sorted_for_display(collect_edges(model_cls))
    lines = ["@startuml"]
    for e in edges:
        src = "[*]" if e.source == "*" else f'"{e.display_source}"'
        lines.append(f'{src} --> "{e.target}" : {e.label}')
    lines.append("@enduml")
    return "\n".join(lines)
