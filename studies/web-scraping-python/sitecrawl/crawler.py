"""O orquestrador: frontier, workers, escopo, limites e estatísticas.

Modelo de execução — uma fila (`asyncio.Queue`) e N workers assíncronos. Não há
threads: raspagem é I/O-bound, o processo passa 95% do tempo esperando a rede.
`asyncio` entrega a mesma concorrência de threads sem o custo de contexto e sem
os problemas de compartilhamento de estado (todo o código roda numa thread só).

O gargalo de CPU real é o parsing de HTML. Se ele aparecer no profile, o
caminho é `loop.run_in_executor` com um `ProcessPoolExecutor` só para a extração
— não trocar o modelo de rede.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .config import CrawlConfig
from .dedupe import NearDuplicateIndex
from .discovery import collect_sitemap_urls, probe_common_paths
from .extract import extract, looks_javascript_rendered, looks_like_block_page
from .fetch import Fetcher
from .robots import RobotsCache
from .store import OutputWriter, StateStore
from .urls import canonicalize, host_of, in_scope, looks_like_asset, looks_like_document

log = logging.getLogger("sitecrawl")


@dataclass(slots=True)
class Stats:
    seeded: int = 0
    fetched: int = 0
    saved: int = 0
    duplicates: int = 0
    skipped_robots: int = 0
    skipped_scope: int = 0
    blocked: int = 0
    errors: int = 0
    rendered: int = 0
    bytes_downloaded: int = 0
    elapsed: float = 0.0
    by_status: dict[int, int] = field(default_factory=dict)

    def summary(self) -> str:
        rate = self.fetched / self.elapsed if self.elapsed else 0
        partes = [
            f"{self.fetched} páginas em {self.elapsed:.1f}s ({rate:.1f} req/s)",
            f"{self.saved} salvas",
            f"{self.duplicates} duplicadas",
            f"{self.errors} erros",
            f"{self.skipped_robots} barradas por robots",
        ]
        if self.blocked:
            partes.append(f"{self.blocked} DESAFIOS ANTI-BOT")
        partes.append(f"{self.bytes_downloaded / 1e6:.1f} MB")
        return " — ".join(partes[:1]) + " — " + ", ".join(partes[1:])


class Crawler:
    def __init__(self, config: CrawlConfig) -> None:
        self.config = config
        self.stats = Stats()
        self.fetcher = Fetcher(config)
        self.robots = RobotsCache(self.fetcher, config.user_agent)
        self.state = StateStore(config.output_dir / "estado.sqlite3")
        self.writer = OutputWriter(
            config.output_dir,
            jsonl=config.write_jsonl,
            markdown=config.write_markdown,
            raw_html=config.write_raw_html,
        )
        self.dedupe = NearDuplicateIndex(config.near_duplicate_distance)
        self._queue: asyncio.Queue[tuple[str, int]] = asyncio.Queue()
        self._stopping = asyncio.Event()
        self._canonical_seen: set[str] = set()
        self._renderer = None

    # ------------------------------------------------------------------ run

    async def run(self) -> Stats:
        started = time.monotonic()
        try:
            await self._seed()
            if self.config.render == "always" or self.config.render == "auto":
                await self._maybe_start_renderer()

            workers = [
                asyncio.create_task(self._worker(i), name=f"worker-{i}")
                for i in range(self.config.concurrency)
            ]
            await self._queue.join()
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        finally:
            await self._shutdown()

        self.stats.elapsed = time.monotonic() - started
        self.stats.bytes_downloaded = self.fetcher.stats["bytes"]
        return self.stats

    async def _shutdown(self) -> None:
        if self._renderer is not None:
            await self._renderer.close()
        await self.fetcher.aclose()
        self.writer.close()
        self.state.close()

    async def _maybe_start_renderer(self) -> None:
        from .render import BrowserRenderer

        if not BrowserRenderer.available():
            if self.config.render == "always":
                raise RuntimeError(
                    "render=always exige Playwright: pip install playwright "
                    "&& playwright install chromium"
                )
            log.warning("Playwright ausente — modo 'auto' seguirá só com HTTP")
            return
        self._renderer = BrowserRenderer(
            user_agent=self.config.user_agent, wait_ms=self.config.render_wait_ms
        )
        await self._renderer.start()

    # --------------------------------------------------------------- semear

    async def _seed(self) -> None:
        """Popula o frontier: sitemaps primeiro, sementes depois, resume no fim."""
        candidates: list[str] = []

        if self.config.use_sitemaps:
            declared: list[str] = []
            for url in self.config.start_urls:
                if self.config.respect_robots:
                    declared += await self.robots.sitemaps(url)
            if not declared:
                for url in self.config.start_urls:
                    # Sem robots acessível não há o que sondar: ou o host está
                    # fora do ar, ou ele já nos proibiu de tudo.
                    if self.config.respect_robots and not await self.robots.allowed(url):
                        continue
                    origin = canonicalize(url).split("/", 3)[:3]
                    declared += await probe_common_paths(self.fetcher, "/".join(origin) + "/")
            if declared:
                found = await collect_sitemap_urls(self.fetcher, declared)
                candidates += [item.url for item in found]
                log.info("sitemap forneceu %d URLs", len(candidates))

        restored = 0
        if self.config.resume:
            for url, depth in self.state.pending():
                self._queue.put_nowait((url, depth))
                restored += 1
            if restored:
                log.info("retomando %d URLs pendentes do estado anterior", restored)
            for digest, sim in self.state.seen_hashes():
                if digest:
                    self.dedupe.seen_exact(digest)
                if sim and sim.isdigit():
                    self.dedupe.add(int(sim))

        # As sementes entram sempre: filtrar a URL de partida pelo mesmo regex que
        # filtra os links descobertos deixaria o crawl sem nada por onde começar
        # (`--incluir '/blog/'` a partir da home é o caso normal, não o erro).
        for url in self.config.start_urls:
            self._enqueue(url, 0, seed=True)
        for url in candidates:
            self._enqueue(url, 0)

        self.stats.seeded = self._queue.qsize()
        log.info("frontier inicial: %d URLs", self.stats.seeded)

    def _enqueue(self, raw_url: str, depth: int, *, seed: bool = False) -> bool:
        if self._stopping.is_set() or depth > self.config.max_depth:
            return False

        url = canonicalize(raw_url)
        if not url:
            return False

        if not in_scope(url, self.config.allowed_hosts, allow_subdomains=self.config.allow_subdomains):
            self.stats.skipped_scope += 1
            return False
        if not seed:
            if looks_like_asset(url) and not (
                self.config.include_documents and looks_like_document(url)
            ):
                return False
            if not self.config.wants(url):
                return False
        if not self.state.enqueue(url, depth):  # já conhecida (memória ou execução anterior)
            return False

        self._queue.put_nowait((url, depth))
        return True

    # -------------------------------------------------------------- workers

    async def _worker(self, index: int) -> None:
        while True:
            url, depth = await self._queue.get()
            try:
                if self._stopping.is_set():
                    continue
                if self.stats.fetched >= self.config.max_pages:
                    # Nada de marcar como concluída: o que sobrou na fila fica
                    # 'pending' no SQLite e o próximo --resume continua daqui.
                    self._stopping.set()
                    continue
                await self._process(url, depth)
                self.state.mark_done(url)
            except Exception:
                # Um worker que morre por exceção não processada trava o crawl
                # inteiro: a fila nunca esvazia e o join() espera para sempre.
                self.stats.errors += 1
                log.exception("erro inesperado processando %s", url)
                self.state.mark_done(url)
            finally:
                self._queue.task_done()

    async def _process(self, url: str, depth: int) -> None:
        if self.config.respect_robots and not await self.robots.allowed(url):
            self.stats.skipped_robots += 1
            self._record(url, depth, outcome="skipped", error="robots.txt")
            return

        if self.config.respect_crawl_delay:
            delay = await self.robots.crawl_delay(url)
            if delay:
                self.fetcher.throttle.set_delay(host_of(url), delay)

        response = await self.fetcher.get(url)
        self.stats.fetched += 1
        self.stats.by_status[response.status] = self.stats.by_status.get(response.status, 0) + 1

        if self.stats.fetched % 25 == 0:
            log.info(
                "progresso: %d buscadas, %d salvas, %d na fila",
                self.stats.fetched, self.stats.saved, self._queue.qsize(),
            )

        if response.error is not None:
            self.stats.errors += 1
            self._record(url, depth, status=response.status, outcome="error", error=response.error)
            return

        if not response.ok:
            self._record(url, depth, status=response.status, outcome="skipped",
                         error=f"HTTP {response.status}")
            return

        if not response.is_html:
            self._record(url, depth, status=response.status, outcome="skipped",
                         error=f"tipo {response.content_type}")
            return

        html = response.text
        content = extract(html, response.final_url)

        # SPA: o HTML servido é só a casca. Só então vale o custo do navegador.
        if self._renderer is not None and (
            self.config.render == "always"
            or looks_javascript_rendered(content, self.config.render_min_words)
        ):
            rendered_html, _ = await self._renderer.render(response.final_url)
            if rendered_html:
                html = rendered_html
                content = extract(html, response.final_url)
                self.stats.rendered += 1

        # HTTP 200 não quer dizer conteúdo: pode ser página de desafio anti-bot.
        if looks_like_block_page(content, html):
            self.stats.blocked += 1
            log.warning("página de desafio anti-bot em %s — reduza a velocidade", url)
            self.fetcher.throttle.set_delay(host_of(url), self.config.per_host_delay * 3)
            self._record(url, depth, status=response.status, outcome="blocked",
                         error="página de desafio anti-bot", content=content)
            return

        # A URL final do redirect também não precisa ser buscada de novo.
        final_canonical = canonicalize(response.final_url)
        if final_canonical != url:
            self.state.enqueue(final_canonical, depth)
            self.state.mark_done(final_canonical)

        # rel=canonical: o próprio site declarando qual URL é a oficial.
        declared = canonicalize(content.canonical) if content.canonical else ""
        identity = declared or final_canonical
        if identity in self._canonical_seen:
            self.stats.duplicates += 1
            self._record(url, depth, status=response.status, outcome="duplicate",
                         error="rel=canonical já visto", content=content)
            self._follow(content, url, depth)
            return
        self._canonical_seen.add(identity)

        if self.dedupe.seen_exact(content.content_hash):
            self.stats.duplicates += 1
            self._record(url, depth, status=response.status, outcome="duplicate",
                         error="conteúdo idêntico", content=content)
            self._follow(content, url, depth)
            return

        near = self.dedupe.find_duplicate(content.simhash)
        if near is not None:
            self.stats.duplicates += 1
            self._record(url, depth, status=response.status, outcome="duplicate",
                         error="quase-duplicado (simhash)", content=content)
            self._follow(content, url, depth)
            return
        self.dedupe.add(content.simhash)

        record = self._record(url, depth, status=response.status, outcome="saved", content=content)
        record["final_url"] = response.final_url
        record["text"] = content.text
        record["markdown"] = content.markdown
        record["description"] = content.description
        record["jsonld"] = content.jsonld
        record["images"] = content.images[:50]
        self.writer.write(record, html=html)
        self.stats.saved += 1

        self._follow(content, url, depth)

    def _follow(self, content, url: str, depth: int) -> None:
        if not self.config.follow_links or content.nofollow:
            return
        for link in content.links:
            self._enqueue(link, depth + 1)

    def _record(
        self,
        url: str,
        depth: int,
        *,
        status: int = 0,
        outcome: str,
        error: str | None = None,
        content=None,
    ) -> dict:
        record = {
            "url": url,
            "final_url": url,
            "status": status,
            "depth": depth,
            "outcome": outcome,
            "error": error,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "title": content.title if content else "",
            "lang": content.lang if content else "",
            "author": content.author if content else "",
            "published": content.published if content else "",
            "word_count": content.word_count if content else 0,
            "content_hash": content.content_hash if content else "",
            "simhash": content.simhash if content else 0,
        }
        self.state.record(record)
        return record


async def crawl(config: CrawlConfig) -> Stats:
    return await Crawler(config).run()


def stats_as_dict(stats: Stats) -> dict:
    return asdict(stats)
