(() => {
    "use strict";

    const app = document.getElementById("production-dashboard");
    if (!app) return;

    const snapshotUrl = app.dataset.snapshotUrl;
    const liveUrl = app.dataset.liveUrl;
    const controlUrl = app.dataset.slideshowControlUrl;
    const wiredUrl = app.dataset.wiredUrl;
    const csrfToken = app.dataset.csrfToken;
    const liveDot = document.getElementById("operator-live-dot");
    const liveLabel = document.getElementById("operator-live-label");
    const launchWiredButton = document.getElementById("launch-wired-display");
    let source = null;
    let reconnectTimer = null;
    let lastLiveSequence = 0;

    function toast(message, kind = "info") {
        if (window.ArcobalenoToast) window.ArcobalenoToast(message, kind);
    }

    function setText(id, value) {
        const node = document.getElementById(id);
        if (node && typeof value !== "undefined" && value !== null) node.textContent = String(value);
    }

    function setLiveState(online) {
        liveDot?.classList.toggle("is-offline", !online);
        if (liveLabel) liveLabel.textContent = online ? "Live status connected" : "Reconnecting live status…";
    }

    function updateCounts(counts = {}) {
        setText("metric-total", counts.total);
        setText("metric-approved", counts.approved);
        setText("metric-pending", counts.pending);
        setText("safety-hidden-count", counts.hidden);
        setText("safety-favorite-count", counts.favorites);
    }

    function updateDisplays(displays = {}) {
        setText("metric-displays", displays.total ?? 0);
        setText("metric-displayed", displays.displayed_unique ?? 0);
        setText("metric-display-detail", `${displays.wired ?? 0} wired · ${displays.network ?? 0} network`);
        setText("display-wired-count", displays.wired ?? 0);
        setText("display-network-count", displays.network ?? 0);
        setText("display-preview-count", displays.preview ?? 0);
        setText("display-health-title", displays.total ? "Displays connected" : "No display heartbeat yet");
    }

    function updateControl(control = {}) {
        if (control.slideshow_state) {
            setText("slideshow-state-pill", control.slideshow_state.replace(/^./, (letter) => letter.toUpperCase()));
            document.querySelectorAll("[data-slideshow-state]").forEach((button) => {
                button.classList.toggle("is-active", button.dataset.slideshowState === control.slideshow_state);
            });
        }
        const badge = document.getElementById("event-mode-badge");
        if (badge && control.event_mode) {
            badge.textContent = control.event_mode === "live" ? "● Event live" : "Setup";
            badge.classList.toggle("badge-live", control.event_mode === "live");
            badge.classList.toggle("badge-setup", control.event_mode !== "live");
        }
    }

    function updateSnapshot(data) {
        lastLiveSequence = Math.max(lastLiveSequence, Number(data.live_sequence || 0));
        updateCounts(data.counts || {});
        updateDisplays(data.displays || {});
        updateControl(data.control || {});
        setText("metric-guests", data.guest_count);
    }

    async function fetchSnapshot() {
        try {
            const response = await fetch(snapshotUrl, { cache: "no-store", headers: { Accept: "application/json" } });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) return;
            updateSnapshot(data);
        } catch (_error) {
            setLiveState(false);
        }
    }

    function parseEvent(event) {
        try {
            const data = JSON.parse(event.data);
            lastLiveSequence = Math.max(lastLiveSequence, Number(data.sequence || event.lastEventId || 0));
            return data;
        } catch (_error) {
            return {};
        }
    }

    function connectLive() {
        source?.close();
        const url = new URL(liveUrl, window.location.origin);
        url.searchParams.set("since", String(lastLiveSequence));
        source = new EventSource(url);
        source.onopen = () => setLiveState(true);
        source.addEventListener("stream.ready", parseEvent);
        source.onerror = () => {
            setLiveState(false);
            source?.close();
            window.clearTimeout(reconnectTimer);
            reconnectTimer = window.setTimeout(connectLive, 2500);
        };
        source.addEventListener("photo.uploaded", (event) => {
            const payload = parseEvent(event).payload || {};
            updateCounts(payload.counts || {});
            const guest = payload.guest_name || "A guest";
            const warning = payload.moderation_status === "pending";
            toast(`${guest} uploaded a new photo${warning ? " — review queue" : ""}.`, warning ? "warning" : "info");
        });
        source.addEventListener("photo.updated", (event) => {
            const payload = parseEvent(event).payload || {};
            updateCounts(payload.counts || {});
            if (payload.action === "hide") toast("A photo was hidden from every slideshow.", "warning");
        });
        source.addEventListener("photo.deleted", (event) => updateCounts((parseEvent(event).payload || {}).counts || {}));
        source.addEventListener("display.updated", (event) => updateDisplays((parseEvent(event).payload || {}).displays || {}));
        source.addEventListener("control.updated", (event) => updateControl((parseEvent(event).payload || {}).control || {}));
        source.addEventListener("timeline.updated", () => fetchSnapshot());
    }

    async function sendControl(values) {
        const form = new FormData();
        form.append("_csrf_token", csrfToken);
        Object.entries(values).forEach(([key, value]) => form.append(key, value));
        try {
            const response = await fetch(controlUrl, {
                method: "POST",
                body: form,
                headers: { Accept: "application/json", "X-CSRF-Token": csrfToken },
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.error || "Display control failed.");
            updateControl(data.control || {});
            const label = values.command || values.state || "display";
            toast(`Slideshow: ${label}.`);
        } catch (error) {
            toast(error.message || "Could not update the slideshow.", "error");
        }
    }

    async function launchWiredDisplay() {
        launchWiredButton.disabled = true;
        try {
            let targetScreen = null;
            if (typeof window.getScreenDetails === "function") {
                try {
                    const details = await window.getScreenDetails();
                    targetScreen = details.screens.find((screen) => screen !== details.currentScreen)
                        || details.screens.find((screen) => !screen.isPrimary)
                        || null;
                } catch (_error) {
                    // Permission may be declined; the normal popup fallback still works.
                }
            }

            const features = ["popup=yes", "resizable=yes", "scrollbars=no"];
            if (targetScreen) {
                features.push(
                    `left=${Math.round(targetScreen.left)}`,
                    `top=${Math.round(targetScreen.top)}`,
                    `width=${Math.round(targetScreen.availWidth || targetScreen.width)}`,
                    `height=${Math.round(targetScreen.availHeight || targetScreen.height)}`,
                );
            }
            const displayWindow = window.open(wiredUrl, "arcobaleno-wired-display", features.join(","));
            if (!displayWindow) throw new Error("The browser blocked the display window. Allow pop-ups for ARCOBALENO and try again.");
            displayWindow.focus();
            toast(targetScreen ? "Wired slideshow opened on the second screen." : "Wired slideshow opened. Move it to the projector if needed.");
        } catch (error) {
            toast(error.message || "Could not open the wired display.", "error");
        } finally {
            launchWiredButton.disabled = false;
        }
    }

    document.addEventListener("click", (event) => {
        const stateButton = event.target.closest("[data-slideshow-state]");
        if (stateButton) {
            const values = { state: stateButton.dataset.slideshowState };
            if (stateButton.dataset.title) values.title = stateButton.dataset.title;
            if (stateButton.dataset.message) values.message = stateButton.dataset.message;
            sendControl(values);
            return;
        }
        const commandButton = event.target.closest("[data-slideshow-command]");
        if (commandButton) sendControl({ command: commandButton.dataset.slideshowCommand });
    });

    document.addEventListener("keydown", (event) => {
        if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement || event.target instanceof HTMLSelectElement) return;
        if (event.key === "F12") {
            event.preventDefault();
            sendControl({ state: "emergency", title: "We’ll be right back", message: "Preparing your memories…" });
        } else if (event.key === "F10") {
            event.preventDefault();
            sendControl({ state: "playing" });
        }
    });

    launchWiredButton?.addEventListener("click", launchWiredDisplay);
    window.addEventListener("beforeunload", () => source?.close());
    fetchSnapshot();
    connectLive();
    window.setInterval(fetchSnapshot, 10000);
})();
