"use strict";

// ─── состояние ────────────────────────────────────────────────────────────────

const state = {
  config: null,
  agents: [],
  runId: null,
  since: 0,
  timer: null,
  ticker: null,
  startedAt: 0,
  report: null,
  markdown: "",
  skipClarify: false,
};

const PRESETS = ["Backend Python Developer", "ML Engineer", "iOS Developer (Swift)", "Data Engineer"];

const LEVELS = {
  "critical": { text: "необходимый", cls: "red", order: 0 },
  "important": { text: "большой спрос", cls: "yellow", order: 1 },
  "nice-to-have": { text: "небольшой спрос", cls: "neutral", order: 2 },
};

const TRENDS = {
  "growing": { text: "растёт", cls: "green", order: 0 },
  "stable": { text: "стабильно", cls: "blue", order: 1 },
  "declining": { text: "снижается", cls: "red", order: 2 },
};

const CATEGORIES = {
  languages: "Языки программирования",
  frameworks: "Фреймворки и библиотеки",
  infrastructure: "Инфраструктура",
  soft_skills: "Soft skills",
};

const GRADES = ["junior", "middle", "senior", "lead"];

// порядок вкладок отчёта; на первых четырёх внизу появляется переход к следующей
const TAB_FLOW = ["skills", "salary", "learning", "portfolio", "quality", "source"];

const CRITERIA = {
  salary_market_match: "Зарплаты соответствуют рынку",
  skills_consistency: "Согласованность навыков",
  learning_path_quality: "Качество плана обучения",
  portfolio_relevance: "Релевантность портфолио",
};

// ─── утилиты ─────────────────────────────────────────────────────────────────

const $ = (id) => document.getElementById(id);

function esc(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function num(value) {
  if (value === null || value === undefined || value === "") return "—";
  return new Intl.NumberFormat("ru-RU").format(value);
}

function secs(value) {
  if (value === null || value === undefined) return "—";
  return `${Number(value).toFixed(1)} с`;
}

function badge(text, cls) {
  return `<span class="badge ${cls || "neutral"}">${esc(text)}</span>`;
}

function dateLabel(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

function safeUrl(url) {
  if (typeof url !== "string") return null;
  return /^https?:\/\//i.test(url.trim()) ? url.trim() : null;
}

// Публичное демо можно закрыть токеном (DEMO_TOKEN на Vercel) — тогда ссылка
// раздаётся в виде https://<домен>/?token=...
const DEMO_TOKEN = new URLSearchParams(location.search).get("token") || "";

async function post(path, body) {
  const headers = { "Content-Type": "application/json" };
  if (DEMO_TOKEN) headers["X-Demo-Token"] = DEMO_TOKEN;
  return api(path, { method: "POST", headers, body: JSON.stringify(body) });
}

async function api(path, options) {
  const res = await fetch(path, options);
  let data = null;
  try {
    data = await res.json();
  } catch (e) {
    data = null;
  }
  if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
  return data;
}

function download(name, text, type) {
  const blob = new Blob([text], { type: type || "text/plain;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = name;
  link.click();
  URL.revokeObjectURL(link.href);
}

// ─── инициализация ───────────────────────────────────────────────────────────

async function init() {
  $("role-chips").innerHTML = PRESETS.map((p) => `<button class="chip" data-preset="${esc(p)}">${esc(p)}</button>`).join("");
  $("role-chips").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-preset]");
    if (!btn) return;
    $("role").value = btn.dataset.preset;
    state.skipClarify = true;
    hide($("suggest-box"));
  });

  $("run-btn").addEventListener("click", startRun);
  $("role").addEventListener("input", () => { state.skipClarify = false; });
  $("role").addEventListener("keydown", (e) => { if (e.key === "Enter") startRun(); });
  $("tabs").addEventListener("click", onTabClick);
  $("report-panel").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-goto]");
    if (!btn) return;
    selectTab(btn.dataset.goto);
    $("tabs").scrollIntoView({ behavior: "smooth", block: "start" });
  });
  ["role", "skill-level", "goal"].forEach((id) => fillHintOnTab($(id)));
  $("suggest-chips").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-suggest]");
    if (!btn) return;
    $("role").value = btn.dataset.suggest;
    state.skipClarify = true;
    hide($("suggest-box"));
    startRun();
  });

  try {
    state.config = await api("/api/config");
  } catch (e) {
    showNotice(`Не удалось получить конфигурацию: ${e.message}`, true);
    return;
  }

  state.agents = state.config.agents || [];
  renderExamples(state.config.examples || []);

  const runEnabled = state.config.run_enabled !== undefined
    ? state.config.run_enabled
    : state.config.llm_ready;

  if (!runEnabled) {
    showNotice(
      `${state.config.llm_error || "Живой запуск недоступен"} — открой готовый отчёт из examples/.`,
      true
    );
    $("run-btn").disabled = true;
    $("run-hint").textContent = "живой запуск выключен";
  } else if (state.config.needs_token && !DEMO_TOKEN) {
    showNotice("Демо закрыто токеном — нужна ссылка вида ?token=... Готовые отчёты доступны без токена.", true);
    $("run-btn").disabled = true;
    $("run-hint").textContent = "нужен токен в ссылке";
  }

  loadStats();
  observeReveals();
}

/** Tab в пустом поле подставляет пример из placeholder, а не уводит фокус. */
function fillHintOnTab(field) {
  if (!field) return;
  field.addEventListener("keydown", (e) => {
    if (e.key !== "Tab" || e.shiftKey || e.ctrlKey || e.altKey || e.metaKey) return;
    const hint = field.placeholder || "";
    if (!hint || field.value.trim() !== "") return;
    e.preventDefault();
    field.value = hint;
    field.setSelectionRange(hint.length, hint.length);
    if (field.id === "role") state.skipClarify = PRESETS.includes(hint);
  });
}

function showNotice(text, isError) {
  const box = $("llm-notice");
  box.textContent = text;
  box.className = `notice${isError ? " error" : ""}`;
}

function hide(node) { node.classList.add("hidden"); }
function show(node) { node.classList.remove("hidden"); }

function observeReveals() {
  const items = document.querySelectorAll(".reveal");
  if (!("IntersectionObserver" in window)) {
    items.forEach((n) => n.classList.add("shown"));
    return;
  }
  const io = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting) {
        entry.target.classList.add("shown");
        io.unobserve(entry.target);
      }
    });
  }, { threshold: 0.08 });
  items.forEach((n) => io.observe(n));
}

// ─── готовые примеры ─────────────────────────────────────────────────────────

function renderExamples(items) {
  const box = $("examples");
  if (!items.length) {
    box.innerHTML = `<p class="muted">Папка examples/ пуста.</p>`;
    return;
  }
  box.innerHTML = items.map((item) => `
    <button class="example" data-example="${esc(item.id)}">
      <strong>${esc(item.role)}</strong>
      <span class="mono">${esc(item.id)} · score ${esc(item.quality_score ?? "—")} · ${secs(item.elapsed_sec)} · ${num(item.total_tokens)} токенов</span>
    </button>`).join("");

  box.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-example]");
    if (!btn) return;
    btn.disabled = true;
    try {
      const data = await api(`/api/examples/${encodeURIComponent(btn.dataset.example)}`);
      hide($("pipeline-panel"));
      renderReport(data.report, data.markdown, { source: `Готовый отчёт · ${btn.dataset.example}` });
    } catch (err) {
      showNotice(`Не удалось открыть пример: ${err.message}`, true);
    } finally {
      btn.disabled = false;
    }
  });
}

// ─── запуск pipeline ─────────────────────────────────────────────────────────

async function startRun() {
  if ($("run-btn").disabled) return;
  const role = $("role").value.trim();
  if (!role) {
    $("run-status").textContent = "укажи специальность";
    $("role").focus();
    return;
  }

  $("run-btn").disabled = true;
  hide($("suggest-box"));
  hide($("report-panel"));
  hide($("pipeline-error"));

  try {
    if (!state.skipClarify) {
      $("run-status").innerHTML = `<span class="spinner"></span> проверяю роль`;
      const clarified = await post("/api/clarify", { role });
      if (clarified.suggestions && clarified.suggestions.length) {
        $("suggest-chips").innerHTML = clarified.suggestions
          .map((s) => `<button class="chip" data-suggest="${esc(s)}">${esc(s)}</button>`).join("") +
          `<button class="chip" data-suggest="${esc(role)}">Оставить «${esc(role)}»</button>`;
        show($("suggest-box"));
        $("run-status").textContent = "";
        $("run-btn").disabled = false;
        return;
      }
    }

    $("run-status").innerHTML = `<span class="spinner"></span> запускаю агентов`;

    // На Vercel нет долгоживущего процесса и полный прогон не влезает в лимит
    // функции, поэтому агенты вызываются по одному, а контекст живёт здесь.
    if (state.config.mode === "serverless") return runStepwise(role);

    const started = await post("/api/run", {
      role,
      skill_level: $("skill-level").value.trim(),
      goal: $("goal").value.trim(),
    });

    state.runId = started.run_id;
    state.since = 0;
    state.startedAt = Date.now();
    prepPipeline(role);
    state.timer = setInterval(poll, 700);
    state.ticker = setInterval(tick, 200);
    poll();
  } catch (e) {
    $("run-status").textContent = "";
    $("run-btn").disabled = false;
    showNotice(e.message, true);
  }
}

/** Пошаговый прогон: один HTTP-запрос на агента, контекст накапливается здесь. */
async function runStepwise(role) {
  const context = {
    role,
    generated_at: new Date().toISOString(),
    skill_level: $("skill-level").value.trim(),
    goal: $("goal").value.trim(),
    _agent_timings: {},
    _agent_tokens: {},
  };
  const usageTotal = { input_tokens: 0, output_tokens: 0, total_tokens: 0, calls: 0 };
  const elapsed = () => Number(((Date.now() - state.startedAt) / 1000).toFixed(2));

  state.startedAt = Date.now();
  state.liveTokens = 0;
  prepPipeline(role);
  state.ticker = setInterval(tick, 200);

  try {
    for (const agent of state.agents) {
      setAgentRunning(agent.name);
      context._pipeline_elapsed = elapsed();
      const step = await post("/api/agent", { name: agent.name, context, usage_total: usageTotal });
      Object.assign(context, step.result);
      context._agent_timings[agent.name] = step.elapsed_sec;
      context._agent_tokens[agent.name] = step.tokens;
      Object.keys(usageTotal).forEach((key) => {
        usageTotal[key] += (step.usage && step.usage[key]) || 0;
      });
      setAgentDone(agent.name, step.elapsed_sec, step.tokens);
    }

    context._pipeline_elapsed = elapsed();
    const finished = await post("/api/finish", { context });

    stopPolling();
    $("run-status").textContent = "готово";
    $("run-btn").disabled = false;
    $("pipeline-meta").textContent = `${secs(context._pipeline_elapsed)} · ${num(usageTotal.total_tokens)} токенов`;
    renderReport(context, finished.markdown, { source: "Живой запуск" });
    loadStats();
  } catch (e) {
    stopPolling();
    $("run-status").textContent = "";
    $("run-btn").disabled = false;
    const running = state.agents.find((a) => {
      const row = agentRow(a.name);
      return row && row.dataset.state === "running";
    });
    if (running) setAgentError(running.name);
    const box = $("pipeline-error");
    box.textContent = e.message;
    show(box);
  }
}

function prepPipeline(role) {
  $("pipeline-role").textContent = role;
  $("pipeline-meta").textContent = "0.0 с · 0 токенов";
  $("progress").style.width = "0%";
  $("agent-list").innerHTML = state.agents.map((agent, i) => `
    <div class="agent-row" data-agent="${esc(agent.name)}" data-state="pending" style="--index:${i}">
      <div class="idx">${String(i + 1).padStart(2, "0")}</div>
      <div>
        <div class="name">${esc(agent.label)}</div>
        <div class="about">${esc(agent.about)}</div>
        <div class="output">→ ${esc(agent.output)}</div>
      </div>
      <div class="metrics">
        ${badge("ожидает", "neutral")}
        <span class="mono metric"></span>
      </div>
    </div>`).join("");
  show($("pipeline-panel"));
  $("pipeline-panel").scrollIntoView({ behavior: "smooth", block: "start" });
}

function tick() {
  const elapsed = (Date.now() - state.startedAt) / 1000;
  const tokens = state.liveTokens || 0;
  $("pipeline-meta").textContent = `${elapsed.toFixed(1)} с · ${num(tokens)} токенов`;
}

async function poll() {
  if (!state.runId) return;
  let snap;
  try {
    snap = await api(`/api/run/${state.runId}?since=${state.since}`);
  } catch (e) {
    return; // сеть моргнула — попробуем на следующем тике
  }

  state.since = snap.event_count;
  (snap.events || []).forEach(applyEvent);

  if (snap.status === "done") {
    stopPolling();
    $("run-status").textContent = "готово";
    $("run-btn").disabled = false;
    $("pipeline-meta").textContent = `${secs(snap.report._pipeline_elapsed)} · ${num(
      (snap.report.run_stats && snap.report.run_stats.tokens && snap.report.run_stats.tokens.total_tokens) || state.liveTokens
    )} токенов`;
    renderReport(snap.report, snap.markdown, { source: "Живой запуск" });
    loadStats();
  } else if (snap.status === "error") {
    stopPolling();
    $("run-status").textContent = "";
    $("run-btn").disabled = false;
    const box = $("pipeline-error");
    box.textContent = snap.error || "Запуск завершился с ошибкой";
    show(box);
  }
}

function stopPolling() {
  clearInterval(state.timer);
  clearInterval(state.ticker);
  state.timer = null;
  state.ticker = null;
}

function agentRow(name) {
  return name ? $("agent-list").querySelector(`[data-agent="${name}"]`) : null;
}

function setAgentRunning(name) {
  const row = agentRow(name);
  if (!row) return;
  row.dataset.state = "running";
  row.querySelector(".badge").outerHTML = `<span class="badge blue pulse">в работе</span>`;
}

function setAgentDone(name, elapsedSec, tokens) {
  const row = agentRow(name);
  if (!row) return;
  row.dataset.state = "done";
  row.querySelector(".badge").outerHTML = badge("готово", "green");
  row.querySelector(".metric").textContent = `${secs(elapsedSec)} · ${num(tokens)} токенов`;
  state.liveTokens = (state.liveTokens || 0) + (tokens || 0);
  const done = $("agent-list").querySelectorAll('[data-state="done"]').length;
  $("progress").style.width = `${(done / state.agents.length) * 100}%`;
}

function setAgentError(name) {
  const row = agentRow(name);
  if (!row) return;
  row.dataset.state = "error";
  row.querySelector(".badge").outerHTML = badge("ошибка", "red");
}

function applyEvent(evt) {
  const payload = evt.payload || {};

  if (evt.event === "agent_start") setAgentRunning(payload.name);
  if (evt.event === "agent_done") setAgentDone(payload.name, payload.elapsed_sec, payload.tokens);
  if (evt.event === "agent_error" || evt.event === "pipeline_error") setAgentError(payload.name);
}

// ─── отчёт ───────────────────────────────────────────────────────────────────

function renderReport(report, markdown, meta) {
  if (!report) return;
  state.report = report;
  state.markdown = markdown || "";
  state.liveTokens = 0;

  const critic = report.critic_result || {};
  $("report-eyebrow").textContent = (meta && meta.source) || "Карьерный отчёт";
  $("report-role").textContent = report.role || "";
  $("report-score").textContent = critic.quality_score ?? "—";
  $("report-consistent").outerHTML = critic.is_consistent
    ? `<span class="badge green" id="report-consistent">согласован</span>`
    : `<span class="badge yellow" id="report-consistent">требует внимания</span>`;

  const tokens = (report.run_stats && report.run_stats.tokens) || {};
  const chips = [
    badge(dateLabel(report.generated_at), "value"),
    badge(secs(report._pipeline_elapsed), "value"),
    badge(`${num(tokens.total_tokens)} токенов`, "value"),
  ];
  if (report.skill_level) chips.push(badge(`уровень: ${report.skill_level}`, "text"));
  if (report.goal) chips.push(badge(`цель: ${report.goal}`, "text"));
  $("report-meta").innerHTML = chips.join("");

  $("panel-skills").innerHTML = renderSkills(report.skill_map || {});
  $("panel-salary").innerHTML = renderSalary(report.salary_table || {});
  $("panel-learning").innerHTML = renderLearning(report.learning_path || {});
  $("panel-portfolio").innerHTML = renderPortfolio((report.learning_path || {}).portfolio_project || {});
  $("panel-quality").innerHTML = renderQuality(critic, report);
  $("panel-source").innerHTML = renderSource();

  document.querySelectorAll(".tab-panel").forEach(wrapTables);
  renderNextButtons();

  $("panel-source").querySelectorAll("[data-dl]").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.dataset.dl === "md") download("report.md", state.markdown, "text/markdown;charset=utf-8");
      else download("report.json", JSON.stringify(state.report, null, 2), "application/json;charset=utf-8");
    });
  });

  selectTab("skills");
  show($("report-panel"));
  $("report-panel").scrollIntoView({ behavior: "smooth", block: "start" });
}

function onTabClick(e) {
  const tab = e.target.closest(".tab");
  if (tab) selectTab(tab.dataset.tab);
}

function selectTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.tab === name)));
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `panel-${name}`));
}

/** Внизу первых четырёх вкладок — переход к следующему шагу отчёта. */
function renderNextButtons() {
  const labels = {};
  document.querySelectorAll(".tab").forEach((tab) => {
    labels[tab.dataset.tab] = tab.textContent.trim();
  });

  TAB_FLOW.slice(0, 4).forEach((name, i) => {
    const next = TAB_FLOW[i + 1];
    const panel = $(`panel-${name}`);
    if (!panel || !next) return;
    const nav = document.createElement("div");
    nav.className = "panel-nav";
    nav.innerHTML = `<button class="btn-next" data-goto="${esc(next)}">
      <span class="label">
        <span class="hint">Дальше</span>
        <span class="step">${esc(labels[next] || next)}</span>
      </span>
      <span class="arrow" aria-hidden="true">&rarr;</span>
    </button>`;
    panel.appendChild(nav);
  });
}

/** Узкий экран: широкая таблица прокручивается сама, а не растягивает страницу. */
function wrapTables(root) {
  root.querySelectorAll("table.data").forEach((table) => {
    if (table.parentElement && table.parentElement.classList.contains("table-scroll")) return;
    const scroller = document.createElement("div");
    scroller.className = "table-scroll";
    table.parentNode.insertBefore(scroller, table);
    scroller.appendChild(table);
  });
}

function blockTitle(title, note) {
  return `<div class="block-title"><h3>${esc(title)}</h3><div class="rule"></div>${note ? `<span class="mono">${esc(note)}</span>` : ""}</div>`;
}

function renderSkills(skillMap) {
  const order = (skill) =>
    (LEVELS[skill.level] ? LEVELS[skill.level].order : 9) * 10 + (TRENDS[skill.trend] ? TRENDS[skill.trend].order : 9);

  const blocks = Object.entries(CATEGORIES).map(([key, title]) => {
    const skills = [...(skillMap[key] || [])].sort((a, b) => order(a) - order(b));
    if (!skills.length) return "";
    const isSoft = key === "soft_skills";
    const rows = skills.map((s) => {
      const lvl = LEVELS[s.level] || { text: s.level, cls: "neutral" };
      const trd = TRENDS[s.trend] || { text: s.trend, cls: "neutral" };
      return `<tr>
        <td class="name">${esc(s.name)}</td>
        <td>${badge(lvl.text, lvl.cls)}</td>
        <td>${badge(trd.text, trd.cls)}</td>
        ${isSoft ? "" : `<td class="reason">${esc(s.trend_reason)}</td>`}
      </tr>`;
    }).join("");

    return `<div class="block">
      ${blockTitle(title, `${skills.length} навыков`)}
      <table class="data">
        <thead><tr><th>Навык</th><th>Востребованность</th><th>Тренд</th>${isSoft ? "" : "<th>Почему</th>"}</tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
  });

  return blocks.join("") || `<p class="muted">Нет данных по навыкам.</p>`;
}

function renderSalary(table) {
  const trend = TRENDS[table.market_trend] || { text: table.market_trend, cls: "neutral" };

  const money = (range, suffix) => {
    if (!range) return `<td class="num">—</td>`;
    return `<td class="num"><b>${num(range.median)}</b> ${esc(suffix)}<br><span class="muted">${num(range.min)}–${num(range.max)}</span></td>`;
  };

  const rows = GRADES.map((grade) => {
    const data = table[grade] || {};
    return `<tr>
      <td class="name">${grade.charAt(0).toUpperCase() + grade.slice(1)}</td>
      ${money(data.moscow, "тыс ₽")}
      ${money(data.regions_rub, "тыс ₽")}
      ${money(data.remote_usd, "$")}
    </tr>`;
  }).join("");

  const employers = (table.top_employers || []).map((e) => `
    <div class="card alt">
      <h4>${esc(e.name)}</h4>
      <div style="margin-bottom:10px">${badge(e.type, "blue")}</div>
      <p class="muted" style="font-size:13px">${esc(e.description)}</p>
    </div>`).join("");

  return `
    <div class="block">
      ${blockTitle("Зарплаты по грейдам", "медиана и диапазон")}
      <div style="margin-bottom:18px">
        ${badge(`рынок: ${trend.text}`, trend.cls)}
        <p class="sub" style="margin-top:16px">Почему?</p>
        <p class="muted" style="font-size:14px">${esc(table.market_trend_reason)}</p>
      </div>
      <table class="data">
        <thead><tr><th>Грейд</th><th>Москва</th><th>Регионы РФ</th><th>Remote</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <p class="mono" style="margin-top:14px">Источник грейдов — вакансии hh.ru API</p>
    </div>
    ${employers ? `<div class="block">${blockTitle("Топ работодателей")}<div class="cards three">${employers}</div></div>` : ""}`;
}

function renderLearning(path) {
  const phases = (path.phases || []).map((phase, i) => {
    const topics = (phase.topics || []).map((t) => `<span class="chip" style="cursor:default">${esc(t)}</span>`).join("");
    const steps = (phase.path || []).map((s) => `<li>${esc(s)}</li>`).join("");
    const projects = (phase.practice_projects || []).map((p) => `
      <div class="card alt">
        <h4>${esc(p.name)}</h4>
        <p class="muted" style="font-size:13px">${esc(p.description)}</p>
      </div>`).join("");
    const resources = [...(phase.resources || [])]
      .sort((a, b) => (a.is_free === false ? 1 : 0) - (b.is_free === false ? 1 : 0))
      .map((r) => {
        const url = safeUrl(r.url);
        const name = url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(r.name)}</a>` : esc(r.name);
        return `<li>${name} <span class="mono">${esc(r.type)}</span> ${badge(r.is_free === false ? "платно" : "бесплатно", r.is_free === false ? "yellow" : "green")}</li>`;
      }).join("");

    return `<details class="phase">
      <summary class="phase-head">
        <h3>Фаза ${i + 1}: ${esc(phase.name)}</h3>
        <span class="mono">${esc(phase.duration_days)} дней</span>
        <span class="chev" aria-hidden="true"></span>
      </summary>
      <div class="phase-body">
        <p class="sub">Темы</p>
        <div class="chips">${topics}</div>
        <p class="sub">Маршрут</p>
        <ol class="steps">${steps}</ol>
        <p class="sub">Milestone</p>
        <p class="callout">${esc(phase.milestone)}</p>
        <p class="sub">Практика</p>
        <div class="cards three">${projects}</div>
        <p class="sub">Ресурсы</p>
        <ul class="clean">${resources}</ul>
      </div>
    </details>`;
  }).join("");

  const gap = path.gap_analysis || {};
  const list = (items) => `<ul class="clean">${(items || []).map((i) => `<li>${esc(i)}</li>`).join("")}</ul>`;

  return `
    <div class="block">${blockTitle("План на 90 дней", "три фазы по 30 дней")}${phases || `<p class="muted">Нет данных.</p>`}</div>
    <div class="block">
      ${blockTitle("Gap-анализ")}
      <div class="cards two">
        <div class="card"><h4>Quick wins</h4><p class="mono" style="margin-bottom:12px">2–4 недели</p>${list(gap.quick_wins)}</div>
        <div class="card"><h4>Long term</h4><p class="mono" style="margin-bottom:12px">3+ месяца</p>${list(gap.long_term)}</div>
      </div>
    </div>`;
}

function renderPortfolio(project) {
  if (!project.name) return `<p class="muted">Портфолио-проект не сформирован.</p>`;
  const list = (items) => `<ul class="clean">${(items || []).map((i) => `<li>${esc(i)}</li>`).join("")}</ul>`;
  const skills = (project.skills_demonstrated || []).map((s) => `<span class="chip" style="cursor:default">${esc(s)}</span>`).join("");

  return `
    <div class="block">
      ${blockTitle("Портфолио-проект")}
      <div class="card">
        <h4 style="font-size:22px">${esc(project.name)}</h4>
        <p class="callout" style="margin-top:12px">${esc(project.problem)}</p>
        <p class="sub">Сценарии использования</p>
        ${list(project.user_stories)}
        <p class="sub">Технические задачи</p>
        ${list(project.technical_challenges)}
        <p class="sub">Навыки в проекте</p>
        <div class="chips">${skills}</div>
      </div>
    </div>`;
}

function renderQuality(critic, report) {
  const breakdown = critic.score_breakdown || {};
  const rows = Object.entries(CRITERIA).map(([key, label]) => {
    const item = breakdown[key] || {};
    const score = item.score || 0;
    return `<div class="score-row">
      <div class="top"><span>${esc(label)}</span><span class="mono">${score}/25</span></div>
      <div class="bar"><i style="width:${(score / 25) * 100}%"></i></div>
      <p class="muted" style="font-size:13px">${esc(item.reason)}</p>
    </div>`;
  }).join("");

  const warnings = critic.warnings || [];
  const timings = report._agent_timings || {};
  const tokensByAgent = report._agent_tokens || {};
  const perAgent = state.agents.length
    ? state.agents
    : Object.keys(timings).map((name) => ({ name, label: name }));

  const agentRows = perAgent.map((agent) => `
    <tr>
      <td class="name">${esc(agent.label || agent.name)}</td>
      <td class="num">${secs(timings[agent.name])}</td>
      <td class="num">${num(tokensByAgent[agent.name])}</td>
    </tr>`).join("");

  return `
    <div class="block">
      ${blockTitle("Разбор оценки", `${critic.quality_score ?? "—"}/100`)}
      <div class="score-rows">${rows}</div>
      <p class="callout" style="margin-top:24px">${esc(critic.quality_score_reason)}</p>
    </div>
    <div class="block">
      ${blockTitle("Замечания критика", `${warnings.length}`)}
      ${warnings.length
        ? `<ul class="clean">${warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>`
        : `<p class="muted">Критик не нашёл противоречий.</p>`}
    </div>
    <div class="block">
      ${blockTitle("Цена запуска", "время и токены по агентам")}
      <table class="data">
        <thead><tr><th>Агент</th><th>Время</th><th>Токены</th></tr></thead>
        <tbody>${agentRows}</tbody>
      </table>
    </div>`;
}

function renderSource() {
  return `
    <div class="block">
      ${blockTitle("Файлы отчёта", "report.md · report.json")}
      <div class="actions" style="margin-bottom:22px">
        <button class="btn-ghost" data-dl="md">Скачать report.md</button>
        <button class="btn-ghost" data-dl="json">Скачать report.json</button>
      </div>
      <pre class="source">${esc(state.markdown || "report.md недоступен")}</pre>
    </div>`;
}

// ─── статистика ──────────────────────────────────────────────────────────────

async function loadStats() {
  let data;
  try {
    data = await api("/api/stats");
  } catch (e) {
    $("stats-body").innerHTML = `<p class="muted">Статистика недоступна.</p>`;
    return;
  }

  const s = data.summary || {};
  if (!s.total_runs) {
    $("stats-body").innerHTML = `<p class="muted">stats.json пуст — статистика появится после первого запуска.</p>`;
    return;
  }

  const kpis = [
    ["Запусков", num(s.total_runs)],
    ["Успешных", `${s.success_rate_pct ?? "—"}%`],
    ["Среднее время", secs(s.avg_elapsed_sec)],
    ["Средние токены", num(s.avg_total_tokens)],
    ["Средний score", s.avg_quality_score ?? "—"],
  ].map(([label, value]) => `<div class="kpi"><span class="value">${esc(value)}</span><span class="label">${esc(label)}</span></div>`).join("");

  const labels = {};
  (state.agents || []).forEach((a) => { labels[a.name] = a.label; });

  const maxTime = Math.max(...(s.agents || []).map((a) => a.avg_elapsed_sec || 0), 1);
  const agentRows = (s.agents || []).map((a) => `
    <tr>
      <td class="name">${esc(labels[a.name] || a.name)}</td>
      <td class="num">${secs(a.avg_elapsed_sec)}</td>
      <td style="width:38%"><div class="bar"><i style="width:${((a.avg_elapsed_sec || 0) / maxTime) * 100}%"></i></div></td>
      <td class="num">${num(a.avg_tokens)}</td>
    </tr>`).join("");

  const history = (data.history || []).map((r) => `
    <tr>
      <td class="name">${esc(r.role)}</td>
      <td class="num">${esc(dateLabel(r.generated_at))}</td>
      <td class="num">${secs(r.total_elapsed_sec)}</td>
      <td class="num">${num(r.tokens && r.tokens.total_tokens)}</td>
      <td>${r.quality_score != null ? badge(`${r.quality_score}/100`, r.quality_score >= 85 ? "green" : "yellow") : badge("—", "neutral")}</td>
    </tr>`).join("");

  $("stats-body").innerHTML = `
    <div class="kpis" style="margin-bottom:34px">${kpis}</div>
    <div class="block">
      ${blockTitle("Среднее по агентам")}
      <table class="data">
        <thead><tr><th>Агент</th><th>Время</th><th></th><th>Токены</th></tr></thead>
        <tbody>${agentRows}</tbody>
      </table>
    </div>
    <div class="block">
      ${blockTitle("Последние запуски")}
      <table class="data">
        <thead><tr><th>Роль</th><th>Когда</th><th>Время</th><th>Токены</th><th>Score</th></tr></thead>
        <tbody>${history}</tbody>
      </table>
    </div>`;

  wrapTables($("stats-body"));
}

document.addEventListener("DOMContentLoaded", init);
