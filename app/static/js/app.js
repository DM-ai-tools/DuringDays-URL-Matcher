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
      .map(
        (s) => `<div class="item">
      <div><b>${escapeHtml(s.name || s.id)}${s.builtin ? " · built-in" : ""}</b>
      <small>${escapeHtml(s.index_url || "")}</small></div>
      <span class="pill">${fmtNum(s.product_urls)} URLs</span>
    </div>`
      )
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
    banner.textContent = `Missing columns will be auto-added: ${missing.join(", ")}`;
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
    watchCrawl(res.job_id);
  } catch (err) {
    alert(err.message);
  }
});

function watchCrawl(jobId) {
  const es = new EventSource(`/api/jobs/${jobId}/events`);
  es.onmessage = (ev) => {
    const data = JSON.parse(ev.data);
    if (data.message) $("#crawl-status").textContent = data.message;
    if (data.final) {
      es.close();
      loadDashboard().catch(console.error);
      if (data.status === "error") alert(data.error || "Crawl failed");
    }
  };
}

$("#refresh-dash").addEventListener("click", () => loadDashboard().catch(alert));

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
