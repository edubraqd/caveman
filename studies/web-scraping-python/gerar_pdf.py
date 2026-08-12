"""Converte o estudo (README.md) em PDF paginado.

    python gerar_pdf.py                      # README.md → estudo-web-scraping.pdf
    python gerar_pdf.py entrada.md saida.pdf

Pipeline: Markdown → HTML com CSS de impressão → Chromium headless → PDF.

Por que Chromium e não reportlab/weasyprint: o documento tem tabelas largas,
blocos de código com diagramas em arte ASCII e âncoras internas. O motor de
layout de um navegador resolve tudo isso sem trabalho manual, e o `page.pdf()`
do Playwright dá numeração de página e controle de quebra.

Requer: pip install markdown pymdown-extensions pygments playwright
"""

from __future__ import annotations

import asyncio
import re
import sys
from datetime import date
from pathlib import Path

import markdown

CSS = """
@page {
  size: A4;
  margin: 20mm 18mm 18mm 18mm;
}

:root {
  --tinta: #1a1a1a;
  --suave: #555;
  --linha: #d4d4d4;
  --destaque: #0b5cad;
  --fundo-codigo: #f6f7f9;
}

* { box-sizing: border-box; }

body {
  font-family: "DejaVu Serif", Georgia, serif;
  font-size: 10.5pt;
  line-height: 1.55;
  color: var(--tinta);
  margin: 0;
  hyphens: auto;
}

/* --- capa ------------------------------------------------------------- */
.capa {
  height: 247mm;                 /* altura útil da página A4 com as margens */
  display: flex;
  flex-direction: column;
  justify-content: center;
  page-break-after: always;
  border-top: 6px solid var(--destaque);
  padding-top: 12mm;
}
.capa h1 {
  font-size: 30pt;
  line-height: 1.15;
  margin: 0 0 6mm 0;
  border: none;
  padding: 0;
  /* Sem isto, a regra geral `h1 { page-break-before: always }` empurra o
     título para a página 2 e a capa sai em branco. */
  page-break-before: avoid;
  break-before: avoid;
}
.capa .subtitulo { font-size: 13pt; color: var(--suave); margin-bottom: 14mm; }
.capa .meta {
  font-family: "DejaVu Sans", sans-serif;
  font-size: 9.5pt;
  color: var(--suave);
  border-top: 1px solid var(--linha);
  padding-top: 4mm;
}
.capa .meta strong { color: var(--tinta); font-weight: 600; }

/* --- títulos ---------------------------------------------------------- */
h1, h2, h3, h4 {
  font-family: "DejaVu Sans", sans-serif;
  font-weight: 600;
  line-height: 1.25;
  page-break-after: avoid;
  break-after: avoid;
}
h1 {
  font-size: 19pt;
  margin: 0 0 6mm 0;
  padding-bottom: 2mm;
  border-bottom: 2px solid var(--destaque);
  page-break-before: always;
}
h1.sem-quebra { page-break-before: avoid; }
h2 { font-size: 14pt; margin: 9mm 0 3mm 0; color: #123; }
h3 { font-size: 11.5pt; margin: 6mm 0 2mm 0; }
h4 { font-size: 10.5pt; margin: 5mm 0 2mm 0; color: var(--suave); }

p { margin: 0 0 3mm 0; orphans: 3; widows: 3; }

a { color: var(--destaque); text-decoration: none; }

strong { font-weight: 600; }

/* --- código ----------------------------------------------------------- */
code {
  font-family: "DejaVu Sans Mono", monospace;
  font-size: 8.6pt;
  background: var(--fundo-codigo);
  padding: 0.5mm 1mm;
  border-radius: 2px;
}
pre {
  font-family: "DejaVu Sans Mono", monospace;
  background: var(--fundo-codigo);
  border: 1px solid var(--linha);
  border-left: 3px solid var(--destaque);
  border-radius: 3px;
  padding: 3mm 4mm;
  margin: 3mm 0 4mm 0;
  font-size: 7.9pt;
  line-height: 1.35;
  overflow: visible;
  white-space: pre-wrap;
  word-break: break-word;
  page-break-inside: avoid;
  break-inside: avoid;
}
/* font-size e line-height precisam ficar no <pre>, que é o bloco: o <code>
   interno é inline, e o strut do bloco é quem define a altura da caixa de
   linha. Ajustar só o <code> não encolhe espaçamento nenhum. */
pre code {
  background: none;
  padding: 0;
  font-size: inherit;
  line-height: inherit;
}
/* Diagramas em arte ASCII: sem quebra de linha, e line-height colado a 1 para
   que os caracteres │ de linhas consecutivas se encostem e formem uma borda
   contínua em vez de tracejada. */
pre.diagrama { white-space: pre; font-size: 6.9pt; line-height: 1.0; }
pre.diagrama code { white-space: pre; }

/* --- tabelas ---------------------------------------------------------- */
table {
  width: 100%;
  border-collapse: collapse;
  margin: 3mm 0 5mm 0;
  font-family: "DejaVu Sans", sans-serif;
  font-size: 8.8pt;
  page-break-inside: avoid;
  break-inside: avoid;
}
thead { display: table-header-group; }
th, td {
  border: 1px solid var(--linha);
  padding: 1.6mm 2.2mm;
  text-align: left;
  vertical-align: top;
}
th { background: #eef2f6; font-weight: 600; }
tbody tr:nth-child(even) { background: #fafbfc; }
td code, th code { font-size: 8pt; }

/* --- listas ----------------------------------------------------------- */
ul, ol { margin: 0 0 3mm 0; padding-left: 6mm; }
li { margin-bottom: 1mm; }
ul.task-list { list-style: none; padding-left: 2mm; }
ul.task-list li { position: relative; padding-left: 6mm; }
ul.task-list input { display: none; }
ul.task-list li::before {
  content: "\\2610";
  position: absolute;
  left: 0;
  font-size: 11pt;
  color: var(--destaque);
}

blockquote {
  margin: 3mm 0;
  padding: 1mm 4mm;
  border-left: 3px solid var(--linha);
  color: var(--suave);
}

hr { border: none; border-top: 1px solid var(--linha); margin: 6mm 0; }

/* --- sumário ---------------------------------------------------------- */
#sumario + ol { font-family: "DejaVu Sans", sans-serif; font-size: 10pt; }
#sumario + ol li { margin-bottom: 1.6mm; }
"""

MODELO = """<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8"><title>{titulo}</title>
<style>{css}</style></head><body>
<section class="capa">
  <h1>{titulo}</h1>
  <div class="subtitulo">{subtitulo}</div>
  <div class="meta">
    <strong>Implementação de referência:</strong> sitecrawl/ · 55 testes automatizados<br>
    <strong>Repositório:</strong> studies/web-scraping-python<br>
    <strong>Gerado em:</strong> {data}
  </div>
</section>
{corpo}
</body></html>"""

RODAPE = """<div style="font-family:DejaVu Sans,sans-serif;font-size:7pt;color:#777;
width:100%;padding:0 18mm;display:flex;justify-content:space-between;">
<span>Estudo profundo: extrair o conteúdo inteiro de um site com Python</span>
<span class="pageNumber"></span></div>"""

VAZIO = '<div style="display:none"></div>'


def marcar_diagramas(html: str) -> str:
    """Blocos com caracteres de desenho de caixa não podem sofrer quebra de linha."""
    def substituir(m: re.Match[str]) -> str:
        bloco = m.group(0)
        if any(c in bloco for c in "─│┌┐└┘├┤┬┴┼▶▼"):
            return bloco.replace("<pre>", '<pre class="diagrama">', 1)
        return bloco

    return re.sub(r"<pre>.*?</pre>", substituir, html, flags=re.DOTALL)


def construir_html(md_texto: str) -> tuple[str, str]:
    linhas = md_texto.splitlines()
    titulo = linhas[0].lstrip("# ").strip() if linhas else "Documento"

    # A primeira linha vira a capa; o subtítulo é o primeiro parágrafo.
    resto = "\n".join(linhas[1:]).lstrip("\n")
    subtitulo = ""
    for linha in resto.splitlines():
        if linha.strip():
            subtitulo = re.sub(r"[`*]", "", linha.strip())
            break

    corpo_md = resto
    html = markdown.markdown(
        corpo_md,
        extensions=[
            "extra",             # tabelas, listas de definição, atributos
            "sane_lists",
            "toc",
            "pymdownx.tasklist",
            "pymdownx.betterem",
        ],
        extension_configs={"toc": {"permalink": False}},
    )
    html = marcar_diagramas(html)

    # O primeiro <h1> não deve empurrar uma página em branco depois da capa.
    html = html.replace("<h1", '<h1 class="sem-quebra"', 1)

    return titulo, MODELO.format(
        titulo=titulo,
        subtitulo=subtitulo,
        css=CSS,
        corpo=html,
        data=date.today().strftime("%d/%m/%Y"),
    )


async def render_pdf(html: str, destino: Path) -> None:
    from playwright.async_api import async_playwright

    origem = destino.with_suffix(".html")
    origem.write_text(html, encoding="utf-8")

    async with async_playwright() as p:
        navegador = await p.chromium.launch(
            executable_path="/opt/pw-browsers/chromium"
            if Path("/opt/pw-browsers/chromium").exists()
            else None
        )
        pagina = await navegador.new_page()
        await pagina.goto(origem.resolve().as_uri(), wait_until="networkidle")
        await pagina.pdf(
            path=str(destino),
            format="A4",
            print_background=True,
            display_header_footer=True,
            header_template=VAZIO,
            footer_template=RODAPE,
            margin={"top": "18mm", "bottom": "16mm", "left": "18mm", "right": "18mm"},
        )
        await navegador.close()
    origem.unlink(missing_ok=True)


def main(argv: list[str]) -> int:
    entrada = Path(argv[1] if len(argv) > 1 else "README.md")
    saida = Path(argv[2] if len(argv) > 2 else "estudo-web-scraping.pdf")

    if not entrada.exists():
        print(f"não encontrei {entrada}", file=sys.stderr)
        return 1

    titulo, html = construir_html(entrada.read_text(encoding="utf-8"))
    asyncio.run(render_pdf(html, saida))
    print(f"{titulo}\n→ {saida.resolve()} ({saida.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
