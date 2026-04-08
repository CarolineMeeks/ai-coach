const state = {
  date: new Date().toISOString().slice(0, 10),
};

const dateInput = document.querySelector("#date-input");
const metricGrid = document.querySelector("#metric-grid");
const summaryCard = document.querySelector("#summary-card");
const trendNotes = document.querySelector("#trend-notes");
const chatLog = document.querySelector("#chat-log");
const historyLog = document.querySelector("#history-log");
const chatForm = document.querySelector("#chat-form");
const chatInput = document.querySelector("#chat-input");
const chatSubmit = document.querySelector("#chat-submit");
const template = document.querySelector("#message-template");

dateInput.value = state.date;

function addMessage(speaker, content) {
  const fragment = template.content.cloneNode(true);
  fragment.querySelector(".speaker").textContent = speaker;
  fragment.querySelector(".content").textContent = content;
  chatLog.appendChild(fragment);
  chatLog.scrollTop = chatLog.scrollHeight;
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
  const readiness = payload.coach.readiness;
  const prescription = payload.coach.prescription;
  const fatloss = payload.fatloss.verdict;
  const daysSinceDose = payload.zepbound.days_since_last_dose;
  summaryCard.innerHTML = `
    <p class="label">Today</p>
    <h2>${readiness.toUpperCase()}</h2>
    <p>${prescription}</p>
    <p class="meta">Fat-loss read: ${fatloss}. Zepbound: ${daysSinceDose} day(s) since last shot.</p>
  `;
}

function setMetrics(payload) {
  const stats = payload.coach.stats;
  const latest = payload.fatloss.latest;
  const zep = payload.zepbound;
  const metrics = [
    ["Zone Minutes", `${stats.zone_minutes}`],
    ["Zone Split", `${stats.fat_burn_zone_minutes}/${stats.cardio_zone_minutes}/${stats.peak_zone_minutes}`],
    ["Steps", `${stats.steps} / ${stats.step_goal}`],
    ["Sleep", `${stats.sleep} (${stats.sleep_efficiency}% eff.)`],
    ["Resting HR", `${stats.resting_hr}`],
    ["Body Fat", `${latest.fat_pct}%`],
    ["Lean Mass", `${latest.lean_mass_kg} kg`],
    ["Zepbound", `${zep.latest_entry.estimated_amount_mg} mg in system`],
    ["Movement", `${stats.movement_minutes} min`],
  ];
  metricGrid.innerHTML = metrics
    .map(([label, value]) => `<div class="metric"><p>${label}</p><strong>${value}</strong></div>`)
    .join("");
}

function setTrendNotes(payload) {
  const pieces = [
    ...payload.trends.coach_notes,
    ...payload.fatloss.coach_notes,
    ...payload.zepbound.coach_notes,
  ];
  trendNotes.innerHTML = pieces.map((note) => `<p>${note}</p>`).join("");
}

async function loadStatus() {
  const response = await fetch(`/api/status?date=${state.date}`);
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || "Failed to load status.");
  }
  setSummary(payload);
  setMetrics(payload);
  setTrendNotes(payload);
}

async function loadHistory() {
  const response = await fetch("/api/history?limit=20");
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || "Failed to load history.");
  }
  renderHistory(payload.items || []);
}

async function askCoach(message) {
  addMessage("You", message);
  chatSubmit.disabled = true;
  chatSubmit.textContent = "Thinking...";
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

addMessage("Coach", "Ask me what to do today, whether you should train, how fat loss is going, or where you are in the shot cycle.");
loadStatus().catch((error) => addMessage("Coach", error.message));
loadHistory().catch((error) => addMessage("Coach", error.message));
