"""
День 33. База знаний для поддержки: FAQ и документация из папки docs/.

Индексируются только *.md и *.txt из docs/. Тикеты и пользователи в индекс НЕ попадают:
они берутся из CRM через MCP на каждый вопрос, чтобы ответ опирался на актуальные данные.
Разбиение — structure (дни 21–28), эмбеддинги local (без сети).
"""

import os

from chunking import structure_chunks
from embeddings import make_embedder
from index_store import Index, save_index
from loaders import Document, _title

EXT = {".md": "markdown", ".txt": "text"}


def collect_kb(docs_dir: str, root: str) -> list:
    out = []
    for name in sorted(os.listdir(docs_dir)):
        path = os.path.join(docs_dir, name)
        ext = os.path.splitext(name)[1].lower()
        if not os.path.isfile(path) or ext not in EXT:
            continue
        with open(path, encoding="utf-8-sig") as f:
            text = f.read().replace("\r\n", "\n").strip()
        if text:
            rel = os.path.relpath(path, root).replace("\\", "/")
            out.append(Document(rel, name, EXT[ext], _title(EXT[ext], text, name), text))
    return out


def build_kb(root: str, index_path: str, backend: str = "local") -> dict:
    docs = collect_kb(os.path.join(root, "docs"), root)
    chunks = [c for d in docs for c in structure_chunks(d)]
    texts = [f"{c.source} {c.section}\n{c.text}" for c in chunks]
    embedder = make_embedder(backend).fit(texts)
    save_index(index_path, chunks, embedder.embed(texts), {
        "strategy": "structure", "project": "support-kb",
        "documents": [d.source for d in docs], "embedder": embedder.state()})
    return {"documents": len(docs), "chunks": len(chunks), "sources": [d.source for d in docs]}


def load_kb(index_path: str) -> Index:
    return Index(index_path)
