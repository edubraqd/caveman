"""Configuração do crawl — um único objeto passado para todos os módulos."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .urls import host_of

DEFAULT_USER_AGENT = (
    "sitecrawl/1.0 (+https://github.com/seu-usuario/seu-repo; "
    "crawler de estudo; contato: voce@exemplo.com)"
)


@dataclass(slots=True)
class CrawlConfig:
    """Todos os botões do crawler.

    Os defaults são deliberadamente conservadores: 1 requisição por segundo por
    host, 4 workers, 500 páginas. Um crawler educado é um crawler que continua
    tendo acesso ao site amanhã.
    """

    # --- alvo -------------------------------------------------------------
    start_urls: list[str]
    allowed_hosts: set[str] = field(default_factory=set)
    allow_subdomains: bool = True
    include_patterns: list[re.Pattern[str]] = field(default_factory=list)
    exclude_patterns: list[re.Pattern[str]] = field(default_factory=list)

    # --- limites ----------------------------------------------------------
    max_pages: int = 500
    max_depth: int = 5
    max_body_bytes: int = 5 * 1024 * 1024

    # --- educação (politeness) -------------------------------------------
    concurrency: int = 4
    per_host_delay: float = 1.0
    respect_robots: bool = True
    respect_crawl_delay: bool = True
    user_agent: str = DEFAULT_USER_AGENT
    timeout: float = 20.0
    max_retries: int = 3

    # --- descoberta -------------------------------------------------------
    use_sitemaps: bool = True
    follow_links: bool = True
    include_documents: bool = False  # .pdf, .txt

    # --- renderização -----------------------------------------------------
    render: str = "never"  # never | auto | always
    render_wait_ms: int = 1500
    render_min_words: int = 60  # abaixo disso, o modo "auto" tenta o navegador

    # --- saída ------------------------------------------------------------
    output_dir: Path = Path("saida")
    write_jsonl: bool = True
    write_markdown: bool = True
    write_raw_html: bool = False
    resume: bool = True

    # --- deduplicação -----------------------------------------------------
    near_duplicate_distance: int = 3  # distância de Hamming no simhash; 0 desliga

    def __post_init__(self) -> None:
        self.output_dir = Path(self.output_dir)
        if not self.allowed_hosts:
            self.allowed_hosts = {h for h in (host_of(u) for u in self.start_urls) if h}
        if self.concurrency < 1:
            raise ValueError("concurrency precisa ser >= 1")
        if self.render not in {"never", "auto", "always"}:
            raise ValueError("render precisa ser never, auto ou always")

    def wants(self, url: str) -> bool:
        """Filtro de include/exclude por regex, aplicado sobre a URL canônica."""
        if self.exclude_patterns and any(p.search(url) for p in self.exclude_patterns):
            return False
        if self.include_patterns and not any(p.search(url) for p in self.include_patterns):
            return False
        return True
