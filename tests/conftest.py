import pytest
import sqlalchemy
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

engine = sqlalchemy.create_engine("sqlite:///:memory:", echo=True)
SessionGen = sessionmaker(bind=engine)
Base = declarative_base()


def pytest_sessionstart():
    Base.metadata.create_all(engine)


@pytest.fixture(scope="function")
def session():
    Base.metadata.create_all(engine)  # Creates any dynamically imported tables
    return SessionGen()
