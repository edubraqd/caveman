"""Deduplicação de conteúdo: exata (SHA-256) e aproximada (SimHash).

Um site de e-commerce serve a mesma página em `/produto/123`,
`/categoria/x/produto/123` e `/produto/123?cor=azul`. A URL canônica pega parte
disso; o resto só o conteúdo revela.

  * **SHA-256 do texto normalizado** pega o duplicado exato. Barato e sem falso
    positivo.
  * **SimHash de 64 bits** (Charikar, usado pelo Google) pega o quase-duplicado:
    duas páginas idênticas exceto pelo bloco "artigos relacionados" ficam a uma
    distância de Hamming pequena. Distância ≤ 3 é o corte clássico para 64 bits.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

_WORD = re.compile(r"\w+", re.UNICODE)
MASK64 = (1 << 64) - 1


def normalize_text(text: str) -> str:
    """Baixa o ruído antes de hashear: espaços, acentos compostos, maiúsculas."""
    text = unicodedata.normalize("NFKC", text or "")
    return " ".join(text.lower().split())


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def simhash(text: str, *, shingle: int = 3) -> int:
    """SimHash de 64 bits sobre shingles de `shingle` palavras.

    Shingles (n-gramas de palavras) em vez de palavras soltas porque a ordem
    importa: "cachorro morde homem" e "homem morde cachorro" têm as mesmas
    palavras e conteúdos bem diferentes.
    """
    tokens = _WORD.findall(normalize_text(text))
    if not tokens:
        return 0

    if len(tokens) >= shingle:
        features = [" ".join(tokens[i : i + shingle]) for i in range(len(tokens) - shingle + 1)]
    else:
        features = tokens

    vector = [0] * 64
    for feature in features:
        digest = int.from_bytes(hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest(), "big")
        for bit in range(64):
            vector[bit] += 1 if digest >> bit & 1 else -1

    result = 0
    for bit in range(64):
        if vector[bit] > 0:
            result |= 1 << bit
    return result


def hamming(a: int, b: int) -> int:
    return ((a ^ b) & MASK64).bit_count()


class NearDuplicateIndex:
    """Índice de SimHash com bandas, para não comparar contra tudo que já viu.

    Comparação linear é O(n) por página — 50 mil páginas viram 1,25 bilhão de
    comparações. Dividindo o hash em 4 bandas de 16 bits e indexando cada banda,
    dois hashes a distância ≤ 3 obrigatoriamente compartilham ao menos uma banda
    idêntica (princípio da casa dos pombos), então basta comparar os candidatos
    daquele balde.
    """

    BANDS = 4
    BAND_BITS = 16

    def __init__(self, max_distance: int = 3) -> None:
        if max_distance >= self.BANDS:
            raise ValueError("max_distance precisa ser < número de bandas (4)")
        self.max_distance = max_distance
        self._buckets: list[dict[int, list[int]]] = [{} for _ in range(self.BANDS)]
        self._exact: set[str] = set()

    def seen_exact(self, digest: str) -> bool:
        if digest in self._exact:
            return True
        self._exact.add(digest)
        return False

    def find_duplicate(self, value: int) -> int | None:
        if self.max_distance <= 0 or value == 0:
            return None
        for band in range(self.BANDS):
            key = value >> (band * self.BAND_BITS) & ((1 << self.BAND_BITS) - 1)
            for candidate in self._buckets[band].get(key, ()):
                if hamming(candidate, value) <= self.max_distance:
                    return candidate
        return None

    def add(self, value: int) -> None:
        if value == 0:
            return
        for band in range(self.BANDS):
            key = value >> (band * self.BAND_BITS) & ((1 << self.BAND_BITS) - 1)
            self._buckets[band].setdefault(key, []).append(value)
