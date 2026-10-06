"""Suppress SDK wire/body logging only in the current Guardian request context."""
from contextlib import contextmanager
from contextvars import ContextVar
import logging

_private_request = ContextVar("guardian_private_provider_logs", default=False)


class _PrivateRequestFilter(logging.Filter):
    def filter(self, record):
        return not _private_request.get()


_filter = _PrivateRequestFilter()
_KNOWN = ("openai._base_client", "openai._utils._logs", "httpx", "httpcore.connection",
          "httpcore.connection_pool", "httpcore.http11", "httpcore.http2", "httpcore.proxy", "httpcore.socks")


@contextmanager
def private_provider_logs():
    # Logger filters are context-aware: concurrent main calls retain their existing logging.
    names = tuple(logging.Logger.manager.loggerDict) + _KNOWN
    for name in names:
        if name == "httpx" or name.startswith(("openai.", "httpcore.")):
            logging.getLogger(name).addFilter(_filter)
    token = _private_request.set(True)
    try:
        yield
    finally:
        _private_request.reset(token)
