(() => {
    "use strict";

    const app = document.getElementById("slideshow-app");
    if (!app) return;

    const feedUrl = app.dataset.feedUrl;
    const heartbeatUrl = app.dataset.heartbeatUrl;
    const originalEventId = app.dataset.eventId;
    const displayMode = app.dataset.displayMode || "network";
    const csrfToken = app.dataset.csrfToken;
    const secondsPerPhoto = Math.max(2, Number(app.dataset.secondsPerPhoto) || 7);
    const pollSeconds = Math.max(0.5, Number(app.dataset.pollSeconds) || 2);
    const showGuestName = app.dataset.showGuestName !== "false";
    const showTimestamp = app.dataset.showTimestamp !== "false";

    const themeNames = ["default", "floral", "secret-garden", "ocean", "ballroom", "disco"];

    function applyTheme(value) {
        const theme = themeNames.includes(String(value || "")) ? String(value) : "default";
        for (const name of themeNames) document.body.classList.remove(`theme-${name}`);
        document.body.classList.add(`theme-${theme}`);
        app.dataset.theme = theme;
    }

    applyTheme(app.dataset.theme);

    const frames = Array.from(document.querySelectorAll("[data-slide-frame]"));
    const waitingScreen = document.getElementById("waiting-screen");
    const waitingCopy = document.getElementById("waiting-copy");
    const connectionPill = document.getElementById("connection-pill");
    const connectionLabel = document.getElementById("connection-label");
    const caption = document.getElementById("photo-caption");
    const captionName = document.getElementById("caption-name");
    const captionTime = document.getElementById("caption-time");
    const positionCurrent = document.getElementById("position-current");
    const positionTotal = document.getElementById("position-total");
    const controls = document.getElementById("slideshow-controls");
    const previousButton = document.getElementById("previous-button");
    const playButton = document.getElementById("play-button");
    const playIcon = document.getElementById("play-icon");
    const nextButton = document.getElementById("next-button");
    const infoButton = document.getElementById("info-button");
    const fullscreenButton = document.getElementById("fullscreen-button");
    const keyboardHint = document.getElementById("keyboard-hint");
    const toast = document.getElementById("slideshow-toast");
    const fullscreenGate = document.getElementById("fullscreen-gate");
    const productionScreens = Array.from(document.querySelectorAll(".production-screen"));
    const logoScreen = document.getElementById("production-logo-screen");
    const instructionsScreen = document.getElementById("production-instructions-screen");
    const instructionPages = Array.from(document.querySelectorAll("[data-instruction-page]"));
    const instructionCountdown = document.getElementById("joining-countdown");
    let instructionState = {active:false,page:0,remaining:0};
    const blackScreen = document.getElementById("production-black-screen");
    const emergencyScreen = document.getElementById("production-emergency-screen");
    const timelineScreen = document.getElementById("production-timeline-screen");
    const markerScreen = document.getElementById("production-marker-screen");

    const photos = [];
    const photoIds = new Set();
    const failedPhotoIds = new Set();
    const priorityQueue = [];

    let activeFrameIndex = -1;
    let currentPhotoId = null;
    let currentEventId = originalEventId;
    let cursor = 0;
    let playing = true;
    let infoVisible = true;
    let advanceTimer = null;
    let sharedSyncEnabled = false;
    let syncPhotoPending = null;
    let controlsTimer = null;
    let toastTimer = null;
    let polling = false;
    let hasCompletedInitialFeed = false;
    let eventActive = true;
    let productionState = "playing";
    let lastControlUpdated = "";
    let lastCommandSequence = -1;

    const displayStorageKey = `arcobaleno_display_id:${originalEventId}:${displayMode}`;
    let displayId = "";
    try {
        displayId = localStorage.getItem(displayStorageKey) || "";
        if (!displayId) {
            displayId = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
            localStorage.setItem(displayStorageKey, displayId);
        }
    } catch (_error) {
        displayId = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    }

    function setConnection(state, label) {
        connectionPill.dataset.state = state;
        connectionLabel.textContent = label;
    }

    function showToast(message, duration = 2600) {
        window.clearTimeout(toastTimer);
        toast.textContent = message;
        toast.hidden = false;
        toastTimer = window.setTimeout(() => { toast.hidden = true; }, duration);
    }

    function formatPhotoTime(value) {
        if (!showTimestamp || !value) return "";
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return "";
        return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit", month: "short", day: "numeric" }).format(date);
    }

    function currentIndex() {
        return photos.findIndex((photo) => photo.id === currentPhotoId);
    }

    function usablePhotos() {
        return photos.filter((photo) => !failedPhotoIds.has(photo.id));
    }

    function updateControls() {
        const hasPhotos = usablePhotos().length > 0;
        previousButton.disabled = !hasPhotos;
        nextButton.disabled = !hasPhotos;
        playButton.disabled = !hasPhotos;
        playIcon.textContent = playing ? "Ⅱ" : "▶";
        playButton.setAttribute("aria-label", playing ? "Pause slideshow" : "Resume slideshow");
        playButton.title = playing ? "Pause slideshow" : "Resume slideshow";
        infoButton.setAttribute("aria-label", infoVisible ? "Hide photo details" : "Show photo details");
        infoButton.title = infoVisible ? "Hide photo details" : "Show photo details";
    }

    function clearAdvanceTimer() {
        window.clearTimeout(advanceTimer);
        advanceTimer = null;
    }

    function scheduleAdvance(delaySeconds = secondsPerPhoto) {
        clearAdvanceTimer();
        const availableCount = usablePhotos().length;
        if (!playing || productionState !== "playing" || !eventActive || document.hidden || availableCount < 1) return;
        if (availableCount < 2 && priorityQueue.length < 1) return;
        advanceTimer = window.setTimeout(() => advance(1, false), delaySeconds * 1000);
    }

    function preloadPhoto(photo) {
        return new Promise((resolve, reject) => {
            const loader = new Image();
            const timeout = window.setTimeout(() => { loader.src = ""; reject(new Error("Photo loading timed out.")); }, 15000);
            loader.onload = () => { window.clearTimeout(timeout); resolve(photo.url); };
            loader.onerror = () => { window.clearTimeout(timeout); reject(new Error("This image format could not be displayed.")); };
            loader.src = photo.url;
        });
    }

    async function showPhoto(photo, manual = false) {
        if (!photo || failedPhotoIds.has(photo.id)) return false;
        clearAdvanceTimer();
        try {
            await preloadPhoto(photo);
        } catch (_error) {
            failedPhotoIds.add(photo.id);
            showToast("One photo could not be displayed and was skipped.");
            updateControls();
            window.setTimeout(() => advance(1, false), 220);
            return false;
        }

        const nextFrameIndex = activeFrameIndex === 0 ? 1 : 0;
        const nextFrame = frames[nextFrameIndex];
        const oldFrame = activeFrameIndex >= 0 ? frames[activeFrameIndex] : null;
        const foreground = nextFrame.querySelector("[data-slide-photo]");
        const background = nextFrame.querySelector("[data-slide-background]");
        foreground.src = photo.url;
        foreground.alt = showGuestName && photo.guest_name ? `Photo shared by ${photo.guest_name}` : "Guest event photo";
        background.src = photo.url;
        nextFrame.setAttribute("aria-hidden", "false");
        nextFrame.classList.add("is-active");
        if (oldFrame) {
            oldFrame.classList.remove("is-active");
            oldFrame.setAttribute("aria-hidden", "true");
        }

        activeFrameIndex = nextFrameIndex;
        currentPhotoId = photo.id;
        waitingScreen.classList.add("is-hidden");
        caption.hidden = false;
        captionName.textContent = showGuestName ? (photo.guest_name || "Guest") : "A shared memory";
        const timeLabel = formatPhotoTime(photo.created_at);
        captionTime.textContent = timeLabel;
        captionTime.hidden = !timeLabel;
        const index = currentIndex();
        positionCurrent.textContent = String(index >= 0 ? index + 1 : 1);
        positionTotal.textContent = String(photos.length);
        updateControls();
        sendHeartbeat();
        if (!sharedSyncEnabled) scheduleAdvance(secondsPerPhoto);
        return true;
    }

    function nextSequentialPhoto(direction) {
        const available = usablePhotos();
        if (!available.length) return null;
        if (!currentPhotoId) return available[0];
        const index = available.findIndex((photo) => photo.id === currentPhotoId);
        const base = index >= 0 ? index : 0;
        return available[(base + direction + available.length) % available.length];
    }

    function nextPriorityPhoto() {
        while (priorityQueue.length) {
            const id = priorityQueue.shift();
            const photo = photos.find((item) => item.id === id);
            if (photo && !failedPhotoIds.has(id) && id !== currentPhotoId) return photo;
        }
        return null;
    }

    function advance(direction, manual) {
        if (!photos.length) return;
        let photo = null;
        if (!manual && direction > 0) photo = nextPriorityPhoto();
        if (!photo) photo = nextSequentialPhoto(direction);
        if (photo) showPhoto(photo, manual);
    }

    function clearVisualFeed() {
        activeFrameIndex = -1;
        currentPhotoId = null;
        frames.forEach((frame) => {
            frame.classList.remove("is-active");
            frame.setAttribute("aria-hidden", "true");
        });
        waitingScreen.classList.remove("is-hidden");
        caption.hidden = true;
        clearAdvanceTimer();
    }

    function resetFeed(eventId) {
        photos.splice(0, photos.length);
        photoIds.clear();
        failedPhotoIds.clear();
        priorityQueue.splice(0, priorityQueue.length);
        currentEventId = eventId || originalEventId;
        hasCompletedInitialFeed = false;
        positionTotal.textContent = "0";
        clearVisualFeed();
    }

    function addPhotos(incoming, prioritize) {
        const added = [];
        for (const photo of incoming || []) {
            if (!photo?.id || !photo.url) continue;
            const existingIndex = photos.findIndex((item) => item.id === photo.id);
            if (existingIndex >= 0) {
                photos[existingIndex] = photo;
                continue;
            }
            photoIds.add(photo.id);
            photos.push(photo);
            added.push(photo);
            if (prioritize) priorityQueue.push(photo.id);
        }
        photos.sort((a, b) => String(a.created_at || "").localeCompare(String(b.created_at || "")));
        positionTotal.textContent = String(photos.length);
        return added;
    }

    function removePhotos(ids) {
        const removed = new Set(ids || []);
        if (!removed.size) return;
        for (let index = photos.length - 1; index >= 0; index -= 1) {
            if (removed.has(photos[index].id)) {
                photoIds.delete(photos[index].id);
                photos.splice(index, 1);
            }
        }
        for (let index = priorityQueue.length - 1; index >= 0; index -= 1) {
            if (removed.has(priorityQueue[index])) priorityQueue.splice(index, 1);
        }
        positionTotal.textContent = String(photos.length);
        if (currentPhotoId && removed.has(currentPhotoId)) {
            currentPhotoId = null;
            if (photos.length) showPhoto(photos[0], true);
            else clearVisualFeed();
        }
    }

    function hideProductionScreens() {
        productionScreens.forEach((screen) => { screen.hidden = true; });
        app.classList.remove("production-screen-active");
    }

    function renderTimeline(entries) {
        const list = document.getElementById("timeline-screen-list");
        list.replaceChildren();
        for (const entry of entries || []) {
            const row = document.createElement("article");
            const time = document.createElement("time");
            time.textContent = entry.scheduled_time || "Today";
            const copy = document.createElement("div");
            const title = document.createElement("strong");
            title.textContent = entry.title || "Event moment";
            copy.append(title);
            if (entry.detail) {
                const detail = document.createElement("span");
                detail.textContent = entry.detail;
                copy.append(detail);
            }
            row.append(time, copy);
            list.append(row);
        }
        if (!list.children.length) {
            const empty = document.createElement("p");
            empty.className = "timeline-screen-empty";
            empty.textContent = "The event schedule will appear here.";
            list.append(empty);
        }
    }

    function applyControl(control = {}, timeline = [], featuredMarker = null) {
        const commandSequence = Number(control.slideshow_command_sequence || 0);
        if (!sharedSyncEnabled && commandSequence > lastCommandSequence && lastCommandSequence >= 0) {
            if (control.slideshow_command === "next") advance(1, true);
            else if (control.slideshow_command === "previous") advance(-1, true);
            else if (control.slideshow_command === "restart" && photos.length) showPhoto(photos[0], true);
        }
        lastCommandSequence = Math.max(lastCommandSequence, commandSequence);

        const updated = String(control.updated_at || "");
        if (updated && updated === lastControlUpdated) return;
        lastControlUpdated = updated;
        productionState = control.slideshow_state || "playing";
        document.body.dataset.productionState = productionState;
        hideProductionScreens();

        if (productionState === "playing") {
            playing = true;
            if (!currentPhotoId && photos.length) showPhoto(photos[0], false);
            if (!sharedSyncEnabled) scheduleAdvance();
        } else if (productionState === "paused") {
            playing = false;
            clearAdvanceTimer();
        } else {
            playing = false;
            clearAdvanceTimer();
            app.classList.add("production-screen-active");
            if (productionState === "instructions") {
                instructionsScreen.hidden = false;
                instructionPages.forEach((page,i) => { page.hidden = i !== instructionState.page; });
                instructionCountdown.textContent = instructionState.remaining + "s";
            } else if (productionState === "black") {
                blackScreen.hidden = false;
                document.getElementById("hold-screen-message").textContent = control.slideshow_message || "Please enjoy this special moment.";
            } else if (productionState === "emergency") {
                emergencyScreen.hidden = false;
                document.getElementById("emergency-title").textContent = control.slideshow_title || "We’ll be right back";
                document.getElementById("emergency-message").textContent = control.slideshow_message || "Preparing your memories…";
            } else if (productionState === "timeline") {
                timelineScreen.hidden = false;
                renderTimeline(timeline);
            } else if (productionState === "marker" && featuredMarker) {
                markerScreen.hidden = false;
                document.getElementById("marker-time").textContent = featuredMarker.scheduled_time || "A special moment";
                document.getElementById("marker-title").textContent = featuredMarker.title || "Event moment";
                document.getElementById("marker-detail").textContent = featuredMarker.detail || "";
            } else {
                logoScreen.hidden = false;
                document.getElementById("production-logo-message").textContent = control.slideshow_message || "Waiting for the next moment…";
            }
        }
        updateControls();
        sendHeartbeat();
    }

    async function pollFeed() {
        if (polling) return;
        polling = true;
        try {
            const separator = feedUrl.includes("?") ? "&" : "?";
            const response = await fetch(`${feedUrl}${separator}cursor=${encodeURIComponent(cursor)}`, { cache: "no-store", headers: { Accept: "application/json" } });
            const data = await response.json().catch(() => ({}));
            if (response.status === 409 || data.active === false) {
                eventActive = false;
                setConnection("ended", "Event ended");
                waitingCopy.textContent = "The event has ended. This display will keep the last received memory.";
                clearAdvanceTimer();
                return;
            }
            if (!response.ok) throw new Error(data.error || "Unable to update slideshow.");

            eventActive = true;
            setConnection("online", displayMode === "wired" ? "Wired live" : "Network live");
            if (data.event_id && data.event_id !== currentEventId) {
                window.location.reload();
                return;
            }
            if (data.reset) resetFeed(data.event_id || currentEventId);
            removePhotos(data.removed_ids || []);
            const prioritize = hasCompletedInitialFeed;
            const added = addPhotos(data.photos, prioritize);
            cursor = Number.isFinite(Number(data.cursor)) ? Number(data.cursor) : cursor;
            applyTheme(data.event?.theme);
            sharedSyncEnabled = data.sync?.mode === "server";
            if (sharedSyncEnabled) clearAdvanceTimer();
            instructionState = data.instruction || {active:false,page:0,remaining:0};
            applyControl(data.control || {}, data.timeline || [], data.featured_marker || null);
            if (productionState === "instructions") {
                instructionsScreen.hidden = false;
                instructionPages.forEach((page,i) => { page.hidden = i !== instructionState.page; });
                instructionCountdown.textContent = instructionState.remaining + "s";
            }
            if (sharedSyncEnabled && data.sync.photo_id && productionState === "playing") {
                const target = photos.find(p => p.id === data.sync.photo_id);
                if (target && currentPhotoId !== target.id && syncPhotoPending !== target.id) {
                    syncPhotoPending = target.id;
                    try { await showPhoto(target, true); } finally { syncPhotoPending = null; }
                }
            }

            if (!sharedSyncEnabled && !currentPhotoId && photos.length && productionState === "playing") await showPhoto(photos[0], false);
            else if (!sharedSyncEnabled && added.length && prioritize && playing && productionState === "playing") scheduleAdvance(Math.min(2.5, secondsPerPhoto));

            if (!photos.length) {
                waitingScreen.classList.remove("is-hidden");
                waitingCopy.textContent = "Photos will appear here as guests share them.";
            }
            hasCompletedInitialFeed = true;
            updateControls();
        } catch (_error) {
            setConnection("offline", "Reconnecting");
            if (!currentPhotoId) waitingCopy.textContent = "Reconnecting to the ARCOBALENO event server…";
        } finally {
            polling = false;
        }
    }

    function togglePlay() {
        playing = !playing;
        updateControls();
        showToast(playing ? "Local slideshow resumed" : "Local slideshow paused", 1300);
        if (playing) scheduleAdvance();
        else clearAdvanceTimer();
    }

    function toggleInfo() {
        infoVisible = !infoVisible;
        app.classList.toggle("hide-info", !infoVisible);
        updateControls();
        revealControls();
    }

    async function toggleFullscreen() {
        try {
            if (!document.fullscreenElement) await document.documentElement.requestFullscreen();
            else await document.exitFullscreen();
        } catch (_error) {
            showToast("Fullscreen was blocked. Press F11 in the browser instead.", 3500);
        }
    }

    function updateFullscreenButton() {
        const fullscreen = Boolean(document.fullscreenElement);
        fullscreenButton.setAttribute("aria-label", fullscreen ? "Exit fullscreen" : "Enter fullscreen");
        fullscreenButton.title = fullscreen ? "Exit fullscreen" : "Enter fullscreen";
    }

    async function sendHeartbeat() {
        if (!heartbeatUrl || !displayId) return;
        const form = new FormData();
        form.append("_csrf_token", csrfToken);
        form.append("display_id", displayId);
        form.append("mode", displayMode);
        form.append("current_photo_id", currentPhotoId || "");
        form.append("state", productionState);
        form.append("label", `${screen.width}×${screen.height}`);
        try {
            await fetch(heartbeatUrl, { method: "POST", body: form, headers: { "X-CSRF-Token": csrfToken }, keepalive: true });
        } catch (_error) {
            // Feed connectivity already communicates the failure on-screen.
        }
    }

    function hideControlsSoon() {
        window.clearTimeout(controlsTimer);
        if (!playing) return;
        controlsTimer = window.setTimeout(() => {
            if (!controls.matches(":focus-within")) app.classList.add("controls-hidden");
        }, 3200);
    }

    function revealControls() {
        app.classList.remove("controls-hidden");
        hideControlsSoon();
    }

    previousButton.addEventListener("click", () => { advance(-1, true); revealControls(); });
    nextButton.addEventListener("click", () => { advance(1, true); revealControls(); });
    playButton.addEventListener("click", () => { togglePlay(); revealControls(); });
    infoButton.addEventListener("click", toggleInfo);
    fullscreenButton.addEventListener("click", toggleFullscreen);
    document.getElementById("start-fullscreen-display")?.addEventListener("click", async () => {
        await toggleFullscreen();
        fullscreenGate.hidden = true;
    });

    document.addEventListener("fullscreenchange", updateFullscreenButton);
    document.addEventListener("keydown", (event) => {
        if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement) return;
        if (event.code === "Space") { event.preventDefault(); togglePlay(); }
        else if (event.key === "ArrowLeft") { event.preventDefault(); advance(-1, true); }
        else if (event.key === "ArrowRight") { event.preventDefault(); advance(1, true); }
        else if (event.key.toLowerCase() === "f") { event.preventDefault(); toggleFullscreen(); }
        else if (event.key.toLowerCase() === "i") { event.preventDefault(); toggleInfo(); }
        revealControls();
    });

    ["mousemove", "pointerdown", "touchstart"].forEach((eventName) => document.addEventListener(eventName, revealControls, { passive: true }));
    window.addEventListener("offline", () => setConnection("offline", "Offline"));
    window.addEventListener("online", pollFeed);
    document.addEventListener("visibilitychange", () => {
        if (document.hidden) clearAdvanceTimer();
        else { pollFeed(); scheduleAdvance(); sendHeartbeat(); }
    });

    updateControls();
    updateFullscreenButton();
    window.setTimeout(() => keyboardHint.classList.add("is-hidden"), 7000);
    pollFeed();
    sendHeartbeat();
    window.setInterval(pollFeed, pollSeconds * 1000);
    window.setInterval(sendHeartbeat, 5000);
    hideControlsSoon();
})();
