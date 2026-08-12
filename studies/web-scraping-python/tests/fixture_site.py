"""Um site de mentira, servido em localhost, para testar o crawler de verdade.

Testar crawler contra a internet é lento, não determinístico e mal-educado com
o site alvo. Um servidor local de 80 linhas dá controle total: robots.txt,
sitemap, redirect, duplicata, 429 — tudo reproduzível.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _artigo(titulo: str, paragrafos: int = 8, extra: str = "") -> str:
    corpo = "\n".join(
        f"<p>Este é o parágrafo {i} de {titulo}, com texto longo o bastante para "
        f"o extrator classificar como conteúdo principal e não como boilerplate "
        f"de navegação ou rodapé do site.</p>"
        for i in range(paragrafos)
    )
    return f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8"><title>{titulo}</title>
<meta name="description" content="Descrição de {titulo}">
<script type="application/ld+json">{{"@type":"Article","headline":"{titulo}"}}</script>
</head><body>
<nav><a href="/">Início</a> <a href="/a">A</a> <a href="/b">B</a></nav>
<article><h1>{titulo}</h1>{corpo}{extra}</article>
<footer>Rodapé com aviso de copyright que não deve entrar no conteúdo.</footer>
</body></html>"""


INDEX = """<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8"><title>Início</title></head><body>
<h1>Início</h1>
<p>Página inicial do site de teste, com links para todo o resto do conteúdo.</p>
<ul>
  <li><a href="/a">Artigo A</a></li>
  <li><a href="/b">Artigo B</a></li>
  <li><a href="/c?utm_source=newsletter">Artigo C</a></li>
  <li><a href="/c?utm_source=twitter">Artigo C de novo</a></li>
  <li><a href="/duplicado">Cópia do artigo A</a></li>
  <li><a href="/privado">Área privada</a></li>
  <li><a href="/redireciona">Vai redirecionar</a></li>
  <li><a href="/estilo.css">Folha de estilo</a></li>
  <li><a href="https://outro-dominio-qualquer.invalid/x">Site externo</a></li>
  <li><a href="mailto:contato@exemplo.com">E-mail</a></li>
</ul>
</body></html>"""

PAGINAS = {
    "/": INDEX,
    "/a": _artigo("Artigo A"),
    "/b": _artigo("Artigo B"),
    "/c": _artigo("Artigo C"),
    "/duplicado": _artigo("Artigo A"),  # conteúdo idêntico a /a
    "/so-no-sitemap": _artigo("Só no Sitemap"),  # não linkada em lugar nenhum
    "/privado": _artigo("Privado"),
}

ROBOTS = """User-agent: *
Disallow: /privado
Crawl-delay: 0
Sitemap: {base}/sitemap.xml
"""

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{base}/</loc><lastmod>2026-01-01</lastmod></url>
  <url><loc>{base}/a</loc></url>
  <url><loc>{base}/so-no-sitemap</loc></url>
</urlset>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # silencia o log no stderr do teste
        pass

    def do_GET(self) -> None:  # noqa: N802 (nome exigido pela stdlib)
        base = f"http://{self.headers.get('Host', '127.0.0.1')}"
        path = self.path.split("?")[0]
        self.server.hits.append(self.path)  # type: ignore[attr-defined]

        if path == "/robots.txt":
            return self._send(ROBOTS.format(base=base), "text/plain")
        if path == "/sitemap.xml":
            return self._send(SITEMAP.format(base=base), "application/xml")
        if path == "/redireciona":
            self.send_response(301)
            self.send_header("Location", "/b")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/estilo.css":
            return self._send("body{color:red}", "text/css")
        if path in PAGINAS:
            return self._send(PAGINAS[path], "text/html; charset=utf-8")

        self._send("<html><body>não encontrado</body></html>", "text/html", status=404)

    def _send(self, body: str, content_type: str, status: int = 200) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class FixtureSite:
    """Context manager que sobe o servidor em uma porta livre."""

    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.hits = []  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def hits(self) -> list[str]:
        return self.server.hits  # type: ignore[attr-defined]

    def __enter__(self) -> "FixtureSite":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
