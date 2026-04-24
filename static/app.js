// Aim demo frontend — fetch + 2 s polling against the FastAPI surface.
// Matches the curl flow in CLAUDE.md: POST /aim → POST /aim/{id}/digest?mode=… → GET /digest/{id}.

const USER_ID = "demo";  // auth out of scope (DECISIONS D15) — trust-the-client.
const POLL_MS = 2000;

const $ = (sel) => document.querySelector(sel);
const formCard = $("#aim-form-card");
const form = $("#aim-form");
const formTitle = $("#aim-form-title");
const submitBtn = $("#aim-form-submit");
const cancelBtn = $("#aim-form-cancel");
const feedback = $("#aim-form-feedback");
const newAimBtn = $("#new-aim-btn");
const refreshBtn = $("#refresh-btn");
const aimsList = $("#aims-list");
const digestView = $("#digest-view");
const digestMetaLine = $("#digest-meta-line");

// Per-aim live state — drives row status pill while a digest is generating.
const aimState = new Map(); // aim_id → { jobId, status, mode, startedAt }
let activeDigestId = null;

// ---------- Aim form (create + edit) ----------

newAimBtn.addEventListener("click", () => openForm(null));
cancelBtn.addEventListener("click", closeForm);

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const editingId = $("#f-aim-id").value;
  const body = {
    user_id: USER_ID,
    title: $("#f-title").value.trim(),
    summary: splitLines($("#f-summary").value),
    monitored_entities: splitCsv($("#f-entities").value),
    regions: splitCsv($("#f-regions").value),
    update_types: splitCsv($("#f-updates").value),
  };
  if (!body.title || !body.summary.length || !body.regions.length) {
    setFeedback("Title, at least one summary bullet, and a region are required.", "error");
    return;
  }
  setFeedback("Saving…");
  submitBtn.disabled = true;
  try {
    const url = editingId ? `/aim/${encodeURIComponent(editingId)}` : "/aim";
    const method = editingId ? "PUT" : "POST";
    const res = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`${method} ${url} → ${res.status}`);
    setFeedback(editingId ? "Aim updated" : "Aim created", "ok");
    closeForm();
    await loadAims();
  } catch (err) {
    setFeedback(`Save failed: ${err.message}`, "error");
  } finally {
    submitBtn.disabled = false;
  }
});

function openForm(aim) {
  formCard.classList.remove("collapsed");
  setFeedback("");
  if (aim) {
    formTitle.textContent = `Edit Aim — ${aim.title}`;
    submitBtn.textContent = "Save changes";
    $("#f-aim-id").value = aim.aim_id;
    $("#f-title").value = aim.title || "";
    $("#f-summary").value = (aim.summary || []).join("\n");
    $("#f-entities").value = (aim.monitored_entities || []).join(", ");
    $("#f-regions").value = (aim.regions || []).join(", ");
    $("#f-updates").value = (aim.update_types || []).join(", ");
  } else {
    formTitle.textContent = "Create Aim";
    submitBtn.textContent = "Create Aim";
    form.reset();
    $("#f-aim-id").value = "";
  }
  formCard.scrollIntoView({ behavior: "smooth", block: "start" });
}

function closeForm() {
  formCard.classList.add("collapsed");
  form.reset();
  $("#f-aim-id").value = "";
  setFeedback("");
}

function setFeedback(msg, kind = "") {
  feedback.textContent = msg || "";
  feedback.className = "aim-form__feedback" + (kind ? ` is-${kind}` : "");
}

// ---------- Aim list ----------

refreshBtn.addEventListener("click", loadAims);

async function loadAims() {
  aimsList.innerHTML = `<div class="empty-state"><span class="spinner"></span>Loading Aims…</div>`;
  let aims = [];
  try {
    const res = await fetch(`/aims?user_id=${encodeURIComponent(USER_ID)}`);
    if (!res.ok) throw new Error(`GET /aims → ${res.status}`);
    aims = await res.json();
  } catch (err) {
    aimsList.innerHTML = `<div class="alert alert--error">Failed to load Aims: ${esc(err.message)}</div>`;
    return;
  }
  if (!aims.length) {
    aimsList.innerHTML = `
      <div class="empty-state">
        <strong>No Aims yet</strong>
        Click <em>+ New Aim</em> above to create your first monitoring config.
      </div>`;
    return;
  }
  aimsList.innerHTML = "";
  for (const aim of aims) aimsList.appendChild(renderAimRow(aim));
}

function renderAimRow(aim) {
  const row = document.createElement("article");
  row.className = "aim-row";
  row.dataset.aimId = aim.aim_id;
  const summary = (aim.summary || []).map((b) => `<li>${esc(b)}</li>`).join("");
  const chips = [
    ...((aim.monitored_entities || []).map((x) => chip(x, "entity", "Entity"))),
    ...((aim.regions || []).map((x) => chip(x, "region", "Region"))),
    ...((aim.update_types || []).map((x) => chip(x, "update", "Update"))),
  ].join("");
  row.innerHTML = `
    <div class="aim-row__head">
      <h3 class="aim-row__title">${esc(aim.title)}</h3>
      <div class="aim-row__actions">
        <button class="icon-btn" type="button" data-action="edit" aria-label="Edit Aim" title="Edit">
          <svg aria-hidden="true"><use href="#i-pen"/></svg>
        </button>
        <button class="icon-btn icon-btn--danger" type="button" data-action="delete" aria-label="Delete Aim" title="Delete">
          <svg aria-hidden="true"><use href="#i-trash"/></svg>
        </button>
      </div>
    </div>
    <ul class="aim-row__summary">${summary}</ul>
    <div class="aim-row__chips">${chips}</div>
    <div class="aim-row__generate">
      <label for="mode-${aim.aim_id}">Mode</label>
      <select id="mode-${aim.aim_id}" data-action="mode">
        <option value="incremental" selected>Incremental — dedup-aware</option>
        <option value="force">Force — re-ingest</option>
        <option value="cached">Cached — skip ingest (&lt;10 s)</option>
      </select>
      <button class="btn btn--primary" type="button" data-action="generate">Generate Digest</button>
      <span class="aim-row__status" data-role="status"></span>
    </div>
    <details class="aim-row__history" data-role="history-wrap">
      <summary>History</summary>
      <div class="aim-history" data-role="history"><span class="empty-state">Click to load…</span></div>
    </details>
  `;
  row.querySelector('[data-action="edit"]').addEventListener("click", () => openForm(aim));
  row.querySelector('[data-action="delete"]').addEventListener("click", () => deleteAim(aim));
  row.querySelector('[data-action="generate"]').addEventListener("click", () => {
    const mode = row.querySelector('[data-action="mode"]').value;
    generateDigest(aim, mode, row);
  });
  const details = row.querySelector('[data-role="history-wrap"]');
  details.addEventListener("toggle", () => {
    if (details.open) loadHistory(aim, row);
  });
  refreshRowStatus(row, aim.aim_id);
  return row;
}

async function loadHistory(aim, row) {
  const host = row.querySelector('[data-role="history"]');
  host.innerHTML = `<span class="empty-state"><span class="spinner"></span>Loading history…</span>`;
  let items = [];
  try {
    const res = await fetch(`/aim/${encodeURIComponent(aim.aim_id)}/digests`);
    if (!res.ok) throw new Error(`GET /aim/.../digests → ${res.status}`);
    items = await res.json();
  } catch (err) {
    host.innerHTML = `<div class="alert alert--error">Failed to load history: ${esc(err.message)}</div>`;
    return;
  }
  if (!items.length) {
    host.innerHTML = `<span class="empty-state">No past digests yet.</span>`;
    return;
  }
  host.innerHTML = items.map((d) => {
    const when = d.generated_at ? new Date(d.generated_at).toLocaleString() : "(unknown)";
    const meta = [
      d.mode ? `<code>${esc(d.mode)}</code>` : "",
      d.items != null ? `${d.items} items` : "",
      d.sections != null ? `${d.sections} sections` : "",
      d.date_range ? esc(d.date_range) : "",
    ].filter(Boolean).join(" · ");
    return `
      <button class="aim-history__item" type="button" data-digest-id="${esc(d.digest_id)}">
        <div class="aim-history__head">
          <span class="aim-history__when">${esc(when)}</span>
          <span class="aim-history__headline">${esc(d.headline || "(no headline)")}</span>
        </div>
        <div class="aim-history__meta">${meta}</div>
      </button>`;
  }).join("");
  host.querySelectorAll("[data-digest-id]").forEach((btn) => {
    btn.addEventListener("click", () => openHistoryDigest(aim, btn.dataset.digestId));
  });
}

async function openHistoryDigest(aim, digestId) {
  activeDigestId = digestId;  // stops any in-flight poll from clobbering the view
  digestMetaLine.textContent = `${aim.title} — history`;
  digestView.innerHTML = `
    <div class="card" style="display:flex; align-items:center; gap:12px;">
      <span class="spinner"></span><span>Loading digest…</span>
    </div>`;
  try {
    const res = await fetch(`/digest/${encodeURIComponent(digestId)}`);
    if (!res.ok) throw new Error(`GET /digest → ${res.status}`);
    const d = await res.json();
    renderDigest(aim, d, d.mode || "history");
  } catch (err) {
    digestView.innerHTML = `<div class="alert alert--error">Failed to load digest: ${esc(err.message)}</div>`;
  }
}

function chip(text, kind, label) {
  return `<span class="chip chip--type-${kind}"><span class="chip__label">${esc(label)}</span>${esc(text)}</span>`;
}

async function deleteAim(aim) {
  if (!confirm(`Delete aim "${aim.title}"?`)) return;
  try {
    const res = await fetch(`/aim/${encodeURIComponent(aim.aim_id)}`, { method: "DELETE" });
    if (res.status !== 204) throw new Error(`DELETE → ${res.status}`);
    aimState.delete(aim.aim_id);
    if ($("#f-aim-id").value === aim.aim_id) closeForm();
    await loadAims();
  } catch (err) {
    alert(`Delete failed: ${err.message}`);
  }
}

// ---------- Digest trigger + polling ----------

async function generateDigest(aim, mode, row) {
  setRowStatus(row, "running", `${mode} · queueing…`);
  digestMetaLine.textContent = `${aim.title} — ${mode}`;
  digestView.innerHTML = `
    <div class="card" style="display:flex; align-items:center; gap:12px;">
      <span class="spinner"></span>
      <span>Pipeline running for <strong>${esc(aim.title)}</strong> in <code>${esc(mode)}</code> mode…</span>
    </div>`;
  let jobId;
  try {
    const res = await fetch(
      `/aim/${encodeURIComponent(aim.aim_id)}/digest?mode=${encodeURIComponent(mode)}`,
      { method: "POST" },
    );
    if (!res.ok) throw new Error(`POST /digest → ${res.status}`);
    const data = await res.json();
    jobId = data.job_id || data.digest_id;
  } catch (err) {
    setRowStatus(row, "error", `Failed: ${err.message}`);
    digestView.innerHTML = `<div class="alert alert--error">Failed to start pipeline: ${esc(err.message)}</div>`;
    return;
  }
  aimState.set(aim.aim_id, { jobId, status: "queued", mode, startedAt: Date.now() });
  activeDigestId = jobId;
  pollDigest(aim, jobId, row, mode);
}

async function pollDigest(aim, jobId, row, mode) {
  while (true) {
    if (activeDigestId !== jobId) return;  // user kicked off a newer run
    let data;
    try {
      const res = await fetch(`/digest/${encodeURIComponent(jobId)}`);
      if (!res.ok) throw new Error(`GET /digest → ${res.status}`);
      data = await res.json();
    } catch (err) {
      setRowStatus(row, "error", `Poll failed: ${err.message}`);
      digestView.innerHTML = `<div class="alert alert--error">Lost the digest job: ${esc(err.message)}</div>`;
      return;
    }
    const isComplete = data.status === "complete" || (Array.isArray(data.sections) && data.sections.length >= 0 && data.headline !== undefined);
    if (data.status === "failed") {
      setRowStatus(row, "error", "Pipeline failed");
      const err = data?.funnel?.error || "Unknown error — check server logs";
      digestView.innerHTML = `
        <div class="alert alert--error">
          <strong>Pipeline failed.</strong> ${esc(err)}
        </div>`;
      aimState.set(aim.aim_id, { jobId, status: "failed", mode });
      return;
    }
    if (isComplete) {
      setRowStatus(row, "ok", `${mode} · done in ${secondsSince(aimState.get(aim.aim_id)?.startedAt)}s`);
      aimState.set(aim.aim_id, { jobId, status: "complete", mode });
      renderDigest(aim, data, mode);
      const details = row?.querySelector('[data-role="history-wrap"]');
      if (details?.open) loadHistory(aim, row);
      return;
    }
    setRowStatus(row, "running", `${mode} · ${data.status || "queued"}…`);
    aimState.set(aim.aim_id, { jobId, status: data.status || "queued", mode, startedAt: aimState.get(aim.aim_id)?.startedAt || Date.now() });
    await sleep(POLL_MS);
  }
}

function setRowStatus(row, kind, text) {
  if (!row) return;
  const el = row.querySelector('[data-role="status"]');
  if (!el) return;
  el.className = `aim-row__status is-${kind}`;
  el.innerHTML = `<span class="dot"></span>${esc(text)}`;
}

function refreshRowStatus(row, aimId) {
  const state = aimState.get(aimId);
  if (!state) return;
  const kindMap = { complete: "ok", failed: "error" };
  const kind = kindMap[state.status] || "running";
  const text = state.status === "complete"
    ? `${state.mode} · done`
    : state.status === "failed"
      ? "Last run failed"
      : `${state.mode} · ${state.status}…`;
  setRowStatus(row, kind, text);
}

// ---------- Digest rendering ----------

function renderDigest(aim, d, mode) {
  const sections = (d.sections || []).map(renderSection).join("");
  const funnel = d.funnel || {};
  const funnelBits = [
    funnel.ingested != null ? `ingested ${funnel.ingested}` : null,
    funnel.upserted != null ? `upserted ${funnel.upserted}` : null,
    funnel.retrieved != null ? `retrieved ${funnel.retrieved}` : null,
    funnel.sections != null ? `${funnel.sections} sections` : null,
    funnel.items != null ? `${funnel.items} items` : null,
  ].filter(Boolean).map((b) => `<code>${esc(b)}</code>`).join(" ");

  const generated = d.generated_at ? new Date(d.generated_at).toLocaleString() : "";
  const empty = !d.sections || !d.sections.length;
  digestView.innerHTML = `
    <div class="digest__header">
      <div class="digest__date">${esc(d.date_range || "")} · ${esc(mode)} mode</div>
      <h3 class="digest__headline">${esc(d.headline || "Digest ready")}</h3>
      <div class="digest__meta">
        <span>For <strong>${esc(aim.title)}</strong></span>
        ${generated ? `<span>· generated ${esc(generated)}</span>` : ""}
      </div>
      ${funnelBits ? `<div class="digest__funnel digest__meta">Funnel: ${funnelBits}</div>` : ""}
    </div>
    <div class="digest__sections">
      ${empty
        ? `<div class="digest-empty"><strong>No items in this digest.</strong>Try <em>force</em> mode to re-ingest, or relax the Aim's region filter.</div>`
        : sections}
    </div>
  `;
  digestView.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderSection(s) {
  const items = (s.items || []).map(renderItem).join("");
  return `
    <section class="digest-section">
      <h3 class="digest-section__title">${esc(s.title || "Untitled section")}</h3>
      <div class="digest-items">${items}</div>
    </section>`;
}

function renderItem(i) {
  const sources = (i.source_urls || []).map((u) => `
    <a class="chip" href="${esc(u)}" target="_blank" rel="noopener">
      <svg aria-hidden="true" style="width:11px;height:11px;"><use href="#i-external"/></svg>
      ${esc(shortHost(u))}
    </a>`).join("");
  const score = Number.isFinite(i.relevance_score) && i.relevance_score > 0
    ? `<span class="digest-item__score">★ ${i.relevance_score}</span>`
    : "";
  return `
    <article class="digest-item">
      <div class="digest-item__head">
        <h4 class="digest-item__title">${esc(i.title || "")}${score}</h4>
        ${i.item_type ? `<span class="digest-item__type">${esc(i.item_type)}</span>` : ""}
      </div>
      <p class="digest-item__body">${esc(i.body || "")}</p>
      <div class="digest-item__sources">${sources}</div>
    </article>`;
}

// ---------- Helpers ----------

function splitLines(s) {
  return (s || "").split("\n").map((x) => x.trim()).filter(Boolean);
}
function splitCsv(s) {
  return (s || "").split(",").map((x) => x.trim()).filter(Boolean);
}
function shortHost(u) {
  try { return new URL(u).hostname.replace(/^www\./, ""); } catch { return u; }
}
function secondsSince(ts) {
  if (!ts) return "?";
  return Math.max(1, Math.round((Date.now() - ts) / 1000));
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

// Boot.
loadAims();
