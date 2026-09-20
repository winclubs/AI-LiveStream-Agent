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
            if (currentVoiceTableEngineFilter === "moss_tts_nano") {
                return vp.includes("moss") || vp.includes("nano") || vid.includes("moss");
            } else if (currentVoiceTableEngineFilter === "cosyvoice") {
                return vp.includes("cosy") || vid.includes("bailian") || vid.includes("cosyvoice") || vid.startsWith("long");
            } else if (currentVoiceTableEngineFilter === "elevenlabs") {
                return vp.includes("eleven");
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
            activeEngine = "moss_tts_nano";
            activeEngineLabel = "MOSS-TTS-Nano";
        }
    }

    // 2. 根据当前语音引擎过滤音色
    const engineVoices = (voices || []).filter(v => {
        const vp = (v.provider_name || "").toLowerCase();
        if (activeEngine.includes("moss") || activeEngine.includes("nano")) {
            return vp.includes("moss") || vp.includes("nano") || (v.id && v.id.includes("moss"));
        } else if (activeEngine.includes("cosy")) {
            return vp.includes("cosy") || (v.id && (v.id.includes("bailian") || v.id.includes("cosyvoice")));
        } else if (activeEngine.includes("eleven")) {
            return vp.includes("eleven");
        } else if (vp) {
            return vp === activeEngine;
        }
        return true;
    });

    const curSelected = anchorVoiceSel.value;
    anchorVoiceSel.innerHTML = `<option value="">未绑定音色 (开播采用默认发音)</option>`;

function formatVoiceDisplayName(v) {
    if (!v) return "";
    const name = String(v.name || v.id || "").trim();
    // 判断名称是否已有明确的性别或人设说明括号
    const hasExplicitDesc = /[\(（].*?(女|男|童|妹|姐|叔|哥|少女|主播).*?[\)）]/.test(name);
    if (hasExplicitDesc) {
        return name;
    }
    
    // 智能推导性别 (优先读取后端下发的 gender，其次本地特征推导)
    let g = (v.gender || "").toLowerCase();
    if (!g || g === "unknown") {
        const lower = `${name} ${v.id || ''}`.toLowerCase();
        if (/女|girl|female|woman|少女|知性|萌音|姐|妹|娘|春|夏|婉|悦|玲|stella|bella|xiaoxiao|xiaoyi/.test(lower)) {
            g = "female";
        } else if (/男|boy|male|man|老铁|叔|哥|爷|诚|华|硕|渊|飞|杰|天|平|yunjian|yunxi/.test(lower)) {
            g = "male";
        }
    }

    if (g === "female") {
        return `${name} (女声)`;
    } else if (g === "male") {
        return `${name} (男声)`;
    }
    return name;
}

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
            opt.innerText = `👑 ${formatVoiceDisplayName(v)} (专属克隆)`;
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
            opt.innerText = `🎙️ ${formatVoiceDisplayName(v)}`;
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

// 在线试听：页内浮动播放器 (不新开标签页，即点即听)
let previewAudioEl = null;
async function previewVoice(voiceId) {
    if (!voiceId) return;
    if (window._ttsPreviewController && typeof window._ttsPreviewController.stopAll === "function") {
        window._ttsPreviewController.stopAll();
    }
    if (previewAudioEl) {
        try {
            previewAudioEl.pause();
            previewAudioEl.src = "";
        } catch (e) { /* 忽略 */ }
    }

    const audioUrl = `${API_BASE}/voices/${voiceId}/preview?t=${Date.now()}`;
    if (typeof showToast === "function") {
        showToast("正在加载试听音频...", "info", 1200);
    }

    try {
        const resp = await fetch(audioUrl, { method: "GET" });
        if (!resp.ok) {
            let errorMsg = `服务返回 HTTP ${resp.status}`;
            try {
                const errData = await resp.json();
                if (errData && errData.detail) errorMsg = errData.detail;
            } catch (_) {}
            if (typeof showToast === "function") {
                showToast(`试听失败: ${errorMsg}`, "warning", 5000);
            }
            return;
        }

        const blob = await resp.blob();
        const blobUrl = URL.createObjectURL(blob);
        previewAudioEl = new Audio(blobUrl);
        if (window._ttsPreviewController) {
            window._ttsPreviewController.audio = previewAudioEl;
        }

        previewAudioEl.onended = () => {
            URL.revokeObjectURL(blobUrl);
        };

        const playPromise = previewAudioEl.play();
        if (playPromise !== undefined) {
            playPromise.then(() => {
                if (typeof showToast === "function") showToast("正在播放声音样本...", "info", 2000);
            }).catch(err => {
                console.warn("试听播放异常:", err);
                if (err && err.name === "NotAllowedError") {
                    alert("浏览器阻止了自动播放，请先点击页面任意位置激活音频权限，再点一次试听");
                } else {
                    if (typeof showToast === "function") {
                        showToast("音频解码失败，请确认音频格式完整", "warning", 3000);
                    }
                }
            });
        }
    } catch (netErr) {
        console.warn("试听网络请求异常:", netErr);
        if (typeof showToast === "function") {
            showToast("网络请求异常，无法加载试听音频", "error", 3000);
        }
    }
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

async function deleteVoice(voiceId, name) {
    if (!confirm(`确认删除音色【${name}】？`)) return;
    try {
        const res = await fetch(`${API_BASE}/voices/${voiceId}`, { method: "DELETE" });
        const json = await res.json();
        if (json.code === 0) loadVoiceTable();
    } catch (e) { alert("删除失败: " + e); }
}

