"""Regras de robots.txt — inclusive o caso dos curingas, que a stdlib erra."""

from __future__ import annotations

import asyncio

import pytest

from sitecrawl.config import CrawlConfig
from sitecrawl.fetch import Fetcher
from sitecrawl.robots import _Parser, RobotsCache

from .fixture_site import FixtureSite

# Trecho real do robots.txt do PyPI (agosto/2026).
ROBOTS_PYPI = """Sitemap: https://pypi.org/sitemap.xml

User-agent: *
Disallow: /simple/
Disallow: /pypi/*/json
Disallow: /pypi*?
Disallow: /search*
Disallow: /account/
"""

protego_instalado = _Parser("").engine == "protego"


def test_prefixo_simples_funciona_em_qualquer_parser():
    parser = _Parser(ROBOTS_PYPI)
    assert parser.can_fetch("bot", "https://pypi.org/simple/requests/") is False
    assert parser.can_fetch("bot", "https://pypi.org/account/login/") is False
    assert parser.can_fetch("bot", "https://pypi.org/help/") is True


@pytest.mark.skipif(not protego_instalado, reason="requer protego")
def test_curingas_sao_respeitados_com_protego():
    """A stdlib libera estas três; o site proibiu todas."""
    parser = _Parser(ROBOTS_PYPI)
    assert parser.can_fetch("bot", "https://pypi.org/pypi/requests/json") is False
    assert parser.can_fetch("bot", "https://pypi.org/search?q=http") is False
    assert parser.can_fetch("bot", "https://pypi.org/pypi?x=1") is False


def test_sitemap_declarado_e_lido():
    assert _Parser(ROBOTS_PYPI).sitemaps() == ["https://pypi.org/sitemap.xml"]


def _rodar(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


@pytest.fixture()
def site():
    with FixtureSite() as fixture:
        yield fixture


def test_cache_busca_robots_uma_vez_por_host(site):
    async def cenario():
        config = CrawlConfig(start_urls=[site.base_url], per_host_delay=0.0)
        fetcher = Fetcher(config)
        cache = RobotsCache(fetcher, config.user_agent)
        try:
            # Cinco consultas, um único GET /robots.txt.
            resultados = [await cache.allowed(f"{site.base_url}/a") for _ in range(5)]
            bloqueada = await cache.allowed(f"{site.base_url}/privado")
        finally:
            await fetcher.aclose()
        return resultados, bloqueada

    resultados, bloqueada = _rodar(cenario())
    assert all(resultados)
    assert bloqueada is False
    assert site.hits.count("/robots.txt") == 1


def test_5xx_no_robots_proibe_tudo():
    """RFC 9309 §2.3.1.4: robots.txt indisponível = disallow all."""

    async def cenario():
        config = CrawlConfig(
            start_urls=["http://127.0.0.1:9"],  # porta fechada: erro de conexão
            per_host_delay=0.0,
            max_retries=0,
            timeout=2.0,
        )
        fetcher = Fetcher(config)
        cache = RobotsCache(fetcher, config.user_agent)
        try:
            return await cache.allowed("http://127.0.0.1:9/qualquer")
        finally:
            await fetcher.aclose()

    assert _rodar(cenario()) is False
