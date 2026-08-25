"""Daily option chain archiver for the tastytrade API.

Read-only by construction: the OAuth application backing this is registered
with the `read` scope, and nothing here posts to an order endpoint.
"""

__version__ = "0.1.0"
