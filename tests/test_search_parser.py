"""TDD 테스트: 다중 키워드 조합 검색 AST 파서 · SQLite FTS5/Trigram 하이브리드 검색 · 이중 DB."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest


def _seed_sample_specs(db_path: Path) -> None:
    from backend.db import init_specs_db, specs_write_conn

    init_specs_db(db_path)
    with specs_write_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO documents(doc_id, kh_code, spec_code, title, rev_date, source_file, sha256, total_pages, heading_count) "
            "VALUES ('I24000', 'KH_01079679', 'I24000', '제어(설비) INSTRUMENT 계기 설치 시방서', '202506', 'I24000.pdf', 'sha_i24', 3, 2)"
        )
        conn.execute(
            "INSERT INTO documents(doc_id, kh_code, spec_code, title, rev_date, source_file, sha256, total_pages, heading_count) "
            "VALUES ('I21000', 'KH_01079676', 'I21000', '제어(설비) 케이블 덕트 설치 시방서', '202409', 'I21000.pdf', 'sha_i21', 2, 1)"
        )
        pages = [
            (
                "I24000_p020", "I24000", 20, 3, "body", "3.1.1.4", "차압 센서 설치",
                "3.1.1.4. 차압 센서 설치\nPROBE, SENSOR, 대기압 구성 3 POINT 설치.\nSAMPLE LINE 보온 및 결로 방지 처리한다. 최소 100 mm 이격.",
                [["차압", 0.1, 0.2, 0.15, 0.22], ["센서", 0.16, 0.2, 0.22, 0.22], ["보온", 0.2, 0.3, 0.25, 0.32]],
            ),
            (
                "I24000_p045", "I24000", 45, 3, "body", "3.14", "DIKE LEVEL S.W 설치 기준 (I24140)",
                "3.14. DIKE LEVEL S.W 설치 기준 (I24140)\nANCHOR BOLT 고정. 누수 감지 시 즉각 응답. 케이블 배선 확인.",
                [["DIKE", 0.1, 0.1, 0.18, 0.12], ["ANCHOR", 0.2, 0.4, 0.3, 0.42]],
            ),
            (
                "I24000_p233", "I24000", 233, 3, "history", "5.1.5", "제/개정 세부이력",
                "개정 전 개정 후 배기 덕트 정압계(N-Logic) 차압 센서 설치 기준 반영",
                [["차압", 0.4, 0.5, 0.45, 0.52]],
            ),
            (
                "I21000_p008", "I21000", 8, 2, "body", "3.2", "케이블 덕트 및 트레이 지지대 설치",
                "3.2. 케이블 덕트 및 트레이 지지대 설치\n케이블 트레이 간격 1500mm 이하, 전력 케이블과 제어 케이블 분리 포설.",
                [["케이블", 0.1, 0.2, 0.18, 0.22], ["트레이", 0.2, 0.2, 0.28, 0.22]],
            ),
            (
                "I21000_p012", "I21000", 12, 2, "body", "3.5", "제어 케이블 접지 기준",
                "3.5. 제어 케이블 접지 기준\n제어 신호 케이블 실드 한쪽 접지 원칙.",
                [["제어", 0.1, 0.2, 0.15, 0.22], ["케이블", 0.16, 0.2, 0.24, 0.22]],
            ),
        ]
        for pid, did, pno, tp, ptype, sno, stitle, ctext, toks in pages:
            conn.execute(
                "INSERT INTO pages(page_id, doc_id, page_no, total_pages, page_type, section_no, section_title, "
                "width_px, height_px, sha256, has_preview, has_pdf, ocr_status, clean_text, tokens_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1600, 2264, ?, 1, 1, '', ?, ?)",
                (pid, did, pno, tp, ptype, sno, stitle, f"sha_{did}", ctext, json.dumps(toks, ensure_ascii=False)),
            )
            doc_title = "제어(설비) INSTRUMENT 계기 설치 시방서" if did == "I24000" else "제어(설비) 케이블 덕트 설치 시방서"
            conn.execute(
                "INSERT INTO pages_fts(page_id, doc_id, spec_code, doc_title, section_no, section_title, clean_text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (pid, did, did, doc_title, sno, stitle, ctext),
            )
            blob = f"{did} {doc_title} {sno} {stitle} {ctext}"
            conn.execute("INSERT INTO pages_trigram(page_id, doc_id, search_blob) VALUES (?, ?, ?)", (pid, did, blob))


def test_parse_boolean_query_ast_and_positive_terms():
    from backend.search_parser import extract_positive_terms, parse_query

    ast = parse_query('(차압 | 정압) 보온 -배기 "최소 100 mm"')
    terms = extract_positive_terms(ast)
    assert "차압" in terms
    assert "정압" in terms
    assert "보온" in terms
    assert "최소 100 mm" in terms
    assert "배기" not in terms


def test_and_or_not_phrase_combination_search(tmp_path):
    from backend.search_parser import search_pages

    db = tmp_path / "specs.db"
    _seed_sample_specs(db)
    conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # 1) AND 조합 (공백 + 짧은 2글자 한국어 키워드 포함)
        r_and = search_pages(conn, "차압 보온")
        assert [x["page_id"] for x in r_and["items"]] == ["I24000_p020"]
        assert "[[H0]]차압[[/H0]]" in r_and["items"][0]["snippet"]
        assert "[[H1]]보온[[/H1]]" in r_and["items"][0]["snippet"]

        # 2) NOT 제외 검색
        r_not = search_pages(conn, "케이블 -트레이")
        pids_not = [x["page_id"] for x in r_not["items"]]
        assert "I21000_p012" in pids_not
        assert "I24000_p045" in pids_not
        assert "I21000_p008" not in pids_not

        # 3) OR + 괄호 + NOT 조합
        r_complex = search_pages(conn, "(차압 | ANCHOR) -배기")
        pids_c = {x["page_id"] for x in r_complex["items"]}
        assert pids_c == {"I24000_p020", "I24000_p045"}

        # 4) 정확 구문 검색
        r_phrase = search_pages(conn, '"최소 100 mm"')
        assert [x["page_id"] for x in r_phrase["items"]] == ["I24000_p020"]

        # 5) 본문만 필터 (history 제외)
        r_body = search_pages(conn, "차압", body_only=True)
        assert [x["page_id"] for x in r_body["items"]] == ["I24000_p020"]

        # 6) 동일 문서 내 조합 검색 (scope="doc"): p.20(보온)과 p.45(ANCHOR)는 I24000 내 공존
        r_doc = search_pages(conn, "보온 ANCHOR", scope="doc")
        doc_pids = {x["page_id"] for x in r_doc["items"]}
        assert doc_pids == {"I24000_p020", "I24000_p045"}

        # 7) 1페이지 내 조합 검색 (scope="page"): 한 장표에 둘 다 없으므로 0건
        r_page = search_pages(conn, "보온 ANCHOR", scope="page")
        assert r_page["total"] == 0
    finally:
        conn.close()


def test_find_highlight_boxes_for_multiple_keywords():
    from backend.search_parser import match_token_boxes

    tokens = [
        ["차압", 0.10, 0.20, 0.15, 0.22],
        ["센서", 0.16, 0.20, 0.22, 0.22],
        ["보온", 0.20, 0.30, 0.25, 0.32],
    ]
    hits = match_token_boxes(tokens, ["차압", "보온"])
    assert len(hits) == 2
    assert hits[0]["term_idx"] == 0 and hits[0]["term"] == "차압"
    assert hits[1]["term_idx"] == 1 and hits[1]["term"] == "보온"
