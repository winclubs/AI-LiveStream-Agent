function toggleUploadDocModal() {
    const box = document.getElementById("upload-doc-box");
    if (!box) return;
    box.style.display = box.style.display === "none" ? "block" : "none";
}

async function loadKnowledgeList() {
    try {
        const res = await fetch(`${API_BASE}/knowledge/list`);
        if (!res.ok) return;
        const json = await res.json();
        const docs = json.data || [];
        
        // 统计更新
        const docCountEl = document.getElementById("kb-doc-count");
        const chunkCountEl = document.getElementById("kb-chunk-count");
        if (docCountEl) docCountEl.innerText = `${docs.length} 篇`;
        
        let totalChunks = 0;
        docs.forEach(d => totalChunks += (d.chunk_count || 0));
        if (chunkCountEl) chunkCountEl.innerText = `${totalChunks} 条`;

        const tbody = document.getElementById("kb-docs-tbody");
        if (!tbody) return;

        if (docs.length === 0) {
            tbody.innerHTML = `<tr><td colspan="5" style="text-align: center; color: var(--text-secondary); padding: 30px;">暂无入库知识文档，请点击上方【上传知识文献】导入</td></tr>`;
            return;
        }

        tbody.innerHTML = docs.map(doc => `
            <tr>
                <td style="font-weight: 600; color: var(--text-primary);">
                    <div style="display: flex; align-items: center; gap: 6px;">
                        ${svg("clipboard", "icon-sm")}
                        <span>${doc.doc_name || "未命名"}</span>
                    </div>
                </td>
                <td style="font-family: var(--font-mono); font-size: 11px; color: var(--text-secondary);">${doc.doc_id || ""}</td>
                <td><span class="brand-badge green">${doc.chunk_count || 0} 个切片</span></td>
                <td style="color: var(--text-secondary); font-size: 12px; max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${doc.preview || ''}">
                    ${doc.preview || "-"}
                </td>
                <td>
                    <button class="btn btn-sm" style="color: var(--red); border-color: rgba(239,68,68,0.3);" onclick="deleteKnowledgeDoc('${doc.doc_id}', '${doc.doc_name || ''}')">
                        ${svg("trash", "icon-sm")} 删除
                    </button>
                </td>
            </tr>
        `).join("");
    } catch (e) {
        console.error("加载知识库列表异常:", e);
    }
}

async function submitUploadDoc() {
    const fileInput = document.getElementById("kb-upload-file");
    const titleInput = document.getElementById("kb-upload-title");
    const loadingEl = document.getElementById("kb-upload-loading");
    const submitBtn = document.getElementById("btn-submit-upload-doc");

    if (!fileInput || !fileInput.files || fileInput.files.length === 0) {
        alert("请先选择要上传的文档文件 (.txt, .md, .csv, .pdf, .docx)");
        return;
    }

    const file = fileInput.files[0];
    const docName = titleInput ? titleInput.value.trim() : "";

    const formData = new FormData();
    formData.append("file", file);
    if (docName) {
        formData.append("doc_name", docName);
    }

    try {
        if (loadingEl) loadingEl.style.display = "inline";
        if (submitBtn) submitBtn.disabled = true;

        const res = await fetch(`${API_BASE}/knowledge/upload`, {
            method: "POST",
            body: formData
        });
        const json = await res.json();

        if (res.ok && json.code === 0) {
            showToast(json.message || "文档已成功导入知识库", "success");
            fileInput.value = "";
            if (titleInput) titleInput.value = "";
            toggleUploadDocModal();
            loadKnowledgeList();
        } else {
            alert(json.detail || json.message || "上传解析失败");
        }
    } catch (e) {
        alert("上传知识库文档异常: " + e);
    } finally {
        if (loadingEl) loadingEl.style.display = "none";
        if (submitBtn) submitBtn.disabled = false;
    }
}

async function deleteKnowledgeDoc(docId, docName) {
    if (!confirm(`确认要删除文献【${docName || docId}】及其所有分块切片吗？`)) return;

    try {
        const res = await fetch(`${API_BASE}/knowledge/${docId}`, {
            method: "DELETE"
        });
        const json = await res.json();
        if (res.ok && json.code === 0) {
            showToast(json.message || "文献已成功移除", "success");
            loadKnowledgeList();
        } else {
            alert(json.detail || json.message || "删除失败");
        }
    } catch (e) {
        alert("删除知识文献异常: " + e);
    }
}

async function testKnowledgeQuery() {
    const queryInput = document.getElementById("kb-test-query");
    const topkSelect = document.getElementById("kb-test-topk");
    const container = document.getElementById("kb-test-results-container");
    const listEl = document.getElementById("kb-test-results-list");

    if (!queryInput || !queryInput.value.trim()) {
        alert("请输入要测试检索的问题关键词");
        return;
    }

    const query = queryInput.value.trim();
    const top_k = parseInt(topkSelect ? topkSelect.value : "3", 10) || 3;

    try {
        const res = await fetch(`${API_BASE}/knowledge/search`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ query, top_k })
        });
        const json = await res.json();

        if (container) container.style.display = "block";
        const ragData = json.data || {};
        const results = ragData.hits || [];

        if (results.length === 0) {
            if (listEl) {
                listEl.innerHTML = `<div style="color: var(--amber); font-size: 13px; padding: 8px 0;">未检索到匹配的知识切片（综合匹配分低于引擎置信度门限，将触发安全兜底话术）。</div>`;
            }
            return;
        }

        if (listEl) {
            listEl.innerHTML = results.map((item, idx) => `
                <div style="background: rgba(255,255,255,0.03); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); padding: 12px; margin-bottom: 10px;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                        <span style="font-weight: 600; font-size: 13px; color: var(--signal);">#${idx + 1} 来自文献: ${item.doc_name || '未命名'}</span>
                        <span class="brand-badge green">综合匹配分: ${(item.score || 0).toFixed(4)}</span>
                    </div>
                    <div style="font-size: 11px; font-family: var(--font-mono); color: var(--text-secondary); margin-bottom: 6px;">Chunk ID: ${item.chunk_id || '-'}</div>
                    <div style="font-size: 13px; color: var(--text-primary); line-height: 1.6; background: rgba(0,0,0,0.25); padding: 8px 12px; border-radius: 4px; border-left: 3px solid var(--signal);">
                        ${item.content || ''}
                    </div>
                </div>
            `).join("");
        }
    } catch (e) {
        alert("知识检索测试异常: " + e);
    }
}

// ===========================================================================
// RAG 引擎状态 (真实语义向量 / 降级) 与 实时多模态视觉感知配置
// ===========================================================================
async function loadKnowledgeStatus() {
    try {
        const res = await fetch(`${API_BASE}/knowledge/status`);
        const json = await res.json();
        const el = document.getElementById("kb-engine-status");
        if (el && json.data) {
            const d = json.data;
            const backend = d.vector_backend === "onnx" ? "BGE 真实语义向量 (ONNX)" : "哈希投影向量 (离线降级)";
            el.innerText = `${backend} · ${d.dim} 维 · ${d.chunks} 分块 · 置信阈值 ${d.min_score}`;
        }
    } catch (e) { /* 静默 */ }
}

async function loadVisionConfig() {
    try {
        const res = await fetch(`${API_BASE}/settings/vision`);
        const json = await res.json();
        const d = json.data || {};
        const cb = document.getElementById("vision-enabled");
        if (cb) cb.checked = !!d.enabled;
        const src = document.getElementById("vision-source");
        if (src) src.value = d.source || "desktop_screen";
        const iv = document.getElementById("vision-interval");
        if (iv) iv.value = d.interval_sec || 2.5;
        const st = document.getElementById("vision-status");
        if (st) {
            const s = d.status || {};
            if (!s.enabled) {
                st.innerText = "视觉通道：已关闭";
                st.style.color = "var(--text-muted)";
            } else if (s.available) {
                st.innerText = "视觉通道：已启用 · 采集正常";
                st.style.color = "var(--signal)";
            } else {
                st.innerText = "视觉通道：已启用但依赖缺失 (请安装 Pillow/opencv-python)";
                st.style.color = "var(--amber)";
            }
        }
    } catch (e) { /* 静默 */ }
}

async function saveVisionConfig() {
    const enabled = (document.getElementById("vision-enabled") || {}).checked || false;
    const source = (document.getElementById("vision-source") || {}).value || "desktop_screen";
    const interval_sec = parseFloat((document.getElementById("vision-interval") || {}).value || "2.5");
    try {
        const res = await fetch(`${API_BASE}/settings/vision`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ enabled, source, interval_sec })
        });
        const json = await res.json();
        showToast(json.message || "视觉感知配置已保存", json.code === 0 ? "success" : "error");
        loadVisionConfig();
    } catch (e) {
        alert("保存视觉感知配置异常: " + e);
    }
}

// ===========================================================================
// 物理音频输出设备与虚拟声卡 (VB-Cable) 配置 (规划 §7.1/§7.2)
// ===========================================================================
async function loadAudioDevices() {
    try {
        const res = await fetch(`${API_BASE}/settings/audio-devices`);
        const json = await res.json();
        if (json.code === 0 && json.data) {
            const selectEl = document.getElementById("audio-device-select");
            const badgeEl = document.getElementById("audio-device-badge");
            const status = json.data.status || {};
            const devices = json.data.devices || [];

            if (badgeEl) {
                if (!status.available) {
                    badgeEl.className = "brand-badge amber";
                    badgeEl.innerText = "未安装 sounddevice (已软降级)";
                } else {
                    badgeEl.className = "brand-badge green";
                    badgeEl.innerText = `输出至: ${status.device_name || '默认设备'}`;
                }
            }

            if (selectEl) {
                selectEl.innerHTML = `<option value="">系统默认音频播放设备</option>`;
                devices.forEach(d => {
                    const opt = document.createElement("option");
                    opt.value = String(d.index);
                    const mark = d.is_virtual_cable ? " ★ [推荐虚拟声卡 VB-Cable]" : (d.is_default ? " [系统默认]" : "");
                    opt.textContent = `#${d.index}: ${d.name}${mark}`;
                    if (status.device_index !== null && status.device_index === d.index) {
                        opt.selected = true;
                    }
                    selectEl.appendChild(opt);
                });
            }
        }
    } catch (e) {
        console.warn("加载音频设备列表异常:", e);
    }
}

async function saveAudioDeviceSelection() {
    const selectEl = document.getElementById("audio-device-select");
    const val = selectEl ? selectEl.value : "";
    const devIdx = val === "" ? null : parseInt(val);

    try {
        const res = await fetch(`${API_BASE}/settings/audio-device`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ device_index: devIdx })
        });
        const json = await res.json();
        if (json.code === 0) {
            showToast(json.message || "音频输出设备已更新", "success");
            loadAudioDevices();
        } else {
            showToast("切换音频设备失败: " + (json.detail || json.message), "error");
        }
    } catch (e) {
        alert("保存音频设备选择异常: " + e);
    }
}

/* =========================================================================
   内置 RTMP 直推引擎前端控制与联动 (脱离 OBS 一键推流)
   ========================================================================= */

const RTMP_STORAGE_KEY_URL = "ai_live_rtmp_url";
const RTMP_STORAGE_KEY_KEY = "ai_live_rtmp_key";

function toggleRtmpModal() {
    const modal = document.getElementById("rtmp-settings-modal");
    if (!modal) return;
    const isHidden = modal.style.display === "none" || !modal.style.display;
    modal.style.display = isHidden ? "flex" : "none";
    if (isHidden) {
        const urlInput = document.getElementById("rtmp-input-url");
        const keyInput = document.getElementById("rtmp-input-key");
        if (urlInput && !urlInput.value) {
            urlInput.value = localStorage.getItem(RTMP_STORAGE_KEY_URL) || "rtmp://live-push.bilivideo.com/live-bvc/";
        }
        if (keyInput && !keyInput.value) {
            keyInput.value = localStorage.getItem(RTMP_STORAGE_KEY_KEY) || "";
        }
        refreshRtmpStatus();
    }
}

function fillRtmpPreset(platform) {
    const urlInput = document.getElementById("rtmp-input-url");
    if (!urlInput) return;
    switch (platform) {
        case "bilibili":
            urlInput.value = "rtmp://live-push.bilivideo.com/live-bvc/";
            break;
        case "douyin":
            urlInput.value = "rtmp://live-push.douyincdn.com/live/";
            break;
        case "kuaishou":
            urlInput.value = "rtmp://live-push.kuaishou.com/live/";
            break;
        case "channels":
            urlInput.value = "rtmp://channels.weixin.qq.com/live/";
            break;
    }
    showToast(`已填入 ${platform} 常用推流前缀，请在下方补充直播码`, "info");
}

async function refreshRtmpStatus() {
    try {
        const res = await fetch(`${API_BASE}/live/rtmp/status`);
        const json = await res.json();
        if (json.code === 0 && json.data) {
            updateRtmpUI(json.data);
        }
    } catch (e) {
        console.warn("读取 RTMP 状态失败:", e);
    }
}

function updateRtmpUI(status) {
    const indicator = document.getElementById("rtmp-status-indicator");
    const badge = document.getElementById("rtmp-stream-badge");
    const btn = document.getElementById("btn-toggle-rtmp");
    const durEl = document.getElementById("rtmp-metric-duration");
    const frameEl = document.getElementById("rtmp-metric-frames");
    const audioEl = document.getElementById("rtmp-metric-audio");
    const errBanner = document.getElementById("rtmp-error-banner");

    const isStreaming = Boolean(status.is_streaming);

    if (indicator) {
        if (isStreaming) {
            indicator.style.color = "#10B981";
            indicator.textContent = "● 直推传输中 (25 FPS)";
        } else if (status.state === "error") {
            indicator.style.color = "#ef4444";
            indicator.textContent = "✕ 推流异常";
        } else {
            indicator.style.color = "#94a3b8";
            indicator.textContent = "○ 未启动";
        }
    }

    if (badge) {
        badge.style.display = isStreaming ? "inline-flex" : "none";
        if (isStreaming) {
            badge.textContent = `● RTMP直推 (${status.duration_seconds}s)`;
        }
    }

    if (btn) {
        if (isStreaming) {
            btn.style.background = "#ef4444";
            btn.style.borderColor = "#ef4444";
            btn.textContent = "⏹ 停止直推";
        } else {
            btn.style.background = "#10B981";
            btn.style.borderColor = "#10B981";
            btn.textContent = "▶ 立即开始直推";
        }
    }

    if (durEl) {
        const secs = Math.floor(status.duration_seconds || 0);
        const h = String(Math.floor(secs / 3600)).padStart(2, "0");
        const m = String(Math.floor((secs % 3600) / 60)).padStart(2, "0");
        const s = String(secs % 60).padStart(2, "0");
        durEl.textContent = `${h}:${m}:${s}`;
    }

    if (frameEl) frameEl.textContent = String(status.frames_sent || 0);
    if (audioEl) {
        const kb = Math.round((status.audio_bytes_sent || 0) / 1024);
        audioEl.textContent = `${kb} KB`;
    }

    if (errBanner) {
        if (status.last_error && !isStreaming) {
            errBanner.style.display = "block";
            errBanner.textContent = "最后推流提示: " + status.last_error;
        } else {
            errBanner.style.display = "none";
        }
    }
}

async function toggleRtmpStreaming() {
    const btn = document.getElementById("btn-toggle-rtmp");
    const isStopping = btn && btn.textContent.includes("停止");

    if (isStopping) {
        try {
            const res = await fetch(`${API_BASE}/live/rtmp/stop`, { method: "POST" });
            const json = await res.json();
            showToast(json.message || "推流已安全停止", "info");
            refreshRtmpStatus();
        } catch (e) {
            showToast("停止推流请求失败: " + e, "error");
        }
    } else {
        const urlInput = document.getElementById("rtmp-input-url");
        const keyInput = document.getElementById("rtmp-input-key");
        const rtmpUrl = (urlInput ? urlInput.value : "").trim();
        const streamKey = (keyInput ? keyInput.value : "").trim();

        if (!rtmpUrl) {
            showToast("请先填写推流服务器地址 (RTMP URL)", "warn");
            return;
        }

        localStorage.setItem(RTMP_STORAGE_KEY_URL, rtmpUrl);
        localStorage.setItem(RTMP_STORAGE_KEY_KEY, streamKey);

        try {
            const res = await fetch(`${API_BASE}/live/rtmp/start`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    rtmp_url: rtmpUrl,
                    stream_key: streamKey,
                    width: 720,
                    height: 960,
                    fps: 25,
                    bitrate_kbps: 2500
                })
            });
            const json = await res.json();
            if (json.code === 0) {
                showToast("内置 RTMP 直推引擎已启动！", "success");
                refreshRtmpStatus();
            } else {
                showToast("启动推流失败: " + (json.message || json.detail), "error");
                refreshRtmpStatus();
            }
        } catch (e) {
            showToast("请求启动推流异常: " + e, "error");
        }
    }
}

/* =========================================================================
   GPU配置(2) 与数字人画面选型：一行多卡片与独立多行描述卡片联动
   ========================================================================= */
