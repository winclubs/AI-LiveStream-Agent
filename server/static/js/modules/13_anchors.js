async function loadAnchors() {
    try {
        const res = await fetch(`${API_BASE}/anchors/list`);
        const json = await res.json();
        if (json.code !== 0) return;
        anchorCache = json.data || [];

        // 读取当前开播主播 (音色绑定链路: startLiveDirect 依赖 selected_anchor_id)
        let currentAnchorId = "";
        try {
            const mRes = await fetch(`${API_BASE}/settings/live-mode`);
            const mJson = await mRes.json();
            if (mJson.code === 0 && mJson.data) currentAnchorId = mJson.data.selected_anchor_id || "";
        } catch (e) {}

        const tbody = document.getElementById("anchors-tbody");
        if (!tbody) return;
        tbody.innerHTML = "";
        if (anchorCache.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; color:var(--text-muted); padding: 20px;">暂无主播档案，请在上方创建</td></tr>';
            return;
        }
        anchorCache.forEach(a => {
            const voiceName = escapeHtml((voiceCache.find(v => v.id === a.voice_id) || {}).name || "未绑定");
            const portrait = a.photos && a.photos.portrait;
            const photoCount = Object.values(a.photos || {}).filter(p => p).length;
            const thumb = portrait
                ? `<img src="/static-file?path=${encodeURIComponent(portrait)}" style="width: 38px; height: 38px; object-fit: cover; border-radius: 50%; border: 1px solid var(--border-subtle); vertical-align: middle; margin-right: 6px;" onerror="this.src='/static/svg/default_avatar.svg'"> `
                : `<img src="/static/svg/default_avatar.svg" style="width: 38px; height: 38px; object-fit: cover; border-radius: 50%; border: 1px solid var(--border-subtle); vertical-align: middle; margin-right: 6px;"> `;
            const isCurrent = a.id === currentAnchorId;
            const currentTag = isCurrent
                ? '<span class="badge-recommend" style="font-size: 10px; padding: 2px 8px; margin-left: 6px;">● 开播主播</span>'
                : "";
            const tr = document.createElement("tr");
            tr.innerHTML = `
                <td style="font-weight: 600; white-space: nowrap;">${thumb}${escapeHtml(a.name)}${currentTag}</td>
                <td>${voiceName}</td>
                <td>${photoCount}/4 张</td>
                <td style="max-width: 200px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtml(a.remark || '')}">${escapeHtml(a.remark || '-')}</td>
                <td style="white-space: nowrap;">
                    <div class="table-actions">
                        <button class="btn btn-sm ${isCurrent ? "btn-primary" : ""}" onclick="setCurrentAnchor('${a.id}')" ${isCurrent ? "disabled" : ""}>${isCurrent ? "已选中" : "设为开播"}</button>
                        <button class="btn btn-sm" onclick="editAnchor('${a.id}')">编辑</button>
                        <button class="btn btn-sm btn-danger" onclick="deleteAnchor('${a.id}', '${escapeHtml(a.name)}')">删除</button>
                    </div>
                </td>
            `;
            tbody.appendChild(tr);
        });
    } catch (e) { console.error("加载主播失败", e); }
}

async function setCurrentAnchor(anchorId) {
    try {
        const res = await fetch(`${API_BASE}/settings/selected-anchor`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ anchor_id: anchorId })
        });
        const json = await res.json();
        if (json.code === 0) {
            showToast("已设为开播主播，开播时将按其绑定音色发声 ✓", "success");
            loadAnchors();
        } else {
            alert("设置失败: " + (json.detail || json.message || "未知错误"));
        }
    } catch (e) { alert("设置异常: " + e); }
}

async function submitAnchor() {
    const editId = document.getElementById("anchor-edit-id").value;
    const name = document.getElementById("anchor-name").value.trim();
    if (!name) { alert("请输入主播姓名"); return; }

    const fd = new FormData();
    if (editId) fd.append("id", editId);
    fd.append("name", name);
    fd.append("voice_id", document.getElementById("anchor-voice").value || "");
    fd.append("remark", document.getElementById("anchor-remark").value.trim() || "");
    ["portrait", "full_body", "half_body", "side"].forEach(slot => {
        const input = document.getElementById(`anchor-photo-${slot}`);
        if (input && input.files && input.files.length) fd.append(slot, input.files[0]);
    });

    try {
        const url = editId ? `${API_BASE}/anchors/update` : `${API_BASE}/anchors/create`;
        const res = await fetch(url, { method: "POST", body: fd });
        const json = await res.json();
        if (json.code === 0) {
            resetAnchorForm();
            loadAnchors();
        } else {
            alert("保存失败: " + (json.detail || json.message));
        }
    } catch (e) { alert("保存异常: " + e); }
}

function editAnchor(anchorId) {
    const a = anchorCache.find(x => x.id === anchorId);
    if (!a) return;
    document.getElementById("anchor-edit-id").value = a.id;
    document.getElementById("anchor-name").value = a.name;
    document.getElementById("anchor-voice").value = a.voice_id || "";
    document.getElementById("anchor-remark").value = a.remark || "";
    document.getElementById("anchor-edit-hint").innerText = `(正在编辑: ${a.name})`;
    document.getElementById("anchor-reset-btn").style.display = "inline-block";
}

function resetAnchorForm() {
    document.getElementById("anchor-edit-id").value = "";
    document.getElementById("anchor-name").value = "";
    document.getElementById("anchor-remark").value = "";
    ["portrait", "full_body", "half_body", "side"].forEach(slot => {
        const input = document.getElementById(`anchor-photo-${slot}`);
        if (input) input.value = "";
    });
    document.getElementById("anchor-edit-hint").innerText = "";
    document.getElementById("anchor-reset-btn").style.display = "none";
}

async function deleteAnchor(anchorId, name) {
    if (!confirm(`确认删除主播【${name}】？`)) return;
    try {
        const res = await fetch(`${API_BASE}/anchors/${anchorId}`, { method: "DELETE" });
        const json = await res.json();
        if (json.code === 0) loadAnchors();
    } catch (e) { alert("删除失败: " + e); }
}

// ============================================================================
// 直播大屏运营参数 (需求8)：模式/主播/场观/打赏/成交额/网络/时长
// ============================================================================
async function loadLiveStats() {
    if (document.hidden) return;  // 页面不可见时暂停轮询，节省资源
    try {
        const res = await fetch(`${API_BASE}/live/stats`);
        const json = await res.json();
        if (json.code !== 0) return;
        const d = json.data;

        const set = (id, val) => { const el = document.getElementById(id); if (el) el.innerHTML = val; };
        set("stat-mode", d.mode ? `${d.mode} 档 · ${d.platform || ""}` : "未配置 (请先完成开播向导)");
        set("stat-anchor", d.anchor_name || "—");
        set("stat-duration", formatDuration(d.duration_sec));
        set("stat-network", d.network && d.network.status === "connected"
            ? `<span style="color: var(--accent-emerald);">● 链路正常 (${d.network.ws_clients} 客户端)</span>`
            : '<span style="color: var(--text-muted);">○ 空闲未开播</span>');
        set("stat-viewers", `${d.viewer_count} <span style="font-size:12px;color:var(--text-muted);">(${d.peak_viewer_count})</span>`);
        set("stat-gift", `¥${(d.gift_income_yuan || 0).toFixed(2)} <span style="font-size:12px;color:var(--text-muted);">(${d.gift_count}单)</span>`);
        set("stat-gmv", `¥${(d.gmv_yuan || 0).toFixed(2)} <span style="font-size:12px;color:var(--text-muted);">(${d.orders_count}单)</span>`);
        set("stat-volume", `${d.danmaku_count} / <span style="color: var(--accent-danger);">${d.guardrail_hits}</span>`);

        // 轮询数字人渲染与虚拟摄像头遥测指标
        try {
            const mRes = await fetch(`${API_BASE}/live/media/status`);
            if (mRes.ok) {
                const mJson = await mRes.json();
                const md = mJson.data || {};
                const fpsBadge = document.getElementById("stream-fps-badge");
                const camBadge = document.getElementById("cam-status-badge");
                const modeBadge = document.getElementById("stream-mode-badge");
                if (fpsBadge) fpsBadge.innerText = `${(md.fps || 25.0).toFixed(1)} FPS`;
                if (modeBadge) modeBadge.innerText = md.driver_type === "procedural_avatar" ? "程序化头像渲染" : (md.driver_type === "remote_gpu" ? "远程帧预览" : "仿真降级");
                if (camBadge && md.virtual_cam) {
                    if (md.virtual_cam.is_active) {
                        camBadge.className = "brand-badge green";
                        camBadge.innerText = "虚拟摄像头输出中";
                    } else if (md.virtual_cam.available === false) {
                        camBadge.className = "brand-badge";
                        camBadge.innerText = "虚拟摄像头未安装";
                    } else {
                        camBadge.className = "brand-badge";
                        camBadge.innerText = "虚拟摄像头就绪";
                    }
                }
            }
        } catch (e) {}
    } catch (e) { /* 静默轮询 */ }
}

function formatDuration(sec) {
    if (!sec || sec <= 0) return "00:00:00";
    const h = String(Math.floor(sec / 3600)).padStart(2, "0");
    const m = String(Math.floor((sec % 3600) / 60)).padStart(2, "0");
    const s = String(sec % 60).padStart(2, "0");
    return `${h}:${m}:${s}`;
}

async function registerOrder() {
    const amount = parseFloat(document.getElementById("order-amount").value);
    const sku = document.getElementById("order-sku").value.trim();
    if (!amount || amount <= 0) { alert("请输入有效成交金额"); return; }
    if (!sku) { alert("请输入已上架商品的 SKU"); return; }
    try {
        const res = await fetch(`${API_BASE}/live/stats/order`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ amount, sku })
        });
        const json = await res.json();
        if (json.code === 0) {
            document.getElementById("order-amount").value = "";
            document.getElementById("order-sku").value = "";
            logDanmaku(`${svg("dollar", "icon-sm")} 成交登记`, json.message, true, true);
            loadLiveStats();
        } else {
            alert(json.detail || json.message || "登记失败");
        }
    } catch (e) { alert("登记异常: " + e); }
}

// ============================================================================
// 知识库管理与 RAG 语义检索交互逻辑
// ============================================================================

