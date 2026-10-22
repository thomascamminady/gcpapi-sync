"""Package version."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("gcpapi-sync")
except PackageNotFoundError:  # running from a source checkout without installing
    __version__ = "0.0.0"
