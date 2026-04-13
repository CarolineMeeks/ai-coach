const state = {
  date: "",
  coach: null,
  trends: null,
  fatloss: null,
  zepbound: null,
  water: null,
};

const dateInput = document.querySelector("#date-input");
const metricGrid = document.querySelector("#metric-grid");
const summaryCard = document.querySelector("#summary-card");
const cacheBanner = document.querySelector("#cache-banner");
const trendNotes = document.querySelector("#trend-notes");
const chatLog = document.querySelector("#chat-log");
const historyLog = document.querySelector("#history-log");
const chatForm = document.querySelector("#chat-form");
const chatInput = document.querySelector("#chat-input");
const chatSubmit = document.querySelector("#chat-submit");
const template = document.querySelector("#message-template");
const reconnectUrl = "/connect-fitbit";

function addMessage(speaker, content) {
  const fragment = template.content.cloneNode(true);
  fragment.querySelector(".speaker").textContent = speaker;
  fragment.querySelector(".content").textContent = content;
  chatLog.appendChild(fragment);
  chatLog.scrollTop = chatLog.scrollHeight;
}

function setPanelLoading(element, isLoading) {
  element.dataset.loading = isLoading ? "true" : "false";
}

function renderHistory(items) {
  if (!items.length) {
    historyLog.innerHTML = "<p>No interactions logged yet.</p>";
    return;
  }
  historyLog.innerHTML = items
    .slice()
    .reverse()
    .map(
      (item) => `
        <div class="history-item">
          <p class="history-meta">${item.timestamp} · ${item.source} · ${item.topic}</p>
          <p><strong>You:</strong> ${item.message}</p>
          <p><strong>Coach:</strong> ${item.reply}</p>
        </div>
      `
    )
    .join("");
}

function setSummary(payload) {
  setPanelLoading(summaryCard, false);
  const readiness = payload.coach.readiness;
  const prescription = payload.coach.prescription;
  const primaryGoal = payload.coach.primary_goal;
  const exerciseGoal = payload.coach.exercise_goal;
  const fatloss = state.fatloss?.verdict || "loading";
  let zepboundText = "loading";
  if (state.zepbound) {
    zepboundText = state.zepbound.goal_status.shot_logged.summary;
  }
  const goalStatus = primaryGoal.status.replaceAll("_", " ");
  const exerciseStatus = exerciseGoal.status.replaceAll("_", " ");
  const goalSummary = primaryGoal.kind === "recovery_walk"
    ? `${primaryGoal.actual} / ${primaryGoal.target} movement minutes`
    : `${primaryGoal.actual} / ${primaryGoal.target} ${primaryGoal.unit}`;
  summaryCard.innerHTML = `
    <p class="label">Today</p>
    <h2>${readiness.toUpperCase()}</h2>
    <p>${prescription}</p>
    <div class="goal-callout" data-status="${primaryGoal.status}">
      <p class="goal-eyebrow">Main Goal</p>
      <h3>${primaryGoal.label}</h3>
      <p>${primaryGoal.reason}</p>
      <p class="goal-meta">Status: ${goalStatus}. Progress: ${goalSummary}.</p>
      <p class="goal-meta">Combined exercise goal: ${exerciseStatus}.</p>
    </div>
    <p class="meta">Fat-loss read: ${fatloss}. Zepbound: ${zepboundText}.</p>
  `;
  const cache = payload.cache_status;
  cacheBanner.hidden = false;
  cacheBanner.textContent = cache.message;
  cacheBanner.dataset.mode = cache.used_stale ? "stale" : cache.used_cache ? "cache" : "fresh";
}

function setMetrics() {
  setPanelLoading(metricGrid, false);
  if (!state.coach || !state.fatloss || !state.zepbound) {
    return;
  }
  const stats = state.coach.stats;
  const latest = state.fatloss.latest;
  const zep = state.zepbound;
  const water = state.water;
  const metrics = [
    ["Main Goal", `${state.coach.primary_goal.label} (${state.coach.primary_goal.status.replaceAll("_", " ")})`],
    ["Exercise Goal", `${state.coach.exercise_goal.label}: ${state.coach.exercise_goal.status.replaceAll("_", " ")}`],
    ["Zone Minutes", `${stats.zone_minutes}`],
    ["Zone Split", `${stats.fat_burn_zone_minutes}/${stats.cardio_zone_minutes}/${stats.peak_zone_minutes}`],
    ["Steps", `${stats.steps} / ${stats.step_goal}`],
    ["Sleep", `${stats.sleep} (${stats.sleep_efficiency}% eff.)`],
    ["Resting HR", `${stats.resting_hr}`],
    ["Body Fat", `${latest.fat_pct}%`],
    ["Lean Mass", `${latest.lean_mass_kg} kg`],
    ["Zepbound", `${zep.latest_entry.estimated_amount_mg} mg in system`],
    ["Shot Log", zep.goal_status.shot_logged.summary],
    ["Water", water ? `${water.total_oz} / ${water.minimum_target_oz}-${water.ideal_target_oz} oz` : "loading"],
    ["Movement", `${stats.movement_minutes} min`],
  ];
  metricGrid.innerHTML = metrics
    .map(([label, value]) => `<div class="metric"><p>${label}</p><strong>${value}</strong></div>`)
    .join("");
}

function setTrendNotes() {
  setPanelLoading(trendNotes, false);
  if (!state.trends || !state.fatloss || !state.zepbound) {
    return;
  }
  const pieces = [
    ...state.trends.coach_notes,
    ...state.fatloss.coach_notes,
    ...state.zepbound.coach_notes,
    ...(state.water?.coach_notes || []),
  ];
  trendNotes.innerHTML = pieces.map((note) => `<p>${note}</p>`).join("");
}

async function fetchJson(path) {
  const response = await fetch(path);
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || "Failed to load status.");
  }
  return payload;
}

async function loadToday() {
  setPanelLoading(summaryCard, true);
  try {
    const path = state.date ? `/api/today?date=${state.date}` : "/api/today";
    const payload = await fetchJson(path);
    state.coach = payload.coach;
    state.date = payload.date;
    dateInput.value = state.date;
    setSummary(payload);
  } catch (error) {
    setPanelLoading(summaryCard, false);
    summaryCard.innerHTML = `
      <p class="label">Today</p>
      <h2>Reconnect Fitbit</h2>
      <p>${error.message || "Fitbit data did not load."}</p>
      <p class="meta">If the hosted app lost its tokens, reconnecting is the fix.</p>
      <p><a class="reconnect-link" href="${reconnectUrl}">Reconnect Fitbit</a></p>
    `;
    throw error;
  }
}

async function loadSecondary() {
  setPanelLoading(metricGrid, true);
  setPanelLoading(trendNotes, true);
  try {
    const [trendsPayload, fatlossPayload, zepboundPayload, waterPayload] = await Promise.all([
      fetchJson(`/api/trends?date=${state.date}`),
      fetchJson(`/api/fatloss?date=${state.date}`),
      fetchJson(`/api/zepbound?date=${state.date}`),
      fetchJson(`/api/water?date=${state.date}`),
    ]);
    state.trends = trendsPayload.trends;
    state.fatloss = fatlossPayload.fatloss;
    state.zepbound = zepboundPayload.zepbound;
    state.water = waterPayload.water;
    setMetrics();
    setTrendNotes();
    setSummary({ coach: state.coach, cache_status: trendsPayload.cache_status });
  } catch (error) {
    setPanelLoading(metricGrid, false);
    setPanelLoading(trendNotes, false);
    metricGrid.innerHTML = `<div class="metric"><p>Extra context</p><strong>Still loading</strong></div>`;
    trendNotes.innerHTML = `<p>${error.message || "Secondary panels are still loading."}</p>`;
  }
}

async function loadStatus() {
  dateInput.value = state.date;
  state.coach = null;
  state.trends = null;
  state.fatloss = null;
  state.zepbound = null;
  state.water = null;
  await loadToday();
  await loadSecondary();
}

async function loadHistory() {
  setPanelLoading(historyLog, true);
  const response = await fetch("/api/history?limit=20");
  const payload = await response.json();
  if (!response.ok) {
    setPanelLoading(historyLog, false);
    throw new Error(payload.error || "Failed to load history.");
  }
  setPanelLoading(historyLog, false);
  renderHistory(payload.items || []);
}

async function askCoach(message) {
  addMessage("You", message);
  chatSubmit.disabled = true;
  chatSubmit.textContent = "Thinking...";
  const lowered = message.toLowerCase();
  const touchesWater = lowered.includes("water") || lowered.includes("hydration") || /\b\d+(?:\.\d+)?\s*(?:oz|ounces?)\b/.test(lowered);
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, date: state.date }),
    });
    const payload = await response.json();
    if (!response.ok) {
      addMessage("Coach", payload.error || "Something went sideways.");
      return;
    }
    addMessage("Coach", payload.reply);
    if (touchesWater) {
      try {
        await loadStatus();
      } catch (error) {
        addMessage("Coach", error.message || "Water updated, but the dashboard did not refresh cleanly.");
      }
    }
    loadHistory().catch((error) => addMessage("Coach", error.message));
  } catch (error) {
    addMessage("Coach", error.message || "The request failed before the coach could answer.");
  } finally {
    chatSubmit.disabled = false;
    chatSubmit.textContent = "Ask Coach";
  }
}

dateInput.addEventListener("change", async (event) => {
  state.date = event.target.value;
  try {
    await loadStatus();
  } catch (error) {
    addMessage("Coach", error.message);
  }
});

document.querySelectorAll(".chip").forEach((button) => {
  button.addEventListener("click", () => {
    askCoach(button.dataset.prompt);
  });
});

chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = chatInput.value.trim();
  if (!message) {
    addMessage("Coach", "Type a question first. The button is a coach, not a Ouija board.");
    chatInput.focus();
    return;
  }
  chatInput.value = "";
  await askCoach(message);
});

chatInput.addEventListener("keydown", async (event) => {
  if (event.key !== "Enter" || event.shiftKey) {
    return;
  }
  event.preventDefault();
  chatForm.requestSubmit();
});

addMessage("Coach", "Ask me what to do today, whether you should train, what goals you are working on, how water is going, how fat loss is going, or where you are in your shot cycle.");
loadStatus().catch((error) => addMessage("Coach", error.message));
loadHistory().catch((error) => addMessage("Coach", error.message));
