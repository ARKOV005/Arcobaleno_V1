(() => {
    "use strict";

    const screen = document.getElementById("shutdown-screen");
    const label = document.getElementById("shutdown-status-label");
    const detail = document.getElementById("shutdown-status-detail");
    if (!screen || !label || !detail) return;

    const startedAt = Date.now();

    async function checkForShutdown() {
        try {
            const response = await fetch(`/health?shutdown-check=${Date.now()}`, {
                cache: "no-store",
                signal: AbortSignal.timeout(900),
            });
            if (!response.ok) throw new Error("Server unavailable");
            if (Date.now() - startedAt < 10000) {
                window.setTimeout(checkForShutdown, 450);
                return;
            }
            label.textContent = "Still finishing safely…";
            detail.textContent = "Wait a few more seconds. Do not force-close the terminal window yet.";
            window.setTimeout(checkForShutdown, 900);
        } catch (_error) {
            screen.classList.add("is-complete");
            label.textContent = "Shutdown complete";
            detail.textContent = "The server session closed cleanly. The terminal window has closed; you may now close this tab.";
        }
    }

    window.setTimeout(checkForShutdown, 700);
})();
