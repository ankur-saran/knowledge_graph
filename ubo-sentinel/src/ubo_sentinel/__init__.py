"""ubo-sentinel: beneficial ownership and sanctions exposure screening."""

from importlib.metadata import version

# Recorded in every decision as `engine_version`.
__version__ = version("ubo-sentinel")

__all__ = ["__version__"]
