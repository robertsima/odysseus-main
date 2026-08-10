"""Local-first MCP adapter over user-owned mood exports.

This package never contacts How We Feel (or any other network service). It
reads files the user placed in an approved local directory, normalizes them
into a stable internal schema, and exposes narrowly scoped read-only tools.
"""

__version__ = "0.1.0"

# Bumped whenever normalization or fingerprinting changes in a way that would
# produce different rows for the same input file. Recorded on every batch so a
# rebuild can tell which parser produced a given set of entries.
PARSER_VERSION = "1"

__all__ = ["PARSER_VERSION", "__version__"]
