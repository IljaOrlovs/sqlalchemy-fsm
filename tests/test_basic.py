import pytest
import sqlalchemy

from sqlalchemy_fsm import FSMField, transition
from sqlalchemy_fsm.exc import SetupError

from .conftest import Base


class BlogPost(Base):
    __tablename__ = "blogpost"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = sqlalchemy.Column(FSMField)

    def __init__(self, *args, **kwargs):
        self.state = "new"
        super(BlogPost, self).__init__(*args, **kwargs)

    @transition(source="new", target="published")
    def published(self):
        pass

    @transition(source="published", target="hidden")
    def hidden(self):
        pass

    @transition(source="new", target="removed")
    def removed(self):
        raise Exception("No rights to delete %s" % self)

    @transition(source=["published", "hidden"], target="stolen")
    def stolen(self):
        pass

    @transition(source="*", target="moderated")
    def moderated(self):
        pass


class TestFSMField(object):
    @pytest.fixture
    def model(self):
        return BlogPost()

    def test_initial_state_instatiated(self, model):
        assert model.state == "new"

    def test_meta_attached(self, model):
        assert model.published._sa_fsm_meta
        assert "FSMMeta" in repr(model.published._sa_fsm_meta)

    def test_known_transition_should_succeed(self, model):
        assert not model.published()  # Model is not publish-ed yet
        assert model.published.can_proceed()
        model.published.set()
        assert model.state == "published"
        # model is publish-ed now
        assert model.published()

        assert model.hidden.can_proceed()
        model.hidden.set()
        assert model.state == "hidden"

    def test_unknow_transition_fails(self, model):
        assert not model.hidden.can_proceed()
        with pytest.raises(NotImplementedError) as err:
            model.hidden.set()
        assert "Unable to switch from" in str(err)

    def test_state_non_changed_after_fail(self, model):
        with pytest.raises(Exception) as err:
            model.removed.set()
        assert "No rights to delete" in str(err)
        assert model.removed.can_proceed()
        assert model.state == "new"

    def test_mutiple_source_support_path_1_works(self, model):
        model.published.set()
        model.stolen.set()
        assert model.state == "stolen"

    def test_mutiple_source_support_path_2_works(self, model):
        model.published.set()
        model.hidden.set()
        model.stolen.set()
        assert model.state == "stolen"

    def test_star_shortcut_succeed(self, model):
        assert model.moderated.can_proceed()
        model.moderated.set()
        assert model.state == "moderated"

    def test_query_filter(self, session):
        model1 = BlogPost()
        model2 = BlogPost()
        model3 = BlogPost()
        model4 = BlogPost()
        model3.published.set()
        model4.published.set()

        session.add_all([model1, model2, model3, model4])
        session.commit()

        ids = [model1.id, model2.id, model3.id, model4.id]

        # Check that one can query by fsm handler
        query_results = (
            session.query(BlogPost)
            .filter(
                BlogPost.published(),
                BlogPost.id.in_(ids),
            )
            .all()
        )
        assert len(query_results) == 2, query_results
        assert model3 in query_results
        assert model4 in query_results

        negated_query_results = (
            session.query(BlogPost)
            .filter(
                ~BlogPost.published(),
                BlogPost.id.in_(ids),
            )
            .all()
        )
        assert len(negated_query_results) == 2, query_results
        assert model1 in negated_query_results
        assert model2 in negated_query_results


class InvalidModel(Base):
    __tablename__ = "invalidmodel"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    state = sqlalchemy.Column(FSMField)
    action = sqlalchemy.Column(FSMField)

    def __init__(self, *args, **kwargs):
        self.state = "new"
        self.action = "no"
        super(InvalidModel, self).__init__(*args, **kwargs)

    @transition(source="new", target="no")
    def validated(self):
        pass


class TestInvalidModel(object):
    def test_two_fsmfields_in_one_model_not_allowed(self):
        model = InvalidModel()
        with pytest.raises(SetupError) as err:
            model.validated()
        assert "More than one FSMField found" in str(err)


class Document(Base):
    __tablename__ = "document"
    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    status = sqlalchemy.Column(FSMField)

    def __init__(self, *args, **kwargs):
        self.status = "new"
        super(Document, self).__init__(*args, **kwargs)

    @transition(source="new", target="published")
    def published(self):
        pass


class TestDocument(object):
    def test_any_state_field_name_allowed(self):
        model = Document()
        model.published.set()
        assert model.status == "published"


class NullSource(Base):
    __tablename__ = "null_source"

    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
    status = sqlalchemy.Column(FSMField, nullable=True)

    @transition(source=None, target="published")
    def pub_from_none(self):
        pass

    @transition(source=None, target="new")
    def new_from_none(self):
        pass

    @transition(source=["new", None], target="published")
    def pub_from_either(self):
        pass

    @transition(source="*", target="end")
    def end_from_all(self):
        pass


class TestNullSource(object):
    @pytest.fixture
    def model(self):
        return NullSource()

    def test_null_to_end(self, model):
        assert model.status is None
        model.end_from_all.set()
        assert model.status == "end"

    def test_null_pub_end(self, model):
        assert model.status is None
        model.pub_from_none.set()
        assert model.status == "published"
        model.end_from_all.set()
        assert model.status == "end"

    def test_null_new_pub_end(self, model):
        assert model.status is None
        model.new_from_none.set()
        assert model.status == "new"
        model.pub_from_either.set()
        assert model.status == "published"
        model.end_from_all.set()
        assert model.status == "end"
