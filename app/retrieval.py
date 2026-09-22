"""Lexical (BM25) retrieval over the manifest-approved knowledge base. Stdlib plus PyYAML only."""
from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

log = logging.getLogger("assistant")

INJECTION_PATTERNS = [
    r"ignore (all |any |the )?(previous|prior|above) instructions",
    r"system (prompt|override)",
    r"call (the )?(tool|function)",
    r"you must now",
    r"disregard (the |all )?(rules|instructions)",
    r"confirm_pending_action",
]
_INJECTION_RE = re.compile("|".join(INJECTION_PATTERNS), re.IGNORECASE)
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "be", "my", "i", "you", "your",
    "it", "this", "that", "with", "at", "do", "does", "can", "what", "how", "me", "we", "our", "will", "if",
    "before", "after", "should", "have", "has", "there", "any", "about", "am", "please",
}


@dataclass(frozen=True)
class Chunk:
    doc_id: str
    title: str
    chunk_id: str
    text: str
    language: str


@dataclass
class RetrievalResult:
    status: Literal["ok", "no_match", "error"]
    chunks: list[Chunk]


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text) if t.lower() not in STOPWORDS and len(t) > 1]


def load_manifest(kb_dir: Path) -> list[dict]:
    return yaml.safe_load((kb_dir / "manifest.yaml").read_text(encoding="utf-8"))["documents"]


def chunk_markdown(doc_id: str, title: str, language: str, text: str, max_words: int = 250) -> list[Chunk]:
    chunks: list[Chunk] = []
    for section in re.split(r"^## ", text, flags=re.MULTILINE):
        body = section.strip()
        if not body:
            continue
        words = body.split()
        pieces = [body] if len(words) <= max_words else [
            " ".join(words[i:i + max_words]) for i in range(0, len(words), max_words)]
        for piece in pieces:
            chunks.append(Chunk(doc_id, title, f"{doc_id}#{len(chunks)}", piece, language))
    return chunks


def looks_injected(text: str) -> bool:
    return _INJECTION_RE.search(text) is not None


class BM25Index:
    def __init__(self, chunks: list[Chunk], k1: float = 1.5, b: float = 0.75):
        self.chunks = chunks
        self.k1, self.b = k1, b
        self.doc_tokens = [tokenize(c.title + " " + c.text) for c in chunks]
        self.doc_len = [len(t) for t in self.doc_tokens]
        self.avgdl = (sum(self.doc_len) / len(self.doc_len)) if self.doc_len else 1.0
        self.tf = [Counter(t) for t in self.doc_tokens]
        df: Counter = Counter()
        for tokens in self.doc_tokens:
            df.update(set(tokens))
        n = len(chunks)
        self.idf = {term: math.log(1 + (n - d + 0.5) / (d + 0.5)) for term, d in df.items()}

    def score(self, query: str) -> list[tuple[float, Chunk]]:
        q = tokenize(query)
        scored = []
        for i, chunk in enumerate(self.chunks):
            s = 0.0
            for term in q:
                f = self.tf[i].get(term, 0)
                if not f:
                    continue
                norm = 1 - self.b + self.b * self.doc_len[i] / self.avgdl
                s += self.idf[term] * f * (self.k1 + 1) / (f + self.k1 * norm)
            if s > 0:
                scored.append((s, chunk))
        return sorted(scored, key=lambda x: -x[0])


class Retriever:
    def __init__(self, kb_dir: str | Path, threshold: float = 3.0, screen_enabled: bool = True):
        self.kb_dir = Path(kb_dir)
        self.threshold = threshold
        self.skipped: list[str] = []
        chunks: list[Chunk] = []
        for doc in load_manifest(self.kb_dir):
            text = (self.kb_dir / doc["file"]).read_text(encoding="utf-8")
            for chunk in chunk_markdown(doc["id"], doc["title"], str(doc.get("language", "en")), text):
                if screen_enabled and looks_injected(chunk.text):
                    self.skipped.append(chunk.chunk_id)
                    log.warning("kb_chunk_skipped_injection_screen", extra={"chunk_id": chunk.chunk_id})
                    continue
                chunks.append(chunk)
        self.index = BM25Index(chunks)

    def search(self, query: str, k: int = 3) -> RetrievalResult:
        try:
            hits = [c for s, c in self.index.score(query) if s >= self.threshold][:k]
        except Exception:  # retrieval is best effort; the turn continues without content
            log.exception("retrieval_error")
            return RetrievalResult("error", [])
        return RetrievalResult("ok" if hits else "no_match", hits)
