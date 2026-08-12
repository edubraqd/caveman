"""Normalização e escopo de URLs.

O frontier de um crawler morre por duplicata. `https://x.com/a`, `http://X.com/a/`,
`https://x.com/a?utm_source=news` e `https://x.com/a#secao` são a mesma página para
o servidor e quatro páginas diferentes para um `set()` ingênuo. Este módulo reduz
todas elas à mesma chave canônica.
"""

from __future__ import annotations

import posixpath
import re
from urllib.parse import (
    parse_qsl,
    quote,
    unquote,
    urldefrag,
    urljoin,
    urlsplit,
    urlunsplit,
)

# Parâmetros que nunca mudam o conteúdo servido — só rastreiam a origem do clique.
TRACKING_PARAMS = frozenset(
    {
        "gclid", "fbclid", "msclkid", "yclid", "dclid", "gclsrc", "gbraid", "wbraid",
        "mc_cid", "mc_eid", "igshid", "ref", "ref_src", "referrer", "source",
        "spm", "scm", "_ga", "_gl", "vero_id", "vero_conv", "oly_anon_id",
        "oly_enc_id", "hsa_cam", "hsa_grp", "hsa_ad", "hsa_src", "hsa_tgt",
        "hsa_kw", "hsa_mt", "hsa_net", "hsa_ver", "_hsenc", "_hsmi", "trk", "trkCampaign",
    }
)

DEFAULT_PORTS = {"http": "80", "https": "443"}

# Extensões que quase nunca contêm texto útil para um crawler de conteúdo.
ASSET_EXTENSIONS = frozenset(
    """
    .css .js .mjs .map .json .xml .rss .atom
    .png .jpg .jpeg .gif .webp .avif .svg .ico .bmp .tif .tiff
    .mp3 .mp4 .avi .mov .wmv .flv .webm .ogg .oga .ogv .m4a .m4v .wav
    .zip .gz .bz2 .xz .7z .rar .tar .tgz .dmg .exe .msi .deb .rpm .apk
    .woff .woff2 .ttf .otf .eot
    .doc .docx .xls .xlsx .ppt .pptx .odt .ods .odp
    """.split()
)

# Documentos que valem a pena baixar mesmo não sendo HTML.
DOCUMENT_EXTENSIONS = frozenset({".pdf", ".txt", ".md"})

_MULTI_SLASH = re.compile(r"/{2,}")


def canonicalize(
    url: str,
    *,
    strip_trailing_slash: bool = True,
    keep_query: bool = True,
    drop_params: frozenset[str] = TRACKING_PARAMS,
) -> str:
    """Reduz uma URL à sua forma canônica.

    Regras aplicadas, em ordem:
      1. remove o fragmento (`#secao`) — nunca chega ao servidor;
      2. minúsculas em esquema e host (o path continua case-sensitive);
      3. remove porta padrão (`:80` em http, `:443` em https);
      4. resolve `.` e `..` e colapsa `//` no path;
      5. remove parâmetros de rastreamento e ordena o restante;
      6. remove a barra final (configurável).

    O que este normalizador *não* faz de propósito: remover `www.`. Muitos sites
    servem conteúdo diferente (ou redirecionam em loop) entre ápex e `www`.
    Deixe o redirect do servidor decidir e deduplique pela URL final.
    """
    url = url.strip()
    if not url:
        return ""

    url, _ = urldefrag(url)
    parts = urlsplit(url)

    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if not host:
        return ""

    # IDN → punycode, para que "café.com" e "xn--caf-dma.com" colidam.
    try:
        host = host.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        pass

    netloc = host
    if parts.port and str(parts.port) != DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"
    if parts.username:
        credentials = parts.username
        if parts.password:
            credentials += f":{parts.password}"
        netloc = f"{credentials}@{netloc}"

    path = _normalize_path(parts.path)

    query = ""
    if keep_query and parts.query:
        pairs = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in drop_params and not k.lower().startswith("utm_")
        ]
        # Ordenar torna ?a=1&b=2 e ?b=2&a=1 a mesma chave. Isso quebra o raríssimo
        # servidor que depende da ordem dos parâmetros — desligue via keep_query.
        pairs.sort()
        query = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in pairs)

    if strip_trailing_slash and len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    return urlunsplit((scheme, netloc, path, query, ""))


def _normalize_path(path: str) -> str:
    if not path:
        return "/"
    # Decodifica escapes desnecessários e recodifica de forma estável, para que
    # /a%2Db e /a-b não virem duas entradas distintas no frontier.
    path = quote(unquote(path), safe="/:@!$&'()*+,;=~-._")
    path = _MULTI_SLASH.sub("/", path)
    if "./" in path or path.endswith((".", "..")):
        normalized = posixpath.normpath(path)
        if path.endswith("/") and not normalized.endswith("/"):
            normalized += "/"
        path = normalized
    return path or "/"


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def in_scope(url: str, allowed_hosts: set[str], *, allow_subdomains: bool = True) -> bool:
    """A URL pertence ao site que estamos varrendo?

    `allow_subdomains` trata `blog.exemplo.com` como parte de `exemplo.com`.
    Sem isso, metade dos sites modernos (docs., blog., help.) fica de fora.
    """
    host = host_of(url)
    if not host:
        return False
    for allowed in allowed_hosts:
        allowed = allowed.lower().lstrip(".")
        if host == allowed:
            return True
        if allow_subdomains and host.endswith("." + allowed):
            return True
        # www.exemplo.com deve casar com a semente exemplo.com e vice-versa.
        if host.removeprefix("www.") == allowed.removeprefix("www."):
            return True
    return False


def resolve(base: str, href: str) -> str | None:
    """Resolve um href relativo contra a página de origem.

    Devolve `None` para tudo que não é navegável por HTTP: `mailto:`,
    `javascript:`, `tel:`, `data:` e âncoras puras.
    """
    href = (href or "").strip()
    if not href or href.startswith("#"):
        return None
    lowered = href.lower()
    if lowered.startswith(("mailto:", "javascript:", "tel:", "data:", "sms:", "ftp:", "file:")):
        return None
    try:
        absolute = urljoin(base, href)
    except ValueError:
        return None
    if not absolute.lower().startswith(("http://", "https://")):
        return None
    return absolute


def path_extension(url: str) -> str:
    path = urlsplit(url).path
    dot = path.rfind(".")
    slash = path.rfind("/")
    if dot > slash:
        return path[dot:].lower()
    return ""


def looks_like_asset(url: str) -> bool:
    return path_extension(url) in ASSET_EXTENSIONS


def looks_like_document(url: str) -> bool:
    return path_extension(url) in DOCUMENT_EXTENSIONS


def slugify_for_path(url: str, *, max_len: int = 120) -> str:
    """Transforma uma URL em um caminho de arquivo seguro e legível.

    `https://ex.com/blog/post-1?page=2` → `ex.com/blog/post-1__page=2.md`
    """
    parts = urlsplit(url)
    host = parts.hostname or "sem-host"
    path = unquote(parts.path).strip("/")
    if not path:
        path = "index"
    if parts.query:
        path = f"{path}__{parts.query}"
    path = re.sub(r"[^A-Za-z0-9/_.\-=&]+", "-", path)
    path = _MULTI_SLASH.sub("/", path).strip("-/") or "index"

    segments = []
    for segment in path.split("/"):
        segment = segment[:max_len].strip("-.") or "_"
        # Nomes reservados no Windows travam a escrita sem erro óbvio.
        if segment.upper().split(".")[0] in {
            "CON", "PRN", "AUX", "NUL",
            *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }:
            segment = "_" + segment
        segments.append(segment)
    return posixpath.join(host, *segments)
