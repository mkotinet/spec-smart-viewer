"""다중 키워드 조합 검색 AST 파서 · SQLite FTS5/Trigram 하이브리드 실행기 · 컬러 스니펫/BBox 매처."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TermNode:
    value: str
    is_phrase: bool = False


@dataclass(frozen=True)
class NotNode:
    child: ASTNode


@dataclass(frozen=True)
class AndNode:
    left: ASTNode
    right: ASTNode


@dataclass(frozen=True)
class OrNode:
    left: ASTNode
    right: ASTNode


ASTNode = TermNode | NotNode | AndNode | OrNode


def _tokenize(raw: str) -> list[tuple[str, str]]:
    """검색 쿼리를 (, ), |, &, AND, OR, NOT, -, +, PHRASE, TERM 토큰으로 분리."""
    tokens: list[tuple[str, str]] = []
    s = (raw or "").strip()
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch.isspace():
            i += 1
            continue
        if ch in "()":
            tokens.append((ch, ch))
            i += 1
            continue
        if ch == "|":
            tokens.append(("OR", "|"))
            i += 1
            continue
        if ch == "&":
            tokens.append(("AND", "&"))
            i += 1
            continue
        if ch == '"' or ch == "'":
            quote = ch
            i += 1
            buf: list[str] = []
            while i < n and s[i] != quote:
                buf.append(s[i])
                i += 1
            if i < n and s[i] == quote:
                i += 1
            phrase = " ".join("".join(buf).split())
            if phrase:
                tokens.append(("PHRASE", phrase))
            continue

        # 일반 단어 읽기 (공백, 괄호, |, &, 따옴표 전까지; 단어 중간의 '-'는 보존)
        j = i
        while j < n and not s[j].isspace() and s[j] not in '()|&"\'':
            j += 1
        word = s[i:j]
        i = j
        if not word:
            continue
        up = word.upper()
        if up == "AND" or word == "+":
            tokens.append(("AND", word))
        elif up == "OR":
            tokens.append(("OR", word))
        elif up == "NOT" or word in {"-", "!"}:
            tokens.append(("NOT", word))
        elif word[0] in {"-", "!"} and len(word) > 1:
            tokens.append(("NOT", word[0]))
            tokens.append(("TERM", word[1:]))
        elif word[0] == "+" and len(word) > 1:
            if tokens and tokens[-1][0] in {"TERM", "PHRASE", ")"}:
                tokens.append(("AND", "+"))
            tokens.append(("TERM", word[1:]))
        else:
            tokens.append(("TERM", word))
    return tokens


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]):
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def consume(self) -> tuple[str, str]:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def parse(self) -> ASTNode | None:
        if not self.tokens:
            return None
        node = self.parse_or()
        return node

    def parse_or(self) -> ASTNode | None:
        left = self.parse_and()
        if left is None:
            return None
        while (p := self.peek()) is not None and p[0] == "OR":
            self.consume()
            right = self.parse_and()
            if right is not None:
                left = OrNode(left, right)
        return left

    def parse_and(self) -> ASTNode | None:
        left = self.parse_not()
        if left is None:
            return None
        while (p := self.peek()) is not None:
            if p[0] == "AND":
                self.consume()
                right = self.parse_not()
                if right is not None:
                    left = AndNode(left, right)
            elif p[0] in {"(", "NOT", "TERM", "PHRASE"}:
                right = self.parse_not()
                if right is not None:
                    left = AndNode(left, right)
            else:
                break
        return left

    def parse_not(self) -> ASTNode | None:
        p = self.peek()
        if p is not None and p[0] == "NOT":
            self.consume()
            child = self.parse_not()
            if child is not None:
                return NotNode(child)
            return None
        return self.parse_primary()

    def parse_primary(self) -> ASTNode | None:
        p = self.peek()
        if p is None:
            return None
        if p[0] == "(":
            self.consume()
            inner = self.parse_or()
            if self.peek() and self.peek()[0] == ")":
                self.consume()
            return inner
        if p[0] in {"TERM", "PHRASE"}:
            self.consume()
            return TermNode(p[1], is_phrase=(p[0] == "PHRASE"))
        # 예상치 못한 닫는 괄호 등은 건너뜀
        self.consume()
        return self.parse_primary()


def parse_query(raw: str) -> ASTNode | None:
    """다중 키워드 조합 검색 문자열을 AST로 파싱한다."""
    return _Parser(_tokenize(raw)).parse()


def extract_positive_terms(node: ASTNode | None) -> list[str]:
    """AST에서 부정(NOT) 하위 트리를 제외한 긍정 검색 키워드 목록을 중복 없이 추출."""
    seen: set[str] = set()
    ordered: list[str] = []

    def _walk(n: ASTNode | None, negated: bool) -> None:
        if n is None:
            return
        if isinstance(n, TermNode):
            if not negated:
                k = n.value.lower()
                if k not in seen:
                    seen.add(k)
                    ordered.append(n.value)
        elif isinstance(n, NotNode):
            _walk(n.child, not negated)
        elif isinstance(n, (AndNode, OrNode)):
            _walk(n.left, negated)
            _walk(n.right, negated)

    _walk(node, False)
    return ordered


def _escape_like(raw: str) -> str:
    return raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fts_phrase_literal(raw: str) -> str:
    clean = raw.replace('"', ' ').strip()
    if not clean:
        return ""
    return f'"{clean}"'


def _match_atomic_pages(
    conn: sqlite3.Connection,
    term: TermNode,
    has_trigram: bool,
    universe_sql: str,
    universe_params: list[Any],
) -> set[str]:
    val = term.value.strip()
    if not val:
        return set()

    norm_ws = " ".join(val.split()).lower()
    if term.is_phrase:
        like = f"%{_escape_like(norm_ws)}%"
        # 3글자 이상이면 trigram으로 1차 축소 가능, 최종적으로 공백 정규화 일치 확인
        rows = conn.execute(
            f"""
            SELECT p.page_id, d.spec_code, d.title AS doc_title, p.section_no, p.section_title, p.clean_text
            FROM pages p
            JOIN documents d ON d.doc_id = p.doc_id
            WHERE {universe_sql}
              AND (LOWER(p.clean_text) LIKE ? ESCAPE '\\'
                   OR LOWER(p.section_title) LIKE ? ESCAPE '\\'
                   OR LOWER(d.title) LIKE ? ESCAPE '\\')
            """,
            [*universe_params, like, like, like],
        ).fetchall()
        matched: set[str] = set()
        for r in rows:
            joined = " ".join(
                " ".join(x.split())
                for x in (r["spec_code"], r["doc_title"], r["section_no"], r["section_title"], r["clean_text"])
                if x
            ).lower()
            if norm_ws in joined:
                matched.add(r["page_id"])
        return matched

    matched_ids: set[str] = set()
    lit = _fts_phrase_literal(val)
    if lit:
        # FTS5 토큰 접두사(대소문자 무시, 영숫자/단어)
        if re.fullmatch(r"[A-Za-z0-9가-힣_]+", val):
            fts_q = f"{lit}*"
        else:
            fts_q = lit
        try:
            for r in conn.execute(
                "SELECT page_id FROM pages_fts WHERE pages_fts MATCH ?",
                (fts_q,),
            ).fetchall():
                matched_ids.add(r[0])
        except sqlite3.OperationalError:
            pass

    if has_trigram and len(val) >= 3:
        tri_lit = _fts_phrase_literal(val)
        if tri_lit:
            try:
                for r in conn.execute(
                    "SELECT page_id FROM pages_trigram WHERE pages_trigram MATCH ?",
                    (tri_lit,),
                ).fetchall():
                    matched_ids.add(r[0])
            except sqlite3.OperationalError:
                pass

    # 짧은 1~2글자 한국어('차압', '덕트')나 특수문자 포함 코드('N-Logic', '3.14') 보완
    like = f"%{_escape_like(val)}%"
    for r in conn.execute(
        f"""
        SELECT p.page_id
        FROM pages p
        JOIN documents d ON d.doc_id = p.doc_id
        WHERE {universe_sql}
          AND (p.clean_text LIKE ? ESCAPE '\\'
               OR p.section_title LIKE ? ESCAPE '\\'
               OR p.section_no LIKE ? ESCAPE '\\'
               OR d.title LIKE ? ESCAPE '\\'
               OR d.spec_code LIKE ? ESCAPE '\\')
        """,
        [*universe_params, like, like, like, like, like],
    ).fetchall():
        matched_ids.add(r[0])

    return matched_ids


def _build_multi_snippet(text: str, terms: list[str], fallback_title: str = "") -> str:
    src = " ".join((text or "").split())
    if not src:
        src = " ".join((fallback_title or "").split())
    if not src:
        return ""
    if not terms:
        return src[:160] + ("…" if len(src) > 160 else "")

    low = src.lower()
    # 모든 키워드의 등장 위치 수집
    hit_positions: list[tuple[int, int]] = []
    for idx, t in enumerate(terms):
        tl = t.lower()
        pos = low.find(tl)
        if pos >= 0:
            hit_positions.append((pos, idx))

    if not hit_positions:
        return src[:160] + ("…" if len(src) > 160 else "")

    # 가장 많은 서로 다른 키워드가 몰려 있는 160자 창 선택
    best_start = max(0, hit_positions[0][0] - 40)
    best_score = -1
    for pos, _ in hit_positions:
        w_start = max(0, pos - 40)
        w_end = w_start + 170
        distinct = len({tl for tl in terms if tl.lower() in low[w_start:w_end]})
        if distinct > best_score:
            best_score = distinct
            best_start = w_start

    window = src[best_start:best_start + 170]
    prefix = "…" if best_start > 0 else ""
    suffix = "…" if best_start + 170 < len(src) else ""

    # 긴 키워드 우선 치환으로 중첩 마커 손상 방지
    indexed_terms = sorted(enumerate(terms), key=lambda x: len(x[1]), reverse=True)
    # 단일 패스 정규식 치환
    patterns: list[str] = []
    idx_by_lower: dict[str, int] = {}
    for idx, term in indexed_terms:
        tl = term.lower()
        if tl and tl not in idx_by_lower:
            idx_by_lower[tl] = idx % 4
            patterns.append(re.escape(term))
    if not patterns:
        return prefix + window + suffix

    rx = re.compile("|".join(patterns), re.IGNORECASE)
    highlighted = rx.sub(
        lambda m: f"[[H{idx_by_lower.get(m.group(0).lower(), 0)}]]{m.group(0)}[[/H{idx_by_lower.get(m.group(0).lower(), 0)}]]",
        window,
    )
    return prefix + highlighted + suffix


def _score_page(row: sqlite3.Row, terms: list[str]) -> tuple[int, int, int]:
    """관련도 점수 계산 (높을수록 상위). 반환: (-score, doc_order_key, page_no)."""
    if not terms:
        return (0, 0, int(row["page_no"]))

    title_blob = f"{row['doc_title']} {row['section_no']} {row['section_title']}".lower()
    body_low = (row["clean_text"] or "").lower()
    score = 0
    matched_positions: list[int] = []

    for t in terms:
        tl = t.lower()
        if tl in title_blob:
            score += 100
        cnt = body_low.count(tl)
        if cnt > 0:
            score += 30 + min(cnt, 5) * 4
            matched_positions.append(body_low.find(tl))

    # 키워드 근접도 보너스 (2개 이상 키워드가 150자 이내 공존)
    if len(matched_positions) >= 2:
        matched_positions.sort()
        for a, b in zip(matched_positions, matched_positions[1:]):
            if b - a <= 150:
                score += 20
                break

    if row["page_type"] == "body":
        score += 25
    elif row["page_type"] == "diagram":
        score += 15
    elif row["page_type"] in {"toc", "history"}:
        score -= 10

    return (-score, 0, int(row["page_no"]))


def search_pages(
    conn: sqlite3.Connection,
    q: str = "",
    doc_ids: list[str] | None = None,
    page_type: str = "",
    body_only: bool = False,
    scope: str = "page",
    page_ids_filter: set[str] | None = None,
    page: int = 1,
    limit: int = 40,
) -> dict[str, Any]:
    """시방서 장표 목록 조회 및 다중 키워드 조합 검색 실행."""
    where_parts: list[str] = ["1=1"]
    params: list[Any] = []

    if doc_ids:
        clean_docs = [d.strip() for d in doc_ids if d.strip()]
        if clean_docs:
            ph = ",".join("?" for _ in clean_docs)
            where_parts.append(f"p.doc_id IN ({ph})")
            params.extend(clean_docs)
    if page_type:
        where_parts.append("p.page_type = ?")
        params.append(page_type.strip())
    elif body_only:
        where_parts.append("p.page_type IN ('body', 'diagram', 'appendix')")
    if page_ids_filter is not None:
        if not page_ids_filter:
            return {"total": 0, "page": page, "limit": limit, "terms": [], "items": []}
        ph = ",".join("?" for _ in page_ids_filter)
        where_parts.append(f"p.page_id IN ({ph})")
        params.extend(sorted(page_ids_filter))

    universe_sql = " AND ".join(where_parts)
    q_clean = (q or "").strip()
    offset = max(0, (page - 1) * limit)

    if not q_clean:
        total = conn.execute(
            f"SELECT COUNT(*) FROM pages p JOIN documents d ON d.doc_id = p.doc_id WHERE {universe_sql}",
            params,
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT p.page_id, p.doc_id, d.spec_code, d.kh_code, d.title AS doc_title,
                   p.page_no, p.total_pages, p.page_type, p.section_no, p.section_title,
                   p.width_px, p.height_px, p.sha256, p.has_preview, p.has_pdf, p.ocr_status,
                   p.clean_text
            FROM pages p
            JOIN documents d ON d.doc_id = p.doc_id
            WHERE {universe_sql}
            ORDER BY d.spec_code, d.title, p.page_no
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["v"] = d["sha256"][:8]
            d["snippet"] = _build_multi_snippet(d.pop("clean_text", ""), [], d.get("section_title", ""))
            items.append(d)
        return {"total": total, "page": page, "limit": limit, "terms": [], "items": items}

    ast = parse_query(q_clean)
    if ast is None:
        return {"total": 0, "page": page, "limit": limit, "terms": [], "items": []}

    pos_terms = extract_positive_terms(ast)
    has_trigram = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pages_trigram'"
    ).fetchone() is not None

    universe_rows = conn.execute(
        f"SELECT p.page_id, p.doc_id FROM pages p JOIN documents d ON d.doc_id = p.doc_id WHERE {universe_sql}",
        params,
    ).fetchall()
    universe_pages = {r["page_id"] for r in universe_rows}
    page_to_doc = {r["page_id"]: r["doc_id"] for r in universe_rows}
    universe_docs = set(page_to_doc.values())

    def _eval_page_scope(n: ASTNode) -> set[str]:
        if isinstance(n, TermNode):
            return _match_atomic_pages(conn, n, has_trigram, universe_sql, params) & universe_pages
        if isinstance(n, NotNode):
            return universe_pages - _eval_page_scope(n.child)
        if isinstance(n, AndNode):
            left = _eval_page_scope(n.left)
            if not left:
                return set()
            return left & _eval_page_scope(n.right)
        if isinstance(n, OrNode):
            return _eval_page_scope(n.left) | _eval_page_scope(n.right)
        return set()

    def _eval_doc_scope(n: ASTNode) -> tuple[set[str], set[str]]:
        """문서 단위 논리 평가 후 (일치 doc_id 집합, 관련 page_id 집합) 반환."""
        if isinstance(n, TermNode):
            pgs = _match_atomic_pages(conn, n, has_trigram, universe_sql, params) & universe_pages
            docs = {page_to_doc[pid] for pid in pgs}
            return docs, pgs
        if isinstance(n, NotNode):
            sub_docs, _ = _eval_doc_scope(n.child)
            docs = universe_docs - sub_docs
            return docs, {pid for pid, did in page_to_doc.items() if did in docs}
        if isinstance(n, AndNode):
            ld, lp = _eval_doc_scope(n.left)
            rd, rp = _eval_doc_scope(n.right)
            docs = ld & rd
            pgs = {pid for pid in (lp | rp) if page_to_doc.get(pid) in docs}
            return docs, pgs
        if isinstance(n, OrNode):
            ld, lp = _eval_doc_scope(n.left)
            rd, rp = _eval_doc_scope(n.right)
            return ld | rd, lp | rp
        return set(), set()

    if scope == "doc":
        matched_docs, matched_pages = _eval_doc_scope(ast)
        # 긍정 키워드가 있는데 문서 전체가 들어온 경우 긍정 키워드 등장 페이지만 우선 노출
        final_ids = sorted(pid for pid in matched_pages if page_to_doc.get(pid) in matched_docs)
    else:
        final_ids = sorted(_eval_page_scope(ast))

    if not final_ids:
        return {"total": 0, "page": page, "limit": limit, "terms": pos_terms, "items": []}

    ph = ",".join("?" for _ in final_ids)
    all_rows = conn.execute(
        f"""
        SELECT p.page_id, p.doc_id, d.spec_code, d.kh_code, d.title AS doc_title,
               p.page_no, p.total_pages, p.page_type, p.section_no, p.section_title,
               p.width_px, p.height_px, p.sha256, p.has_preview, p.has_pdf, p.ocr_status,
               p.clean_text
        FROM pages p
        JOIN documents d ON d.doc_id = p.doc_id
        WHERE p.page_id IN ({ph})
        """,
        final_ids,
    ).fetchall()

    scored = sorted(all_rows, key=lambda r: (_score_page(r, pos_terms)[0], r["spec_code"], int(r["page_no"])))
    total = len(scored)
    page_rows = scored[offset:offset + limit]

    items = []
    for r in page_rows:
        d = dict(r)
        d["v"] = d["sha256"][:8]
        d["snippet"] = _build_multi_snippet(d.pop("clean_text", ""), pos_terms, d.get("section_title", ""))
        items.append(d)

    return {"total": total, "page": page, "limit": limit, "terms": pos_terms, "items": items}


def match_token_boxes(tokens: list[list[Any]], positive_terms: list[str]) -> list[dict[str, Any]]:
    """페이지 내 단어/줄 좌표 목록에서 다중 키워드와 일치하는 BBox들을 색상 인덱스(term_idx)와 함께 반환."""
    if not tokens or not positive_terms:
        return []
    hits: list[dict[str, Any]] = []
    seen_boxes: set[tuple[int, float, float]] = set()
    clean_terms = [(idx, t.strip(), t.strip().lower()) for idx, t in enumerate(positive_terms) if t.strip()]

    for tok in tokens:
        if len(tok) < 5:
            continue
        text = str(tok[0])
        tl = text.lower()
        x0, y0, x1, y1 = float(tok[1]), float(tok[2]), float(tok[3]), float(tok[4])
        for tidx, orig_term, term_low in clean_terms:
            # 단일 단어 일치 또는 구문의 구성 단어가 토큰에 포함된 경우
            parts = [p for p in term_low.split() if len(p) >= 2] or [term_low]
            if term_low in tl or (len(parts) > 1 and any(p == tl or p in tl for p in parts)):
                key = (tidx, round(x0, 3), round(y0, 3))
                if key in seen_boxes:
                    continue
                seen_boxes.add(key)
                hits.append({
                    "term_idx": tidx % 4,
                    "term": orig_term,
                    "text": text,
                    "x0": round(x0, 5),
                    "y0": round(y0, 5),
                    "x1": round(x1, 5),
                    "y1": round(y1, 5),
                })
                break
    return hits
