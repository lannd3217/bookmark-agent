import functools
import logging
import time

log = logging.getLogger("claude")


def instrument(client):
    """Wrap client.messages.create to log model, token usage, and latency of each call."""
    create = client.messages.create
    if getattr(create, "_logged", False):
        return client

    @functools.wraps(create)
    def logged_create(*args, **kwargs):
        start = time.perf_counter()
        try:
            resp = create(*args, **kwargs)
        except Exception as exc:
            ms = (time.perf_counter() - start) * 1000
            log.warning("claude model=%s error=%s latency_ms=%.0f", kwargs.get("model"), type(exc).__name__, ms)
            raise
        ms = (time.perf_counter() - start) * 1000
        usage = getattr(resp, "usage", None)
        log.info(
            "claude model=%s input_tokens=%s output_tokens=%s latency_ms=%.0f",
            getattr(resp, "model", None) or kwargs.get("model"),
            getattr(usage, "input_tokens", None),
            getattr(usage, "output_tokens", None),
            ms,
        )
        return resp

    logged_create._logged = True
    client.messages.create = logged_create
    return client
