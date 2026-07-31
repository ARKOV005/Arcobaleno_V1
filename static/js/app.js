(() => {
    "use strict";

    const serverPill = document.querySelector("#server-pill");
    const serverLabel = document.querySelector("#server-label");
    const liveTime = document.querySelector("#live-time");
    const navMore = document.querySelector(".nav-more");
    const toastStack = document.getElementById("app-toast-stack");
    const storageAlert = document.getElementById("global-storage-alert");
    const storageAlertTitle = document.getElementById("global-storage-alert-title");
    const storageAlertCopy = document.getElementById("global-storage-alert-copy");
    let lastStorageLevel = storageAlert?.dataset.storageLevel || "healthy";

    window.ArcobalenoToast = (message, kind = "info", duration = 3600) => {
        if (!toastStack || !message) return;
        const toast = document.createElement("div");
        toast.className = `app-toast app-toast-${kind}`;
        toast.setAttribute("role", kind === "error" ? "alert" : "status");
        const mark = document.createElement("span");
        mark.className = "app-toast-mark";
        mark.setAttribute("aria-hidden", "true");
        mark.textContent = kind === "error" ? "×" : kind === "warning" ? "!" : "●";
        const copy = document.createElement("span");
        copy.textContent = String(message);
        toast.append(mark, copy);
        toastStack.append(toast);
        requestAnimationFrame(() => toast.classList.add("is-visible"));
        window.setTimeout(() => {
            toast.classList.remove("is-visible");
            window.setTimeout(() => toast.remove(), 220);
        }, Math.max(1200, duration));
    };

    function updateClock() {
        if (!liveTime) return;
        liveTime.textContent = new Intl.DateTimeFormat(undefined, {
            dateStyle: "medium",
            timeStyle: "medium",
        }).format(new Date());
    }

    function formatBytes(value) {
        let size = Number(value || 0);
        const units = ["B", "KB", "MB", "GB", "TB"];
        let index = 0;
        while (size >= 1024 && index < units.length - 1) {
            size /= 1024;
            index += 1;
        }
        const digits = index === 0 ? 0 : 2;
        return `${size.toFixed(digits)} ${units[index]}`;
    }

    function updateStorageAlert(data) {
        if (!storageAlert || !data) return;
        const level = String(data.storage_level || "healthy");
        const free = formatBytes(data.storage_free_bytes);

        storageAlert.classList.remove("is-hidden", "is-warning", "is-critical");
        storageAlert.classList.add(level === "critical" ? "is-critical" : level === "warning" ? "is-warning" : "is-hidden");
        storageAlert.dataset.storageLevel = level;
        storageAlert.hidden = level === "healthy";
        storageAlert.setAttribute("role", level === "critical" ? "alert" : "status");

        if (level === "critical") {
            storageAlertTitle.textContent = "Uploads blocked to protect event photos";
            storageAlertCopy.textContent = `${free} free. Free space before guests continue uploading.`;
        } else if (level === "warning") {
            storageAlertTitle.textContent = "Event storage is running low";
            storageAlertCopy.textContent = `${free} free. Uploads remain open, but free space before the event grows larger.`;
        }

        if (level !== lastStorageLevel && level !== "healthy") {
            window.ArcobalenoToast(
                level === "critical"
                    ? "Guest uploads were blocked to protect event photos."
                    : "Event storage is running low.",
                level === "critical" ? "error" : "warning",
                7000,
            );
        }
        lastStorageLevel = level;
    }

    async function updateHealth() {
        if (!serverPill || !serverLabel) return;

        try {
            const response = await fetch("/health", { cache: "no-store" });
            if (!response.ok) throw new Error("Health check failed");
            const data = await response.json();
            serverPill.classList.remove("offline");
            serverLabel.textContent = "Server online";
            updateStorageAlert(data);
        } catch (_error) {
            serverPill.classList.add("offline");
            serverLabel.textContent = "Reconnecting";
        }
    }

    async function copyValue(button) {
        const value = button.dataset.copy;
        const scope = button.closest("[data-copy-scope]");
        const feedback = scope?.querySelector("[data-copy-feedback]")
            || document.querySelector("[data-copy-feedback]");
        if (!value || !feedback) return;

        try {
            await navigator.clipboard.writeText(value);
            feedback.textContent = "Guest link copied.";
            window.ArcobalenoToast("Guest link copied.");
        } catch (_error) {
            feedback.textContent = `Guest address: ${value}`;
        }
    }

    document.querySelectorAll("[data-copy]").forEach((button) => {
        button.addEventListener("click", () => copyValue(button));
    });

    document.querySelectorAll("[data-confirm]").forEach((form) => {
        form.addEventListener("submit", (event) => {
            const message = form.dataset.confirm || "Continue with this action?";
            const requiredText = String(form.dataset.confirmText || "").trim();
            if (requiredText) {
                const entered = window.prompt(`${message}\n\nType ${requiredText} to confirm.`);
                if (entered !== requiredText) event.preventDefault();
                return;
            }
            if (!window.confirm(message)) event.preventDefault();
        });
    });

    const endEventForm = document.querySelector("[data-confirm-end-event]");
    endEventForm?.addEventListener("submit", (event) => {
        const accepted = window.confirm(
            "End this event and move its complete workspace into the archive? This cannot be undone from the dashboard."
        );
        if (!accepted) event.preventDefault();
    });

    navMore?.querySelectorAll("a").forEach((link) => {
        link.addEventListener("click", () => navMore.removeAttribute("open"));
    });

    document.addEventListener("click", (event) => {
        if (navMore?.hasAttribute("open") && !navMore.contains(event.target)) {
            navMore.removeAttribute("open");
        }
    });

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") navMore?.removeAttribute("open");
    });

    updateClock();
    updateHealth();
    window.setInterval(updateClock, 1000);
    window.setInterval(updateHealth, 5000);
})();
