"""Structured logging of the MCP traffic.

The package is named `logging` because the assignment names it that. It does
not shadow the standard library: Python 3 resolves imports absolutely, so a
plain `import logging` anywhere in this project still reaches the stdlib
module, and this one is only ever reachable as `host.logging`.
"""
