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

            // 1. 渲染大模型网格与多模型卡片列表
            renderLLMEcosystemGrid(currentSelectedLLMProvider);
            renderConfiguredLLMs(cachedAllConfigs);

            // 若大模型编辑区尚未与激活项绑定过，优先把激活的大模型同步到编辑面板
            const activeLLM = cachedAllConfigs.find(c => c.config_group === "llm" && c.is_active);
            if (activeLLM && !document.getElementById("llm-editor-config-id").value) {
                const pMeta = resolveLLMProviderMeta(activeLLM.provider_name, activeLLM);
                selectLLMProvider(pMeta.id, activeLLM);
            } else if (!document.getElementById("llm-editor-config-id").value) {
                selectLLMProvider(currentSelectedLLMProvider);
            }

            // 2. 渲染语音合成生态网格与已配置发音清单
            const activeTTS = cachedAllConfigs.find(c => c.config_group === "tts" && c.is_active);
            if (activeTTS) {
                const tMeta = resolveTTSProviderMeta(activeTTS.provider_name, activeTTS);
                currentSelectedTTSProvider = tMeta.id;
                renderTTSEcosystemGrid(tMeta.id);
                renderConfiguredTTS(cachedAllConfigs);
                await selectTTSProvider(tMeta.id, activeTTS);
            } else {
                renderTTSEcosystemGrid(currentSelectedTTSProvider);
                renderConfiguredTTS(cachedAllConfigs);
                if (!document.getElementById("tts-editor-config-id").value) {
                    await selectTTSProvider(currentSelectedTTSProvider);
                }
            }

            // 3. 渲染其他高级/扩展服务商卡片（如远程 GPU 渲染节点等）
            const container = document.getElementById("settings-configs-container");
            if (!container) return;
            container.innerHTML = "";

            const otherConfigs = cachedAllConfigs.filter(c => c.config_group !== "llm" && c.config_group !== "tts");
            if (otherConfigs.length === 0) {
                container.style.display = "none";
                return;
            }
            container.style.display = "block";
            if (otherConfigs.length === 0) {
                container.innerHTML = `
                    <div style="text-align: center; padding: 28px; color: var(--text-muted); background: rgba(0,0,0,0.18); border-radius: var(--radius-md); border: 1px dashed rgba(148, 163, 184, 0.2);">
                        暂未配置额外的语音合成或远程 GPU 算力服务商，系统将默认采用原生轻量服务。点击右上角「添加自定义服务商」可接入第三方服务。
                    </div>
                `;
                return;
            }

            otherConfigs.forEach(cfg => {
                const meta = PROVIDER_META[cfg.provider_name] || {
                    title: `${cfg.provider_name} 自定义服务`,
                    typeBadge: cfg.config_group.toUpperCase(),
                    requiredTag: '<span class="badge-optional">自定义</span>',
                    recommendTag: '',
                    desc: "用户自定义接入的第三方 API 服务商。",
                    urlLabel: "接口 Base URL 地址:",
                    urlTip: "服务商 API 的请求根地址。",
                    urlPills: [],
                    modelLabel: "模型 / 音色代号 (Model ID / Voice ID):",
                    modelTip: "调用的具体模型或音色代号。",
                    modelPills: [],
                    keyLabel: "API 密钥 (API Key):",
                    keyTip: "访问该服务所需的认证密钥。"
                };

                const div = document.createElement("div");
                div.className = "settings-sub-card";
                div.innerHTML = `
                    <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 12px; border-bottom: 1px solid var(--border-subtle); padding-bottom: 10px;">
                        <div>
                            <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
                                <strong style="font-size: 15px; color: var(--text-primary);">${meta.title}</strong>
                                <span class="brand-badge">${meta.typeBadge}</span>
                                ${meta.requiredTag}
                                ${meta.recommendTag}
                            </div>
                            <p style="font-size: 12px; color: var(--text-secondary); margin-top: 6px; line-height: 1.5;">${meta.desc}</p>
                        </div>
                        <label style="display: flex; align-items: center; gap: 6px; font-size: 13px; cursor: pointer; white-space: nowrap; margin-left: 12px;">
                            <input type="checkbox" id="active-${cfg.id}" ${cfg.is_active ? 'checked' : ''}>
                            <span style="font-weight: 600; color: ${cfg.is_active ? 'var(--accent-emerald)' : 'var(--text-muted)'};">${cfg.is_active ? '● 当前正在使用' : '○ 未启用'}</span>
                        </label>
                    </div>

                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 14px;">
                        <div>
                            <label class="form-label">${meta.urlLabel}</label>
                            <input type="text" id="url-${cfg.id}" class="form-control" value="${cfg.base_url || ''}" placeholder="例如 http://127.0.0.1:9233">
                            <div class="field-tip">${meta.urlTip}</div>
                        </div>
                        <div>
                            <label class="form-label">${meta.modelLabel}</label>
                            <input type="text" id="model-${cfg.id}" class="form-control" value="${cfg.model_name || ''}" placeholder="例如 CosyVoice2-0.5B">
                            <div class="field-tip">${meta.modelTip}</div>
                        </div>
                    </div>

                    <div style="margin-bottom: 16px;">
                        <label class="form-label">${meta.keyLabel}</label>
                        <input type="password" id="key-${cfg.id}" class="form-control" placeholder="${cfg.masked_key ? '已加密存储: ' + cfg.masked_key + ' (若不修改请留空)' : '输入 API Key 密钥'}">
                    </div>

                    <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; background: rgba(0,0,0,0.2); padding: 10px 14px; border-radius: 8px;">
                        <div style="display: flex; gap: 10px;">
                            <button class="btn btn-sm btn-primary" onclick="saveSettingConfig('${cfg.config_group}', '${cfg.provider_name}', '${cfg.id}', this)">保存此项配置修改</button>
                            <button class="btn btn-sm" onclick="pingSettingConfig('${cfg.config_group}', '${cfg.provider_name}', '${cfg.id}', this)">连通性测试 · 探测延迟</button>
                        </div>
                        <span class="ping-status" id="status-${cfg.id}" style="font-size: 12px; font-weight: 500;"></span>
                    </div>
                `;
                container.appendChild(div);
            });
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
        alert("请输入服务商标识（如 custom_tts）");
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

