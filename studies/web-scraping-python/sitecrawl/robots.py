"""robots.txt: cache por host, com o comportamento de erro que o RFC 9309 manda.

Regra prática que quase todo tutorial erra:
  * **404 / 410** → não existe robots.txt → *tudo liberado*.
  * **5xx ou timeout** → o servidor não conseguiu responder → *tudo proibido*
    (o RFC 9309 §2.3.1.4 diz "unavailable" = disallow all). Tratar 503 como
    "pode tudo" é exatamente o comportamento que faz um site bloquear seu IP.

**Sobre o parser.** O `urllib.robotparser` da biblioteca padrão *não implementa
curingas* (`*` e `$`) nos caminhos. Isso não é um detalhe acadêmico: ele erra
para o lado errado — libera o que o site proibiu. Medido contra o robots.txt do
PyPI, que usa `Disallow: /pypi/*/json` e `Disallow: /search*`:

    /pypi/requests/json     stdlib=PODE    protego=BARRADA
    /search?q=http          stdlib=PODE    protego=BARRADA
    /pypi?x=1               stdlib=PODE    protego=BARRADA

Por isso preferimos `protego` (implementa as regras do Google, mesma ideia de
interface) quando ele está instalado, e caímos para a stdlib com um aviso
quando não está.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from .fetch import Fetcher

log = logging.getLogger("sitecrawl.robots")

try:
    from protego import Protego
except ImportError:  # pragma: no cover - depende do ambiente
    Protego = None


class _Parser:
    """Fachada sobre protego (preferido) ou urllib.robotparser (reserva)."""

    __slots__ = ("_impl", "engine")

    def __init__(self, texto: str) -> None:
        if Protego is not None:
            self._impl = Protego.parse(texto)
            self.engine = "protego"
        else:
            parser = RobotFileParser()
            parser.parse(texto.splitlines())
            self._impl = parser
            self.engine = "stdlib"

    def can_fetch(self, agent: str, url: str) -> bool:
        if self.engine == "protego":
            return bool(self._impl.can_fetch(url, agent))
        return bool(self._impl.can_fetch(agent, url))

    def crawl_delay(self, agent: str) -> float | None:
        delay = self._impl.crawl_delay(agent)
        return float(delay) if delay is not None else None

    def sitemaps(self) -> list[str]:
        if self.engine == "protego":
            return list(self._impl.sitemaps or [])
        return list(self._impl.site_maps() or [])


class _Rules:
    __slots__ = ("parser", "allow_all", "disallow_all", "sitemaps")

    def __init__(self) -> None:
        self.parser: _Parser | None = None
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
                parser = _Parser(response.text)
                rules.parser = parser
                rules.sitemaps = parser.sitemaps()
                if parser.engine == "stdlib":
                    log.warning(
                        "protego ausente: curingas em robots.txt (Disallow: /x/*/y) "
                        "serão IGNORADOS e o crawler pode acessar o que o site proibiu. "
                        "Instale com: pip install protego"
                    )

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
            return rules.parser.crawl_delay(self._agent)
        except (AttributeError, ValueError):  # parser sem entradas ou valor inválido
            return None

    async def sitemaps(self, url: str) -> list[str]:
        return list((await self._rules_for(url)).sitemaps)
