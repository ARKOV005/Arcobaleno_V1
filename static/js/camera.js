(() => {
    "use strict";

    const app = document.getElementById("camera-app");
    if (!app) return;

    const eventId = app.dataset.eventId;
    const statusUrl = app.dataset.statusUrl;
    const uploadUrl = app.dataset.uploadUrl;
    const csrfToken = app.dataset.csrfToken;
    const storageKey = `arcobaleno_guest_name:${eventId}`;
    const conductStorageKey = `arcobaleno_guest_conduct:${eventId}`;
    const facingStorageKey = `arcobaleno_camera_facing:${eventId}`;

    const themeNames = ["default", "floral", "secret-garden", "ocean", "ballroom", "disco"];

    function applyTheme(value) {
        const theme = themeNames.includes(String(value || "")) ? String(value) : "default";
        for (const name of themeNames) document.body.classList.remove(`theme-${name}`);
        document.body.classList.add(`theme-${theme}`);
        app.dataset.theme = theme;
    }

    applyTheme(app.dataset.theme);

    const stage = document.getElementById("camera-stage");
    const video = document.getElementById("camera-video");
    const preview = document.getElementById("photo-preview");
    const canvas = document.getElementById("capture-canvas");
    const input = document.getElementById("camera-input");
    const primary = document.getElementById("primary-action");
    const retake = document.getElementById("retake-button");
    const switchCamera = document.getElementById("switch-camera");
    const status = document.getElementById("camera-status");
    const modeMessage = document.getElementById("camera-mode-message");
    const remainingPill = document.getElementById("remaining-pill");
    const remainingCount = document.getElementById("remaining-count");
    const remainingLabel = document.getElementById("remaining-label");
    const identityButton = document.getElementById("identity-button");
    const guestNameDisplay = document.getElementById("guest-name-display");
    const nameGate = document.getElementById("name-gate");
    const nameForm = document.getElementById("name-form");
    const nameInput = document.getElementById("guest-name-input");
    const nameError = document.getElementById("name-error");
    const conductAgreement = document.getElementById("guest-conduct-agreement");
    const stageFeedback = document.getElementById("stage-feedback");
    const stageFeedbackIcon = document.getElementById("stage-feedback-icon");
    const stageFeedbackTitle = document.getElementById("stage-feedback-title");
    const stageFeedbackSubtitle = document.getElementById("stage-feedback-subtitle");
    const stageFeedbackProgress = document.getElementById("stage-feedback-progress");
    const stageFeedbackProgressBar = document.getElementById("stage-feedback-progress-bar");
    const guestTimeline = document.getElementById("guest-timeline");
    const guestTimelineList = document.getElementById("guest-timeline-list");

    let guestName = "";
    let selectedBlob = null;
    let selectedFilename = "camera-photo.jpg";
    let currentUploadId = null;
    let previewUrl = null;
    let stream = null;
    let facingMode = readStoredFacingMode();
    let liveMode = false;
    let limitAllowed = app.dataset.initialRemaining !== "0";
    let uploadsOpen = app.dataset.initialUploadsOpen !== "false";
    let storageOpen = app.dataset.initialStorageOpen !== "false";
    let allowed = limitAllowed && uploadsOpen && storageOpen;
    const MAX_AUTOMATIC_UPLOAD_RETRIES = 3;
    const UPLOAD_RETRY_DELAYS_MS = [1500, 3000, 6000];

    let statusTimer = null;
    let statusRequestInFlight = false;
    let uploadInFlight = false;
    let uploadRetryCount = 0;
    let uploadRetryTimer = null;
    let pendingRetryNumber = 0;
    let activeUploadRequest = null;
    let pageClosing = false;
    let feedbackMode = "hidden";

    function safeStorageGet(key) {
        try {
            return window.localStorage.getItem(key);
        } catch (_error) {
            return null;
        }
    }

    function safeStorageSet(key, value) {
        try {
            window.localStorage.setItem(key, value);
        } catch (_error) {
            // Private browsing may block storage. The camera still works for this visit.
        }
    }

    function readStoredFacingMode() {
        return safeStorageGet(facingStorageKey) === "user" ? "user" : "environment";
    }

    function cleanName(value) {
        return String(value || "").trim().replace(/\s+/g, " ").slice(0, 80);
    }

    function unavailableLabel() {
        if (!storageOpen) return "Storage Full";
        if (!uploadsOpen) return "Uploads Paused";
        return "Photo Limit Reached";
    }

    function showStageFeedback(kind, title, subtitle, options = {}) {
        if (!stageFeedback) return;

        feedbackMode = kind;
        stage.dataset.feedback = kind;
        stageFeedback.hidden = false;
        stageFeedback.className = `stage-feedback stage-feedback-${kind}`;
        stageFeedbackTitle.textContent = title || "";
        stageFeedbackSubtitle.textContent = subtitle || "";
        stageFeedbackIcon.textContent = options.icon || "";
        stageFeedbackIcon.hidden = !options.icon;

        if (options.progress) {
            stageFeedbackProgress.hidden = false;
            const percent = Math.max(0, Math.min(100, Number(options.progressPercent || 0)));
            stageFeedbackProgressBar.style.width = `${percent}%`;
        } else {
            stageFeedbackProgress.hidden = true;
            stageFeedbackProgressBar.style.width = "0%";
        }
    }

    function hideStageFeedback(force = false) {
        if (!stageFeedback) return;
        if (!force && (feedbackMode === "paused" || feedbackMode === "offline" || feedbackMode === "storage")) return;
        feedbackMode = "hidden";
        delete stage.dataset.feedback;
        stageFeedback.hidden = true;
        stageFeedback.className = "stage-feedback";
        stageFeedbackProgress.hidden = true;
        stageFeedbackProgressBar.style.width = "0%";
    }

    function setGuestName(value) {
        guestName = cleanName(value);
        guestNameDisplay.textContent = guestName || "Enter your name";
        if (guestName) safeStorageSet(storageKey, guestName);
    }

    function openNameGate() {
        nameInput.value = guestName;
        if (conductAgreement) conductAgreement.checked = safeStorageGet(conductStorageKey) === "accepted";
        nameError.textContent = "";
        nameGate.hidden = false;
        window.setTimeout(() => nameInput.focus(), 40);
    }

    function closeNameGate() {
        nameGate.hidden = true;
    }

    function stopStream() {
        if (stream) stream.getTracks().forEach((track) => track.stop());
        stream = null;
        liveMode = false;
        switchCamera.hidden = true;
        video.srcObject = null;
        stage.classList.remove("live");
    }

    function syncControls() {
        allowed = limitAllowed && uploadsOpen && storageOpen;

        switchCamera.hidden = !(liveMode && !selectedBlob && !uploadInFlight);
        retake.hidden = !selectedBlob;
        retake.disabled = uploadInFlight;

        primary.classList.toggle("ready", Boolean(selectedBlob));
        primary.disabled = uploadInFlight || !allowed;

        if (selectedBlob) {
            if (!uploadInFlight) primary.textContent = "Send Photo";
            else if (uploadRetryTimer) primary.textContent = "Retrying…";
            else primary.textContent = uploadRetryCount > 0 ? "Resending…" : "Sending…";
            return;
        }

        primary.textContent = allowed
            ? (liveMode ? "Take Photo" : "Open Camera")
            : unavailableLabel();
    }

    function clearUploadRetryTimer() {
        if (uploadRetryTimer) window.clearTimeout(uploadRetryTimer);
        uploadRetryTimer = null;
        pendingRetryNumber = 0;
    }

    function resetUploadRetryState() {
        clearUploadRetryTimer();
        uploadRetryCount = 0;
        activeUploadRequest = null;
    }

    function clearPreview() {
        resetUploadRetryState();
        selectedBlob = null;
        selectedFilename = "camera-photo.jpg";
        currentUploadId = null;
        input.value = "";
        if (previewUrl) URL.revokeObjectURL(previewUrl);
        previewUrl = null;
        preview.removeAttribute("src");
        stage.classList.remove("preview");
        if (liveMode) stage.classList.add("live");
        hideStageFeedback();
        syncControls();
    }

    function showPreview(blob, filename) {
        resetUploadRetryState();
        selectedBlob = blob;
        selectedFilename = filename || "camera-photo.jpg";
        currentUploadId = crypto.randomUUID
            ? crypto.randomUUID()
            : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
        if (previewUrl) URL.revokeObjectURL(previewUrl);
        previewUrl = URL.createObjectURL(blob);
        preview.src = previewUrl;
        stage.classList.remove("live");
        stage.classList.add("preview");
        hideStageFeedback();
        status.textContent = "Review the photo, then retake or send it.";
        syncControls();
    }

    function applyUploadControl(data) {
        if (typeof data.uploads_open !== "undefined") {
            uploadsOpen = Boolean(data.uploads_open);
        }
        const message = data.pause_message
            || app.dataset.initialPauseMessage
            || "Photo uploads are temporarily paused.";

        if (!uploadsOpen) {
            showStageFeedback("paused", "Upload paused", message, { icon: "Ⅱ" });
            status.textContent = message;
        } else if (feedbackMode === "paused") {
            hideStageFeedback(true);
        }

        syncControls();
    }

    function applyStorageControl(data) {
        if (typeof data.storage_accepting_uploads !== "undefined") {
            storageOpen = Boolean(data.storage_accepting_uploads);
        }
        const message = data.storage_message
            || app.dataset.initialStorageMessage
            || "The event server is low on storage. Please tell the operator.";

        if (!storageOpen) {
            showStageFeedback("storage", "Server storage full", message, { icon: "!" });
            status.textContent = message;
        } else if (feedbackMode === "storage") {
            hideStageFeedback(true);
        }

        syncControls();
    }

    function renderTimeline(entries) {
        if (!guestTimeline || !guestTimelineList || !Array.isArray(entries)) return;
        guestTimeline.hidden = entries.length === 0;
        guestTimelineList.replaceChildren();
        entries.forEach((entry) => {
            const article = document.createElement("article");
            article.className = "guest-timeline-entry";
            const time = document.createElement("time");
            time.textContent = entry.scheduled_time || "Soon";
            const copy = document.createElement("div");
            const title = document.createElement("strong");
            title.textContent = entry.title || "Event moment";
            copy.appendChild(title);
            if (entry.detail) {
                const detail = document.createElement("span");
                detail.textContent = entry.detail;
                copy.appendChild(detail);
            }
            article.append(time, copy);
            guestTimelineList.appendChild(article);
        });
    }

    function updateRemaining(data) {
        const unlimited = Boolean(data.unlimited);
        if (typeof data.allowed !== "undefined") {
            limitAllowed = Boolean(data.allowed);
        } else if (unlimited) {
            limitAllowed = true;
        }

        remainingCount.textContent = unlimited ? "∞" : String(data.remaining ?? 0);
        remainingLabel.textContent = unlimited ? "unlimited shots" : "shots left";
        remainingPill.classList.toggle("remaining-empty", !unlimited && Number(data.remaining ?? 0) <= 0);
        remainingPill.setAttribute(
            "aria-label",
            unlimited ? "Unlimited shots" : `${data.remaining ?? 0} shots left`,
        );
        applyUploadControl(data);
        applyStorageControl(data);
    }

    async function refreshStatus() {
        if (statusRequestInFlight || document.hidden) return;
        statusRequestInFlight = true;
        const controller = new AbortController();
        const timeoutId = window.setTimeout(() => controller.abort(), 5000);

        try {
            const response = await fetch(statusUrl, {
                cache: "no-store",
                signal: controller.signal,
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || "Unable to check camera status.");
            if (feedbackMode === "offline" && !uploadInFlight) hideStageFeedback(true);
            updateRemaining(data);
            applyTheme(data.event?.theme);
            renderTimeline(data.timeline);
        } catch (error) {
            const message = error?.name === "AbortError"
                ? "The server is taking too long to respond."
                : (error.message || "Unable to check camera status.");
            showStageFeedback(
                "offline",
                "Connection lost",
                "Stay on the event network. ARCOBALENO will keep retrying.",
                { icon: "!" },
            );
            status.textContent = message;
        } finally {
            window.clearTimeout(timeoutId);
            statusRequestInFlight = false;
        }
    }

    async function startLiveCamera() {
        if (!navigator.mediaDevices?.getUserMedia) return false;

        try {
            showStageFeedback("loading", "Preparing camera", "Please wait a moment.", { progress: true, progressPercent: 30 });
            stopStream();
            stream = await navigator.mediaDevices.getUserMedia({
                video: { facingMode: { ideal: facingMode } },
                audio: false,
            });
            video.srcObject = stream;
            await video.play();
            liveMode = true;
            stage.classList.add("live");
            hideStageFeedback(true);
            modeMessage.textContent = "Live camera ready.";
            status.textContent = "Frame the moment, then take the photo.";
            if (!uploadsOpen) {
                applyUploadControl({
                    uploads_open: false,
                    pause_message: app.dataset.initialPauseMessage,
                });
            }
            if (!storageOpen) {
                applyStorageControl({
                    storage_accepting_uploads: false,
                    storage_message: app.dataset.initialStorageMessage,
                });
            }
            syncControls();
            return true;
        } catch (_error) {
            stopStream();
            hideStageFeedback(true);
            syncControls();
            return false;
        }
    }

    function openNativeCamera() {
        input.setAttribute("capture", "environment");
        showStageFeedback("loading", "Preparing camera", "Your phone camera will open in its own screen.", { progress: true, progressPercent: 100 });
        window.setTimeout(() => {
            if (!selectedBlob) hideStageFeedback(true);
        }, 1200);
        input.click();
    }

    function captureLiveFrame() {
        if (!video.videoWidth || !video.videoHeight) {
            status.textContent = "The camera is still starting. Try again in a moment.";
            return;
        }

        canvas.width = video.videoWidth;
        canvas.height = video.videoHeight;
        const context = canvas.getContext("2d");
        if (!context) {
            status.textContent = "The photo could not be prepared. Please try again.";
            return;
        }

        context.drawImage(video, 0, 0, canvas.width, canvas.height);
        canvas.toBlob((blob) => {
            if (blob) showPreview(blob, `arcobaleno-${Date.now()}.jpg`);
        }, "image/jpeg", 0.94);
    }

    function isTemporaryUploadFailure(statusCode) {
        return [0, 408, 425, 500, 502, 503, 504].includes(Number(statusCode || 0));
    }

    function finishConnectionFailure(message) {
        clearUploadRetryTimer();
        uploadInFlight = false;
        activeUploadRequest = null;
        showStageFeedback(
            "error",
            "Connection trouble",
            message || "The photo is still ready. Tap Send Photo to try again.",
            { icon: "!" },
        );
        status.textContent = message || "The photo is still ready. Tap Send Photo to try again.";
        syncControls();
    }

    function queueAutomaticUploadRetry(message) {
        activeUploadRequest = null;

        if (pageClosing) {
            uploadInFlight = false;
            return;
        }

        if (uploadRetryCount >= MAX_AUTOMATIC_UPLOAD_RETRIES) {
            finishConnectionFailure(
                "ARCOBALENO could not reconnect after several attempts. Your photo is still here—tap Send Photo to retry.",
            );
            return;
        }

        const retryNumber = uploadRetryCount + 1;
        const delay = UPLOAD_RETRY_DELAYS_MS[Math.min(retryNumber - 1, UPLOAD_RETRY_DELAYS_MS.length - 1)];
        pendingRetryNumber = retryNumber;
        uploadInFlight = true;

        const waitingMessage = navigator.onLine === false
            ? "Waiting for the event Wi-Fi. Your photo is still safe on this screen."
            : `Retrying automatically (${retryNumber} of ${MAX_AUTOMATIC_UPLOAD_RETRIES})…`;
        showStageFeedback("offline", "Connection interrupted", waitingMessage, { icon: "!" });
        status.textContent = message || waitingMessage;
        syncControls();

        const retryWhenReady = () => {
            if (pageClosing || !selectedBlob) return;
            if (navigator.onLine === false) {
                showStageFeedback(
                    "offline",
                    "Waiting for event Wi-Fi",
                    "Your photo is still safe on this screen. ARCOBALENO will retry when the connection returns.",
                    { icon: "!" },
                );
                status.textContent = "Waiting for the event Wi-Fi. Your photo has not been lost.";
                uploadRetryTimer = window.setTimeout(retryWhenReady, 2000);
                syncControls();
                return;
            }

            uploadRetryTimer = null;
            uploadRetryCount = pendingRetryNumber;
            pendingRetryNumber = 0;
            sendUploadAttempt();
        };

        clearUploadRetryTimer();
        pendingRetryNumber = retryNumber;
        uploadRetryTimer = window.setTimeout(retryWhenReady, delay);
        syncControls();
    }

    function handlePermanentUploadFailure(data) {
        uploadInFlight = false;
        activeUploadRequest = null;
        clearUploadRetryTimer();

        if (typeof data.remaining !== "undefined") updateRemaining(data);
        else {
            if (typeof data.uploads_open !== "undefined") applyUploadControl(data);
            if (typeof data.storage_accepting_uploads !== "undefined") applyStorageControl(data);
        }

        if (data.storage_accepting_uploads === false) {
            showStageFeedback("storage", "Server storage full", data.storage_message || data.error, { icon: "!" });
        } else if (data.integrity_error === true) {
            showStageFeedback("error", "Photo could not be read", data.error || "Retake it or choose another photo.", { icon: "!" });
        } else {
            showStageFeedback("error", "Upload failed", data.error || "Please try again.", { icon: "!" });
        }
        status.textContent = data.error || "Upload failed. Please try again.";
        syncControls();
    }

    function handleUploadSuccess(data) {
        uploadInFlight = false;
        activeUploadRequest = null;
        clearUploadRetryTimer();
        updateRemaining(data);

        const isPending = data.moderation_status === "pending";
        const isCurated = data.moderation_mode === "curated";
        const wasRecoveredRetry = Boolean(data.duplicate);
        const successTitle = wasRecoveredRetry ? "Photo already received" : "Photo shared successfully";
        const successSubtitle = wasRecoveredRetry
            ? "The earlier send completed safely. No duplicate was created."
            : (isCurated
                ? "It was sent to the event operator for approval."
                : (isPending ? "It will join the slideshow after the short safety buffer." : "It is ready for the slideshow."));

        showStageFeedback("success", successTitle, successSubtitle, { icon: "✓", progress: true, progressPercent: 100 });
        status.textContent = wasRecoveredRetry
            ? "Photo confirmed. No duplicate was created."
            : (isCurated
                ? "Photo sent for operator review."
                : (isPending ? "Photo received and waiting briefly before display." : "Photo sent successfully."));

        window.setTimeout(() => {
            clearPreview();
            status.textContent = allowed
                ? "Ready for another memory."
                : "Thank you! This device reached the photo limit.";
        }, 1200);
    }

    function sendUploadAttempt() {
        if (!selectedBlob || !guestName || pageClosing) {
            uploadInFlight = false;
            syncControls();
            return;
        }

        uploadInFlight = true;
        const attemptLabel = uploadRetryCount > 0
            ? `Resending photo (${uploadRetryCount} of ${MAX_AUTOMATIC_UPLOAD_RETRIES})…`
            : "Keep this page open while the photo is sent.";
        showStageFeedback("loading", uploadRetryCount > 0 ? "Reconnecting and resending" : "Uploading photo", attemptLabel, { progress: true, progressPercent: 0 });
        status.textContent = attemptLabel;
        syncControls();

        const formData = new FormData();
        formData.append("photo", selectedBlob, selectedFilename);
        formData.append("guest_name", guestName);
        // The same ID is reused for every retry. If the first request reached the
        // server but its response was lost, the retry confirms that photo instead
        // of storing it or deducting a shot twice.
        formData.append("upload_id", currentUploadId);

        const xhr = new XMLHttpRequest();
        activeUploadRequest = xhr;
        xhr.open("POST", uploadUrl);
        xhr.setRequestHeader("X-CSRF-Token", csrfToken);
        xhr.responseType = "json";
        xhr.timeout = 90000;

        xhr.upload.addEventListener("progress", (event) => {
            if (!event.lengthComputable) return;
            const percent = Math.max(1, Math.round((event.loaded / event.total) * 100));
            showStageFeedback(
                "loading",
                uploadRetryCount > 0 ? "Reconnecting and resending" : "Uploading photo",
                `Sending photo… ${percent}%`,
                { progress: true, progressPercent: percent },
            );
        });

        xhr.addEventListener("load", () => {
            const data = xhr.response || {};
            if (xhr.status >= 200 && xhr.status < 300) {
                handleUploadSuccess(data);
                return;
            }
            if (isTemporaryUploadFailure(xhr.status)) {
                queueAutomaticUploadRetry(data.error || "The event server is temporarily unavailable.");
                return;
            }
            handlePermanentUploadFailure(data);
        });

        xhr.addEventListener("error", () => {
            queueAutomaticUploadRetry("The connection was interrupted while sending the photo.");
        });

        xhr.addEventListener("timeout", () => {
            queueAutomaticUploadRetry("The event server took too long to respond.");
        });

        xhr.addEventListener("abort", () => {
            if (pageClosing) return;
            queueAutomaticUploadRetry("The upload connection stopped unexpectedly.");
        });

        xhr.addEventListener("loadend", () => {
            if (activeUploadRequest === xhr) activeUploadRequest = null;
        });

        xhr.send(formData);
    }

    function uploadPhoto() {
        if (!selectedBlob || !guestName || uploadInFlight) return;
        uploadRetryCount = 0;
        clearUploadRetryTimer();
        sendUploadAttempt();
    }

    nameForm.addEventListener("submit", (event) => {
        event.preventDefault();
        const value = cleanName(nameInput.value);
        if (!value) {
            nameError.textContent = "Enter your name to continue.";
            return;
        }
        if (conductAgreement && !conductAgreement.checked) {
            nameError.textContent = "Confirm that you will keep uploads respectful.";
            return;
        }
        safeStorageSet(conductStorageKey, "accepted");
        setGuestName(value);
        closeNameGate();
    });

    identityButton.addEventListener("click", openNameGate);

    input.addEventListener("change", () => {
        const file = input.files?.[0];
        if (file) showPreview(file, file.name || `arcobaleno-${Date.now()}.jpg`);
        else if (!selectedBlob) hideStageFeedback(true);
    });

    primary.addEventListener("click", () => {
        if (uploadInFlight) return;
        if (!guestName) {
            openNameGate();
            return;
        }
        if (selectedBlob) {
            uploadPhoto();
            return;
        }
        if (!allowed) return;
        if (liveMode) captureLiveFrame();
        else openNativeCamera();
    });

    retake.addEventListener("click", () => {
        if (uploadInFlight) return;
        clearPreview();
        if (liveMode) {
            status.textContent = "Frame the moment, then take the photo.";
        } else {
            openNativeCamera();
        }
    });

    switchCamera.addEventListener("click", async () => {
        if (!liveMode || selectedBlob || uploadInFlight) return;
        facingMode = facingMode === "environment" ? "user" : "environment";
        safeStorageSet(facingStorageKey, facingMode);
        status.textContent = "Switching camera…";
        await startLiveCamera();
    });

    window.addEventListener("offline", () => {
        showStageFeedback("offline", "Connection lost", "Stay on the event network. ARCOBALENO will keep retrying.", { icon: "!" });
        status.textContent = "Connection lost. Stay on the event Wi-Fi and try again.";
    });
    window.addEventListener("online", () => {
        if (feedbackMode === "offline" && !uploadInFlight) hideStageFeedback(true);
        if (uploadInFlight && uploadRetryTimer && pendingRetryNumber > 0) {
            window.clearTimeout(uploadRetryTimer);
            uploadRetryTimer = null;
            uploadRetryCount = pendingRetryNumber;
            pendingRetryNumber = 0;
            sendUploadAttempt();
        }
        refreshStatus();
    });
    window.addEventListener("pageshow", refreshStatus);
    document.addEventListener("visibilitychange", () => {
        if (!document.hidden) refreshStatus();
    });
    window.addEventListener("beforeunload", () => {
        pageClosing = true;
        if (statusTimer) window.clearInterval(statusTimer);
        clearUploadRetryTimer();
        if (activeUploadRequest) activeUploadRequest.abort();
        stopStream();
    });

    const storedName = cleanName(safeStorageGet(storageKey));
    const conductAccepted = safeStorageGet(conductStorageKey) === "accepted";
    if (storedName) setGuestName(storedName);
    if (!storedName || !conductAccepted) openNameGate();

    applyUploadControl({
        uploads_open: uploadsOpen,
        pause_message: app.dataset.initialPauseMessage,
    });
    applyStorageControl({
        storage_accepting_uploads: storageOpen,
        storage_message: app.dataset.initialStorageMessage,
    });
    refreshStatus();
    statusTimer = window.setInterval(refreshStatus, 5000);

    startLiveCamera().then((started) => {
        if (!started) {
            modeMessage.textContent = "Your phone camera will open in its own screen.";
            status.textContent = uploadsOpen
                ? "Tap Open Camera whenever you are ready."
                : (app.dataset.initialPauseMessage || "Photo uploads are temporarily paused.");
            syncControls();
        }
    });
})();
