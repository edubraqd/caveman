"""Roteiro executável: cada estágio do crawler, rodando de verdade, um por vez.

    python passo_a_passo.py                    # alvo padrão: pypi.org
    python passo_a_passo.py https://seu-site.com

A ideia é ver o mecanismo funcionando isoladamente antes de ver o crawler
inteiro rodando. Cada passo faz pouquíssimas requisições e respeita o intervalo
educado — o roteiro completo gasta cerca de 20 requisições no alvo.

Leia junto com o README.md: cada passo aponta a seção correspondente.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from sitecrawl.config import CrawlConfig
from sitecrawl.crawler import crawl
from sitecrawl.dedupe import hamming, simhash
from sitecrawl.discovery import parse_sitemap
from sitecrawl.extract import extract, looks_like_block_page
from sitecrawl.fetch import Fetcher
from sitecrawl.robots import RobotsCache
from sitecrawl.urls import canonicalize

LARGURA = 78


def titulo(numero: int, texto: str, secao: str) -> None:
    print(f"\n{'═' * LARGURA}")
    print(f"  PASSO {numero} — {texto}")
    print(f"  README {secao}")
    print("═" * LARGURA)


def sub(texto: str) -> None:
    print(f"\n── {texto} " + "─" * max(0, LARGURA - len(texto) - 5))


async def passo_1_robots(fetcher: Fetcher, robots: RobotsCache, base: str) -> None:
    titulo(1, "robots.txt: o mapa oficial do que pode", "§4.1 e §6.4")

    resposta = await fetcher.get(f"{base}/robots.txt")
    print(f"GET /robots.txt → HTTP {resposta.status}, {len(resposta.body)} bytes\n")
    for linha in resposta.text.strip().splitlines()[:16]:
        print(f"  │ {linha}")

    sub("o parser decidindo, URL por URL")
    candidatas = [
        f"{base}/",
        f"{base}/help/",
        f"{base}/project/requests/",
        f"{base}/simple/requests/",       # Disallow: /simple/
        f"{base}/account/login/",         # Disallow: /account/
        f"{base}/pypi/requests/json",     # Disallow com curinga: /pypi/*/json
        f"{base}/search?q=http",          # Disallow com curinga: /search*
    ]
    for url in candidatas:
        permitido = await robots.allowed(url)
        marca = "✓ PODE " if permitido else "✗ BARRADA"
        print(f"  {marca}  {urlsplit(url).path}{'?' + urlsplit(url).query if urlsplit(url).query else ''}")

    sub("por que NÃO usar o urllib.robotparser da biblioteca padrão")
    from urllib.robotparser import RobotFileParser

    from sitecrawl.robots import _Parser

    stdlib = RobotFileParser()
    stdlib.parse(resposta.text.splitlines())
    nosso = _Parser(resposta.text)

    print(f"  parser em uso agora: {nosso.engine}\n")
    print("  caminho                      stdlib      em uso")
    for caminho in ("/pypi/requests/json", "/search?q=http", "/pypi?x=1", "/simple/x/"):
        url = base + caminho
        a = "PODE" if stdlib.can_fetch("bot", url) else "BARRADA"
        b = "PODE" if nosso.can_fetch("bot", url) else "BARRADA"
        alerta = "   ← a stdlib libera o que o site proibiu" if a != b else ""
        print(f"  {caminho.ljust(28)} {a.ljust(11)} {b}{alerta}")

    print("""
  O robots.txt do alvo proíbe esses caminhos com curinga (`/pypi/*/json`,
  `/search*`, `/pypi*?`). O urllib.robotparser NÃO implementa curingas: trata
  o `*` como caractere literal e libera tudo. Ele erra para o lado errado —
  você acessa o que foi proibido e nem fica sabendo.
  Por isso o sitecrawl usa `protego` quando disponível (regras do Google).""")

    atraso = await robots.crawl_delay(f"{base}/")
    print(f"\n  Crawl-delay declarado: {atraso if atraso else 'nenhum (usamos o nosso padrão)'}")

    mapas = await robots.sitemaps(f"{base}/")
    print(f"  Sitemaps declarados: {mapas or 'nenhum'}")


async def passo_2_sitemap(fetcher: Fetcher, robots: RobotsCache, base: str) -> None:
    titulo(2, "sitemap.xml: a lista pronta, sem seguir link nenhum", "§4.2")

    mapas = await robots.sitemaps(f"{base}/")
    if not mapas:
        print("  Alvo não declara sitemap; pularíamos para a sondagem de caminhos comuns.")
        return

    resposta = await fetcher.get(mapas[0])
    paginas, filhos = parse_sitemap(resposta.body, resposta.final_url)
    print(f"GET {mapas[0]} → HTTP {resposta.status}, {len(resposta.body) / 1024:.1f} KB")
    print(f"  → {len(paginas)} URLs de página, {len(filhos)} sub-sitemaps\n")

    if filhos:
        print("  É um ÍNDICE de sitemaps: aponta para outros sitemaps, não para páginas.")
        print(f"  Primeiros 3 de {len(filhos)}:")
        for filho in filhos[:3]:
            print(f"    · {filho}")

        sub("abrindo UM sub-sitemap (e parando por aí, para não abusar do alvo)")
        resposta = await fetcher.get(filhos[0])
        paginas, _ = parse_sitemap(resposta.body, resposta.final_url)
        print(f"  {filhos[0]}")
        print(f"  → {len(paginas)} URLs neste único arquivo\n")

    for pagina in paginas[:5]:
        selo = f"  (lastmod {pagina.lastmod})" if pagina.lastmod else ""
        print(f"    · {pagina.url}{selo}")

    total_estimado = len(paginas) * max(len(filhos), 1)
    print(f"\n  Uma requisição rendeu {len(paginas)} URLs.")
    if filhos:
        print(f"  O índice inteiro renderia da ordem de {total_estimado:,} URLs.".replace(",", "."))
    print("  Descobrir isso seguindo <a href> custaria milhares de requisições.")


def passo_3_canonicalizacao(base: str) -> None:
    titulo(3, "Canonicalização: cinco URLs que são a mesma página", "§6.1")

    variantes = [
        f"{base}/help/",
        f"{base}/help",
        f"{base.upper().replace('HTTPS', 'https')}/help/",
        f"{base}/help/?utm_source=newsletter&utm_campaign=abril",
        f"{base}/help/#perguntas",
        f"{base}:443/help/",
    ]
    print("  entrada".ljust(52) + "→ canônica")
    for variante in variantes:
        print(f"  {variante[:50].ljust(52)}→ {canonicalize(variante)}")

    unicas = {canonicalize(v) for v in variantes}
    print(f"\n  {len(variantes)} URLs de entrada → {len(unicas)} chave(s) no frontier.")
    print("  Sem isto, o crawler buscaria a mesma página 6 vezes e o servidor")
    print("  veria o comportamento como um ataque.")


async def passo_4_buscar(fetcher: Fetcher, base: str) -> str:
    titulo(4, "Buscar uma página: o que a camada de rede realmente entrega", "§6.3 e §6.5")

    alvo = f"{base}/help/"
    resposta = await fetcher.get(alvo)

    print(f"  URL pedida .......... {resposta.url}")
    print(f"  URL final ........... {resposta.final_url}"
          + ("   ← houve redirect" if resposta.final_url != resposta.url else ""))
    print(f"  Status .............. {resposta.status}")
    print(f"  Content-Type ........ {resposta.headers.get('content-type', '?')}")
    print(f"  Tamanho ............. {len(resposta.body) / 1024:.1f} KB")
    print(f"  Tempo ............... {resposta.elapsed * 1000:.0f} ms")
    print(f"  Servidor ............ {resposta.headers.get('server', '?')}")
    print(f"  Cache ............... {resposta.headers.get('cache-control', '?')}")

    sub("decodificação de bytes para texto")
    print(f"  charset do cabeçalho: {resposta.headers.get('content-type', '')}")
    print(f"  primeiros bytes .... {resposta.body[:40]!r}")
    print(f"  texto decodificado . {resposta.text[:60]!r}")
    print("\n  A ordem é: charset do cabeçalho → <meta charset> → utf-8 → cp1252 →")
    print("  latin-1 (que nunca falha). Site brasileiro antigo serve ISO-8859-1")
    print("  sem declarar, e é assim que 'ção' vira 'Ã§Ã£o' em 10 mil páginas.")
    return resposta.text


def passo_5_extrair(html: str, url: str) -> None:
    titulo(5, "Extração: separar o conteúdo do menu, rodapé e banner", "§7")

    inicio = time.perf_counter()
    conteudo = extract(html, url)
    duracao = (time.perf_counter() - inicio) * 1000

    print(f"  título ............. {conteudo.title[:60]}")
    print(f"  idioma ............. {conteudo.lang}")
    print(f"  descrição .......... {conteudo.description[:60]}")
    print(f"  canonical .......... {conteudo.canonical}")
    print(f"  palavras ........... {conteudo.word_count}")
    print(f"  links .............. {len(conteudo.links)}")
    print(f"  imagens ............ {len(conteudo.images)}")
    print(f"  blocos JSON-LD ..... {len(conteudo.jsonld)}")
    print(f"  noindex / nofollow . {conteudo.noindex} / {conteudo.nofollow}")
    print(f"  tempo de extração .. {duracao:.0f} ms  ← o gargalo de CPU (§5.2)")

    host = urlsplit(url).hostname or ""
    internos = [link for link in conteudo.links if (urlsplit(link).hostname or "") == host]
    print(f"\n  links internos {len(internos)} · externos {len(conteudo.links) - len(internos)}")
    print("  (só os internos alimentam o frontier)")

    if conteudo.jsonld:
        sub("JSON-LD: dado que o site publica estruturado, de propósito")
        print("  " + json.dumps(conteudo.jsonld[0], ensure_ascii=False)[:300])

    sub("markdown extraído (primeiras linhas)")
    for linha in conteudo.markdown.splitlines()[:8]:
        print(f"  │ {linha[:72]}")

    sub("o que ficou de fora")
    for ruido in ("Skip to main content", "©", "Sponsors", "Site map"):
        dentro = ruido.lower() in conteudo.text.lower()
        print(f"  {'ainda presente' if dentro else 'descartado    '}  {ruido!r}")


async def passo_6_bloqueio(fetcher: Fetcher, base: str) -> None:
    titulo(6, "HTTP 200 que NÃO é conteúdo: a falha mais traiçoeira", "§6.7")

    alvo = f"{base}/project/requests/"
    resposta = await fetcher.get(alvo)
    conteudo = extract(resposta.text, resposta.final_url)
    bloqueada = looks_like_block_page(conteudo, resposta.text)

    print(f"  GET {alvo}")
    print(f"  Status HTTP ........ {resposta.status}   ← parece sucesso")
    print(f"  Título ............. {conteudo.title!r}")
    print(f"  Palavras ........... {conteudo.word_count}")
    print(f"  Detectada bloqueio . {bloqueada}")

    if bloqueada:
        print("\n  Um crawler que só olha `status == 200` salvaria isto como se fosse")
        print("  a página do pacote. O erro só apareceria semanas depois, na hora de")
        print("  usar o dado. O sinal composto é: pouquíssimo texto E um marcador")
        print("  conhecido no HTML.")
        print(f"\n  Texto salvo seria: {conteudo.text[:160]!r}")
    else:
        print("\n  Desta vez a página veio normal. O bloqueio é intermitente — mais um")
        print("  motivo para a checagem ficar ligada em produção.")


async def passo_7_dedupe(fetcher: Fetcher, base: str, html_referencia: str) -> None:
    titulo(7, "Deduplicação de conteúdo (não de URL)", "§6.8")

    referencia = extract(html_referencia, f"{base}/help/")
    outra = await fetcher.get(f"{base}/sponsors/")
    diferente = extract(outra.text, outra.final_url)

    print(f"  SHA-256 de /help/ ......... {referencia.content_hash[:32]}…")
    print(f"  SHA-256 de /sponsors/ ..... {diferente.content_hash[:32]}…")
    print("\n  Basta um caractere de diferença para o SHA-256 mudar por inteiro — ele")
    print("  só serve para duplicata EXATA. Para o quase-duplicado, SimHash, que")
    print("  muda pouco quando o texto muda pouco:\n")

    palavras = referencia.text.split()
    hash_ref = simhash(referencia.text)

    cenarios = [
        ("a mesma página, de novo", referencia.text),
        ("+ 1 frase no rodapé", referencia.text + " Veja também os artigos relacionados."),
        ("10% do texto trocado", " ".join(palavras[: int(len(palavras) * 0.9)])),
        ("50% do texto trocado", " ".join(palavras[: len(palavras) // 2])),
        ("outra página (/sponsors/)", diferente.text),
    ]
    print(f"  {'cenário'.ljust(30)} distância   veredito")
    for rotulo, texto in cenarios:
        distancia = hamming(hash_ref, simhash(texto))
        veredito = "DUPLICATA" if distancia <= 3 else "conteúdo distinto"
        print(f"  {rotulo.ljust(30)} {distancia:9d}   {veredito}")

    print(f"""
  O documento tem {len(palavras)} palavras. Uma frase a mais não move o hash —
  e isso é o comportamento DESEJADO: significa que a mesma página, com um
  bloco de "relacionados" diferente, continua sendo reconhecida como a mesma.
  A distância só cresce quando o conteúdo realmente muda.

  Corte clássico para 64 bits: distância ≤ 3 é duplicata.""")


async def passo_8_politeness(fetcher: Fetcher, base: str) -> None:
    titulo(8, "Politeness: o intervalo entre requisições, medido", "§2 e §6.5")

    alvos = [f"{base}/help/", f"{base}/sponsors/", f"{base}/classifiers/"]
    inicio = time.monotonic()
    marcas = []
    for alvo in alvos:
        await fetcher.get(alvo)
        marcas.append(time.monotonic() - inicio)

    print(f"  intervalo configurado: {fetcher.config.per_host_delay:.1f}s por host\n")
    anterior = 0.0
    for alvo, marca in zip(alvos, marcas):
        print(f"  t+{marca:5.2f}s   (Δ {marca - anterior:4.2f}s)   {urlsplit(alvo).path}")
        anterior = marca
    print("\n  O limite é POR HOST, não global: 8 workers em 8 domínios diferentes é")
    print("  aceitável; 8 no mesmo servidor é um ataque de negação de serviço.")


async def passo_9_crawl(base: str, saida: Path) -> None:
    titulo(9, "O crawler completo, com tudo junto", "§3")

    config = CrawlConfig(
        start_urls=[f"{base}/help/"],
        output_dir=saida,
        max_pages=8,
        max_depth=2,
        per_host_delay=1.5,
        concurrency=2,
        use_sitemaps=False,  # o sitemap do alvo é gigante; aqui queremos ver os links
    )
    print(f"  semente ......... {config.start_urls[0]}")
    print(f"  limite .......... {config.max_pages} páginas, profundidade {config.max_depth}")
    print(f"  intervalo ....... {config.per_host_delay}s por host, {config.concurrency} workers")
    print("\n  rodando…\n")

    stats = await crawl(config)
    print(f"  {stats.summary()}")
    print(f"\n  status HTTP: {stats.by_status}")
    print(f"  fora de escopo (links externos ignorados): {stats.skipped_scope}")


def passo_10_inspecionar(saida: Path) -> None:
    titulo(10, "Inspecionar o resultado: o crawl 'terminou' — e daí?", "§11")

    banco = saida / "estado.sqlite3"
    if banco.exists():
        conexao = sqlite3.connect(banco)
        sub("distribuição de resultados (SQL direto no estado)")
        for resultado, quantidade in conexao.execute(
            "SELECT outcome, COUNT(*) FROM pages GROUP BY outcome ORDER BY 2 DESC"
        ):
            print(f"    {str(resultado).ljust(12)} {quantidade}")

        sub("páginas suspeitamente curtas (extrator falhando?)")
        curtas = conexao.execute(
            "SELECT url, word_count FROM pages WHERE outcome='saved' AND word_count < 100"
        ).fetchall()
        print(f"    {len(curtas)} página(s) com menos de 100 palavras")
        for url, palavras in curtas[:3]:
            print(f"      {palavras:5d}  {url}")

        sub("frontier: o que sobrou para um --resume")
        pendentes = conexao.execute(
            "SELECT COUNT(*) FROM frontier WHERE state='pending'"
        ).fetchone()[0]
        print(f"    {pendentes} URLs pendentes — rodar de novo continua daqui")
        conexao.close()

    jsonl = saida / "paginas.jsonl"
    if jsonl.exists():
        registros = [json.loads(linha) for linha in jsonl.read_text("utf-8").splitlines()]
        sub(f"paginas.jsonl — {len(registros)} registros")
        for registro in registros:
            print(f"    {registro['word_count']:6d} palavras  {registro['title'][:38].ljust(40)}"
                  f"  {urlsplit(registro['url']).path}")

    markdowns = sorted((saida / "markdown").rglob("*.md")) if (saida / "markdown").exists() else []
    if markdowns:
        sub(f"markdown/ — {len(markdowns)} arquivos; amostra de {markdowns[0].name}")
        for linha in markdowns[0].read_text("utf-8").splitlines()[:12]:
            print(f"    │ {linha[:70]}")


async def main(argv: list[str]) -> int:
    alvo = argv[1] if len(argv) > 1 else "https://pypi.org"
    base = alvo.rstrip("/")
    saida = Path("saida-passo-a-passo")

    print("╔" + "═" * (LARGURA - 2) + "╗")
    print("║" + "  ROTEIRO DE ESTUDO — cada estágio do crawler, rodando de verdade".ljust(LARGURA - 2) + "║")
    print("║" + f"  alvo: {base}".ljust(LARGURA - 2) + "║")
    print("╚" + "═" * (LARGURA - 2) + "╝")

    config = CrawlConfig(
        start_urls=[base + "/"],
        per_host_delay=1.5,
        output_dir=saida,
        user_agent="sitecrawl-estudo/1.0 (+exemplo educacional; contato: voce@exemplo.com)",
    )
    fetcher = Fetcher(config)
    robots = RobotsCache(fetcher, config.user_agent)

    try:
        await passo_1_robots(fetcher, robots, base)
        await passo_2_sitemap(fetcher, robots, base)
        passo_3_canonicalizacao(base)
        html = await passo_4_buscar(fetcher, base)
        passo_5_extrair(html, f"{base}/help/")
        await passo_6_bloqueio(fetcher, base)
        await passo_7_dedupe(fetcher, base, html)
        await passo_8_politeness(fetcher, base)
    finally:
        await fetcher.aclose()

    await passo_9_crawl(base, saida)
    passo_10_inspecionar(saida)

    print(f"\n{'═' * LARGURA}")
    print(f"  Fim. Requisições feitas ao alvo: ~20. Saída em {saida.resolve()}")
    print(f"{'═' * LARGURA}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv)))
