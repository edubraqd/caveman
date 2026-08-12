"""Descoberta de URLs sem seguir link nenhum.

Antes de sair rastejando por `<a href>`, pergunte ao site onde estão as páginas.
Quase todo CMS publica um `sitemap.xml`, e ele é ordens de grandeza mais barato:
uma requisição pode render 50 mil URLs, com data de modificação inclusa.

Ordem de tentativa:
  1. `Sitemap:` declarado no robots.txt (fonte oficial);
  2. caminhos convencionais (`/sitemap.xml`, `/sitemap_index.xml`, ...);
  3. feeds RSS/Atom (`/feed`, `/rss`), que também listam conteúdo recente.
"""

from __future__ import annotations

import gzip
import logging
from dataclasses import dataclass
from urllib.parse import urljoin
from xml.etree import ElementTree

from .fetch import Fetcher, decode

log = logging.getLogger("sitecrawl.discovery")

COMMON_SITEMAP_PATHS = (
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/sitemap/sitemap.xml",
    "/wp-sitemap.xml",  # WordPress 5.5+
    "/sitemap.xml.gz",
)

COMMON_FEED_PATHS = ("/feed", "/feed/", "/rss", "/rss.xml", "/atom.xml", "/index.xml")

MAX_SITEMAP_DEPTH = 3


@dataclass(slots=True)
class DiscoveredUrl:
    url: str
    lastmod: str | None = None
    source: str = "sitemap"


def _localname(tag: str) -> str:
    """`{http://www.sitemaps.org/schemas/sitemap/0.9}loc` → `loc`.

    Sitemaps reais usam namespaces inconsistentes (ou nenhum). Ignorar o
    namespace e casar pelo nome local evita um `findall` que devolve vazio
    silenciosamente — o bug mais comum ao parsear sitemap com ElementTree.
    """
    return tag.rsplit("}", 1)[-1].lower() if "}" in tag else tag.lower()


def parse_sitemap(xml_bytes: bytes, base_url: str) -> tuple[list[DiscoveredUrl], list[str]]:
    """Devolve (urls de páginas, urls de sub-sitemaps)."""
    if xml_bytes[:2] == b"\x1f\x8b":  # magic number do gzip
        try:
            xml_bytes = gzip.decompress(xml_bytes)
        except OSError:
            return [], []

    text = decode(xml_bytes, "").lstrip("﻿ \n\r\t")
    if not text:
        return [], []

    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        log.debug("sitemap inválido em %s: %s", base_url, exc)
        return [], []

    root_name = _localname(root.tag)
    pages: list[DiscoveredUrl] = []
    children: list[str] = []

    if root_name == "sitemapindex":
        for node in root:
            for child in node:
                if _localname(child.tag) == "loc" and child.text:
                    children.append(urljoin(base_url, child.text.strip()))
        return pages, children

    if root_name == "urlset":
        for node in root:
            loc = lastmod = None
            for child in node:
                name = _localname(child.tag)
                if name == "loc" and child.text:
                    loc = child.text.strip()
                elif name == "lastmod" and child.text:
                    lastmod = child.text.strip()
            if loc:
                pages.append(DiscoveredUrl(urljoin(base_url, loc), lastmod))
        return pages, children

    # RSS 2.0 (<rss><channel><item><link>) e Atom (<feed><entry><link href>).
    if root_name in {"rss", "feed"}:
        for element in root.iter():
            name = _localname(element.tag)
            if name == "link":
                href = element.get("href") or (element.text or "").strip()
                if href:
                    pages.append(DiscoveredUrl(urljoin(base_url, href), source="feed"))
        return pages, children

    return pages, children


async def collect_sitemap_urls(
    fetcher: Fetcher,
    sitemap_urls: list[str],
    *,
    limit: int = 50_000,
) -> list[DiscoveredUrl]:
    """Percorre sitemaps (incluindo índices aninhados) até `limit` URLs."""
    found: list[DiscoveredUrl] = []
    seen_sitemaps: set[str] = set()
    queue = [(url, 0) for url in sitemap_urls]

    while queue and len(found) < limit:
        sitemap_url, depth = queue.pop(0)
        if sitemap_url in seen_sitemaps or depth > MAX_SITEMAP_DEPTH:
            continue
        seen_sitemaps.add(sitemap_url)

        response = await fetcher.get(sitemap_url)
        if not response.ok or not response.body:
            continue

        pages, children = parse_sitemap(response.body, response.final_url)
        found.extend(pages)
        queue.extend((child, depth + 1) for child in children)
        log.info(
            "sitemap %s → %d urls, %d sub-sitemaps", sitemap_url, len(pages), len(children)
        )

    return found[:limit]


async def probe_common_paths(fetcher: Fetcher, origin: str) -> list[str]:
    """Testa caminhos convencionais quando o robots.txt não declara sitemap.

    Sondagem é palpite, então vale `retries=0`: sem isso, um host fora do ar
    custa 15 caminhos × 4 tentativas com backoff — dois minutos de espera antes
    de o crawl sequer começar.
    """
    hits: list[str] = []
    for path in COMMON_SITEMAP_PATHS + COMMON_FEED_PATHS:
        candidate = urljoin(origin, path)
        response = await fetcher.get(candidate, retries=0)
        if response.error is not None:
            # Erro de rede no primeiro palpite = host inalcançável, não caminho
            # errado. Continuar sondando é desperdício puro.
            log.debug("sondagem abortada em %s: %s", candidate, response.error)
            break
        if response.ok and response.body[:512].lstrip()[:1] == b"<":
            hits.append(response.final_url)
            # Um índice válido já basta; não vale sondar as variantes restantes.
            if path in COMMON_SITEMAP_PATHS:
                break
    return hits
