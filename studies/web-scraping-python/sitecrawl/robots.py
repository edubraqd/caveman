"""robots.txt: cache por host, com o comportamento de erro que o RFC 9309 manda.

Regra prática que quase todo tutorial erra:
  * **404 / 410** → não existe robots.txt → *tudo liberado*.
  * **5xx ou timeout** → o servidor não conseguiu responder → *tudo proibido*
    (o RFC 9309 §2.3.1.4 diz "unavailable" = disallow all). Tratar 503 como
    "pode tudo" é exatamente o comportamento que faz um site bloquear seu IP.

O parser é o `urllib.robotparser` da biblioteca padrão. Ele não implementa a
extensão `Allow` com curinga da forma que Google e Bing implementam, então em
sites complexos vale trocar por `protego` (mesma interface, regras do Google).
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from .fetch import Fetcher

log = logging.getLogger("sitecrawl.robots")


class _Rules:
    __slots__ = ("parser", "allow_all", "disallow_all", "sitemaps")

    def __init__(self) -> None:
        self.parser: RobotFileParser | None = None
        self.allow_all = False
        self.disallow_all = False
        self.sitemaps: list[str] = []


class RobotsCache:
    def __init__(self, fetcher: Fetcher, user_agent: str) -> None:
        self._fetcher = fetcher
        self._agent = user_agent
        self._hosts: dict[str, _Rules] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def _rules_for(self, url: str) -> _Rules:
        parts = urlsplit(url)
        key = f"{parts.scheme}://{parts.netloc}"
        if key in self._hosts:
            return self._hosts[key]

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in self._hosts:  # outro worker resolveu enquanto esperávamos
                return self._hosts[key]

            robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
            rules = _Rules()
            response = await self._fetcher.get(robots_url)

            if response.error is not None or response.status >= 500:
                rules.disallow_all = True
                log.warning("robots.txt indisponível em %s — tratando como disallow all", key)
            elif response.status in (401, 403):
                # Acesso restrito ao próprio robots.txt: o RFC manda proibir tudo.
                rules.disallow_all = True
            elif response.status >= 400:
                rules.allow_all = True
            else:
                parser = RobotFileParser()
                parser.parse(response.text.splitlines())
                rules.parser = parser
                rules.sitemaps = list(parser.site_maps() or [])

            self._hosts[key] = rules
            return rules

    async def allowed(self, url: str) -> bool:
        rules = await self._rules_for(url)
        if rules.disallow_all:
            return False
        if rules.allow_all or rules.parser is None:
            return True
        return rules.parser.can_fetch(self._agent, url)

    async def crawl_delay(self, url: str) -> float | None:
        rules = await self._rules_for(url)
        if rules.parser is None:
            return None
        try:
            delay = rules.parser.crawl_delay(self._agent)
        except AttributeError:  # parser sem entradas
            return None
        return float(delay) if delay is not None else None

    async def sitemaps(self, url: str) -> list[str]:
        return list((await self._rules_for(url)).sitemaps)
