function initNavigation() {
    const navItems = document.querySelectorAll(".nav-item");
    navItems.forEach(item => {
        item.addEventListener("click", () => {
            navItems.forEach(n => n.classList.remove("active"));
            item.classList.add("active");

            const tab = item.getAttribute("data-tab");
            try { localStorage.setItem("active_admin_tab", tab); } catch (e) {}
            document.querySelectorAll(".tab-content").forEach(tc => tc.classList.remove("active"));
            const target = document.getElementById(`tab-${tab}`);
            if (target) target.classList.add("active");

            if (tab === "live") { if (typeof refreshLiveGpuTelemetry === 'function') refreshLiveGpuTelemetry(); }
            if (tab === "settings") { loadSettings(); loadVisionConfig(); }
            if (tab === "gpu") { loadGpuAvatarProviders(); }
            if (tab === "wizard") { loadWizardAvatarProviders(); }
            if (tab === "knowledge") { loadKnowledgeList(); loadKnowledgeStatus(); }
            if (tab === "anchors") { if (typeof loadAnchors === 'function') loadAnchors(); }
            if (tab === "voices") {
                loadAudioDevices();
                loadVoiceTable();

                // 动态决议当前应当选中的 TTS 引擎（严格以用户配置为准，无配置时才默认本地引擎）
                const resolvedTTS = (typeof resolveActiveOrPreferredTTSProvider === "function")
                    ? resolveActiveOrPreferredTTSProvider(typeof cachedAllConfigs !== "undefined" ? cachedAllConfigs : null)
                    : { providerId: "moss_tts_nano", config: null };

                if (typeof renderTTSEcosystemGrid === 'function') {
                    renderTTSEcosystemGrid(resolvedTTS.providerId);
                }
                if (typeof loadSettings === 'function') {
                    loadSettings();
                } else if (typeof renderConfiguredTTS === 'function' && typeof cachedAllConfigs !== 'undefined') {
                    renderConfiguredTTS(cachedAllConfigs);
                }
            }
        });
    });

    // 页面刷新后自动恢复用户之前停留的 Tab (如 voices 面板)，绝不强制回退到向导
    try {
        const lastTab = localStorage.getItem("active_admin_tab");
        if (lastTab && lastTab !== "wizard") {
            const lastItem = document.querySelector(`.nav-item[data-tab="${lastTab}"]`);
            if (lastItem) {
                setTimeout(() => lastItem.click(), 50);
            }
        }
    } catch (e) {}
}

// 2. 全双工 WebSocket 连接
