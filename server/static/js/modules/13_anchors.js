let _loadingAnchorsPromise = null;

async function loadAnchors() {
    // 1. [缓存优先秒开] 若内存中已有主播列表，0ms 瞬间渲染出表格，杜绝任何白屏等待
    if (Array.isArray(anchorCache) && anchorCache.length > 0) {
        renderAnchorsTable(anchorCache, window._currentLiveAnchorId || "");
    }

    // 2. [防抖去重] 若当前已有正在飞行的拉取请求，直接复用同一个 Promise，杜绝重复并发
    if (_loadingAnchorsPromise) {
        return _loadingAnchorsPromise;
    }

    _loadingAnchorsPromise = (async () => {
        try {
            // 3. [纯本地数据库直出] 单次请求直出全部主播数据、音色中文名与当前开播状态，零远程依赖，< 5ms 极速返回
            const res = await fetch(`${API_BASE}/anchors/list`);
            const json = await res.json();

            if (json.code === 0 && Array.isArray(json.data)) {
                anchorCache = json.data;
                const currentAnchorId = json.selected_anchor_id || window._currentLiveAnchorId || "";
                window._currentLiveAnchorId = currentAnchorId;

                // 4. 本地数据到达，立即毫秒级更新渲染表格
                renderAnchorsTable(anchorCache, currentAnchorId);
                // 同步数字人资产工场的主播下拉框
                syncAvatarTaskAnchorSelect(anchorCache);
                syncAvatarActionAnchorSelect(anchorCache);
            }

            // 5. [后台静默填充] 仅为上方表单的“绑定音色”下拉框提供选项，绝不阻塞主播表格展示
            syncAnchorVoiceOptions();
            // 6. 加载数字人视频切片任务列表
            loadAvatarTasks();
            // 7. 加载动作视频状态机与电商带货场景智能绑定配置
            loadAvatarActions();
        } catch (e) {
            console.error("加载主播失败", e);
        } finally {
            _loadingAnchorsPromise = null;
        }
    })();

    return _loadingAnchorsPromise;
}

// 独立的快速表格渲染函数 (纯本地数据计算，< 2ms 渲染完成)
function renderAnchorsTable(anchors, currentAnchorId) {
    const tbody = document.getElementById("anchors-tbody");
    if (!tbody) return;
    tbody.innerHTML = "";
    if (!anchors || anchors.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; color:var(--text-muted); padding: 20px;">暂无主播档案，请在上方创建</td></tr>';
        return;
    }

    const ANCHOR_TYPE_MAP = {
        ecommerce: {
            label: "带货主播",
            sub: "促单逼单",
            icon: "🛍️",
            style: "background-color: rgba(16, 185, 129, 0.14) !important; color: #34d399 !important; border: 1px solid rgba(16, 185, 129, 0.38) !important;",
            chevron: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%2334d399' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E"
        },
        entertainment: {
            label: "娱乐主播",
            sub: "逗梗陪伴",
            icon: "🎭",
            style: "background-color: rgba(236, 72, 153, 0.14) !important; color: #f472b6 !important; border: 1px solid rgba(236, 72, 153, 0.38) !important;",
            chevron: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23f472b6' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E"
        },
        expert: {
            label: "专业专家",
            sub: "前置免责",
            icon: "⚖️",
            style: "background-color: rgba(56, 189, 248, 0.14) !important; color: #38bdf8 !important; border: 1px solid rgba(56, 189, 248, 0.38) !important;",
            chevron: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%2338bdf8' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E"
        },
        chat: {
            label: "闲聊扯淡",
            sub: "唠嗑搭子",
            icon: "☕",
            style: "background-color: rgba(251, 146, 60, 0.14) !important; color: #fb923c !important; border: 1px solid rgba(251, 146, 60, 0.38) !important;",
            chevron: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23fb923c' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E"
        }
    };

    anchors.forEach(a => {
        // 直接使用本地数据库联表直出的音色中文名称，无需等待前端额外拉取音色库匹配
        const voiceDisplayName = a.voice_name || (voiceCache && voiceCache.find(v => v.id === a.voice_id)?.name) || (a.voice_id ? a.voice_id : "未绑定");
        const voiceName = escapeHtml(voiceDisplayName);
        const portrait = a.photos && a.photos.portrait;
        const photoCount = Object.values(a.photos || {}).filter(p => p).length;
        const thumb = portrait
            ? `<img src="/static-file?path=${encodeURIComponent(portrait)}" style="width: 38px; height: 38px; object-fit: cover; border-radius: 50%; border: 1px solid var(--border-subtle); vertical-align: middle; margin-right: 6px;" onerror="this.src='/static/svg/default_avatar.svg'"> `
            : `<img src="/static/svg/default_avatar.svg" style="width: 38px; height: 38px; object-fit: cover; border-radius: 50%; border: 1px solid var(--border-subtle); vertical-align: middle; margin-right: 6px;"> `;
        const isCurrent = a.is_current_live || (a.id === currentAnchorId);
        const currentTag = isCurrent
            ? '<span class="badge-recommend" style="font-size: 10px; padding: 2px 8px; margin-left: 6px;">● 开播主播</span>'
            : "";

        const typeCfg = ANCHOR_TYPE_MAP[a.anchor_type] || ANCHOR_TYPE_MAP.ecommerce;
        const currentType = a.anchor_type || "ecommerce";

        const typeSelectorHtml = `
            <select class="anchor-table-type-select" 
                    onchange="quickChangeAnchorType('${a.id}', this.value, '${escapeHtml(a.name)}')" 
                    title="点击可直接快速切换主播人设定位与话术风格 (实时保存)" 
                    style="${typeCfg.style} background-image: url('${typeCfg.chevron}');">
                <option value="ecommerce" ${currentType === "ecommerce" ? "selected" : ""}>🛍️ 带货主播（促单逼单）</option>
                <option value="entertainment" ${currentType === "entertainment" ? "selected" : ""}>🎭 娱乐主播（逗梗陪伴）</option>
                <option value="expert" ${currentType === "expert" ? "selected" : ""}>⚖️ 专业专家（咨询法理前置免责）</option>
                <option value="chat" ${currentType === "chat" ? "selected" : ""}>☕ 闲聊扯淡（唠嗑搭子）</option>
            </select>
        `;

        const hasTrainedAvatar = Boolean(a.avatar_asset_dir);
        const avatarBadge = hasTrainedAvatar
            ? '<span class="badge-recommend" style="font-size:10px; padding:1px 6px; background:rgba(16,185,129,0.15); color:#34d399; border:1px solid rgba(16,185,129,0.3); margin-left:4px;" title="已绑定切片视频模型资产">🎬 已切片</span>'
            : '';

        const tr = document.createElement("tr");
        tr.innerHTML = `
            <td style="font-weight: 600; white-space: nowrap;">${thumb}${escapeHtml(a.name)}${currentTag}</td>
            <td style="white-space: nowrap;">${typeSelectorHtml}</td>
            <td id="anchor-row-voice-${a.id}">${voiceName}</td>
            <td>${photoCount}/4 张${avatarBadge}</td>
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
}

// 独立的音色下拉选项后台异步同步器 (仅用于上方表单的音色下拉选择，绝不阻塞主播表格展示)
async function syncAnchorVoiceOptions() {
    try {
        if (!voiceCache || voiceCache.length === 0) {
            const vRes = await fetch(`${API_BASE}/voices/list`);
            const vJson = await vRes.json();
            if (vJson.code === 0) {
                voiceCache = vJson.data || [];
                window._voiceProfilesCache = voiceCache;
            }
        }
        if (typeof populateAnchorVoiceSelect === "function") {
            await populateAnchorVoiceSelect(voiceCache);
        }
    } catch (e) {
        console.warn("后台同步主播音色下拉失败", e);
    }
}

async function quickChangeAnchorType(anchorId, newType, anchorName) {
    try {
        const fd = new FormData();
        fd.append("id", anchorId);
        fd.append("anchor_type", newType);
        const res = await fetch(`${API_BASE}/anchors/update`, { method: "POST", body: fd });
        const json = await res.json();
        if (json.code === 0) {
            const typeLabels = {
                ecommerce: "🛍️ 带货主播（促单逼单）",
                entertainment: "🎭 娱乐主播（逗梗陪伴）",
                expert: "⚖️ 专业专家（咨询法理前置免责）",
                chat: "☕ 闲聊扯淡（唠嗑搭子）"
            };
            const typeLabel = typeLabels[newType] || newType;
            showToast(`已将主播【${anchorName}】类型调整为：${typeLabel} ✓`, "success");
            // 同步更新本地缓存
            const item = anchorCache.find(x => x.id === anchorId);
            if (item) item.anchor_type = newType;
            // 若上方正在编辑该主播，同步更新表单中的选择框
            const editId = document.getElementById("anchor-edit-id").value;
            if (editId === anchorId) {
                const formTypeEl = document.getElementById("anchor-type");
                if (formTypeEl) formTypeEl.value = newType;
            }
            loadAnchors();
        } else {
            alert("修改主播类型失败: " + (json.detail || json.message));
            loadAnchors();
        }
    } catch (e) {
        alert("网络或接口异常: " + e);
        loadAnchors();
    }
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

    const anchorTypeEl = document.getElementById("anchor-type");
    const anchorType = anchorTypeEl ? anchorTypeEl.value : "ecommerce";

    const fd = new FormData();
    if (editId) fd.append("id", editId);
    fd.append("name", name);
    fd.append("anchor_type", anchorType);
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
            showToast(editId ? `主播【${name}】资料已更新 ✓` : `主播【${name}】档案已创建 ✓`, "success");
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

    // 回显主播类型
    const anchorTypeEl = document.getElementById("anchor-type");
    if (anchorTypeEl) {
        anchorTypeEl.value = a.anchor_type || "ecommerce";
    }

    // 选中绑定的音色
    const voiceSelect = document.getElementById("anchor-voice");
    if (voiceSelect) {
        if (a.voice_id) {
            let opt = voiceSelect.querySelector(`option[value="${a.voice_id}"]`);
            if (!opt) {
                // 如果当前引擎过滤列表中未包含该音色（如属于其它引擎或已下线音色），追加并选中
                const vMatch = (voiceCache || []).find(v => v.id === a.voice_id);
                const optName = a.voice_name || (vMatch ? `${vMatch.name} (${vMatch.provider_name || '其他引擎'})` : a.voice_id);
                opt = document.createElement("option");
                opt.value = a.voice_id;
                opt.innerText = `${optName} [当前绑定/其他引擎]`;
                voiceSelect.appendChild(opt);
            }
            voiceSelect.value = a.voice_id;
        } else {
            voiceSelect.value = "";
        }
    }

    document.getElementById("anchor-remark").value = a.remark || "";
    document.getElementById("anchor-edit-hint").innerText = `(正在编辑: ${a.name})`;
    document.getElementById("anchor-reset-btn").style.display = "inline-block";

    // 平滑滚动并高亮提示
    const nameInput = document.getElementById("anchor-name");
    if (nameInput) {
        nameInput.scrollIntoView({ behavior: "smooth", block: "center" });
        nameInput.focus();
    }
}

function resetAnchorForm() {
    document.getElementById("anchor-edit-id").value = "";
    document.getElementById("anchor-name").value = "";
    const anchorTypeEl = document.getElementById("anchor-type");
    if (anchorTypeEl) {
        anchorTypeEl.value = "ecommerce";
    }
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
// 数字人视频切片与训练工作台 (Phase 2 Avatar Task Management)
// ============================================================================
let _activeAvatarTaskId = null;
let _avatarTaskPollingTimer = null;
let _avatarTasksCache = [];

function syncAvatarTaskAnchorSelect(anchors) {
    const sel = document.getElementById("avatar-task-anchor");
    if (!sel) return;
    const currentVal = sel.value;
    sel.innerHTML = '<option value="">-- 暂不绑定 (仅生成通用模型资产) --</option>';
    if (Array.isArray(anchors)) {
        anchors.forEach(a => {
            const opt = document.createElement("option");
            opt.value = a.id;
            opt.innerText = `${a.name} (${a.anchor_type || '带货主播'})`;
            sel.appendChild(opt);
        });
    }
    if (currentVal) sel.value = currentVal;
}

async function loadAvatarTasks() {
    const tbody = document.getElementById("avatar-tasks-tbody");
    if (!tbody) return;
    try {
        const res = await fetch(`${API_BASE}/anchors/avatar/tasks`);
        const json = await res.json();
        if (json.code === 0 && Array.isArray(json.data)) {
            _avatarTasksCache = json.data;
            renderAvatarTasksTable(_avatarTasksCache);

            // 如果有正在运行的任务且未在轮询，自动拉起轮询
            const runningTask = _avatarTasksCache.find(t => t.status === "processing" || t.status === "pending");
            if (runningTask && !_avatarTaskPollingTimer) {
                _activeAvatarTaskId = runningTask.id;
                startAvatarTaskPolling(runningTask.id);
            }
        }
    } catch (e) {
        console.warn("加载数字人切片任务失败", e);
    }
}

function renderAvatarTasksTable(tasks) {
    const tbody = document.getElementById("avatar-tasks-tbody");
    if (!tbody) return;
    tbody.innerHTML = "";
    if (!tasks || tasks.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" style="text-align:center; color:var(--text-muted); padding: 16px;">暂无数字人训练任务，可在上方上传真人视频开始制作</td></tr>';
        return;
    }

    const STATUS_MAP = {
        completed: { text: "已就绪 (100%)", badge: "background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3);" },
        processing: { text: "训练切片中", badge: "background: rgba(56, 189, 248, 0.15); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.3);" },
        pending: { text: "排队等待中", badge: "background: rgba(251, 146, 60, 0.15); color: #fb923c; border: 1px solid rgba(251, 146, 60, 0.3);" },
        failed: { text: "训练失败", badge: "background: rgba(239, 68, 68, 0.15); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3);" },
        cancelled: { text: "已取消", badge: "background: rgba(156, 163, 175, 0.15); color: #9ca3af; border: 1px solid rgba(156, 163, 175, 0.3);" },
    };

    tasks.forEach(t => {
        const st = STATUS_MAP[t.status] || STATUS_MAP.pending;
        const thumbHtml = t.preview_path
            ? `<img src="/static-file?path=${encodeURIComponent(t.preview_path)}" style="width: 38px; height: 38px; object-fit: cover; border-radius: 4px; border: 1px solid var(--border-subtle);" onerror="this.src='/static/svg/default_avatar.svg'">`
            : `<div style="width:38px; height:38px; border-radius:4px; background:rgba(255,255,255,0.05); display:flex; align-items:center; justify-content:center; font-size:11px; color:var(--text-muted);">无图</div>`;

        // 查找关联主播名称
        let anchorName = "通用资产 (未绑定)";
        if (t.anchor_id && Array.isArray(anchorCache)) {
            const m = anchorCache.find(a => a.id === t.anchor_id);
            if (m) anchorName = `${m.name}`;
            else anchorName = t.anchor_id;
        }

        const framesText = (t.meta && t.meta.frame_count) ? `${t.meta.frame_count} 帧` : "-";
        const timeText = t.created_at ? t.created_at.substring(0, 19).replace("T", " ") : "-";

        let actionHtml = "";
        if (t.status === "completed") {
            actionHtml = `
                <div class="table-actions">
                    <button class="btn btn-sm btn-primary" onclick="applyAvatarTaskToAnchorPrompt('${t.id}', '${escapeHtml(t.name)}')">应用至主播</button>
                    <button class="btn btn-sm btn-danger" onclick="deleteAvatarTask('${t.id}', '${escapeHtml(t.name)}')">删除</button>
                </div>
            `;
        } else if (t.status === "processing" || t.status === "pending") {
            actionHtml = `
                <div class="table-actions">
                    <button class="btn btn-sm btn-danger" onclick="cancelAvatarTaskById('${t.id}')">取消</button>
                </div>
            `;
        } else {
            actionHtml = `
                <div class="table-actions">
                    <button class="btn btn-sm btn-danger" onclick="deleteAvatarTask('${t.id}', '${escapeHtml(t.name)}')">删除</button>
                </div>
            `;
        }

        const tr = document.createElement("tr");
        tr.innerHTML = `
            <td>${thumbHtml}</td>
            <td style="font-weight: 600;">${escapeHtml(t.name)}</td>
            <td>${escapeHtml(anchorName)}</td>
            <td>${framesText}</td>
            <td><span class="badge-recommend" style="font-size:11px; padding:2px 8px; ${st.badge}">${st.text}${t.status === 'processing' ? ` (${t.progress}%)` : ''}</span></td>
            <td style="font-size: 12px; color: var(--text-muted);">${timeText}</td>
            <td style="white-space: nowrap;">${actionHtml}</td>
        `;
        tbody.appendChild(tr);
    });
}

async function submitAvatarTask() {
    const nameInput = document.getElementById("avatar-task-name");
    const anchorSelect = document.getElementById("avatar-task-anchor");
    const videoInput = document.getElementById("avatar-task-video");
    const hint = document.getElementById("avatar-task-hint");
    const submitBtn = document.getElementById("btn-submit-avatar-task");

    const name = nameInput ? nameInput.value.trim() : "";
    if (!name) {
        alert("请输入数字人资产名称");
        if (nameInput) nameInput.focus();
        return;
    }

    if (!videoInput || !videoInput.files || videoInput.files.length === 0) {
        alert("请选择要上传制作切片的真人说话视频文件 (MP4/MOV)");
        return;
    }

    const file = videoInput.files[0];
    const anchorId = anchorSelect ? anchorSelect.value : "";

    const fd = new FormData();
    fd.append("name", name);
    fd.append("file", file);
    if (anchorId) fd.append("anchor_id", anchorId);

    try {
        if (submitBtn) submitBtn.disabled = true;
        if (hint) hint.innerText = "正在上传视频并初始化后台切片流水线...";

        const res = await fetch(`${API_BASE}/anchors/avatar/task`, {
            method: "POST",
            body: fd,
        });
        const json = await res.json();
        if (json.code === 0 && json.data && json.data.task_id) {
            showToast(`数字人切片任务【${name}】已提交，后台开始流水线制作 ✓`, "success");
            if (videoInput) videoInput.value = "";
            if (nameInput) nameInput.value = "";
            if (hint) hint.innerText = "";

            _activeAvatarTaskId = json.data.task_id;
            startAvatarTaskPolling(json.data.task_id);
            loadAvatarTasks();
        } else {
            alert("提交任务失败: " + (json.detail || json.message || "未知错误"));
            if (hint) hint.innerText = "";
        }
    } catch (e) {
        alert("请求异常: " + e);
        if (hint) hint.innerText = "";
    } finally {
        if (submitBtn) submitBtn.disabled = false;
    }
}

function startAvatarTaskPolling(taskId) {
    if (_avatarTaskPollingTimer) {
        clearInterval(_avatarTaskPollingTimer);
        _avatarTaskPollingTimer = null;
    }

    const box = document.getElementById("avatar-task-active-box");
    if (box) box.style.display = "block";

    const poll = async () => {
        try {
            const res = await fetch(`${API_BASE}/anchors/avatar/tasks/${taskId}`);
            if (!res.ok) return;
            const json = await res.json();
            if (json.code !== 0 || !json.data) return;

            const t = json.data;
            const percentEl = document.getElementById("active-task-percent");
            const barEl = document.getElementById("active-task-bar");
            const titleEl = document.getElementById("active-task-title");
            const stageEl = document.getElementById("active-task-stage");

            if (titleEl) titleEl.innerText = `正在处理: ${t.name}`;
            if (percentEl) percentEl.innerText = `${t.progress}%`;
            if (barEl) barEl.style.width = `${t.progress}%`;
            if (stageEl) stageEl.innerText = t.stage_message || "切片提取中...";

            if (t.status === "completed") {
                clearInterval(_avatarTaskPollingTimer);
                _avatarTaskPollingTimer = null;
                showToast(`🎬 数字人资产【${t.name}】切片与特征提取完成！已就绪开播 ✓`, "success");
                loadAvatarTasks();
                loadAnchors();
                setTimeout(() => {
                    if (box) box.style.display = "none";
                }, 3000);
            } else if (t.status === "failed" || t.status === "cancelled") {
                clearInterval(_avatarTaskPollingTimer);
                _avatarTaskPollingTimer = null;
                if (t.status === "failed") {
                    showToast(`数字人制作失败: ${t.error_message || '未知异常'}`, "danger");
                }
                loadAvatarTasks();
                setTimeout(() => {
                    if (box) box.style.display = "none";
                }, 3000);
            }
        } catch (e) {
            console.warn("轮询切片任务进度异常", e);
        }
    };

    poll();
    _avatarTaskPollingTimer = setInterval(poll, 1200);
}

async function cancelActiveAvatarTask() {
    if (!_activeAvatarTaskId) return;
    if (!confirm("确定取消当前正在执行的切片任务吗？")) return;
    await cancelAvatarTaskById(_activeAvatarTaskId);
}

async function cancelAvatarTaskById(taskId) {
    try {
        const res = await fetch(`${API_BASE}/anchors/avatar/tasks/${taskId}/cancel`, {
            method: "POST"
        });
        const json = await res.json();
        if (json.code === 0) {
            showToast("已成功取消切片训练任务", "info");
            loadAvatarTasks();
            const box = document.getElementById("avatar-task-active-box");
            if (box && _activeAvatarTaskId === taskId) {
                box.style.display = "none";
                if (_avatarTaskPollingTimer) clearInterval(_avatarTaskPollingTimer);
            }
        } else {
            alert("取消失败: " + (json.detail || json.message));
        }
    } catch (e) {
        alert("取消异常: " + e);
    }
}

async function applyAvatarTaskToAnchorPrompt(taskId, taskName) {
    if (!Array.isArray(anchorCache) || anchorCache.length === 0) {
        alert("当前尚无主播档案，请先在上方创建主播后再绑定");
        return;
    }

    let msg = `请选择要将数字人模型【${taskName}】绑定至哪位主播：\n\n`;
    anchorCache.forEach((a, idx) => {
        msg += `${idx + 1}. ${a.name} (ID: ${a.id})\n`;
    });
    msg += `\n请输入序号 (1-${anchorCache.length})：`;

    const choice = prompt(msg, "1");
    if (!choice) return;
    const index = parseInt(choice, 10) - 1;
    if (isNaN(index) || index < 0 || index >= anchorCache.length) {
        alert("输入序号无效");
        return;
    }

    const targetAnchor = anchorCache[index];
    try {
        const res = await fetch(`${API_BASE}/anchors/avatar/tasks/${taskId}/apply`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ anchor_id: targetAnchor.id }),
        });
        const json = await res.json();
        if (json.code === 0) {
            showToast(`已成功将数字人模型绑定至主播【${targetAnchor.name}】✓`, "success");
            loadAnchors();
            loadAvatarTasks();
        } else {
            alert("绑定失败: " + (json.detail || json.message));
        }
    } catch (e) {
        alert("绑定异常: " + e);
    }
}

async function deleteAvatarTask(taskId, name) {
    if (!confirm(`确认删除数字人切片任务【${name}】及其磁盘缓存产物？`)) return;
    try {
        const res = await fetch(`${API_BASE}/anchors/avatar/tasks/${taskId}`, {
            method: "DELETE"
        });
        const json = await res.json();
        if (json.code === 0) {
            showToast("数字人切片任务已删除 ✓", "success");
            loadAvatarTasks();
        } else {
            alert("删除失败: " + (json.detail || json.message));
        }
    } catch (e) {
        alert("删除异常: " + e);
    }
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
// 🎭 阶段三：数字人动作状态机与电商带货场景智能联动 (Action State Machine)
// ============================================================================
let _avatarActionsCache = [];
let _pendingUploadActionId = null;

function syncAvatarActionAnchorSelect(anchors) {
    const sel = document.getElementById("edit-action-anchor");
    if (!sel) return;
    const oldVal = sel.value;
    sel.innerHTML = '<option value="">-- 全局通用 (所有主播生效) --</option>';
    if (Array.isArray(anchors)) {
        anchors.forEach(a => {
            sel.innerHTML += `<option value="${escapeHtml(a.id)}">${escapeHtml(a.name)}</option>`;
        });
    }
    if (oldVal) sel.value = oldVal;
}

async function loadAvatarActions() {
    try {
        const res = await fetch(`${API_BASE}/anchors/avatar/actions`);
        const json = await res.json();
        if (json.code === 0 && Array.isArray(json.data)) {
            _avatarActionsCache = json.data;
            renderAvatarActionsTable(_avatarActionsCache, json.current_status);
        }
    } catch (e) {
        console.error("加载动作状态机配置失败:", e);
        const tbody = document.getElementById("avatar-actions-tbody");
        if (tbody) {
            tbody.innerHTML = '<tr><td colspan="8" style="text-align:center; color:var(--red); padding:16px;">加载动作配置失败</td></tr>';
        }
    }
}

function renderAvatarActionsTable(actions, currentStatus) {
    const tbody = document.getElementById("avatar-actions-tbody");
    if (!tbody) return;

    // 1. 更新遥测条
    if (currentStatus) {
        const curEl = document.getElementById("action-telemetry-current");
        const prioEl = document.getElementById("action-telemetry-priority");
        const cdEl = document.getElementById("action-telemetry-countdown");
        if (curEl) {
            const curName = currentStatus.action_name || (currentStatus.action_code === 0 ? "待机呼吸" : `动作 ${currentStatus.action_code}`);
            curEl.innerText = `Action ${currentStatus.action_code}: ${curName}`;
            curEl.style.color = currentStatus.action_code === 0 ? "#34d399" : "#fbbf24";
        }
        if (prioEl) {
            prioEl.innerText = `P${currentStatus.priority || 0}`;
        }
        if (cdEl) {
            if (currentStatus.action_code === 0) {
                cdEl.innerText = "循环常驻";
                cdEl.style.color = "var(--text-primary)";
            } else {
                const rem = typeof currentStatus.remaining_seconds === "number" ? currentStatus.remaining_seconds.toFixed(1) : "0.0";
                cdEl.innerText = `${rem} 秒后复位`;
                cdEl.style.color = "#34d399";
            }
        }
    }

    // 2. 渲染表格
    tbody.innerHTML = "";
    if (!actions || actions.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" style="text-align:center; color:var(--text-muted); padding:16px;">暂无动作配置</td></tr>';
        return;
    }

    const TRIGGER_TYPE_MAP = {
        "both": '<span class="badge-recommend" style="font-size:10px; background:rgba(59,130,246,0.15); color:#60a5fa; border:1px solid rgba(59,130,246,0.3);">双轨驱动</span>',
        "keyword": '<span class="badge-recommend" style="font-size:10px; background:rgba(16,185,129,0.15); color:#34d399; border:1px solid rgba(16,185,129,0.3);">话术关键词</span>',
        "event": '<span class="badge-recommend" style="font-size:10px; background:rgba(245,158,11,0.15); color:#fbbf24; border:1px solid rgba(245,158,11,0.3);">实时场控</span>',
        "manual": '<span class="badge-recommend" style="font-size:10px; background:rgba(156,163,175,0.15); color:#9ca3af; border:1px solid rgba(156,163,175,0.3);">手动触发</span>',
    };

    const EVENT_NAME_MAP = {
        "welcome": "进房欢迎 (welcome)",
        "follow": "点赞关注 (follow)",
        "order": "下单成交 (order)",
        "gift": "礼物致谢 (gift)",
    };

    actions.forEach(act => {
        const tr = document.createElement("tr");
        const isCurrent = currentStatus && currentStatus.action_code === act.action_code;
        if (isCurrent) {
            tr.style.backgroundColor = "rgba(245, 158, 11, 0.08)";
        }

        const typeBadge = TRIGGER_TYPE_MAP[act.trigger_type] || act.trigger_type;
        const kwText = act.trigger_keywords ? `<div style="font-size:11px; color:var(--text-muted); margin-top:2px;">匹配: ${escapeHtml(act.trigger_keywords)}</div>` : '';
        const evtText = act.trigger_events ? (EVENT_NAME_MAP[act.trigger_events] || act.trigger_events) : '<span style="color:var(--text-muted); font-size:11px;">未绑定</span>';

        const framesBadge = act.frames_count > 0
            ? `<span class="badge-recommend" style="font-size:11px; background:rgba(16,185,129,0.15); color:#34d399; border:1px solid rgba(16,185,129,0.3);">🎬 ${act.frames_count} 帧就绪</span>`
            : `<span style="font-size:11px; color:var(--text-muted);">未提取视频</span>`;

        const anchorLabel = act.anchor_id ? `<span style="font-size:11px; color:#38bdf8;">专用主播</span>` : `<span style="font-size:11px; color:var(--text-muted);">全局通用</span>`;

        tr.innerHTML = `
            <td>
                <span style="font-weight:700; font-size:13px; color:${act.action_code === 0 ? '#34d399' : '#fbbf24'};">
                    #${act.action_code}
                </span>
                ${isCurrent ? '<span style="display:inline-block; width:6px; height:6px; border-radius:50%; background:#10b981; margin-left:4px;" title="当前运行中"></span>' : ''}
            </td>
            <td>
                <div style="font-weight:600; font-size:13px;">${escapeHtml(act.action_name)}</div>
                <div style="font-size:10px; color:var(--text-muted);">${act.mirror_loop ? '🔁 镜像循环' : '单向循环'}</div>
            </td>
            <td>${anchorLabel}</td>
            <td>
                <div>${typeBadge}</div>
                ${kwText}
            </td>
            <td><span style="font-size:12px; font-weight:500;">${evtText}</span></td>
            <td>
                <div style="font-size:12px; font-weight:600;">${act.duration_sec}s</div>
                <div style="font-size:10px; color:#fbbf24;">优先级 P${act.priority}</div>
            </td>
            <td>${framesBadge}</td>
            <td>
                <div style="display:flex; gap:6px; flex-wrap:wrap;">
                    <button class="btn btn-sm btn-primary" style="padding:2px 8px; font-size:11px;" onclick="testTriggerAvatarAction(${act.action_code})" title="手动触发测试状态机切换">
                        ▶ 测试
                    </button>
                    <button class="btn btn-sm" style="padding:2px 8px; font-size:11px;" onclick="triggerUploadActionClip('${act.id}')" title="上传高清切片短视频并抽帧">
                        🎬 视频
                    </button>
                    <button class="btn btn-sm" style="padding:2px 8px; font-size:11px;" onclick="openActionEditCard('${act.id}')" title="编辑动作参数与关键词">
                        ✏ 编辑
                    </button>
                    ${act.action_code !== 0 ? `<button class="btn btn-sm btn-danger" style="padding:2px 8px; font-size:11px;" onclick="deleteAvatarAction('${act.id}')" title="删除动作">🗑</button>` : ''}
                </div>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

function openActionEditCard(actionId) {
    const card = document.getElementById("action-edit-card");
    if (!card) return;
    const act = _avatarActionsCache.find(a => a.id === actionId);
    if (!act) return;

    document.getElementById("action-edit-title").innerText = `编辑动作槽位: #${act.action_code} - ${act.action_name}`;
    document.getElementById("edit-action-id").value = act.id;
    document.getElementById("edit-action-code").value = act.action_code;
    document.getElementById("edit-action-code").disabled = (act.action_code === 0);
    document.getElementById("edit-action-name").value = act.action_name;
    document.getElementById("edit-action-anchor").value = act.anchor_id || "";
    document.getElementById("edit-action-trigger-type").value = act.trigger_type || "both";
    document.getElementById("edit-action-duration").value = act.duration_sec;
    document.getElementById("edit-action-priority").value = act.priority;
    document.getElementById("edit-action-keywords").value = act.trigger_keywords || "";
    document.getElementById("edit-action-events").value = act.trigger_events || "";
    document.getElementById("edit-action-mirror").checked = !!act.mirror_loop;
    document.getElementById("edit-action-active").checked = !!act.is_active;

    card.style.display = "block";
    card.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function closeActionEditCard() {
    const card = document.getElementById("action-edit-card");
    if (card) card.style.display = "none";
}

async function submitSaveAvatarAction() {
    const id = document.getElementById("edit-action-id").value;
    const action_code = parseInt(document.getElementById("edit-action-code").value, 10);
    const action_name = document.getElementById("edit-action-name").value.trim();
    if (isNaN(action_code) || action_code < 0) {
        alert("请输入有效的动作槽位编号 (>= 0)");
        return;
    }
    if (!action_name) {
        alert("动作名称不可为空");
        return;
    }

    const payload = {
        id: id || undefined,
        action_code: action_code,
        action_name: action_name,
        anchor_id: document.getElementById("edit-action-anchor").value || null,
        trigger_type: document.getElementById("edit-action-trigger-type").value,
        duration_sec: parseFloat(document.getElementById("edit-action-duration").value) || 3.5,
        priority: parseInt(document.getElementById("edit-action-priority").value, 10) || 1,
        trigger_keywords: document.getElementById("edit-action-keywords").value.trim(),
        trigger_events: document.getElementById("edit-action-events").value.trim(),
        mirror_loop: document.getElementById("edit-action-mirror").checked ? 1 : 0,
        is_active: document.getElementById("edit-action-active").checked ? 1 : 0,
    };

    try {
        const res = await fetch(`${API_BASE}/anchors/avatar/actions`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const json = await res.json();
        if (json.code === 0) {
            closeActionEditCard();
            loadAvatarActions();
        } else {
            alert(json.detail || json.message || "保存失败");
        }
    } catch (e) {
        alert("保存动作配置异常: " + e);
    }
}

function triggerUploadActionClip(actionId) {
    _pendingUploadActionId = actionId;
    const uploader = document.getElementById("action-clip-uploader");
    if (uploader) {
        uploader.value = "";
        uploader.click();
    }
}

async function handleActionClipFileSelected(input) {
    if (!input.files || input.files.length === 0 || !_pendingUploadActionId) return;
    const file = input.files[0];
    const actId = _pendingUploadActionId;
    _pendingUploadActionId = null;

    const fd = new FormData();
    fd.append("file", file);

    const btn = document.activeElement;
    if (btn && btn.tagName === "BUTTON") {
        btn.disabled = true;
        btn.innerText = "上传中...";
    }

    try {
        const res = await fetch(`${API_BASE}/anchors/avatar/actions/${actId}/upload-clip`, {
            method: "POST",
            body: fd,
        });
        const json = await res.json();
        if (json.code === 0) {
            alert(json.message || "切片视频已成功解析并加载！");
            loadAvatarActions();
        } else {
            alert(json.detail || json.message || "上传解析失败");
        }
    } catch (e) {
        alert("上传切片视频异常: " + e);
    } finally {
        if (btn && btn.tagName === "BUTTON") {
            btn.disabled = false;
            btn.innerText = "🎬 视频";
        }
    }
}

async function testTriggerAvatarAction(actionCode) {
    try {
        const res = await fetch(`${API_BASE}/anchors/avatar/actions/test-trigger`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action_code: actionCode }),
        });
        const json = await res.json();
        if (json.code === 0) {
            renderAvatarActionsTable(_avatarActionsCache, json.current_status);
        } else {
            alert(json.message || "抢占被忽略");
        }
    } catch (e) {
        console.error("调试触发动作异常:", e);
    }
}

async function deleteAvatarAction(actionId) {
    if (!confirm("确定要删除该动作配置及已关联的切片视频吗？")) return;
    try {
        const res = await fetch(`${API_BASE}/anchors/avatar/actions/${actionId}`, {
            method: "DELETE",
        });
        const json = await res.json();
        if (json.code === 0) {
            loadAvatarActions();
        } else {
            alert(json.detail || json.message || "删除失败");
        }
    } catch (e) {
        alert("删除动作失败: " + e);
    }
}

