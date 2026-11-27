(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  // ── State ──
  const state = {
    user: null,
    docs: [],
    selectedDocIds: new Set(),
    query: "",
    scope: "page",
    pageType: "",
    bodyOnly: false,
    favOnly: false,
    viewMode: "grid", // "grid" | "detail"
    page: 1,
    limit: 40,
    total: 0,
    terms: [],
    items: [],
    activePage: null,
    showBoxes: true,
    showText: true,
    pinMode: false,
    boxCursor: 0,
    zoom: 1,
    panX: 0,
    panY: 0,
    lastKstDate: getKSTDateString(),
    deferredPrompt: null,
    versionData: null,
    syncPollTimer: null,
    pendingPinCoord: null
  };

  // ── KST Date Helper (Rule 4: never toISOString()) ──
  function getKSTDateString() {
    return new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Seoul",
      year: "numeric",
      month: "2-digit",
      day: "2-digit"
    }).format(new Date());
  }

  // ── Safe HTML Escaping & Multi-Color Highlight Renderer ──
  function escapeHtml(raw) {
    return String(raw ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function renderMarkedSnippet(snippet) {
    const escaped = escapeHtml(snippet || "");
    return escaped.replace(/\[\[H([0-3])\]\]([\s\S]*?)\[\[\/H\1\]\]/g, (_m, idx, txt) => {
      return `<mark class="kw-${idx}">${txt}</mark>`;
    });
  }

  function renderCleanTextWithTerms(text, terms) {
    const clean = text || "(추출된 텍스트가 없습니다)";
    if (!terms || !terms.length) {
      return escapeHtml(clean);
    }
    const indexed = terms
      .map((t, idx) => ({ t, idx: idx % 4 }))
      .filter((x) => x.t)
      .sort((a, b) => b.t.length - a.t.length);

    if (!indexed.length) return escapeHtml(clean);

    const escapeRegex = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const rx = new RegExp(`(${indexed.map((x) => escapeRegex(x.t)).join("|")})`, "gi");
    const colorMap = new Map(indexed.map((x) => [x.t.toLowerCase(), x.idx]));

    let out = "";
    let lastIndex = 0;
    for (const match of clean.matchAll(rx)) {
      const mStart = match.index ?? 0;
      const matchedText = match[0];
      out += escapeHtml(clean.slice(lastIndex, mStart));
      const cIdx = colorMap.get(matchedText.toLowerCase()) ?? 0;
      out += `<mark class="kw-${cIdx}">${escapeHtml(matchedText)}</mark>`;
      lastIndex = mStart + matchedText.length;
    }
    out += escapeHtml(clean.slice(lastIndex));
    return out;
  }

  let toastTimer = null;
  function showToast(msg) {
    const el = $("toast");
    el.textContent = msg;
    el.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), 2600);
  }

  // ── Network Fetch with Rule 9 Resilient Session Handling ──
  async function apiFetch(url, opts = {}) {
    const res = await fetch(url, {
      credentials: "same-origin",
      ...opts,
      headers: {
        ...(opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json" } : {}),
        ...(opts.headers || {})
      }
    });
    if (res.status === 401) {
      showLoginOverlay();
      throw new Error("UNAUTHORIZED");
    }
    return res;
  }

  function showLoginOverlay() {
    state.user = null;
    $("loginOverlay").classList.remove("hidden");
    $("userBadge").classList.add("hidden");
    $("btnLogout").classList.add("hidden");
    $("btnAdminMenu").classList.add("hidden");
  }

  function hideLoginOverlay(user) {
    state.user = user;
    $("loginOverlay").classList.add("hidden");
    $("loginError").classList.add("hidden");
    $("userBadge").textContent = `${user.username} (${user.role})`;
    $("userBadge").classList.remove("hidden");
    $("btnLogout").classList.remove("hidden");
    $("btnAdminMenu").classList.toggle("hidden", user.role !== "admin");
  }

  // ── Theme ──
  function initTheme() {
    const saved = localStorage.getItem("ssv_theme") || "dark";
    document.body.setAttribute("data-theme", saved);
  }

  function toggleTheme() {
    const cur = document.body.getAttribute("data-theme") === "light" ? "dark" : "light";
    document.body.setAttribute("data-theme", cur);
    localStorage.setItem("ssv_theme", cur);
  }

  // ── Multi-Keyword Query Builder & Chips ──
  function parseChipsFromQuery(q) {
    const chips = [];
    const rx = /"([^"]+)"|([+-]?\S+)/g;
    for (const m of (q || "").matchAll(rx)) {
      const token = m[0];
      if (!token || token === "|") continue;
      if (m[1]) {
        chips.push({ raw: token, label: `구문: "${m[1]}"`, type: "phrase" });
      } else if ((token.startsWith("-") || token.startsWith("!")) && token.length > 1) {
        chips.push({ raw: token, label: `제외: ${token.slice(1)}`, type: "neg" });
      } else {
        chips.push({ raw: token, label: token, type: "and" });
      }
    }
    return chips;
  }

  function renderKeywordChips() {
    const container = $("kwChipsList");
    container.innerHTML = "";
    const chips = parseChipsFromQuery($("searchInput").value);
    for (const chip of chips) {
      const span = document.createElement("span");
      span.className = `kw-chip${chip.type === "neg" ? " is-neg" : ""}`;
      const lbl = document.createElement("span");
      lbl.textContent = chip.label;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "kw-chip-del";
      btn.textContent = "×";
      btn.title = "키워드 제거";
      btn.addEventListener("click", () => {
        const cur = $("searchInput").value;
        const next = cur.replace(chip.raw, "").replace(/\s{2,}/g, " ").trim();
        $("searchInput").value = next;
        updateClearButton();
        runSearch(1);
      });
      span.appendChild(lbl);
      span.appendChild(btn);
      container.appendChild(span);
    }
  }

  function appendKeywordOp(op) {
    const input = $("kwQuickInput");
    const raw = input.value.trim();
    if (!raw) {
      input.focus();
      return;
    }
    const clean = raw.replace(/"/g, "");
    let snippet = clean;
    if (op === "or") {
      const cur = $("searchInput").value.trim();
      snippet = cur ? `| ${clean}` : clean;
    } else if (op === "not") {
      snippet = `-${clean}`;
    } else if (op === "phrase") {
      snippet = `"${clean}"`;
    }
    const existing = $("searchInput").value.trim();
    $("searchInput").value = existing ? `${existing} ${snippet}` : snippet;
    input.value = "";
    updateClearButton();
    runSearch(1);
  }

  function updateClearButton() {
    $("btnClearSearch").classList.toggle("hidden", !$("searchInput").value.trim());
    renderKeywordChips();
  }

  // ── Bootstrap Data (Meta & Docs) ──
  async function loadMetaAndDocs() {
    const [metaRes, docsRes] = await Promise.all([
      apiFetch("/api/meta"),
      apiFetch("/api/docs")
    ]);
    const meta = await metaRes.json();
    const docsPayload = await docsRes.json();
    state.docs = docsPayload.items || [];

    const dbReady = Boolean(meta.db_ready && docsPayload.db_ready);
    $("syncBanner").classList.toggle("hidden", dbReady);

    if (dbReady) {
      $("metaBadge").textContent =
        `NRD-K2 · ${meta.total_docs}권 · ${meta.total_pages}p · OCR ${meta.total_ocr_pages}p`;
    } else {
      $("metaBadge").textContent = "NRD-K2 · 초기 동기화 대기중";
    }

    renderDocChips();
  }

  function renderDocChips() {
    const wrap = $("docChips");
    wrap.innerHTML = "";

    const allBtn = document.createElement("button");
    allBtn.type = "button";
    allBtn.className = `doc-chip${state.selectedDocIds.size === 0 ? " active" : ""}`;
    allBtn.textContent = "전체 시방서";
    allBtn.addEventListener("click", () => {
      state.selectedDocIds.clear();
      renderDocChips();
      runSearch(1);
    });
    wrap.appendChild(allBtn);

    for (const d of state.docs) {
      const btn = document.createElement("button");
      btn.type = "button";
      const selected = state.selectedDocIds.has(d.doc_id);
      btn.className = `doc-chip${selected ? " active" : ""}`;
      btn.textContent = `${d.spec_code} · ${d.title} (${d.total_pages}p)`;
      btn.addEventListener("click", () => {
        if (state.selectedDocIds.has(d.doc_id)) {
          state.selectedDocIds.delete(d.doc_id);
        } else {
          state.selectedDocIds.add(d.doc_id);
        }
        renderDocChips();
        runSearch(1);
      });
      wrap.appendChild(btn);
    }
  }

  // ── Search & Paging ──
  async function runSearch(targetPage = 1) {
    if (!state.user) return;
    state.page = Math.max(1, targetPage);
    state.query = $("searchInput").value.trim();
    state.scope = $("scopeSelect").value;
    state.pageType = $("pageTypeSelect").value;

    const params = new URLSearchParams({
      q: state.query,
      scope: state.scope,
      page_type: state.pageType,
      body_only: state.bodyOnly ? "true" : "false",
      fav_only: state.favOnly ? "true" : "false",
      page: String(state.page),
      limit: String(state.limit)
    });
    if (state.selectedDocIds.size > 0) {
      params.set("doc_ids", Array.from(state.selectedDocIds).join(","));
    }

    const res = await apiFetch(`/api/pages?${params.toString()}`);
    const data = await res.json();
    state.total = data.total || 0;
    state.terms = data.terms || [];
    state.items = data.items || [];

    const totalPages = Math.max(1, Math.ceil(state.total / state.limit));
    const pageInfoText = `${state.page} / ${totalPages}`;
    $("gridPageInfo").textContent = pageInfoText;
    $("sidePageInfo").textContent = pageInfoText;
    $("resCount").textContent = String(state.total);
    $("btnGridPrev").disabled = state.page <= 1;
    $("btnSidePrev").disabled = state.page <= 1;
    $("btnGridNext").disabled = state.page >= totalPages;
    $("btnSideNext").disabled = state.page >= totalPages;

    const summaryBase = state.query
      ? `"${state.query}" 검색 결과 ${state.total}건`
      : `전체 장표 ${state.total}페이지`;
    $("gridSummary").textContent = state.scope === "doc" && state.query
      ? `${summaryBase} (시방서 단위 교차검색)`
      : summaryBase;

    renderGridCards();
    renderSideResults();
  }

  function renderGridCards() {
    const grid = $("pageGrid");
    grid.innerHTML = "";
    if (!state.items.length) {
      const empty = document.createElement("div");
      empty.className = "dialog-hint";
      empty.textContent = "조건에 일치하는 시방서 장표가 없습니다.";
      grid.appendChild(empty);
      return;
    }

    const frag = document.createDocumentFragment();
    for (const item of state.items) {
      const card = document.createElement("button");
      card.type = "button";
      const isActive = state.activePage && state.activePage.page_id === item.page_id;
      card.className = `page-card${isActive ? " selected" : ""}`;

      const thumbUrl = `/api/pages/${encodeURIComponent(item.page_id)}/thumb.webp?v=${encodeURIComponent(item.v || "")}`;
      const ocrBadge = item.ocr_status === "ocr_done" ? '<span class="badge-ocr">OCR</span>' : "";
      const secText = item.section_no ? `${item.section_no} ${item.section_title || ""}`.trim() : (item.section_title || "본문");

      card.innerHTML = `
        <div class="card-thumb-wrap">
          <img src="${escapeHtml(thumbUrl)}" loading="lazy" alt="${escapeHtml(item.page_id)}">
          <div class="card-badges">
            <span class="badge-spec">${escapeHtml(item.spec_code)}</span>
            <div>${ocrBadge}<span class="badge-page">p.${item.page_no}/${item.total_pages}</span></div>
          </div>
        </div>
        <div class="card-body">
          <div class="card-doc-title">${escapeHtml(item.doc_title)}</div>
          <div class="card-section">${escapeHtml(secText)}</div>
          <div class="card-snippet">${renderMarkedSnippet(item.snippet)}</div>
        </div>
      `;
      card.addEventListener("click", () => {
        setViewMode("detail");
        openPageDetail(item.page_id, true);
      });
      frag.appendChild(card);
    }
    grid.appendChild(frag);
  }

  function renderSideResults() {
    const list = $("resultsList");
    list.innerHTML = "";
    if (!state.items.length) {
      const empty = document.createElement("div");
      empty.className = "dialog-hint";
      empty.textContent = "검색 결과가 없습니다.";
      list.appendChild(empty);
      return;
    }

    const frag = document.createDocumentFragment();
    for (const item of state.items) {
      const btn = document.createElement("button");
      btn.type = "button";
      const isActive = state.activePage && state.activePage.page_id === item.page_id;
      btn.className = `side-item${isActive ? " active" : ""}`;
      const secText = item.section_no ? `${item.section_no} ${item.section_title || ""}`.trim() : (item.section_title || "본문");
      btn.innerHTML = `
        <div class="side-item-top">
          <span>${escapeHtml(item.spec_code)} · p.${item.page_no}</span>
          <span>${item.ocr_status === "ocr_done" ? "OCR" : escapeHtml(item.page_type)}</span>
        </div>
        <div class="side-item-title">${escapeHtml(secText)}</div>
        <div class="side-item-snip">${renderMarkedSnippet(item.snippet)}</div>
      `;
      btn.addEventListener("click", () => openPageDetail(item.page_id, true));
      frag.appendChild(btn);
    }
    list.appendChild(frag);
  }

  // ── View Modes & Tabs ──
  function setViewMode(mode) {
    state.viewMode = mode;
    const isGrid = mode === "grid";
    $("gridView").classList.toggle("hidden", !isGrid);
    $("detailView").classList.toggle("hidden", isGrid);
    $("btnModeGrid").classList.toggle("active", isGrid);
    $("btnModeDetail").classList.toggle("active", !isGrid);
    if (!isGrid && !state.activePage && state.items.length) {
      openPageDetail(state.items[0].page_id, false);
    }
    if (!isGrid && state.activePage) {
      requestAnimationFrame(() => fitCanvas());
    }
  }

  function switchSideTab(tabName) {
    document.querySelectorAll(".side-tab").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.tab === tabName);
    });
    $("tabResults").classList.toggle("hidden", tabName !== "results");
    $("tabToc").classList.toggle("hidden", tabName !== "toc");
    $("tabFavs").classList.toggle("hidden", tabName !== "favs");
    if (tabName === "favs") {
      loadFavorites();
    }
  }

  // ── Page Detail, Highlight Overlay, TOC, Text Inspector ──
  async function openPageDetail(pageId, mobileOpenStage = false) {
    if (!pageId) return;
    const params = new URLSearchParams();
    if (state.query) params.set("q", state.query);

    const res = await apiFetch(`/api/pages/${encodeURIComponent(pageId)}?${params.toString()}`);
    if (!res.ok) {
      showToast("장표를 불러오지 못했습니다.");
      return;
    }
    const detail = await res.json();
    state.activePage = detail;
    state.boxCursor = 0;

    if (mobileOpenStage) {
      $("detailView").classList.add("mobile-stage-open");
    }

    renderGridCards();
    renderSideResults();
    renderStageDetail(detail);
    renderTocTree(detail);
  }

  function renderStageDetail(detail) {
    $("emptyState").classList.add("hidden");
    const secLabel = detail.section_no ? `${detail.section_no} ${detail.section_title || ""}`.trim() : (detail.section_title || "");
    $("pageTitleHeader").textContent =
      `${detail.spec_code} · p.${detail.page_no}/${detail.total_pages}${secLabel ? ` · ${secLabel}` : ""}`;

    $("btnPrevPage").disabled = !detail.siblings?.prev_page_id;
    $("btnNextPage").disabled = !detail.siblings?.next_page_id;

    // Favorite state
    $("btnFavToggle").classList.toggle("active", Boolean(detail.is_favorite));
    $("btnFavToggle").setAttribute("aria-pressed", String(Boolean(detail.is_favorite)));
    $("btnFavToggle").textContent = detail.is_favorite ? "★ 즐겨찾기" : "☆ 즐겨찾기";

    // Single PDF link
    const pdfBtn = $("btnOpenPdf");
    if (detail.has_pdf) {
      pdfBtn.href = `/api/pages/${encodeURIComponent(detail.page_id)}/pdf?v=${encodeURIComponent(detail.v || "")}`;
      pdfBtn.classList.remove("hidden");
    } else {
      pdfBtn.classList.add("hidden");
    }

    // OCR badge + clean text
    $("ocrStatusBadge").classList.toggle("hidden", detail.ocr_status !== "ocr_done");
    $("cleanTextBody").innerHTML = renderCleanTextWithTerms(detail.clean_text, detail.terms || state.terms);

    // Highlight counter
    const boxes = detail.highlight_boxes || [];
    const hitBtn = $("btnNextBox");
    if (boxes.length > 0) {
      hitBtn.classList.remove("hidden");
      hitBtn.textContent = `매칭 1/${boxes.length}`;
    } else {
      hitBtn.classList.add("hidden");
    }

    // Load preview image
    const img = $("previewImg");
    img.onload = () => fitCanvas();
    img.src = `/api/pages/${encodeURIComponent(detail.page_id)}/preview.webp?v=${encodeURIComponent(detail.v || "")}`;

    renderHighlightBoxes();
    renderPins();
  }

  function renderHighlightBoxes() {
    const wrap = $("overlayBoxes");
    wrap.innerHTML = "";
    const detail = state.activePage;
    if (!detail || !state.showBoxes) return;

    const boxes = detail.highlight_boxes || [];
    boxes.forEach((b, idx) => {
      const div = document.createElement("div");
      div.className = `hl-box hl-${b.term_idx % 4}${idx === state.boxCursor ? " focused" : ""}`;
      div.style.left = `${(b.x0 * 100).toFixed(3)}%`;
      div.style.top = `${(b.y0 * 100).toFixed(3)}%`;
      div.style.width = `${Math.max(0.4, (b.x1 - b.x0) * 100).toFixed(3)}%`;
      div.style.height = `${Math.max(0.6, (b.y1 - b.y0) * 100).toFixed(3)}%`;
      div.title = b.term;
      wrap.appendChild(div);
    });
  }

  function jumpToNextHighlightBox() {
    const detail = state.activePage;
    const boxes = detail?.highlight_boxes || [];
    if (!boxes.length) return;
    state.boxCursor = (state.boxCursor + 1) % boxes.length;
    $("btnNextBox").textContent = `매칭 ${state.boxCursor + 1}/${boxes.length}`;
    renderHighlightBoxes();

    const box = boxes[state.boxCursor];
    const vp = $("canvasViewport").getBoundingClientRect();
    const img = $("previewImg");
    if (!img.clientWidth || !img.clientHeight) return;

    const targetZoom = Math.max(state.zoom, 1.8);
    state.zoom = Math.min(8, targetZoom);
    const cx = ((box.x0 + box.x1) / 2) * img.clientWidth;
    const cy = ((box.y0 + box.y1) / 2) * img.clientHeight;
    state.panX = vp.width / 2 - cx * state.zoom;
    state.panY = vp.height / 2 - cy * state.zoom;
    applyTransform();
  }

  function renderTocTree(detail) {
    $("tocDocHead").textContent = `${detail.spec_code} · ${detail.doc_title}`;
    const tree = $("tocTree");
    tree.innerHTML = "";
    const sections = detail.sections || [];
    if (!sections.length) {
      const empty = document.createElement("div");
      empty.className = "dialog-hint";
      empty.textContent = "추출된 목차 조항이 없습니다.";
      tree.appendChild(empty);
      return;
    }

    const frag = document.createDocumentFragment();
    for (const sec of sections) {
      const btn = document.createElement("button");
      btn.type = "button";
      const active = sec.page_id === detail.page_id;
      const lvlClass = sec.level >= 3 ? " lvl-3" : sec.level === 2 ? " lvl-2" : "";
      btn.className = `toc-item${lvlClass}${active ? " active" : ""}`;
      btn.innerHTML = `
        <span>${escapeHtml(sec.section_no)} ${escapeHtml(sec.title)}</span>
        <span class="pager-text">p.${sec.start_page}</span>
      `;
      btn.addEventListener("click", () => openPageDetail(sec.page_id, true));
      frag.appendChild(btn);
    }
    tree.appendChild(frag);
  }

  // ── 60fps Zoom & Pan Canvas ──
  function applyTransform() {
    $("canvasTransform").style.transform =
      `translate3d(${state.panX.toFixed(1)}px, ${state.panY.toFixed(1)}px, 0) scale(${state.zoom.toFixed(4)})`;
    $("zoomLabel").textContent = `${Math.round(state.zoom * 100)}%`;
  }

  function fitCanvas() {
    const vp = $("canvasViewport");
    const img = $("previewImg");
    const rect = vp.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const natW = img.naturalWidth || state.activePage?.width_px || 1000;
    const natH = img.naturalHeight || state.activePage?.height_px || 1400;

    $("canvasTransform").style.width = `${natW}px`;
    const scale = Math.min((rect.width - 24) / natW, (rect.height - 24) / natH);
    state.zoom = Math.max(0.1, Math.min(4, scale));
    state.panX = (rect.width - natW * state.zoom) / 2;
    state.panY = Math.max(12, (rect.height - natH * state.zoom) / 2);
    applyTransform();
  }

  function zoomAtPoint(factor, clientX, clientY) {
    const rect = $("canvasViewport").getBoundingClientRect();
    const ox = clientX - rect.left;
    const oy = clientY - rect.top;
    const nextZoom = Math.max(0.15, Math.min(8, state.zoom * factor));
    const ratio = nextZoom / state.zoom;
    state.panX = ox - (ox - state.panX) * ratio;
    state.panY = oy - (oy - state.panY) * ratio;
    state.zoom = nextZoom;
    applyTransform();
  }

  function setupCanvasGestures() {
    const vp = $("canvasViewport");
    let dragging = false;
    let moved = false;
    let startX = 0;
    let startY = 0;
    let originX = 0;
    let originY = 0;
    const pointers = new Map();
    let pinchDist0 = 0;
    let pinchZoom0 = 1;

    vp.addEventListener("wheel", (e) => {
      e.preventDefault();
      zoomAtPoint(e.deltaY < 0 ? 1.14 : 0.88, e.clientX, e.clientY);
    }, { passive: false });

    vp.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".pin-marker")) return;
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      vp.setPointerCapture(e.pointerId);
      if (pointers.size === 1) {
        dragging = true;
        moved = false;
        startX = e.clientX;
        startY = e.clientY;
        originX = state.panX;
        originY = state.panY;
      } else if (pointers.size === 2) {
        dragging = false;
        const pts = Array.from(pointers.values());
        pinchDist0 = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y);
        pinchZoom0 = state.zoom;
      }
    });

    vp.addEventListener("pointermove", (e) => {
      if (!pointers.has(e.pointerId)) return;
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pointers.size === 2) {
        const pts = Array.from(pointers.values());
        const d = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y);
        if (pinchDist0 > 10) {
          const cx = (pts[0].x + pts[1].x) / 2;
          const cy = (pts[0].y + pts[1].y) / 2;
          const targetZoom = Math.max(0.15, Math.min(8, pinchZoom0 * (d / pinchDist0)));
          zoomAtPoint(targetZoom / state.zoom, cx, cy);
        }
        return;
      }
      if (dragging) {
        const dx = e.clientX - startX;
        const dy = e.clientY - startY;
        if (Math.abs(dx) > 4 || Math.abs(dy) > 4) moved = true;
        state.panX = originX + dx;
        state.panY = originY + dy;
        applyTransform();
      }
    });

    const endPtr = (e) => {
      if (pointers.size === 1 && !moved && state.pinMode && state.activePage) {
        const tfRect = $("canvasTransform").getBoundingClientRect();
        const nx = (e.clientX - tfRect.left) / tfRect.width;
        const ny = (e.clientY - tfRect.top) / tfRect.height;
        if (nx >= 0 && nx <= 1 && ny >= 0 && ny <= 1) {
          state.pendingPinCoord = { x: nx, y: ny };
          $("pinCoordHint").textContent = `위치 (${Math.round(nx * 100)}%, ${Math.round(ny * 100)}%)에 메모를 남깁니다.`;
          $("pinNoteInput").value = "";
          openDialog("pinDialog");
        }
      }
      pointers.delete(e.pointerId);
      dragging = false;
    };

    vp.addEventListener("pointerup", endPtr);
    vp.addEventListener("pointercancel", endPtr);
  }

  // ── Pins & Favorites ──
  function renderPins() {
    const overlay = $("overlayPins");
    const list = $("pinsList");
    overlay.innerHTML = "";
    list.innerHTML = "";

    const detail = state.activePage;
    const pins = detail?.pins || [];
    if (!pins.length) {
      const hint = document.createElement("div");
      hint.className = "dialog-hint";
      hint.textContent = "등록된 핀 메모가 없습니다.";
      list.appendChild(hint);
      return;
    }

    pins.forEach((p, idx) => {
      const marker = document.createElement("button");
      marker.type = "button";
      marker.className = "pin-marker";
      marker.style.left = `${(p.x * 100).toFixed(2)}%`;
      marker.style.top = `${(p.y * 100).toFixed(2)}%`;
      marker.textContent = String(idx + 1);
      marker.title = `${p.username}: ${p.note}`;
      marker.addEventListener("click", () => showToast(`#${idx + 1} (${p.username}): ${p.note}`));
      overlay.appendChild(marker);

      const row = document.createElement("div");
      row.className = "pin-row";
      const txtWrap = document.createElement("div");
      txtWrap.innerHTML = `<strong>#${idx + 1} ${escapeHtml(p.username)}</strong><br>${escapeHtml(p.note)}`;
      row.appendChild(txtWrap);

      if (state.user && (state.user.username === p.username || state.user.role === "admin")) {
        const del = document.createElement("button");
        del.type = "button";
        del.className = "btn-small";
        del.textContent = "삭제";
        del.addEventListener("click", async () => {
          const res = await apiFetch(`/api/pages/${encodeURIComponent(detail.page_id)}/pins/${p.id}`, {
            method: "DELETE"
          });
          if (res.ok) {
            showToast("핀 메모를 삭제했습니다.");
            openPageDetail(detail.page_id, false);
          }
        });
        row.appendChild(del);
      }
      list.appendChild(row);
    });
  }

  async function loadFavorites() {
    if (!state.user) return;
    const res = await apiFetch("/api/favorites");
    const data = await res.json();
    const items = data.items || [];
    $("favCount").textContent = String(items.length);

    const list = $("favsList");
    list.innerHTML = "";
    if (!items.length) {
      const empty = document.createElement("div");
      empty.className = "dialog-hint";
      empty.textContent = "즐겨찾기한 장표가 없습니다.";
      list.appendChild(empty);
      return;
    }

    for (const item of items) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "side-item";
      btn.innerHTML = `
        <div class="side-item-top">
          <span>${escapeHtml(item.spec_code || item.page_id)} · p.${item.page_no || "?"}</span>
          <span>${escapeHtml(item.created_at || "")}</span>
        </div>
        <div class="side-item-title">${escapeHtml(item.section_no ? `${item.section_no} ${item.section_title}` : (item.doc_title || item.page_id))}</div>
        ${item.note ? `<div class="side-item-snip">${escapeHtml(item.note)}</div>` : ""}
      `;
      btn.addEventListener("click", () => {
        setViewMode("detail");
        openPageDetail(item.page_id, true);
      });
      list.appendChild(btn);
    }
  }

  async function toggleFavorite() {
    const detail = state.activePage;
    if (!detail) return;
    const method = detail.is_favorite ? "DELETE" : "POST";
    const res = await apiFetch(`/api/favorites/${encodeURIComponent(detail.page_id)}`, {
      method,
      body: method === "POST" ? JSON.stringify({ note: detail.section_title || "" }) : undefined
    });
    if (res.ok) {
      detail.is_favorite = !detail.is_favorite;
      renderStageDetail(detail);
      loadFavorites();
      showToast(detail.is_favorite ? "즐겨찾기에 추가했습니다." : "즐겨찾기에서 해제했습니다.");
    }
  }

  // ── Admin Center ──
  async function loadSyncStatus() {
    const box = $("syncStatusBox");
    try {
      const res = await apiFetch("/api/admin/sync/status");
      const data = await res.json();
      const prog = data.progress || {};
      box.innerHTML = `
        <div><strong>상태:</strong> ${escapeHtml(data.state || "unknown")} (주기: ${escapeHtml(data.interval_min)}분)</div>
        <div><strong>최근 확인(KST):</strong> ${escapeHtml(data.last_checked_kst || "-")}</div>
        <div><strong>최근 반영(KST):</strong> ${escapeHtml(data.last_updated_kst || "-")}</div>
        <div><strong>사유:</strong> ${escapeHtml(data.last_reason || "-")}</div>
        ${prog.stage ? `<div><strong>진행:</strong> ${escapeHtml(prog.stage)} ${escapeHtml(prog.detail || "")}</div>` : ""}
        ${data.last_error ? `<div style="color:var(--danger)"><strong>오류:</strong> ${escapeHtml(data.last_error)}</div>` : ""}
      `;
      if (data.state === "running") {
        clearTimeout(state.syncPollTimer);
        state.syncPollTimer = setTimeout(loadSyncStatus, 2500);
      } else {
        clearTimeout(state.syncPollTimer);
      }
    } catch (err) {
      box.textContent = "동기화 워커 상태를 조회할 수 없습니다.";
    }
  }

  async function loadAdminUsers() {
    const res = await apiFetch("/api/admin/users");
    const data = await res.json();
    const list = $("adminUsersList");
    list.innerHTML = "";

    for (const u of data.items || []) {
      const row = document.createElement("div");
      row.className = "admin-row";
      row.innerHTML = `
        <div>
          <strong>${escapeHtml(u.username)}</strong> (${escapeHtml(u.role)})
          <div class="pager-text">생성: ${escapeHtml(u.created_at)}</div>
        </div>
      `;
      const actions = document.createElement("div");
      actions.style.display = "flex";
      actions.style.gap = "6px";

      const resetBtn = document.createElement("button");
      resetBtn.type = "button";
      resetBtn.className = "btn-small";
      resetBtn.textContent = "비번변경";
      resetBtn.addEventListener("click", async () => {
        const pw = window.prompt(`${u.username}의 새 비밀번호(6자 이상):`);
        if (!pw || pw.length < 6) return;
        const r = await apiFetch(`/api/admin/users/${encodeURIComponent(u.username)}/password`, {
          method: "PATCH",
          body: JSON.stringify({ password: pw })
        });
        if (r.ok) showToast("비밀번호를 변경했습니다.");
      });
      actions.appendChild(resetBtn);

      if (state.user && u.username !== state.user.username) {
        const delBtn = document.createElement("button");
        delBtn.type = "button";
        delBtn.className = "btn-small";
        delBtn.textContent = "삭제";
        delBtn.addEventListener("click", async () => {
          if (!window.confirm(`${u.username} 계정을 삭제하시겠습니까?`)) return;
          const r = await apiFetch(`/api/admin/users/${encodeURIComponent(u.username)}`, { method: "DELETE" });
          if (r.ok) {
            showToast("계정을 삭제했습니다.");
            loadAdminUsers();
          }
        });
        actions.appendChild(delBtn);
      }
      row.appendChild(actions);
      list.appendChild(row);
    }
  }

  async function loadAuditLogs() {
    const action = $("auditActionFilter").value;
    const params = new URLSearchParams({ limit: "100" });
    if (action) params.set("action", action);
    const res = await apiFetch(`/api/admin/audit-logs?${params.toString()}`);
    const data = await res.json();
    const list = $("adminAuditList");
    list.innerHTML = "";
    for (const log of data.items || []) {
      const row = document.createElement("div");
      row.className = "admin-row";
      row.innerHTML = `
        <div>
          <strong>[${escapeHtml(log.action)}]</strong> ${escapeHtml(log.username || "-")} → ${escapeHtml(log.target || "-")}
          <div class="pager-text">${escapeHtml(log.at_kst)} · IP ${escapeHtml(log.ip || "-")}${log.detail ? ` · ${escapeHtml(log.detail)}` : ""}</div>
        </div>
      `;
      list.appendChild(row);
    }
  }

  // ── Dialog Helpers (iOS Safari Fallback + Rule 17) ──
  function openDialog(id) {
    const dlg = $(id);
    if (!dlg.open) {
      dlg.showModal();
    }
  }

  function closeDialog(id) {
    const dlg = $(id);
    if (dlg.open) {
      dlg.close();
    }
    if (id === "adminDialog") {
      clearTimeout(state.syncPollTimer);
    }
  }

  function setupDialogs() {
    document.querySelectorAll("[data-close-dialog]").forEach((btn) => {
      btn.addEventListener("click", () => closeDialog(btn.dataset.closeDialog));
    });
    document.querySelectorAll("dialog.app-dialog").forEach((dlg) => {
      dlg.addEventListener("click", (e) => {
        if (e.target === dlg) closeDialog(dlg.id);
      });
    });
  }

  // ── Version Check & #modalWhatsNew Auto-Popup (Rule 16) ──
  async function checkAppVersion(forceShow = false) {
    try {
      const res = await fetch(`/api/version?t=${Date.now()}`, { cache: "no-store" });
      if (!res.ok) return;
      const v = await res.json();
      state.versionData = v;
      $("btnWhatsNew").textContent = `v${v.version}`;
      $("whatsNewTitle").textContent = `${v.name || "시방서 스마트 뷰어"} v${v.version}`;
      $("whatsNewMeta").textContent = `빌드 ${v.build} · 배포 ${v.released_at}`;

      const featWrap = $("whatsNewFeatures");
      featWrap.innerHTML = "";
      for (const f of v.features || []) {
        const card = document.createElement("div");
        card.className = "feature-card";
        card.innerHTML = `<strong>${escapeHtml(f.icon || "•")} ${escapeHtml(f.title)}</strong><p>${escapeHtml(f.desc)}</p>`;
        featWrap.appendChild(card);
      }

      const seen = localStorage.getItem("ssv_seen_version");
      if (forceShow || (state.user && seen !== v.version)) {
        openDialog("modalWhatsNew");
      }
    } catch (_err) {
      // ignore offline
    }
  }

  // ── PWA Install & Service Worker (Rule 15 & Rule 16 Loop Guard) ──
  function setupPWA() {
    window.addEventListener("beforeinstallprompt", (e) => {
      e.preventDefault();
      state.deferredPrompt = e;
      $("btnPromptInstallNow").classList.remove("hidden");
    });

    $("btnInstallPwa").addEventListener("click", () => openDialog("pwaDialog"));
    $("btnPromptInstallNow").addEventListener("click", async () => {
      if (!state.deferredPrompt) return;
      state.deferredPrompt.prompt();
      await state.deferredPrompt.userChoice;
      state.deferredPrompt = null;
      $("btnPromptInstallNow").classList.add("hidden");
      closeDialog("pwaDialog");
    });

    if ("serviceWorker" in navigator) {
      let reloading = false;
      navigator.serviceWorker.addEventListener("controllerchange", () => {
        if (reloading) return;
        const lastTs = Number(sessionStorage.getItem("ssv_reload_ts") || "0");
        if (Date.now() - lastTs < 60_000) return;
        reloading = true;
        sessionStorage.setItem("ssv_reload_ts", String(Date.now()));
        window.location.reload();
      });

      navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => null);
    }
  }

  // ── Mobile Lifecycle & KST Date Rollover (Rule 5) ──
  function checkLifecycleAndDate() {
    const today = getKSTDateString();
    if (today !== state.lastKstDate) {
      state.lastKstDate = today;
      if (state.user) {
        loadMetaAndDocs().catch(() => null);
      }
    }
    checkAppVersion(false);
  }

  // ── Auth Boot ──
  async function bootstrapSession() {
    try {
      const res = await fetch("/api/auth/me", { credentials: "same-origin" });
      if (res.status === 401) {
        showLoginOverlay();
        return;
      }
      if (!res.ok) {
        // 일시 서버/네트워크 오류 시 기존 화면 유지 (Rule 9)
        return;
      }
      const user = await res.json();
      hideLoginOverlay(user);
      await loadMetaAndDocs();
      await Promise.all([runSearch(1), loadFavorites()]);
      await checkAppVersion(false);
    } catch (_err) {
      // 오프라인 복귀 대기
    }
  }

  // ── Event Binding ──
  function bindEvents() {
    $("btnTheme").addEventListener("click", toggleTheme);
    $("btnWhatsNew").addEventListener("click", () => checkAppVersion(true));
    $("btnDismissWhatsNew").addEventListener("click", () => {
      if (state.versionData?.version) {
        localStorage.setItem("ssv_seen_version", state.versionData.version);
      }
      closeDialog("modalWhatsNew");
    });

    $("btnModeGrid").addEventListener("click", () => setViewMode("grid"));
    $("btnModeDetail").addEventListener("click", () => setViewMode("detail"));
    $("btnMobileBack").addEventListener("click", () => {
      $("detailView").classList.remove("mobile-stage-open");
    });

    $("btnBannerRefresh").addEventListener("click", () => {
      loadMetaAndDocs().then(() => runSearch(1));
    });

    // Login & Logout
    $("loginForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      $("loginError").classList.add("hidden");
      try {
        const res = await fetch("/api/auth/login", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            username: $("loginUser").value.trim(),
            password: $("loginPass").value
          })
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          $("loginError").textContent = data.detail || "로그인에 실패했습니다.";
          $("loginError").classList.remove("hidden");
          return;
        }
        $("loginPass").value = "";
        hideLoginOverlay(data);
        await loadMetaAndDocs();
        await Promise.all([runSearch(1), loadFavorites()]);
        await checkAppVersion(false);
      } catch (_err) {
        $("loginError").textContent = "서버와 연결할 수 없습니다.";
        $("loginError").classList.remove("hidden");
      }
    });

    $("btnLogout").addEventListener("click", async () => {
      await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" }).catch(() => null);
      showLoginOverlay();
    });

    // Search & Filters
    $("searchForm").addEventListener("submit", (e) => {
      e.preventDefault();
      runSearch(1);
    });
    $("searchInput").addEventListener("input", updateClearButton);
    $("btnClearSearch").addEventListener("click", () => {
      $("searchInput").value = "";
      updateClearButton();
      runSearch(1);
    });
    $("scopeSelect").addEventListener("change", () => runSearch(1));
    $("pageTypeSelect").addEventListener("change", () => runSearch(1));
    $("btnBodyOnly").addEventListener("click", () => {
      state.bodyOnly = !state.bodyOnly;
      $("btnBodyOnly").classList.toggle("active", state.bodyOnly);
      $("btnBodyOnly").setAttribute("aria-pressed", String(state.bodyOnly));
      runSearch(1);
    });
    $("btnFavOnly").addEventListener("click", () => {
      state.favOnly = !state.favOnly;
      $("btnFavOnly").classList.toggle("active", state.favOnly);
      $("btnFavOnly").setAttribute("aria-pressed", String(state.favOnly));
      runSearch(1);
    });

    document.querySelectorAll(".btn-chip-add").forEach((btn) => {
      btn.addEventListener("click", () => appendKeywordOp(btn.dataset.op));
    });
    $("kwQuickInput").addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        appendKeywordOp("and");
      }
    });

    // Pagers
    $("btnGridPrev").addEventListener("click", () => runSearch(state.page - 1));
    $("btnSidePrev").addEventListener("click", () => runSearch(state.page - 1));
    $("btnGridNext").addEventListener("click", () => runSearch(state.page + 1));
    $("btnSideNext").addEventListener("click", () => runSearch(state.page + 1));

    // Sidebar Tabs
    document.querySelectorAll(".side-tab").forEach((btn) => {
      btn.addEventListener("click", () => switchSideTab(btn.dataset.tab));
    });

    // Viewer Controls
    $("btnPrevPage").addEventListener("click", () => {
      if (state.activePage?.siblings?.prev_page_id) {
        openPageDetail(state.activePage.siblings.prev_page_id, true);
      }
    });
    $("btnNextPage").addEventListener("click", () => {
      if (state.activePage?.siblings?.next_page_id) {
        openPageDetail(state.activePage.siblings.next_page_id, true);
      }
    });

    $("btnZoomFit").addEventListener("click", fitCanvas);
    $("btnZoom100").addEventListener("click", () => {
      const vp = $("canvasViewport").getBoundingClientRect();
      state.zoom = 1;
      const w = ($("previewImg").naturalWidth || 1000);
      state.panX = Math.max(12, (vp.width - w) / 2);
      state.panY = 12;
      applyTransform();
    });
    $("btnZoomIn").addEventListener("click", () => {
      const vp = $("canvasViewport").getBoundingClientRect();
      zoomAtPoint(1.25, vp.left + vp.width / 2, vp.top + vp.height / 2);
    });
    $("btnZoomOut").addEventListener("click", () => {
      const vp = $("canvasViewport").getBoundingClientRect();
      zoomAtPoint(0.8, vp.left + vp.width / 2, vp.top + vp.height / 2);
    });

    $("btnToggleBoxes").addEventListener("click", () => {
      state.showBoxes = !state.showBoxes;
      $("btnToggleBoxes").classList.toggle("active", state.showBoxes);
      $("btnToggleBoxes").setAttribute("aria-pressed", String(state.showBoxes));
      renderHighlightBoxes();
    });
    $("btnNextBox").addEventListener("click", jumpToNextHighlightBox);

    $("btnToggleText").addEventListener("click", () => {
      state.showText = !state.showText;
      $("textInspector").classList.toggle("hidden", !state.showText);
      $("btnToggleText").classList.toggle("active", state.showText);
      $("btnToggleText").setAttribute("aria-pressed", String(state.showText));
      requestAnimationFrame(() => fitCanvas());
    });
    $("btnCloseText").addEventListener("click", () => {
      state.showText = false;
      $("textInspector").classList.add("hidden");
      $("btnToggleText").classList.remove("active");
      $("btnToggleText").setAttribute("aria-pressed", "false");
      requestAnimationFrame(() => fitCanvas());
    });

    $("btnCopyText").addEventListener("click", async () => {
      const txt = state.activePage?.clean_text || "";
      if (!txt) return;
      try {
        await navigator.clipboard.writeText(txt);
        showToast("정제 텍스트를 클립보드에 복사했습니다.");
      } catch (_err) {
        showToast("클립보드 복사 권한을 확인하세요.");
      }
    });

    $("btnFavToggle").addEventListener("click", toggleFavorite);

    $("btnPinMode").addEventListener("click", () => {
      state.pinMode = !state.pinMode;
      $("btnPinMode").classList.toggle("active", state.pinMode);
      $("btnPinMode").setAttribute("aria-pressed", String(state.pinMode));
      $("canvasViewport").classList.toggle("pin-cursor", state.pinMode);
      if (state.pinMode) showToast("장표 위 원하는 지점을 클릭해 핀 메모를 등록하세요.");
    });

    $("pinForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!state.activePage || !state.pendingPinCoord) return;
      const note = $("pinNoteInput").value.trim();
      if (!note) return;
      const res = await apiFetch(`/api/pages/${encodeURIComponent(state.activePage.page_id)}/pins`, {
        method: "POST",
        body: JSON.stringify({ x: state.pendingPinCoord.x, y: state.pendingPinCoord.y, note })
      });
      if (res.ok) {
        closeDialog("pinDialog");
        state.pinMode = false;
        $("btnPinMode").classList.remove("active");
        $("canvasViewport").classList.remove("pin-cursor");
        showToast("핀 메모를 저장했습니다.");
        openPageDetail(state.activePage.page_id, false);
      }
    });

    // Admin Center
    $("btnAdminMenu").addEventListener("click", () => {
      openDialog("adminDialog");
      loadSyncStatus();
    });
    document.querySelectorAll(".admin-tab").forEach((btn) => {
      btn.addEventListener("click", () => {
        const target = btn.dataset.adminTab;
        document.querySelectorAll(".admin-tab").forEach((b) => b.classList.toggle("active", b === btn));
        $("adminSyncPane").classList.toggle("hidden", target !== "sync");
        $("adminUsersPane").classList.toggle("hidden", target !== "users");
        $("adminAuditPane").classList.toggle("hidden", target !== "audit");
        if (target === "sync") loadSyncStatus();
        if (target === "users") loadAdminUsers();
        if (target === "audit") loadAuditLogs();
      });
    });

    $("btnRefreshSync").addEventListener("click", loadSyncStatus);
    $("btnTriggerSync").addEventListener("click", async () => {
      const res = await apiFetch("/api/admin/sync/trigger", { method: "POST" });
      const data = await res.json().catch(() => ({}));
      showToast(data.status === "already_running" ? "동기화가 이미 실행 중입니다." : "Google Drive 동기화를 시작했습니다.");
      loadSyncStatus();
    });
    $("btnTestAlert").addEventListener("click", async () => {
      const res = await apiFetch("/api/admin/sync/test-alert", { method: "POST" });
      showToast(res.ok ? "n8n 테스트 웹훅을 발송했습니다." : "n8n 알림 발송 실패 또는 미설정");
    });

    $("createUserForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const res = await apiFetch("/api/admin/users", {
        method: "POST",
        body: JSON.stringify({
          username: $("newUsername").value.trim(),
          password: $("newPassword").value,
          role: $("newUserRole").value
        })
      });
      if (res.ok) {
        $("newUsername").value = "";
        $("newPassword").value = "";
        showToast("사용자를 추가했습니다.");
        loadAdminUsers();
      } else {
        const d = await res.json().catch(() => ({}));
        showToast(d.detail || "사용자 추가 실패");
      }
    });

    $("auditActionFilter").addEventListener("change", loadAuditLogs);
    $("btnRefreshAudit").addEventListener("click", loadAuditLogs);

    // Mobile Lifecycle + BFCache + Online Sync (Rule 5 & Rule 9)
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") checkLifecycleAndDate();
    });
    window.addEventListener("pageshow", checkLifecycleAndDate);
    window.addEventListener("focus", checkLifecycleAndDate);
    window.addEventListener("online", () => {
      if (state.user) {
        loadMetaAndDocs().catch(() => null);
      } else {
        bootstrapSession();
      }
    });
    setInterval(checkLifecycleAndDate, 120_000);
  }

  // ── Init ──
  initTheme();
  setupDialogs();
  setupPWA();
  setupCanvasGestures();
  bindEvents();
  bootstrapSession();
})();
