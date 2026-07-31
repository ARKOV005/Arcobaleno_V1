(() => {
    "use strict";

    document.querySelector("[data-print-page]")?.addEventListener("click", () => {
        window.print();
    });
})();
