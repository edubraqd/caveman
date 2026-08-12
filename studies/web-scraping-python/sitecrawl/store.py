"""Persistência: estado do crawl (SQLite) e saída (JSONL + Markdown).

Por que SQLite e não um `set()` na memória: um crawl de site grande leva horas.
Ele vai cair — rede, deploy, `Ctrl-C`, OOM. Sem estado em disco você recomeça do
zero e bate no servidor de novo com as mesmas 10 mil requisições. Com estado, o
`--resume` continua de onde parou.

Por que JSONL e não um JSON só: JSONL é gravado incrementalmente, sobrevive a um
processo morto no meio (perde-se no máximo a última linha) e é lido em streaming
por qualquer ferramenta (`jq`, pandas, DuckDB) sem carregar tudo na RAM.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .urls import slugify_for_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    url           TEXT PRIMARY KEY,
    final_url     TEXT,
    status        INTEGER,
    depth         INTEGER,
    title         TEXT,
    word_count    INTEGER,
    content_hash  TEXT,
    simhash       TEXT,
    outcome       TEXT,           -- saved | duplicate | skipped | error
    error         TEXT,
    fetched_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_pages_hash ON pages(content_hash);
CREATE INDEX IF NOT EXISTS idx_pages_outcome ON pages(outcome);

CREATE TABLE IF NOT EXISTS frontier (
    url    TEXT PRIMARY KEY,
    depth  INTEGER NOT NULL,
    state  TEXT NOT NULL DEFAULT 'pending'   -- pending | done
);
CREATE INDEX IF NOT EXISTS idx_frontier_state ON frontier(state);
"""


class StateStore:
    """Estado do crawl em SQLite, seguro para uso concorrente.

    O loop do crawler é assíncrono mas roda em uma thread só; ainda assim o lock
    protege contra o uso a partir de um executor (renderização com Playwright,
    por exemplo) e o `check_same_thread=False` deixa isso legal.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(SCHEMA)
        # WAL: leitura não bloqueia escrita — dá para inspecionar o progresso
        # com outro processo enquanto o crawl roda.
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.commit()
            self._db.close()

    # --- frontier ---------------------------------------------------------

    def enqueue(self, url: str, depth: int) -> bool:
        """Insere no frontier. Devolve False se a URL já era conhecida."""
        with self._lock:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO frontier (url, depth) VALUES (?, ?)", (url, depth)
            )
            self._db.commit()
            return cursor.rowcount > 0

    def mark_done(self, url: str) -> None:
        with self._lock:
            self._db.execute("UPDATE frontier SET state='done' WHERE url=?", (url,))

    def pending(self) -> list[tuple[str, int]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT url, depth FROM frontier WHERE state='pending' ORDER BY depth, rowid"
            ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def known_urls(self) -> set[str]:
        with self._lock:
            return {row[0] for row in self._db.execute("SELECT url FROM frontier")}

    # --- páginas ----------------------------------------------------------

    def record(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                """INSERT OR REPLACE INTO pages
                   (url, final_url, status, depth, title, word_count,
                    content_hash, simhash, outcome, error, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    record["url"],
                    record.get("final_url"),
                    record.get("status"),
                    record.get("depth"),
                    record.get("title"),
                    record.get("word_count"),
                    record.get("content_hash"),
                    str(record.get("simhash", "")),
                    record.get("outcome"),
                    record.get("error"),
                    record.get("fetched_at") or datetime.now(timezone.utc).isoformat(),
                ),
            )
            self._db.commit()

    def seen_hashes(self) -> Iterator[tuple[str, str]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT content_hash, simhash FROM pages WHERE outcome='saved'"
            ).fetchall()
        return iter(rows)

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT outcome, COUNT(*) FROM pages GROUP BY outcome"
            ).fetchall()
        return {row[0] or "?": row[1] for row in rows}


class OutputWriter:
    """Escreve o conteúdo extraído em JSONL, Markdown e (opcional) HTML bruto."""

    def __init__(
        self,
        output_dir: Path,
        *,
        jsonl: bool = True,
        markdown: bool = True,
        raw_html: bool = False,
    ) -> None:
        self.dir = Path(output_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.markdown = markdown
        self.raw_html = raw_html
        self._jsonl = (
            (self.dir / "paginas.jsonl").open("a", encoding="utf-8") if jsonl else None
        )

    def close(self) -> None:
        if self._jsonl:
            self._jsonl.close()

    def write(self, page: dict[str, Any], *, html: str = "") -> None:
        if self._jsonl:
            self._jsonl.write(json.dumps(page, ensure_ascii=False) + "\n")
            self._jsonl.flush()  # crash-safety vale mais que throughput aqui

        if self.markdown and page.get("markdown"):
            target = self.dir / "markdown" / (slugify_for_path(page["url"]) + ".md")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_front_matter(page) + page["markdown"] + "\n", encoding="utf-8")

        if self.raw_html and html:
            target = self.dir / "html" / (slugify_for_path(page["url"]) + ".html")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(html, encoding="utf-8")


def _front_matter(page: dict[str, Any]) -> str:
    """Cabeçalho YAML para que cada .md continue rastreável até a origem."""
    fields = {
        "url": page.get("url", ""),
        "title": page.get("title", ""),
        "lang": page.get("lang", ""),
        "published": page.get("published", ""),
        "author": page.get("author", ""),
        "fetched_at": page.get("fetched_at", ""),
        "word_count": page.get("word_count", 0),
    }
    lines = ["---"]
    for key, value in fields.items():
        if value in ("", None, 0):
            continue
        if isinstance(value, int):
            lines.append(f"{key}: {value}")
            continue
        text = str(value).replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{key}: "{text}"')
    lines.append("---\n\n")
    return "\n".join(lines)


def to_record(obj: Any) -> dict[str, Any]:
    return asdict(obj) if hasattr(obj, "__dataclass_fields__") else dict(obj)
