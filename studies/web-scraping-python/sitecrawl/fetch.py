"""Camada de rede: HTTP assíncrono com educação, retry e limite de corpo.

Três coisas que separam um fetcher de brinquedo de um utilizável:
  1. **Politeness por host** — o limite é por servidor, não global. 16 workers
     batendo em 16 hosts diferentes é aceitável; 16 no mesmo host é um ataque.
  2. **Backoff que obedece o servidor** — `429` e `503` costumam vir com
     `Retry-After`. Ignorar esse cabeçalho é o caminho mais rápido para um ban.
  3. **Teto de download** — um único `.iso` linkado por engano estoura a memória
     do processo. Streaming + corte por tamanho resolve.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import ssl
import time
from dataclasses import dataclass, field

import httpx

from .config import CrawlConfig
from .urls import host_of

log = logging.getLogger("sitecrawl.fetch")

RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
# httpx.HTTPError cobre timeout, redirect e protocolo; ssl/OSError cobrem DNS,
# handshake e conexão recusada, que escapam da hierarquia do httpx.
NETWORK_ERRORS = (httpx.HTTPError, ssl.SSLError, OSError)
_META_CHARSET = re.compile(
    rb"""<meta[^>]+charset=["']?\s*([a-zA-Z0-9_\-]+)""", re.IGNORECASE
)


@dataclass(slots=True)
class Response:
    url: str  # URL pedida (canônica)
    final_url: str  # após redirects
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    text: str = ""
    elapsed: float = 0.0
    error: str | None = None
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    @property
    def is_html(self) -> bool:
        return self.content_type in {"text/html", "application/xhtml+xml", ""}


class HostThrottle:
    """Serializa e espaça as requisições de cada host."""

    def __init__(self, default_delay: float) -> None:
        self._default = default_delay
        self._delays: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    def set_delay(self, host: str, delay: float) -> None:
        # Só aumentamos o intervalo: se o robots.txt pede 10s, respeitamos 10s
        # mesmo que a linha de comando peça 0.5s.
        self._delays[host] = max(delay, self._delays.get(host, self._default))

    async def wait(self, host: str) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        delay = self._delays.get(host, self._default)
        async with lock:
            elapsed = time.monotonic() - self._last.get(host, 0.0)
            if elapsed < delay:
                await asyncio.sleep(delay - elapsed)
            self._last[host] = time.monotonic()


class Fetcher:
    """Cliente HTTP assíncrono compartilhado por todos os workers."""

    def __init__(self, config: CrawlConfig) -> None:
        self.config = config
        self.throttle = HostThrottle(config.per_host_delay)
        limits = httpx.Limits(
            max_connections=config.concurrency * 2,
            max_keepalive_connections=config.concurrency,
        )
        self._client = httpx.AsyncClient(
            follow_redirects=True,
            max_redirects=5,
            timeout=httpx.Timeout(config.timeout, connect=min(10.0, config.timeout)),
            limits=limits,
            headers={
                "User-Agent": config.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
            },
        )
        self.stats: dict[str, int] = {
            "requests": 0, "retries": 0, "errors": 0, "bytes": 0,
        }

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(self, url: str, *, polite: bool = True, retries: int | None = None) -> Response:
        """`retries=0` para requisições especulativas (sondagem de sitemap, por
        exemplo): repetir 4 vezes cada palpite contra um host fora do ar
        transforma uma sondagem de 2s em uma espera de 2 minutos."""
        host = host_of(url)
        last_error: str | None = None
        max_retries = self.config.max_retries if retries is None else retries

        for attempt in range(max_retries + 1):
            if polite:
                await self.throttle.wait(host)

            started = time.monotonic()
            try:
                response = await self._stream(url)
            except NETWORK_ERRORS as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                self.stats["retries"] += 1
                if attempt < max_retries:
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                break

            self.stats["requests"] += 1
            self.stats["bytes"] += len(response.body)
            response.elapsed = time.monotonic() - started

            if response.status in RETRY_STATUS and attempt < max_retries:
                self.stats["retries"] += 1
                await asyncio.sleep(self._backoff(attempt, response.headers))
                # Um 429 é o servidor dizendo "devagar". Ficar mais lento com ele
                # é permanente para o resto do crawl, não só nesta tentativa.
                if response.status == 429:
                    self.throttle.set_delay(host, self.config.per_host_delay * 2)
                continue

            return response

        self.stats["errors"] += 1
        log.debug("falha definitiva em %s: %s", url, last_error)
        return Response(url=url, final_url=url, status=0, error=last_error or "erro desconhecido")

    async def _stream(self, url: str) -> Response:
        """GET com corte de corpo por tamanho.

        `client.get()` baixaria o arquivo inteiro antes de devolver o controle.
        Com `stream()` cortamos no limite configurado e abandonamos a conexão.
        """
        chunks: list[bytes] = []
        total = 0
        truncated = False

        async with self._client.stream("GET", url) as response:
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > self.config.max_body_bytes:
                await response.aclose()
                return Response(
                    url=url,
                    final_url=str(response.url),
                    status=response.status_code,
                    headers=dict(response.headers),
                    truncated=True,
                )
            async for chunk in response.aiter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total >= self.config.max_body_bytes:
                    truncated = True
                    break

            body = b"".join(chunks)
            result = Response(
                url=url,
                final_url=str(response.url),
                status=response.status_code,
                headers={k.lower(): v for k, v in response.headers.items()},
                body=body,
                truncated=truncated,
            )
        result.text = decode(body, result.headers.get("content-type", ""))
        return result

    def _backoff(self, attempt: int, headers: dict[str, str] | None = None) -> float:
        if headers:
            retry_after = headers.get("retry-after")
            if retry_after:
                try:
                    # Só a forma numérica; a forma HTTP-date é rara e cara de parsear.
                    return min(float(retry_after), 60.0)
                except ValueError:
                    pass
        # Exponencial com jitter: sem o jitter, N workers que falharam juntos
        # voltam juntos e derrubam o servidor de novo no mesmo instante.
        return min(2**attempt, 30.0) * (0.5 + random.random())


def decode(body: bytes, content_type: str) -> str:
    """Decodifica bytes para texto na ordem em que a especificação manda.

    Cabeçalho HTTP → `<meta charset>` → UTF-8 → latin-1 (que nunca falha).
    Sites brasileiros antigos ainda servem ISO-8859-1 sem declarar no header.
    """
    if not body:
        return ""

    for encoding in _charset_candidates(body, content_type):
        try:
            return body.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("latin-1", errors="replace")


def _charset_candidates(body: bytes, content_type: str) -> list[str]:
    candidates: list[str] = []
    if "charset=" in content_type:
        candidates.append(content_type.split("charset=")[-1].split(";")[0].strip().strip('"\''))
    match = _META_CHARSET.search(body[:4096])
    if match:
        candidates.append(match.group(1).decode("ascii", errors="ignore"))
    candidates += ["utf-8", "cp1252"]
    seen: set[str] = set()
    return [c for c in candidates if c and not (c.lower() in seen or seen.add(c.lower()))]
