from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

from .pushdown import register_sparql_pushdown, unregister_sparql_pushdown
from .store import VortexStore

try:
    __version__ = _version("vortex-rdflib")
except PackageNotFoundError:  # source tree without an install
    __version__ = "0.0.0.dev0"

__all__ = [
    "VortexStore",
    "register_sparql_pushdown",
    "unregister_sparql_pushdown",
    "__version__",
]
