"""The latest message in a chat app (Claude's chat view), straight from its accessibility tree.

Reading a whole chat window gives sidebar, menus and every visible message as one blob. Chat apps
built on the web mark their messages up for screen readers, so a per-app profile (config chat_apps)
says where they are:
- a container with web role `container_role` (Claude: "feed", named "Chat messages");
- one element per message with role `message_role` (Claude: "article"), named like `message_name`
  ("Message 8 of 8"), which gives the order;
- who wrote it from the start of its first text ("You said:" / "Claude responded:"), only there,
  so a message that merely contains "Claude responded:" can't pass as Claude's. In Claude that first
  text is a screen-reader label (role plus a preview of the opening words, repeated in full after
  it), so it's dropped once the role is known;
- a reply still being written (`streaming_name`, Claude: "Currently streaming message") counts as the
  newest message from `reply_role`, marked streaming;
- clutter left out: text inside `skip_inside` elements (buttons such as Copy, Retry or "Thought for
  13s"; screen-reader status lines) and pieces matching `drop_text`.

The rules are plain functions (tested without UI Automation); read_messages() is the only part that
touches the tree and runs on the UIA thread. If a profile doesn't match the window, it raises NoMatch
and the caller reads the window the old way (and logs a warning).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

SKIP_KINDS = {"button": "Button", "status": "StatusBar", "menu": "Menu", "toolbar": "ToolBar"}  # -> UIA type


class NoMatch(Exception):
    """The profile didn't fit the window (layout changed, or another view of the app is open)."""


@dataclass(frozen=True)
class Profile:
    container_role: str
    message_role: str
    message_name: re.Pattern
    roles: tuple[tuple[str, str], ...]  # (text prefix, role)
    reply_role: str
    skip_inside: tuple[str, ...] = ()
    drop_text: tuple[re.Pattern, ...] = ()
    streaming_name: re.Pattern | None = None

    @classmethod
    def from_config(cls, opts: dict) -> Profile:
        roles = tuple((str(k), str(v)) for k, v in (opts.get("roles") or {}).items())
        if not roles:
            raise ValueError("roles is empty")
        skip = tuple(str(k) for k in opts.get("skip_inside") or ())
        if unknown := [k for k in skip if k not in SKIP_KINDS]:
            raise ValueError(f"unknown skip_inside {unknown}; use {sorted(SKIP_KINDS)}")
        return cls(
            container_role=str(opts["container_role"]),
            message_role=str(opts["message_role"]),
            message_name=re.compile(str(opts.get("message_name") or r"(\d+)")),
            roles=roles,
            reply_role=str(opts.get("reply_role") or roles[-1][1]),
            skip_inside=skip,
            drop_text=tuple(re.compile(p) for p in opts.get("drop_text") or ()),
            streaming_name=re.compile(str(opts["streaming_name"])) if opts.get("streaming_name") else None,
        )


def load_profiles(config: dict) -> dict[str, Profile]:
    """exe name -> Profile from config chat_apps.profiles; a broken profile is skipped with a warning."""
    profiles = {}
    for exe, opts in ((config.get("chat_apps") or {}).get("profiles") or {}).items():
        try:
            profiles[str(exe).lower()] = Profile.from_config(opts or {})
        except (KeyError, ValueError, re.error) as exc:
            log.warning("chat_apps profile for %s ignored: %s", exe, exc)
    return profiles


@dataclass(frozen=True)
class Node:
    text: str
    skip: bool = False  # inside a button, status line or other skipped element


@dataclass(frozen=True)
class RawMessage:
    name: str
    nodes: tuple[Node, ...]


@dataclass(frozen=True)
class Message:
    number: int
    total: int | None
    role: str
    text: str
    streaming: bool = False


_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?)\]%])")


def kept_pieces(nodes: tuple[Node, ...] | list[Node], profile: Profile) -> list[str]:
    """The message's own text pieces: skipped nodes, empty ones and drop_text pieces left out."""
    kept = []
    for node in nodes:
        text = " ".join(node.text.split())
        if node.skip or not text or any(p.search(text) for p in profile.drop_text):
            continue
        kept.append(text)
    return kept


def join(pieces: list[str]) -> str:
    return _SPACE_BEFORE_PUNCT.sub(r"\1", " ".join(pieces))


def parse(raw: RawMessage, profile: Profile, order: int) -> Message:
    """Number (from the name, else its order), role (from the start of the first piece only) and text."""
    pieces = kept_pieces(raw.nodes, profile)
    if profile.streaming_name and profile.streaming_name.search(raw.name or ""):
        return Message(order, None, profile.reply_role, join(pieces), streaming=True)
    numbers = profile.message_name.search(raw.name or "")
    number = int(numbers.group(1)) if numbers else order
    total = int(numbers.group(2)) if numbers and numbers.re.groups >= 2 else None
    role = "unknown"
    for prefix, name in profile.roles:
        if pieces and pieces[0].startswith(prefix):
            role = name
            # The labelled first piece repeats the opening words; keep it only if it's all there is.
            pieces = pieces[1:] if len(pieces) > 1 else [pieces[0][len(prefix):].lstrip()]
            break
    return Message(number, total, role, join(pieces))


def number_streaming(messages: list[Message]) -> list[Message]:
    """Oldest first; a reply still being written is numbered after the newest finished message."""
    done = sorted((m for m in messages if not m.streaming), key=lambda m: m.number)
    top = max((m.number for m in done), default=0)
    live = [Message(top + i, None, m.role, m.text, True)
            for i, m in enumerate((m for m in messages if m.streaming), 1)]
    return done + live


def latest(messages: list[Message], role: str | None = None, finished: bool = False) -> Message | None:
    """The newest message (highest number), optionally only from `role`, optionally not still streaming."""
    pool = [m for m in messages if (role is None or m.role == role) and not (finished and m.streaming)]
    return max(pool, key=lambda m: m.number) if pool else None


# ---------------------------------------------------------------- UI Automation (run on the UIA thread)

def read_messages(hwnd: int, profile: Profile) -> list[Message]:
    """All loaded messages in the window, oldest first. Raises NoMatch if the profile doesn't fit."""
    import comtypes

    from core import uia

    client, UIA = uia._uia()
    try:
        root = client.ElementFromHandle(hwnd)
        role = lambda r: client.CreatePropertyCondition(UIA.UIA_AriaRolePropertyId, r)  # noqa: E731
        container = root.FindFirst(UIA.TreeScope_Descendants, role(profile.container_role))
        if not container:
            raise NoMatch(f"no element with role {profile.container_role!r}")
        cache = client.CreateCacheRequest()
        for prop in (UIA.UIA_NamePropertyId, UIA.UIA_RuntimeIdPropertyId):
            cache.AddProperty(prop)
        found = container.FindAllBuildCache(UIA.TreeScope_Descendants, role(profile.message_role), cache)
        if not found or found.Length == 0:
            raise NoMatch(f"no {profile.message_role!r} elements inside the {profile.container_role!r}")
        text_type = client.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId, UIA.UIA_TextControlTypeId)
        skip_types = [client.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId,
                                                     getattr(UIA, f"UIA_{SKIP_KINDS[k]}ControlTypeId"))
                      for k in profile.skip_inside]
        skip_cond = None
        if skip_types:
            skip_cond = skip_types[0] if len(skip_types) == 1 else client.CreateOrConditionFromArray(skip_types)
        messages = []
        for i in range(found.Length):
            el = found.GetElement(i)
            texts = el.FindAllBuildCache(UIA.TreeScope_Descendants, text_type, cache)
            skipped: set[tuple] = set()
            if skip_cond is not None:
                holders = el.FindAllBuildCache(UIA.TreeScope_Descendants, skip_cond, cache)
                for j in range(holders.Length):
                    inner = holders.GetElement(j).FindAllBuildCache(UIA.TreeScope_Descendants, text_type, cache)
                    skipped.update(_rid(inner.GetElement(k), UIA) for k in range(inner.Length))
            nodes = tuple(Node(t.CachedName or "", _rid(t, UIA) in skipped)
                          for t in (texts.GetElement(k) for k in range(texts.Length)))
            messages.append(parse(RawMessage(el.CachedName or "", nodes), profile, i + 1))
    except comtypes.COMError as exc:
        raise NoMatch(f"UI Automation error {exc}") from exc
    return number_streaming(messages)


def _rid(el, UIA) -> tuple:
    return tuple(el.GetCachedPropertyValue(UIA.UIA_RuntimeIdPropertyId) or ())
