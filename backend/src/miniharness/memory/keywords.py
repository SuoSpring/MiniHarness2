"""轻量中英文关键词规范化，不为关键词额外调用模型。"""

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from math import log1p


def normalize_keywords(values: Iterable[str] | None) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise ValueError("keywords 必须是字符串数组。")
    result = []
    for value in values or []:
        if not isinstance(value, str):
            raise ValueError("每个关键词必须是字符串。")
        word = unicodedata.normalize("NFKC", value).strip().casefold()[:60]
        if word and word not in result:
            result.append(word)
        if len(result) >= 12:
            break
    return result


def tokens(text: str) -> Iterable[str]:
    text = unicodedata.normalize("NFKC", text).casefold()
    for word in re.findall(r"[a-z0-9][a-z0-9_.+#/-]*|[\u4e00-\u9fff]+", text):
        # 中文无需额外分词依赖；二字片段配合显式关键词匹配。
        parts = (
            [word]
            if not re.fullmatch(r"[\u4e00-\u9fff]{3,}", word)
            else [word[index : index + 2] for index in range(len(word) - 1)]
        )
        yield from parts


def search_terms(query: str, keywords: list[str] | None = None) -> list[str]:
    terms = normalize_keywords(keywords)
    for part in tokens(query):
        if part not in terms:
            terms.append(part[:60])
        if len(terms) >= 32:
            break
    return terms


def bm25_rank(rows: list[dict], terms: list[str], limit: int) -> list[dict]:
    """BM25 的词频、文档频率和平均长度均取自本次有权读取的语料。"""
    if not terms or not rows:
        return []
    documents, frequencies = [], Counter()
    for row in rows:
        words = normalize_keywords(row["keywords"])
        labels = [term for word in words for term in dict.fromkeys([word, *tokens(word)])]
        indexed = [*tokens(row["key"])] * 2 + labels * 3 + list(tokens(row["content"]))
        counts = Counter(indexed)
        documents.append((len(indexed), counts))
        frequencies.update(term for term in terms if term in counts)
    average = sum(length for length, _ in documents) / len(rows) or 1
    scores = []
    for row, (length, counts) in zip(rows, documents, strict=True):
        score = 0.0
        for term in terms:
            tf = counts[term]
            if tf:
                idf = log1p((len(rows) - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
                score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / average))
        if score:
            scores.append((score, row))
    scores.sort(key=lambda item: (-item[0], item[1]["id"]))
    return [row for _, row in scores[:limit]]
