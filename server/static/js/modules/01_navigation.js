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

            // 切入云端模型页时重新读取最新直播模式 (避免向导完成后模式显示滞后)
            if (tab === "settings") { loadSettings(); loadVisionConfig(); }
            if (tab === "gpu") { loadGpuAvatarProviders(); }
            if (tab === "wizard") { loadWizardAvatarProviders(); }
            if (tab === "knowledge") { loadKnowledgeList(); loadKnowledgeStatus(); }
            if (tab === "anchors") { if (typeof loadAnchors === 'function') loadAnchors(); }
            if (tab === "voices") {
                loadAudioDevices();
                loadVoiceTable();
                if (typeof loadSettings === 'function') {
                    loadSettings();
                } else {
                    if (typeof renderTTSEcosystemGrid === 'function') {
                        renderTTSEcosystemGrid(typeof currentSelectedTTSProvider !== 'undefined' ? currentSelectedTTSProvider : 'edge_tts');
                    }
                    if (typeof renderConfiguredTTS === 'function' && typeof cachedAllConfigs !== 'undefined') {
                        renderConfiguredTTS(cachedAllConfigs);
                    }
                }
                if (typeof ensureTTSKeyAndUrlFilled === 'function') {
                    ensureTTSKeyAndUrlFilled(typeof currentSelectedTTSProvider !== 'undefined' ? currentSelectedTTSProvider : 'cosyvoice');
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
