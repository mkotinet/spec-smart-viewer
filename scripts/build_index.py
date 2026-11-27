#!/usr/bin/env python3
"""시방서 전처리 · 조항 추출 · WebP 변환 · 증분 SQLite FTS5/Trigram 인덱서.

DS920+ 워커에서 격리 서브프로세스로 호출하거나 CLI로 단독 실행할 수 있다.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PIL import Image
import pypdfium2 as pdfium
from pypdf import PdfReader, PdfWriter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.db import init_specs_db  # noqa: E402
from scripts.ocr_engine import (  # noqa: E402
    OCR_ENGINE_VERSION,
    is_ocr_available,
    needs_spec_ocr,
    ocr_pil_image,
    page_image_ratio,
)

KST = timezone(timedelta(hours=9), "KST")
PARSER_VERSION = 1
PREVIEW_W = 1600
THUMB_W = 480

_test_extract_override = None

_RE_KH_CODE = re.compile(r"(KH_\d{6,10})", re.IGNORECASE)
_RE_SPEC_CODE = re.compile(r"\(([A-Z]\d{4,6})\)", re.IGNORECASE)
_RE_REV_DATE = re.compile(r"_(\d{6,8})$")
_RE_HEADING = re.compile(r"^(\d+(?:\.\d+){0,3})\.\s+(.+)$")
_RE_PAGE_FRAC = re.compile(r"^\d+\s*/\s*\d+$")
_RE_TOC_DOTS = re.compile(r"\.{4,}\s*\d+\s*$")


def parse_spec_filename(filename: str) -> dict[str, str]:
    """시방서 파일명에서 KH 코드, 규격 코드(I24000), 개정년월, 문서 제목 추출."""
    stem = Path(filename).stem
    kh_m = _RE_KH_CODE.search(stem)
    kh_code = kh_m.group(1).upper() if kh_m else ""

    spec_m = _RE_SPEC_CODE.search(stem)
    spec_code = spec_m.group(1).upper() if spec_m else ""

    rev_m = _RE_REV_DATE.search(stem)
    rev_date = rev_m.group(1) if rev_m else ""

    title = stem
    if kh_m:
        title = title.replace(kh_m.group(1), "")
    if rev_m:
        title = title[:rev_m.start()]
    title = title.replace("+", " ").replace("_", " ")
    # `제어(설비)전선관` -> `제어(설비) 전선관` 정리
    title = re.sub(r"제어\(설비\)(?=\S)", "제어(설비) ", title)
    if spec_code:
        title = re.sub(rf"\({re.escape(spec_code)}\)", "", title, flags=re.IGNORECASE)
    title = " ".join(title.split()).strip(" -_")
    if not title:
        title = stem
    if not spec_code:
        # fallback: 파일명에서 영숫자 코드 또는 해시 슬러그
        slug = re.sub(r"[^A-Za-z0-9]+", "_", stem).strip("_")[:16].upper()
        spec_code = slug or "SPEC"

    return {
        "kh_code": kh_code,
        "spec_code": spec_code,
        "rev_date": rev_date,
        "title": title,
    }


def strip_spec_boilerplate(raw_text: str, page_no: int = 1, total_pages: int = 1) -> str:
    """삼성전자 시방서 매 페이지 반복 머리글·대외비 문구·페이지 번호 바 제거."""
    out: list[str] = []
    for raw_ln in (raw_text or "").splitlines():
        ln = " ".join(raw_ln.split())
        if not ln:
            continue
        if ln == "대외비":
            continue
        if _RE_PAGE_FRAC.fullmatch(ln):
            continue
        if any(
            hint in ln
            for hint in (
                "이 문서는 삼성전자",
                "This document is a property",
                "related laws.",
                "금지 됩니다",
                "무단으로 본 문서의",
                "Any unauthorized dissemination",
            )
        ):
            continue
        if page_no > 1 and (
            re.search(r"시방서\s*\([A-Z]\d+\)|Specification\s+KH_\d+|proposal for the installation.*KH_\d+", ln, re.IGNORECASE)
            or (re.search(r"KH_\d{6,}", ln) and len(ln) < 120 and not ln[0].isdigit())
        ):
            continue
        out.append(ln)
    return "\n".join(out)


def classify_spec_page(clean_text: str, page_no: int, total_pages: int, img_ratio: float = 0.0) -> str:
    """장표 성격 분류: cover / toc / history / diagram / appendix / body."""
    txt = (clean_text or "").strip()
    if page_no == 1 and (len(txt) < 220 or "글로벌" in txt):
        return "cover"
    head_120 = txt[:120]
    if "- 목 차 -" in head_120 or "목 차" in head_120 or txt.count("....") >= 3:
        return "toc"
    if "개정 전" in txt and "개정 후" in txt:
        return "history"
    if "제/개정 이력" in head_120 or "제/개정 세부이력" in head_120:
        return "history"
    if len(txt) < 40 and img_ratio >= 0.15:
        return "diagram"
    if len(txt) < 60 and ("부속서" in txt or "별첨" in txt):
        return "appendix"
    return "body"


def extract_page_headings(clean_text: str) -> list[dict[str, Any]]:
    """정제된 본문 텍스트에서 장/절 번호 및 제목, 세부 코드(I24140 등) 추출."""
    headings: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw_ln in (clean_text or "").splitlines():
        ln = raw_ln.strip()
        if not ln or _RE_TOC_DOTS.search(ln) or "...." in ln:
            continue
        m = _RE_HEADING.match(ln)
        if not m:
            continue
        sec_no = m.group(1).strip()
        title = m.group(2).strip()
        if len(title) > 80 or len(title) < 2:
            continue
        # 개정 이력 표의 날짜/숫자 항목 오탐 방지 (`1. ‘표지’` 등 인용부호 시작 제외)
        if title[0] in {"'", '"', "‘", "“"}:
            continue
        code_m = _RE_SPEC_CODE.search(title)
        code_ref = code_m.group(1).upper() if code_m else ""
        key = (sec_no, title)
        if key in seen:
            continue
        seen.add(key)
        headings.append({
            "section_no": sec_no,
            "title": title,
            "code_ref": code_ref,
            "level": sec_no.count(".") + 1,
        })
    return headings


def extract_page_tokens(page, page_no: int, total_pages: int) -> tuple[str, list[list[Any]]]:
    """PDFium 페이지에서 원시 텍스트와 단어 좌표([[word, x0, y0, x1, y1], ...])를 안전하게 추출."""
    pw, ph = page.get_size()
    if pw <= 0 or ph <= 0:
        return "", []
    tp = page.get_textpage()
    try:
        raw_text = tp.get_text_bounded() or ""
        n_chars = tp.count_chars()
        if n_chars <= 0:
            return raw_text, []

        # 문자별 좌표를 단어로 묶기 (보일러플레이트 필터링과 BBox 하이라이트용)
        words: list[list[Any]] = []
        buf: list[str] = []
        bx0, by0, bx1, by1 = 1.0, 1.0, 0.0, 0.0

        def _flush() -> None:
            nonlocal buf, bx0, by0, bx1, by1
            if not buf:
                return
            w = "".join(buf).strip()
            if len(w) >= 2 and not _RE_PAGE_FRAC.fullmatch(w) and w != "대외비":
                # 상단 반복 대외비 머리글 영역(y1 < 0.145) 제외 (page >= 2)
                if not (page_no > 1 and by1 < 0.145):
                    words.append([w, round(bx0, 4), round(by0, 4), round(bx1, 4), round(by1, 4)])
            buf = []
            bx0, by0, bx1, by1 = 1.0, 1.0, 0.0, 0.0

        # 문자 수 상한 방어로 과도한 CPU 방지
        limit = min(n_chars, 25_000)
        chunk = tp.get_text_range(0, limit) or ""
        for idx, ch in enumerate(chunk):
            if ch.isspace():
                _flush()
                continue
            try:
                l, b, r, t = tp.get_charbox(idx)
            except Exception:
                continue
            nx0 = max(0.0, min(1.0, min(l, r) / pw))
            nx1 = max(0.0, min(1.0, max(l, r) / pw))
            ny0 = max(0.0, min(1.0, 1.0 - max(b, t) / ph))
            ny1 = max(0.0, min(1.0, 1.0 - min(b, t) / ph))
            if buf and (ny0 > by1 + 0.015 or ny1 < by0 - 0.015):
                _flush()
            buf.append(ch)
            bx0 = min(bx0, nx0)
            by0 = min(by0, ny0)
            bx1 = max(bx1, nx1)
            by1 = max(by1, ny1)
        _flush()
        return raw_text, words
    finally:
        tp.close()


def _run_ocr_subprocess(pdf_path: Path, page_indices: list[int], max_w: int = PREVIEW_W) -> dict[int, dict[str, Any]]:
    if not page_indices:
        return {}
    with tempfile.NamedTemporaryFile(prefix="ssv_ocr_", suffix=".json", delete=False) as tf:
        out_json = Path(tf.name)
    try:
        cmd = [
            sys.executable, "-m", "scripts.ocr_engine",
            "--pdf", str(pdf_path),
            "--pages", ",".join(str(i) for i in page_indices),
            "--max-w", str(max_w),
            "--out-json", str(out_json),
        ]
        subprocess.run(cmd, check=True, cwd=str(ROOT), timeout=max(300, len(page_indices) * 180))
        raw = json.loads(out_json.read_text(encoding="utf-8"))
        return {int(k): v for k, v in raw.items()}
    except Exception as exc:
        print(f"  [OCR 경고] 서브프로세스 실패({pdf_path.name}): {exc}", flush=True)
        return {}
    finally:
        out_json.unlink(missing_ok=True)


def _ocr_cache_file(cache_dir: Path | None, sha256: str, page_no: int) -> Path | None:
    if cache_dir is None:
        return None
    return cache_dir / f"{sha256[:16]}_p{page_no:03d}_v{OCR_ENGINE_VERSION}.json"


def _load_ocr_cache(cache_dir: Path | None, sha256: str, page_no: int) -> dict[str, Any] | None:
    p = _ocr_cache_file(cache_dir, sha256, page_no)
    if p is None or not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "text" in data and isinstance(data.get("tokens"), list):
            return data
    except Exception:
        return None
    return None


def _save_ocr_cache(cache_dir: Path | None, sha256: str, page_no: int, text: str, tokens: list[list[Any]]) -> None:
    p = _ocr_cache_file(cache_dir, sha256, page_no)
    if p is None:
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"text": text, "tokens": tokens}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def process_single_pdf(job: dict[str, Any]) -> dict[str, Any]:
    """단일 시방서 PDF를 분석해 문서/장표/조항 메타데이터와 1600px/480px WebP, 1페이지 PDF 생성."""
    pdf_path = Path(job["pdf_path"])
    doc_id = job["doc_id"]
    rel_str = job["rel_str"]
    sha256 = job["sha256"]
    fmeta = job["file_meta"]
    out_dir = Path(job["out_dir"])
    ocr_cache_dir = Path(job["ocr_cache_dir"]) if job.get("ocr_cache_dir") else None
    enable_ocr = bool(job.get("enable_ocr", False))
    ocr_subprocess = bool(job.get("ocr_subprocess", True))

    doc = pdfium.PdfDocument(str(pdf_path))
    total_pages = len(doc)
    reader = PdfReader(str(pdf_path)) if total_pages >= 1 else None

    # 1차 패스: 디지털 텍스트·단어좌표·이미지 비율 수집 및 OCR 대상 선별
    raw_pages: list[dict[str, Any]] = []
    ocr_indices: list[int] = []
    cached_ocr: dict[int, dict[str, Any]] = {}

    for idx in range(total_pages):
        page_no = idx + 1
        page = doc[idx]
        pw, ph = page.get_size()
        if _test_extract_override is not None:
            raw_txt, tokens = _test_extract_override(pdf_path.name, page_no)
            img_ratio = 0.0
        else:
            raw_txt, tokens = extract_page_tokens(page, page_no, total_pages)
            img_ratio = page_image_ratio(page)
        page.close()

        cleaned = strip_spec_boilerplate(raw_txt, page_no=page_no, total_pages=total_pages)
        if needs_spec_ocr(cleaned, img_ratio):
            ocr_indices.append(idx)
            hit = _load_ocr_cache(ocr_cache_dir, sha256, page_no)
            if hit is not None:
                cached_ocr[idx] = hit

        raw_pages.append({
            "idx": idx,
            "page_no": page_no,
            "pw": pw,
            "ph": ph,
            "clean_text": cleaned,
            "tokens": tokens,
            "img_ratio": img_ratio,
        })

    uncached_ocr = [idx for idx in ocr_indices if idx not in cached_ocr]
    ocr_ready = enable_ocr and bool(ocr_indices) and (bool(cached_ocr) or is_ocr_available())
    subproc_ocr: dict[int, dict[str, Any]] = {}
    if enable_ocr and uncached_ocr and ocr_subprocess and is_ocr_available():
        subproc_ocr = _run_ocr_subprocess(pdf_path, uncached_ocr, max_w=PREVIEW_W)
        for idx, res in subproc_ocr.items():
            _save_ocr_cache(ocr_cache_dir, sha256, idx + 1, res.get("text", ""), res.get("tokens", []))

    thumb_dir = out_dir / "thumbs"
    prev_dir = out_dir / "previews"
    pdf_out_dir = out_dir / "pdfs"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    prev_dir.mkdir(parents=True, exist_ok=True)
    pdf_out_dir.mkdir(parents=True, exist_ok=True)

    pages_out: list[dict[str, Any]] = []
    sections_out: list[dict[str, Any]] = []
    active_sec_no = ""
    active_sec_title = ""
    ocr_done_pages = 0

    try:
        for rp in raw_pages:
            idx = rp["idx"]
            page_no = rp["page_no"]
            pw, ph = rp["pw"], rp["ph"]
            if pw <= 0 or ph <= 0:
                continue

            page_id = f"{doc_id}_p{page_no:03d}"
            clean_text = rp["clean_text"]
            tokens = list(rp["tokens"])
            ocr_status = ""

            # WebP 렌더링 (1600px 프리뷰 + 480px 썸네일)
            page = doc[idx]
            scale = max(0.8, min(PREVIEW_W / max(pw, ph), 2.5))
            bmp = page.render(scale=scale, rotation=0)
            pil_img = bmp.to_pil().convert("RGB")
            bmp.close()
            page.close()

            try:
                if idx in ocr_indices:
                    if idx in cached_ocr:
                        ocr_res = cached_ocr[idx]
                        ocr_txt = ocr_res.get("text", "")
                        ocr_toks = ocr_res.get("tokens", [])
                        if ocr_txt:
                            clean_text = (clean_text + "\n" + ocr_txt).strip()
                        tokens.extend(ocr_toks)
                        ocr_status = "ocr_done"
                        ocr_done_pages += 1
                    elif ocr_ready:
                        if ocr_subprocess:
                            ocr_res = subproc_ocr.get(idx)
                            if ocr_res:
                                ocr_txt = ocr_res.get("text", "")
                                ocr_toks = ocr_res.get("tokens", [])
                                if ocr_txt:
                                    clean_text = (clean_text + "\n" + ocr_txt).strip()
                                tokens.extend(ocr_toks)
                                ocr_status = "ocr_done"
                                ocr_done_pages += 1
                            else:
                                ocr_status = "ocr_pending"
                        else:
                            ocr_txt, ocr_toks = ocr_pil_image(pil_img)
                            _save_ocr_cache(ocr_cache_dir, sha256, page_no, ocr_txt, ocr_toks)
                            if ocr_txt:
                                clean_text = (clean_text + "\n" + ocr_txt).strip()
                            tokens.extend(ocr_toks)
                            ocr_status = "ocr_done"
                            ocr_done_pages += 1
                    else:
                        ocr_status = "ocr_pending"

                w_px, h_px = pil_img.size
                prev_img = pil_img.copy()
                prev_img.thumbnail((PREVIEW_W, PREVIEW_W), Image.Resampling.BILINEAR)
                prev_img.save(prev_dir / f"{page_id}.webp", "WEBP", quality=82, method=2)
                w_px, h_px = prev_img.size
                prev_img.close()

                thumb_img = pil_img.copy()
                thumb_img.thumbnail((THUMB_W, THUMB_W), Image.Resampling.BILINEAR)
                thumb_img.save(thumb_dir / f"{page_id}.webp", "WEBP", quality=76, method=2)
                thumb_img.close()
            finally:
                pil_img.close()

            # 단일 페이지 PDF 저장
            has_pdf = 0
            try:
                if reader is not None and idx < len(reader.pages):
                    writer = PdfWriter()
                    writer.add_page(reader.pages[idx])
                    with open(pdf_out_dir / f"{page_id}.pdf", "wb") as fh:
                        writer.write(fh)
                    has_pdf = 1
            except Exception:
                has_pdf = 0

            ptype = classify_spec_page(clean_text, page_no, total_pages, rp["img_ratio"])
            headings = extract_page_headings(clean_text) if ptype not in {"cover", "toc", "history"} else []
            if headings:
                # 해당 페이지에서 처음 등장한 최상위(가장 짧은 번호 또는 첫 번째) 조항을 대표 섹션으로 연결
                active_sec_no = headings[0]["section_no"]
                active_sec_title = headings[0]["title"]
                for h in headings:
                    sections_out.append({
                        "doc_id": doc_id,
                        "section_no": h["section_no"],
                        "title": h["title"],
                        "code_ref": h["code_ref"],
                        "level": h["level"],
                        "start_page": page_no,
                        "end_page": page_no,
                        "page_id": page_id,
                    })

            sec_no = active_sec_no
            sec_title = active_sec_title
            if ptype == "cover":
                sec_no, sec_title = "", "표지"
            elif ptype == "toc":
                sec_no, sec_title = "", "목차"
            elif ptype == "history":
                sec_no, sec_title = "5", "제/개정 이력"

            pages_out.append({
                "page_id": page_id,
                "doc_id": doc_id,
                "page_no": page_no,
                "total_pages": total_pages,
                "page_type": ptype,
                "section_no": sec_no,
                "section_title": sec_title,
                "width_px": w_px,
                "height_px": h_px,
                "sha256": sha256,
                "has_preview": 1,
                "has_pdf": has_pdf,
                "ocr_status": ocr_status,
                "clean_text": clean_text[:24_000],
                "tokens_json": json.dumps(tokens[:1200], ensure_ascii=False, separators=(",", ":")),
            })
    finally:
        doc.close()

    # 섹션 end_page 채우기 (동일 레벨 또는 상위 레벨 섹션 시작 전 페이지까지)
    for i, sec in enumerate(sections_out):
        end_p = total_pages
        for nxt in sections_out[i + 1:]:
            if nxt["level"] <= sec["level"] and nxt["start_page"] > sec["start_page"]:
                end_p = max(sec["start_page"], nxt["start_page"] - 1)
                break
        sec["end_page"] = end_p

    doc_row = {
        "doc_id": doc_id,
        "kh_code": fmeta["kh_code"],
        "spec_code": fmeta["spec_code"],
        "title": fmeta["title"],
        "rev_date": fmeta["rev_date"],
        "source_file": rel_str,
        "sha256": sha256,
        "total_pages": len(pages_out),
        "heading_count": len(sections_out),
        "ocr_pages": ocr_done_pages,
    }
    return {"doc": doc_row, "pages": pages_out, "sections": sections_out}


def _load_existing_by_sha(db_path: Path, out_dir: Path) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    if not db_path.is_file():
        return {}, {}
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta").fetchall()}
        docs = [dict(r) for r in conn.execute("SELECT * FROM documents").fetchall()]
        pages_by_doc: dict[str, list[dict[str, Any]]] = {}
        for pr in conn.execute("SELECT * FROM pages ORDER BY page_no").fetchall():
            d = dict(pr)
            pages_by_doc.setdefault(d["doc_id"], []).append(d)
        secs_by_doc: dict[str, list[dict[str, Any]]] = {}
        for sr in conn.execute(
            "SELECT doc_id, section_no, title, code_ref, level, start_page, end_page, page_id "
            "FROM sections ORDER BY start_page, id"
        ).fetchall():
            d = dict(sr)
            secs_by_doc.setdefault(d["doc_id"], []).append(d)
    except sqlite3.Error:
        return {}, {}
    finally:
        conn.close()

    valid: dict[str, dict[str, Any]] = {}
    for doc in docs:
        did = doc["doc_id"]
        plist = pages_by_doc.get(did, [])
        if not plist:
            continue
        ok = all(
            (out_dir / "thumbs" / f"{p['page_id']}.webp").is_file()
            and (out_dir / "previews" / f"{p['page_id']}.webp").is_file()
            for p in plist
        )
        if ok:
            valid[doc["sha256"]] = {
                "doc": doc,
                "pages": plist,
                "sections": secs_by_doc.get(did, []),
            }
    return valid, meta


def _write_specs_db_atomic(
    out_dir: Path,
    bundles: list[dict[str, Any]],
    meta: dict[str, str],
) -> None:
    staging = out_dir / ".specs.db.staging"
    for suffix in ("", "-wal", "-shm"):
        (out_dir / f".specs.db.staging{suffix}").unlink(missing_ok=True)
    init_specs_db(staging)

    conn = sqlite3.connect(staging, timeout=30)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=OFF")
    has_trigram = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pages_trigram'"
    ).fetchone() is not None

    total_pages = 0
    total_headings = 0
    ocr_pages = 0

    for b in sorted(bundles, key=lambda x: (x["doc"]["spec_code"], x["doc"]["title"])):
        doc = b["doc"]
        pages = b["pages"]
        sections = b["sections"]
        total_pages += len(pages)
        total_headings += len(sections)
        ocr_pages += sum(1 for p in pages if p.get("ocr_status") == "ocr_done")

        conn.execute(
            """
            INSERT INTO documents(
                doc_id, kh_code, spec_code, title, rev_date, source_file, sha256,
                total_pages, heading_count, ocr_pages
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                doc["doc_id"], doc["kh_code"], doc["spec_code"], doc["title"], doc["rev_date"],
                doc["source_file"], doc["sha256"], len(pages), len(sections),
                sum(1 for p in pages if p.get("ocr_status") == "ocr_done"),
            ),
        )
        for p in pages:
            conn.execute(
                """
                INSERT INTO pages(
                    page_id, doc_id, page_no, total_pages, page_type, section_no, section_title,
                    width_px, height_px, sha256, has_preview, has_pdf, ocr_status, clean_text, tokens_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    p["page_id"], p["doc_id"], p["page_no"], p["total_pages"], p["page_type"],
                    p["section_no"], p["section_title"], p["width_px"], p["height_px"],
                    p["sha256"], p["has_preview"], p["has_pdf"], p["ocr_status"],
                    p["clean_text"], p["tokens_json"],
                ),
            )
            conn.execute(
                "INSERT INTO pages_fts(page_id, doc_id, spec_code, doc_title, section_no, section_title, clean_text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    p["page_id"], p["doc_id"], doc["spec_code"], doc["title"],
                    p["section_no"], p["section_title"], p["clean_text"],
                ),
            )
            if has_trigram:
                blob = " ".join(
                    x for x in (doc["spec_code"], doc["title"], p["section_no"], p["section_title"], p["clean_text"]) if x
                )
                conn.execute(
                    "INSERT INTO pages_trigram(page_id, doc_id, search_blob) VALUES (?, ?, ?)",
                    (p["page_id"], p["doc_id"], blob[:24_000]),
                )

        if sections:
            conn.executemany(
                """
                INSERT INTO sections(
                    doc_id, section_no, title, code_ref, level, start_page, end_page, page_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        s["doc_id"], s["section_no"], s["title"], s["code_ref"],
                        s["level"], s["start_page"], s["end_page"], s["page_id"],
                    )
                    for s in sections
                ],
            )

    meta["total_docs"] = str(len(bundles))
    meta["total_pages"] = str(total_pages)
    meta["total_headings"] = str(total_headings)
    meta["ocr_pages"] = str(ocr_pages)
    meta["parser_version"] = str(PARSER_VERSION)
    meta["ocr_engine_version"] = str(OCR_ENGINE_VERSION)
    conn.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", list(meta.items()))
    conn.execute("INSERT INTO pages_fts(pages_fts) VALUES('optimize')")
    if has_trigram:
        conn.execute("INSERT INTO pages_trigram(pages_trigram) VALUES('optimize')")
    conn.commit()
    conn.execute("VACUUM")
    conn.close()

    for suffix in ("-wal", "-shm"):
        (out_dir / f"specs.db{suffix}").unlink(missing_ok=True)
    staging.replace(out_dir / "specs.db")


def build_index_incremental(
    input_dir: Path,
    out_dir: Path,
    project_name: str = "NRD-K2 제어(설비) 시방서",
    enable_ocr: bool = False,
    ocr_subprocess: bool = True,
    reparse_all: bool = False,
    max_delete_ratio: float = 0.30,
    min_delete_guard_pdfs: int = 5,
    ocr_cache_dir: Path | None = None,
) -> dict[str, Any]:
    """SHA-256 비교 기반 시방서 증분 인덱싱·WebP 생성·선택적 OCR (100% DS920+ 서버 구동)."""
    started = time.perf_counter()
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "specs.db"
    eff_ocr_cache = ocr_cache_dir if ocr_cache_dir is not None else (out_dir / "ocr_cache")

    pdfs = sorted(p for p in input_dir.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf")
    by_hash: dict[str, dict[str, Any]] = {}
    used_doc_ids: set[str] = set()

    for p in pdfs:
        rel = p.relative_to(input_dir)
        digest = sha256_file(p)
        if digest in by_hash:
            continue
        fmeta = parse_spec_filename(p.name)
        cand_id = fmeta["spec_code"]
        if cand_id in used_doc_ids:
            cand_id = f"{cand_id}_{digest[:6]}"
        used_doc_ids.add(cand_id)
        by_hash[digest] = {
            "pdf_path": str(p),
            "rel_str": str(rel),
            "doc_id": cand_id,
            "file_meta": fmeta,
            "sha256": digest,
            "out_dir": str(out_dir),
            "ocr_cache_dir": str(eff_ocr_cache),
            "enable_ocr": enable_ocr,
            "ocr_subprocess": ocr_subprocess,
        }

    existing_by_sha, existing_meta = _load_existing_by_sha(db_path, out_dir)
    prev_parser_ver = int(existing_meta.get("parser_version", "0") or 0)
    ocr_ready = enable_ocr and is_ocr_available()

    removed_shas = [sha for sha in existing_by_sha if sha not in by_hash]
    if (
        max_delete_ratio > 0
        and len(existing_by_sha) >= min_delete_guard_pdfs
        and (len(removed_shas) / len(existing_by_sha)) > max_delete_ratio
    ):
        pct = (len(removed_shas) / len(existing_by_sha)) * 100.0
        raise RuntimeError(
            f"대량 삭제 안전장치 작동: 기존 {len(existing_by_sha)}개 시방서 중 {len(removed_shas)}개({pct:.1f}%) 삭제 감지"
        )

    reused_bundles: list[dict[str, Any]] = []
    new_jobs: list[dict[str, Any]] = []
    added_files: list[str] = []
    reparsed_count = 0

    for sha, entry in by_hash.items():
        if sha in existing_by_sha:
            bundle = existing_by_sha[sha]
            has_pending_ocr = ocr_ready and any(p.get("ocr_status") == "ocr_pending" for p in bundle["pages"])
            need_reparse = reparse_all or (prev_parser_ver < PARSER_VERSION) or has_pending_ocr or (
                bundle["doc"]["doc_id"] != entry["doc_id"]
            )
            if need_reparse:
                new_jobs.append(entry)
                reparsed_count += 1
            else:
                reused_bundles.append(bundle)
        else:
            new_jobs.append(entry)
            added_files.append(entry["rel_str"])

    if not new_jobs and not removed_shas and db_path.is_file():
        elapsed = round(time.perf_counter() - started, 2)
        total_pages = sum(len(b["pages"]) for b in reused_bundles)
        total_headings = sum(len(b["sections"]) for b in reused_bundles)
        return {
            "status": "no_changes",
            "at_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
            "source_pdfs": len(pdfs),
            "unique_pdfs": len(by_hash),
            "added_pdfs": 0,
            "removed_pdfs": 0,
            "reparsed_pdfs": 0,
            "added_pages": 0,
            "removed_pages": 0,
            "failed_count": 0,
            "total_docs": len(reused_bundles),
            "total_pages": total_pages,
            "total_headings": total_headings,
            "elapsed_sec": elapsed,
            "added_files": [],
            "removed_files": [],
        }

    new_bundles: list[dict[str, Any]] = []
    failed: list[str] = []
    for job in new_jobs:
        try:
            new_bundles.append(process_single_pdf(job))
        except Exception as exc:
            failed.append(f"{job['rel_str']}: {exc}")
            print(f"  [경고] 시방서 인덱싱 건너뜀: {job['rel_str']} — {exc}", flush=True)
        gc.collect()

    final_bundles = reused_bundles + new_bundles
    meta = {
        "project": project_name,
        "built_at_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
        "source_pdfs": str(len(pdfs)),
        "unique_pdfs": str(len(by_hash)),
        "failed_count": str(len(failed)),
    }
    _write_specs_db_atomic(out_dir, final_bundles, meta)

    # 고아 WebP 및 1페이지 PDF 정리
    active_page_ids = {p["page_id"] for b in final_bundles for p in b["pages"]}
    removed_pages = 0
    removed_files = []
    for sha in removed_shas:
        b = existing_by_sha[sha]
        removed_files.append(b["doc"]["source_file"])
        for p in b["pages"]:
            pid = p["page_id"]
            if pid not in active_page_ids:
                removed_pages += 1
                (out_dir / "thumbs" / f"{pid}.webp").unlink(missing_ok=True)
                (out_dir / "previews" / f"{pid}.webp").unlink(missing_ok=True)
                (out_dir / "pdfs" / f"{pid}.pdf").unlink(missing_ok=True)

    elapsed = round(time.perf_counter() - started, 2)
    return {
        "status": "updated" if (added_files or removed_shas or reparsed_count > 0) else "no_changes",
        "at_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
        "source_pdfs": len(pdfs),
        "unique_pdfs": len(by_hash),
        "added_pdfs": len(added_files),
        "removed_pdfs": len(removed_shas),
        "reparsed_pdfs": reparsed_count,
        "added_pages": sum(len(b["pages"]) for b in new_bundles),
        "removed_pages": removed_pages,
        "failed_count": len(failed),
        "failed_samples": failed[:10],
        "total_docs": len(final_bundles),
        "total_pages": sum(len(b["pages"]) for b in final_bundles),
        "total_headings": sum(len(b["sections"]) for b in final_bundles),
        "ocr_pages": sum(b["doc"]["ocr_pages"] for b in final_bundles),
        "elapsed_sec": elapsed,
        "added_files": added_files[:30],
        "removed_files": removed_files[:30],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="시방서 증분 인덱서 (DS920+ 서버 단독/CLI 공용)")
    ap.add_argument("--input", default=str(ROOT / "raw_specs"))
    ap.add_argument("--out", default=str(ROOT / "data"))
    ap.add_argument("--project", default="NRD-K2 제어(설비) 시방서")
    ap.add_argument("--ocr", action="store_true")
    ap.add_argument("--reparse", action="store_true")
    ap.add_argument("--max-delete-ratio", type=float, default=0.30)
    ap.add_argument("--ocr-cache-dir", default="")
    ap.add_argument("--summary-json", default="")
    args = ap.parse_args()

    cache_dir = Path(args.ocr_cache_dir) if args.ocr_cache_dir else None
    summary = build_index_incremental(
        Path(args.input),
        Path(args.out),
        project_name=args.project,
        enable_ocr=args.ocr,
        ocr_subprocess=True,
        reparse_all=args.reparse,
        max_delete_ratio=args.max_delete_ratio,
        ocr_cache_dir=cache_dir,
    )
    payload = json.dumps(summary, ensure_ascii=False)
    if args.summary_json:
        Path(args.summary_json).write_text(payload, encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
