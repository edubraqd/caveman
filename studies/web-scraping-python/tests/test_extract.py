from sitecrawl.extract import extract, looks_javascript_rendered, looks_like_block_page

HTML = """<!DOCTYPE html>
<html lang="pt-BR"><head>
<title>Título da Página</title>
<meta name="description" content="Uma descrição curta">
<link rel="canonical" href="/artigo-oficial">
<meta name="robots" content="noindex, nofollow">
<script type="application/ld+json">{"@type":"Article","headline":"Oi"}</script>
</head><body>
<nav><a href="/menu">Menu</a></nav>
<article><h1>Título</h1>
""" + "\n".join(
    f"<p>Parágrafo {i} com texto suficientemente longo para o extrator considerar "
    f"conteúdo principal e não navegação lateral do site.</p>"
    for i in range(8)
) + """
<a href="https://externo.com/x">externo</a>
<a href="/interno" rel="nofollow">não seguir</a>
<img src="/img/foto.png">
</article>
<footer>Rodapé</footer></body></html>"""


def test_metadados():
    resultado = extract(HTML, "https://ex.com/pagina")
    assert resultado.lang == "pt-BR"
    assert resultado.description == "Uma descrição curta"
    assert resultado.canonical == "https://ex.com/artigo-oficial"
    assert resultado.noindex is True
    assert resultado.nofollow is True
    assert resultado.jsonld and resultado.jsonld[0]["headline"] == "Oi"


def test_links_absolutos_e_rel_nofollow():
    resultado = extract(HTML, "https://ex.com/pagina")
    assert "https://ex.com/menu" in resultado.links
    assert "https://externo.com/x" in resultado.links
    # link marcado com rel=nofollow não entra no frontier
    assert "https://ex.com/interno" not in resultado.links


def test_base_href_mudam_a_resolucao():
    html = '<html><head><base href="https://cdn.ex.com/v2/"></head><body><a href="pag">x</a></body></html>'
    resultado = extract(html, "https://ex.com/original")
    assert "https://cdn.ex.com/v2/pag" in resultado.links


def test_conteudo_principal_exclui_menu_e_rodape():
    resultado = extract(HTML, "https://ex.com/pagina")
    assert "Parágrafo 0" in resultado.text
    assert "Rodapé" not in resultado.text
    assert resultado.word_count > 50
    assert resultado.content_hash and resultado.simhash


def test_pagina_vazia_nao_explode():
    resultado = extract("", "https://ex.com/vazia")
    assert resultado.word_count == 0
    assert resultado.links == []


def test_detecta_pagina_de_desafio_anti_bot():
    """Caso real: pypi.org/project/requests devolveu 200 com 'Client Challenge'."""
    desafio = """<html><head><title>Client Challenge</title></head><body>
    <p>Verifying you are human. This may take a few seconds.</p>
    <script src="/cdn-cgi/challenge-platform/h/b/orchestrate"></script></body></html>"""
    assert looks_like_block_page(extract(desafio, "https://ex.com/p"), desafio) is True


def test_artigo_que_fala_de_captcha_nao_e_falso_positivo():
    artigo = HTML.replace("Parágrafo 0", "Parágrafo 0 sobre captcha e access denied")
    assert looks_like_block_page(extract(artigo, "https://ex.com/p"), artigo) is False


def test_heuristica_de_spa():
    casca = extract(
        '<html><body><div id="root"></div><script src="/app.js"></script></body></html>',
        "https://ex.com/app",
    )
    assert looks_javascript_rendered(casca, min_words=60) is True
    completa = extract(HTML, "https://ex.com/pagina")
    assert looks_javascript_rendered(completa, min_words=60) is False
