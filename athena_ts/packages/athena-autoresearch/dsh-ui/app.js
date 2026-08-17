const $ = (id) => document.getElementById(id)

const STATUS_BADGE = {
  COMPLETED: "badge--success",
  SUPPORTED: "badge--success",
  RUNNING: "badge--info",
  WAITING: "badge--warning",
  INCONCLUSIVE: "badge--warning",
  FAILED: "badge--danger",
  STOPPED: "badge--danger",
  REFUTED: "badge--danger",
}

const STATUS_DOT = {
  RUNNING: "running",
  WAITING: "paused",
  COMPLETED: "completed",
  FAILED: "error",
  STOPPED: "error",
}

function setStatusBadge(status) {
  const badge = $("status")
  badge.textContent = status ?? "—"
  badge.className = `badge ${STATUS_BADGE[status] ?? "badge--neutral"}`
}

function setTopbarStatus(connected, status) {
  const dot = $("status-dot")
  const text = $("status-text")
  if (!connected) {
    dot.dataset.status = "idle"
    text.textContent = "idle · 未连接"
    return
  }
  dot.dataset.status = STATUS_DOT[status] ?? "idle"
  text.textContent = `${status ?? "NOT_STARTED"} · ${new Date().toLocaleTimeString()}`
}

async function refresh() {
  try {
    const response = await fetch("/api/status")
    const data = await response.json()
    $("run_id").textContent = data.run_id ?? "—"
    $("phase").textContent = data.phase ?? "—"
    $("sota").textContent = data.sota_experiment_id ?? "—"
    $("queued").textContent = data.queued_hypotheses ?? "—"
    setStatusBadge(data.status)
    $("console-pane").textContent = (data.console_lines ?? []).join("\n") || "（暂无输出）"
    $("console-note").textContent = `自动刷新 · ${new Date().toLocaleTimeString()}`
    setTopbarStatus(true, data.status)
    syncToBottom()
  } catch {
    setTopbarStatus(false)
  }
}

// Function rail: scroll to section and keep the active icon in sync.
for (const button of document.querySelectorAll(".rail button[data-target]")) {
  button.addEventListener("click", () => {
    const target = $(button.dataset.target)
    if (!target) return
    target.scrollIntoView({ behavior: "smooth", block: "start" })
    for (const other of document.querySelectorAll(".rail button[data-target]")) {
      other.classList.toggle("active", other === button)
    }
  })
}

// Floating "回到底部" button for the main scroll container.
const mainPane = document.querySelector(".main")
const toBottom = $("to-bottom")
const nearBottom = () => mainPane.scrollHeight - mainPane.scrollTop - mainPane.clientHeight < 48

function syncToBottom() {
  toBottom.hidden = nearBottom()
}

mainPane.addEventListener("scroll", syncToBottom, { passive: true })
toBottom.addEventListener("click", () => {
  mainPane.scrollTo({ top: mainPane.scrollHeight, behavior: "smooth" })
})
syncToBottom()

$("run").addEventListener("click", async () => {
  $("run").disabled = true
  try {
    const response = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dir: $("dir").value }),
    })
    const data = await response.json()
    console.log("run result", data)
  } finally {
    await refresh()
    $("run").disabled = false
  }
})

$("refresh").addEventListener("click", refresh)
refresh()
setInterval(refresh, 1500)

if (window.mountHypothesisGraph) {
  window.mountHypothesisGraph($("graph"))
}
