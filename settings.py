"""Owner settings from Hermes's plugin settings (plugin.yaml config_schema -> Desktop's settings form, or
`plugins.entries.docket.settings` in config.yaml). Every value has a safe default, so Docket works with none set.

Read through the plugin context Hermes gave register(); outside Hermes (tests, the eval) the defaults apply.
"""

from __future__ import annotations

import contextlib
import contextvars
from typing import Any

DEFAULTS: dict[str, Any] = {
    "business_name": "", "business_phone": "", "business_email": "", "license": "",
    "quote_valid_days": 30, "invoice_due_days": 14, "payment_terms": "", "public_link": "", "followup_after_days": 5,
    "sender_email": "",
}
# A deployment can pin a setting for the whole process with an environment variable; it wins over the plugin
# setting (and over /docket link), so a hosted workspace's customer links always point at its own front door.
ENV_OVERRIDES = {"public_link": "DOCKET_PUBLIC_LINK", "sender_email": "DOCKET_SENDER_EMAIL"}
LIMITS = {"quote_valid_days": (1, 365), "invoice_due_days": (0, 365), "followup_after_days": (1, 90)}
BUSINESS_KEYS = {"business_name": "name", "business_phone": "phone", "business_email": "email",
                 "license": "license", "payment_terms": "payment_terms"}


_SCOPED: contextvars.ContextVar[dict | None] = contextvars.ContextVar("docket_settings_scope", default=None)


@contextlib.contextmanager
def scoped(values: dict):
    """Read settings from values (a profile's remembered settings) instead of Hermes. The accept page uses this
    for a named profile's link, since a customer's click arrives with no Hermes profile active."""
    token = _SCOPED.set(dict(values))
    try:
        yield
    finally:
        _SCOPED.reset(token)


def env_value(key: str) -> str:
    """The deployment's pinned value for key ('' when none): see ENV_OVERRIDES."""
    import os
    name = ENV_OVERRIDES.get(key)
    return (os.environ.get(name) or "").strip() if name else ""


def in_scope() -> bool:
    return _SCOPED.get() is not None


def snapshot() -> dict:
    """Every setting's current value, for the accept page to use later (see scoped)."""
    return {key: setting(key) for key in DEFAULTS}


def setting(key: str) -> Any:
    """The owner's value for key (clamped to sane limits), else the default."""
    default = DEFAULTS[key]
    scoped_values = _SCOPED.get()
    pinned = env_value(key)
    if pinned:
        value = pinned
    elif scoped_values is not None:
        value = scoped_values.get(key, default)
    else:
        try:
            from . import runtime
            ctx = runtime.CTX
            value = ctx.get_config(key, default) if ctx is not None else default
        except Exception:
            value = default
    if isinstance(default, int):
        try:
            value = int(value)
        except (TypeError, ValueError):
            return default
        lo, hi = LIMITS.get(key, (value, value))
        return min(max(value, lo), hi)
    return str(value or "").strip()


def apply_business(book: Any) -> Any:
    """Overlay the business details set in Settings onto the price book's business section (non-empty only)."""
    if book is None:
        return book
    for key, field in BUSINESS_KEYS.items():
        value = setting(key)
        if value:
            book.business[field] = value
    return book
