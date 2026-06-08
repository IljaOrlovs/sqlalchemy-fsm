import pytest
import sqlalchemy

from sqlalchemy_fsm import FSMField, transition

from .conftest import Base


# Alternative syntax - separately defined transaction and sqlalchemy classes
class SeparatePublishHandler:
    @transition(source="new")
    def do_one(self, instance):
        instance.side_effect = "SeparatePublishHandler::did_one"

    @transition(source="hidden")
    def do_two(self, instance):
        instance.side_effect = "SeparatePublishHandler::did_two"


@transition(target="pre_decorated_publish")
class SeparateDecoratedPublishHandler:
    @transition(source="new")
    def do_one(self, instance):
        instance.side_effect = "SeparatePublishHandler::did_one"

    @transition(target="pre_decorated_publish", source="hidden")
    def do_two(self, instance):
        instance.side_effect = "SeparatePublishHandler::did_two"


class AltSyntaxBlogPost(Base):
    __tablename__ = "AltSyntaxBlogPost"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = sqlalchemy.Column(FSMField)
    side_effect = sqlalchemy.Column(sqlalchemy.String)

    def __init__(self, *args, **kwargs):
        self.state = "new"
        self.side_effect = "default"
        super().__init__(*args, **kwargs)

    @transition(source="new", target="hidden")
    def hide(self):
        pass

    pre_decorated_publish = SeparateDecoratedPublishHandler
    post_decorated_publish = transition(target="post_decorated_publish")(
        SeparatePublishHandler
    )


class TestAltSyntaxBlogPost:
    @pytest.fixture
    def model(self):
        return AltSyntaxBlogPost()

    def test_pre_decorated_publish(self, model):
        model.pre_decorated_publish.set()
        assert model.state == "pre_decorated_publish"
        assert model.side_effect == "SeparatePublishHandler::did_one"

    def test_pre_decorated_publish_from_hidden(self, model):
        model.hide.set()
        assert model.state == "hidden"
        assert model.hide()
        assert not model.pre_decorated_publish()
        model.pre_decorated_publish.set()
        assert model.state == "pre_decorated_publish"
        assert model.pre_decorated_publish()
        assert model.side_effect == "SeparatePublishHandler::did_two"

    def test_post_decorated_from_hidden(self, model):
        model.post_decorated_publish.set()
        assert model.state == "post_decorated_publish"
        assert model.side_effect == "SeparatePublishHandler::did_one"

    def test_post_decorated_publish_from_hidden(self, model):
        model.hide.set()
        assert model.state == "hidden"
        model.post_decorated_publish.set()
        assert model.state == "post_decorated_publish"
        assert model.side_effect == "SeparatePublishHandler::did_two"

    def mk_records(self, session, count):
        records = [AltSyntaxBlogPost() for idx in range(10)]
        session.add_all(records)
        return records

    @pytest.mark.parametrize("query_method", ["call", "is_"])
    def test_class_query(self, session, query_method):
        hidden_records = self.mk_records(session, 5)
        pre_decorated_published = self.mk_records(session, 5)
        post_decorated_published = self.mk_records(session, 5)

        [el.hide.set() for el in hidden_records]
        [el.pre_decorated_publish.set() for el in pre_decorated_published]
        [el.post_decorated_publish.set() for el in post_decorated_published]

        session.commit()

        all_ids = [
            el.id
            for el in (
                hidden_records + pre_decorated_published + post_decorated_published
            )
        ]
        for handler, expected_group in [
            ("hide", hidden_records),
            ("pre_decorated_publish", pre_decorated_published),
            ("post_decorated_publish", post_decorated_published),
        ]:
            expected_ids = {el.id for el in expected_group}
            attr = getattr(AltSyntaxBlogPost, handler)

            if query_method == "call":
                attr_filter = {
                    True: attr(),
                    False: ~attr(),
                }
            elif query_method == "is_":
                attr_filter = {
                    True: attr.is_(True),
                    False: attr.is_(False),
                }
            else:
                raise NotImplementedError(query_method)

            matching = (
                session.query(AltSyntaxBlogPost)
                .filter(
                    attr_filter[True],
                    AltSyntaxBlogPost.id.in_(all_ids),
                )
                .all()
            )
            assert len(matching) == len(expected_group)
            assert {el.id for el in matching} == expected_ids

            not_matching = (
                session.query(AltSyntaxBlogPost)
                .filter(
                    attr_filter[False],
                    AltSyntaxBlogPost.id.in_(all_ids),
                )
                .all()
            )
            assert len(not_matching) == (len(all_ids) - len(expected_group))
            assert not expected_ids.intersection(el.id for el in not_matching), (
                expected_ids.intersection(el.id for el in not_matching)
            )
