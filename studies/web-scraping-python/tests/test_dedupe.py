from sitecrawl.dedupe import (
    NearDuplicateIndex,
    content_hash,
    hamming,
    normalize_text,
    simhash,
)

TEXTO = (
    "O crawler percorre o site inteiro respeitando o robots.txt e o intervalo "
    "entre requisições. Ele guarda o estado em SQLite para poder continuar "
    "depois de uma interrupção, e deduplica o conteúdo por hash e por simhash. "
    "A extração usa trafilatura para separar o artigo do menu e do rodapé."
)


def test_normalizacao_ignora_espaco_e_caixa():
    assert normalize_text("  Olá   MUNDO\n") == "olá mundo"
    assert content_hash("Olá mundo") == content_hash("olá   mundo ")


def test_simhash_identico_tem_distancia_zero():
    assert hamming(simhash(TEXTO), simhash(TEXTO)) == 0


def test_simhash_pequena_alteracao_fica_perto():
    modificado = TEXTO + " Veja também os artigos relacionados."
    distancia = hamming(simhash(TEXTO), simhash(modificado))
    assert distancia <= 8, f"distância {distancia} alta demais para uma frase extra"


def test_simhash_texto_diferente_fica_longe():
    outro = (
        "Receita de pão de queijo mineiro com polvilho azedo, queijo meia cura, "
        "ovos e leite morno. Asse em forno pré-aquecido por vinte e cinco minutos."
    )
    assert hamming(simhash(TEXTO), simhash(outro)) > 15


def test_indice_encontra_quase_duplicata():
    indice = NearDuplicateIndex(max_distance=3)
    a = simhash(TEXTO)
    indice.add(a)
    # o mesmo hash tem de ser encontrado
    assert indice.find_duplicate(a) == a
    # um texto sem relação nenhuma, não
    assert indice.find_duplicate(simhash("assunto totalmente diferente aqui")) is None


def test_indice_exato():
    indice = NearDuplicateIndex()
    digest = content_hash(TEXTO)
    assert indice.seen_exact(digest) is False
    assert indice.seen_exact(digest) is True
