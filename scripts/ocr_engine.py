#!/usr/bin/env python3
"""시방서 이미지 전용 장표 선택적 RapidOCR 엔진 (격리 서브프로세스 실행용).

- 디지털 본문(98.8%)은 OCR을 건너뛰고, 본문 텍스트 < 40자 & 이미지 면적 >= 15%인 장표만 처리한다.
- DS920+ RAM 보호를 위해 단발 서브프로세스 CLI(--pdf --pages --out-json)로 실행 후 즉시 종료해
  ONNX Runtime 메모리를 OS에 100% 반환한다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from PIL import Image

OCR_ENGINE_VERSION = 1
_ENGINE = None

_BOILERPLATE_HINTS = (
    "삼성전자",
    "Semiconductor business",
    "related laws",
    "금지 됩니다",
    "무단으로 본 문서",
    "Any unauthorized",
)


def needs_spec_ocr(clean_text: str, img_ratio: float) -> bool:
    """보일러플레이트 제거 후 본문 텍스트가 부족하고 실제 이미지가 면적 15% 이상인 장표만 OCR 선별."""
    return len((clean_text or "").strip()) < 40 and float(img_ratio) >= 0.15


def page_image_ratio(page) -> float:
    """PDFium 페이지 내 래스터 이미지 객체 면적 합 / 페이지 전체 면적 비율 계산."""
    try:
        pw, ph = page.get_size()
        page_area = max(float(pw) * float(ph), 1.0)
        img_area = 0.0
        for obj in page.get_objects():
            if getattr(obj, "type", 0) == 3:  # FPDF_PAGEOBJ_IMAGE
                try:
                    left, bottom, right, top = obj.get_bounds()
                    img_area += max(0.0, (right - left) * (top - bottom))
                except Exception:
                    pass
        return min(1.0, img_area / page_area)
    except Exception:
        return 0.0


def is_ocr_available() -> bool:
    try:
        import rapidocr  # noqa: F401
        return True
    except Exception:
        return False


def get_ocr_engine():
    """프로세스당 1회 RapidOCR 한국어+영문 PP-OCRv5 모바일 ONNX 엔진 초기화."""
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR

    threads = max(1, min(int(os.environ.get("OCR_THREADS", "1")), 4))
    _ENGINE = RapidOCR(params={
        "Rec.lang_type": LangRec.KOREAN,
        "Rec.ocr_version": OCRVersion.PPOCRV5,
        "Rec.model_type": ModelType.MOBILE,
        "Det.ocr_version": OCRVersion.PPOCRV4,
        "Det.model_type": ModelType.MOBILE,
        "Det.limit_side_len": 1280,
        "EngineConfig.onnxruntime.intra_op_num_threads": threads,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
    })
    return _ENGINE


def warmup_models() -> None:
    """Docker 이미지 빌드 시 ONNX 모델을 사전 다운로드하여 런타임 오프라인 실행 보장."""
    eng = get_ocr_engine()
    blank = Image.new("RGB", (64, 64), (255, 255, 255))
    eng(blank)


def _is_boilerplate_ocr_line(txt: str) -> bool:
    s = (txt or "").strip()
    if not s or s == "대외비":
        return True
    if re.fullmatch(r"\d+\s*/\s*\d+", s):
        return True
    if any(h in s for h in _BOILERPLATE_HINTS):
        return True
    if re.search(r"KH_\d{6,}", s) or "Specification" in s:
        return True
    return False


def ocr_pil_image(pil_img: Image.Image) -> tuple[str, list[list[Any]]]:
    """단일 장표 이미지를 OCR해 정제 텍스트와 단어/구문 좌표([[text, x0, y0, x1, y1], ...]) 반환."""
    import numpy as np

    w, h = pil_img.size
    if w <= 0 or h <= 0:
        return "", []
    eng = get_ocr_engine()
    out = eng(np.array(pil_img.convert("RGB")))
    if out is None or not getattr(out, "txts", None):
        return "", []

    lines: list[str] = []
    tokens: list[list[Any]] = []
    for box, txt, sc in zip(out.boxes, out.txts, out.scores):
        clean = " ".join((txt or "").split())
        if not clean or float(sc) < 0.45:
            continue
        if _is_boilerplate_ocr_line(clean):
            continue
        bx0 = max(0.0, min(1.0, float(min(p[0] for p in box)) / w))
        by0 = max(0.0, min(1.0, float(min(p[1] for p in box)) / h))
        bx1 = max(0.0, min(1.0, float(max(p[0] for p in box)) / w))
        by1 = max(0.0, min(1.0, float(max(p[1] for p in box)) / h))
        lines.append(clean)
        # 줄 전체와 공백 분리 단어를 모두 토큰으로 보관(단일/구문 하이라이트 모두 대응)
        tokens.append([clean, round(bx0, 4), round(by0, 4), round(bx1, 4), round(by1, 4)])
        words = clean.split()
        if len(words) > 1:
            total_len = max(sum(len(wd) for wd in words), 1)
            cur = bx0
            span = bx1 - bx0
            for wd in words:
                frac = len(wd) / total_len
                wx1 = min(bx1, cur + span * frac)
                if len(wd) >= 2:
                    tokens.append([wd, round(cur, 4), round(by0, 4), round(wx1, 4), round(by1, 4)])
                cur = wx1

    return "\n".join(lines), tokens


def run_ocr_for_pdf_pages(
    pdf_path: Path,
    page_indices: list[int],
    max_w: int = 1600,
) -> dict[int, dict[str, Any]]:
    import pypdfium2 as pdfium

    out: dict[int, dict[str, Any]] = {}
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        for idx in page_indices:
            if idx < 0 or idx >= len(doc):
                continue
            page = doc[idx]
            try:
                pw, ph = page.get_size()
                if pw <= 0 or ph <= 0:
                    continue
                scale = max(1.0, min(max_w / max(pw, ph), 2.2))
                bmp = page.render(scale=scale, rotation=0)
                pil = bmp.to_pil().convert("RGB")
                bmp.close()
                try:
                    text, tokens = ocr_pil_image(pil)
                    out[idx] = {"text": text, "tokens": tokens}
                finally:
                    pil.close()
            finally:
                page.close()
    finally:
        doc.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="시방서 이미지 페이지 선택적 RapidOCR CLI")
    ap.add_argument("--warmup", action="store_true", help="ONNX 모델 사전 다운로드")
    ap.add_argument("--pdf", help="대상 PDF 경로")
    ap.add_argument("--pages", default="", help="0-based 페이지 인덱스 콤마 목록")
    ap.add_argument("--max-w", type=int, default=1600)
    ap.add_argument("--out-json", help="결과 JSON 경로")
    args = ap.parse_args()

    if args.warmup:
        warmup_models()
        print("SPEC_OCR_WARMUP_OK")
        return 0
    if not args.pdf or not args.pages:
        print("오류: --pdf 와 --pages 가 필요합니다", file=sys.stderr)
        return 2
    indices = [int(x) for x in args.pages.split(",") if x.strip().isdigit()]
    res = run_ocr_for_pdf_pages(Path(args.pdf), indices, max_w=args.max_w)
    payload = json.dumps({str(k): v for k, v in res.items()}, ensure_ascii=False)
    if args.out_json:
        Path(args.out_json).write_text(payload, encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
