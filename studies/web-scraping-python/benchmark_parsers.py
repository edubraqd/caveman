"""Mede o custo real de cada camada: parsing de DOM e extração de conteúdo.

Rode contra páginas reais suas, não contra HTML sintético — o custo depende do
tamanho e da bagunça do documento, e HTML gerado por CMS é bem mais sujo que
qualquer fixture.

    python benchmark_parsers.py https://exemplo.com/uma-pagina
"""

from __future__ import annotations

import statistics
import sys
import time
from typing import Callable

import httpx

REPETICOES = 30


def cronometrar(fn: Callable[[], object], repeticoes: int = REPETICOES) -> tuple[float, float]:
    """Devolve (mediana, desvio) em milissegundos. Mediana porque a média é
    refém do primeiro ciclo, que paga aquecimento de cache e alocação."""
    amostras = []
    for _ in range(repeticoes):
        inicio = time.perf_counter()
        fn()
        amostras.append((time.perf_counter() - inicio) * 1000)
    return statistics.median(amostras), statistics.stdev(amostras) if len(amostras) > 1 else 0.0


def main(url: str) -> int:
    html = httpx.get(url, timeout=30, follow_redirects=True,
                     headers={"User-Agent": "sitecrawl-benchmark"}).text
    print(f"{url}\n{len(html) / 1024:.0f} KB de HTML\n")

    resultados: list[tuple[str, float, float]] = []

    from selectolax.parser import HTMLParser

    def selectolax_links():
        arvore = HTMLParser(html)
        return [n.attributes.get("href") for n in arvore.css("a[href]")]

    resultados.append(("selectolax (Lexbor, C)", *cronometrar(selectolax_links)))

    try:
        from bs4 import BeautifulSoup

        def bs4_stdlib():
            sopa = BeautifulSoup(html, "html.parser")
            return [a.get("href") for a in sopa.find_all("a", href=True)]

        resultados.append(("BeautifulSoup + html.parser", *cronometrar(bs4_stdlib)))

        def bs4_lxml():
            sopa = BeautifulSoup(html, "lxml")
            return [a.get("href") for a in sopa.find_all("a", href=True)]

        resultados.append(("BeautifulSoup + lxml", *cronometrar(bs4_lxml)))
    except ImportError:
        print("(beautifulsoup4/lxml ausentes — pulando)\n")

    try:
        import lxml.html

        def lxml_puro():
            arvore = lxml.html.fromstring(html)
            return arvore.xpath("//a/@href")

        resultados.append(("lxml.html puro", *cronometrar(lxml_puro)))
    except ImportError:
        pass

    import trafilatura

    resultados.append(
        (
            "trafilatura (extração de conteúdo)",
            *cronometrar(lambda: trafilatura.extract(html), repeticoes=10),
        )
    )

    base = min(mediana for _, mediana, _ in resultados)
    largura = max(len(nome) for nome, _, _ in resultados)
    print(f"{'operação'.ljust(largura)}  mediana     desvio   relativo")
    print("-" * (largura + 32))
    for nome, mediana, desvio in sorted(resultados, key=lambda r: r[1]):
        print(f"{nome.ljust(largura)}  {mediana:7.2f}ms  ±{desvio:5.2f}   {mediana / base:5.1f}×")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "https://pypi.org/help/"))
