"""sitecrawl — crawler de estudo para extrair o conteúdo inteiro de um site.

Uso como biblioteca:

    import asyncio
    from sitecrawl import CrawlConfig, crawl

    config = CrawlConfig(start_urls=["https://exemplo.com"], max_pages=50)
    stats = asyncio.run(crawl(config))
    print(stats.summary())
"""

from .config import CrawlConfig
from .crawler import Crawler, Stats, crawl

__all__ = ["CrawlConfig", "Crawler", "Stats", "crawl"]
__version__ = "1.0.0"
