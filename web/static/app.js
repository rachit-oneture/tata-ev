"use strict";
// Paste logs -> POST /api/analyze -> render the plain-language report.
const $ = selector => document.querySelector(selector);
const apiBaseUrl = String(globalThis.TATA_EV_CONFIG?.apiBaseUrl || "").replace(/\/+$/, "");
const apiUrl = path => apiBaseUrl + path;
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
let controller = null;
let activeAnalysis = null;
let feedbackBusy = false;

function updateFeedbackControls() {
  const ready = Boolean(activeAnalysis?.id);
  $("#feedback-actions").hidden = !ready;
  $("#feedback-actions").setAttribute("aria-busy", String(feedbackBusy));
  $("#thumb-up").setAttribute("aria-pressed", String(activeAnalysis?.rating === "up"));
  $("#thumb-down").setAttribute("aria-pressed", String(activeAnalysis?.rating === "down"));
  for (const id of ["#thumb-up", "#thumb-down", "#feedback-button", "#feedback-submit"]) {
    $(id).disabled = !ready || feedbackBusy;
  }
}

function openFeedback() {
  if (!activeAnalysis || feedbackBusy) return;
  $("#feedback-resolution").value = activeAnalysis.feedback;
  $("#feedback-status").hidden = true;
  $("#feedback-dialog").showModal();
}

async function saveClientFeedback(change, fromDialog = false) {
  if (!activeAnalysis || feedbackBusy) return false;
  const reviewedAnalysis = activeAnalysis;
  feedbackBusy = true;
  updateFeedbackControls();
  const status = $(fromDialog ? "#feedback-status" : "#rating-status");
  status.hidden = false;
  status.textContent = "Saving…";
  try {
    const response = await fetch(apiUrl("/api/feedback"), {
      method: "POST", headers: {"Content-Type": "application/json", Accept: "application/json"},
      body: JSON.stringify({analysis_id: reviewedAnalysis.id, ...change}),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Feedback could not be saved. Please try again.");
    if (activeAnalysis !== reviewedAnalysis) return false;
    activeAnalysis.rating = result.rating;
    activeAnalysis.feedback = result.feedback;
    $("#rating-status").textContent = "Saved. Thank you for your feedback.";
    if (fromDialog) $("#feedback-dialog").close();
    return true;
  } catch (error) {
    if (activeAnalysis === reviewedAnalysis) status.textContent = error.message;
    return false;
  } finally {
    feedbackBusy = false;
    updateFeedbackControls();
  }
}

function highlightedLogLine(line) {
  const match = line.match(/^(\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}:\d{2}\s+[A-Z]{2,5})(\s*)/);
  if (!match) return esc(line) || " ";
  return `<span class="timestamp">${esc(match[1])}</span>${esc(match[2])}${esc(line.slice(match[0].length))}`;
}

function syncEditorScroll() {
  const logs = $("#logs");
  const shift = `translate(${-logs.scrollLeft}px, ${-logs.scrollTop}px)`;
  $("#log-highlight").style.transform = shift;
  const numbers = $("#line-numbers .line-numbers-inner");
  if (numbers) numbers.style.transform = `translateY(${-logs.scrollTop}px)`;
}

function renderEditorSource({resetScroll = false} = {}) {
  const logs = $("#logs"), highlight = $("#log-highlight"), gutter = $("#line-numbers");
  const lines = logs.value.split(/\r?\n/);
  highlight.innerHTML = lines.map((line, index) => `<span class="log-line" data-line="${index + 1}">${highlightedLogLine(line)}</span>`).join("");
  gutter.innerHTML = `<div class="line-numbers-inner">${lines.map((_, index) => `<span>${index + 1}</span>`).join("")}</div>`;
  requestAnimationFrame(() => {
    // Match the textarea's text width, excluding its vertical scrollbar.
    highlight.style.width = `${logs.clientWidth}px`;
    const sourceLines = highlight.querySelectorAll(".log-line");
    const numbers = gutter.querySelectorAll("span");
    sourceLines.forEach((line, index) => { if (numbers[index]) numbers[index].style.height = `${line.getBoundingClientRect().height}px`; });
    if (resetScroll) { logs.scrollTop = 0; logs.scrollLeft = 0; }
    syncEditorScroll();
  });
}

const LOOKUP_TEXT = {
  documented: ["documented", "The OCPP vendor ID matches the table's manufacturer text. Physical make, model and firmware are not confirmed."],
  unscoped_table: ["weak", "Listed in a table that names no manufacturer. Not confirmed for this charger."],
  vendor_unknown: ["weak", "The log has no vendor ID; another maker's table lists this code."],
  other_vendor: ["weak", "Only a different manufacturer's table lists this code. It probably does not apply."],
  phrase_match: ["weak", "The text matches a table entry but the code differs. Not confirmed."],
  not_found: ["none", "No definition in the supplied code tables."],
};

function codeCards(signals) {
  const shown = signals.filter(s => ["vendor_code", "code_in_info", "ocpp_error", "vendor_message", "message", "stop_reason", "rejected", "connection_lost", "faulted"].includes(s.kind));
  if (!shown.length) return "<p>No error code or fault was reported.</p>";
  return shown.map(s => {
    const where = s.scope === "charger" ? "charger-wide" : (s.connector ? `connector ${esc(s.connector)}` : "");
    const seen = `first at ${esc(s.time)} (line ${s.lines[0]}${s.lines.length > 1 ? `, seen ${s.lines.length} times` : ""})${where ? ", " + where : ""}`;
    const kindLabel = s.kind === "ocpp_error" ? "OCPP standard error code" :
      s.kind === "vendor_code" || s.kind === "code_in_info" ? "Vendor-reported code" :
      s.kind === "vendor_message" ? "Vendor error-field text" : "Reported value";
    if (s.lookup) {
      const [cls, note] = LOOKUP_TEXT[s.lookup.status] || LOOKUP_TEXT.not_found;
      const defs = s.lookup.entries.map(e => `<div><strong>${esc(e.description)}</strong> <small>${esc(e.manufacturer || "no manufacturer named")}${e.page ? ", page " + esc(e.page) : ""}</small></div>`).join("");
      return `<div class="code-card ${cls}"><strong>${esc(s.value)}</strong> <span>${esc(s.kind === "code_in_info" ? "(reported in the info text)" : "")}</span><small>${esc(kindLabel)}</small>${defs}<small>${esc(s.lookup.note || note)}</small><small>${seen}</small></div>`;
    }
    const meaning = s.meaning ? `<div>${esc(s.meaning)} <small>(${esc(s.meaning_source)})</small></div>` : "";
    return `<div class="code-card ${s.meaning ? "documented" : "weak"}"><strong>${esc(s.value)}</strong><small>${esc(kindLabel)}</small>${meaning}<small>${seen}</small></div>`;
  }).join("");
}

function section(number, title, content, open = true) {
  return `<details class="report-section" ${open ? "open" : ""}><summary><h3><span class="section-number">${number}</span>${title}</h3></summary>${content}</details>`;
}

function symptomBlock(a) {
  if (!a.no_code_reported || !a.symptoms.length) return "";
  const items = a.symptoms.map(x => `<li><strong>${esc(x.title)}</strong>${x.time ? ` <small>(${esc(x.time)}${x.line ? ", line " + x.line : ""})</small>` : ""}<br>${esc(x.detail)}</li>`).join("");
  return `<div class="notice"><strong>No error code was reported.</strong> ${esc(a.code_note)}<ul class="steps">${items}</ul></div>`;
}


const CERT = {direct: "seen directly in the log", strongly_inferred: "strongly inferred", weakly_inferred: "weakly inferred"};
const SRC = {log: "log", protocol_knowledge: "protocol knowledge", catalogue: "code tables", not_available: "not available", registry: "charger registry"};
const STAGE_NAMES = {PLUGGED_IN: "Vehicle plugged in", START_COMMAND_ACCEPTED: "Start command accepted", AUTHORIZED: "ID authorized",
  TRANSACTION_STARTED: "Charging session created", ENERGY_TRANSFER: "Energy delivered"};
const RESULT_TEXT = {
  INSUFFICIENT_EVIDENCE: "Not enough evidence to name the cause",
  CODE_REPORTED: "The charger reported a code",
  STANDARD_ERROR_REPORTED: "The charger reported a standard OCPP error",
  NO_PROBLEM_SEEN: "No problem seen",
};
const tag = (cert, src) => `<small class="prov">${esc(CERT[cert] || cert)}${src ? " · " + esc(SRC[src] || src) : ""}</small>`;

function assessmentHtml(a) {
  const x = a.assessment, st = x.stage_assessment, ca = x.cause_assessment, d = x.documented_faults_to_check;
  const ladder = st.ladder.map(s => `<li class="stage ${s.reached ? "yes" : "no"}">${s.reached ? "✔" : "✘"} ${esc(STAGE_NAMES[s.stage] || s.stage)}${s.evidence.lines?.length ? ` <small>(line ${s.evidence.lines[0]})</small>` : ""}</li>`).join("");
  const fw = st.failure_window;
  const where = `<p><strong>Progress stopped:</strong> after “${esc(STAGE_NAMES[st.last_confirmed_stage.value] || st.last_confirmed_stage.value || "nothing confirmed")}”, before “${esc(STAGE_NAMES[st.next_expected_stage] || st.next_expected_stage || "-")}”. ${tag(fw.certainty, fw.source)}<br><small>${esc(fw.basis)}</small></p>
    <p><small>${esc(st.sub_stage.note)} ${tag(st.sub_stage.certainty, st.sub_stage.source)}</small></p>`;
  let cause = `<p><strong>Cause:</strong> ${esc(RESULT_TEXT[ca.result] || ca.result)}.</p><p>${esc(ca.note || "")}</p>`;
  if (ca.likely_domain) cause += `<p><strong>Most that can be said about where:</strong> ${esc(ca.likely_domain.value.replaceAll("_", " ").toLowerCase())} ${tag(ca.likely_domain.certainty, ca.likely_domain.source)}${ca.likely_domain.note ? `<br><small>${esc(ca.likely_domain.note)}</small>` : ""}</p>`;
  const o = x.observed;
  const observed = `<ul class="steps"><li>OCPP statuses: ${esc(o.ocpp_statuses_in_attempt.value.join(", ") || "none")}</li><li>OCPP error codes: ${esc(o.ocpp_error_codes.value.join(", ") || "none")}</li><li>Vendor fault reported by the charger: <strong>${o.vendor_fault_reported.value ? "yes" : "no"}</strong></li><li>Energy delivered: ${esc(o.energy_delivered.value ?? "unknown")}</li><li>Stop reason: ${esc(o.stop_reason.value ?? "none")}</li></ul>`;
  const neg = x.not_observed.map(n => `<li class="${n.observed ? "bad" : ""}">${n.observed ? "⚠ " : "✔ "}${esc(n.check)}: ${n.observed ? esc(n.detail) + ` <small>(line ${n.lines[0]})</small>` : "not observed"}</li>`).join("");
  const h = x.supplied_logs_evidence;
  let history = "";
  if (h) {
    const exact = h.exact_charger ? (h.exact_charger.state === "seen_in_corpus"
      ? `<li>This charger identifier appears in ${esc(h.exact_charger.samples)} supplied source log(s). Recorded statuses: ${esc(Object.entries(h.exact_charger.statuses).map(([s,n]) => `${s} (${n})`).join(", ") || "none")}. Statuses with an OCPP error: ${esc(h.exact_charger.status_error_count)}.</li>`
      : `<li>${esc(h.exact_charger.note)}</li>`) : "";
    const vendor = h.reported_vendor ? `<li>Reported vendor ID ${esc(h.reported_vendor.name)} appears on ${esc(h.reported_vendor.chargers)} identifier(s) in ${esc(h.reported_vendor.samples)} supplied source log(s). ${esc(h.reported_vendor.documented_code_reports)} documented code report(s) were recorded; ${esc(h.reported_vendor.code_reports_with_ocpp_error)} included an OCPP error. <small>${esc(h.reported_vendor.note)}</small></li>` : "";
    const repeats = (h.repeated_statuses || []).map(r => `<li>${esc(r.status)} appears ${esc(r.occurrences)} time(s) on ${esc(r.chargers)} identifier(s) in the supplied corpus. <small>${esc(r.note)}</small></li>`).join("");
    if (exact || vendor || repeats) history = `<h4>Evidence from all supplied logs</h4><ul class="steps">${exact}${vendor}${repeats}</ul><p><small>${esc(h.corpus?.source || "")}</small></p>`;
  }
  let faults = "";
  if (d.families.length) {
    const shared = d.shared_contradictions.map(c => `<p class="notice">${esc(c)}</p>`).join("");
    const fam = d.families.map(f => `<details><summary>${esc(f.family.replaceAll("_", " ").toLowerCase())} <small>(${f.documented_entries} documented)</small></summary>${f.entries.map(e => `<div class="code-card ${e.contradicts.length ? "none" : "weak"}"><strong>${esc(e.code)}</strong> ${esc(e.description)} <small>${esc(e.manufacturer)}${e.page ? ", page " + esc(e.page) : ""}</small>${e.contradicts.map(c => `<small>✘ ${esc(c)}</small>`).join("")}${e.supports.map(c => `<small>✔ ${esc(c)}</small>`).join("")}${e.caveats.map(c => `<small>• ${esc(c)}</small>`).join("")}</div>`).join("")}</details>`).join("");
    faults = `<p><strong>Documented faults that fit this stage — none was reported by the charger.</strong></p>${shared}<p><small>${esc(d.note)}</small></p>${fam}`;
  } else if (d.note) faults = `<p><small>${esc(d.note)}</small></p>`;
  const ctx = x.context.connector_protocol.value ? "" : `<p class="notice">${esc(x.context.connector_protocol.note)}</p>`;
  return `<h4>What the log shows</h4>${observed}<h4>How far the session got</h4><ul class="ladder">${ladder}</ul>${where}<h4>Cause</h4>${cause}<h4>Checked and not seen</h4><ul class="steps">${neg}</ul>${history}${ctx}${faults}`;
}

function llmReviewHtml(review) {
  if (!review) return "";
  if (review.status !== "ok") return `<p class="notice">LLM review was unavailable. The deterministic analysis above is still complete.</p>`;
  const r = review.review, d = r.code_decision;
  const decision = d.state === "reported" ? `Reported code: ${d.code}` :
    d.state === "candidate_to_check" ? `Code to check: ${d.code} (not reported by the charger)` : "No code can be selected from this log";
  const lines = r.line_by_line.map(x => `<li><strong>Line ${esc(x.line)}:</strong> ${esc(x.meaning)}<br><small>${esc(x.relevance)}</small></li>`).join("");
  return `<p><strong>${esc(decision)}</strong></p><p>${esc(d.comparison)}</p><p><strong>Conclusion:</strong> ${esc(r.diagnosis)}</p><p><strong>Reason:</strong> ${esc(r.reason)}</p><p><small>Model: ${esc(review.model)} · ${esc(review.usage.latency_seconds)} s</small></p><details><summary>Line-by-line LLM review</summary><ol class="steps">${lines}</ol></details>`;
}

function chargerIdentityHtml(data) {
  const devices = data.devices || data.case_assessment?.devices || [];
  const attempts = data.attempts || [];
  const unique = values => [...new Set(values.filter(value => value != null && value !== ""))].join(", ");
  const fields = [
    ["Vendor", unique([...devices.map(d => d.vendor), ...attempts.map(a => a.reported_vendor_id)])],
    ["Charger ID", unique([...devices.map(d => d.charger), ...attempts.map(a => a.charger), data.summary?.charger])],
    ["Session ID", unique(attempts.map(a => a.transaction_id))],
    ["Connector", unique(attempts.map(a => a.connector))],
    ["Model", unique(devices.map(d => d.model))],
    ["Total attempts", String(attempts.length)],
  ];
  return `<section class="charger-identity" aria-label="Log details"><dl>${fields.map(([label, value]) => `<div><dt>${esc(label)}</dt><dd>${esc(value || "Not recorded")}</dd></div>`).join("")}</dl></section>`;
}

function problemPhaseTagsHtml(attempts) {
  const affected = attempts.filter(a => ["problem", "symptom_only"].includes(a.verdict));
  const tags = affected.map(a => a.phase === "after_start"
    ? {phase: "after", text: "After start"}
    : {phase: "before", text: "Before start"});
  return tags.filter((tag, index) => tags.findIndex(other => other.text === tag.text) === index)
    .map(tag => `<span class="case-phase-tag ${tag.phase}" aria-label="When the problem appeared: ${esc(tag.text)}">${esc(tag.text)}</span>`).join("");
}

function caseSignalTagsHtml(attempts) {
  const affected = attempts.filter(a => ["problem", "symptom_only"].includes(a.verdict));
  const distinct = values => [...new Set(values.filter(value => value != null && value !== ""))].join(", ");
  const errorCodes = distinct(affected.flatMap(a => a.signals
    .filter(s => ["ocpp_error", "vendor_code", "code_in_info", "vendor_message"].includes(s.kind)).map(s => s.value)));
  const statuses = affected.flatMap(a => a.assessment.observed.ocpp_statuses_in_attempt.value);
  const problemStatuses = distinct(statuses.filter(status => ["SuspendedEV", "SuspendedEVSE", "Faulted"].includes(status)));
  const error = errorCodes || (affected.length && affected.every(a => a.suspension_no_error)
    ? "NoError — no fault code reported" : "No fault code reported");
  return `<span class="case-phase-tag case-error-tag"><strong>Error code:</strong> ${esc(error)}</span><span class="case-phase-tag case-status-tag"><strong>Problem status:</strong> ${esc(problemStatuses || "No problem status reported")}</span>`;
}

function caseAssessmentHtml(c, attempts = []) {
  if (!c) return "";
  const metrics = c.kpis.map(k => `<div class="case-kpi"><span>${esc(k.label)}</span><strong>${esc(k.value)}</strong></div>`).join("");
  const evidence = c.evidence.map(x => `<li>${esc(x)}</li>`).join("");
  const devices = (c.devices || []).map(d => {
    const parts = [d.vendor, d.model, d.firmware ? `firmware ${d.firmware}` : ""].filter(Boolean);
    return `<p class="case-device"><strong>Charger-reported at boot:</strong> ${esc(parts.join(" · "))} <small>(line ${esc(d.line)})</small></p>`;
  }).join("");
  const missing = (c.unresolved_vendor_values || []).length ? `<p class="case-missing"><strong>Code-table gap:</strong> No supplied code-table definition matches ${esc(c.unresolved_vendor_values.map(x => `${x.value} (lines ${x.lines.join(", ")})`).join(", "))}. Add the service code table for this charger model to define it.</p>` : "";
  const separate = c.separate_reports.length ? `<p class="case-separate"><strong>Separate later report:</strong> ${esc(c.separate_reports.join(", "))}. It is outside these charging attempts and is not used to explain them.</p>` : "";
  const reading = c.evidence_reading ? `<div class="case-reading"><h3>Evidence-based reading</h3><p>${esc(c.evidence_reading.diagnosis)}</p><p>${esc(c.evidence_reading.reason)}</p><small>Read against all ${esc(c.evidence_reading.lines_reviewed)} parsed log lines.</small></div>` : "";
  return `<section class="case-assessment"><div class="case-heading"><p class="case-eyebrow">CASE DIAGNOSIS</p>${problemPhaseTagsHtml(attempts)}${caseSignalTagsHtml(attempts)}</div><h2>${esc(c.immediate_reason)}</h2><div class="case-kpis">${metrics}</div>${devices}${missing}<div class="case-columns"><div><h3>What the pattern shows</h3><ul class="steps">${evidence}</ul></div><div><h3>Most likely fault domain</h3><p>${esc(c.fault_domain)}</p><h3>Why it happened</h3><p>${esc(c.root_cause_limit)}</p></div></div>${reading}${separate}</section>`;
}

function compactLogHtml(compact) {
  if (!compact?.text) return "";
  return `<details class="compact-log"><summary><strong>See the compact log supplied to the LLM</strong><small>${esc(compact.line_count)} source lines</small></summary><p>${esc(compact.note)}</p><pre>${esc(compact.text)}</pre></details>`;
}

function attemptHtml(a, index, total) {
  const verdict = {problem: ["problem", "Problem found"], symptom_only: ["problem", "Problem seen, no code"], ended_normally: ["fine", "No fault"], unclear: ["unclear", "Unclear"]}[a.verdict];
  const phase = a.phase === "after_start" ? "after" : "before";
  const p = a.primary_signal;
  const headCode = p ? p.value : "";
  const w = a.when;
  const facts = [
    ["Charger", a.charger || "not named in the log"], ["Reported vendor ID", a.reported_vendor_id || "not recorded"],
    ["Connector", a.connector ?? "unknown"], ["Transaction", a.transaction_id || "not recorded"],
    ["Started (attempt)", w.first_entry], ["Charging session started", w.session_started || "not confirmed in the log"],
    ["First problem", w.first_problem || "none reported"], ["Session stopped", w.session_stopped || "no stop in the log"],
    ["Duration of the session", w.duration || "n/a"], ["Attempt ended", w.ended_by || "unknown"],
  ].map(([k, v]) => `<p class="report-fact"><strong>${esc(k)}:</strong> ${esc(v)}</p>`).join("");
  const meter = a.meter.start != null ? `<p class="report-fact"><strong>Meter:</strong> ${esc(a.meter.start)} → ${esc(a.meter.stop ?? "no stop reading")}${a.meter.change != null ? ` (change ${esc(a.meter.change)})` : ""}</p>` : "";
  const plainStory = `<p class="easy-summary"><strong>${esc(a.phase_label)}.</strong> ${esc(a.summary.what_happened)} ${esc(a.summary.error)}</p><ul class="steps">${a.how.map(h => `<li>${esc(h)}</li>`).join("")}</ul><div class="attempt-facts">${facts}${meter}</div>`;
  const timeline = a.timeline.map(t => `<div class="timeline-line ${t.abnormal ? "abnormal" : ""} ${t.context ? "dim" : ""}"><time>${esc(t.time)}</time>${esc(t.text)}${t.context ? " (before the attempt)" : ""}${t.charger_level ? " (charger-wide)" : ""} <small>line ${t.line}</small></div>`).join("");
  const before = a.signals_before_attempt.length ? `<div class="notice"><strong>Reported just before this attempt:</strong>${codeCards(a.signals_before_attempt)}</div>` : "";
  return `<details class="attempt" ${a.verdict === "problem" || total === 1 ? "open" : ""}>
    <summary><strong>Attempt ${a.number}</strong><span class="badge ${phase}">${esc(a.phase_label)}</span><span class="badge ${verdict[0]}">${verdict[1]}</span>${headCode ? `<span>${esc(headCode)}</span>` : (a.no_code_reported ? "<span>no error code reported</span>" : "")}<span>${esc(w.first_entry)}</span></summary>
    <div class="attempt-body">
      ${headCode ? `<p class="code-line">${esc(headCode)}</p>` : (a.no_code_reported ? `<p class="code-line">No error code reported</p>` : "")}
      <p class="report-fact"><strong>Why “${esc(a.phase_label.toLowerCase())}”:</strong> ${esc(a.phase_basis)}</p>
      ${section("01", "Error codes and what they mean", before + (a.no_code_reported ? symptomBlock(a) : codeCards(a.signals)))}
      ${section("02", "What happened", plainStory)}
      ${section("03", "Full timeline", timeline, false)}
    </div></details>`;
}

function render(data) {
  activeAnalysis = data.analysis_id ? {id: data.analysis_id, rating: null, feedback: ""} : null;
  $("#rating-status").textContent = "";
  updateFeedbackControls();
  const attempts = data.attempts || [];
  const assessment = data.case_assessment || {};
  const unique = values => [...new Set(values.filter(Boolean))];
  function attemptReport(attempt, index) {
    const signals = (attempt ? attempt.signals : data.other_signals || [])
      .filter(s => ["ocpp_error", "vendor_code", "code_in_info", "vendor_message"].includes(s.kind) && s.value && s.value !== "NoError");
    const codes = unique(signals.map(s => s.value));
    const descriptions = unique(signals.flatMap(s => {
      if (s.lookup?.status === "documented") return (s.lookup.entries || []).map(entry => `${s.value}: ${entry.description}`);
      return s.meaning ? [`${s.value}: ${s.meaning}`] : [];
    }));
    const reading = attempt?.evidence_reading || (attempts.length <= 1 ? assessment.evidence_reading : null);
    const cause = reading?.reason || attempt?.assessment?.cause_assessment?.note || "The physical root cause is not established from the available evidence.";
    const checks = Array.isArray(reading?.needs_confirmation) ? reading.needs_confirmation.filter(x => typeof x === "string" && x.trim()) : [];
    const action = checks.length ? checks.join("; ") : "Confirm the applicable manufacturer code definition and collect additional charger and vehicle diagnostics.";
    const fields = [
      ["Error Code", codes.join(", ") || "No error code reported"],
      ["Error Description", descriptions.join("; ") || attempt?.summary?.error || assessment.immediate_reason || "No confirmed error description is available."],
      ["Probable Reason / Root Cause", cause],
      ["Recommended Feedback / Action", action],
    ];
    const heading = attempt ? `<h2>Attempt ${esc(attempt.number ?? index + 1)}</h2>` : "";
    return `<section class="case-assessment">${heading}${fields.map(([label, value]) => `<div class="report-field"><h3>${esc(label)}</h3><p>${esc(value)}</p></div>`).join("")}</section>`;
  }
  const warning = (data.warnings || []).length ? `<p class="notice">${esc(data.warnings.join(" "))}</p>` : "";
  const reports = attempts.length ? attempts.map(attemptReport).join("") : attemptReport(null, 0);
  $("#results").innerHTML = `<article class="saved-client-report document-only">${chargerIdentityHtml(data)}${reports}${warning}</article>`;
  $("#results").hidden = false;
  $("#results").setAttribute("aria-busy", "false");

  $("#line-list").innerHTML = data.lines.map(l => `<li class="log-explanation ${l.abnormal ? "abnormal" : ""}"><h3>Line ${l.line}${l.attempt ? ` <small>attempt ${l.attempt}</small>` : (l.context_for ? ` <small>just before attempt ${l.context_for}</small>` : "")}</h3><p><strong>Log:</strong> <span class="source-text">${esc(l.raw)}</span></p><p><strong>Plain English:</strong> ${esc(l.explanation)}${l.status !== "parsed" ? " <em>(not fully understood)</em>" : ""}</p></li>`).join("");
  $("#review-count").textContent = data.lines.length + " entries";
  $("#source-notes").innerHTML = "";
  $("#line-review").hidden = false;
}

function showError(message) {
  activeAnalysis = null;
  updateFeedbackControls();
  $("#results").hidden = true;
  $("#error").textContent = message;
  $("#error").hidden = false;
}

function reset() {
  controller?.abort();
  activeAnalysis = null;
  updateFeedbackControls();
  if ($("#feedback-dialog").open) $("#feedback-dialog").close();
  $("#results").hidden = true; $("#results").innerHTML = "";
  $("#results-placeholder").hidden = false; $("#error").hidden = true;
  $("#line-review").hidden = true; $("#line-list").innerHTML = "";
  $("#analyze-button").disabled = !$("#logs").value.trim();
}

function updateCount(resetScroll = false) {
  $("#line-count").textContent = $("#logs").value.split(/\r?\n/).filter(l => l.trim()).length + " lines";
  renderEditorSource({resetScroll});
}

async function analyze() {
  const logs = $("#logs").value;
  if (!logs.trim()) return;
  controller?.abort();
  const requestController = new AbortController();
  controller = requestController;
  activeAnalysis = null;
  updateFeedbackControls();
  $("#error").hidden = true; $("#results-placeholder").hidden = true;
  $("#results").hidden = false; $("#results").setAttribute("aria-busy", "true");
  $("#results").innerHTML = '<p class="report-loading" role="status">Reading each log line and comparing the error-code store…</p>';
  $("#analyze-button").disabled = true;
  try {
    const response = await fetch(apiUrl("/api/analyze"), {
      method: "POST", headers: {"Content-Type": "application/json", Accept: "application/json"},
      body: JSON.stringify({logs, use_llm: true}), signal: requestController.signal, cache: "no-store",
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "The logs could not be analyzed.");
    if (requestController === controller && !requestController.signal.aborted) render(data);
  } catch (error) {
    if (requestController === controller && !requestController.signal.aborted && error.name !== "AbortError") showError(error.message);
  } finally {
    $("#analyze-button").disabled = !$("#logs").value.trim();
  }
}

async function loadSampleList() {
  try {
    const res = await fetch(apiUrl("/api/samples"), {cache: "no-store"});
    if (!res.ok) return;
    const data = await res.json();
    const picker = $("#sample-picker");
    for (const group of data.groups) {
      const og = document.createElement("optgroup");
      og.label = `${group.label} (${group.samples.length})`;
      for (const s of group.samples) {
        const opt = document.createElement("option");
        opt.value = s.id;
        opt.textContent = `${s.label} · ${s.lines} lines`;
        og.appendChild(opt);
      }
      picker.appendChild(og);
    }
  } catch { /* the picker simply stays empty; pasting still works */ }
}

async function loadSample(id) {
  if (!id) return;
  try {
    const res = await fetch(apiUrl("/api/samples/" + encodeURIComponent(id)), {cache: "no-store"});
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "The sample could not be loaded.");
    $("#logs").value = data.logs;
    updateCount(true);
    reset();
  } catch (error) {
    showError(error.message);
  }
}

$("#sample-picker").addEventListener("change", event => loadSample(event.target.value));
$("#thumb-up").addEventListener("click", () => saveClientFeedback({rating: "up"}));
$("#thumb-down").addEventListener("click", async () => {
  if (await saveClientFeedback({rating: "down"})) openFeedback();
});
$("#feedback-button").addEventListener("click", openFeedback);
$("#feedback-close").addEventListener("click", () => $("#feedback-dialog").close());
$("#feedback-cancel").addEventListener("click", () => $("#feedback-dialog").close());
$("#feedback-form").addEventListener("submit", event => {
  event.preventDefault();
  const feedback = $("#feedback-resolution").value.trim();
  if (!feedback) return;
  saveClientFeedback({feedback}, true);
});
$("#logs").addEventListener("input", () => { $("#sample-picker").value = ""; updateCount(); reset(); });
$("#logs").addEventListener("paste", () => requestAnimationFrame(() => { $("#logs").scrollTop = 0; syncEditorScroll(); }));
$("#logs").addEventListener("scroll", syncEditorScroll);
new ResizeObserver(() => renderEditorSource()).observe($(".log-source"));
$("#analyze-form").addEventListener("submit", event => { event.preventDefault(); analyze(); });
$("#clear-button").addEventListener("click", () => { $("#logs").value = ""; $("#sample-picker").value = ""; updateCount(true); reset(); });
loadSampleList();
updateCount(true);
reset();
