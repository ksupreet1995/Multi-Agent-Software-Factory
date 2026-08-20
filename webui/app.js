// Software Factory — live UI controller

const PIPELINE_STEPS = [
  ["director", "Director"],
  ["memory", "Memory"],
  ["identity", "Identity (JIT token)"],
  ["gateway", "Gateway → CRM tool"],
  ["code_writer", "Code Writer"],
  ["code_reviewer", "Code Reviewer"],
  ["code_interpreter", "Code Interpreter"],
  ["report_builder", "Report Builder"],
];

const $ = (id) => document.getElementById(id);

// ---- init -----------------------------------------------------------------
async function init() {
  buildPipeline();
  wireTabs();
  await loadStatus();
  await loadTenants();
  $("run-btn").addEventListener("click", runWorkflow);
  $("eval-btn").addEventListener("click", runEvals);
  $("pipeline-btn").addEventListener("click", runPipeline);
}

// ---- Strands -> AgentCore pipeline ----------------------------------------
function runPipeline() {
  const btn = $("pipeline-btn");
  btn.disabled = true;
  ["author", "run", "score"].forEach((s) => { $(`ps-${s}`).className = "pstage"; });
  const box = $("pipe-results");
  box.innerHTML = '<div class="pipe-log">Starting pipeline…</div>';

  const es = new EventSource("/api/pipeline");
  es.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.stage === "__end__") { es.close(); btn.disabled = false; return; }
    handlePipelineEvent(ev);
  };
  es.onerror = () => { es.close(); btn.disabled = false; };
}

function handlePipelineEvent(ev) {
  const node = $(`ps-${ev.stage}`);
  if (node) {
    if (ev.status === "running") node.className = "pstage running";
    else if (ev.status === "done") node.className = "pstage done";
    else if (ev.status === "error") node.className = "pstage error";
  }
  const box = $("pipe-results");
  const line = document.createElement("div");
  line.className = "pipe-log";
  line.textContent = `[${ev.stage}] ${ev.detail}`;
  box.appendChild(line);

  // Render the scenarios Strands Evals authored (its actual contribution).
  if (ev.stage === "author" && ev.status === "done" && ev.data.cases) {
    const wrap = document.createElement("div");
    wrap.className = "authored-cases";
    wrap.innerHTML = `<div class="authored-title">Strands Evals authored ${ev.data.cases.length} scenarios + ground truth:</div>`;
    for (const c of ev.data.cases) {
      const item = document.createElement("div");
      item.className = "authored-case";
      item.innerHTML = `
        <div class="ac-prompt">“${c.input}”</div>
        <div class="ac-gt"><span class="ac-tag">expected result</span> ${c.assertion || "—"}</div>
        <div class="ac-gt"><span class="ac-tag">expected tools</span> ${(c.trajectory || []).join(" → ")}</div>`;
      wrap.appendChild(item);
    }
    box.appendChild(wrap);
  }

  // Render AgentCore (managed) batch metrics — the OUTER-LOOP monitoring role
  if (ev.stage === "score" && ev.status === "done" && ev.data.results) {
    const note = document.createElement("div");
    note.className = "pipe-scorenote";
    note.innerHTML = "<b>AgentCore (managed)</b> — scores &amp; monitors the deployed agent at scale:";
    box.appendChild(note);
    box.appendChild(metricRow(ev.data.results, "agentcore"));
  }

  // Render Strands gate verdict — the INNER-LOOP pre-deploy role
  if (ev.stage === "strands_gate" && ev.status === "done" && ev.data.gate) {
    box.appendChild(renderGate(ev.data.gate));
  }
  box.scrollTop = box.scrollHeight;
}

function renderGate(gate) {
  const wrap = document.createElement("div");
  wrap.className = "gate-block";
  const pass = gate.verdict === "PASS";
  let cases = "";
  for (const c of gate.cases || []) {
    cases += `<div class="gate-case">
      <span class="gate-chip ${c.passed ? "ok" : "no"}">${c.passed ? "PASS" : "REVIEW"} ${c.score.toFixed(2)}</span>
      <span class="gate-scenario">${c.scenario}</span>
      <div class="gate-reason">${c.reason}</div>
    </div>`;
  }
  wrap.innerHTML = `
    <div class="pipe-scorenote"><b>Strands Evals (local)</b> — pre-deploy gate: would this ship?</div>
    <div class="gate-verdict ${pass ? "ok" : "no"}">${gate.verdict} — ${gate.passed}/${gate.total} scenarios passed (threshold ${Math.round(gate.threshold * 100)}%)</div>
    <div class="gate-cases">${cases}</div>`;
  return wrap;
}

function metricRow(results, kind) {
  const wrap = document.createElement("div");
  wrap.className = "pipe-final";
  for (const r of results) {
    const m = document.createElement("div");
    m.className = `pipe-metric ${kind}`;
    const val = formatScore(r.aggregate, r.evaluator);
    m.innerHTML = `<div class="v">${val}</div>
      <div class="k">${r.evaluator.replace("Builtin.", "")}<br>${r.count} evaluated</div>`;
    wrap.appendChild(m);
  }
  return wrap;
}

function buildPipeline() {
  const el = $("pipeline");
  el.innerHTML = "";
  for (const [key, label] of PIPELINE_STEPS) {
    const node = document.createElement("div");
    node.className = "pnode";
    node.id = `p-${key}`;
    node.innerHTML = `<span class="dot"></span><span>${label}</span>`;
    el.appendChild(node);
  }
}

function wireTabs() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      document.querySelectorAll(".tab-body").forEach((b) => b.classList.add("hidden"));
      tab.classList.add("active");
      $(`tab-${tab.dataset.tab}`).classList.remove("hidden");
    });
  });
}

async function loadStatus() {
  const s = await (await fetch("/api/status")).json();
  const modePill = $("mode-pill");
  modePill.textContent = s.live_available ? "LIVE · AgentCore" : "LIVE unavailable";
  modePill.classList.add(s.live_available ? "ok" : "warn");

  const cw = $("cw-pill");
  if (s.cloudwatch_enabled) {
    cw.textContent = `CloudWatch: ${s.cloudwatch_log_group}`;
    cw.classList.add("ok");
  } else {
    cw.textContent = `CloudWatch: ${s.cloudwatch_status}`;
    cw.classList.add("warn");
  }

  // Reflect deployed-director availability on the LIVE indicator.
  const ind = $("live-indicator");
  if (ind) {
    if (s.live_available) {
      ind.textContent = "● LIVE — deployed AgentCore runtimes";
      ind.classList.add("ok");
    } else {
      ind.textContent = "● LIVE unavailable — deploy the AgentCore project first";
      ind.classList.add("warn");
    }
  }

  // Online eval config status badge
  const os = s.online_eval || {};
  const el = $("online-status");
  if (os.available) {
    el.innerHTML = `<span class="live">${os.status}</span> — continuously scoring live director traffic`;
  } else {
    el.textContent = os.status || "not configured";
  }
}

// ---- evals ----------------------------------------------------------------
async function runEvals() {
  const btn = $("eval-btn");
  btn.disabled = true;
  const box = $("eval-results");
  box.innerHTML = '<div class="eval-loading"><span class="spinner"></span> Running on-demand evaluation (LLM-as-a-judge, ~1–2 min)…</div>';
  try {
    const res = await (await fetch("/api/evals")).json();
    renderEvals(res);
  } catch (e) {
    box.innerHTML = `<div class="empty">Eval failed: ${e}</div>`;
  }
  btn.disabled = false;
}

// Different evaluators use different scales, so a raw "1" or "2" is ambiguous.
// report_quality is an LLM judge on a 1–5 rubric; everything else here
// (built-ins + the code-based report_schema_check) is a 0.0–1.0 fraction.
// This map lets us render each score against its own scale.
function evalScale(name) {
  const n = (name || "").toLowerCase();
  if (n.includes("report_quality")) {
    return { max: 5, suffix: " / 5", decimals: 0 };
  }
  return { max: 1, suffix: "", decimals: 2 };
}

// Fraction (0–1) of the score against its own scale — drives bar width + color.
function scoreFraction(v, name) {
  if (v == null) return null;
  return v / evalScale(name).max;
}

function scoreClass(v, name) {
  const f = scoreFraction(v, name);
  if (f == null) return "mid";
  if (f >= 0.8) return "good";
  if (f >= 0.5) return "mid";
  return "bad";
}

// Human-readable score against its scale: "2 / 5" for report_quality,
// "1.00" for 0–1 evaluators.
function formatScore(v, name) {
  if (v == null) return "—";
  const sc = evalScale(name);
  return v.toFixed(sc.decimals) + sc.suffix;
}

function renderEvals(res) {
  const box = $("eval-results");
  if (res.error) {
    box.innerHTML = `<div class="empty">${res.error}</div>`;
    return;
  }
  box.innerHTML = "";
  const header = document.createElement("div");
  header.className = "evals-sub";
  const scoped = res.scoped_session ? ` · session ${res.scoped_session.slice(0, 28)}…` : "";
  header.textContent = `Scored ${res.session_count} session(s) in ${res.region}${scoped}`;
  box.appendChild(header);

  for (const r of res.results) {
    const frac = scoreFraction(r.aggregate, r.evaluator);
    const pct = frac != null ? Math.round(frac * 100) : 0;
    const scaleNote = evalScale(r.evaluator).max === 5
      ? "LLM judge · 1–5 scale"
      : "0.0–1.0 scale";
    const card = document.createElement("div");
    card.className = "eval-card";
    let sessions = "";
    for (const s of (r.sessions || []).slice(0, 3)) {
      const expl = s.explanation || "";
      const sVal = formatScore(s.value, r.evaluator);
      const sLabel = s.label ? `${s.label} (${sVal})` : sVal;
      sessions += `<div class="eval-session">
        <span class="sid">${s.session_id || "(session)"}</span> — ${sLabel}
        <details class="expl-wrap"><summary>${expl.slice(0, 140)}${expl.length > 140 ? "…" : ""}</summary>
        <div class="expl-full">${expl}</div></details>
      </div>`;
    }
    card.innerHTML = `
      <div class="ehead">
        <span class="eval-name">${r.evaluator}<span class="eval-scale">${scaleNote}</span></span>
        <span class="eval-score ${scoreClass(r.aggregate, r.evaluator)}">${formatScore(r.aggregate, r.evaluator)}</span>
      </div>
      <div class="bar"><span style="width:${pct}%"></span></div>
      <div class="eval-sessions">${sessions}</div>`;
    box.appendChild(card);
  }
}

async function loadTenants() {
  const tenants = await (await fetch("/api/tenants")).json();
  const sel = $("tenant");
  sel.innerHTML = "";
  for (const t of tenants) {
    const opt = document.createElement("option");
    opt.value = t.id;
    opt.textContent = `${t.name} (${t.rate_limit}/min)`;
    sel.appendChild(opt);
  }
}

// ---- run ------------------------------------------------------------------
function resetView() {
  $("feed").innerHTML = "";
  $("code-view").textContent = "// generated Python will appear here";
  $("crm-view").querySelector("tbody").innerHTML = "";
  $("slide-body").textContent = "";
  $("slide-sub").textContent = "";
  $("download").classList.add("hidden");
  for (const [key] of PIPELINE_STEPS) {
    const n = $(`p-${key}`);
    if (n) n.className = "pnode";
  }
}

function runWorkflow() {
  const tenant = $("tenant").value;
  const prompt = $("prompt").value;
  const btn = $("run-btn");
  resetView();
  btn.disabled = true;
  $("tenant-tag").textContent = `— ${tenant} · LIVE`;

  const params = new URLSearchParams({ tenant, prompt });
  const es = new EventSource(`/api/run?${params.toString()}`);

  es.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.step === "__end__") {
      es.close();
      btn.disabled = false;
      finalizeStuckCards();
      return;
    }
    handleEvent(ev);
  };
  es.onerror = () => {
    es.close();
    btn.disabled = false;
  };
}

function handleEvent(ev) {
  updatePipeline(ev);
  addFeedCard(ev);
  renderData(ev);
}

// When the stream ends, flip any card/pipeline node still "running" to done,
// so the UI never leaves stuck spinners if a done-event was missed.
function finalizeStuckCards() {
  document.querySelectorAll('.card[data-status="running"]').forEach((card) => {
    card.dataset.status = "done";
    const s = card.querySelector(".cstatus");
    s.className = "cstatus done";
    s.innerHTML = statusLabel("done");
  });
  document.querySelectorAll(".pnode.running").forEach((n) => {
    n.className = "pnode done";
  });
}

function updatePipeline(ev) {
  const n = $(`p-${ev.step}`);
  if (!n) return;
  if (ev.status === "running") n.className = "pnode running";
  else if (ev.status === "done" && !n.classList.contains("error")) n.className = "pnode done";
  else if (ev.status === "error") n.className = "pnode error";
}

function addFeedCard(ev) {
  const feed = $("feed");
  const empty = feed.querySelector(".empty");
  if (empty) empty.remove();

  // Update existing running card of same step to done, instead of duplicating.
  const existing = feed.querySelector(`.card[data-step="${ev.step}"][data-status="running"]`);
  if (existing && ev.status !== "running") {
    existing.dataset.status = ev.status;
    existing.querySelector(".cstatus").className = `cstatus ${ev.status}`;
    existing.querySelector(".cstatus").innerHTML = statusLabel(ev.status);
    existing.querySelector(".cdetail").textContent = ev.detail;
    return;
  }

  const card = document.createElement("div");
  card.className = "card";
  card.dataset.step = ev.step;
  card.dataset.status = ev.status;
  card.innerHTML = `
    <div class="chead">
      <span class="badge ${ev.step}">${ev.step.replace(/_/g, " ")}</span>
      <span class="ctitle">${ev.title}</span>
      <span class="cstatus ${ev.status}">${statusLabel(ev.status)}</span>
    </div>
    <p class="cdetail">${ev.detail || ""}</p>`;
  feed.appendChild(card);
  feed.scrollTop = feed.scrollHeight;
}

function statusLabel(status) {
  if (status === "running") return '<span class="spinner"></span> running';
  if (status === "done") return "✓ done";
  if (status === "error") return "✗ halted";
  return status;
}

function renderData(ev) {
  const d = ev.data || {};
  if (ev.step === "gateway" && d.records) renderCrm(d.records);
  if (ev.step === "code_writer" && d.code) {
    $("code-view").textContent = d.code;
    activateTab("code");
  }
  if (ev.step === "report_builder" && ev.status === "done") {
    renderReport(ev, d);
    activateTab("report");
  }
}

function renderCrm(records) {
  const tbody = $("crm-view").querySelector("tbody");
  tbody.innerHTML = "";
  for (const r of records) {
    const tr = document.createElement("tr");
    if (r.status === "at_risk") tr.className = "at_risk";
    tr.innerHTML = `<td>${r.name}</td><td>$${r.mrr.toLocaleString()}</td>
      <td>${r.status}</td><td>${r.tickets_open}</td>`;
    tbody.appendChild(tr);
  }
}

let lastReport = { title: "Customer Status Report", tenant: "", text: "" };

function renderReport(ev, d) {
  $("slide-body").textContent = d.report_text || "";
  $("slide-sub").textContent = `Tenant ${document.getElementById("tenant").value} · ${d.kind?.toUpperCase() || ""}`;
  const dl = $("download");
  const tenant = document.getElementById("tenant").value;
  if (d.report_text) {
    // The deployed director returns report text — build the .pptx on demand.
    lastReport = { title: "Customer Status Report", tenant, text: d.report_text };
    dl.href = "#";
    dl.onclick = downloadReportPptx;
    dl.classList.remove("hidden");
    dl.textContent = "⬇ Download PowerPoint (.pptx)";
  }
}

async function downloadReportPptx(e) {
  if (e) e.preventDefault();
  const resp = await fetch("/api/report-pptx", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(lastReport),
  });
  if (!resp.ok) { alert("Could not generate PowerPoint"); return; }
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "customer_status_report.pptx";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function activateTab(name) {
  document.querySelectorAll(".tab").forEach((t) => {
    t.classList.toggle("active", t.dataset.tab === name);
  });
  document.querySelectorAll(".tab-body").forEach((b) => b.classList.add("hidden"));
  $(`tab-${name}`).classList.remove("hidden");
}

init();
