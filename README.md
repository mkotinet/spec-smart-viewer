# 시방서 스마트 뷰어 (`spec-smart-viewer`)

NRD-K2 제어(설비) 시방서 PDF를 **Synology DS920+ 서버 단독(Mac 미사용)** 으로 Google Drive에서 주기적으로 동기화·인덱싱·선택적 OCR 처리하고, 모바일/데스크톱에서 **전체 장표 브라우징 및 다중 키워드 조합 검색**을 지원하는 초경량 PWA 웹 뷰어입니다.

## 주요 기능

- **다중 키워드 조합 검색 (`backend/search_parser.py`)**
  - **필수(AND)**: 공백 또는 `AND` (`차압 보온`)
  - **선택(OR) / 괄호 그룹**: `|` 또는 `OR`, `(...)` (`(엘보 | Elbow) Fitting`)
  - **제외(NOT)**: `-단어` 또는 `!단어` (`케이블 -트레이`)
  - **정확 구문**: `"최소 100 mm"`
  - **범위 전환**: 동일 장표 내 조합(`scope=page`) / 동일 시방서 권 내 다른 페이지 교차 조합(`scope=doc`)
  - **시각적 키워드 칩 빌더**: 검색창 하단 `+ 필수(AND)`, `+ 선택(OR)`, `+ 제외(-)`, `+ 구문(" ")` 및 원클릭 칩 제거 지원
- **전체 장표 그리드 & 정밀 장표 뷰어 (`frontend/`)**
  - **전체 장표 그리드**: `480px` WebP 썸네일 카드, 스니펫 다색 하이라이트(`<mark class="kw-0..3">`), 문서/장표 성격 필터(`body`, `diagram`, `toc`, `history`, `appendix`, `cover`), 즐겨찾기 필터
  - **정밀 장표 뷰어**: `1600px` 고해상도 WebP 60fps CSS `translate3d`/`scale` 줌·팬, PDFium 단어 좌표 기반 다색 바운딩 박스 오버레이 및 `[매칭 N/M]` 점프, 문서 목차(TOC) 트리 이동, 추출 원문 텍스트 뷰어·복사, 장표 핀 메모, 1페이지 분할 PDF 열람
- **100% DS920+ 서버 단독 동기화 및 선택적 OCR (`worker/sync_worker.py`, `scripts/build_index.py`)**
  - `spec-smart-viewer` (`256m`) + `spec-sync-worker` (`768m`, `cpu_shares: 256`, `oom_score_adj: 700`) 2-컨테이너 분리 구성
  - SHA-256 증분 인덱싱, `DELETE` 저널 + `?mode=ro` SQLite 튜닝
  - 이미지 비중이 높고 디지털 텍스트가 부족한 페이지만 선별해 서브프로세스 RapidOCR(`PP-OCRv4 Det + PP-OCRv5 Korean Rec`) 수행 후 OS 메모리 100% 반환
- **PWA·보안 표준 (`AGENTS.md` 준수)**
  - Private GitHub(`mkotinet/spec-smart-viewer`), PBKDF2-SHA256 비밀번호 해시, HttpOnly 세션 쿠키, 관리자/일반 사용자 RBAC, KST(`Asia/Seoul`) 감사 로그, Rate Limit, Zip-Slip 경로 검증
  - 무캐시 `/manifest.json`, `/sw.js`, `/api/version` + `#modalWhatsNew` 자동 릴리즈 노트 및 OS별 PWA 설치 가이드

## 디렉토리 구조

```text
spec-smart-viewer/
├── backend/
│   ├── config.py          # 환경변수 및 KST(Asia/Seoul) 시각 도우미
│   ├── db.py              # specs.db(FTS5/Trigram) + app_state.db(인증/핀/감사)
│   ├── main.py            # FastAPI API, RBAC, 미디어 서빙, Zip-Slip 방어 번들 수신
│   └── search_parser.py   # 다중 키워드 Boolean/구문 AST 파서 & BBox 매칭
├── scripts/
│   ├── ocr_engine.py      # RapidOCR(PP-OCRv4 Det + v5 Korean Rec) 서브프로세스
│   └── build_index.py     # PDFium 텍스트/좌표/목차/WebP/1p-PDF 증분 인덱서
├── worker/
│   └── sync_worker.py     # rclone GDrive 동기화 + build_index + n8n 웹훅 제어 서버(:8099)
├── frontend/
│   ├── index.html
│   └── static/
│       ├── app.js         # 그리드/정밀뷰어, 칩빌더, 줌·팬, BFCache/PWA 생명주기
│       ├── style.css
│       ├── manifest.json
│       ├── sw.js
│       └── icons/
├── tests/
│   ├── conftest.py
│   ├── test_app.py
│   ├── test_indexer.py
│   └── test_search_parser.py
├── Dockerfile
├── Dockerfile.worker
├── docker-compose.yml
├── requirements.txt
├── requirements-worker.txt
├── version.json
├── 기술레포트.md
├── 제작히스토리.md
└── 트러블슈팅.md
```

## 로컬 검증 명령

```bash
uv run --python 3.12 --no-project \
  --with fastapi==0.128.8 --with python-multipart==0.0.20 --with httpx \
  --with pytest --with pillow --with pypdfium2==5.15.2 --with pypdf==6.21.4 \
  pytest tests/ -v

node --check frontend/static/app.js && node --check frontend/static/sw.js
```
