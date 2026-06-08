"""Tests for `sqlalchemy_fsm.extras.graph`."""

import pytest
import sqlalchemy

from sqlalchemy_fsm import FSMField, transition
from sqlalchemy_fsm.extras.graph import (
    WILDCARD_NODE,
    Edge,
    collect_edges,
    to_dot,
    to_mermaid,
    to_plantuml,
)

from .conftest import Base


class Article(Base):
    __tablename__ = "GraphArticle"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = sqlalchemy.Column(FSMField)

    def __init__(self, *a, **kw):
        self.state = "draft"
        super().__init__(*a, **kw)

    @transition(source="draft", target="published")
    def publish(self):
        pass

    @transition(source=["draft", "published"], target="archived")
    def archive(self):
        pass

    @transition(source="*", target="deleted")
    def delete(self):
        pass


@transition(target="republished")
class _Republish:
    @transition(source="archived")
    def from_archived(self, instance):
        pass


class ClassGrouped(Base):
    __tablename__ = "GraphClassGrouped"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = sqlalchemy.Column(FSMField)

    def __init__(self, *a, **kw):
        self.state = "archived"
        super().__init__(*a, **kw)

    republish = _Republish


class TestCollectEdges:
    def test_collects_all_targets(self):
        edges = collect_edges(Article)
        labels = {e.label for e in edges}
        assert labels == {"publish", "archive", "delete"}

    def test_wildcard_source_emitted_as_synthetic_node(self):
        edges = collect_edges(Article)
        delete_edges = [e for e in edges if e.label == "delete"]
        assert len(delete_edges) == 1
        assert delete_edges[0].source == "*"
        assert delete_edges[0].display_source == WILDCARD_NODE

    def test_multi_source_fans_out(self):
        edges = collect_edges(Article)
        archive_edges = sorted(
            (e for e in edges if e.label == "archive"), key=lambda e: e.source or ""
        )
        assert [e.source for e in archive_edges] == ["draft", "published"]
        assert all(e.target == "archived" for e in archive_edges)

    def test_class_grouped_expands_sub_handlers(self):
        edges = collect_edges(ClassGrouped)
        assert edges == [
            Edge(source="archived", target="republished", label="republish"),
        ]

    def test_output_is_deterministic(self):
        assert collect_edges(Article) == collect_edges(Article)


class TestMermaid:
    def test_renders_state_diagram(self):
        out = to_mermaid(Article)
        assert out.startswith("stateDiagram-v2")
        assert "draft --> published: publish" in out
        assert "draft --> archived: archive" in out
        assert "published --> archived: archive" in out
        assert f"{WILDCARD_NODE} --> deleted: delete" in out


class TestDot:
    def test_emits_digraph_block(self):
        out = to_dot(Article)
        assert out.startswith("digraph Article {")
        assert out.rstrip().endswith("}")
        assert '"draft" -> "published" [label="publish"];' in out
        assert f'"{WILDCARD_NODE}" -> "deleted" [label="delete"];' in out

    def test_wildcard_uses_ellipse_shape(self):
        out = to_dot(Article)
        assert f'"{WILDCARD_NODE}" [shape=ellipse];' in out
        assert '"draft" [shape=box];' in out


class TestPlantUml:
    def test_emits_start_and_end_markers(self):
        out = to_plantuml(Article)
        assert out.startswith("@startuml")
        assert out.rstrip().endswith("@enduml")

    def test_wildcard_uses_initial_state(self):
        out = to_plantuml(Article)
        # PlantUML uses [*] for the initial pseudo-state.
        assert '[*] --> "deleted" : delete' in out


# Silence unused fixture import warning under strict configs.
_ = pytest
