"""Renderização com navegador — o último recurso, não o primeiro.

Custo real medido em ordens de grandeza: uma requisição HTTP simples gasta
~1-5 MB de RAM e dezenas de milissegundos; uma página renderizada em Chromium
gasta ~100-300 MB por contexto e 1-3 segundos. É 50-100× mais caro.

Antes de ligar o navegador, tente nesta ordem:
  1. **A API interna.** Abra o DevTools → aba Network → filtro `Fetch/XHR`. A SPA
     que você quer raspar está chamando um endpoint JSON. Chamar esse endpoint
     direto é mais rápido, mais estável e devolve dado já estruturado.
  2. **O estado embutido.** Muito framework serializa o dado no HTML:
     `__NEXT_DATA__` (Next.js), `__NUXT__` (Nuxt), `window.__INITIAL_STATE__`.
     Um `<script>` com JSON dentro resolve sem navegador nenhum.
  3. **A versão sem JS.** Alguns sites servem HTML completo para crawlers ou têm
     `/amp/`, feed RSS ou uma API pública documentada.

Só quando nada disso existe é que vale o Chromium.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("sitecrawl.render")

_STATE_PATTERNS = (
    re.compile(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL),
    re.compile(r"window\.__NUXT__\s*=\s*(\{.*?\});?\s*</script>", re.DOTALL),
    re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});?\s*</script>", re.DOTALL),
    re.compile(r"window\.__APOLLO_STATE__\s*=\s*(\{.*?\});?\s*</script>", re.DOTALL),
)


def embedded_state(html: str) -> str | None:
    """Procura o JSON de hidratação do framework antes de apelar para o browser."""
    for pattern in _STATE_PATTERNS:
        match = pattern.search(html)
        if match:
            return match.group(1).strip()
    return None


class BrowserRenderer:
    """Wrapper fino sobre o Playwright, com um contexto reaproveitado.

    Instalação (opcional):
        pip install playwright && playwright install chromium
    """

    def __init__(self, *, user_agent: str, wait_ms: int = 1500, headless: bool = True) -> None:
        self.user_agent = user_agent
        self.wait_ms = wait_ms
        self.headless = headless
        self._playwright = None
        self._browser = None
        self._context = None

    @staticmethod
    def available() -> bool:
        try:
            import playwright.async_api  # noqa: F401
        except ImportError:
            return False
        return True

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        self._context = await self._browser.new_context(
            user_agent=self.user_agent,
            viewport={"width": 1366, "height": 900},
            locale="pt-BR",
        )
        # Bloquear mídia corta metade do tempo de carregamento e quase todo o
        # tráfego. Nunca bloqueie os scripts — são eles que geram o conteúdo.
        await self._context.route(
            re.compile(r"\.(png|jpe?g|gif|webp|avif|svg|ico|woff2?|ttf|otf|mp4|webm|mp3)(\?|$)"),
            lambda route: route.abort(),
        )

    async def close(self) -> None:
        for resource in (self._context, self._browser):
            if resource is not None:
                await resource.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._context = self._browser = self._playwright = None

    async def render(self, url: str) -> tuple[str, int]:
        """Devolve (html após o JS rodar, status HTTP). `("", 0)` em falha."""
        if self._context is None:
            raise RuntimeError("chame start() antes de render()")

        page = await self._context.new_page()
        try:
            # 'networkidle' é tentador e traiçoeiro: páginas com polling ou
            # websocket nunca ficam ociosas e o wait estoura o timeout.
            # 'domcontentloaded' + espera curta é mais previsível.
            response = await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(self.wait_ms)
            html = await page.content()
            return html, (response.status if response else 0)
        except Exception as exc:
            log.warning("renderização falhou em %s: %s", url, exc)
            return "", 0
        finally:
            await page.close()
