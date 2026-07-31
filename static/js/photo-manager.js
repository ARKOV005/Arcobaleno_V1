(() => {
    "use strict";

    const app = document.getElementById("photo-manager");
    if (!app) return;

    const listUrl = app.dataset.listUrl;
    const liveUrl = app.dataset.liveUrl;
    const snapshotUrl = app.dataset.snapshotUrl;
    const slideshowControlUrl = app.dataset.slideshowControlUrl;
    const photoLimitUrl = app.dataset.photoLimitUrl;
    const csrfToken = app.dataset.csrfToken;
    const around = app.dataset.around || "";

    const grid = document.getElementById("live-photo-grid");
    const emptyState = document.getElementById("photo-manager-empty");
    const loadOlderButton = document.getElementById("load-older-photos");
    const limitNote = document.getElementById("photo-manager-limit-note");
    const liveDot = document.getElementById("photo-live-dot");
    const liveDetail = document.getElementById("photo-live-detail");
    const flowPanel = document.getElementById("event-console-flow");
    const flowLabel = document.getElementById("event-flow-label");
    const flowDetail = document.getElementById("event-flow-detail");
    const feedFreezeButton = document.getElementById("feed-freeze-button");
    const feedFreezeLabel = document.getElementById("feed-freeze-label");
    const freezePanel = document.getElementById("feed-freeze-panel");
    const queueCount = document.getElementById("feed-queue-count");
    const releaseNextButton = document.getElementById("release-next-photo");
    const releaseFiveButton = document.getElementById("release-five-photos");
    const resumeFeedButton = document.getElementById("resume-feed-button");
    const slideshowToggleButton = document.getElementById("slideshow-toggle-button");
    const slideshowToggleLabel = document.getElementById("slideshow-toggle-label");
    const slideshowToggleDetail = document.getElementById("slideshow-toggle-detail");
    const holdButton = document.getElementById("hold-screen-button");
    const emergencyButton = document.getElementById("emergency-screen-button");
    const slideshowStateLabel = document.getElementById("pm-slideshow-state");
    const newBanner = document.getElementById("new-photo-banner");
    const newCount = document.getElementById("new-photo-count");
    const searchInput = document.getElementById("photo-search-input");
    const filterBar = document.getElementById("photo-filter");
    const photoLimitForm = document.getElementById("photo-limit-form");
    const photoLimitInput = document.getElementById("photo-limit-input");
    const photoLimitLabel = document.getElementById("pm-limit-label");
    const photoLimitHelp = document.getElementById("photo-limit-help");
    const lightbox = document.getElementById("photo-lightbox");
    const lightboxImage = document.getElementById("photo-lightbox-image");
    const lightboxName = document.getElementById("photo-lightbox-name");
    const lightboxTime = document.getElementById("photo-lightbox-time");
    const lightboxActions = document.getElementById("photo-lightbox-actions");

    const photos = new Map();
    const orderedIds = [];
    const queuedNewIds = [];
    const queuedPhotoPayloads = new Map();
    const fetchingPhotoIds = new Set();
    const releasingPhotoIds = new Set();
    const MAX_ACTIVE = 300;
    const RECONCILE_PAGE_SIZE = 120;
    const RECONCILE_MAX_PAGES = 5;
    const RECONCILE_INTERVAL_MS = 5000;

    let currentFilter = "all";
    let searchTerm = "";
    let nextBefore = "";
    let hasMore = false;
    let loading = false;
    let reconciling = false;
    let source = null;
    let reconnectTimer = null;
    let selectedPhotoId = null;
    let lastLiveSequence = 0;
    let feedFrozen = false;
    let connectionState = "reconnecting";
    let lastSuccessfulSyncAt = 0;
    const SYNC_HEALTH_WINDOW_MS = 15000;
    let slideshowState = app.dataset.initialSlideshowState || "playing";
    let photoLimit = Number(app.dataset.initialPhotoLimit || 0);

    function toast(message, kind = "info") {
        if (window.ArcobalenoToast) window.ArcobalenoToast(message, kind);
    }

    function formatTime(value) {
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return String(value || "");
        return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
    }

    function formatBytes(value) {
        let size = Number(value || 0);
        const units = ["B", "KB", "MB", "GB"];
        let index = 0;
        while (size >= 1024 && index < units.length - 1) {
            size /= 1024;
            index += 1;
        }
        return `${size.toFixed(index ? 1 : 0)} ${units[index]}`;
    }

    function friendlyState(state) {
        const labels = {
            playing: "Playing",
            paused: "Paused",
            black: "Hold screen",
            logo: "Logo screen",
            emergency: "Emergency",
            timeline: "Timeline",
            marker: "Marker",
        };
        return labels[state] || "Playing";
    }

    function statusLabel(photo) {
        if (photo.moderation_status === "pending") return "Review";
        if (photo.moderation_status === "hidden") return "Hidden";
        return "Ready";
    }

    function isNearTop() {
        return window.scrollY < 360;
    }

    function updateCounts(counts = {}) {
        const fields = {
            total: "pm-total",
            pending: "pm-pending",
            hidden: "pm-hidden",
            favorites: "pm-favorites",
        };
        Object.entries(fields).forEach(([key, id]) => {
            const node = document.getElementById(id);
            if (node && typeof counts[key] !== "undefined") node.textContent = String(counts[key]);
        });
    }

    function setPhotoLimit(value) {
        photoLimit = Math.max(0, Number(value) || 0);
        photoLimitInput.value = String(photoLimit);
        photoLimitLabel.textContent = photoLimit === 0 ? "Unlimited" : `${photoLimit} each`;
    }

    function hasRecentSuccessfulSync() {
        return Date.now() - lastSuccessfulSyncAt <= SYNC_HEALTH_WINDOW_MS;
    }

    function markSyncHealthy() {
        lastSuccessfulSyncAt = Date.now();
        if (connectionState !== "online") setConnectionState("online");
    }

    function updateFlowState() {
        const automatic = !feedFrozen && slideshowState === "playing" && connectionState === "online";
        flowPanel.dataset.state = automatic ? "automatic" : "operator";
        if (automatic) {
            flowLabel.textContent = "Hands-free active";
            flowDetail.textContent = "Feed live · slideshow playing";
        } else if (connectionState !== "online") {
            flowLabel.textContent = "Reconnecting";
            flowDetail.textContent = "Photos remain stored safely";
        } else {
            flowLabel.textContent = "Operator control active";
            const parts = [];
            if (feedFrozen) parts.push(`${queuedNewIds.length} waiting`);
            if (slideshowState !== "playing") parts.push(friendlyState(slideshowState));
            flowDetail.textContent = parts.join(" · ") || "One control is paused";
        }
    }

    function updateSlideshowControls() {
        slideshowStateLabel.textContent = friendlyState(slideshowState);
        const inactive = slideshowState !== "playing";
        slideshowToggleLabel.textContent = inactive ? "Resume slideshow" : "Pause slideshow";
        slideshowToggleDetail.textContent = inactive ? "Return to automatic play" : "Projector keeps current photo";
        slideshowToggleButton.classList.toggle("is-active", slideshowState === "paused");
        holdButton.classList.toggle("is-active", slideshowState === "black");
        emergencyButton.classList.toggle("is-active", slideshowState === "emergency");
        updateFlowState();
    }

    function setConnectionState(state) {
        connectionState = state;
        liveDot.classList.toggle("is-offline", state !== "online");
        liveDot.classList.toggle("is-paused", feedFrozen && state === "online");
        if (state === "online") {
            liveDetail.textContent = feedFrozen
                ? "Feed frozen locally. Guest uploads continue saving."
                : "New thumbnails appear automatically. No refresh required.";
        } else {
            liveDetail.textContent = "Reconnecting to the live feed. Missed thumbnails are recovered automatically.";
        }
        updateFlowState();
    }

    function updateQueueUi() {
        const count = queuedNewIds.length;
        queueCount.textContent = `${count} ${count === 1 ? "photo" : "photos"} waiting`;
        newCount.textContent = `${count} ${count === 1 ? "photo" : "photos"} waiting`;
        releaseNextButton.disabled = count === 0;
        releaseFiveButton.disabled = count === 0;
        newBanner.hidden = feedFrozen || count === 0;
        freezePanel.hidden = !feedFrozen;
        feedFreezeButton.dataset.state = feedFrozen ? "frozen" : "live";
        feedFreezeButton.classList.toggle("is-active", feedFrozen);
        feedFreezeLabel.textContent = feedFrozen ? "Resume feed" : "Freeze feed";
        liveDot.classList.toggle("is-paused", feedFrozen && connectionState === "online");
        setConnectionState(connectionState);
    }

    function isKnownOrPending(photoId) {
        return photos.has(photoId)
            || queuedNewIds.includes(photoId)
            || releasingPhotoIds.has(photoId)
            || fetchingPhotoIds.has(photoId);
    }

    function queuePhoto(photoId, photo = null) {
        if (!photoId || photos.has(photoId) || releasingPhotoIds.has(photoId)) return;
        if (photo) queuedPhotoPayloads.set(photoId, photo);
        if (!queuedNewIds.includes(photoId)) queuedNewIds.push(photoId);
        updateQueueUi();
    }

    function photoRecordChanged(previous, current) {
        if (!previous || !current) return true;
        const keys = [
            "guest_name",
            "created_at",
            "size_bytes",
            "moderation_status",
            "moderation_mode",
            "auto_approve_at",
            "favorite",
            "thumbnail_url",
            "original_url",
            "download_url",
            "moderate_url",
            "delete_url",
            "session_key",
        ];
        return keys.some((key) => previous[key] !== current[key]);
    }

    function matches(photo) {
        if (currentFilter === "favorites" && !photo.favorite) return false;
        if (["approved", "pending", "hidden"].includes(currentFilter) && photo.moderation_status !== currentFilter) return false;
        if (searchTerm && !String(photo.guest_name || "").toLowerCase().includes(searchTerm)) return false;
        return true;
    }

    function actionButton(label, action, className = "") {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `button button-small ${className}`.trim();
        button.textContent = label;
        button.dataset.action = action;
        return button;
    }

    function createMoreMenu(photo) {
        const details = document.createElement("details");
        details.className = "photo-card-more";
        const summary = document.createElement("summary");
        summary.setAttribute("aria-label", "More photo actions");
        summary.textContent = "•••";
        const menu = document.createElement("div");
        menu.className = "photo-card-menu";

        const open = document.createElement("button");
        open.type = "button";
        open.textContent = "Open photo";
        open.dataset.openPhoto = photo.id;
        const download = document.createElement("a");
        download.href = photo.download_url;
        download.textContent = "Download original";
        download.setAttribute("download", "");
        const remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "Delete permanently";
        remove.className = "photo-menu-danger";
        remove.dataset.action = "delete";
        menu.append(open, download, remove);
        details.append(summary, menu);
        details.addEventListener("toggle", () => {
            const card = details.closest(".live-photo-card");
            card?.classList.toggle("is-menu-open", details.open);
            if (!details.open) return;
            grid.querySelectorAll(".photo-card-more[open]").forEach((other) => {
                if (other !== details) other.open = false;
            });
        });
        return details;
    }

    function createActions(photo, compact = false) {
        const wrapper = document.createElement("div");
        wrapper.className = compact ? "photo-actions photo-actions-compact" : "photo-actions photo-actions-simple";
        wrapper.dataset.photoId = photo.id;

        if (photo.moderation_status === "pending") {
            wrapper.append(actionButton("Approve", "approve", "button-primary"));
            wrapper.append(actionButton("Hide", "hide", "button-danger"));
        } else if (photo.moderation_status === "hidden") {
            wrapper.append(actionButton("Restore", "unhide", "button-primary"));
        } else {
            wrapper.append(actionButton("Hide", "hide", "button-secondary"));
        }
        wrapper.append(actionButton(photo.favorite ? "★" : "☆", photo.favorite ? "unfavorite" : "favorite", "button-secondary photo-favorite-action"));
        if (compact) {
            const download = document.createElement("a");
            download.className = "button button-secondary button-small";
            download.href = photo.download_url;
            download.textContent = "Download";
            download.setAttribute("download", "");
            wrapper.append(download, actionButton("Delete", "delete", "button-quiet-danger"));
        } else {
            wrapper.append(createMoreMenu(photo));
        }
        return wrapper;
    }

    function createCard(photo) {
        const card = document.createElement("article");
        card.className = `photo-card live-photo-card status-${photo.moderation_status}`;
        card.dataset.photoId = photo.id;

        const previewButton = document.createElement("button");
        previewButton.type = "button";
        previewButton.className = "photo-preview photo-preview-button";
        previewButton.dataset.openPhoto = photo.id;

        const image = document.createElement("img");
        image.src = photo.thumbnail_url;
        image.alt = `Photo uploaded by ${photo.guest_name || "Guest"}`;
        image.loading = "lazy";
        image.decoding = "async";
        previewButton.append(image);

        const badge = document.createElement("span");
        badge.className = `moderation-badge moderation-${photo.moderation_status}`;
        badge.textContent = statusLabel(photo);
        previewButton.append(badge);

        if (photo.favorite) {
            const favorite = document.createElement("span");
            favorite.className = "favorite-badge";
            favorite.textContent = "★";
            previewButton.append(favorite);
        }

        const body = document.createElement("div");
        body.className = "photo-card-body photo-card-body-compact";
        const copy = document.createElement("div");
        const title = document.createElement("h2");
        title.textContent = photo.guest_name || "Guest";
        const meta = document.createElement("p");
        meta.className = "photo-meta";
        meta.textContent = `${formatTime(photo.created_at)} · ${formatBytes(photo.size_bytes)}`;
        copy.append(title, meta);
        body.append(copy, createActions(photo));
        card.append(previewButton, body);
        return card;
    }

    function render() {
        const fragment = document.createDocumentFragment();
        let shown = 0;
        for (const id of orderedIds) {
            const photo = photos.get(id);
            if (!photo || !matches(photo)) continue;
            fragment.append(createCard(photo));
            shown += 1;
            if (shown >= MAX_ACTIVE) break;
        }
        grid.replaceChildren(fragment);
        emptyState.hidden = shown > 0;
        limitNote.hidden = orderedIds.length <= MAX_ACTIVE;
        loadOlderButton.hidden = !hasMore || orderedIds.length >= MAX_ACTIVE;
    }

    function insertOrUpdate(photo, prepend = true) {
        if (!photo?.id) return;
        const exists = photos.has(photo.id);
        photos.set(photo.id, photo);
        if (!exists) {
            if (prepend) orderedIds.unshift(photo.id);
            else orderedIds.push(photo.id);
        }
    }

    async function requestPhotos({ append = false } = {}) {
        if (loading) return;
        loading = true;
        loadOlderButton.disabled = true;
        try {
            const url = new URL(listUrl, window.location.origin);
            url.searchParams.set("limit", append ? "60" : "100");
            if (append && nextBefore) url.searchParams.set("before", nextBefore);
            if (around) url.searchParams.set("around", around);
            const response = await fetch(url, { cache: "no-store" });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.error || "Could not load the photo library.");
            markSyncHealthy();
            if (!append) {
                photos.clear();
                orderedIds.splice(0, orderedIds.length);
            }
            for (const photo of data.photos || []) insertOrUpdate(photo, false);
            lastLiveSequence = Math.max(lastLiveSequence, Number(data.live_sequence || 0));
            updateCounts(data.counts || {});
            setPhotoLimit(data.photo_limit ?? photoLimit);
            hasMore = Boolean(data.has_more);
            nextBefore = data.next_before || "";
            render();
        } catch (error) {
            toast(error.message || "Could not load the photo library.", "error");
        } finally {
            loading = false;
            loadOlderButton.disabled = false;
        }
    }

    async function reconcilePhotos() {
        if (reconciling || loading) return;
        reconciling = true;
        try {
            const recovered = [];
            let before = "";
            let reachedKnownBoundary = false;
            let knownPhotoChanged = false;
            let latestCounts = null;
            let latestPhotoLimit = null;

            for (let page = 0; page < RECONCILE_MAX_PAGES && !reachedKnownBoundary; page += 1) {
                const url = new URL(listUrl, window.location.origin);
                url.searchParams.set("limit", String(RECONCILE_PAGE_SIZE));
                if (before) url.searchParams.set("before", before);
                if (around) url.searchParams.set("around", around);

                const response = await fetch(url, { cache: "no-store" });
                const data = await response.json().catch(() => ({}));
                if (!response.ok) throw new Error(data.error || "Could not reconcile the live photo feed.");
                markSyncHealthy();

                latestCounts = data.counts || latestCounts;
                latestPhotoLimit = data.photo_limit ?? latestPhotoLimit;
                const batch = Array.isArray(data.photos) ? data.photos : [];

                for (const photo of batch) {
                    if (!photo?.id) continue;

                    if (photos.has(photo.id)) {
                        const previous = photos.get(photo.id);
                        if (photoRecordChanged(previous, photo)) {
                            photos.set(photo.id, photo);
                            knownPhotoChanged = true;
                        }
                        reachedKnownBoundary = true;
                        break;
                    }

                    if (queuedNewIds.includes(photo.id)) {
                        queuedPhotoPayloads.set(photo.id, photo);
                        continue;
                    }

                    if (releasingPhotoIds.has(photo.id) || fetchingPhotoIds.has(photo.id)) {
                        continue;
                    }

                    recovered.push(photo);
                }

                if (reachedKnownBoundary || !data.has_more || !data.next_before || batch.length === 0) break;
                before = data.next_before;
            }

            if (latestCounts) updateCounts(latestCounts);
            if (latestPhotoLimit !== null) setPhotoLimit(latestPhotoLimit);

            let addedToGrid = false;
            for (const photo of recovered.slice().reverse()) {
                if (!photo?.id || isKnownOrPending(photo.id)) {
                    if (photo?.id && queuedNewIds.includes(photo.id)) queuedPhotoPayloads.set(photo.id, photo);
                    continue;
                }
                if (feedFrozen || !isNearTop()) {
                    queuePhoto(photo.id, photo);
                } else {
                    insertOrUpdate(photo, true);
                    addedToGrid = true;
                }
            }

            if (knownPhotoChanged || addedToGrid) render();
        } catch (_error) {
            // EventSource remains the primary live channel. The next reconciliation retries quietly.
        } finally {
            reconciling = false;
        }
    }

    async function fetchPhoto(photoId, { force = false, renderAfter = true } = {}) {
        if (!force && (feedFrozen || !isNearTop())) {
            queuePhoto(photoId);
            return false;
        }
        if (fetchingPhotoIds.has(photoId)) return false;
        fetchingPhotoIds.add(photoId);
        try {
            const queuedPhoto = queuedPhotoPayloads.get(photoId);
            if (queuedPhoto) {
                queuedPhotoPayloads.delete(photoId);
                insertOrUpdate(queuedPhoto, true);
                if (renderAfter) render();
                return true;
            }
            const response = await fetch(`/api/operator/photos/${encodeURIComponent(photoId)}`, { cache: "no-store" });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.error || "Photo unavailable.");
            insertOrUpdate(data.photo, true);
            if (renderAfter) render();
            return true;
        } catch (_error) {
            // Reconciliation and EventSource reconnects recover missed items safely.
            return false;
        } finally {
            fetchingPhotoIds.delete(photoId);
        }
    }

    async function releaseQueued(count = Infinity, { resume = false, scroll = true } = {}) {
        if (resume) feedFrozen = false;
        const amount = Math.min(queuedNewIds.length, Number.isFinite(count) ? count : queuedNewIds.length);
        const ids = queuedNewIds.splice(0, amount);
        ids.forEach((id) => releasingPhotoIds.add(id));
        updateQueueUi();
        let changed = false;
        for (const id of ids) {
            changed = await fetchPhoto(id, { force: true, renderAfter: false }) || changed;
            releasingPhotoIds.delete(id);
        }
        if (changed) render();
        if (scroll && ids.length) window.scrollTo({ top: 0, behavior: "smooth" });
        updateQueueUi();
    }

    async function moderate(photoId, action) {
        const photo = photos.get(photoId);
        if (!photo) return;
        if (action === "delete" && !window.confirm("Delete this photo permanently from the active event?")) return;
        const endpoint = action === "delete" ? photo.delete_url : photo.moderate_url;
        const form = new FormData();
        form.append("_csrf_token", csrfToken);
        if (action !== "delete") form.append("action", action);
        try {
            const response = await fetch(endpoint, {
                method: "POST",
                body: form,
                headers: { Accept: "application/json", "X-CSRF-Token": csrfToken },
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.error || "The photo could not be updated.");
            if (action === "delete") {
                photos.delete(photoId);
                const index = orderedIds.indexOf(photoId);
                if (index >= 0) orderedIds.splice(index, 1);
                if (selectedPhotoId === photoId) lightbox.close();
                toast("Photo deleted.");
            } else {
                insertOrUpdate(data.photo, true);
                toast(action === "hide" ? "Photo hidden from every slideshow." : "Photo updated.");
                if (selectedPhotoId === photoId) openLightbox(photoId);
            }
            updateCounts(data.counts || {});
            render();
        } catch (error) {
            toast(error.message || "The photo could not be updated.", "error");
        }
    }

    function openLightbox(photoId) {
        const photo = photos.get(photoId);
        if (!photo) return;
        selectedPhotoId = photoId;
        lightboxImage.src = photo.original_url;
        lightboxImage.alt = `Photo uploaded by ${photo.guest_name || "Guest"}`;
        lightboxName.textContent = photo.guest_name || "Guest";
        lightboxTime.textContent = `${formatTime(photo.created_at)} · ${statusLabel(photo)} · session ${photo.session_key || "unknown"}`;
        lightboxActions.replaceChildren(createActions(photo, true));
        if (!lightbox.open) lightbox.showModal();
    }

    async function postSlideshowControl(fields) {
        const form = new FormData();
        form.append("_csrf_token", csrfToken);
        Object.entries(fields).forEach(([key, value]) => form.append(key, value));
        const response = await fetch(slideshowControlUrl, {
            method: "POST",
            body: form,
            headers: { Accept: "application/json", "X-CSRF-Token": csrfToken },
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || "The slideshow control could not be changed.");
        if (data.control) applyControl(data.control);
        return data;
    }

    function applyControl(control = {}) {
        if (control.slideshow_state) slideshowState = control.slideshow_state;
        updateSlideshowControls();
    }

    async function setSlideshowState(state) {
        try {
            const fields = { state };
            if (state === "emergency") {
                fields.title = "We’ll be right back";
                fields.message = "Preparing your memories…";
            } else if (state === "black") {
                fields.message = "Please enjoy this special moment.";
            }
            await postSlideshowControl(fields);
            toast(state === "playing" ? "Slideshow resumed." : `${friendlyState(state)} active.`);
        } catch (error) {
            toast(error.message || "The slideshow control could not be changed.", "error");
        }
    }

    function parseEvent(event) {
        // Receiving any SSE message proves the live channel is healthy.
        // Some Windows production-server/browser combinations deliver the
        // custom stream.ready event before firing EventSource.onopen.
        if (connectionState !== "online") setConnectionState("online");
        try {
            const data = JSON.parse(event.data);
            lastLiveSequence = Math.max(lastLiveSequence, Number(data.sequence || event.lastEventId || 0));
            return data;
        } catch (_error) {
            return {};
        }
    }

    function connectLive() {
        if (source) source.close();
        setConnectionState("reconnecting");
        const url = new URL(liveUrl, window.location.origin);
        url.searchParams.set("since", String(lastLiveSequence));
        source = new EventSource(url);
        source.onopen = markSyncHealthy;
        source.addEventListener("stream.ready", parseEvent);
        source.onerror = () => {
            // Some Windows production-server combinations periodically close SSE.
            // Keep the console healthy while snapshot/reconciliation sync is succeeding.
            if (!hasRecentSuccessfulSync()) setConnectionState("reconnecting");
            source?.close();
            window.clearTimeout(reconnectTimer);
            reconnectTimer = window.setTimeout(connectLive, 2500);
        };
        source.addEventListener("photo.uploaded", (event) => {
            const data = parseEvent(event);
            const payload = data.payload || {};
            updateCounts(payload.counts || {});
            if (payload.photo_id) fetchPhoto(payload.photo_id);
            if (!feedFrozen && isNearTop()) {
                const guest = payload.guest_name || "A guest";
                toast(`New upload from ${guest}.`, payload.moderation_status === "pending" ? "warning" : "info");
            }
        });
        source.addEventListener("photo.updated", (event) => {
            const data = parseEvent(event);
            const payload = data.payload || {};
            if (payload.photo) {
                const photoId = payload.photo.id;
                if (photos.has(photoId)) insertOrUpdate(payload.photo, true);
                else if (queuedNewIds.includes(photoId) || feedFrozen || !isNearTop()) queuePhoto(photoId, payload.photo);
                else insertOrUpdate(payload.photo, true);
            }
            updateCounts(payload.counts || {});
            render();
        });
        source.addEventListener("photo.deleted", (event) => {
            const data = parseEvent(event);
            const payload = data.payload || {};
            if (payload.photo_id) {
                photos.delete(payload.photo_id);
                const index = orderedIds.indexOf(payload.photo_id);
                if (index >= 0) orderedIds.splice(index, 1);
                const queueIndex = queuedNewIds.indexOf(payload.photo_id);
                if (queueIndex >= 0) queuedNewIds.splice(queueIndex, 1);
                queuedPhotoPayloads.delete(payload.photo_id);
                releasingPhotoIds.delete(payload.photo_id);
                fetchingPhotoIds.delete(payload.photo_id);
                render();
                updateQueueUi();
            }
            updateCounts(payload.counts || {});
        });
        source.addEventListener("control.updated", (event) => {
            const data = parseEvent(event);
            applyControl(data.payload?.control || {});
        });
        source.addEventListener("settings.updated", (event) => {
            const data = parseEvent(event);
            if (typeof data.payload?.photo_limit !== "undefined") setPhotoLimit(data.payload.photo_limit);
        });
    }

    async function refreshSnapshot() {
        try {
            const response = await fetch(snapshotUrl, { cache: "no-store" });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) return;
            markSyncHealthy();
            updateCounts(data.counts || {});
            const guests = document.getElementById("pm-guests");
            if (guests) guests.textContent = String(data.guest_count ?? guests.textContent);
            applyControl(data.control || {});
            setPhotoLimit(data.photo_limit ?? photoLimit);
            const now = Date.now();
            let changed = false;
            for (const photo of photos.values()) {
                if (photo.moderation_status === "pending" && photo.auto_approve_at) {
                    const due = new Date(photo.auto_approve_at).getTime();
                    if (Number.isFinite(due) && due <= now && photo.moderation_mode === "family") {
                        photo.moderation_status = "approved";
                        changed = true;
                    }
                }
            }
            if (changed) render();
        } catch (_error) {
            // The live connection indicator communicates reconnecting state.
        }
    }

    async function savePhotoLimit(event) {
        event.preventDefault();
        const submit = photoLimitForm.querySelector("button[type='submit']");
        submit.disabled = true;
        photoLimitHelp.textContent = "Saving…";
        const form = new FormData();
        form.append("_csrf_token", csrfToken);
        form.append("photo_limit", photoLimitInput.value);
        try {
            const response = await fetch(photoLimitUrl, {
                method: "POST",
                body: form,
                headers: { Accept: "application/json", "X-CSRF-Token": csrfToken },
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.error || "The photo limit could not be saved.");
            setPhotoLimit(data.photo_limit);
            photoLimitHelp.textContent = data.photo_limit === 0
                ? "Guests may now upload unlimited photos from each device."
                : `Each guest device may upload up to ${data.photo_limit} photos.`;
            toast("Guest photo limit updated.");
        } catch (error) {
            photoLimitHelp.textContent = error.message || "The photo limit could not be saved.";
            toast(photoLimitHelp.textContent, "error");
        } finally {
            submit.disabled = false;
        }
    }

    grid.addEventListener("click", (event) => {
        const open = event.target.closest("[data-open-photo]");
        if (open) return openLightbox(open.dataset.openPhoto);
        const action = event.target.closest("[data-action]");
        const wrapper = action?.closest("[data-photo-id]");
        if (action && wrapper) moderate(wrapper.dataset.photoId, action.dataset.action);
    });
    document.addEventListener("click", (event) => {
        grid.querySelectorAll(".photo-card-more[open]").forEach((details) => {
            if (!details.contains(event.target)) details.open = false;
        });
    });
    document.addEventListener("keydown", (event) => {
        if (event.key !== "Escape") return;
        grid.querySelectorAll(".photo-card-more[open]").forEach((details) => {
            details.open = false;
        });
    });
    lightboxActions.addEventListener("click", (event) => {
        const action = event.target.closest("[data-action]");
        const wrapper = action?.closest("[data-photo-id]");
        if (action && wrapper) moderate(wrapper.dataset.photoId, action.dataset.action);
    });
    document.getElementById("photo-lightbox-close")?.addEventListener("click", () => lightbox.close());
    lightbox.addEventListener("click", (event) => { if (event.target === lightbox) lightbox.close(); });
    lightbox.addEventListener("close", () => {
        selectedPhotoId = null;
        lightboxImage.removeAttribute("src");
    });

    filterBar.addEventListener("click", (event) => {
        const button = event.target.closest("[data-filter]");
        if (!button) return;
        currentFilter = button.dataset.filter;
        filterBar.querySelectorAll("button").forEach((item) => item.classList.toggle("is-active", item === button));
        render();
    });
    searchInput.addEventListener("input", () => {
        searchTerm = searchInput.value.trim().toLowerCase();
        render();
    });
    loadOlderButton.addEventListener("click", () => requestPhotos({ append: true }));
    newBanner.addEventListener("click", () => releaseQueued(Infinity, { resume: false }));
    feedFreezeButton.addEventListener("click", () => {
        if (feedFrozen) releaseQueued(Infinity, { resume: true });
        else {
            feedFrozen = true;
            updateQueueUi();
            toast("Feed frozen. Guest uploads are still being saved.");
        }
    });
    releaseNextButton.addEventListener("click", () => releaseQueued(1, { resume: false, scroll: false }));
    releaseFiveButton.addEventListener("click", () => releaseQueued(5, { resume: false, scroll: false }));
    resumeFeedButton.addEventListener("click", () => releaseQueued(Infinity, { resume: true }));
    slideshowToggleButton.addEventListener("click", () => setSlideshowState(slideshowState === "playing" ? "paused" : "playing"));
    holdButton.addEventListener("click", () => setSlideshowState(slideshowState === "black" ? "playing" : "black"));
    emergencyButton.addEventListener("click", () => setSlideshowState(slideshowState === "emergency" ? "playing" : "emergency"));
    document.querySelectorAll("[data-slideshow-command]").forEach((button) => {
        button.addEventListener("click", async () => {
            try {
                await postSlideshowControl({ command: button.dataset.slideshowCommand });
                toast(`Slideshow ${button.dataset.slideshowCommand} command sent.`);
            } catch (error) {
                toast(error.message || "The slideshow command could not be sent.", "error");
            }
        });
    });
    photoLimitForm.addEventListener("submit", savePhotoLimit);

    window.addEventListener("beforeunload", () => source?.close());
    updateQueueUi();
    updateSlideshowControls();
    requestPhotos().then(connectLive);
    refreshSnapshot();
    window.setInterval(refreshSnapshot, 10000);
    window.setInterval(reconcilePhotos, RECONCILE_INTERVAL_MS);
})();
