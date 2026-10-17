"""Search, read+summarize page, weather, save_research.

Everything that comes back from the web is untrusted: the dispatcher wraps it and taints the turn.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from actions.system import normalize_url
from core.llm import USAGE_LIMIT_MESSAGE, LLMUnavailable
from core.ratelimit import TTLCache
from core.secrets import env_key
from core.registry import Registry, Tier, Tool
from core.untrusted import wrap

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) JARVIS-desktop/0.1"
MAX_PAGE_BYTES = 2_000_000
MAX_TEXT_CHARS = 20_000

# Replaced in tests: an httpx transport (e.g. MockTransport), and whether private addresses may be fetched.
_transport: httpx.AsyncBaseTransport | None = None
_allow_private = False

WMO_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers",
    82: "violent showers", 85: "snow showers", 86: "heavy snow showers", 95: "thunderstorm",
    96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}


@dataclass
class ResearchState:
    """What has been researched for the current question, for the page cap and save_research."""

    question: str = ""
    pages: list[dict[str, str]] = field(default_factory=list)
    results: list[dict[str, str]] = field(default_factory=list)  # search hits, for notes when no page was read
    pages_this_turn: int = 0
    searched_this_turn: bool = False

    def new_turn(self) -> None:
        self.pages_this_turn = 0
        self.searched_this_turn = False


def _client(**kwargs: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=_transport, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=15, **kwargs
    )


def _is_private_host(host: str) -> bool:
    if host.lower() in {"localhost", "localhost.localdomain"} or host.lower().endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved


BOT_BLOCK_STATUS = {401, 403, 429, 451}
BLOCK_PAGE = re.compile(r"access (to this page )?(has been )?denied|\bforbidden\b|are you a robot|captcha|"
                        r"attention required|just a moment|verify you are (a )?human|unusual traffic",
                        re.IGNORECASE)
_browser_fetch_enabled = True  # tests turn this off


async def browser_fetch(url: str) -> tuple[str, str]:
    """Load a page in a throwaway headless Chrome: no profile, no cookies, closed straight after.
    For sites that refuse plain downloads. Returns (final_url, html)."""
    from playwright.async_api import async_playwright

    from core.privacy import is_blocked_url

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        try:
            page = await (await browser.new_context()).new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            if is_blocked_url(page.url) or _is_private_host(urlsplit(page.url).hostname or ""):
                raise ValueError("that page redirected somewhere I don't read")
            await page.wait_for_timeout(1500)  # let script-built articles render
            return page.url, (await page.content())[: MAX_PAGE_BYTES]
        finally:
            await browser.close()


async def fetch_page(url: str) -> tuple[str, str, str]:
    """Download a page and extract its main text. Returns (final_url, title, text)."""
    import trafilatura

    from core.privacy import is_blocked_url

    normalized = normalize_url(url)
    if normalized is None:
        raise ValueError("not a web address")
    if not _allow_private and _is_private_host(urlsplit(normalized).hostname or ""):
        raise ValueError("private or local addresses can't be read")
    if is_blocked_url(normalized):
        raise ValueError("account and payment pages can't be read")
    try:
        async with _client() as http:
            async with http.stream("GET", normalized) as response:
                response.raise_for_status()
                kind = response.headers.get("content-type", "")
                if "html" not in kind and "text" not in kind:
                    raise ValueError(f"not a web page ({kind or 'unknown type'})")
                body = b""
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_PAGE_BYTES:
                        break
                final_url = str(response.url)
        html = body.decode(response.encoding or "utf-8", errors="replace")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code not in BOT_BLOCK_STATUS or not _browser_fetch_enabled:
            raise
        log.info("%s refused a plain download (%d); trying a headless browser", urlsplit(normalized).hostname,
                 exc.response.status_code)
        try:
            final_url, html = await browser_fetch(normalized)
        except Exception as browser_exc:
            raise ValueError(f"the site blocked me ({exc.response.status_code})") from browser_exc
    text = trafilatura.extract(html, url=final_url, include_comments=False, include_tables=True) or ""
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    title = " ".join(match.group(1).split())[:200] if match else urlsplit(final_url).hostname or final_url
    return final_url, title, text[:MAX_TEXT_CHARS]


async def summarise(llm, url: str, text: str, question: str) -> str:
    """Summarise page text (already redacted if needed) for a question. Raises LLMUnavailable."""
    if llm is None:
        return text[:1200]
    prompt = (
        f"Question: {question or '(none given; summarise the main points)'}\n\n"
        "Summarise the page below in at most 120 words, focused on the question. Keep concrete facts, "
        "names, numbers and prices. If the page doesn't answer the question, say so.\n\n" + wrap(url, text)
    )
    return await llm.generate(prompt, system="You summarise web pages for a voice assistant.")


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60].strip("-")
    return slug or "research"


def next_free_path(folder: Path, title: str) -> Path:
    """Never overwrite: search-apis.md, search-apis-2.md, ..."""
    base = slugify(title)
    candidate, n = folder / f"{base}.md", 2
    while candidate.exists():
        candidate, n = folder / f"{base}-{n}.md", n + 1
    return candidate


def display_path(path: Path) -> str:
    home = os.environ.get("USERPROFILE", "")
    text = str(path)
    return text[len(home):].lstrip("\\/") if home and text.lower().startswith(home.lower()) else text


class Web:
    def __init__(self, config: dict, services) -> None:
        self.llm = services.llm
        limits = config.get("limits", {})
        self.page_cap = int(limits.get("research_pages_max", 3))
        self.cache = TTLCache(Path(services.data_dir) / "cache" / "page_summaries.json",
                              ttl_s=float(limits.get("page_cache_ttl_h", 24)) * 3600)
        self.research_dir = Path(os.path.expandvars(config.get("paths", {}).get("research_dir",
                                                                                "%USERPROFILE%\\Documents\\jarvis")))
        self.default_city = (config.get("weather") or {}).get("default_city")
        self.state = ResearchState()

    # ---------------------------------------------------------------- tools

    async def search_web(self, query: str, mode: str = "general") -> str:
        """Web search (DuckDuckGo, or Tavily if TAVILY_API_KEY is set). Rung 1. Tier SAFE; output untrusted."""
        if not self.state.searched_this_turn:
            self.state = ResearchState(question=query, searched_this_turn=True)
        news = mode == "news"
        results = None
        if key := env_key("TAVILY_API_KEY"):
            try:
                results = await self._tavily(query, news, key)
            except httpx.HTTPError as exc:
                log.warning("Tavily failed (%s); falling back to DuckDuckGo", exc)
        if results is None:
            results = await asyncio.to_thread(self._ddgs, query, news)
        if not results:
            return f"No results for {query!r}."
        seen = {r["url"] for r in self.state.results}
        self.state.results.extend(r for r in results if r.get("url") and r["url"] not in seen)
        lines = [f"{i}. {r['title']} ({r['url']})\n   {r['snippet']}" for i, r in enumerate(results, 1)]
        return "\n".join(lines)

    async def read_page(self, url: str, question: str = "") -> str:
        """Read a page and summarise it. Rung 1 (HTTP) + Flash. Tier SAFE; output untrusted. Cached 24 h."""
        if self.state.pages_this_turn >= self.page_cap:
            return f"Page limit reached ({self.page_cap} per question). Answer with what you have."
        self.state.pages_this_turn += 1
        try:
            final_url, title, text = await fetch_page(url)
        except (httpx.HTTPError, ValueError) as exc:
            return f"Couldn't read that page ({exc})."
        if BLOCK_PAGE.search(title) or (not text.strip() and BLOCK_PAGE.search(title + " " + final_url)):
            return "Couldn't read that page (the site blocks automated readers). Try another result."
        if not text.strip():
            return f"Couldn't read that page ({title} has no readable text)."
        question = question or self.state.question
        key = TTLCache.key(final_url, hashlib.sha256(text.encode("utf-8")).hexdigest(), question)
        summary = self.cache.get(key)
        if summary is None:
            try:
                summary = await self._summarise(final_url, text, question)
            except LLMUnavailable:
                return USAGE_LIMIT_MESSAGE
            self.cache.set(key, summary)
        self.state.pages.append({"url": final_url, "title": title, "summary": summary})
        return f"Title: {title}\nURL: {final_url}\nSummary: {summary}"

    async def get_weather(self, city: str = "") -> str:
        """Current weather and today's range (Open-Meteo, no key). Rung 1. Tier SAFE, read-only."""
        city = (city or self.default_city or "").strip()
        if not city:
            return "Which city? I don't have a default city set."
        async with _client() as http:
            geo = (await http.get("https://geocoding-api.open-meteo.com/v1/search",
                                  params={"name": city, "count": 1})).json()
            if not geo.get("results"):
                return f"I couldn't find a place called {city}."
            place = geo["results"][0]
            forecast = (await http.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": place["latitude"], "longitude": place["longitude"], "timezone": "auto", "forecast_days": 1,
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            })).json()
        now, day = forecast["current"], forecast["daily"]
        name = ", ".join(p for p in [place.get("name"), place.get("admin1"), place.get("country")] if p)
        rain = day.get("precipitation_probability_max", [None])[0]
        return (
            f"{name}: {now['temperature_2m']:.0f}°C (feels like {now['apparent_temperature']:.0f}), "
            f"{WMO_CODES.get(now['weather_code'], 'unknown conditions')}, humidity {now['relative_humidity_2m']}%, "
            f"wind {now['wind_speed_10m']:.0f} km/h. Today {day['temperature_2m_min'][0]:.0f} to "
            f"{day['temperature_2m_max'][0]:.0f}°C" + (f", {rain}% chance of rain." if rain is not None else ".")
        )

    async def save_research(self, title: str) -> str:
        """Write the last research as markdown in Documents\\jarvis. Rung 2 (file write). Tier CONFIRM."""
        if not self.state.pages and not self.state.results:
            return "There's no research to save yet."
        self.research_dir.mkdir(parents=True, exist_ok=True)
        path = next_free_path(self.research_dir, title)
        if path.resolve().parent != self.research_dir.resolve():
            return "I can only save inside the research folder."
        body = await self._report(title)
        sources = "\n".join(f"- [{p['title']}]({p['url']})" for p in self.state.pages or self.state.results[:10])
        text = f"# {title}\n\n_Question: {self.state.question}_\n\n{body}\n\n## Sources\n\n{sources}\n"
        with path.open("x", encoding="utf-8") as f:  # "x": fail rather than overwrite
            f.write(text)
        return f"Saved to {display_path(path)}."

    def save_summary(self, args: dict[str, Any]) -> str:
        path = next_free_path(self.research_dir, str(args.get("title", "")))
        return f"Save research notes to {display_path(path)}"

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _ddgs(query: str, news: bool) -> list[dict[str, str]]:
        from ddgs import DDGS

        raw = DDGS().news(query, max_results=5) if news else DDGS().text(query, max_results=5)
        return [{"title": r.get("title", ""), "url": r.get("href") or r.get("url", ""),
                 "snippet": (r.get("body") or "")[:300]} for r in raw]

    @staticmethod
    async def _tavily(query: str, news: bool, key: str) -> list[dict[str, str]]:
        async with _client() as http:
            response = await http.post("https://api.tavily.com/search", headers={"Authorization": f"Bearer {key}"},
                                       json={"query": query, "max_results": 5, "topic": "news" if news else "general"})
            response.raise_for_status()
        return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": (r.get("content") or "")[:300]}
                for r in response.json().get("results", [])]

    async def _summarise(self, url: str, text: str, question: str) -> str:
        return await summarise(self.llm, url, text, question)

    async def _report(self, title: str) -> str:
        # Read pages when there are any; otherwise the search results' snippets.
        items = [(p["url"], p["title"], p["summary"]) for p in self.state.pages] or [
            (r["url"], r["title"], r.get("snippet", "")) for r in self.state.results[:10]]
        notes = "\n\n".join(wrap(url, f"{t}\n{text}") for url, t, text in items)
        fallback = "\n\n".join(f"### {t}\n\n{text}" for _, t, text in items)
        if self.llm is None:
            return fallback
        prompt = (
            f"Write research notes in markdown titled '{title}' answering: {self.state.question}\n"
            "Use 300 to 600 words, short sections and bullet points, based only on these page summaries. "
            "Don't add a title line or a sources list; those are added separately.\n\n" + notes
        )
        try:
            return await self.llm.generate(prompt, system="You write clear, factual research notes.", max_tokens=4000)
        except LLMUnavailable:
            return fallback


def register(registry: Registry, config: dict, services) -> None:
    web = Web(config, services)
    services.web = web
    registry.on_user_turn(lambda: web.state.new_turn())  # late-bound: the state object is replaced per question
    cap = web.page_cap
    registry.register(Tool(
        name="search_web",
        description=(
            "Look things up on the web: facts you don't know, current events, prices, opening times, or when "
            "the user says 'search' or 'look up'. Not for well-known facts you can just answer, and for a "
            "judgement that needs current facts (compare, decide, calculate) use ask_brain (or think_through with use_web). "
            "For research questions: search, then read_page on the 2 or 3 most relevant "
            f"results (max {cap}), answer in 2 or 3 spoken sentences, then offer to save longer notes. "
            "Use mode 'news' for current events."
        ),
        func=web.search_web,
        tier=Tier.SAFE,
        read_only=True,
        untrusted_output=True,
        rung=1,
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "mode": {"type": "string", "enum": ["general", "news"]},
            },
            "required": ["query"],
        },
        summarize=lambda a: f"Search the {'news' if a.get('mode') == 'news' else 'web'} for {a.get('query')!r}",
    ))
    registry.register(Tool(
        name="read_page",
        description="Read a web page and get a short summary focused on a question.",
        func=web.read_page,
        tier=Tier.SAFE,
        read_only=True,
        untrusted_output=True,
        rung=1,
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "question": {"type": "string", "description": "What to look for on the page."},
            },
            "required": ["url"],
        },
        summarize=lambda a: f"Read {a.get('url')}" + (f" to find: {a['question']}" if a.get('question') else ''),
    ))
    registry.register(Tool(
        name="get_weather",
        description="Current weather and today's forecast for a city.",
        func=web.get_weather,
        tier=Tier.SAFE,
        read_only=True,
        rung=1,
        parameters={"type": "object", "properties": {"city": {"type": "string"}}},
    ))
    registry.register(Tool(
        name="save_research",
        description="Save the latest research as a markdown file in Documents\\jarvis. Needs confirmation.",
        func=web.save_research,
        tier=Tier.CONFIRM,
        rung=2,
        parameters={
            "type": "object",
            "properties": {"title": {"type": "string", "description": "Short title, e.g. 'Free search APIs'."}},
            "required": ["title"],
        },
        summarize=web.save_summary,
    ))
