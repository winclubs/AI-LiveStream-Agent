let currentVoiceTableEngineFilter = "";

// 按引擎筛选音色资产库
function filterVoiceTableByEngine(engine, btn) {
    currentVoiceTableEngineFilter = (engine || "").trim().toLowerCase();
    document.querySelectorAll("#voice-engine-filters .voice-filter-btn").forEach(b => {
        b.classList.toggle("active", b === btn || (b.getAttribute("data-engine") || "") === currentVoiceTableEngineFilter);
    });
    renderVoiceTableRows();
}

// 格式化所属语音引擎徽标
function formatVoiceEngineBadge(providerName) {
    const p = (providerName || "").toLowerCase();
    if (p.includes("edge")) {
        return '<span class="brand-badge" style="background: rgba(2, 132, 199, 0.15); border: 1px solid rgba(2, 132, 199, 0.4); color: #38bdf8; font-size: 11px; padding: 2px 7px;">Edge-TTS</span>';
    } else if (p.includes("cosy")) {
        return '<span class="brand-badge" style="background: rgba(234, 88, 12, 0.15); border: 1px solid rgba(234, 88, 12, 0.4); color: #fb923c; font-size: 11px; padding: 2px 7px;">CosyVoice</span>';
    } else if (p.includes("sovits") || p.includes("gpt")) {
        return '<span class="brand-badge" style="background: rgba(14, 165, 233, 0.15); border: 1px solid rgba(14, 165, 233, 0.4); color: #7dd3fc; font-size: 11px; padding: 2px 7px;">GPT-SoVITS</span>';
    } else if (p.includes("eleven")) {
        return '<span class="brand-badge" style="background: rgba(244, 63, 94, 0.15); border: 1px solid rgba(244, 63, 94, 0.4); color: #fb7185; font-size: 11px; padding: 2px 7px;">ElevenLabs</span>';
    } else if (p.includes("custom") || p.includes("local") || p.includes("自建")) {
        return '<span class="brand-badge" style="background: rgba(168, 85, 247, 0.15); border: 1px solid rgba(168, 85, 247, 0.4); color: #c084fc; font-size: 11px; padding: 2px 7px;">本地自建</span>';
    } else if (p.includes("chat")) {
        return '<span class="brand-badge" style="background: rgba(5, 150, 105, 0.15); border: 1px solid rgba(5, 150, 105, 0.4); color: #34d399; font-size: 11px; padding: 2px 7px;">ChatTTS</span>';
    } else if (p) {
        return `<span class="brand-badge" style="background: rgba(148, 163, 184, 0.12); border: 1px solid rgba(148, 163, 184, 0.3); color: #cbd5e1; font-size: 11px; padding: 2px 7px;">${escapeHtml(providerName)}</span>`;
    }
    return '<span style="color: var(--text-muted); font-size: 11px;">通用引擎</span>';
}

// 渲染音色表格行
function renderVoiceTableRows() {
    const tbody = document.getElementById("voices-tbody");
    const countBadge = document.getElementById("voice-asset-count-badge");
    if (!tbody) return;

    let list = voiceCache || [];
    if (currentVoiceTableEngineFilter) {
        list = list.filter(v => {
            const vp = (v.provider_name || "").toLowerCase();
            const vid = (v.id || "").toLowerCase();
            if (currentVoiceTableEngineFilter === "edge_tts") {
                return vp.includes("edge") || vid.startsWith("zh-");
            } else if (currentVoiceTableEngineFilter === "cosyvoice") {
                return vp.includes("cosy") || vid.includes("bailian") || vid.includes("cosyvoice") || vid.startsWith("long");
            } else if (currentVoiceTableEngineFilter === "gpt_sovits") {
                return vp.includes("sovits") || vp.includes("gpt");
            } else if (currentVoiceTableEngineFilter === "elevenlabs") {
                return vp.includes("eleven");
            } else if (currentVoiceTableEngineFilter === "custom_tts") {
                return vp.includes("custom") || vp.includes("local") || vp.includes("自建");
            } else if (currentVoiceTableEngineFilter === "chattts") {
                return vp.includes("chat");
            }
            return vp === currentVoiceTableEngineFilter;
        });
    }

    if (countBadge) {
        countBadge.innerText = `${list.length} 款音色`;
    }

    tbody.innerHTML = "";
    if (list.length === 0) {
        tbody.innerHTML = `<tr><td colspan="5" style="text-align: center; color: var(--text-muted); padding: 24px;">暂无可显示的音色资产。请在「TTS配置」页面选定语音引擎并保存，即可自动批量同步入库！</td></tr>`;
        return;
    }

    list.forEach(v => {
        const isCloned = v.voice_type === "cloned" || v.is_clone || (v.id && (v.id.includes("bailian") || v.id.startsWith("clone_") || v.id.includes("cosyvoice-v")));
        const typeBadge = isCloned
            ? '<span style="color: #fbbf24; font-weight: 600; font-size: 11.5px; display: inline-flex; align-items: center; gap: 3px;"><span>👑</span> 专属克隆</span>'
            : '<span style="color: #94a3b8; font-size: 11.5px;">官方预设</span>';

        const tr = document.createElement("tr");
        tr.innerHTML = `
            <td style="font-weight: 600; color: #FFFFFF;">
                <span title="Voice-ID: ${escapeHtml(v.id)}">${escapeHtml(v.name)}</span>
            </td>
            <td>${formatVoiceEngineBadge(v.provider_name)}</td>
            <td>${typeBadge}</td>
            <td style="font-family: var(--font-mono);">${(v.speech_speed || 1.0).toFixed(2)}x</td>
            <td style="white-space: nowrap; text-align: right;">
                <div class="table-actions" style="justify-content: flex-end;">
                    <button class="btn btn-xs" onclick="previewVoice('${v.id}')" title="在线合成试听此音色">
                        ${typeof svg === "function" ? svg("play", "icon-sm") : "▶"} 试听
                    </button>
                    <button class="btn btn-xs" onclick="editVoice('${v.id}')" title="修改音色名称">
                        改名
                    </button>
                    <button class="btn btn-xs btn-danger" onclick="deleteVoice('${v.id}', '${escapeHtml(v.name)}')" title="从音色资产库移除">
                        删除
                    </button>
                </div>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

// 主播管理页音色下拉框按当前生效的语音合成引擎精准填充
async function populateAnchorVoiceSelect(voices) {
    const anchorVoiceSel = document.getElementById("anchor-voice");
    if (!anchorVoiceSel) return;

    // 1. 确保全局 API 配置已就绪，实时获取当前默认生效的 TTS 语音引擎
    if (typeof cachedAllConfigs === "undefined" || !Array.isArray(cachedAllConfigs) || cachedAllConfigs.length === 0) {
        try {
            const cfgRes = await fetch(`${API_BASE}/settings/configs`);
            const cfgJson = await cfgRes.json();
            if (cfgJson.code === 0 && Array.isArray(cfgJson.data)) {
                cachedAllConfigs = cfgJson.data;
            }
        } catch (errCfg) {
            console.warn("未能实时获取 TTS 生效配置:", errCfg);
        }
    }

    let activeEngine = "";
    let activeEngineLabel = "";

    if (typeof cachedAllConfigs !== "undefined" && Array.isArray(cachedAllConfigs)) {
        const activeTTSCfg = cachedAllConfigs.find(c => c.config_group === "tts" && c.is_active);
        if (activeTTSCfg) {
            const pMeta = (typeof resolveTTSProviderMeta === "function") ? resolveTTSProviderMeta(activeTTSCfg.provider_name, activeTTSCfg) : null;
            activeEngine = pMeta ? pMeta.id : (activeTTSCfg.provider_name || "").toLowerCase();
            activeEngineLabel = pMeta ? pMeta.name : (activeEngine.includes("cosy") ? "CosyVoice" : activeTTSCfg.provider_name);
        }
    }

    // 若无明确激活项，取配置列表中的首个 TTS 项，最后才以 Edge-TTS 兜底
    if (!activeEngine) {
        const anyTTS = (typeof cachedAllConfigs !== "undefined" && Array.isArray(cachedAllConfigs))
            ? cachedAllConfigs.find(c => c.config_group === "tts")
            : null;
        if (anyTTS) {
            const pMeta = (typeof resolveTTSProviderMeta === "function") ? resolveTTSProviderMeta(anyTTS.provider_name, anyTTS) : null;
            activeEngine = pMeta ? pMeta.id : (anyTTS.provider_name || "").toLowerCase();
            activeEngineLabel = pMeta ? pMeta.name : (activeEngine.includes("cosy") ? "CosyVoice" : anyTTS.provider_name);
        } else {
            activeEngine = "edge_tts";
            activeEngineLabel = "Edge-TTS";
        }
    }

    // 2. 根据当前语音引擎过滤音色
    const engineVoices = (voices || []).filter(v => {
        const vp = (v.provider_name || "").toLowerCase();
        if (activeEngine.includes("cosy")) {
            return vp.includes("cosy") || (v.id && (v.id.includes("bailian") || v.id.includes("cosyvoice")));
        } else if (activeEngine.includes("edge")) {
            return vp.includes("edge") || (v.id && v.id.startsWith("zh-"));
        } else if (activeEngine.includes("chattts")) {
            return vp.includes("chattts") || (v.id && v.id.startsWith("seed_"));
        } else if (activeEngine.includes("sovits")) {
            return vp.includes("sovits");
        } else if (activeEngine.includes("eleven")) {
            return vp.includes("eleven");
        } else if (vp) {
            return vp === activeEngine;
        }
        return true;
    });

    const curSelected = anchorVoiceSel.value;
    anchorVoiceSel.innerHTML = `<option value="">未绑定音色 (开播采用默认发音)</option>`;

    // 若当前引擎下有音色，以分组方式填充
    const listToRender = engineVoices.length > 0 ? engineVoices : (voices || []);
    const clones = listToRender.filter(v => v.voice_type === "cloned" || v.is_clone);
    const presets = listToRender.filter(v => !(v.voice_type === "cloned" || v.is_clone));

    if (clones.length > 0) {
        const grpClone = document.createElement("optgroup");
        grpClone.label = "👑 专属声音克隆资产";
        clones.forEach(v => {
            const opt = document.createElement("option");
            opt.value = v.id;
            opt.innerText = `👑 ${v.name} (专属克隆)`;
            grpClone.appendChild(opt);
        });
        anchorVoiceSel.appendChild(grpClone);
    }

    if (presets.length > 0) {
        const grpPreset = document.createElement("optgroup");
        grpPreset.label = `🎙️ ${activeEngineLabel} 官方预设音色`;
        presets.forEach(v => {
            const opt = document.createElement("option");
            opt.value = v.id;
            opt.innerText = `🎙️ ${v.name}`;
            grpPreset.appendChild(opt);
        });
        anchorVoiceSel.appendChild(grpPreset);
    }

    // 维持原有选中状态
    if (curSelected) {
        anchorVoiceSel.value = curSelected;
    }

    // 提示当前绑定的所属引擎
    const hintEl = document.getElementById("anchor-voice-engine-hint");
    if (hintEl) {
        hintEl.innerHTML = `<span style="color: var(--accent-emerald); font-size: 11px;">💡 当前直播语音引擎: <strong>${escapeHtml(activeEngineLabel)}</strong> (下拉仅列出该引擎的音色资产)</span>`;
    }
}

async function loadVoiceTable() {
    try {
        const res = await fetch(`${API_BASE}/voices/list`);
        const json = await res.json();
        if (json.code !== 0) return;
        voiceCache = json.data || [];
        window._voiceProfilesCache = voiceCache;

        // 1. 渲染音色资产表格
        renderVoiceTableRows();

        // 2. 联动主播管理页的音色下拉（按当前生效引擎过滤）
        await populateAnchorVoiceSelect(voiceCache);
    } catch (e) {
        console.error("加载音色失败", e);
    }
}

async function submitVoice() {
    const editIdEl = document.getElementById("voice-edit-id");
    const editId = editIdEl ? editIdEl.value : "";
    const nameEl = document.getElementById("voice-name");
    const name = nameEl ? nameEl.value.trim() : "";
    const fileInput = document.getElementById("voice-file");
    if (!name) { alert("请输入音色名称"); return; }

    try {
        if (!editId) {
            // 新建：必须上传声音样本
            if (!fileInput.files || !fileInput.files.length) {
                alert("请选择 10 秒左右清晰人声音频文件 (WAV/MP3)");
                return;
            }
            const fd = new FormData();
            fd.append("name", name);
            fd.append("audio_file", fileInput.files[0]);
            fd.append("speed", document.getElementById("voice-speed").value);
            const res = await fetch(`${API_BASE}/voices/clone`, { method: "POST", body: fd });
            const json = await res.json();
            if (json.code !== 0) { alert("上传失败: " + (json.detail || json.message)); return; }

            showToast(`音色【${name}】样本已成功保存至音色资产库。`, "success", 3000);
        } else {
            const res = await fetch(`${API_BASE}/voices/update`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ id: editId, name, speech_speed: parseFloat(document.getElementById("voice-speed").value) || 1.0 })
            });
            const json = await res.json();
            if (json.code !== 0) { alert("更新失败: " + (json.detail || json.message)); return; }
        }
        resetVoiceForm();
        loadVoiceTable();
    } catch (e) { alert("操作异常: " + e); }
}

// 在线试听：页内浮动播放器 (不新开标签页，即点即听)
let previewAudioEl = null;
function previewVoice(voiceId) {
    if (window._ttsPreviewController && typeof window._ttsPreviewController.stopAll === "function") {
        window._ttsPreviewController.stopAll();
    }
    if (previewAudioEl) { try { previewAudioEl.pause(); } catch (e) { /* 忽略 */ } }
    previewAudioEl = new Audio(`${API_BASE}/voices/${voiceId}/preview`);
    if (window._ttsPreviewController) {
        window._ttsPreviewController.audio = previewAudioEl;
    }
    previewAudioEl.play().then(() => {
        showToast("正在播放声音样本...", "info", 2000);
    }).catch(() => {
        alert("浏览器阻止了自动播放，请先点击页面任意位置激活音频权限，再点一次试听");
    });
}

async function editVoice(voiceId) {
    const v = voiceCache.find(x => x.id === voiceId);
    if (!v) return;
    const newName = prompt(`请输入音色【${v.name}】的新名称:`, v.name);
    if (newName === null) return;
    const cleanName = newName.trim();
    if (!cleanName) {
        if (typeof showToast === "function") showToast("音色名称不能为空", "warning");
        return;
    }
    try {
        const res = await fetch(`${API_BASE}/voices/update`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ id: voiceId, name: cleanName })
        });
        const json = await res.json();
        if (json.code === 0) {
            if (typeof showToast === "function") showToast(`已成功将音色重命名为【${cleanName}】`, "success");
            await loadVoiceTable();
            // 联动刷新已配置的 TTS 引擎卡片，立即呈现新音色名
            if (typeof renderConfiguredTTS === "function" && typeof cachedAllConfigs !== "undefined") {
                renderConfiguredTTS(cachedAllConfigs);
            }
        } else {
            alert("修改失败: " + (json.detail || json.message));
        }
    } catch (e) {
        alert("改名异常: " + e);
    }
}

function resetVoiceForm() {
    const editIdEl = document.getElementById("voice-edit-id");
    if (editIdEl) editIdEl.value = "";
    const nameEl = document.getElementById("voice-name");
    if (nameEl) nameEl.value = "";
    const fileEl = document.getElementById("voice-file");
    if (fileEl) fileEl.value = "";
    const speedEl = document.getElementById("voice-speed");
    if (speedEl) speedEl.value = 1.0;
    const speedValEl = document.getElementById("voice-speed-val");
    if (speedValEl) speedValEl.innerText = "1.0x";
    const hintEl = document.getElementById("voice-edit-hint");
    if (hintEl) hintEl.innerText = "";
    const resetBtn = document.getElementById("voice-reset-btn");
    if (resetBtn) resetBtn.style.display = "none";
}

async function deleteVoice(voiceId, name) {
    if (!confirm(`确认删除音色【${name}】？`)) return;
    try {
        const res = await fetch(`${API_BASE}/voices/${voiceId}`, { method: "DELETE" });
        const json = await res.json();
        if (json.code === 0) loadVoiceTable();
    } catch (e) { alert("删除失败: " + e); }
}

// 服务商详细元数据定义 (帮助用户秒懂每一项是干什么的、是否必填、常见填法)
const PROVIDER_META = {
    "openai_compatible": {
        title: "云端大语言模型 (AI 主播思考大脑)",
        typeBadge: "LLM 大脑",
        requiredTag: '<span class="badge-required">必配其一</span>',
        recommendTag: '<span class="badge-recommend">推荐首选</span>',
        desc: "负责实时理解直播间观众弹幕、组织高情商话术、讲透商品核心卖点、并根据逼单节奏催单。支持任意兼容 OpenAI 接口规范的云端大模型服务（如 DeepSeek、Kimi、通义千问、OpenAI 等）。",
        urlLabel: "服务商 API 接口地址 (Base URL):",
        urlTip: "服务商提供的模型请求根地址，通常以 <code>/v1</code> 结尾。如使用 DeepSeek 请点击下方快捷标签填入。",
        urlPills: [
            { text: "填入 DeepSeek 官方地址", val: "https://api.deepseek.com/v1" },
            { text: "填入 OpenAI 官方地址", val: "https://api.openai.com/v1" },
            { text: "填入 硅基流动 SiliconFlow", val: "https://api.siliconflow.cn/v1" }
        ],
        modelLabel: "思考模型代号 (Model ID):",
        modelTip: "<strong>什么是模型代号？</strong> 决定 AI 主播具体用哪一个聪明程度的脑子来思考并吐字。例如 DeepSeek 填 <code>deepseek-chat</code>，OpenAI 填 <code>gpt-4o</code>。",
        modelPills: [
            { text: "deepseek-chat (超实惠·高情商带货推荐)", val: "deepseek-chat" },
            { text: "deepseek-reasoner (深度逻辑推理)", val: "deepseek-reasoner" },
            { text: "gpt-4o (国际顶配多模态)", val: "gpt-4o" }
        ],
        keyLabel: "API 访问密钥 (API Key - 本地硬件加密):",
        keyTip: "在对应大模型开放平台注册后生成的密钥（以 <code>sk-</code> 开头）。必填，系统会以 AES-256 加密存放在本地。"
    },
    "local_ollama": {
        title: "本地离线大模型 (Ollama 纯单机引擎)",
        typeBadge: "LLM 大脑",
        requiredTag: '<span class="badge-optional">选填</span>',
        recommendTag: '',
        desc: "在您的本地独立显卡上运行开源模型，完全断网也能正常直播，免收任何 API 接口费，隐私绝对保密。需电脑预先下载并运行 Ollama 软件。",
        urlLabel: "本地 Ollama 监听地址:",
        urlTip: "本地 Ollama 服务的网络端口地址，默认通常为 <code>http://127.0.0.1:11434</code>。",
        urlPills: [
            { text: "默认地址: http://127.0.0.1:11434", val: "http://127.0.0.1:11434" }
        ],
        modelLabel: "本地模型名称 (Model Tag):",
        modelTip: "<strong>什么是本地模型名？</strong> 即您在本地终端通过 <code>ollama pull</code> 下载的模型名称标签。",
        modelPills: [
            { text: "deepseek-r1:14b", val: "deepseek-r1:14b" },
            { text: "deepseek-r1:8b", val: "deepseek-r1:8b" },
            { text: "qwen2.5:7b", val: "qwen2.5:7b" }
        ],
        keyLabel: "API 访问密钥 (API Key):",
        keyTip: "本地单机部署无需 API Key，保持留空即可。"
    },
    "cloud_edge_tts": {
        title: "微软免费智能语音 (Edge-TTS 高保真发音)",
        typeBadge: "TTS 语音",
        requiredTag: '<span class="badge-required">必配其一</span>',
        recommendTag: '<span class="badge-recommend">新手强烈推荐</span>',
        desc: "由微软免费提供的知性自然发音引擎，零显卡算力占用，完全免费，开箱即用，自带多种拟真普通话主播音色，发音流畅自然。",
        urlLabel: "服务通信通道:",
        urlTip: "使用微软内置免费直连通道，无需设置，保持留空即可。",
        urlPills: [],
        modelLabel: "主播发音人音色代号 (Voice ID):",
        modelTip: "<strong>什么是发音人音色？</strong> 决定 AI 主播开口说话的声音音色与性格特征。点击下方快捷标签可一键选择：",
        modelPills: [
            { text: "zh-CN-XiaoxiaoNeural (知性亲和女声·带货首选)", val: "zh-CN-XiaoxiaoNeural" },
            { text: "zh-CN-XiaoyiNeural (甜美活泼少女·娱乐陪伴)", val: "zh-CN-XiaoyiNeural" },
            { text: "zh-CN-YunjianNeural (沉稳专业男声·咨询顾问)", val: "zh-CN-YunjianNeural" },
            { text: "zh-CN-YunxiNeural (自然阳光暖男·唠嗑互动)", val: "zh-CN-YunxiNeural" }
        ],
        keyLabel: "API 访问密钥 (API Key):",
        keyTip: "微软免费通道免鉴权，无需填写 Key。"
    },
    "local_cosyvoice": {
        title: "本地声音克隆引擎 (CosyVoice 零样本声音复刻)",
        typeBadge: "TTS 语音",
        requiredTag: '<span class="badge-optional">选填</span>',
        recommendTag: '',
        desc: "基于阿里开源 CosyVoice2 架构。只需您上传一段 10 秒钟的原声音频，即可直接复刻出您的专属声音进行直播（需要本地具备 6GB+ 独立显卡）。",
        urlLabel: "本地 CosyVoice 推理端口地址:",
        urlTip: "本地独立启动的 CosyVoice 服务端口，默认通常为 <code>http://127.0.0.1:9233</code>。",
        urlPills: [
            { text: "默认地址: http://127.0.0.1:9233", val: "http://127.0.0.1:9233" }
        ],
        modelLabel: "声学克隆版本号:",
        modelTip: "<strong>版本号说明：</strong> 默认为官方推荐的 <code>CosyVoice2-0.5B</code> 多情感流式生成模型。",
        modelPills: [
            { text: "CosyVoice2-0.5B (低延迟流式)", val: "CosyVoice2-0.5B" }
        ],
        keyLabel: "API 访问密钥 (API Key):",
        keyTip: "本地私有服务免密，无需填写。"
    },
    "remote_gpu": {
        title: "端云分离远程 GPU 节点 (云端 4090 渲染回传)",
        typeBadge: "远程算力",
        requiredTag: '<span class="badge-required">模式 C 必配</span>',
        recommendTag: '<span class="badge-recommend">端云分离首选</span>',
        desc: "在 RunPod / AutoDL / 阿里云租用 4090 容器后，填入其 WebSocket 渲染网关地址与鉴权 Token，本地调度中枢即可把渲染任务穿透到云端，4K 画质推回本地 OBS。",
        urlLabel: "云端节点 WebSocket 地址:",
        urlTip: "格式 <code>ws://服务器IP:8888/ws/render</code>。参考 scripts/docker-runpod.sh 一键拉起云端节点。",
        urlPills: [],
        modelLabel: "节点备注 (选填):",
        modelTip: "例如：AutoDL 华东 4090 按量计费节点。",
        modelPills: [],
        keyLabel: "云端节点鉴权 Token (AUTH_TOKEN):",
        keyTip: "云端容器启动时设置的 AUTH_TOKEN，本地与云端必须一致。"
    }
};

// ==============================================================================
// 8. 云端大语言模型 (AI 主播思考大脑) 多生态选型、多配置列表与连通性测试
// ==============================================================================

const BUILTIN_LLM_ECOSYSTEM = [
    {
        id: "deepseek",
        name: "DeepSeek",
        tagline: "国产性价比标杆 · 高情商促单",
        brandColor: "#0284C7",
        logoSvg: "/static/svg/model_deepseek.svg",
        defaultBaseUrl: "https://api.deepseek.com/v1",
        recommendedModels: ["deepseek-chat", "deepseek-reasoner"],
        desc: "国产顶流高情商大模型，超低调用资费，高情商话术与实时弹幕互动首选。",
        urlPills: [
            { text: "DeepSeek 官方 (推荐)", val: "https://api.deepseek.com/v1" },
            { text: "备用直连端点", val: "https://api.deepseek.com" }
        ],
        modelPills: [
            { text: "deepseek-chat (超实惠·高情商带货首选)", val: "deepseek-chat" },
            { text: "deepseek-reasoner (深度逻辑推理链)", val: "deepseek-reasoner" }
        ]
    },
    {
        id: "qwen",
        name: "Qwen 通义",
        tagline: "直播电商霸主 · 极速指令遵循",
        brandColor: "#0070F3",
        logoSvg: "https://img.alicdn.com/imgextra/i2/O1CN01TOFMg022PLymzwaSX_!!6000000007112-55-tps-40-40.svg",
        defaultBaseUrl: "https://ws-mw0wa7jqi376y132.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        recommendedModels: ["qwen-plus", "qwen-turbo", "qwen-max", "qwen2.5-72b-instruct"],
        desc: "阿里系模型适合电商直播话术生成，支持促销表达与商品解读，并可与 CosyVoice 配置组合使用。",
        urlPills: [
            { text: "☁️ 我的百炼专属节点 (北京)", val: "https://ws-mw0wa7jqi376y132.cn-beijing.maas.aliyuncs.com/compatible-mode/v1" },
            { text: "阿里云 DashScope (官方兼容)", val: "https://dashscope.aliyuncs.com/compatible-mode/v1" },
            { text: "本地 Ollama Qwen2.5", val: "http://127.0.0.1:11434/v1" }
        ],
        modelPills: [
            { text: "qwen-plus (电商直播强烈推荐)", val: "qwen-plus" },
            { text: "qwen-turbo (毫秒极速首包响应)", val: "qwen-turbo" },
            { text: "qwen-max (高阶多任务复杂推理)", val: "qwen-max" }
        ]
    },
    {
        id: "minimax",
        name: "MiniMax",
        tagline: "万亿长文本 · 细腻拟真共情",
        brandColor: "#FF5226",
        logoSvg: "/static/svg/model_minimax.svg",
        defaultBaseUrl: "https://api.minimax.chat/v1",
        recommendedModels: ["MiniMax-Text-01", "abab6.5s-chat"],
        desc: "全自研万亿长文本与拟真共情模型，情绪与音色表达出众，直播陪伴感强。",
        urlPills: [
            { text: "MiniMax 官方", val: "https://api.minimax.chat/v1" }
        ],
        modelPills: [
            { text: "MiniMax-Text-01 (旗舰长文本)", val: "MiniMax-Text-01" },
            { text: "abab6.5s-chat (拟人多轮对话)", val: "abab6.5s-chat" }
        ]
    },
    {
        id: "kimi",
        name: "Kimi 月暗",
        tagline: "超长上下文 · 直播选品记忆",
        brandColor: "#1783FF",
        logoSvg: "/static/svg/model_kimi.svg",
        defaultBaseUrl: "https://api.moonshot.cn/v1",
        recommendedModels: ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        desc: "无损超长上下文标杆，百款商品 SKU 参数、品牌白皮书与大促规则精准深度召回，零幻觉不乱编。",
        urlPills: [
            { text: "Moonshot 官方", val: "https://api.moonshot.cn/v1" }
        ],
        modelPills: [
            { text: "moonshot-v1-8k (常规弹幕互动)", val: "moonshot-v1-8k" },
            { text: "moonshot-v1-32k (海量商品挂载召回)", val: "moonshot-v1-32k" }
        ]
    },
    {
        id: "glm",
        name: "GLM 智谱",
        tagline: "清华系标杆 · 合规安全极速",
        brandColor: "#0D9488",
        logoSvg: "/static/svg/model_glm.svg",
        defaultBaseUrl: "https://open.bigmodel.cn/api/paas/v4/",
        recommendedModels: ["glm-4-flash", "glm-4-plus", "glm-4"],
        desc: "智谱自研成熟基座，中文语境深厚，安全审查与合规能力极强，glm-4-flash 免费且超快。",
        urlPills: [
            { text: "智谱开放平台官方", val: "https://open.bigmodel.cn/api/paas/v4/" }
        ],
        modelPills: [
            { text: "glm-4-flash (超快免费极速响应)", val: "glm-4-flash" },
            { text: "glm-4-plus (高阶综合认知)", val: "glm-4-plus" }
        ]
    },
    {
        id: "gemini",
        name: "Gemini",
        tagline: "超快首包 · 原生多模态感知",
        brandColor: "#2563EB",
        logoSvg: "/static/svg/model_gemini.svg",
        defaultBaseUrl: "https://generativelanguage.googleapis.com/v1beta/openai/",
        recommendedModels: ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-pro"],
        desc: "Google 旗舰多模态大模型，flash 系列具备毫秒级首包极速生成，契合多模态眼见即所言。",
        urlPills: [
            { text: "Google 官方 OpenAI 兼容通道", val: "https://generativelanguage.googleapis.com/v1beta/openai/" }
        ],
        modelPills: [
            { text: "gemini-2.0-flash (次世代极速)", val: "gemini-2.0-flash" },
            { text: "gemini-1.5-flash (超快首包)", val: "gemini-1.5-flash" }
        ]
    },
    {
        id: "chatgpt",
        name: "ChatGPT",
        tagline: "全球顶级旗舰 · 全能综合推理",
        brandColor: "#10A37F",
        logoSvg: "/static/svg/model_chatgpt.svg",
        defaultBaseUrl: "https://api.openai.com/v1",
        recommendedModels: ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"],
        desc: "OpenAI 工业级基准大模型，具备出色的多任务理解、结构化输出与丰富插件兼容能力。",
        urlPills: [
            { text: "OpenAI 官方", val: "https://api.openai.com/v1" }
        ],
        modelPills: [
            { text: "gpt-4o (旗舰综合多模态)", val: "gpt-4o" },
            { text: "gpt-4o-mini (经济型轻量大脑)", val: "gpt-4o-mini" }
        ]
    },
    {
        id: "custom",
        name: "自定义/离线",
        tagline: "支持 Ollama / 硅基流动",
        brandColor: "#F59E0B",
        logoSvg: "/static/svg/model_custom.svg",
        defaultBaseUrl: "https://api.siliconflow.cn/v1",
        recommendedModels: ["Qwen/Qwen2.5-72B-Instruct", "deepseek-ai/DeepSeek-V3"],
        desc: "任意符合 OpenAI API 规范的代理服务、云端 API 或本地 Ollama/vLLM 离线网关自由接入。",
        urlPills: [
            { text: "硅基流动 SiliconFlow", val: "https://api.siliconflow.cn/v1" },
            { text: "本地 Ollama 离线", val: "http://127.0.0.1:11434/v1" }
        ],
        modelPills: [
            { text: "Qwen/Qwen2.5-72B-Instruct", val: "Qwen/Qwen2.5-72B-Instruct" },
            { text: "deepseek-ai/DeepSeek-V3", val: "deepseek-ai/DeepSeek-V3" }
        ]
    }
];

let currentSelectedLLMProvider = "deepseek";
let cachedAllConfigs = [];

// 初始化大模型品牌卡片网格 (严格 8 列横向一排)
