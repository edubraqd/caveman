"""Interface de linha de comando.

    python -m sitecrawl https://exemplo.com --max-pages 200 --saida ./saida
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path

from .config import DEFAULT_USER_AGENT, CrawlConfig
from .crawler import crawl, stats_as_dict


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sitecrawl",
        description="Extrai o conteúdo de um site inteiro para JSONL + Markdown.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""exemplos:
  # varredura padrão, educada, 200 páginas
  python -m sitecrawl https://exemplo.com --max-pages 200

  # só a seção de blog, sem sitemap, mais rápido
  python -m sitecrawl https://exemplo.com --incluir '/blog/' --sem-sitemap --delay 0.5

  # site em React que só rende conteúdo depois do JS
  python -m sitecrawl https://app.exemplo.com --render auto

  # continuar um crawl interrompido
  python -m sitecrawl https://exemplo.com --saida ./saida   # --resume é o padrão
""",
    )
    parser.add_argument("urls", nargs="+", help="uma ou mais URLs de partida")
    parser.add_argument("--saida", default="saida", help="diretório de saída (padrão: saida)")

    limites = parser.add_argument_group("limites")
    limites.add_argument("--max-pages", type=int, default=500)
    limites.add_argument("--max-depth", type=int, default=5)
    limites.add_argument("--max-mb", type=float, default=5.0, help="teto por resposta")

    escopo = parser.add_argument_group("escopo")
    escopo.add_argument("--host", action="append", default=[],
                        help="host extra permitido (repetível)")
    escopo.add_argument("--sem-subdominios", action="store_true")
    escopo.add_argument("--incluir", action="append", default=[],
                        help="regex — só URLs que casam são visitadas (repetível)")
    escopo.add_argument("--excluir", action="append", default=[],
                        help="regex — URLs que casam são ignoradas (repetível)")
    escopo.add_argument("--sem-sitemap", action="store_true")
    escopo.add_argument("--sem-links", action="store_true",
                        help="não seguir <a href>; usar só o sitemap")
    escopo.add_argument("--com-documentos", action="store_true", help="baixar .pdf/.txt também")

    educacao = parser.add_argument_group("educação")
    educacao.add_argument("--concorrencia", type=int, default=4)
    educacao.add_argument("--delay", type=float, default=1.0, help="segundos entre requisições por host")
    educacao.add_argument("--ignorar-robots", action="store_true",
                          help="NÃO use isto em site de terceiros")
    educacao.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    educacao.add_argument("--timeout", type=float, default=20.0)
    educacao.add_argument("--tentativas", type=int, default=3)

    render = parser.add_argument_group("renderização")
    render.add_argument("--render", choices=("never", "auto", "always"), default="never")
    render.add_argument("--render-wait", type=int, default=1500, help="ms após domcontentloaded")

    saida = parser.add_argument_group("saída")
    saida.add_argument("--sem-markdown", action="store_true")
    saida.add_argument("--sem-jsonl", action="store_true")
    saida.add_argument("--com-html", action="store_true", help="guardar o HTML bruto")
    saida.add_argument("--sem-resume", action="store_true")
    saida.add_argument("--dist-duplicata", type=int, default=3,
                       help="distância de Hamming do simhash; 0 desliga")
    saida.add_argument("-v", "--verbose", action="count", default=0)
    return parser


def config_from_args(args: argparse.Namespace) -> CrawlConfig:
    return CrawlConfig(
        start_urls=list(args.urls),
        allowed_hosts=set(args.host),
        allow_subdomains=not args.sem_subdominios,
        include_patterns=[re.compile(p) for p in args.incluir],
        exclude_patterns=[re.compile(p) for p in args.excluir],
        max_pages=args.max_pages,
        max_depth=args.max_depth,
        max_body_bytes=int(args.max_mb * 1024 * 1024),
        concurrency=args.concorrencia,
        per_host_delay=args.delay,
        respect_robots=not args.ignorar_robots,
        user_agent=args.user_agent,
        timeout=args.timeout,
        max_retries=args.tentativas,
        use_sitemaps=not args.sem_sitemap,
        follow_links=not args.sem_links,
        include_documents=args.com_documentos,
        render=args.render,
        render_wait_ms=args.render_wait,
        output_dir=Path(args.saida),
        write_jsonl=not args.sem_jsonl,
        write_markdown=not args.sem_markdown,
        write_raw_html=args.com_html,
        resume=not args.sem_resume,
        near_duplicate_distance=args.dist_duplicata,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose > 1 else logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    config = config_from_args(args)
    if args.ignorar_robots:
        print(
            "aviso: --ignorar-robots desliga a checagem de robots.txt. "
            "Use apenas em sites que você controla ou com autorização.",
            file=sys.stderr,
        )

    try:
        stats = asyncio.run(crawl(config))
    except KeyboardInterrupt:
        print("\ninterrompido — rode de novo com o mesmo --saida para continuar", file=sys.stderr)
        return 130

    print(stats.summary())
    (config.output_dir / "resumo.json").write_text(
        json.dumps(stats_as_dict(stats), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"saída em {config.output_dir.resolve()}")
    return 0 if stats.saved else 1


if __name__ == "__main__":
    raise SystemExit(main())
