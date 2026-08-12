"""Extração: de HTML bruto para conteúdo estruturado.

Duas tarefas com ferramentas diferentes:

  * **Navegação** (links, `<base>`, `rel=canonical`, `meta robots`) → parser DOM.
    Aqui usamos `selectolax` (binding do Lexbor, em C). Medido com
    `benchmark_parsers.py` em duas páginas reais de 80 KB e 513 KB: 13× e 12×
    mais rápido que BeautifulSoup com html.parser, e imune a HTML quebrado.
  * **Conteúdo** (o texto do artigo, sem menu/rodapé/banner) → `trafilatura`,
    que combina heurísticas de densidade de texto com regras de boilerplate e
    ainda devolve metadados (autor, data, título).

Escrever seletores CSS à mão para "o conteúdo principal" funciona em um site e
quebra no próximo. Extratores genéricos erram um pouco em todos, mas não exigem
manutenção por site — a escolha certa quando o alvo é "o site inteiro".
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import trafilatura
from selectolax.parser import HTMLParser

from .dedupe import content_hash, simhash
from .urls import resolve

log = logging.getLogger("sitecrawl.extract")


@dataclass(slots=True)
class Extracted:
    title: str = ""
    description: str = ""
    canonical: str | None = None
    lang: str = ""
    author: str = ""
    published: str = ""
    text: str = ""
    markdown: str = ""
    word_count: int = 0
    links: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    jsonld: list[dict] = field(default_factory=list)
    noindex: bool = False
    nofollow: bool = False
    content_hash: str = ""
    simhash: int = 0


def extract(html: str, url: str, *, want_markdown: bool = True) -> Extracted:
    tree = HTMLParser(html)
    result = Extracted()

    # <base href> muda a resolução de TODO href relativo da página. Ignorar isso
    # produz um crawler que gera 404 em massa em sites com CDN de assets.
    base_url = url
    base_node = tree.css_first("base[href]")
    if base_node:
        candidate = resolve(url, base_node.attributes.get("href", ""))
        if candidate:
            base_url = candidate

    _read_head(tree, result, base_url)
    result.links = _links(tree, base_url)
    result.images = _images(tree, base_url)
    result.jsonld = _jsonld(tree)

    body = _main_content(html, url, want_markdown=want_markdown)
    result.text = body["text"]
    result.markdown = body["markdown"]
    result.title = body["title"] or result.title
    result.author = body["author"] or result.author
    result.published = body["date"] or result.published

    if not result.text:
        # Fallback: trafilatura desiste de páginas curtas (index, listagem,
        # contato). Melhor devolver o texto do <body> limpo do que devolver nada.
        result.text = _plain_text(tree)
        result.markdown = result.markdown or result.text

    result.word_count = len(result.text.split())
    result.content_hash = content_hash(result.text)
    result.simhash = simhash(result.text)
    return result


def _read_head(tree: HTMLParser, out: Extracted, base_url: str) -> None:
    html_node = tree.css_first("html")
    if html_node:
        out.lang = (html_node.attributes.get("lang") or "").strip()

    title_node = tree.css_first("title")
    if title_node:
        out.title = (title_node.text() or "").strip()

    for selector, field_name in (
        ('meta[name="description"]', "description"),
        ('meta[property="og:description"]', "description"),
        ('meta[property="og:title"]', "title"),
        ('meta[name="author"]', "author"),
        ('meta[property="article:published_time"]', "published"),
    ):
        node = tree.css_first(selector)
        if node:
            value = (node.attributes.get("content") or "").strip()
            if value and not getattr(out, field_name):
                setattr(out, field_name, value)

    canonical = tree.css_first('link[rel="canonical"][href]')
    if canonical:
        out.canonical = resolve(base_url, canonical.attributes.get("href", ""))

    for node in tree.css('meta[name="robots"], meta[name="googlebot"]'):
        directives = (node.attributes.get("content") or "").lower()
        out.noindex = out.noindex or "noindex" in directives or "none" in directives
        out.nofollow = out.nofollow or "nofollow" in directives or "none" in directives


def _links(tree: HTMLParser, base_url: str) -> list[str]:
    seen: set[str] = set()
    links: list[str] = []
    for node in tree.css("a[href]"):
        rel = (node.attributes.get("rel") or "").lower()
        if "nofollow" in rel:
            continue
        target = resolve(base_url, node.attributes.get("href", ""))
        if target and target not in seen:
            seen.add(target)
            links.append(target)
    return links


def _images(tree: HTMLParser, base_url: str) -> list[str]:
    seen: set[str] = set()
    images: list[str] = []
    for node in tree.css("img[src], img[data-src]"):
        source = node.attributes.get("src") or node.attributes.get("data-src") or ""
        target = resolve(base_url, source)
        if target and target not in seen:
            seen.add(target)
            images.append(target)
    return images


def _jsonld(tree: HTMLParser) -> list[dict]:
    """JSON-LD é ouro: dados já estruturados que o site publica para o Google.

    Preço, autor, data, breadcrumb, receita, avaliação — tudo tipado, sem
    seletor frágil. Sempre tente daqui antes de raspar o DOM.
    """
    blocks: list[dict] = []
    for node in tree.css('script[type="application/ld+json"]'):
        raw = node.text() or ""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            blocks.append(data)
        elif isinstance(data, list):
            blocks.extend(item for item in data if isinstance(item, dict))
    return blocks


def _main_content(html: str, url: str, *, want_markdown: bool) -> dict[str, str]:
    empty = {"text": "", "markdown": "", "title": "", "author": "", "date": ""}
    try:
        raw = trafilatura.extract(
            html,
            url=url,
            output_format="json",
            with_metadata=True,
            include_comments=False,
            include_tables=True,
            include_links=False,
            favor_precision=False,
        )
    except Exception as exc:  # trafilatura levanta de tudo em HTML patológico
        log.debug("trafilatura falhou em %s: %s", url, exc)
        return empty
    if not raw:
        return empty

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return empty

    markdown = ""
    if want_markdown:
        try:
            markdown = (
                trafilatura.extract(
                    html,
                    url=url,
                    output_format="markdown",
                    include_comments=False,
                    include_tables=True,
                    include_links=True,
                    include_formatting=True,
                )
                or ""
            )
        except Exception:
            markdown = ""

    return {
        "text": (data.get("text") or "").strip(),
        "markdown": markdown.strip() or (data.get("text") or "").strip(),
        "title": (data.get("title") or "").strip(),
        "author": (data.get("author") or "").strip(),
        "date": (data.get("date") or "").strip(),
    }


def _plain_text(tree: HTMLParser) -> str:
    for node in tree.css("script, style, noscript, template, svg"):
        node.decompose()
    body = tree.css_first("body")
    if body is None:
        return ""
    return " ".join((body.text(separator=" ") or "").split())


# Marcadores de página de bloqueio / desafio anti-bot. Todos servidos com
# HTTP 200 — o status não denuncia nada.
_BLOCK_MARKERS = (
    "client challenge",
    "just a moment",
    "attention required",
    "checking your browser",
    "verifying you are human",
    "enable javascript and cookies to continue",
    "access denied",
    "acesso negado",
    "request blocked",
    "are you a robot",
    "cf-browser-verification",
    "__cf_chl",
    "/cdn-cgi/challenge-platform",
    "px-captcha",
    "_incapsula_resource",
    "g-recaptcha",
    "h-captcha",
)


def looks_like_block_page(content: Extracted, html: str) -> bool:
    """Detecta a falha mais traiçoeira do scraping: sucesso falso.

    Cloudflare, Akamai, Fastly e PerimeterX devolvem **HTTP 200** com uma página
    de desafio. Um crawler que só olha `status == 200` salva milhares de páginas
    de "Just a moment..." e só descobre semanas depois, na hora de usar o dado.

    O sinal composto é: pouquíssimo texto + um marcador conhecido no HTML.
    Exigir os dois evita marcar como bloqueio um artigo legítimo que só
    mencione "captcha" no meio do texto.
    """
    if content.word_count > 150:
        return False
    amostra = (content.title + " " + content.text + " " + html[:6000]).lower()
    return any(marker in amostra for marker in _BLOCK_MARKERS)


def looks_javascript_rendered(extracted: Extracted, min_words: int) -> bool:
    """Heurística barata: pouco texto + muitos links não é sinal de SPA.

    O sinal forte é pouco texto **e** poucos links — o HTML servido é só a
    casca (`<div id="root"></div>`) e o conteúdo vem depois por fetch.
    """
    return extracted.word_count < min_words and len(extracted.links) < 5
