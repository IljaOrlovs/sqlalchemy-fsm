from importlib.metadata import PackageNotFoundError, version

from . import events, exc
from .sqltypes import FSMField
from .transition import transition

try:
    __version__ = version("sqlalchemy-fsm")
except PackageNotFoundError:
    __version__ = "0.0.0.dev0"
