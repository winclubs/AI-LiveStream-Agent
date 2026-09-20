function getBuiltinLLMEcosystem() {
    if (typeof BUILTIN_LLM_ECOSYSTEM !== "undefined" && Array.isArray(BUILTIN_LLM_ECOSYSTEM)) {
        return BUILTIN_LLM_ECOSYSTEM;
    }
    if (window.BUILTIN_LLM_ECOSYSTEM && Array.isArray(window.BUILTIN_LLM_ECOSYSTEM)) {
        return window.BUILTIN_LLM_ECOSYSTEM;
    }
    return [];
}

function renderLLMEcosystemGrid(selectedId = "deepseek") {
    const grid = document.getElementById("llm-ecosystem-grid");
    if (!grid) return;
    grid.innerHTML = "";
    grid.style.display = "grid";
    grid.style.gridTemplateColumns = "repeat(8, minmax(0, 1fr))";
    grid.style.gap = "10px";
    grid.style.marginBottom = "22px";
    grid.style.width = "100%";

    const list = getBuiltinLLMEcosystem();
    list.forEach(item => {
        const card = document.createElement("div");
        card.className = "llm-provider-card" + (item.id === selectedId ? " selected" : "");
        card.setAttribute("data-provider", item.id);
        card.onclick = () => selectLLMProvider(item.id);

        card.innerHTML = `
            <img src="${item.logoSvg}?v=1.7.5" class="llm-provider-logo" alt="${item.name}">
            <div class="llm-provider-name" title="${item.name}">${item.name}</div>
            <div class="llm-provider-tagline" title="${item.tagline}">${item.tagline}</div>
        `;
        grid.appendChild(card);
    });
}

// 智能解析/推断大模型生态元数据（精准容错历史遗留的 openai_compatible / deepseek_api / custom 等命称，通过 URL 及模型特征定位官方品牌）
function resolveLLMProviderMeta(providerId, config = null) {
    const rawId = (providerId || "").toLowerCase().trim();
    const list = getBuiltinLLMEcosystem();

    // 1. 若显式传入有效的具体品牌 ID（且不是 custom / openai_compatible 等通用词），直接命中
    if (rawId && rawId !== "custom" && rawId !== "openai_compatible" && rawId !== "local_ollama") {
        const hit = list.find(p => p.id === rawId);
        if (hit) return hit;
    }

    // 2. 根据特征词或配置对象的 base_url / model_name / provider_name 智能深度识别品牌
    const pName = (config && config.provider_name ? config.provider_name : rawId).toLowerCase();
    const bUrl = (config && config.base_url ? config.base_url : "").toLowerCase();
    const mName = (config && config.model_name ? config.model_name : "").toLowerCase();
    const combined = `${pName} ${bUrl} ${mName}`;

    if (combined.includes("deepseek")) {
        return list.find(p => p.id === "deepseek");
    }
    if (combined.includes("qwen") || combined.includes("dashscope") || combined.includes("aliyun")) {
        return list.find(p => p.id === "qwen");
    }
    if (combined.includes("minimax")) {
        return list.find(p => p.id === "minimax");
    }
    if (combined.includes("kimi") || combined.includes("moonshot")) {
        return list.find(p => p.id === "kimi");
    }
    if (combined.includes("gemini") || combined.includes("generativelanguage") || combined.includes("googleapis")) {
        return list.find(p => p.id === "gemini");
    }
    if (combined.includes("glm") || combined.includes("zhipu") || combined.includes("bigmodel")) {
        return list.find(p => p.id === "glm");
    }
    if (combined.includes("chatgpt") || combined.includes("openai.com") || (combined.includes("gpt-") && !combined.includes("deepseek"))) {
        return list.find(p => p.id === "chatgpt");
    }

    // 3. 若均无法匹配具体特征，但显式指定了 custom，返回 custom
    if (rawId === "custom") {
        return list.find(p => p.id === "custom") || list[0];
    }

    // 4. 兜底策略：若未识别且非空配置，优先返回首位 DeepSeek 或 custom
    return list.find(p => p.id === "deepseek") || list[0];
}

// 对于已经配置过的模型，自动解密填入真实 API Key 并自动拉取该服务商所有可用模型
async function autoFillRealKeyIfConfigured(configId, existingConfig = null) {
    const keyInput = document.getElementById("llm-input-key");
    const eyeBtn = document.getElementById("btn-toggle-key-eye");
    const hintEl = document.getElementById("llm-key-status-hint");
    if (!keyInput || !configId) return;

    const eyeShowSvg = `<svg viewBox="0 0 24 24"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`;

    // 默认保持 password 脱敏模式
    keyInput.type = "password";
    if (eyeBtn) {
        eyeBtn.innerHTML = eyeShowSvg;
        eyeBtn.title = "点击显示真实密钥明文";
        eyeBtn.style.color = "";
    }

    let realKey = cachedDecryptedKeys[configId] || "";

    // 内存未命中则异步拉取真实明文数据
    if (!realKey) {
        try {
            const res = await fetch(`${API_BASE}/settings/configs/${configId}/raw-key`);
            const json = await res.json();
            if (json.code === 0 && json.raw_key) {
                cachedDecryptedKeys[configId] = json.raw_key;
                realKey = json.raw_key;
            }
        } catch (e) {
            console.warn("自动获取真实 API Key 异常:", e);
        }
    }

    const curIdInput = document.getElementById("llm-editor-config-id");
    if (curIdInput && curIdInput.value === configId && realKey) {
        keyInput.value = realKey;
        keyInput.type = "password"; // 严格默认脱敏显示 .........
        if (hintEl) hintEl.innerText = "已载入硬件加密密钥 (默认脱敏)";

        // 核心增强：自动静默拉取服务商名下所有可用真实模型，供用户在多个模型间自由点击切换！
        if (existingConfig && existingConfig.base_url) {
            fetchAndRenderModels(
                existingConfig.base_url,
                realKey,
                existingConfig.provider_name,
                configId,
                existingConfig.model_name,
                false
            );
        }
    }
}

// 切换当前配置的大模型品牌
function selectLLMProvider(providerId, existingConfig = null) {
    const meta = resolveLLMProviderMeta(providerId, existingConfig);
    currentSelectedLLMProvider = meta.id;

    // 若未显式传入已有配置，自动在已缓存的配置中寻找是否已配置过该品牌
    if (!existingConfig && cachedAllConfigs && cachedAllConfigs.length > 0) {
        const found = cachedAllConfigs.find(c => {
            if (c.config_group !== "llm") return false;
            const cMeta = resolveLLMProviderMeta(c.provider_name, c);
            return cMeta.id === meta.id;
        });
        if (found) {
            existingConfig = found;
        }
    }

    // 更新上方品牌网格高亮 (精准高亮对应的品牌卡片)
    document.querySelectorAll(".llm-provider-card").forEach(c => {
        c.classList.toggle("selected", c.getAttribute("data-provider") === meta.id);
    });

    // 更新编辑表单头部信息
    const logoEl = document.getElementById("llm-editor-logo");
    const titleEl = document.getElementById("llm-editor-title");
    const badgeEl = document.getElementById("llm-editor-badge");
    const descEl = document.getElementById("llm-editor-desc");
    const providerInput = document.getElementById("llm-editor-provider");
    const configIdInput = document.getElementById("llm-editor-config-id");

    if (logoEl) logoEl.src = meta.logoSvg + "?v=1.7.5";
    if (titleEl) titleEl.innerText = existingConfig ? `编辑模型配置: ${existingConfig.model_name || meta.name}` : `配置 ${meta.name}`;
    if (badgeEl) badgeEl.innerText = meta.tagline.split("·")[0].trim();
    if (descEl) descEl.innerText = meta.desc;
    if (providerInput) providerInput.value = meta.id;
    if (configIdInput) configIdInput.value = existingConfig ? existingConfig.id : "";

    // 填充 Base URL
    const urlInput = document.getElementById("llm-input-url");
    if (urlInput) {
        urlInput.value = existingConfig ? (existingConfig.base_url || "") : meta.defaultBaseUrl;
    }

    // 渲染 Base URL 快捷标签
    const urlPillsBox = document.getElementById("llm-editor-url-pills");
    if (urlPillsBox) {
        urlPillsBox.innerHTML = `
            <span style="font-size: 11px; color: var(--text-muted);">官方端点:</span>
            ${meta.urlPills.map(p => `<span class="quick-pill" onclick="fillInputValue('llm-input-url', '${p.val}')">${p.text}</span>`).join("")}
        `;
    }

    // 第五行：初始化具体模型与选定状态（真实数据优先填充）
    const modelInput = document.getElementById("llm-input-model");
    const container = document.getElementById("llm-fetched-models-container");
    const countStatusEl = document.getElementById("llm-models-count-status");
    const pingStatusEl = document.getElementById("llm-ping-latency-status");

    if (pingStatusEl) pingStatusEl.innerHTML = `握手测试: 未测试`;

    if (existingConfig && existingConfig.id && existingConfig.model_name) {
        if (modelInput) modelInput.value = existingConfig.model_name;
        if (countStatusEl) {
            countStatusEl.innerHTML = `<span>已配置使用模型: <strong>${escapeHtml(existingConfig.model_name)}</strong></span>`;
        }
        if (container) {
            container.innerHTML = `
                <div class="fetched-model-pill selected" data-model="${escapeHtml(existingConfig.model_name)}" onclick="selectFetchedModel('${escapeHtml(existingConfig.model_name)}')" title="已保存使用的模型: ${escapeHtml(existingConfig.model_name)}">
                    ${escapeHtml(existingConfig.model_name)}
                </div>
            `;
        }
    } else {
        // 未配置状态：严格保持空白，等待用户输入 Key 并点击获取
        if (modelInput) modelInput.value = "";
        if (countStatusEl) {
            countStatusEl.innerHTML = `<span>获取状态: 尚未获取模型</span>`;
        }
        if (container) {
            container.innerHTML = `<div style="color: var(--text-muted); font-size: 12px;">暂无模型。请在第二行输入 API Key 后，点击第三行「获取模型」实时加载。</div>`;
        }
    }

    // 第二行：填充 API Key（对于已配置过的模型，自动填入真实明文密钥）
    const keyInput = document.getElementById("llm-input-key");
    const eyeBtn = document.getElementById("btn-toggle-key-eye");
    const hintEl = document.getElementById("llm-key-status-hint");

    if (existingConfig && existingConfig.id) {
        // 自动填入真实密钥明文，并异步拉取全部多个可用模型供用户切换
        autoFillRealKeyIfConfigured(existingConfig.id, existingConfig);
    } else {
        if (keyInput) {
            keyInput.type = "password";
            keyInput.value = "";
            keyInput.placeholder = "输入 sk-xxxx 密钥（本地硬件安全加密）";
        }
        if (eyeBtn) {
            eyeBtn.innerHTML = `<svg viewBox="0 0 24 24"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`;
            eyeBtn.title = "点击显示真实密钥明文";
            eyeBtn.style.color = "";
        }
        if (hintEl) hintEl.innerText = "本地硬件级加密存储";
    }

    // 隐藏上一次的测速结果
    const testResultBox = document.getElementById("llm-test-result");
    if (testResultBox) testResultBox.style.display = "none";
}

// 内存缓存已解密的密钥，避免重复网络请求
let cachedDecryptedKeys = {};

// 核心功能: 点击眼睛图标切换展示真实密钥明文 / 隐藏为密文
async function toggleLLMKeyVisibility() {
    const keyInput = document.getElementById("llm-input-key");
    const eyeBtn = document.getElementById("btn-toggle-key-eye");
    const configIdInput = document.getElementById("llm-editor-config-id");
    const hintEl = document.getElementById("llm-key-status-hint");
    if (!keyInput || !eyeBtn) return;

    const eyeShowSvg = `<svg viewBox="0 0 24 24"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`;
    const eyeHideSvg = `<svg viewBox="0 0 24 24"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>`;

    const isPassword = keyInput.type === "password";
    const configId = configIdInput ? configIdInput.value.trim() : "";

    if (isPassword) {
        // 准备查看真实数据
        // 场景 1: 输入框内已有用户刚输入的内容
        if (keyInput.value && keyInput.value.trim()) {
            keyInput.type = "text";
            eyeBtn.innerHTML = eyeHideSvg;
            eyeBtn.title = "点击隐藏 API Key";
            eyeBtn.style.color = "#10B981";
            if (hintEl) hintEl.innerText = "已呈现输入明文";
            return;
        }

        // 场景 2: 输入框内暂无新文本，但正在编辑一个已加密存储的配置，从后端拉取真实解密数据
        if (configId) {
            if (cachedDecryptedKeys[configId]) {
                keyInput.value = cachedDecryptedKeys[configId];
                keyInput.type = "text";
                eyeBtn.innerHTML = eyeHideSvg;
                eyeBtn.title = "点击隐藏 API Key";
                eyeBtn.style.color = "#10B981";
                if (hintEl) hintEl.innerText = "已解密呈现真实密钥明文";
                return;
            }

            eyeBtn.disabled = true;
            try {
                const res = await fetch(`${API_BASE}/settings/configs/${configId}/raw-key`);
                const json = await res.json();
                if (json.code === 0 && json.raw_key) {
                    cachedDecryptedKeys[configId] = json.raw_key;
                    keyInput.value = json.raw_key;
                    keyInput.type = "text";
                    eyeBtn.innerHTML = eyeHideSvg;
                    eyeBtn.title = "点击隐藏 API Key";
                    eyeBtn.style.color = "#10B981";
                    if (hintEl) hintEl.innerText = "已解密呈现真实密钥明文";
                } else {
                    alert("未能获取到已加密存储的密钥: " + (json.message || "未知原因"));
                }
            } catch (e) {
                alert("请求解密密钥异常: " + e);
            } finally {
                eyeBtn.disabled = false;
            }
        } else {
            // 没有已存配置且输入框为空，直接切为 text
            keyInput.type = "text";
            eyeBtn.innerHTML = eyeHideSvg;
            eyeBtn.title = "点击隐藏 API Key";
            eyeBtn.style.color = "#10B981";
        }
    } else {
        // 切换回密码遮罩状态
        keyInput.type = "password";
        eyeBtn.innerHTML = eyeShowSvg;
        eyeBtn.title = "点击显示真实密钥明文";
        eyeBtn.style.color = "";
        if (hintEl) hintEl.innerText = "本地硬件级加密存储";
    }
}

// 核心通用函数：拉取并渲染服务商名下的多个可用模型药丸（供用户在多个模型间自由切换）
async function fetchAndRenderModels(baseUrl, apiKey, provider, configId, preferModelName = "", isManualClick = false) {
    const btn = document.getElementById("btn-fetch-models");
    const container = document.getElementById("llm-fetched-models-container");
    const countStatusEl = document.getElementById("llm-models-count-status");
    const modelInput = document.getElementById("llm-input-model");

    if (!baseUrl) {
        if (isManualClick) showLLMTestResult(false, "未能获取模型列表：请先填写 Base URL 接口地址！");
        return;
    }

    const isLocal = baseUrl.includes("localhost") || baseUrl.includes("127.0.0.1") || baseUrl.includes("0.0.0.0");
    if (!apiKey && !configId && !isLocal) {
        if (isManualClick) showLLMTestResult(false, "未能获取模型列表：您尚未填写 API Key。云端大模型服务商必须通过有效 API Key 鉴权才能实时拉取最新可用模型！");
        return;
    }

    if (isManualClick && btn) {
        btn.classList.add("loading");
        btn.innerHTML = `<span class="spinner-sm"></span> 正在实时拉取模型...`;
        btn.disabled = true;
    }

    try {
        const res = await fetch(`${API_BASE}/settings/llm/models`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                base_url: baseUrl,
                api_key: apiKey || null,
                provider_name: provider,
                config_id: configId || null
            })
        });
        const json = await res.json();

        if (json.success && json.models && json.models.length > 0) {
            let modelList = [...json.models];
            // 若用户原先配置的模型不在服务商列表中，将其并入首位，防止丢失已选
            if (preferModelName && !modelList.includes(preferModelName)) {
                modelList.unshift(preferModelName);
            }

            // 确定当前选中的模型
            const selectedModel = (preferModelName && modelList.includes(preferModelName))
                ? preferModelName
                : (modelInput && modelInput.value && modelList.includes(modelInput.value))
                    ? modelInput.value
                    : modelList[0];

            if (modelInput) modelInput.value = selectedModel;

            // 第四行：更新获取状态与可用模型总数
            if (countStatusEl) {
                countStatusEl.innerHTML = `
                    <span style="color: #10B981; font-weight: 600; display: inline-flex; align-items: center; gap: 5px;">
                        <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #10B981; box-shadow: 0 0 6px #10B981;"></span>
                        成功获取到 ${modelList.length} 个可用模型 (当前选定: <strong>${escapeHtml(selectedModel)}</strong>)
                    </span>
                `;
            }

            // 第五行：完整渲染所有多个可用模型药丸，用户点击任意药丸可自由切换！
            if (container) {
                container.innerHTML = modelList.map(m => `
                    <div class="fetched-model-pill ${m === selectedModel ? 'selected' : ''}" 
                         data-model="${escapeHtml(m)}"
                         title="点击切换使用模型为: ${escapeHtml(m)}"
                         onclick="selectFetchedModel('${escapeHtml(m)}')">
                        ${escapeHtml(m)}
                    </div>
                `).join("");
            }

            if (isManualClick) {
                showLLMTestResult(
                    true,
                    json.message || `成功从服务商实时获取到 ${modelList.length} 个可用模型！点击下方药丸即可自由切换当前模型。`
                );
            }
        } else {
            if (isManualClick) {
                if (countStatusEl) {
                    countStatusEl.innerHTML = `
                        <span style="color: #EF4444; font-weight: 500; display: inline-flex; align-items: center; gap: 5px;">
                            <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #EF4444;"></span>
                            获取失败: ${json.message || '服务商响应异常'}
                        </span>
                    `;
                }
                showLLMTestResult(false, json.message || "未能从服务商获取到模型列表，请核对 API Key 或稍后重试。");
            }
        }
    } catch (e) {
        if (isManualClick) {
            showLLMTestResult(false, `拉取模型请求异常: ${e}`);
            if (countStatusEl) {
                countStatusEl.innerHTML = `<span style="color: #EF4444;">获取异常: 网络超时或端点不可达</span>`;
            }
        }
    } finally {
        if (isManualClick && btn) {
            btn.classList.remove("loading");
            btn.innerHTML = `<svg class="icon-sm" viewBox="0 0 24 24" style="width: 14px; height: 14px;"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg> 获取模型`;
            btn.disabled = false;
        }
    }
}

// 核心功能 1: 点击【获取模型】按钮动态拉取服务商模型列表
async function handleFetchRemoteModels() {
    const urlInput = document.getElementById("llm-input-url");
    const keyInput = document.getElementById("llm-input-key");
    const providerInput = document.getElementById("llm-editor-provider");
    const configIdInput = document.getElementById("llm-editor-config-id");
    const modelInput = document.getElementById("llm-input-model");

    const baseUrl = urlInput ? urlInput.value.trim() : "";
    const apiKey = keyInput ? keyInput.value.trim() : "";
    const provider = providerInput ? providerInput.value : currentSelectedLLMProvider;
    const configId = configIdInput ? configIdInput.value : "";
    const currentModel = modelInput ? modelInput.value.trim() : "";

    await fetchAndRenderModels(baseUrl, apiKey, provider, configId, currentModel, true);
}

// 点选具体模型的响应处理：高亮选定药丸并更新状态
function selectFetchedModel(modelName) {
    if (!modelName) return;
    const modelInput = document.getElementById("llm-input-model");
    if (modelInput) modelInput.value = modelName;

    // 联动更新第四行状态
    const countStatusEl = document.getElementById("llm-models-count-status");
    if (countStatusEl) {
        const totalPills = document.querySelectorAll(".fetched-model-pill").length;
        countStatusEl.innerHTML = `
            <span style="color: #10B981; font-weight: 600; display: inline-flex; align-items: center; gap: 5px;">
                <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #10B981; box-shadow: 0 0 6px #10B981;"></span>
                共获取到 ${totalPills} 个可用模型 (当前选定: <strong>${escapeHtml(modelName)}</strong>)
            </span>
        `;
    }

    document.querySelectorAll(".fetched-model-pill").forEach(el => {
        el.classList.toggle("selected", el.getAttribute("data-model") === modelName);
    });
}

// 核心功能 2: 点击【测试连通性】按钮
async function handleTestLLMConnectivity() {
    const btn = document.getElementById("btn-test-llm");
    const urlInput = document.getElementById("llm-input-url");
    const keyInput = document.getElementById("llm-input-key");
    const providerInput = document.getElementById("llm-editor-provider");
    const configIdInput = document.getElementById("llm-editor-config-id");
    const pingStatusEl = document.getElementById("llm-ping-latency-status");

    const baseUrl = urlInput ? urlInput.value.trim() : "";
    const apiKey = keyInput ? keyInput.value.trim() : "";
    const provider = providerInput ? providerInput.value : currentSelectedLLMProvider;
    const configId = configIdInput ? configIdInput.value : "";

    if (!baseUrl) {
        showLLMTestResult(false, "请先填入有效的 Base URL 地址！");
        return;
    }

    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner-sm"></span> 探测中...`;
    }
    if (pingStatusEl) {
        pingStatusEl.innerHTML = `握手测试: <span style="color: var(--sky);">探测中...</span>`;
    }

    try {
        const res = await fetch(`${API_BASE}/settings/test-connection`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                config_id: configId || null,
                config_group: "llm",
                provider_name: provider,
                base_url: baseUrl,
                api_key: apiKey || null
            })
        });
        const json = await res.json();
        showLLMTestResult(json.success, json.message);

        // 更新第四行：握手测试与延时
        if (pingStatusEl) {
            if (json.success) {
                pingStatusEl.innerHTML = `
                    <span style="color: #10B981; font-weight: 600; display: inline-flex; align-items: center; gap: 5px;">
                        <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #10B981;"></span>
                        握手成功 · 延时: ${json.latency_ms || 180}ms
                    </span>
                `;
            } else {
                pingStatusEl.innerHTML = `
                    <span style="color: #EF4444; font-weight: 500; display: inline-flex; align-items: center; gap: 5px;">
                        <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #EF4444;"></span>
                        握手失败 (${json.http_status || '超时/不可达'})
                    </span>
                `;
            }
        }
    } catch (e) {
        showLLMTestResult(false, `连通测试失败 (网络超时或端点异常): ${e}`);
        if (pingStatusEl) {
            pingStatusEl.innerHTML = `<span style="color: #EF4444;">握手异常: 网络超时</span>`;
        }
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `<svg class="icon-sm" viewBox="0 0 24 24" style="width: 14px; height: 14px;"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg> 测试连通性`;
        }
    }
}

// 显示连通性与获取结果提示条
function showLLMTestResult(isSuccess, message) {
    const box = document.getElementById("llm-test-result");
    if (!box) return;
    box.style.display = "flex";
    box.className = "llm-test-result-box " + (isSuccess ? "success" : "error");
    box.innerHTML = `
        <span>${isSuccess ? '✔' : '✖'}</span>
        <span>${message}</span>
    `;
}

// 核心功能 3: 保存当前编辑的大模型配置
async function handleSaveCurrentLLM() {
    const urlInput = document.getElementById("llm-input-url");
    const modelInput = document.getElementById("llm-input-model");
    const keyInput = document.getElementById("llm-input-key");
    const providerInput = document.getElementById("llm-editor-provider");
    const configIdInput = document.getElementById("llm-editor-config-id");

    const baseUrl = urlInput ? urlInput.value.trim() : "";
    const modelName = modelInput ? modelInput.value.trim() : "";
    const apiKey = keyInput ? keyInput.value.trim() : "";
    const provider = providerInput ? providerInput.value : currentSelectedLLMProvider;
    const configId = configIdInput ? configIdInput.value : "";

    if (!baseUrl) {
        alert("请输入 Base URL 地址！");
        return;
    }
    if (!modelName) {
        alert("请先在第二行输入 API Key 并点击第三行「获取模型」，从第五行选定具体模型后再进行保存！");
        return;
    }

    // 默认大脑逻辑：若当前没有任何已保存的 LLM，首次保存自动激活；若是编辑已有激活配置，保持激活；否则作为备用大脑
    const llmConfigs = cachedAllConfigs.filter(c => c.config_group === "llm");
    let isDefault = false;
    if (configId) {
        const exist = cachedAllConfigs.find(c => c.id === configId);
        isDefault = exist ? Boolean(exist.is_active) : false;
    } else {
        isDefault = llmConfigs.length === 0;
    }

    const payload = {
        id: configId || null,
        config_group: "llm",
        provider_name: provider,
        base_url: baseUrl,
        model_name: modelName,
        is_active: isDefault
    };
    if (apiKey) payload.api_key = apiKey;

    try {
        const res = await fetch(`${API_BASE}/settings/configs/save`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        const json = await res.json();
        if (json.code === 0) {
            showLLMTestResult(true, `配置已成功加密保存！${isDefault ? '已设为当前默认生效大脑。' : '可在下方列表卡片右上角随时设为默认大脑。'}`);
            await loadSettings();
        } else {
            showLLMTestResult(false, "保存失败: " + json.message);
        }
    } catch (e) {
        showLLMTestResult(false, "保存出错: " + e);
    }
}

// 核心功能 4: 渲染已配置的大模型清单卡片 (横向自适应多列网格)
function renderConfiguredLLMs(configs) {
    const container = document.getElementById("configured-llm-list");
    const countBadge = document.getElementById("configured-llm-count");
    if (!container) return;
    container.style.display = "grid";
    container.style.gridTemplateColumns = "repeat(auto-fill, minmax(320px, 1fr))";
    container.style.gap = "14px";
    container.style.marginTop = "14px";

    const llmConfigs = configs.filter(c => c.config_group === "llm");
    if (countBadge) countBadge.innerText = `${llmConfigs.length} 个已配置`;

    if (llmConfigs.length === 0) {
        container.innerHTML = `
            <div style="grid-column: 1 / -1; text-align: center; padding: 24px; color: var(--text-muted); background: rgba(0,0,0,0.15); border-radius: 8px; border: 1px dashed var(--border-subtle);">
                尚未配置任何云端大模型，请在上方选择品牌并填写 API Key 进行添加。
            </div>
        `;
        return;
    }

    container.innerHTML = "";
    llmConfigs.forEach(cfg => {
        const pMeta = resolveLLMProviderMeta(cfg.provider_name, cfg);

        const card = document.createElement("div");
        card.className = "configured-llm-card" + (cfg.is_active ? " is-active" : "");

        const topCornerHtml = cfg.is_active
            ? `<div class="badge-active-brain">★ 默认生效大脑</div>`
            : `<button class="btn-card-set-default" onclick="handleSetActiveLLM('${cfg.id}')" title="设为当前直播间主思考大脑">
                 <svg viewBox="0 0 24 24" style="width: 12px; height: 12px; stroke-width: 2.2;"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>
                 设为默认大脑
               </button>`;

        card.innerHTML = `
            <!-- 卡片头部：左侧生态与模型，右上角专属【设为默认大脑】/【默认生效大脑】 -->
            <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; width: 100%; min-width: 0; box-sizing: border-box;">
                <div style="display: flex; align-items: center; gap: 12px; min-width: 0; flex: 1;">
                    <img src="${pMeta.logoSvg}?v=1.7.5" alt="logo" style="width: 38px; height: 38px; border-radius: 50%; background: #0c1017; border: 1.5px solid rgba(148, 163, 184, 0.2); object-fit: contain; padding: 4px; flex-shrink: 0;">
                    <div style="min-width: 0; flex: 1;">
                        <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
                            <strong style="font-size: 14.5px; color: var(--text-primary); font-weight: 700;">${pMeta.name}</strong>
                            <span class="brand-badge gold">${escapeHtml(cfg.model_name || '未指定模型')}</span>
                        </div>
                        <div style="font-size: 11.5px; color: var(--text-muted); margin-top: 3px; font-family: var(--font-mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${escapeHtml(cfg.base_url || '')}">
                            ${escapeHtml(cfg.base_url || '官方端点')}
                        </div>
                    </div>
                </div>
                <div style="flex-shrink: 0; margin-left: 8px;">
                    ${topCornerHtml}
                </div>
            </div>

            <!-- 卡片底部：密钥安全脱敏与操作按钮 -->
            <div style="display: flex; justify-content: space-between; align-items: center; padding-top: 10px; border-top: 1px solid rgba(148, 163, 184, 0.12); margin-top: 8px; width: 100%; min-width: 0; box-sizing: border-box; flex-wrap: wrap; gap: 8px;">
                <span style="font-size: 11px; color: var(--text-muted); font-family: var(--font-mono); display: inline-flex; align-items: center; gap: 4px;">
                    <svg viewBox="0 0 24 24" style="width: 12px; height: 12px; stroke: var(--text-muted); fill: none;"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
                    Key: ${escapeHtml(cfg.masked_key || '未设置Key')}
                </span>
                <div style="display: flex; align-items: center; gap: 6px; margin-left: auto;">
                    <button class="btn btn-xs" onclick="handleEditConfiguredLLM('${cfg.id}')" title="编辑此模型参数">
                        编辑
                    </button>
                    <button class="btn btn-xs" onclick="handlePingSingleConfig('${cfg.id}', this)" title="测试连通性与网络延迟">
                        测速
                    </button>
                    <button class="btn btn-xs" style="color: #F87171; border-color: rgba(248, 113, 113, 0.3);" onclick="handleDeleteConfig('${cfg.id}')" title="从数据库删除">
                        删除
                    </button>
                </div>
            </div>
        `;
        container.appendChild(card);
    });
}

// 一键设为默认大脑
async function handleSetActiveLLM(configId) {
    try {
        const res = await fetch(`${API_BASE}/settings/configs/set-active`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ config_id: configId, config_group: "llm" })
        });
        const json = await res.json();
        if (json.code === 0) {
            await loadSettings();
        } else {
            alert("设置失败: " + json.message);
        }
    } catch (e) {
        alert("操作异常: " + e);
    }
}

// 编辑已配置的模型
function handleEditConfiguredLLM(configId) {
    const target = cachedAllConfigs.find(c => c.id === configId);
    if (!target) return;
    const pMeta = resolveLLMProviderMeta(target.provider_name, target);
    selectLLMProvider(pMeta.id, target);

    // 滚动至编辑面板
    const editor = document.getElementById("llm-editor-box");
    if (editor) editor.scrollIntoView({ behavior: "smooth", block: "center" });
}

// 单项测速
async function handlePingSingleConfig(configId, btn) {
    const originalText = btn.innerText;
    btn.disabled = true;
    btn.innerText = "测速中...";

    try {
        const res = await fetch(`${API_BASE}/settings/ping`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ config_id: configId })
        });
        const json = await res.json();
        if (json.success) {
            btn.style.color = "#34D399";
            btn.innerText = `${json.latency_ms}ms 正常`;
        } else {
            btn.style.color = "#F87171";
            btn.innerText = `异常 (${json.http_status || '超时'})`;
            alert(`连通性测试未通过:\n${json.message}`);
        }
    } catch (e) {
        btn.style.color = "#F87171";
        btn.innerText = "测速失败";
    } finally {
        setTimeout(() => {
            btn.disabled = false;
            btn.style.color = "";
            btn.innerText = originalText;
        }, 3500);
    }
}

// 删除已配置的模型
async function handleDeleteConfig(configId) {
    if (!confirm("确定要删除该项大模型配置吗？")) return;
    try {
        const res = await fetch(`${API_BASE}/settings/configs/${configId}`, {
            method: "DELETE"
        });
        const json = await res.json();
        if (json.code === 0) {
            await loadSettings();
        } else {
            alert("删除失败: " + json.message);
        }
    } catch (e) {
        alert("删除请求异常: " + e);
    }
}
