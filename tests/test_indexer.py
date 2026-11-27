"""TDD 테스트: 시방서 전처리(보일러플레이트 제거·조항 추출)·증분 인덱싱·선택적 OCR 게이트·고아 정리."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from PIL import Image, ImageDraw


def _make_sample_pdf(path: Path, pages_lines: list[list[str]], landscape: bool = False) -> None:
    """Pillow 없이 reportlab 없이도 pypdfium2 + Pillow 이미지 또는 간단한 PDF 생성(PDF 1.4 UTF-8/ASCII 대신 Pillow 이미지 PDF 또는 pypdf 생성)."""
    # pypdf + pillow로 실제 텍스트가 없는 이미지 페이지 / 혹은 reportlab 대신 간단 PDF 스트림이 필요하므로
    # 텍스트 페이지는 fpdf2 또는 이미지 PDF로 만들 수 있으나, 테스트 의존성 최소화를 위해
    # 순수 텍스트 추출 테스트는 unit test로 검증하고, PDF 파일 통합 테스트는 Pillow PDF로 2장 생성 + monkeypatch 텍스트 추출 조합 사용
    imgs = []
    size = (1032, 729) if landscape else (729, 1032)
    for lines in pages_lines:
        im = Image.new("RGB", size, (255, 255, 255))
        d = ImageDraw.Draw(im)
        for idx, _ in enumerate(lines):
            d.rectangle([40, 40 + idx * 30, 300, 60 + idx * 30], fill=(15, 23, 42))
        imgs.append(im)
    imgs[0].save(path, "PDF", resolution=72.0, save_all=True, append_images=imgs[1:])


def test_parse_spec_filename_and_boilerplate_stripping():
    from scripts.build_index import classify_spec_page, extract_page_headings, parse_spec_filename, strip_spec_boilerplate

    meta = parse_spec_filename("KH_01079679+제어(설비)+INSTRUMENT+계기+설치+시방서(I24000)_202506.pdf")
    assert meta["kh_code"] == "KH_01079679"
    assert meta["spec_code"] == "I24000"
    assert meta["rev_date"] == "202506"
    assert "INSTRUMENT 계기 설치 시방서" in meta["title"]

    raw = (
        "제어(설비) INSTRUMENT 계기 설치 시방서(I24000)\n"
        "Instrument Installation Specification KH_01079679\n"
        "이 문서는 삼성전자 반도체총괄의 자산이므로 무단으로 본 문서의 일부 또는 전부를 제 3 자에게 공개, 배포, 복사 또는 사용하는 것은 관련법에 따라 엄격히 금지 됩니다.\n"
        "This document is a property of Semiconductor business, Samsung Electronics Co. Ltd. Any unauthorized dissemination, distribution, copying of this document is strictly prohibited by related laws.\n"
        "20 / 246\n"
        "대외비\n"
        "3.1.1.4. 차압 센서 설치 (I24010)\n"
        "1) PROBE, SENSOR, 대기압 구성 각 3 POINT 설치\n"
        "2) SAMPLE LINE 보온 처리한다.\n"
    )
    cleaned = strip_spec_boilerplate(raw, page_no=20, total_pages=246)
    assert "삼성전자" not in cleaned
    assert "related laws" not in cleaned
    assert "차압 센서 설치" in cleaned
    assert classify_spec_page(cleaned, page_no=20, total_pages=246, img_ratio=0.05) == "body"

    headings = extract_page_headings(cleaned)
    assert len(headings) == 1
    assert headings[0]["section_no"] == "3.1.1.4"
    assert headings[0]["code_ref"] == "I24010"


def test_needs_spec_ocr_gate():
    from scripts.ocr_engine import needs_spec_ocr

    assert needs_spec_ocr("", img_ratio=0.35) is True
    assert needs_spec_ocr("2) Fitting 의 종류", img_ratio=0.40) is True
    assert needs_spec_ocr("2. 재료\n‘N/A’", img_ratio=0.0) is False
    assert needs_spec_ocr("3.14. DIKE LEVEL S.W 설치 기준 충분한 본문 텍스트가 이미 추출되었습니다.", img_ratio=0.50) is False


def test_incremental_spec_indexing_reuse_and_orphan_cleanup(tmp_path, monkeypatch):
    from scripts.build_index import build_index_incremental

    in_dir = tmp_path / "raw_specs"
    out_dir = tmp_path / "data"
    in_dir.mkdir()
    pdf_a = in_dir / "KH_01079674_제어(설비)+일반공통사항+시방서(I10000)_20221123.pdf"
    pdf_b = in_dir / "KH_01079675+제어(설비)전선관+설치+시방서(I20000)_20240913.pdf"
    _make_sample_pdf(pdf_a, [["cover"], ["body1"]])
    _make_sample_pdf(pdf_b, [["cover"], ["body2"]], landscape=True)

    # 테스트용 디지털 텍스트 주입 (PDF 폰트 임베딩 없이 전처리·DB·WebP·증분 재사용 검증)
    sample_texts = {
        (pdf_a.name, 1): ("대외비 제어(설비) 일반공통사항 시방서 (I10000)", []),
        (pdf_a.name, 2): ("1. 일반사항\n1.1. 목적\n제어설비 공통 시방 기준", [["일반사항", 0.1, 0.2, 0.2, 0.22]]),
        (pdf_b.name, 1): ("대외비 제어(설비) 전선관 설치공사 시방서 (I20000)", []),
        (pdf_b.name, 2): ("3. 시공\n3.1. 전선관 설치\n후강전선관 고정 간격 1.5m", [["후강전선관", 0.1, 0.3, 0.25, 0.32]]),
    }

    def fake_extract(pdf_name: str, page_no: int):
        return sample_texts[(pdf_name, page_no)]

    monkeypatch.setattr("scripts.build_index._test_extract_override", fake_extract, raising=False)

    s1 = build_index_incremental(in_dir, out_dir, enable_ocr=False, project_name="테스트 시방서")
    assert s1["status"] == "updated"
    assert s1["added_pdfs"] == 2
    assert s1["total_pages"] == 4
    assert (out_dir / "specs.db").is_file()
    assert (out_dir / "thumbs" / "I10000_p002.webp").is_file()
    assert (out_dir / "previews" / "I10000_p002.webp").is_file()

    # 변경 없음 재실행 -> 0건 처리
    s2 = build_index_incremental(in_dir, out_dir, enable_ocr=False, project_name="테스트 시방서")
    assert s2["status"] == "no_changes"
    assert s2["added_pdfs"] == 0

    # 1개 삭제 -> 고아 WebP 정리 확인 (min_delete_guard_pdfs=5 이므로 1/2 삭제 허용)
    pdf_b.unlink()
    s3 = build_index_incremental(in_dir, out_dir, enable_ocr=False, project_name="테스트 시방서")
    assert s3["removed_pdfs"] == 1
    assert not (out_dir / "thumbs" / "I20000_p002.webp").is_file()
    assert not (out_dir / "previews" / "I20000_p002.webp").is_file()

    conn = sqlite3.connect(out_dir / "specs.db")
    assert conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 2
    conn.close()
