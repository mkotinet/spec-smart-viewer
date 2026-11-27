"""FastAPI 백엔드 API, 인증/RBAC, PWA 헤더, 다중 키워드 검색 및 동기화 번들 테스트."""
from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import db, main as main_mod
from backend.main import app


def _seed_sample_specs(data_dir: Path) -> None:
    """테스트용 시방서 2권(3페이지)과 썸네일/프리뷰/단일PDF 자산을 생성."""
    db_path = data_dir / "specs.db"
    db.init_specs_db(db_path)

    (data_dir / "thumbs").mkdir(parents=True, exist_ok=True)
    (data_dir / "previews").mkdir(parents=True, exist_ok=True)
    (data_dir / "pdfs").mkdir(parents=True, exist_ok=True)

    sample_pages = [
        {
            "page_id": "I23000__p001",
            "doc_id": "I23000",
            "spec_code": "I23000",
            "kh_code": "KH_01079678",
            "doc_title": "현장계기 설치 시방서",
            "page_no": 1,
            "total_pages": 2,
            "page_type": "body",
            "section_no": "1.1",
            "section_title": "차압전송기 도압배관 및 보온 시공",
            "clean_text": "1.1 차압전송기 도압배관 및 보온 시공\n현장계기 도압배관 보온재 두께는 최소 100 mm 이상 유지한다.",
            "tokens": [
                ["차압전송기", 0.10, 0.12, 0.25, 0.16],
                ["보온", 0.40, 0.12, 0.48, 0.16],
                ["최소", 0.10, 0.30, 0.18, 0.34],
                ["100", 0.20, 0.30, 0.26, 0.34],
                ["mm", 0.27, 0.30, 0.33, 0.34],
            ],
            "sha256": "sha_i23_p001",
            "ocr_status": "native",
        },
        {
            "page_id": "I23000__p002",
            "doc_id": "I23000",
            "spec_code": "I23000",
            "kh_code": "KH_01079678",
            "doc_title": "현장계기 설치 시방서",
            "page_no": 2,
            "total_pages": 2,
            "page_type": "body",
            "section_no": "2.3",
            "section_title": "계장 케이블 포설 기준",
            "clean_text": "2.3 계장 케이블 포설 기준\n전원 케이블과 신호 케이블은 분리 포설하며 전선관 보호를 적용한다.",
            "tokens": [
                ["케이블", 0.12, 0.20, 0.24, 0.24],
                ["전선관", 0.30, 0.20, 0.42, 0.24],
            ],
            "sha256": "sha_i23_p002",
            "ocr_status": "native",
        },
        {
            "page_id": "I24000__p144",
            "doc_id": "I24000",
            "spec_code": "I24000",
            "kh_code": "KH_01079679",
            "doc_title": "튜브피팅 및 트레이 시방서",
            "page_no": 144,
            "total_pages": 1,
            "page_type": "diagram",
            "section_no": "4.2",
            "section_title": "Fitting 체결 및 트레이 배선",
            "clean_text": "4.2 Fitting 체결 및 트레이 배선\n45° Male Elbow Fitting Tube Pipe 연결하기 및 케이블 트레이 포설",
            "tokens": [
                ["Elbow", 0.15, 0.40, 0.28, 0.44],
                ["Fitting", 0.30, 0.40, 0.44, 0.44],
                ["케이블", 0.15, 0.60, 0.28, 0.64],
                ["트레이", 0.30, 0.60, 0.44, 0.64],
            ],
            "sha256": "sha_i24_p144",
            "ocr_status": "ocr_done",
        },
    ]

    with db.specs_write_conn(db_path) as conn:
        conn.execute("INSERT INTO meta(key, value) VALUES ('project_name', 'NRD-K2 시방서')")
        conn.execute("INSERT INTO meta(key, value) VALUES ('built_at_kst', '2026-11-27 12:00:00 KST')")

        conn.execute(
            """
            INSERT INTO documents(
                doc_id, kh_code, spec_code, title, rev_date, source_file, sha256,
                total_pages, heading_count, ocr_pages
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("I23000", "KH_01079678", "I23000", "현장계기 설치 시방서", "2026-01-10", "KH_01079678.pdf", "doc_sha_1", 2, 2, 0),
        )
        conn.execute(
            """
            INSERT INTO documents(
                doc_id, kh_code, spec_code, title, rev_date, source_file, sha256,
                total_pages, heading_count, ocr_pages
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("I24000", "KH_01079679", "I24000", "튜브피팅 및 트레이 시방서", "2026-01-12", "KH_01079679.pdf", "doc_sha_2", 1, 1, 1),
        )

        sections = [
            ("I23000", "1.1", "차압전송기 도압배관 및 보온 시공", "I23000-1.1", 1, 1, 1, "I23000__p001"),
            ("I23000", "2.3", "계장 케이블 포설 기준", "I23000-2.3", 1, 2, 2, "I23000__p002"),
            ("I24000", "4.2", "Fitting 체결 및 트레이 배선", "I24000-4.2", 1, 144, 144, "I24000__p144"),
        ]
        conn.executemany(
            """
            INSERT INTO sections(doc_id, section_no, title, code_ref, level, start_page, end_page, page_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            sections,
        )

        for p in sample_pages:
            conn.execute(
                """
                INSERT INTO pages(
                    page_id, doc_id, page_no, total_pages, page_type, section_no, section_title,
                    width_px, height_px, sha256, has_preview, has_pdf, ocr_status, clean_text, tokens_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    p["page_id"],
                    p["doc_id"],
                    p["page_no"],
                    p["total_pages"],
                    p["page_type"],
                    p["section_no"],
                    p["section_title"],
                    1600,
                    2200,
                    p["sha256"],
                    1,
                    1,
                    p["ocr_status"],
                    p["clean_text"],
                    json.dumps(p["tokens"], ensure_ascii=False),
                ),
            )
            conn.execute(
                """
                INSERT INTO pages_fts(page_id, doc_id, spec_code, doc_title, section_no, section_title, clean_text)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    p["page_id"],
                    p["doc_id"],
                    p["spec_code"],
                    p["doc_title"],
                    p["section_no"],
                    p["section_title"],
                    p["clean_text"],
                ),
            )
            blob = f"{p['spec_code']} {p['doc_title']} {p['section_no']} {p['section_title']} {p['clean_text']}"
            conn.execute(
                "INSERT INTO pages_trigram(page_id, doc_id, search_blob) VALUES (?, ?, ?)",
                (p["page_id"], p["doc_id"], blob),
            )

            (data_dir / "thumbs" / f"{p['page_id']}.webp").write_bytes(b"RIFFfakewebpthumb")
            (data_dir / "previews" / f"{p['page_id']}.webp").write_bytes(b"RIFFfakewebppreview")
            (data_dir / "pdfs" / f"{p['page_id']}.pdf").write_bytes(b"%PDF-1.4\n%fake\n")


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("ADMIN_USER", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "SecretPass123!")
    monkeypatch.setenv("SYNC_TOKEN", "sync-token-xyz")
    monkeypatch.setenv("SYNC_WORKER_URL", "http://worker.local:8099")
    monkeypatch.setenv("COOKIE_SECURE", "false")

    db.invalidate_session_cache()
    main_mod._login_fails.clear()
    _seed_sample_specs(data_dir)
    db.init_state_db()

    with TestClient(app) as c:
        yield c


def _login(client: TestClient, username: str = "admin", password: str = "SecretPass123!") -> None:
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text


def test_healthz_pwa_and_version(client: TestClient) -> None:
    h = client.get("/api/healthz")
    assert h.status_code == 200
    body = h.json()
    assert body["status"] == "ok"
    assert body["specs_db_exists"] is True
    assert body["pages_count"] == 3
    assert "KST" in body["kst"]

    for method in ("get", "head"):
        m = getattr(client, method)("/manifest.json")
        assert m.status_code == 200
        assert "no-store" in m.headers.get("cache-control", "")

        s = getattr(client, method)("/sw.js")
        assert s.status_code == 200
        assert "no-store" in s.headers.get("cache-control", "")
        assert s.headers.get("service-worker-allowed") == "/"

    v = client.get("/api/version")
    assert v.status_code == 200
    assert v.json()["version"] == "1.1.0"
    assert isinstance(v.json()["features"], list)


def test_auth_protection_login_audit_and_rate_limit(client: TestClient) -> None:
    for path in (
        "/api/docs",
        "/api/toc",
        "/api/pages",
        "/api/pages/I23000__p001",
        "/api/favorites",
        "/api/admin/users",
        "/api/admin/audit-logs",
        "/api/admin/sync/status",
    ):
        assert client.get(path).status_code == 401

    bad = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert bad.status_code == 401

    _login(client)
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == "admin"
    assert me.json()["role"] == "admin"

    logs = client.get("/api/admin/audit-logs").json()["items"]
    actions = [row["action"] for row in logs]
    assert "LOGIN_FAILED" in actions
    assert "LOGIN_OK" in actions

    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401

    # Rate limit after repeated failures
    client2 = TestClient(app)
    for idx in range(16):
        res = client2.post(
            "/api/auth/login",
            json={"username": f"attacker_{idx}", "password": "bad"},
        )
    assert res.status_code == 429


def test_rbac_and_admin_user_crud(client: TestClient) -> None:
    _login(client)
    created = client.post(
        "/api/admin/users",
        json={"username": "field_eng", "password": "EngPass123!", "role": "viewer"},
    )
    assert created.status_code == 201

    users = client.get("/api/admin/users").json()["items"]
    assert any(u["username"] == "field_eng" and u["role"] == "viewer" for u in users)

    # Cannot delete own logged-in admin account
    assert client.delete("/api/admin/users/admin").status_code == 400

    # Non-admin user blocked from admin APIs
    viewer_client = TestClient(app)
    _login(viewer_client, "field_eng", "EngPass123!")
    assert viewer_client.get("/api/admin/users").status_code == 403
    assert viewer_client.get("/api/admin/audit-logs").status_code == 403
    assert viewer_client.get("/api/admin/sync/status").status_code == 403
    assert viewer_client.post("/api/admin/sync/trigger").status_code == 403

    # Admin updates viewer password and deletes
    pw_res = client.patch(
        "/api/admin/users/field_eng/password",
        json={"password": "NewPass456!"},
    )
    assert pw_res.status_code == 200
    assert client.delete("/api/admin/users/field_eng").status_code == 200


def test_docs_toc_and_multi_keyword_search(client: TestClient) -> None:
    _login(client)

    docs = client.get("/api/docs").json()["items"]
    assert len(docs) == 2
    assert docs[0]["doc_id"] == "I23000"

    toc = client.get("/api/toc?doc_id=I23000").json()["items"]
    assert [s["section_no"] for s in toc] == ["1.1", "2.3"]

    # Blank query returns all pages ordered cleanly
    all_pages = client.get("/api/pages").json()
    assert all_pages["total"] == 3

    # AND query
    res_and = client.get("/api/pages", params={"q": "차압전송기 보온"}).json()
    assert [it["page_id"] for it in res_and["items"]] == ["I23000__p001"]

    # OR + grouping
    res_or = client.get("/api/pages", params={"q": "(엘보 | Elbow) Fitting"}).json()
    assert [it["page_id"] for it in res_or["items"]] == ["I24000__p144"]

    # NOT exclusion
    res_not = client.get("/api/pages", params={"q": "케이블 -트레이"}).json()
    assert [it["page_id"] for it in res_not["items"]] == ["I23000__p002"]

    # Exact phrase
    res_phrase = client.get("/api/pages", params={"q": '"최소 100 mm"'}).json()
    assert [it["page_id"] for it in res_phrase["items"]] == ["I23000__p001"]

    # page_type=diagram filter
    res_diag = client.get("/api/pages", params={"page_type": "diagram"}).json()
    assert [it["page_id"] for it in res_diag["items"]] == ["I24000__p144"]

    # doc_ids filter
    res_doc = client.get("/api/pages", params={"q": "케이블", "doc_ids": "I24000"}).json()
    assert [it["page_id"] for it in res_doc["items"]] == ["I24000__p144"]


def test_page_detail_highlights_favorites_pins_and_media(client: TestClient) -> None:
    _login(client)
    # Create two viewers for IDOR verification on pins
    client.post("/api/admin/users", json={"username": "u1", "password": "UserPass11!", "role": "viewer"})
    client.post("/api/admin/users", json={"username": "u2", "password": "UserPass22!", "role": "viewer"})

    detail = client.get("/api/pages/I23000__p001", params={"q": "차압전송기 보온"}).json()
    assert detail["doc_id"] == "I23000"
    assert detail["siblings"]["prev_page_id"] is None
    assert detail["siblings"]["next_page_id"] == "I23000__p002"
    assert "최소 100 mm" in detail["clean_text"]
    matched_terms = {b["term"] for b in detail["highlight_boxes"]}
    assert {"차압전송기", "보온"}.issubset(matched_terms)
    assert len(detail["sections"]) == 2

    # Favorites
    assert client.post("/api/favorites/I23000__p001", json={"note": "보온 두께 확인"}).status_code == 200
    favs = client.get("/api/favorites").json()["items"]
    assert len(favs) == 1 and favs[0]["page_id"] == "I23000__p001"
    assert client.delete("/api/favorites/I23000__p001").status_code == 200

    # Pins + IDOR
    c_u1 = TestClient(app)
    _login(c_u1, "u1", "UserPass11!")
    c_u2 = TestClient(app)
    _login(c_u2, "u2", "UserPass22!")

    pin_res = c_u1.post("/api/pages/I23000__p001/pins", json={"x": 0.4, "y": 0.5, "note": "현장 체크"})
    assert pin_res.status_code == 201
    pin_id = pin_res.json()["id"]

    assert c_u2.delete(f"/api/pages/I23000__p001/pins/{pin_id}").status_code == 403
    assert c_u1.delete(f"/api/pages/I23000__p001/pins/{pin_id}").status_code == 200

    # Media routes + immutable cache when ?v=sha256
    thumb = client.get("/api/pages/I23000__p001/thumb.webp", params={"v": "sha_i23_p001"})
    assert thumb.status_code == 200
    assert thumb.headers["content-type"] == "image/webp"
    assert "immutable" in thumb.headers["cache-control"]

    preview = client.get("/api/pages/I23000__p001/preview.webp", params={"v": "sha_i23_p001"})
    assert preview.status_code == 200
    assert preview.content == b"RIFFfakewebppreview"

    pdf_res = client.get("/api/pages/I23000__p001/pdf")
    assert pdf_res.status_code == 200
    assert pdf_res.headers["content-type"] == "application/pdf"

    assert client.get("/api/pages/NO_SUCH_PAGE").status_code == 404
    assert client.get("/api/pages/bad$id").status_code == 400


def _make_tar_gz(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_sync_bundle_security_and_atomic_swap(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. Invalid sync token rejected
    bad_tar = _make_tar_gz({"thumbs/a.webp": b"123"})
    r_forbidden = client.post(
        "/api/admin/sync",
        content=bad_tar,
        headers={"X-Sync-Token": "wrong", "Content-Type": "application/gzip"},
    )
    assert r_forbidden.status_code == 403

    # 2. Zip-Slip path traversal rejected
    evil_tar = _make_tar_gz({"../escape.txt": b"pwn"})
    r_evil = client.post(
        "/api/admin/sync",
        content=evil_tar,
        headers={"X-Sync-Token": "sync-token-xyz", "Content-Type": "application/gzip"},
    )
    assert r_evil.status_code == 400

    # 3. Valid tar bundle swaps specs.db and media
    stage_dir = tmp_path / "stage_new"
    stage_dir.mkdir()
    new_db = stage_dir / "specs.db"
    db.init_specs_db(new_db)
    with db.specs_write_conn(new_db) as conn:
        conn.execute("INSERT INTO meta(key, value) VALUES ('built_at_kst', '2026-11-27 20:00:00 KST')")
        conn.execute(
            "INSERT INTO documents(doc_id, kh_code, spec_code, title, source_file, sha256, total_pages) "
            "VALUES ('I99000', 'KH_NEW', 'I99000', '신규 시방서', 'KH_NEW.pdf', 'docsha99', 1)"
        )
        conn.execute(
            """
            INSERT INTO pages(page_id, doc_id, page_no, total_pages, page_type, section_no, section_title, width_px, height_px, sha256, has_preview, has_pdf, ocr_status, clean_text, tokens_json)
            VALUES ('I99000__p001', 'I99000', 1, 1, 'text', '1.0', '신규 조항', 1000, 1400, 'newsha', 1, 1, 'native', '방폭 계기 접지 기준', '[]')
            """
        )
        conn.execute(
            "INSERT INTO pages_fts(page_id, doc_id, spec_code, doc_title, section_no, section_title, clean_text) VALUES ('I99000__p001', 'I99000', 'I99000', '신규 시방서', '1.0', '신규 조항', '방폭 계기 접지 기준')"
        )
        conn.execute(
            "INSERT INTO pages_trigram(page_id, doc_id, search_blob) VALUES ('I99000__p001', 'I99000', 'I99000 신규 시방서 1.0 신규 조항 방폭 계기 접지 기준')"
        )

    valid_tar = _make_tar_gz(
        {
            "specs.db": new_db.read_bytes(),
            "thumbs/I99000__p001.webp": b"RIFFnewthumb",
            "previews/I99000__p001.webp": b"RIFFnewprev",
            "pdfs/I99000__p001.pdf": b"%PDF-1.4\nnew\n",
        }
    )
    ok_res = client.post(
        "/api/admin/sync",
        content=valid_tar,
        headers={"X-Sync-Token": "sync-token-xyz", "Content-Type": "application/gzip"},
    )
    assert ok_res.status_code == 200
    assert ok_res.json()["status"] == "ok"

    _login(client)
    res_new = client.get("/api/pages", params={"q": "방폭 접지"}).json()
    assert [it["page_id"] for it in res_new["items"]] == ["I99000__p001"]

    # 4. Worker control proxy check
    import backend.main as main_mod

    monkeypatch.setattr(main_mod, "_call_worker", lambda method, path: (200, {"state": "idle", "last_result": {"pages": 1}}))
    st = client.get("/api/admin/sync/status")
    assert st.status_code == 200 and st.json()["state"] == "idle"
