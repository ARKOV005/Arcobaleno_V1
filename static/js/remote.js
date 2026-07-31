(() => {
    "use strict";
    const app = document.getElementById("remote-app");
    if (!app) return;
    const snapshotUrl = app.dataset.snapshotUrl;
    const controlUrl = app.dataset.controlUrl;
    const csrf = app.dataset.csrfToken;
    const pollMs = Math.max(1000, Number(app.dataset.pollSeconds || 3) * 1000);
    const $ = (id) => document.getElementById(id);
    let busy = false;
    let lastAlertSignature = "";
    let timer = null;

    const titleCase = (value) => String(value || "").replace(/-/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
    const bytes = (value) => {
        let size = Number(value || 0); const units = ["B", "KB", "MB", "GB", "TB"]; let index = 0;
        while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
        return `${size.toFixed(index ? 1 : 0)} ${units[index]}`;
    };
    const toast = (message) => {
        const node = $("remote-toast"); node.textContent = message; node.classList.add("is-visible");
        window.setTimeout(() => node.classList.remove("is-visible"), 2200);
    };
    const setPill = (node, text, tone = "") => {
        node.textContent = text; node.className = `remote-pill${tone ? ` is-${tone}` : ""}`;
    };
    const connection = (online, message) => {
        const node = $("remote-connection"); node.classList.toggle("is-online", online); node.classList.toggle("is-offline", !online);
        node.querySelector("strong").textContent = message;
    };
    const renderAlerts = (alerts) => {
        const node = $("remote-alerts"); node.replaceChildren();
        (alerts || []).forEach((alert) => {
            const item = document.createElement("div");
            item.className = `remote-alert${alert.level === "critical" || alert.level === "danger" ? " remote-alert-danger" : ""}`;
            item.textContent = alert.message; node.appendChild(item);
        });
        const signature = JSON.stringify(alerts || []);
        if (signature !== "[]" && signature !== lastAlertSignature && "vibrate" in navigator) navigator.vibrate([120, 60, 120]);
        lastAlertSignature = signature;
    };
    const disableActions = (disabled) => document.querySelectorAll("[data-action]").forEach((button) => button.disabled = disabled);

    const render = (data) => {
        if (!data.active) {
            $("remote-event-title").textContent = "No active event";
            $("remote-event-detail").textContent = "Create or reopen an event on the laptop.";
            setPill($("remote-mode-pill"), "Idle"); disableActions(true); renderAlerts([]); return;
        }
        disableActions(false);
        const event = data.event || {}; const control = data.control || {}; const counts = data.counts || {}; const displays = data.displays || {}; const storage = data.storage || {}; const allowance = data.allowance || {};
        $("remote-event-title").textContent = event.title || "Current event";
        $("remote-event-detail").textContent = [event.hosts, event.event_date, event.venue].filter(Boolean).join(" · ") || "Event workspace active";
        setPill($("remote-mode-pill"), control.event_mode === "live" ? "● Live" : "Setup", control.event_mode === "live" ? "live" : "");
        $("remote-photo-count").textContent = Number(data.photo_count || 0).toLocaleString();
        $("remote-approved-count").textContent = `${Number(counts.approved || 0).toLocaleString()} approved`;
        $("remote-guest-count").textContent = Number(data.guest_count || 0).toLocaleString();
        $("remote-exhausted-count").textContent = allowance.unlimited ? "∞" : Number(allowance.exhausted_devices || 0).toLocaleString();
        $("remote-pending-count").textContent = Number(counts.pending || 0).toLocaleString();
        $("health-server").textContent = data.network_ready ? `Online · ${data.network_ip}` : "Network attention";
        $("health-camera").textContent = control.uploads_open ? "Accepting uploads" : "Uploads paused";
        $("health-display").textContent = `${Number(displays.total || 0)} connected · ${titleCase(control.slideshow_state)}`;
        $("health-storage").textContent = `${titleCase(storage.level)} · ${bytes(storage.free_bytes)} free`;
        const healthy = data.network_ready && storage.level === "healthy" && !(control.event_mode === "live" && Number(displays.total || 0) === 0);
        setPill($("remote-health-pill"), healthy ? "Healthy" : "Attention", healthy ? "good" : "warning");
        setPill($("remote-upload-pill"), control.uploads_open ? "Open" : "Paused", control.uploads_open ? "good" : "warning");
        setPill($("remote-display-pill"), titleCase(control.slideshow_state || "Ready"), Number(displays.total || 0) ? "good" : "");
        renderAlerts(data.alerts || []);
        const latest = data.latest_photo;
        const panel = $("remote-latest-panel");
        if (latest && latest.thumbnail_url) {
            panel.hidden = false; $("remote-latest-name").textContent = latest.guest_name || "Guest";
            $("remote-latest-time").textContent = latest.created_at ? new Date(latest.created_at).toLocaleTimeString([], {hour:"numeric", minute:"2-digit"}) : "Latest";
            const image = $("remote-latest-image"); const nextSrc = `${latest.thumbnail_url}?v=${encodeURIComponent(latest.id)}`; if (image.src !== new URL(nextSrc, location.href).href) image.src = nextSrc;
            ["remote-latest-approve", "remote-latest-hide", "remote-latest-favorite"].forEach((id) => { $(id).dataset.photoId = latest.id; });
            $("remote-latest-approve").disabled = latest.moderation_status === "approved";
            $("remote-latest-hide").disabled = latest.moderation_status === "hidden";
            const favoriteButton = $("remote-latest-favorite");
            favoriteButton.dataset.action = latest.favorite ? "photo-unfavorite" : "photo-favorite";
            favoriteButton.querySelector("strong").textContent = latest.favorite ? "Unfavorite" : "Favorite";
        } else panel.hidden = true;
        $("remote-updated").textContent = `Updated ${new Date().toLocaleTimeString([], {hour:"numeric",minute:"2-digit",second:"2-digit"})}`;
    };

    const refresh = async () => {
        try {
            const response = await fetch(snapshotUrl, {cache:"no-store", headers:{"Accept":"application/json"}});
            if (response.status === 403) { location.href = "/remote/login"; return; }
            const data = await response.json(); if (!response.ok) throw new Error(data.error || "Could not read server status.");
            render(data); connection(true, "Connected to event laptop");
        } catch (error) { connection(false, "Connection lost — retrying"); }
    };

    const sendAction = async (button) => {
        if (busy) return;
        const action = button.dataset.action; const confirmation = button.dataset.confirm;
        if (confirmation && !window.confirm(confirmation)) return;
        const form = new FormData(); form.set("action", action);
        if (button.dataset.photoId) form.set("photo_id", button.dataset.photoId);
        if (action === "archive-event") {
            const value = $("remote-archive-word").value.trim().toUpperCase();
            if (value !== "ARCHIVE") { toast("Type ARCHIVE to confirm"); return; }
            form.set("confirmation", value);
        }
        busy = true; button.classList.add("is-busy"); disableActions(true);
        try {
            const response = await fetch(controlUrl, {method:"POST", body:form, headers:{"X-CSRF-Token":csrf,"Accept":"application/json"}});
            const data = await response.json(); if (!response.ok) throw new Error(data.error || "Action failed.");
            render(data.snapshot || data); toast(action === "archive-event" ? "Event archived safely" : "Control updated");
            if (action === "archive-event") { $("remote-archive-confirm").hidden = true; $("remote-archive-word").value = ""; }
        } catch (error) { toast(error.message || "Action failed"); }
        finally { busy = false; button.classList.remove("is-busy"); disableActions(false); }
    };

    document.querySelectorAll("[data-action]").forEach((button) => button.addEventListener("click", () => sendAction(button)));
    $("remote-archive-open").addEventListener("click", () => { $("remote-archive-confirm").hidden = false; $("remote-archive-word").focus(); });
    $("remote-archive-cancel").addEventListener("click", () => { $("remote-archive-confirm").hidden = true; $("remote-archive-word").value = ""; });
    refresh(); timer = window.setInterval(refresh, pollMs);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
    window.addEventListener("beforeunload", () => window.clearInterval(timer));
})();
