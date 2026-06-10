import pytest
import sqlalchemy
from sqlalchemy.orm import DeclarativeBase, sessionmaker

engine = sqlalchemy.create_engine("sqlite:///:memory:", echo=True)
SessionGen = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


def pytest_sessionstart():
    Base.metadata.create_all(engine)


@pytest.fixture
def session():
    Base.metadata.create_all(engine)  # Creates any dynamically imported tables
    return SessionGen()
