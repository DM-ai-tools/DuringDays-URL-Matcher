const state = {
  fileId: null,
  bounds: null,
  jobId: null,
  info: null,
  pollTimer: null,
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

function showView(name) {
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === name));
}

$$(".tab").forEach((t) => t.addEventListener("click", () => showView(t.dataset.view)));
$("#go-match")?.addEventListener("click", () => showView("match"));

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  const isJson = (res.headers.get("content-type") || "").includes("application/json");
  const data = isJson ? await res.json().catch(() => ({})) : {};
  if (!res.ok) {
    let msg = data.message || res.statusText;
    if (typeof data.detail === "string") msg = data.detail;
    else if (Array.isArray(data.detail)) msg = data.detail.map((d) => d.msg || JSON.stringify(d)).join("; ");
    throw new Error(msg);
  }
  return data;
}

function fmtNum(n) {
  return (n ?? 0).toLocaleString();
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function loadDashboard() {
  const data = await api("/api/dashboard");
  const s = data.stats || {};
  $("#dash-stats").innerHTML = [
    ["Jobs", s.total_jobs],
    ["Completed", s.done_jobs],
    ["Sources", s.sources],
    ["URLs indexed", s.product_urls_indexed],
  ]
    .map(([label, val]) => `<div class="stat glass"><b>${fmtNum(val)}</b><span>${label}</span></div>`)
    .join("");

  $("#dash-jobs").innerHTML =
    (data.jobs || [])
      .slice(0, 12)
      .map((j) => {
        const meta = j.meta || {};
        const detail = [
          meta.filename || meta.name || meta.index_url || "",
          meta.start_row ? `rows ${meta.start_row}–${meta.end_row}` : "",
        ]
          .filter(Boolean)
          .join(" · ");
        const actions =
          j.status === "done" && j.kind === "match" && j.result_path
            ? `<div class="item-actions">
                <span class="pill ${j.status}">${j.status}</span>
                <button type="button" class="glass-btn ghost sm" data-open-job="${j.id}">Open result</button>
              </div>`
            : `<span class="pill ${j.status}">${j.status}</span>`;
        return `<div class="item">
        <div>
          <b>${escapeHtml(j.kind)} · ${escapeHtml(j.id)}</b>
          <small>${escapeHtml(detail)}</small>
        </div>
        ${actions}
      </div>`;
      })
      .join("") || `<p class="empty">No jobs yet — start one from Match.</p>`;

  $$("[data-open-job]").forEach((btn) => {
    btn.addEventListener("click", () => openCompletedJob(btn.dataset.openJob));
  });

  $("#dash-sources").innerHTML =
    (data.sources || [])
      .map(
        (s) => `<div class="item">
      <div><b>${escapeHtml(s.name || s.id)}</b><small>${escapeHtml(s.index_url || "")}</small></div>
      <span class="pill">${fmtNum(s.product_urls)} URLs</span>
    </div>`
      )
      .join("") || `<p class="empty">No catalogues yet.</p>`;

  await fillSourcesSelect(data.sources || []);
  renderSourcesList(data.sources || []);
}

async function fillSourcesSelect(sources) {
  const sel = $("#site-select");
  sel.innerHTML = sources
    .map((s) => `<option value="${s.id}">${s.name || s.id} (${fmtNum(s.product_urls)} URLs)</option>`)
    .join("");
  if ([...sel.options].some((o) => o.value === "bigw")) sel.value = "bigw";
}

function renderSourcesList(sources) {
  $("#sources-list").innerHTML =
    sources
      .map((s) => {
        const cleaned = s.cleaned_data;
        const cleanedLine = cleaned
          ? `<small>${fmtNum(cleaned.brands)} brands · ${fmtNum(cleaned.categories)} categories · ${fmtNum(cleaned.collections)} collections</small>`
          : "";
        return `<div class="item">
      <div><b>${escapeHtml(s.name || s.id)}${s.builtin ? " · built-in" : ""}</b>
      <small>${escapeHtml(s.index_url || "")}</small>
      ${cleanedLine}</div>
      <span class="pill">${fmtNum(s.product_urls)} URLs</span>
    </div>`;
      })
      .join("") || `<p class="empty">Crawl a sitemap to add one.</p>`;
}

function updateWorkingHint(info) {
  const hint = $("#working-hint");
  const resetBtn = $("#btn-reset-working");
  if (!hint || !resetBtn) return;
  if (info?.has_working_copy) {
    hint.hidden = false;
    hint.textContent =
      "This upload has saved row updates. New runs add to or overwrite rows in the same file — download includes all matched rows so far.";
    resetBtn.hidden = false;
  } else {
    hint.hidden = true;
    resetBtn.hidden = true;
  }
}

function applyBounds(info) {
  state.bounds = { min: info.min_row, max: info.max_row, data_rows: info.data_rows };
  const start = $("#start-row");
  const end = $("#end-row");
  start.min = info.min_row;
  start.max = info.max_row;
  end.min = info.min_row;
  end.max = info.max_row;
  start.value = info.min_row;
  end.value = Math.min(info.min_row + 49, info.max_row);
  $("#bounds-hint").textContent =
    `Valid Excel rows: ${info.min_row}–${info.max_row} (${info.data_rows} product rows). Each run updates only the selected range; other matched rows are kept.`;

  const sheetSel = $("#sheet-select");
  sheetSel.innerHTML = (info.sheets || [info.sheet])
    .map((s) => `<option value="${s}" ${s === info.sheet ? "selected" : ""}>${s}</option>`)
    .join("");

  const missing = info.missing_bigw_columns || [];
  const banner = $("#missing-cols");
  if (missing.length) {
    banner.hidden = false;
    const titleNote =
      missing.includes("Title") && info.has_url_column
        ? " Title will be derived from the product URL column."
        : "";
    banner.textContent = `Missing columns will be auto-added: ${missing.join(", ")}.${titleNote}`;
  } else {
    banner.hidden = true;
  }
  updateWorkingHint(info);
  $("#btn-start").disabled = !state.fileId;
}

function clampRange() {
  if (!state.bounds) return { start: +$("#start-row").value, end: +$("#end-row").value };
  let start = +$("#start-row").value;
  let end = +$("#end-row").value;
  start = Math.max(state.bounds.min, Math.min(start, state.bounds.max));
  end = Math.max(state.bounds.min, Math.min(end, state.bounds.max));
  if (start > end) [start, end] = [end, start];
  $("#start-row").value = start;
  $("#end-row").value = end;
  return { start, end };
}

["start-row", "end-row"].forEach((id) => {
  $(`#${id}`).addEventListener("change", clampRange);
});

async function handleFile(file) {
  const fd = new FormData();
  fd.append("file", file);
  $("#upload-meta").textContent = "Uploading…";
  const info = await api("/api/upload", { method: "POST", body: fd });
  state.fileId = info.file_id;
  state.info = info;
  $("#upload-meta").innerHTML = `<strong>${escapeHtml(info.filename)}</strong> · ${escapeHtml(info.sheet)} · ${fmtNum(info.data_rows)} rows`;
  applyBounds(info);
}

const drop = $("#dropzone");
$("#file-input").addEventListener("change", (e) => {
  if (e.target.files?.[0]) handleFile(e.target.files[0]).catch(alert);
});
drop.addEventListener("click", () => $("#file-input").click());
drop.addEventListener("dragover", (e) => {
  e.preventDefault();
  drop.classList.add("drag");
});
drop.addEventListener("dragleave", () => drop.classList.remove("drag"));
drop.addEventListener("drop", (e) => {
  e.preventDefault();
  drop.classList.remove("drag");
  const f = e.dataTransfer.files?.[0];
  if (f) handleFile(f).catch(alert);
});

$("#btn-start").addEventListener("click", async () => {
  try {
    const { start, end } = clampRange();
    const body = {
      file_id: state.fileId,
      start_row: start,
      end_row: end,
      site_id: $("#site-select").value || "bigw",
      sheet: $("#sheet-select").value || undefined,
    };
    const res = await api("/api/match", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    state.jobId = res.job_id;
    $("#progress-card").hidden = false;
    $("#result-card").hidden = true;
    $("#preview-wrap").hidden = true;
    $("#event-log").innerHTML = "";
    $("#bar-fill").style.width = "0%";
    $("#counters").innerHTML = "";
    $("#job-status").textContent = "running";
    $("#job-status").className = "pill running";
    $("#row-live").hidden = false;
    $("#row-live-num").textContent = String(res.start_row);
    $("#progress-card").scrollIntoView({ behavior: "smooth", block: "nearest" });
    if (res.clamped) logLine(`Range clamped to ${res.start_row}–${res.end_row}`);
    if (res.resumed_from_previous) {
      logLine("Continuing from previous updates on this file");
    }
    watchJob(res.job_id);
  } catch (err) {
    alert(err.message);
  }
});

function logLine(msg) {
  const el = document.createElement("div");
  el.textContent = msg;
  const log = $("#event-log");
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
}

function renderCounters(c) {
  if (!c) return;
  $("#counters").innerHTML = Object.entries(c)
    .map(([k, v]) => `<span>${escapeHtml(k)} <b>${fmtNum(v)}</b></span>`)
    .join("");
}

function setLiveRow(n) {
  if (n == null) return;
  $("#row-live").hidden = false;
  $("#row-live-num").textContent = String(n);
}

function clearPoll() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

async function showResultUI(jobId, counters) {
  state.jobId = jobId;
  $("#result-card").hidden = false;
  const dl = $("#btn-download");
  dl.href = `/api/jobs/${jobId}/download`;
  dl.setAttribute("download", "");
  const c = counters || {};
  const parts = Object.entries(c).map(([k, v]) => `${k}: ${v}`);
  $("#result-summary").textContent = parts.length
    ? `${parts.join(" · ")}. Download includes all matched rows saved for this upload.`
    : "Download includes all matched rows saved for this upload.";
  $("#job-status").textContent = "done";
  $("#job-status").className = "pill done";
  $("#progress-msg").textContent = "Matching complete — preview & download below.";
  $("#bar-fill").style.width = "100%";
  $("#result-card").scrollIntoView({ behavior: "smooth", block: "nearest" });
  try {
    await loadPreview(jobId);
  } catch (err) {
    logLine(`Preview deferred: ${err.message}`);
  }
  if (state.fileId) {
    api(`/api/files/${encodeURIComponent(state.fileId)}/inspect`)
      .then((info) => {
        state.info = info;
        updateWorkingHint(info);
      })
      .catch(() => {});
  }
  loadDashboard().catch(console.error);
}

async function loadPreview(jobId) {
  const data = await api(`/api/jobs/${jobId}/preview?limit=50`);
  const table = $("#preview-table");
  const head = `<tr>${data.headers.map((h) => `<th>${escapeHtml(h)}</th>`).join("")}</tr>`;
  const body = data.rows
    .map((r) => `<tr>${r.cells.map((c) => `<td title="${escapeHtml(c)}">${escapeHtml(c)}</td>`).join("")}</tr>`)
    .join("");
  table.innerHTML = head + body;
  $("#preview-wrap").hidden = false;
  $("#preview-label").textContent = data.truncated
    ? `Showing first ${data.rows.length} matched rows (truncated)`
    : `Showing ${data.rows.length} matched rows`;
}

async function openCompletedJob(jobId) {
  showView("match");
  const job = await api(`/api/jobs/${jobId}`);
  if (job.status !== "done" || !job.result_path) {
    alert("Result is not ready for this job.");
    return;
  }
  $("#progress-card").hidden = false;
  await showResultUI(jobId, job.counters);
}

function watchJob(jobId) {
  clearPoll();
  let finished = false;

  const finish = async (data) => {
    if (finished) return;
    finished = true;
    clearPoll();
    if (data.status === "done") {
      await showResultUI(jobId, data.counters);
    } else {
      $("#job-status").textContent = "error";
      $("#job-status").className = "pill error";
      $("#progress-msg").textContent = data.error || "Job failed";
    }
  };

  const es = new EventSource(`/api/jobs/${jobId}/events`);
  es.onmessage = (ev) => {
    const data = JSON.parse(ev.data);
    if (data.message) logLine(data.message);
    if (typeof data.progress === "number") $("#bar-fill").style.width = `${data.progress}%`;
    if (data.counters) renderCounters(data.counters);
    if (data.current_row != null) setLiveRow(data.current_row);
    if (data.status && data.status !== "done") {
      $("#job-status").textContent = data.status;
      $("#job-status").className = `pill ${data.status}`;
    }
    if (!data.final) {
      $("#progress-msg").textContent = data.message || "Working…";
    }
    if (data.final) {
      es.close();
      finish(data);
    }
  };
  es.onerror = () => {
    // Keep polling — SSE can drop on long jobs.
  };

  // Fallback poll so preview/download always appear even if SSE stalls.
  state.pollTimer = setInterval(async () => {
    try {
      const job = await api(`/api/jobs/${jobId}`);
      if (typeof job.progress === "number") $("#bar-fill").style.width = `${job.progress}%`;
      if (job.counters) renderCounters(job.counters);
      if (job.current_row != null) setLiveRow(job.current_row);
      if (job.message) $("#progress-msg").textContent = job.message;
      if (job.status === "done" && job.result_path) {
        es.close();
        finish(job);
      } else if (job.status === "error") {
        es.close();
        finish(job);
      }
    } catch (_) {}
  }, 1500);
}

$("#btn-preview").addEventListener("click", async () => {
  if (!state.jobId) return;
  try {
    await loadPreview(state.jobId);
    $("#preview-wrap").scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (err) {
    alert(err.message);
  }
});

$("#btn-preview-refresh")?.addEventListener("click", async () => {
  if (!state.jobId) return;
  try {
    await loadPreview(state.jobId);
  } catch (err) {
    alert(err.message);
  }
});

$("#btn-download").addEventListener("click", (e) => {
  if (!$("#btn-download").getAttribute("href") || $("#btn-download").getAttribute("href") === "#") {
    e.preventDefault();
    alert("Download is not ready yet.");
  }
});

$("#btn-crawl").addEventListener("click", async () => {
  const name = $("#src-name").value.trim();
  const index_url = $("#src-url").value.trim();
  const product_pattern = $("#src-pattern").value.trim() || null;
  if (!name || !index_url) return alert("Name and sitemap URL are required");
  try {
    $("#crawl-status").textContent = "Starting crawl…";
    const res = await api("/api/sources/crawl", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, index_url, product_pattern }),
    });
    watchCrawl(res.job_id, "#crawl-status");
  } catch (err) {
    alert(err.message);
  }
});

function watchCrawl(jobId, statusSel = "#crawl-status") {
  const es = new EventSource(`/api/jobs/${jobId}/events`);
  es.onmessage = (ev) => {
    const data = JSON.parse(ev.data);
    if (data.message) $(statusSel).textContent = data.message;
    if (data.final) {
      es.close();
      loadDashboard().catch(console.error);
      loadStoredData().catch(console.error);
      if (data.status === "error") alert(data.error || "Job failed");
    }
  };
}

function bulkTargetName() {
  const t = $("#bulk-target").value;
  if (t === "custom") {
    const n = $("#bulk-custom-name").value.trim();
    if (!n) throw new Error("Enter a custom catalogue name");
    return n;
  }
  return t;
}

$("#bulk-target")?.addEventListener("change", () => {
  const custom = $("#bulk-target").value === "custom";
  $("#bulk-custom-wrap").hidden = !custom;
});

$("#btn-bulk-preview")?.addEventListener("click", async () => {
  try {
    const text = $("#bulk-text").value;
    if (!text.trim()) return alert("Paste a URL dump first (or upload a file into the text area)");
    const name = bulkTargetName();
    $("#bulk-status").textContent = "Cleaning preview…";
    const data = await api("/api/sources/bulk/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text,
        name,
        product_pattern: $("#bulk-pattern").value.trim() || null,
      }),
    });
    $("#bulk-status").textContent = `Preview: ${fmtNum(data.unique)} product URLs` +
      (data.brand_count ? `, ${fmtNum(data.brand_count)} brands` : "");
    const box = $("#bulk-preview");
    box.hidden = false;
    const cleaned = data.cleaned_counts || {};
    const cleanedParts = [];
    if (cleaned.brands) cleanedParts.push(`${fmtNum(cleaned.brands)} brands`);
    if (cleaned.categories) cleanedParts.push(`${fmtNum(cleaned.categories)} categories`);
    if (cleaned.brand_categories) cleanedParts.push(`${fmtNum(cleaned.brand_categories)} brand-categories`);
    if (cleaned.collections) cleanedParts.push(`${fmtNum(cleaned.collections)} collections`);
    if (cleaned.brand_pages) cleanedParts.push(`${fmtNum(cleaned.brand_pages)} brand pages`);
    const cleanedLine = cleanedParts.length
      ? `<p class="hint">Will store: ${cleanedParts.join(" · ")}</p>`
      : "";
    box.innerHTML = `<strong>${fmtNum(data.unique)}</strong> unique product URLs cleaned
      ${data.brand_count ? `<br><strong>${fmtNum(data.brand_count)}</strong> brand slugs extracted` : ""}
      ${cleanedLine}
      ${data.samples?.length ? `<ol>${data.samples.map((u) => `<li>${escapeHtml(u)}</li>`).join("")}</ol>` : ""}
      ${data.brand_samples?.length ? `<p>Brand samples: ${data.brand_samples.map((b) => `<code>${escapeHtml(b)}</code>`).join(", ")}</p>` : ""}`;
  } catch (err) {
    alert(err.message);
  }
});

$("#btn-bulk-store")?.addEventListener("click", async () => {
  try {
    const name = bulkTargetName();
    const merge = $("#bulk-merge").value !== "false";
    const product_pattern = $("#bulk-pattern").value.trim() || null;
    const file = $("#bulk-file").files?.[0];
    $("#bulk-status").textContent = "Starting clean & store…";
    $("#bulk-preview").hidden = true;

    let res;
    if (file && !$("#bulk-text").value.trim()) {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("name", name);
      fd.append("merge", String(merge));
      if (product_pattern) fd.append("product_pattern", product_pattern);
      res = await api("/api/sources/bulk/upload", { method: "POST", body: fd });
    } else {
      const text = $("#bulk-text").value;
      if (!text.trim() && !file) return alert("Paste URLs or choose a file");
      let payloadText = text;
      if (file && !text.trim()) {
        payloadText = await file.text();
      }
      res = await api("/api/sources/bulk", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: payloadText, name, merge, product_pattern }),
      });
    }
    watchCrawl(res.job_id, "#bulk-status");
  } catch (err) {
    alert(err.message);
  }
});

$("#bulk-file")?.addEventListener("change", async (e) => {
  const f = e.target.files?.[0];
  if (!f) return;
  try {
    const text = await f.text();
    if (!$("#bulk-text").value.trim()) $("#bulk-text").value = text.slice(0, 500000);
    $("#bulk-status").textContent = `Loaded ${f.name} (${fmtNum(f.size)} bytes) — preview or store`;
  } catch (_) {
    $("#bulk-status").textContent = `Selected ${f.name} — will upload on store`;
  }
});

$("#refresh-dash").addEventListener("click", () => loadDashboard().catch(alert));

const storedState = { type: "products", offset: 0, limit: 50, q: "", total: 0 };

const STORED_LABELS = {
  products: "Products",
  brands: "Brands",
  categories: "Categories",
  brand_categories: "Brand categories",
  collections: "Collections",
  brand_pages: "Brand pages",
};

function renderStoredStats(counts) {
  const el = $("#stored-stats");
  if (!el || !counts) return;
  el.innerHTML = Object.entries(STORED_LABELS)
    .map(([key, label]) => `<span class="stored-stat"><b>${fmtNum(counts[key] || 0)}</b>${label}</span>`)
    .join("");
}

function renderStoredTable(type, items) {
  const table = $("#stored-table");
  if (!table) return;
  if (!items.length) {
    table.innerHTML = `<tbody><tr><td class="empty">No ${escapeHtml(STORED_LABELS[type] || type).toLowerCase()} stored yet.</td></tr></tbody>`;
    return;
  }
  if (type === "brand_categories") {
    table.innerHTML = `<thead><tr><th>Brand</th><th>URL</th></tr></thead><tbody>${items
      .map(
        (row) => `<tr>
          <td><code>${escapeHtml(row.brand || "—")}</code></td>
          <td><a href="${escapeHtml(row.url)}" target="_blank" rel="noopener">${escapeHtml(row.url)}</a></td>
        </tr>`
      )
      .join("")}</tbody>`;
    return;
  }
  if (type === "brands") {
    table.innerHTML = `<thead><tr><th>Brand slug</th><th>Page</th></tr></thead><tbody>${items
      .map(
        (slug) => `<tr>
          <td><code>${escapeHtml(slug)}</code></td>
          <td><a href="https://www.kogan.com/au/${encodeURIComponent(slug)}/" target="_blank" rel="noopener">kogan.com/au/${escapeHtml(slug)}/</a></td>
        </tr>`
      )
      .join("")}</tbody>`;
    return;
  }
  table.innerHTML = `<thead><tr><th>URL</th></tr></thead><tbody>${items
    .map((url) => `<tr><td><a href="${escapeHtml(url)}" target="_blank" rel="noopener">${escapeHtml(url)}</a></td></tr>`)
    .join("")}</tbody>`;
}

async function loadStoredBrowse() {
  const params = new URLSearchParams({
    offset: String(storedState.offset),
    limit: String(storedState.limit),
  });
  if (storedState.q) params.set("q", storedState.q);
  const data = await api(`/api/sources/kogan/cleaned-data/${storedState.type}?${params}`);
  storedState.total = data.total || 0;
  renderStoredTable(storedState.type, data.items || []);
  const from = storedState.total ? storedState.offset + 1 : 0;
  const to = Math.min(storedState.offset + storedState.limit, storedState.total);
  $("#stored-meta").textContent = `${fmtNum(storedState.total)} ${STORED_LABELS[storedState.type] || storedState.type}`;
  $("#stored-page-info").textContent =
    storedState.total ? `Showing ${from}–${to} of ${fmtNum(storedState.total)}` : "No items";
  $("#stored-prev").disabled = storedState.offset <= 0;
  $("#stored-next").disabled = storedState.offset + storedState.limit >= storedState.total;
}

async function loadStoredData() {
  if (!$("#stored-data-panel")) return;
  const summary = await api("/api/sources/kogan/cleaned-data");
  renderStoredStats(summary.counts || {});
  await loadStoredBrowse();
}

$$(".stored-tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".stored-tab").forEach((b) => b.classList.toggle("active", b === btn));
    storedState.type = btn.dataset.type;
    storedState.offset = 0;
    loadStoredBrowse().catch(alert);
  });
});

let storedSearchTimer;
$("#stored-search")?.addEventListener("input", () => {
  clearTimeout(storedSearchTimer);
  storedSearchTimer = setTimeout(() => {
    storedState.q = $("#stored-search").value.trim();
    storedState.offset = 0;
    loadStoredBrowse().catch(console.error);
  }, 300);
});

$("#stored-prev")?.addEventListener("click", () => {
  storedState.offset = Math.max(0, storedState.offset - storedState.limit);
  loadStoredBrowse().catch(alert);
});
$("#stored-next")?.addEventListener("click", () => {
  if (storedState.offset + storedState.limit < storedState.total) {
    storedState.offset += storedState.limit;
    loadStoredBrowse().catch(alert);
  }
});
$("#btn-stored-refresh")?.addEventListener("click", () => loadStoredData().catch(alert));

$$(".tab").forEach((t) => {
  t.addEventListener("click", () => {
    if (t.dataset.view === "sources") loadStoredData().catch(console.error);
  });
});

$("#btn-reset-working")?.addEventListener("click", async () => {
  if (!state.fileId) return;
  if (!confirm("Discard all saved row updates for this upload and start fresh?")) return;
  try {
    await api(`/api/files/${encodeURIComponent(state.fileId)}/working`, { method: "DELETE" });
    const info = await api(`/api/files/${encodeURIComponent(state.fileId)}/inspect`);
    state.info = info;
    applyBounds(info);
  } catch (err) {
    alert(err.message);
  }
});

loadDashboard().catch(console.error);
loadStoredData().catch(console.error);
