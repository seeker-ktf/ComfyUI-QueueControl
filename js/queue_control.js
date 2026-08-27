import { app } from "../../scripts/app.js";

const STYLE = `
.qc-popup {
    padding: 14px;
    min-width: 360px;
    max-width: 500px;
    max-height: 70vh;
    overflow-y: auto;
    background: var(--comfy-menu-bg, #333);
    border: 1px solid var(--border-color, #555);
    border-radius: 6px;
    font-family: sans-serif;
    font-size: 13px;
    color: var(--fg-color, #ddd);
    box-shadow: 0 4px 16px rgba(0,0,0,0.4);
    position: fixed;
    z-index: 99999;
}
.qc-popup h3 {
    margin: 0 0 10px 0;
    font-size: 15px;
    font-weight: 600;
}
.qc-empty {
    color: var(--descrip-text, #999);
    font-style: italic;
    padding: 8px 0;
}
.qc-running {
    padding: 6px 8px;
    margin-bottom: 8px;
    background: rgba(42, 122, 42, 0.3);
    border: 1px solid #3a9a3a;
    border-radius: 4px;
    font-size: 12px;
}
.qc-item {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 6px 8px;
    margin-bottom: 4px;
    background: rgba(255,255,255,0.05);
    border-radius: 4px;
    font-size: 12px;
}
.qc-item.hold {
    opacity: 0.5;
    background: rgba(255,0,0,0.1);
}
.qc-item.next {
    background: rgba(42, 122, 42, 0.2);
    border: 1px solid #3a9a3a;
}
.qc-item-info {
    flex: 1;
    min-width: 0;
}
.qc-item-id {
    font-family: monospace;
    font-size: 11px;
    color: var(--descrip-text, #999);
}
.qc-item-time {
    font-size: 11px;
    color: var(--descrip-text, #999);
}
.qc-priority-controls {
    display: flex;
    align-items: center;
    gap: 3px;
    flex-shrink: 0;
}
.qc-pri-btn {
    width: 24px;
    height: 24px;
    border: 1px solid var(--border-color, #555);
    border-radius: 3px;
    background: var(--comfy-input-bg, #444);
    color: var(--fg-color, #ddd);
    cursor: pointer;
    font-size: 14px;
    line-height: 1;
    display: flex;
    align-items: center;
    justify-content: center;
}
.qc-pri-btn:hover {
    background: var(--border-color, #555);
}
.qc-pri-num {
    width: 20px;
    text-align: center;
    font-weight: bold;
    font-size: 14px;
}
.qc-hold-btn {
    padding: 2px 6px;
    border: 1px solid var(--border-color, #555);
    border-radius: 3px;
    background: var(--comfy-input-bg, #444);
    color: var(--fg-color, #ddd);
    cursor: pointer;
    font-size: 11px;
}
.qc-hold-btn:hover {
    background: #b33;
}
.qc-hold-btn.held {
    background: #b33;
    border-color: #d44;
}
.qc-next-btn {
    padding: 2px 6px;
    border: 1px solid #3a9a3a;
    border-radius: 3px;
    background: rgba(42, 122, 42, 0.4);
    color: var(--fg-color, #ddd);
    cursor: pointer;
    font-size: 11px;
}
.qc-next-btn:hover {
    background: #2a7a2a;
}
.qc-toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 8px;
    padding-bottom: 6px;
    border-bottom: 1px solid var(--border-color, #555);
}
.qc-sort-toggle {
    padding: 2px 8px;
    border: 1px solid var(--border-color, #555);
    border-radius: 3px;
    background: var(--comfy-input-bg, #444);
    color: var(--fg-color, #ddd);
    cursor: pointer;
    font-size: 11px;
}
.qc-sort-toggle:hover {
    background: var(--border-color, #555);
}
.qc-item.flash {
    animation: qc-flash 1s ease-out;
}
@keyframes qc-flash {
    0% { background: rgba(255, 255, 100, 0.3); }
    100% { background: rgba(255, 255, 255, 0.05); }
}
.qc-save-load {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 8px 0;
    border-top: 1px solid var(--border-color, #555);
    margin-top: 8px;
}
.qc-save-btn, .qc-load-btn {
    padding: 4px 10px;
    border: 1px solid var(--border-color, #555);
    border-radius: 3px;
    background: var(--comfy-input-bg, #444);
    color: var(--fg-color, #ddd);
    cursor: pointer;
    font-size: 12px;
}
.qc-save-btn:hover, .qc-load-btn:hover {
    background: var(--border-color, #555);
}
.qc-save-load label {
    font-size: 11px;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 4px;
}
.qc-save-load input[type="checkbox"] {
    cursor: pointer;
}
.qc-status-msg {
    font-size: 11px;
    padding: 4px 8px;
    margin-top: 4px;
    border-radius: 3px;
}
.qc-status-msg.success {
    background: rgba(42, 122, 42, 0.3);
    color: #8f8;
}
.qc-status-msg.error {
    background: rgba(179, 51, 51, 0.3);
    color: #f88;
}
`;

function formatTime(ms) {
    if (!ms) return "";
    const d = new Date(ms);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

app.registerExtension({
    name: "ComfyUI.QueueControl",

    async setup() {
        // Inject styles
        const styleEl = document.createElement("style");
        styleEl.textContent = STYLE;
        document.head.appendChild(styleEl);

        let paused = false;
        let popupEl = null;
        let popupVisible = false;
        let refreshInterval = null;
        let sortByTime = true;  // true = by submission time, false = by priority
        let lastChangedId = null;  // track which item was just changed

        // Fetch initial state — check for restore notification
        try {
            const resp = await fetch("/queue_control/status");
            const data = await resp.json();
            paused = data.paused;
            if (data.restore_message) {
                // Show popup after a short delay so the UI is ready
                setTimeout(() => alert(data.restore_message), 500);
            }
        } catch (e) {
            console.warn("[QueueControl] Could not fetch initial status:", e);
        }

        // Poll for restore message (restore happens ~3s after startup,
        // JS may have loaded before it completes)
        let restoreCheckCount = 0;
        const restoreChecker = setInterval(async () => {
            restoreCheckCount++;
            if (restoreCheckCount > 10) {
                clearInterval(restoreChecker);
                return;
            }
            try {
                const resp = await fetch("/queue_control/status");
                const data = await resp.json();
                if (data.restore_message) {
                    clearInterval(restoreChecker);
                    paused = data.paused;
                    // Update pause button if it exists
                    if (pauseBtnRef) {
                        pauseBtnRef.icon = "play";
                        pauseBtnRef.content = "Resume";
                        pauseBtnRef.element.style.background = "#b33";
                        pauseBtnRef.element.style.borderColor = "#d44";
                    }
                    alert(data.restore_message);
                }
            } catch (e) {}
        }, 2000);

        let pauseBtnRef = null;

        // ── Priority change ──────────────────────────────
        async function setPriority(promptId, priority) {
            try {
                lastChangedId = promptId;
                await fetch("/queue_control/priority", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ prompt_id: promptId, priority }),
                });
                await refreshPopup();
            } catch (e) {
                console.error("[QueueControl] Priority change failed:", e);
            }
        }

        // ── Popup ────────────────────────────────────────
        function createPopup() {
            const el = document.createElement("div");
            el.className = "qc-popup";
            el.innerHTML = `<div class="qc-toolbar">
                <h3 style="margin:0;font-size:15px;font-weight:600;">Queue Control</h3>
                <button class="qc-sort-toggle">Sort: By Time</button>
            </div>
            <div class="qc-content"></div>
            <div class="qc-save-load">
                <button class="qc-save-btn">Save Queue</button>
                <button class="qc-load-btn">Load Queue</button>
                <label><input type="checkbox" class="qc-include-running"> Include running</label>
            </div>
            <div class="qc-status-area"></div>`;
            // Sort toggle handler
            el.querySelector(".qc-sort-toggle").addEventListener("click", (e) => {
                sortByTime = !sortByTime;
                e.target.textContent = sortByTime ? "Sort: By Time" : "Sort: By Priority";
                refreshPopup();
            });
            // Save handler
            el.querySelector(".qc-save-btn").addEventListener("click", async () => {
                const includeRunning = el.querySelector(".qc-include-running").checked;
                const statusArea = el.querySelector(".qc-status-area");
                try {
                    const resp = await fetch("/queue_control/save", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ include_running: includeRunning }),
                    });
                    const data = await resp.json();
                    if (data.ok) {
                        statusArea.innerHTML = `<div class="qc-status-msg success">Saved ${data.count} item(s)</div>`;
                    } else {
                        statusArea.innerHTML = `<div class="qc-status-msg error">Save failed: ${data.error}</div>`;
                    }
                } catch (e) {
                    statusArea.innerHTML = `<div class="qc-status-msg error">Save failed</div>`;
                }
                // Reset the checkbox
                el.querySelector(".qc-include-running").checked = false;
                // Clear status after 5 seconds
                setTimeout(() => { statusArea.innerHTML = ""; }, 5000);
            });
            // Load handler
            el.querySelector(".qc-load-btn").addEventListener("click", async () => {
                const statusArea = el.querySelector(".qc-status-area");
                try {
                    // Check if save exists first
                    const checkResp = await fetch("/queue_control/has_save");
                    const checkData = await checkResp.json();
                    if (!checkData.exists) {
                        statusArea.innerHTML = `<div class="qc-status-msg error">No saved queue found</div>`;
                        setTimeout(() => { statusArea.innerHTML = ""; }, 5000);
                        return;
                    }
                    const resp = await fetch("/queue_control/load", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({}),
                    });
                    const data = await resp.json();
                    if (data.ok) {
                        let msg = `Loaded ${data.loaded} item(s)`;
                        if (data.held > 0) {
                            msg += ` — ${data.held} failed validation (on hold)`;
                        }
                        statusArea.innerHTML = `<div class="qc-status-msg ${data.held > 0 ? 'error' : 'success'}">${msg}</div>`;
                    } else {
                        statusArea.innerHTML = `<div class="qc-status-msg error">Load failed: ${data.error}</div>`;
                    }
                    await refreshPopup();
                } catch (e) {
                    statusArea.innerHTML = `<div class="qc-status-msg error">Load failed</div>`;
                }
                setTimeout(() => { statusArea.innerHTML = ""; }, 8000);
            });
            document.body.appendChild(el);
            return el;
        }

        async function refreshPopup() {
            if (!popupEl || !popupVisible) return;
            const content = popupEl.querySelector(".qc-content");
            try {
                const resp = await fetch("/queue_control/queue");
                const data = await resp.json();

                let html = "";

                // Running jobs
                if (data.running.length > 0) {
                    for (const job of data.running) {
                        const name = job.label || job.prompt_id.substring(0, 8) + "...";
                        html += `<div class="qc-running">
                            ▶ Running: ${name}
                            ${job.create_time ? " — queued " + formatTime(job.create_time) : ""}
                        </div>`;
                    }
                }

                // Queued jobs
                if (data.queued.length === 0) {
                    html += `<div class="qc-empty">Queue is empty</div>`;
                } else {
                    // Sort display order
                    const sorted = [...data.queued];
                    if (sortByTime) {
                        sorted.sort((a, b) => a.create_time - b.create_time);
                    }
                    // else already sorted by priority from server

                    for (const item of sorted) {
                        const isHold = item.priority === 9;
                        const isNext = item.priority === 0;
                        const isFlash = item.prompt_id === lastChangedId;
                        let cls = "qc-item";
                        if (isHold) cls += " hold";
                        else if (isNext) cls += " next";
                        if (isFlash) cls += " flash";
                        const priLabel = isHold ? "H" : item.priority;
                        const timeStr = item.create_time ? formatTime(item.create_time) : "";
                        const name = item.label || item.prompt_id.substring(0, 8) + "...";

                        html += `<div class="${cls}" data-prompt="${item.prompt_id}">
                            <div class="qc-item-info">
                                <div>${name} <span class="qc-item-id">${item.label ? item.prompt_id.substring(0, 8) + "..." : ""}</span></div>
                                <div class="qc-item-time">${timeStr}</div>
                            </div>
                            <div class="qc-priority-controls">
                                ${!isNext ? `<button class="qc-next-btn" data-id="${item.prompt_id}" data-action="next" title="Run next">Next</button>` : ""}
                                <button class="qc-pri-btn" data-id="${item.prompt_id}" data-action="up" title="Higher priority">▲</button>
                                <span class="qc-pri-num">${priLabel}</span>
                                <button class="qc-pri-btn" data-id="${item.prompt_id}" data-action="down" title="Lower priority">▼</button>
                                <button class="qc-hold-btn ${isHold ? 'held' : ''}" data-id="${item.prompt_id}" data-action="hold">${isHold ? "Unhold" : "Hold"}</button>
                            </div>
                        </div>`;
                    }
                }

                content.innerHTML = html;

                // Attach button handlers
                content.querySelectorAll("[data-action]").forEach(btn => {
                    btn.addEventListener("click", async (e) => {
                        const id = btn.dataset.id;
                        const action = btn.dataset.action;
                        const item = data.queued.find(q => q.prompt_id === id);
                        if (!item) return;

                        if (action === "next") {
                            await setPriority(id, 0);
                        } else if (action === "up") {
                            const newPri = Math.max(0, item.priority - 1);
                            await setPriority(id, newPri);
                        } else if (action === "down") {
                            const newPri = Math.min(8, item.priority + 1);
                            await setPriority(id, newPri);
                        } else if (action === "hold") {
                            const newPri = item.priority === 9 ? 5 : 9;
                            await setPriority(id, newPri);
                        }
                    });
                });
            } catch (e) {
                content.innerHTML = `<div class="qc-empty">Error loading queue</div>`;
            }
        }

        function showPopup(anchorEl) {
            if (!popupEl) popupEl = createPopup();
            // Position near the button
            const rect = anchorEl.getBoundingClientRect();
            popupEl.style.top = (rect.bottom + 4) + "px";
            popupEl.style.right = (window.innerWidth - rect.right) + "px";
            popupEl.style.display = "block";
            popupVisible = true;
            refreshPopup();
            // Auto-refresh every 2 seconds while open
            refreshInterval = setInterval(refreshPopup, 2000);
        }

        function hidePopup() {
            if (popupEl) popupEl.style.display = "none";
            popupVisible = false;
            if (refreshInterval) {
                clearInterval(refreshInterval);
                refreshInterval = null;
            }
        }

        function togglePopup(anchorEl) {
            if (popupVisible) {
                hidePopup();
            } else {
                showPopup(anchorEl);
            }
        }

        // Close popup when clicking outside
        document.addEventListener("mousedown", (e) => {
            if (popupEl && popupVisible && !popupEl.contains(e.target)) {
                // Check if click was on the queue button itself
                if (!e.target.closest(".qc-queue-trigger")) {
                    hidePopup();
                }
            }
        });

        // ── Top bar buttons ──────────────────────────────
        try {
            const { ComfyButton } = await import("../../scripts/ui/components/button.js");
            const { ComfyButtonGroup } = await import("../../scripts/ui/components/buttonGroup.js");

            // Pause button
            const pauseBtn = new ComfyButton({
                icon: paused ? "play" : "pause",
                content: paused ? "Resume" : "Pause",
                tooltip: "Pause / Resume Queue",
                action: () => {
                    fetch("/queue_control/pause", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ paused: !paused }),
                    }).then(r => r.json()).then(data => {
                        paused = data.paused;
                        pauseBtn.icon = paused ? "play" : "pause";
                        pauseBtn.content = paused ? "Resume" : "Pause";
                        pauseBtn.element.style.background = paused ? "#b33" : "#2a7a2a";
                        pauseBtn.element.style.borderColor = paused ? "#d44" : "#3a9a3a";
                        // Refresh popup if open
                        refreshPopup();
                    });
                },
                classList: "comfyui-button comfyui-menu-mobile-collapse",
            });

            // Set initial color
            pauseBtnRef = pauseBtn;
            pauseBtn.element.style.background = paused ? "#b33" : "#2a7a2a";
            pauseBtn.element.style.borderColor = paused ? "#d44" : "#3a9a3a";

            // Queue panel button
            const queueBtn = new ComfyButton({
                icon: "format-list-numbered",
                content: "Queue",
                tooltip: "Queue Priority Panel",
                action: () => togglePopup(queueBtn.element),
                classList: "comfyui-button comfyui-menu-mobile-collapse",
            });
            queueBtn.element.classList.add("qc-queue-trigger");

            const group = new ComfyButtonGroup(pauseBtn.element, queueBtn.element);
            app.menu?.settingsGroup.element.before(group.element);
        } catch (e) {
            console.warn("[QueueControl] Could not add top-bar buttons:", e);
        }
    },
});
