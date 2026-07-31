(() => {
    "use strict";
    const title = document.getElementById("timeline-title");
    const icon = document.getElementById("timeline-icon");
    document.querySelectorAll("[data-marker-title]").forEach((button) => {
        button.addEventListener("click", () => {
            if (title) title.value = button.dataset.markerTitle || "";
            if (icon) icon.value = button.dataset.markerIcon || "marker";
            title?.focus();
        });
    });

    const resumeForm = document.querySelector("[data-timeline-resume-form]");
    resumeForm?.addEventListener("submit", async (event) => {
        event.preventDefault();
        try {
            const response = await fetch(resumeForm.action, {
                method: "POST",
                body: new FormData(resumeForm),
                headers: { Accept: "application/json" },
            });
            if (!response.ok) throw new Error();
            window.ArcobalenoToast?.("Slideshow resumed.");
        } catch (_error) {
            window.ArcobalenoToast?.("Could not resume the slideshow.", "error");
        }
    });
})();
