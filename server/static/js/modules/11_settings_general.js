async function loadSettings() {
    try {
        // 读取当前直播模式
        let requiredSet = null;
        try {
            const modeRes = await fetch(`${API_BASE}/settings/live-mode`);
            const modeJson = await modeRes.json();
            const gotoBtn = document.getElementById("btn-goto-wizard");
            if (modeJson.code === 0 && modeJson.data.mode_info) {
                requiredSet = new Set(modeJson.data.mode_info.required_configs);
                renderCurrentModeBadge(modeJson.data.mode_info);
                if (gotoBtn) gotoBtn.innerHTML = svg("compass", "icon-sm") + " 重新配置模式/角色";
            }
        } catch (e) { }

        // 获取全部配置
        const res = await fetch(`${API_BASE}/settings/configs`);
        const json = await res.json();
        if (json.code === 0) {
            cachedAllConfigs = json.data || [];

            // 1. 渲染大模型网格与多模型卡片列表 (高韧性防护)
            const selLLM = (typeof currentSelectedLLMProvider !== "undefined" && currentSelectedLLMProvider)
                ? currentSelectedLLMProvider
                : (window.currentSelectedLLMProvider || "deepseek");
            if (typeof renderLLMEcosystemGrid === "function") {
                renderLLMEcosystemGrid(selLLM);
            }
            if (typeof renderConfiguredLLMs === "function") {
                renderConfiguredLLMs(cachedAllConfigs);
            }

            // 若大模型编辑区尚未与激活项绑定过，优先把激活的大模型同步到编辑面板
            const activeLLM = cachedAllConfigs.find(c => c.config_group === "llm" && c.is_active);
            const llmEditorConfigEl = document.getElementById("llm-editor-config-id");
            if (typeof selectLLMProvider === "function") {
                if (activeLLM && llmEditorConfigEl && !llmEditorConfigEl.value) {
                    const pMeta = typeof resolveLLMProviderMeta === "function" ? resolveLLMProviderMeta(activeLLM.provider_name, activeLLM) : { id: activeLLM.provider_name };
                    selectLLMProvider(pMeta.id, activeLLM);
                } else if (llmEditorConfigEl && !llmEditorConfigEl.value) {
                    selectLLMProvider(selLLM);
                }
            }

            // 2. 渲染语音合成生态网格与已配置发音清单 (严格以用户端配置为准进行选中；无配置时默认本地引擎)
            const resolvedTTS = (typeof resolveActiveOrPreferredTTSProvider === "function")
                ? resolveActiveOrPreferredTTSProvider(cachedAllConfigs)
                : { providerId: "moss_tts_nano", config: null };

            const targetTTSId = resolvedTTS.providerId;
            const targetTTSConfig = resolvedTTS.config;

            window.currentSelectedTTSProvider = targetTTSId;
            if (typeof currentSelectedTTSProvider !== "undefined") currentSelectedTTSProvider = targetTTSId;

            if (typeof renderTTSEcosystemGrid === "function") renderTTSEcosystemGrid(targetTTSId);
            if (typeof renderConfiguredTTS === "function") renderConfiguredTTS(cachedAllConfigs);
            if (typeof selectTTSProvider === "function") {
                await selectTTSProvider(targetTTSId, targetTTSConfig);
            }

            // 联动刷新主播管理页的绑定音色下拉列表与当前生效引擎提示
            if (typeof populateAnchorVoiceSelect === "function") {
                const vList = window._voiceProfilesCache || (typeof voiceCache !== "undefined" ? voiceCache : []);
                populateAnchorVoiceSelect(vList);
            }

        }
    } catch (e) {
        console.error("加载配置失败", e);
    }
}

// 快速填入输入框辅助函数
function fillInputValue(inputId, value) {
    const el = document.getElementById(inputId);
    if (el) {
        el.value = value;
        el.focus();
    }
}

// 保存修改后的非 LLM 配置参数
async function saveSettingConfig(group, name, id, btn) {
    const urlInput = document.getElementById(`url-${id}`);
    const modelInput = document.getElementById(`model-${id}`);
    const keyInput = document.getElementById(`key-${id}`);
    const activeCheck = document.getElementById(`active-${id}`);
    const statusSpan = document.getElementById(`status-${id}`);

    const payload = {
        id: id,
        config_group: group,
        provider_name: name,
        base_url: urlInput ? urlInput.value.trim() : null,
        model_name: modelInput ? modelInput.value.trim() : null,
        is_active: activeCheck ? activeCheck.checked : true
    };

    if (keyInput && keyInput.value.trim()) {
        payload.api_key = keyInput.value.trim();
    }

    if (statusSpan) {
        statusSpan.style.color = "var(--text-secondary)";
        statusSpan.innerText = "正在加密保存...";
    }
    btn.disabled = true;

    try {
        const res = await fetch(`${API_BASE}/settings/configs/save`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        const json = await res.json();
        if (json.code === 0) {
            if (statusSpan) {
                statusSpan.style.color = "var(--accent-emerald)";
                statusSpan.innerText = "✓ 配置已安全保存生效！";
            }
            if (keyInput && keyInput.value.trim()) {
                keyInput.placeholder = "已加密更新，输入可继续修改";
                keyInput.value = "";
            }
        } else {
            if (statusSpan) {
                statusSpan.style.color = "var(--accent-danger)";
                statusSpan.innerText = "保存失败: " + json.message;
            }
        }
    } catch (e) {
        if (statusSpan) {
            statusSpan.style.color = "var(--accent-danger)";
            statusSpan.innerText = "保存出错: " + e;
        }
    } finally {
        btn.disabled = false;
        setTimeout(() => {
            if (statusSpan && statusSpan.innerText.includes("保存生效")) {
                statusSpan.innerText = "";
            }
        }, 4000);
    }
}

// 执行连通性测试 (使用当前输入框内的最新参数)
async function pingSettingConfig(group, name, id, btn) {
    const urlInput = document.getElementById(`url-${id}`);
    const keyInput = document.getElementById(`key-${id}`);
    const statusSpan = document.getElementById(`status-${id}`);

    if (statusSpan) {
        statusSpan.style.color = "var(--text-secondary)";
        statusSpan.innerText = "正在探测链路延迟...";
    }
    btn.disabled = true;

    const payload = {
        config_id: id,
        config_group: group,
        provider_name: name,
        base_url: urlInput ? urlInput.value.trim() : "",
        api_key: keyInput && keyInput.value.trim() ? keyInput.value.trim() : null
    };

    try {
        const res = await fetch(`${API_BASE}/settings/ping`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        const json = await res.json();
        if (statusSpan) {
            if (json.success) {
                statusSpan.style.color = "var(--accent-emerald)";
                statusSpan.innerText = `[OK 连通正常] 首包响应: ${json.latency_ms}ms · ${json.message}`;
            } else {
                statusSpan.style.color = "var(--accent-danger)";
                statusSpan.innerText = `[连通失败] ${json.message}`;
            }
        }
    } catch (e) {
        if (statusSpan) {
            statusSpan.style.color = "var(--accent-danger)";
            statusSpan.innerText = `[探测出错]: ${e}`;
        }
    } finally {
        btn.disabled = false;
    }
}

// 展开/收起添加自定义服务商
function toggleAddProviderForm() {
    toggleAddCustomTTSForm();
}

// 提交新增自定义服务商
async function submitNewProvider() {
    const group = document.getElementById("new-cfg-group").value;
    const name = document.getElementById("new-provider-name").value.trim();
    const url = document.getElementById("new-base-url").value.trim();
    const model = document.getElementById("new-model-name").value.trim();
    const key = document.getElementById("new-api-key").value.trim();

    if (!name) {
        alert("请输入服务商标识（如 my_service）");
        return;
    }

    try {
        const res = await fetch(`${API_BASE}/settings/configs/save`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                config_group: group,
                provider_name: name,
                base_url: url,
                model_name: model,
                api_key: key || null,
                is_active: true
            })
        });
        const json = await res.json();
        if (json.code === 0) {
            alert("自定义服务商已成功添加！");
            toggleAddProviderForm();
            loadSettings();
        } else {
            alert("添加失败: " + json.message);
        }
    } catch (e) {
        alert("请求异常: " + e);
    }
}

// 9. 人工紧急插播

async function sendManualSpeech(customText = null) {
    const text = customText || document.getElementById("manual-speech-input").value;
    if (!text) return;
    try {
        const res = await fetch(`${API_BASE}/live/manual-speech`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                text: text,
                interrupt_ongoing: true
            })
        });
        const json = await res.json();
        if (json.code === 0) {
            triggerBargeInVisual("人工紧急介入");
            logDanmaku("【中控运营插播】", text, true);
            if (!customText) document.getElementById("manual-speech-input").value = "";
        }
    } catch (e) {
        alert("插播失败: " + e);
    }
}

// 10. 模拟观众弹幕送礼 (真实注入后端优先级队列，驱动完整 AI 应答链路)
async function sendMockDanmaku(type) {
    try {
        const res = await fetch(`${API_BASE}/live/mock-event`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ type: type === "gift" ? "gift" : "chat" })
        });
        const json = await res.json();
        if (json.code === 0) {
            showToast(json.message, "info");
            if (type === "gift") {
                logDanmaku("仿真大哥888", "送出 超级大火箭 x 1 (P0 强打断)", true);
            } else {
                logDanmaku("仿真买家小美", "主播，这款多少钱？现在还有优惠吗？", false);
            }
        } else {
            showToast(json.detail || json.message || "注入失败", "warning");
        }
    } catch (e) {
        showToast("模拟事件请求异常: " + e, "error");
    }
}


// ============================================================================
// AI 声明微标 (规划 §14.3)：一键开启"本直播间由人工智能技术辅助生成"合规角标
// ============================================================================
(function mountAiBadge() {
    function render() {
        if (document.getElementById("ai-declaration-badge")) return;

        const badge = document.createElement("div");
        badge.id = "ai-declaration-badge";
        badge.innerText = "本直播间由人工智能技术辅助生成";
        badge.style.cssText = [
            "position: fixed", "top: 14px", "right: 18px", "z-index: 9999",
            "padding: 5px 12px", "border-radius: 999px",
            "background: rgba(15, 23, 42, 0.72)", "color: #7dd3fc",
            "font-size: 12px", "letter-spacing: 1px",
            "border: 1px solid rgba(125, 211, 252, 0.35)",
            "-webkit-backdrop-filter: blur(4px)",
            "backdrop-filter: blur(4px)", "display: none"
        ].join(";");

        const toggle = document.createElement("button");
        toggle.id = "ai-declaration-toggle";
        toggle.innerHTML = `${svg("shield", "icon-sm")} AI 声明`;
        toggle.title = "开启/关闭合规 AI 声明角标";
        toggle.style.cssText = [
            "position: fixed", "bottom: 14px", "right: 18px", "z-index: 9999",
            "padding: 6px 14px", "border-radius: 8px", "cursor: pointer",
            "background: rgba(30, 41, 59, 0.85)", "color: #94a3b8",
            "border: 1px solid rgba(148, 163, 184, 0.3)", "font-size: 12px"
        ].join(";");

        let visible = false;
        toggle.addEventListener("click", () => {
            visible = !visible;
            badge.style.display = visible ? "block" : "none";
            toggle.style.color = visible ? "#7dd3fc" : "#94a3b8";
        });

        document.body.appendChild(badge);
        document.body.appendChild(toggle);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", render);
    } else {
        render();
    }
})();


// ============================================================================
// 开播向导 (需求1+2)：第一步选直播模式 → 第二步选主播角色 → 建议与约束提示词
// ============================================================================
let wizardMode = "A";       // 默认选择 A 档
let wizardRoleType = "ecommerce";

