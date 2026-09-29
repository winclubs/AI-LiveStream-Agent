// ==============================================================================
// 17_operations.js - 运营工作实用工具导航交互模块
// ==============================================================================

function initOperationsPage() {
    bindOperationsSearch();
}

function bindOperationsSearch() {
    const searchInput = document.getElementById("operations-search-input");
    const container = document.getElementById("operations-card-container");
    if (!searchInput || !container) return;

    searchInput.addEventListener("input", () => {
        const query = (searchInput.value || "").trim().toLowerCase();
        const cards = container.querySelectorAll(".operation-card");

        cards.forEach(card => {
            const title = (card.getAttribute("data-title") || "").toLowerCase();
            const textContent = (card.textContent || "").toLowerCase();
            if (!query || title.includes(query) || textContent.includes(query)) {
                card.style.display = "flex";
            } else {
                card.style.display = "none";
            }
        });
    });
}

// 挂载到 window
window.initOperationsPage = initOperationsPage;
