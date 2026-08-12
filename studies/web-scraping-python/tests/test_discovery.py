import gzip

from sitecrawl.discovery import parse_sitemap

URLSET = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://ex.com/a</loc><lastmod>2026-01-02</lastmod></url>
  <url><loc>https://ex.com/b</loc></url>
</urlset>"""

INDEX = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://ex.com/sitemap-1.xml</loc></sitemap>
  <sitemap><loc>/sitemap-2.xml</loc></sitemap>
</sitemapindex>"""

SEM_NAMESPACE = b"""<urlset>
  <url><loc>https://ex.com/c</loc></url>
</urlset>"""

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Blog</title><link>https://ex.com/</link>
  <item><title>Post</title><link>https://ex.com/post-1</link></item>
</channel></rss>"""


def test_urlset():
    paginas, filhos = parse_sitemap(URLSET, "https://ex.com/sitemap.xml")
    assert [p.url for p in paginas] == ["https://ex.com/a", "https://ex.com/b"]
    assert paginas[0].lastmod == "2026-01-02"
    assert filhos == []


def test_indice_resolve_loc_relativa():
    paginas, filhos = parse_sitemap(INDEX, "https://ex.com/sitemap.xml")
    assert paginas == []
    assert filhos == ["https://ex.com/sitemap-1.xml", "https://ex.com/sitemap-2.xml"]


def test_sitemap_sem_namespace():
    """Muito sitemap real omite o xmlns — findall com namespace devolveria vazio."""
    paginas, _ = parse_sitemap(SEM_NAMESPACE, "https://ex.com/s.xml")
    assert [p.url for p in paginas] == ["https://ex.com/c"]


def test_sitemap_gzip():
    paginas, _ = parse_sitemap(gzip.compress(URLSET), "https://ex.com/sitemap.xml.gz")
    assert len(paginas) == 2


def test_feed_rss():
    paginas, _ = parse_sitemap(RSS, "https://ex.com/feed")
    urls = [p.url for p in paginas]
    assert "https://ex.com/post-1" in urls


def test_xml_invalido_nao_explode():
    assert parse_sitemap(b"<isto nao e xml", "https://ex.com/s.xml") == ([], [])
    assert parse_sitemap(b"", "https://ex.com/s.xml") == ([], [])
