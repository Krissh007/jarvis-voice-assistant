"""Keeping personal data out of models and logs, and deciding which browser targets are off-limits.

Rules (enforced in code, not in the prompt):
- password / payment / one-time-code fields are BLOCKED: never typed into, clicked or read
- account pages (Google account, Families, Pay, Gmail, Drive, Photos...) are never opened
- account switching / sign-out / membership controls are never clicked
- email addresses and phone numbers are scrubbed from any page text sent to a model
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

# Pages JARVIS must never open in its browser, even if the model or a page asks.
BLOCKED_DOMAINS = [
    "accounts.google.com", "myaccount.google.com", "families.google.com", "pay.google.com",
    "payments.google.com", "wallet.google.com", "passwords.google.com", "takeout.google.com",
    "mail.google.com", "gmail.com", "drive.google.com", "docs.google.com", "photos.google.com",
    "contacts.google.com", "admin.google.com", "one.google.com", "studio.youtube.com",
]
# Paths on otherwise-allowed sites: account settings, channel/account switching, memberships.
BLOCKED_PATHS = {
    "youtube.com": ["/account", "/channel_switcher", "/paid_memberships", "/signin", "/logout",
                    "/select_site", "/create_channel", "/premium/family", "/purchases"],
    "music.youtube.com": ["/account", "/paid_memberships", "/signin", "/logout"],
    "google.com": ["/accounts", "/settings", "/pay", "/payments", "/wallet"],
}
# Controls that switch or manage accounts. Matched against an element's visible text or label.
BLOCKED_CONTROL_TEXT = [
    "switch account", "add account", "add another account", "sign out", "log out", "logout",
    "manage your google account", "google account", "your data in youtube", "purchases and memberships",
    "paid memberships", "family", "manage family", "youtube studio", "create a channel", "your channel",
    "account menu", "settings", "switch profile", "use another account", "remove account",
]

_SENSITIVE_WORDS = re.compile(
    r"pass(word|wd|code|phrase)?\b|\bpwd\b|\bpin\b|otp|one[- ]time|verification code|security code|"
    r"card|\bcc\b|cvv|cvc|\bexp(iry|iration)?\b|iban|\bsort code|routing|account number|\bssn\b|"
    r"aadhaar|\bpan\b|\bupi\b",
    re.IGNORECASE,
)
_SENSITIVE_AUTOCOMPLETE = re.compile(r"^(cc-|current-password|new-password|one-time-code|transaction-)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_PHONE = re.compile(r"[(+]*\d[\d\s().-]{8,}\d")


def redact(text: str) -> str:
    """Replace email addresses and phone-like numbers (10+ digits) before text goes to a model."""
    text = _EMAIL.sub("[email]", text)
    return _PHONE.sub(lambda m: "[number]" if sum(c.isdigit() for c in m.group()) >= 10 else m.group(), text)


def is_sensitive_field(el: dict[str, Any]) -> bool:
    """A form field JARVIS must never touch: passwords, payment details, one-time codes."""
    kind = str(el.get("type") or "").lower()
    if kind == "password":
        return True
    if _SENSITIVE_AUTOCOMPLETE.match(str(el.get("autocomplete") or "").lower()):
        return True
    if el.get("tag") in {"input", "textarea", "select"} or el.get("editable"):
        label = " ".join(str(el.get(k) or "") for k in ("name", "id", "label", "placeholder", "text"))
        return bool(_SENSITIVE_WORDS.search(label))
    return False


def is_blocked_control(text: str) -> bool:
    """True if a control's label *is* an account-management phrase (a little slack for icons/arrows).
    'Switch account' matches; a video called 'Family Guy funny moments' does not."""
    t = " ".join(text.lower().split())
    if not t:
        return False
    return any(t == p or (t.startswith(p) and len(t) <= len(p) + 6) for p in BLOCKED_CONTROL_TEXT)


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def is_blocked_url(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if any(_host_matches(host, d) for d in BLOCKED_DOMAINS):
        return True
    path = (parts.path or "/").lower()
    return any(_host_matches(host, domain) and any(path.startswith(p) for p in prefixes)
               for domain, prefixes in BLOCKED_PATHS.items())


def route_globs() -> list[str]:
    """Playwright URL globs that catch every blocked page, so navigation can be stopped in the browser."""
    globs = [f"**://{d}/**" for d in BLOCKED_DOMAINS] + [f"**://*.{d}/**" for d in BLOCKED_DOMAINS]
    for domain, prefixes in BLOCKED_PATHS.items():
        for p in prefixes:
            globs += [f"**://{domain}{p}**", f"**://*.{domain}{p}**"]
    return globs


# Chrome preferences for the JARVIS profile: nothing saved that could later be autofilled or leaked.
PROFILE_PREFERENCES = {
    "credentials_enable_service": False,
    "profile": {"password_manager_enabled": False, "password_manager_leak_detection": False},
    "autofill": {"enabled": False, "profile_enabled": False, "credit_card_enabled": False},
    "payments": {"can_make_payment_enabled": False},
    "signin": {"allowed": True},
    "sync": {"requested": False},
}
