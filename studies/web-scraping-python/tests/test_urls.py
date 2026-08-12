import pytest

from sitecrawl.urls import (
    canonicalize,
    in_scope,
    looks_like_asset,
    resolve,
    slugify_for_path,
)


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        # fragmento nunca chega ao servidor
        ("https://ex.com/a#secao", "https://ex.com/a"),
        # esquema e host em minúsculas, path preservado
        ("HTTPS://EX.com/Path", "https://ex.com/Path"),
        # porta padrão some
        ("https://ex.com:443/a", "https://ex.com/a"),
        ("http://ex.com:80/a", "http://ex.com/a"),
        # porta não padrão fica
        ("http://ex.com:8080/a", "http://ex.com:8080/a"),
        # barra final some
        ("https://ex.com/a/", "https://ex.com/a"),
        # raiz mantém a barra
        ("https://ex.com/", "https://ex.com/"),
        ("https://ex.com", "https://ex.com/"),
        # rastreadores somem, parâmetros reais ficam e são ordenados
        ("https://ex.com/a?utm_source=x&id=7", "https://ex.com/a?id=7"),
        ("https://ex.com/a?b=2&a=1", "https://ex.com/a?a=1&b=2"),
        ("https://ex.com/a?fbclid=xyz", "https://ex.com/a"),
        # segmentos relativos resolvidos
        ("https://ex.com/a/b/../c", "https://ex.com/a/c"),
        ("https://ex.com//a///b", "https://ex.com/a/b"),
        # IDN vira punycode
        ("https://café.com/a", "https://xn--caf-dma.com/a"),
    ],
)
def test_canonicalize(entrada, esperado):
    assert canonicalize(entrada) == esperado


def test_canonicalize_torna_variantes_iguais():
    variantes = [
        "https://ex.com/post?utm_campaign=a&id=1",
        "https://EX.com:443/post?id=1&utm_source=b#topo",
        "https://ex.com/post/?id=1",
    ]
    assert len({canonicalize(v) for v in variantes}) == 1


def test_resolve_ignora_esquemas_nao_navegaveis():
    assert resolve("https://ex.com/a", "mailto:x@y.com") is None
    assert resolve("https://ex.com/a", "javascript:void(0)") is None
    assert resolve("https://ex.com/a", "#topo") is None
    assert resolve("https://ex.com/a", "") is None
    assert resolve("https://ex.com/a/b", "../c") == "https://ex.com/c"
    assert resolve("https://ex.com/a", "//cdn.ex.com/x") == "https://cdn.ex.com/x"


def test_escopo():
    hosts = {"ex.com"}
    assert in_scope("https://ex.com/a", hosts)
    assert in_scope("https://www.ex.com/a", hosts)
    assert in_scope("https://blog.ex.com/a", hosts)
    assert not in_scope("https://blog.ex.com/a", hosts, allow_subdomains=False)
    assert not in_scope("https://outro.com/a", hosts)
    # o truque clássico de quem confia em "endswith"
    assert not in_scope("https://malicioso-ex.com/a", hosts)


def test_assets():
    assert looks_like_asset("https://ex.com/a.css")
    assert looks_like_asset("https://ex.com/img/foto.JPG")
    assert not looks_like_asset("https://ex.com/artigo")
    assert not looks_like_asset("https://ex.com/artigo.html")


def test_slug_de_arquivo():
    assert slugify_for_path("https://ex.com/blog/post-1") == "ex.com/blog/post-1"
    assert slugify_for_path("https://ex.com/") == "ex.com/index"
    assert "__" in slugify_for_path("https://ex.com/b?page=2")
