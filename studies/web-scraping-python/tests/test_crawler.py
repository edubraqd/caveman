"""Teste de ponta a ponta contra o site-fixture local."""

from __future__ import annotations

import asyncio
import json

import pytest

from sitecrawl import CrawlConfig, crawl

from .fixture_site import FixtureSite


def rodar(config: CrawlConfig):
    return asyncio.run(crawl(config))


@pytest.fixture()
def site():
    with FixtureSite() as fixture:
        yield fixture


@pytest.fixture()
def resultado(site, tmp_path):
    config = CrawlConfig(
        start_urls=[site.base_url + "/"],
        output_dir=tmp_path / "saida",
        per_host_delay=0.0,
        concurrency=4,
        max_pages=50,
    )
    stats = rodar(config)
    linhas = [
        json.loads(linha)
        for linha in (tmp_path / "saida" / "paginas.jsonl").read_text("utf-8").splitlines()
    ]
    return stats, linhas, site, tmp_path


def test_salva_conteudo(resultado):
    stats, linhas, _, _ = resultado
    assert stats.saved >= 4
    caminhos = {linha["url"].rsplit("/", 1)[-1] or "raiz" for linha in linhas}
    assert {"a", "b"} <= caminhos
    artigo = next(linha for linha in linhas if linha["url"].endswith("/a"))
    assert "parágrafo 0" in artigo["text"].lower()
    assert "Rodapé" not in artigo["text"]
    assert artigo["title"]
    assert artigo["jsonld"]


def test_respeita_robots(resultado):
    stats, linhas, site, _ = resultado
    assert stats.skipped_robots >= 1
    assert not any(linha["url"].endswith("/privado") for linha in linhas)
    # o crawler nem chegou a pedir a página bloqueada
    assert "/privado" not in site.hits


def test_descobre_url_so_do_sitemap(resultado):
    _, linhas, _, _ = resultado
    assert any(linha["url"].endswith("/so-no-sitemap") for linha in linhas), (
        "página não linkada em lugar nenhum deveria vir do sitemap.xml"
    )


def test_detecta_duplicata_exata(resultado):
    stats, linhas, _, _ = resultado
    assert stats.duplicates >= 1
    urls = {linha["url"] for linha in linhas}
    # /duplicado tem conteúdo idêntico a /a: só um dos dois é salvo
    assert not ({u for u in urls if u.endswith("/a")} and {u for u in urls if u.endswith("/duplicado")})


def test_nao_baixa_assets_nem_sai_do_dominio(resultado):
    stats, _, site, _ = resultado
    assert "/estilo.css" not in site.hits
    assert stats.skipped_scope >= 1
    assert not any("outro-dominio-qualquer" in hit for hit in site.hits)


def test_normaliza_utm_e_busca_a_pagina_uma_vez(resultado):
    _, _, site, _ = resultado
    pedidos_c = [hit for hit in site.hits if hit.startswith("/c")]
    assert len(pedidos_c) == 1, f"esperava 1 requisição a /c, houve {pedidos_c}"


def test_escreve_markdown_com_front_matter(resultado):
    _, _, _, tmp_path = resultado
    arquivos = list((tmp_path / "saida" / "markdown").rglob("*.md"))
    assert arquivos
    conteudo = arquivos[0].read_text("utf-8")
    assert conteudo.startswith("---\nurl:")


def paginas_pedidas(hits: list[str]) -> set[str]:
    """Só as páginas de conteúdo — robots.txt e sitemap.xml são relidos por rodada."""
    return {h.split("?")[0] for h in hits} - {"/robots.txt", "/sitemap.xml"}


def test_resume_nao_refaz_trabalho(site, tmp_path):
    comum = dict(
        start_urls=[site.base_url + "/"],
        output_dir=tmp_path / "saida",
        per_host_delay=0.0,
        concurrency=1,
    )
    primeira = rodar(CrawlConfig(**comum, max_pages=2))
    primeiras_paginas = paginas_pedidas(site.hits)
    site.hits.clear()

    segunda = rodar(CrawlConfig(**comum, max_pages=50))
    segundas_paginas = paginas_pedidas(site.hits)

    assert primeira.fetched == 2
    # nenhuma página processada na primeira rodada é buscada de novo na segunda
    assert primeiras_paginas & segundas_paginas == set()
    # e o que ficou pendente foi retomado
    assert segunda.saved >= 1
    assert "/b" in segundas_paginas


def test_limite_de_paginas(site, tmp_path):
    stats = rodar(
        CrawlConfig(
            start_urls=[site.base_url + "/"],
            output_dir=tmp_path / "saida",
            per_host_delay=0.0,
            max_pages=2,
            concurrency=1,
        )
    )
    assert stats.fetched <= 3  # o limite é verificado antes de cada busca


def test_profundidade_zero_so_pega_sementes(site, tmp_path):
    stats = rodar(
        CrawlConfig(
            start_urls=[site.base_url + "/"],
            output_dir=tmp_path / "saida",
            per_host_delay=0.0,
            max_depth=0,
            use_sitemaps=False,
            concurrency=1,
        )
    )
    assert stats.fetched == 1


def test_filtro_por_regex(site, tmp_path):
    import re

    stats = rodar(
        CrawlConfig(
            start_urls=[site.base_url + "/"],
            output_dir=tmp_path / "saida",
            per_host_delay=0.0,
            use_sitemaps=False,
            include_patterns=[re.compile(r"/(a|b)$")],
            concurrency=2,
        )
    )
    # a semente entra sempre; o filtro atua sobre os links descobertos:
    # raiz + /a + /b, e nada mais
    assert stats.fetched == 3
