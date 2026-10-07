// FAQ 问答卡片下拉展开/收起切换
function toggleFaqAnswer(id, btn) {
    const el = document.getElementById(id);
    if (!el) return;
    const isHidden = (el.style.display === "none" || !el.style.display);
    el.style.display = isHidden ? "flex" : "none";
    if (btn) {
        btn.innerText = isHidden ? "收起说明 ▴" : "查看说明 ▾";
    }
}

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
            // 8. 加载数字人软硬件达标体检看板
            loadDigitalHumanHardwareRequirements();
            // 9. 加载形象资产记录 (训练产出登记簿，可清理冗余登记)
            loadAvatarAssets();
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
        let statusBadgeHtml = '<span style="font-size:11px; color:var(--text-muted);">⚪ 待制作数字人</span>';
        let actionButtonsHtml = '';

        if (hasTrainedAvatar) {
            const meta = a.avatar_meta || {};
            const frameInfo = meta.frame_count ? `${meta.frame_count}帧 · ${meta.width || 1280}x${meta.height || 720}` : '25 FPS模型就绪';
            statusBadgeHtml = `
                <div style="display: flex; flex-direction: column; gap: 4px; white-space: nowrap;">
                    <div style="display: flex; align-items: center; gap: 6px; flex-wrap: nowrap; white-space: nowrap;">
                        <span class="badge-recommend" style="font-size: 11.5px; padding: 2px 8px; background: rgba(16,185,129,0.15); color: #34d399; border: 1px solid rgba(16,185,129,0.3); white-space: nowrap; flex-shrink: 0;" title="已关联连续帧切片、面部关键点与口型坐标资产">🎬 视频数字人 (就绪)</span>
                        <button class="btn btn-sm" style="background: rgba(56, 189, 248, 0.12); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.35); padding: 2px 8px; font-size: 11.5px; white-space: nowrap; flex-shrink: 0;" onclick="openAvatarPreviewModal('${a.id}')" title="预览该数字人的动态母轨视频与切片关键指标">👀 预览</button>
                        <button class="btn btn-sm" style="background: rgba(245, 158, 11, 0.12); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.35); padding: 2px 8px; font-size: 11.5px; white-space: nowrap; flex-shrink: 0;" onclick="openAnchorActionsModal('${a.id}', '${escapeHtml(a.name)}')" title="配置该主播的小黄车/致谢/欢迎手势与动作切片">🎭 动作切片</button>
                    </div>
                    <div style="display: flex; align-items: center; gap: 8px; font-size: 11px; white-space: nowrap; line-height: 1.4;">
                        <span style="color: var(--text-muted); font-family: monospace;">${frameInfo}</span>
                        <span style="color: #38bdf8;" title="开播需 GPU >= 4GB 显存或远端租赁 GPU 协同加速">⚡ 开播驱动需 GPU / 云端算力</span>
                    </div>
                </div>
            `;
            actionButtonsHtml = `
                <button class="btn btn-sm" onclick="editAnchor('${a.id}')" title="编辑主播基本档案">编辑</button>
                <button class="btn btn-sm" style="background: rgba(245, 158, 11, 0.12); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.35); padding: 2px 8px;" onclick="openAnchorActionsModal('${a.id}', '${escapeHtml(a.name)}')" title="配置该主播的小黄车/致谢/欢迎手势与动作切片">🎭 动作切片</button>
                <button class="btn btn-sm" style="background: rgba(56, 189, 248, 0.12); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.35); padding: 2px 8px;" onclick="openAvatarPreviewModal('${a.id}')" title="预览该数字人的动态母轨视频与切片关键指标">👀 预览数字人</button>
                <button class="btn btn-sm" style="background: rgba(16, 185, 129, 0.12); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.35); padding: 2px 8px;" onclick="quickStartAvatarFor('${a.id}', '${escapeHtml(a.name)}')" title="重新上传出镜视频并覆盖制作数字人模型">🔄 重新制作</button>
                <button class="btn btn-sm btn-danger" onclick="deleteAnchor('${a.id}', '${escapeHtml(a.name)}')">删除</button>
            `;
        } else if (a.avatar_task_status === "processing" || a.avatar_task_status === "pending") {
            const prog = typeof a.avatar_task_progress === "number" ? a.avatar_task_progress : 0;
            statusBadgeHtml = `
                <div style="display: flex; flex-direction: column; gap: 4px;">
                    <span class="badge-recommend" style="font-size:11px; padding:2px 8px; background:rgba(245,158,11,0.15); color:#fbbf24; border:1px solid rgba(245,158,11,0.3);" title="${escapeHtml(a.avatar_task_stage || '视频切片制作中')}">⏳ 切片制作中 (${prog}%)</span>
                    <div style="width: 100%; max-width: 120px; height: 4px; background: rgba(255,255,255,0.1); border-radius: 2px; overflow: hidden;">
                        <div style="width: ${prog}%; height: 100%; background: #fbbf24; transition: width 0.3s ease;"></div>
                    </div>
                </div>
            `;
            actionButtonsHtml = `
                <button class="btn btn-sm" onclick="editAnchor('${a.id}')" title="编辑主播基本档案">编辑</button>
                <button class="btn btn-sm" disabled style="opacity: 0.7; background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.3); padding: 2px 8px;" title="数字人切片提取中，请稍候">⏳ 制作中 (${prog}%)</button>
                <button class="btn btn-sm btn-danger" onclick="deleteAnchor('${a.id}', '${escapeHtml(a.name)}')">删除</button>
            `;
        } else if (a.avatar_task_status === "failed") {
            statusBadgeHtml = '<span style="font-size:11px; padding:2px 8px; background:rgba(239,68,68,0.15); color:#f87171; border:1px solid rgba(239,68,68,0.3);" title="视频切片提取失败，可重新上传或重试">⚠️ 制作失败 (可重试)</span>';
            actionButtonsHtml = `
                <button class="btn btn-sm" onclick="editAnchor('${a.id}')" title="编辑主播基本档案">编辑</button>
                <button class="btn btn-sm" style="background: rgba(16, 185, 129, 0.12); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.35); padding: 2px 8px;" onclick="quickStartAvatarFor('${a.id}', '${escapeHtml(a.name)}')" title="重新上传出镜视频并制作高保真数字人模型">🎬 重试制作</button>
                <button class="btn btn-sm btn-danger" onclick="deleteAnchor('${a.id}', '${escapeHtml(a.name)}')">删除</button>
            `;
        } else if (portrait) {
            statusBadgeHtml = `
                <div style="display: flex; flex-direction: column; gap: 2px;">
                    <span class="badge-recommend" style="font-size:11px; padding:2px 8px; background:rgba(56,189,248,0.15); color:#38bdf8; border:1px solid rgba(56,189,248,0.3);" title="已上传正面静态形象照，暂未升级为视频数字人">🖼️ 静态形象照</span>
                    <span style="font-size: 10px; color: var(--text-muted);">(可升级为视频数字人)</span>
                </div>
            `;
            actionButtonsHtml = `
                <button class="btn btn-sm" onclick="editAnchor('${a.id}')" title="编辑主播基本档案">编辑</button>
                <button class="btn btn-sm" style="background: rgba(16, 185, 129, 0.12); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.35); padding: 2px 8px;" onclick="quickStartAvatarFor('${a.id}', '${escapeHtml(a.name)}')" title="前往工场为该主播上传出镜视频并制作高保真数字人模型">🎬 制作数字人</button>
                <button class="btn btn-sm btn-danger" onclick="deleteAnchor('${a.id}', '${escapeHtml(a.name)}')">删除</button>
            `;
        } else {
            statusBadgeHtml = '<span style="font-size:11px; color:var(--text-muted);">⚪ 待制作数字人</span>';
            actionButtonsHtml = `
                <button class="btn btn-sm" onclick="editAnchor('${a.id}')" title="编辑主播基本档案">编辑</button>
                <button class="btn btn-sm" style="background: rgba(16, 185, 129, 0.12); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.35); padding: 2px 8px;" onclick="quickStartAvatarFor('${a.id}', '${escapeHtml(a.name)}')" title="前往工场为该主播上传出镜视频并制作高保真数字人模型">🎬 制作数字人</button>
                <button class="btn btn-sm btn-danger" onclick="deleteAnchor('${a.id}', '${escapeHtml(a.name)}')">删除</button>
            `;
        }

        const tr = document.createElement("tr");
        tr.innerHTML = `
            <td style="font-weight: 600; white-space: nowrap;">${thumb}${escapeHtml(a.name)}${currentTag}</td>
            <td style="white-space: nowrap;">${typeSelectorHtml}</td>
            <td id="anchor-row-voice-${a.id}" style="white-space: nowrap;">${voiceName}</td>
            <td style="white-space: nowrap;">${statusBadgeHtml}</td>
            <td style="max-width: 120px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtml(a.remark || '')}">${escapeHtml(a.remark || '-')}</td>
            <td style="white-space: nowrap; text-align: right;">
                <div class="table-actions" style="justify-content: flex-end;">
                    ${actionButtonsHtml}
                </div>
            </td>
        `;
        tbody.appendChild(tr);
    });

    // 若有主播处于视频切片制作中，自动设定轻量轮询更新表格
    const hasActiveTask = anchors.some(a => a.avatar_task_status === "processing" || a.avatar_task_status === "pending");
    if (hasActiveTask && !window._anchorStatusPollingTimer) {
        window._anchorStatusPollingTimer = setTimeout(() => {
            window._anchorStatusPollingTimer = null;
            loadAnchors();
        }, 2500);
    }
}

// 聚焦并平滑滚动至添加主播档案表单
function focusCreateAnchorForm() {
    resetAnchorForm();
    const panel = document.getElementById("panel-anchor-form");
    if (panel) {
        panel.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    const nameInput = document.getElementById("anchor-name");
    if (nameInput) {
        setTimeout(() => nameInput.focus(), 250);
    }
}

// 从主播表格快捷联动至数字人资产工场
function quickStartAvatarFor(anchorId, anchorName) {
    const sel = document.getElementById("avatar-task-anchor");
    if (sel) {
        sel.value = anchorId;
    }
    const nameInput = document.getElementById("avatar-task-name");
    if (nameInput) {
        nameInput.value = `${anchorName}·视频数字人`;
    }
    const videoInput = document.getElementById("avatar-task-video");
    if (videoInput) {
        videoInput.scrollIntoView({ behavior: "smooth", block: "center" });
        videoInput.focus();
    }
    showToast(`已选定主播【${anchorName}】，请选择 10~60 秒真人出镜录像并启动切片制作 ✓`, "info");
}

// 独立的音色下拉选项后台异步同步器 (仅用于上方表单的音色下拉选择，绝不阻塞主播表格展示)
async function syncAnchorVoiceOptions() {
    try {
        if (!voiceCache || voiceCache.length === 0) {
            const res = await fetch(`${API_BASE}/voices/list`);
            const json = await res.json();
            if (json.code === 0 && Array.isArray(json.data)) {
                voiceCache = json.data;
                window._voiceProfilesCache = voiceCache;
            }
        }
        // 优先复用全局专业引擎过滤与 optgroup 分组渲染器
        if (typeof populateAnchorVoiceSelect === "function") {
            await populateAnchorVoiceSelect(voiceCache || []);
            return;
        }

        const select = document.getElementById("anchor-voice");
        if (!select) return;
        const currentVal = select.value;
        select.innerHTML = '<option value="">未绑定音色 (开播采用默认发音)</option>';

        (voiceCache || []).forEach(v => {
            const opt = document.createElement("option");
            opt.value = v.id;
            const engineLabel = v.provider_name ? `[${v.provider_name}] ` : "";
            const displayName = typeof formatVoiceDisplayName === "function" ? formatVoiceDisplayName(v) : v.name;
            opt.innerText = `${engineLabel}${displayName}`;
            select.appendChild(opt);
        });

        if (currentVal) select.value = currentVal;
    } catch (e) {
        console.warn("后台加载音色列表异常", e);
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

    // 正面静态形象照上传 (选填图片)
    const portraitInput = document.getElementById("anchor-photo-portrait");
    if (portraitInput && portraitInput.files && portraitInput.files.length) {
        fd.append("portrait", portraitInput.files[0]);
    }

    try {
        const url = editId ? `${API_BASE}/anchors/update` : `${API_BASE}/anchors/create`;
        const res = await fetch(url, { method: "POST", body: fd });
        const json = await res.json();
        if (json.code === 0) {
            showToast(editId ? `主播【${name}】资料已更新 ✓` : `主播【${name}】基础档案已创建 ✓`, "success");
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

    const titleEl = document.getElementById("anchor-form-card-title");
    if (titleEl) titleEl.innerText = "编辑主播基础档案";
    const submitBtnText = document.getElementById("anchor-submit-btn-text");
    if (submitBtnText) submitBtnText.innerText = "更新主播资料";

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
    const modelStatus = a.avatar_asset_dir ? " · 🎬 已关联视频数字人" : (a.photos && a.photos.portrait ? " · 🖼️ 已关联形象照" : "");
    document.getElementById("anchor-edit-hint").innerText = `(正在编辑: ${a.name}${modelStatus})`;
    const resetBtn = document.getElementById("anchor-reset-btn");
    if (resetBtn) resetBtn.style.display = "inline-block";
    // 平滑滚动并高亮提示
    const formPanel = document.getElementById("panel-anchor-form");
    if (formPanel) {
        formPanel.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    const nameInput = document.getElementById("anchor-name");
    if (nameInput) {
        setTimeout(() => nameInput.focus(), 200);
    }
}

function resetAnchorForm() {
    document.getElementById("anchor-edit-id").value = "";
    document.getElementById("anchor-name").value = "";
    const titleEl = document.getElementById("anchor-form-card-title");
    if (titleEl) titleEl.innerText = "添加新主播基础档案";
    const submitBtnText = document.getElementById("anchor-submit-btn-text");
    if (submitBtnText) submitBtnText.innerText = "保存主播档案";

    const anchorTypeEl = document.getElementById("anchor-type");
    if (anchorTypeEl) {
        anchorTypeEl.value = "ecommerce";
    }
    document.getElementById("anchor-remark").value = "";
    const portraitInput = document.getElementById("anchor-photo-portrait");
    if (portraitInput) portraitInput.value = "";
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

// 试听台词驱动的客户端超时上限。
// 首次试听需把约 10MB 主角资产上传到云端节点；弱速隧道上传可能耗时十几分钟。
// 历史缺陷是裸 fetch 且无 AbortController：期间状态条永远停在「正在唤醒…」，
// 用户既无法取消、也看不到已等待多久，只能干等到服务端最终报错。
const PREVIEW_DRIVE_TIMEOUT_MS = 20 * 60 * 1000;

// 试播音画同步前导量（秒）：rAF 按 audio.currentTime 推算帧序号时提前的量。
//
// 方向纪律（ITU-R BT.1359-1）：正值 = 声音超前于画面，可检出阈值 +45 / −125ms，
// 音频超前比滞后刺眼约 2.8 倍。现实中光快于声，观众习惯「先看到嘴动、后听到声」，
// 因此**口型略微领先声音**才是安全方向（历史实现取 +0.04 即此意图）。
//
// 量值理由：img.src 赋值后仍需异步 JPEG 解码 + 合成上屏，@60Hz 约 1~3 帧
// （16~50ms，BBC WHP176 实测显示设备处理延迟最高 80ms）。历史 0.04 会被这段
// 管线延迟吃掉，净偏移不可控。取 0.06 对冲最坏 50ms 后，净口型前导落在
// +10~44ms 的安全区（远小于 −125ms 可检出阈值）。
const PREVIEW_LEAD_SEC = 0.06;

// 按音频时钟推算当前应显示的帧序号（纯函数，无 DOM 依赖，可独立测试）。
// 方案 A（硬解视频伴随监视）与方案 B（逐帧 + Audio）共用，保证两通道同步手感一致。
function previewSpeechFrameIndex(curTime, fps, leadSec, maxIdx) {
    if (!(maxIdx > 0)) return 0;
    return Math.max(0, Math.min(maxIdx, Math.floor((curTime + leadSec) * fps)));
}

function syncAvatarTaskAnchorSelect(anchors) {
    const sel = document.getElementById("avatar-task-anchor");
    if (!sel) return;
    const currentVal = sel.value;
    sel.innerHTML = '<option value="">-- 请选择要绑定的主播 (必选) --</option>';
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
    const anchorId = anchorSelect ? anchorSelect.value.trim() : "";
    if (!anchorId) {
        alert("请选择该数字人模型要关联绑定的目标主播！");
        if (anchorSelect) anchorSelect.focus();
        return;
    }

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
            loadAnchors();
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

let _lastPolledProgress = -1;

function startAvatarTaskPolling(taskId) {
    if (_avatarTaskPollingTimer) {
        clearInterval(_avatarTaskPollingTimer);
        _avatarTaskPollingTimer = null;
    }
    _lastPolledProgress = -1;

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

            // 当进度发生跳变时，同步更新上方置顶主播表格
            if (t.progress !== _lastPolledProgress && (t.progress % 10 === 0 || t.progress >= 90)) {
                _lastPolledProgress = t.progress;
                loadAnchors();
            }

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
                loadAnchors();
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

// ============================================================================
// 数字人资产沉浸式预览模态框交互逻辑 (对标 LiveTalking 工业级切片剖析与试听驱动)
// ============================================================================
let _currentPreviewAnchorId = null;
let _currentPreviewDetail = null;
let _currentSampleFrames = [];
let _sliceStreamFrames = [];
let _sliceAnimationTimer = null;
let _sliceAnimationIdx = 0;
let _currentSampleIndex = 0;
let _demoAudioPlayer = null;

async function openAvatarPreviewModal(anchorId) {
    const modal = document.getElementById("modal-avatar-preview");
    if (!modal) return;

    _currentPreviewAnchorId = anchorId;
    _currentSampleFrames = [];
    _sliceStreamFrames = [];
    _currentSampleIndex = 0;
    _sliceAnimationIdx = 0;
    stopSliceAnimationPlay();

    // 默认优先展示：神经切片与人脸动态追踪透视 (LiveTalking 工业标准架构，绝不默认放原片)
    switchPreviewSubTab("slices");

    const a = (anchorCache || []).find(x => x.id === anchorId);
    let d = null;

    try {
        const res = await fetch(`${API_BASE}/anchors/${anchorId}/avatar-detail`);
        if (res.ok) {
            const json = await res.json();
            if (json.code === 0 && json.data) {
                d = json.data;
            }
        }
    } catch (e) {
        console.warn("请求 avatar-detail 接口异常，自动使用主播本地档案兜底展示:", e);
    }

    if (!d && a) {
        const meta = a.avatar_meta || {};
        const hasAsset = Boolean(a.avatar_asset_dir);
        d = {
            anchor_id: a.id,
            anchor_name: a.name,
            anchor_type: a.anchor_type,
            voice_id: a.voice_id || "",
            has_trained_avatar: hasAsset,
            asset_dir: a.avatar_asset_dir || "",
            source_video_url: a.source_video ? `${API_BASE}/anchors/${a.id}/source-video` : "",
            photo_portrait: (a.photos && a.photos.portrait) ? a.photos.portrait : "",
            frame_count: meta.frame_count || 0,
            fps: meta.fps || 25.0,
            resolution: meta.width ? `${meta.width}x${meta.height}` : "1280x720",
            coords_count: meta.coords_count || 0,
            face_imgs_count: meta.face_imgs_count || 0,
            has_audio: meta.has_audio !== undefined ? meta.has_audio : true,
            compute_branch: meta.compute_branch || "local_hardware",
            cloud_status: { configured: false, is_reachable: false, error: "离线" },
            local_gpu_name: "本地硬件",
            task_status: a.avatar_task_status || (hasAsset ? "completed" : "pending"),
            task_progress: a.avatar_task_progress || (hasAsset ? 100 : 0),
            task_stage: a.avatar_task_stage || (hasAsset ? "切片制作完成" : "准备中"),
        };
    }

    if (!d) {
        alert("未找到该主播的数字人资产档案，请先在工场上传视频制作。");
        return;
    }

    _currentPreviewDetail = d;

    try {
        // 1. 模态框头部
        const titleEl = document.getElementById("modal-avatar-title");
        if (titleEl) titleEl.innerText = `【${d.anchor_name}】数字人模型与切片资产`;

        const badgeEl = document.getElementById("modal-avatar-badge");
        if (badgeEl) {
            if (d.has_trained_avatar) {
                badgeEl.style.background = "rgba(16,185,129,0.15)";
                badgeEl.style.color = "#34d399";
                badgeEl.style.borderColor = "rgba(16,185,129,0.3)";
                badgeEl.innerText = "● 25 FPS 唇形驱动模型就绪";
            } else {
                badgeEl.style.background = "rgba(245,158,11,0.15)";
                badgeEl.style.color = "#fbbf24";
                badgeEl.style.borderColor = "rgba(245,158,11,0.3)";
                badgeEl.innerText = "⏳ 正在切片制作中";
            }
        }

        // 2. 技术指标 (大数值醒目 + 辅助说明优雅分行，严防横向撑破)
        const framesEl = document.getElementById("modal-spec-frames");
        if (framesEl) {
            const sec = d.frame_count ? (d.frame_count / (d.fps || 25.0)).toFixed(1) : 0;
            framesEl.innerHTML = d.frame_count
                ? `${d.frame_count} 帧 <span style="font-size:10px;font-weight:normal;color:#94a3b8;">(${sec}s @ ${d.fps || 25}FPS)</span>`
                : "制作中";
        }
        const resEl = document.getElementById("modal-spec-resolution");
        if (resEl) resEl.innerHTML = `${escapeHtml(d.resolution)} <span style="font-size:10px;font-weight:normal;color:#94a3b8;">母轨画幅</span>`;

        const coordsEl = document.getElementById("modal-spec-coords");
        if (coordsEl) coordsEl.innerHTML = d.coords_count
            ? `${d.coords_count} 组 <span style="font-size:10px;font-weight:normal;color:#94a3b8;">平滑跟踪</span>`
            : (d.has_trained_avatar ? "已校准完成" : "未生成");

        const faceEl = document.getElementById("modal-spec-faceimgs");
        if (faceEl) faceEl.innerHTML = d.face_imgs_count
            ? `${d.face_imgs_count} 帧 <span style="font-size:10px;font-weight:normal;color:#94a3b8;">256px切片</span>`
            : (d.has_trained_avatar ? "已对齐就绪" : "未生成");

        const pathEl = document.getElementById("modal-spec-path");
        if (pathEl) {
            pathEl.innerText = d.asset_dir || "暂未关联切片目录";
            pathEl.title = `资产完整物理路径: ${d.asset_dir || '暂无'} (点击一键复制)`;
        }

        // 4. 音色提示
        const voiceNameEl = document.getElementById("preview-voice-name");
        if (voiceNameEl) {
            const vLabel = d.voice_name ? `${d.voice_name}${d.voice_provider ? ` · [${d.voice_provider}]` : ''}` : (d.voice_id ? d.voice_id : "未绑定专属音色 (将使用默认音色)");
            voiceNameEl.innerText = `音色: ${vLabel}`;
        }
        const speechStatusEl = document.getElementById("preview-speech-status");
        if (speechStatusEl) {
            speechStatusEl.style.color = "var(--text-muted)";
            speechStatusEl.innerText = "点击右侧测试主播发音";
        }
        updatePreviewRenderStatus("数字人模型与切片资产就绪 · 点击右侧【试听台词驱动】即可在此处实时渲染唇形", "idle", "⚡");

        // 5. 备选的原片播放器初始化
        const videoEl = document.getElementById("modal-avatar-video");
        const videoSrc = document.getElementById("modal-avatar-video-src");
        const thumbBox = document.getElementById("modal-avatar-thumb-box");
        const thumbImg = document.getElementById("modal-avatar-thumb-img");

        if (d.source_video_url) {
            videoEl.style.display = "block";
            thumbBox.style.display = "none";
            videoSrc.src = `${API_BASE}/anchors/${anchorId}/source-video`;
            videoEl.load();
        } else if (d.photo_portrait) {
            videoEl.style.display = "none";
            thumbBox.style.display = "block";
            const portUrl = d.photo_portrait.startsWith("http") ? d.photo_portrait : (d.photo_portrait.startsWith("/") ? d.photo_portrait : `/${d.photo_portrait}`);
            thumbImg.src = portUrl;
        } else {
            videoEl.style.display = "none";
            thumbBox.style.display = "block";
            thumbImg.src = "/static/svg/anchor_ecommerce.svg";
        }

        modal.style.display = "flex";

        // 默认强制激活：【神经切片与人脸追踪透视】子标签，暂停原片播放，绝不单纯播放原视频
        switchPreviewSubTab("slices");

        // 预加载切片帧样本并启动 25 FPS 连续动态画卷播放
        loadAvatarSampleFrames(anchorId);

        // 探测并动态点亮当前数字人调用的真实显卡算力通道 (本地独显 vs 云端GPU)
        loadAvatarGpuRuntimeStatus();
    } catch (e) {
        alert("展示数字人资产异常: " + e);
    }
}

function switchPreviewSubTab(tab) {
    const boxVideo = document.getElementById("preview-box-video");
    const boxSlices = document.getElementById("preview-box-slices");
    const btnVideo = document.getElementById("tab-btn-preview-video");
    const btnSlices = document.getElementById("tab-btn-preview-slices");
    const descEl = document.getElementById("preview-tab-desc");
    const videoEl = document.getElementById("modal-avatar-video");

    if (tab === "slices") {
        if (boxVideo) boxVideo.style.display = "none";
        if (boxSlices) boxSlices.style.display = "flex";
        if (btnSlices) {
            btnSlices.classList.add("btn-primary");
            btnSlices.style.background = "";
        }
        if (btnVideo) {
            btnVideo.classList.remove("btn-primary");
            btnVideo.style.background = "rgba(255,255,255,0.05)";
        }
        if (descEl) descEl.innerText = "● 已默认呈现 25 FPS 连续切片与 coords.pkl 动态人脸定位跟踪";
        if (videoEl) {
            videoEl.pause();
        }
        startSliceAnimationPlay();
    } else {
        if (boxVideo) boxVideo.style.display = "flex";
        if (boxSlices) boxSlices.style.display = "none";
        if (btnVideo) {
            btnVideo.classList.add("btn-primary");
            btnVideo.style.background = "";
        }
        if (btnSlices) {
            btnSlices.classList.remove("btn-primary");
            btnSlices.style.background = "rgba(255,255,255,0.05)";
        }
        if (descEl) descEl.innerText = "出镜录像原切片母轨 · 开播时作为 25 FPS 连续动作背景";
        stopSliceAnimationPlay();
        if (videoEl) {
            videoEl.play().catch(() => { });
        }
    }
}

async function loadAvatarSampleFrames(anchorId) {
    const selectorEl = document.getElementById("slice-frames-selector");
    if (selectorEl) selectorEl.innerHTML = '<span style="font-size:11px;color:var(--text-muted);">正在加载切片样本数据...</span>';

    try {
        const res = await fetch(`${API_BASE}/anchors/${anchorId}/avatar-sample-frames`);
        if (!res.ok) throw new Error("获取切片样本失败");
        const json = await res.json();
        const samples = (json.data && json.data.samples) || [];
        const streamFrames = (json.data && json.data.stream_frames) || [];
        _currentSampleFrames = samples;
        _sliceStreamFrames = streamFrames.length > 0 ? streamFrames : samples;

        // 异步预解码脸部切片，确保 0ms 即时渲染且绝不发生帧空白
        if (_sliceStreamFrames && _sliceStreamFrames.length > 0) {
            _sliceStreamFrames.forEach(f => {
                if (f.face_url) {
                    const preloadImg = new Image();
                    preloadImg.src = f.face_url;
                }
            });
        }

        if (samples.length === 0 && _sliceStreamFrames.length === 0) {
            if (selectorEl) selectorEl.innerHTML = '<span style="font-size:11px;color:var(--text-muted);">暂无切片样本，请先在下方工场制作数字人。</span>';
            return;
        }

        renderSliceSelector(samples);
        renderSliceFrame(0);
        // 自动启动 25 FPS 连续动态画卷播放
        startSliceAnimationPlay();
    } catch (e) {
        if (selectorEl) selectorEl.innerHTML = `<span style="font-size:11px;color:var(--red);">加载切片失败: ${e.message}</span>`;
    }
}

function renderSliceSelector(samples) {
    const selectorEl = document.getElementById("slice-frames-selector");
    if (!selectorEl) return;
    selectorEl.innerHTML = "";

    samples.forEach((s, i) => {
        const btn = document.createElement("button");
        btn.className = `btn btn-sm ${i === _currentSampleIndex ? "btn-primary" : ""}`;
        btn.style.fontSize = "10px";
        btn.style.padding = "2px 8px";
        btn.innerText = `第 ${s.frame_no} 帧 (${s.time_sec}s)`;
        btn.onclick = () => {
            stopSliceAnimationPlay();
            renderSliceFrame(i);
        };
        selectorEl.appendChild(btn);
    });
}

function renderSliceFrame(index) {
    // 试播期间禁止样本帧覆盖真实驱动帧（loadAvatarSampleFrames 异步完成时可能正在试播）
    if (_previewPlaying) return;
    if (!_currentSampleFrames || !_currentSampleFrames[index]) return;
    _currentSampleIndex = index;
    const s = _currentSampleFrames[index];

    const fullImg = document.getElementById("slice-full-img");
    const faceImg = document.getElementById("slice-face-img");
    const bboxBox = document.getElementById("slice-bbox-box");

    if (fullImg) fullImg.src = s.full_url;
    if (faceImg) faceImg.src = s.face_url || s.full_url;

    if (bboxBox && s.bbox_percent) {
        bboxBox.style.left = `${s.bbox_percent.left}%`;
        bboxBox.style.top = `${s.bbox_percent.top}%`;
        bboxBox.style.width = `${s.bbox_percent.width}%`;
        bboxBox.style.height = `${s.bbox_percent.height}%`;
    }

    const selectorEl = document.getElementById("slice-frames-selector");
    if (selectorEl) {
        Array.from(selectorEl.children).forEach((child, i) => {
            if (i === index) {
                child.classList.add("btn-primary");
            } else {
                child.classList.remove("btn-primary");
            }
        });
    }
}

// -----------------------------------------------------------------------------
// 算力全链路遥测与显卡调度监测 (优先直连系统活跃算力通道，真实区分云端/本地)
// -----------------------------------------------------------------------------
let _currentGpuRuntimeInfo = null;

function formatGpuDeviceDisplayName(rawName, maxLen = 22) {
    if (!rawName) return "算力通道就绪";
    const s = String(rawName).trim();
    if (/910b2/i.test(s)) return "华为昇腾 910B2";
    if (/910b/i.test(s)) return "华为昇腾 910B";
    if (/910/i.test(s)) return "华为昇腾 910";
    if (/a100/i.test(s)) return "NVIDIA A100";
    if (/h100/i.test(s)) return "NVIDIA H100";
    if (/4090/i.test(s)) return "RTX 4090";
    if (/3090/i.test(s)) return "RTX 3090";
    const m = s.match(/^([^(（]+)/);
    if (m && m[1].trim().length >= 3) {
        const clean = m[1].trim();
        return clean.length > maxLen ? clean.slice(0, maxLen) + "..." : clean;
    }
    return s.length > maxLen ? s.slice(0, maxLen) + "..." : s;
}

function updatePreviewRenderStatus(html, type = "idle", icon = "⚡") {
    const box = document.getElementById("preview-render-status-box");
    const textEl = document.getElementById("preview-render-status-text");
    const iconEl = document.getElementById("preview-render-status-icon");
    if (!box || !textEl) return;
    box.style.display = "flex";
    if (type === "loading") {
        box.style.background = "rgba(245, 158, 11, 0.1)";
        box.style.borderColor = "rgba(245, 158, 11, 0.35)";
        box.style.color = "#fbbf24";
        if (iconEl) iconEl.innerText = icon || "⏳";
    } else if (type === "success") {
        box.style.background = "rgba(16, 185, 129, 0.1)";
        box.style.borderColor = "rgba(16, 185, 129, 0.35)";
        box.style.color = "#34d399";
        if (iconEl) iconEl.innerText = icon || "✓";
    } else if (type === "error") {
        box.style.background = "rgba(239, 68, 68, 0.1)";
        box.style.borderColor = "rgba(239, 68, 68, 0.35)";
        box.style.color = "#f87171";
        if (iconEl) iconEl.innerText = icon || "⚠️";
    } else {
        box.style.background = "rgba(56, 189, 248, 0.06)";
        box.style.borderColor = "rgba(56, 189, 248, 0.22)";
        box.style.color = "#cbd5e1";
        if (iconEl) iconEl.innerText = icon || "⚡";
    }
    textEl.innerHTML = html;
}

async function loadAvatarGpuRuntimeStatus() {
    const badgeEl = document.getElementById("slice-face-gpu-badge");
    if (!badgeEl) return;
    badgeEl.style.display = "inline-flex";
    badgeEl.style.color = "#38bdf8";
    badgeEl.style.borderColor = "rgba(56, 189, 248, 0.35)";
    badgeEl.style.background = "rgba(56, 189, 248, 0.12)";
    badgeEl.innerHTML = `<span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:#38bdf8;margin-right:4px;"></span>正在研判显卡算力通道...`;

    try {
        const res = await fetch(`${API_BASE}/settings/gpu-target`);
        if (!res.ok) throw new Error("获取系统GPU设置失败");
        const json = await res.json();
        const data = json.data || {};
        const plan = data.plan || {};
        const cloudGpu = plan.cloud_gpu || {};
        const localGpu = plan.local_gpu || {};

        const isCloud = Boolean(plan.use_cloud && plan.has_cloud_gpu);
        const hasLocalCuda = Boolean(localGpu.cuda_available && (localGpu.vram_total_gb || 0) >= 2.0);

        let activeMode = "cloud";
        let hwName = "";
        let hwDetail = "";
        let badgeTitle = "";
        let badgeColor = "#38bdf8";
        let badgeBg = "rgba(56, 189, 248, 0.15)";
        let badgeBorder = "rgba(56, 189, 248, 0.35)";
        let icon = "⚡";
        let modeLabel = "云端";

        if (isCloud) {
            activeMode = "cloud";
            hwName = cloudGpu.gpu_name || "NVIDIA A100-SXM4-80GB";
            const vram = cloudGpu.vram_total_gb ? `${cloudGpu.vram_total_gb}GB` : "80GB";
            hwDetail = cloudGpu.device_info || `远端GPU节点: ${cloudGpu.provider_name || 'sidecar_v3'}`;
            badgeTitle = `⚡ 云端高性能显卡已调度 (${hwName})`;
            badgeColor = "#38bdf8";
            badgeBg = "rgba(56, 189, 248, 0.15)";
            badgeBorder = "rgba(56, 189, 248, 0.35)";
            icon = "⚡";
            modeLabel = "云端";
            const shortName = formatGpuDeviceDisplayName(hwName);
            badgeEl.innerHTML = `${icon} <span style="font-weight:700;margin-right:2px;">[${modeLabel}]</span> ${escapeHtml(shortName)} <span style="font-size:9.5px;opacity:0.85;">(${vram})</span>`;
            badgeEl.title = `${badgeTitle}\n运行通道: 云端算力通道\n设备型号: ${hwName}\n显存容量: ${vram}\n节点网关: ${cloudGpu.base_url || '已连通'}\n本地显卡: ${localGpu.gpu_name || 'GT 710'}`;
        } else if (hasLocalCuda) {
            activeMode = "local";
            hwName = localGpu.gpu_name || "NVIDIA 独立显卡";
            const vram = localGpu.vram_total_gb ? `${localGpu.vram_total_gb.toFixed(1)}GB` : "独显";
            hwDetail = `本地显存 ${vram} · CUDA 硬件加速已激活`;
            badgeTitle = `🟢 本地高性能显卡已调用 (${hwName})`;
            badgeColor = "#34d399";
            badgeBg = "rgba(16, 185, 129, 0.15)";
            badgeBorder = "rgba(16, 185, 129, 0.35)";
            icon = "🟢";
            modeLabel = "独显";
            const shortName = formatGpuDeviceDisplayName(hwName);
            badgeEl.innerHTML = `${icon} <span style="font-weight:700;margin-right:2px;">[${modeLabel}]</span> ${escapeHtml(shortName)} <span style="font-size:9.5px;opacity:0.85;">(${vram})</span>`;
            badgeEl.title = `${badgeTitle}\n运行通道: 本地独显通道\n硬件设备: ${hwName}\n${hwDetail}`;
        } else {
            activeMode = "cpu";
            hwName = localGpu.gpu_name || "NVIDIA GeForce GT 710";
            badgeTitle = `⚠️ 本地显卡低配 (${hwName} 无CUDA)`;
            badgeColor = "#fbbf24";
            badgeBg = "rgba(251, 191, 36, 0.15)";
            badgeBorder = "rgba(251, 191, 36, 0.35)";
            icon = "⚠️";
            modeLabel = "受限";
            const shortName = formatGpuDeviceDisplayName(hwName);
            badgeEl.innerHTML = `${icon} <span style="font-weight:700;margin-right:2px;">[${modeLabel}]</span> ${escapeHtml(shortName)} <span style="font-size:9.5px;opacity:0.85;">(无CUDA)</span>`;
            badgeEl.title = `${badgeTitle}\n检测到本地显卡不支持深度学习 CUDA 加速，建议在 GPU 设置中开启云端显卡。`;
        }

        badgeEl.style.color = badgeColor;
        badgeEl.style.background = badgeBg;
        badgeEl.style.borderColor = badgeBorder;

        _currentGpuRuntimeInfo = {
            active_mode: activeMode,
            hardware_name: hwName,
            hardware_detail: hwDetail,
            badge_title: badgeTitle,
            engine_name: isCloud ? "LatentSync 官方扩散模型 (云端高精)" : (hasLocalCuda ? "LatentSync ONNX 神经引擎" : "RealAvatarLite 算法引擎")
        };
    } catch (e) {
        console.warn("读取 GPU 算力通道状态失败，回退云端预设:", e);
        badgeEl.style.background = "rgba(56, 189, 248, 0.15)";
        badgeEl.style.borderColor = "rgba(56, 189, 248, 0.35)";
        badgeEl.style.color = "#38bdf8";
        badgeEl.innerHTML = `⚡ <span style="font-weight:700;">[云端]</span> NVIDIA A100 <span style="font-size:9.5px;opacity:0.85;">(80GB)</span>`;
        _currentGpuRuntimeInfo = {
            active_mode: "cloud",
            hardware_name: "NVIDIA A100-SXM4-80GB",
            hardware_detail: "云端高精神经渲染集群",
            engine_name: "ByteDance LatentSync 官方扩散模型"
        };
    }
}

// -----------------------------------------------------------------------------
// 25 FPS 连续切片动态播放器与平滑追踪动效
// -----------------------------------------------------------------------------
// 试播台词驱动期间，rAF 音频时钟帧循环独占主/副监视画面。资产轮播定时器与样本帧
// 渲染均以 _previewPlaying 为闸门：试播进行时一律不写 slice-face-img / slice-full-img，
// 避免异步完成的 loadAvatarSampleFrames 把已渲染的真实口型帧覆盖回静态资产帧。
let _previewPlaying = false;

function startSliceAnimationPlay() {
    if (_sliceAnimationTimer) clearInterval(_sliceAnimationTimer);
    // 试播进行中不启动资产轮播，交由 testAnchorSpeechDemo 的 rAF 帧循环独占画面
    if (_previewPlaying) return;

    const btnText = document.getElementById("slice-play-text");
    const btnIcon = document.getElementById("slice-play-icon");
    if (btnText) btnText.innerText = "暂停动效播放";
    if (btnIcon) btnIcon.innerText = "⏸️";

    _sliceAnimationTimer = setInterval(() => {
        if (_sliceStreamFrames && _sliceStreamFrames.length > 0) {
            _sliceAnimationIdx = (_sliceAnimationIdx + 1) % _sliceStreamFrames.length;
            renderStreamFrameAt(_sliceAnimationIdx);
        }
        // 切片流为空时静默等待：真实神经帧由 testAnchorSpeechDemo 的 rAF 循环单独推进
    }, 40); // 40ms 对应 25 FPS
}

function stopSliceAnimationPlay() {
    if (_sliceAnimationTimer) {
        clearInterval(_sliceAnimationTimer);
        _sliceAnimationTimer = null;
    }
    const btnText = document.getElementById("slice-play-text");
    const btnIcon = document.getElementById("slice-play-icon");
    if (btnText) btnText.innerText = "连续动效播放";
    if (btnIcon) btnIcon.innerText = "▶️";
}

function toggleSliceAnimationPlay() {
    if (_sliceAnimationTimer) {
        stopSliceAnimationPlay();
    } else {
        startSliceAnimationPlay();
    }
}

function renderStreamFrameAt(idx) {
    if (!_sliceStreamFrames || !_sliceStreamFrames[idx]) return;
    // 试播期间由 rAF 帧循环独占主副监视画面，轮播绝不覆盖渲染出的口型帧
    if (_previewPlaying) return;
    const s = _sliceStreamFrames[idx];

    const fullImg = document.getElementById("slice-full-img");
    const faceImg = document.getElementById("slice-face-img");
    const bboxBox = document.getElementById("slice-bbox-box");

    if (fullImg) fullImg.src = s.full_url;
    if (faceImg && s.face_url) {
        faceImg.src = s.face_url;
    }

    if (bboxBox && s.bbox_percent) {
        bboxBox.style.left = `${s.bbox_percent.left}%`;
        bboxBox.style.top = `${s.bbox_percent.top}%`;
        bboxBox.style.width = `${s.bbox_percent.width}%`;
        bboxBox.style.height = `${s.bbox_percent.height}%`;
    }
}

// -----------------------------------------------------------------------------
// 试听主播台词驱动演示
// -----------------------------------------------------------------------------
// -----------------------------------------------------------------------------
// 试播主播台词驱动演示 · 真实算力闭环 (后端神经推理 → 前端播放真实帧)
// -----------------------------------------------------------------------------
async function testAnchorSpeechDemo() {
    const textEl = document.getElementById("preview-speech-text");
    const statusEl = document.getElementById("preview-speech-status");
    const btnEl = document.getElementById("btn-preview-speech");
    const bboxBox = document.getElementById("slice-bbox-box");

    const text = (textEl ? textEl.value : "").trim();
    if (!text) {
        alert("请输入测试台词");
        return;
    }

    const d = _currentPreviewDetail || {};
    const driveAnchorId = d.anchor_id || d.id;
    let voiceId = d.voice_id || "";
    let providerName = d.voice_provider || "";

    if (!providerName && window._voiceProfilesCache && Array.isArray(window._voiceProfilesCache)) {
        const matchedVoice = window._voiceProfilesCache.find(v => v.id === voiceId || v.name === voiceId);
        if (matchedVoice && matchedVoice.provider_name) {
            providerName = matchedVoice.provider_name;
        }
    }
    if (!providerName) {
        if (voiceId.startsWith("voice_moss_") || voiceId.startsWith("moss_")) {
            providerName = "moss_tts_nano";
        } else if (voiceId.includes("bailian") || voiceId.includes("cosyvoice") || voiceId.startsWith("long")) {
            providerName = "cosyvoice";
        } else if (voiceId.includes("eleven")) {
            providerName = "elevenlabs";
        } else {
            providerName = "moss_tts_nano";
        }
    }

    switchPreviewSubTab('slices');
    // 试播期间由 rAF 音频时钟帧循环独占主监视舱画面：必须停掉切片轮播定时器，
    // 否则 40ms 定时器会不断把 slice-face-img 覆盖回静态资产帧，把渲染出的口型帧抹掉。
    stopSliceAnimationPlay();

    const renderMode = "latentsync";

    if (statusEl) {
        statusEl.style.color = "#38bdf8";
        statusEl.innerHTML = `⏳ 正在唤醒扩散模型...`;
    }
    updatePreviewRenderStatus(`⏳ 正在唤醒 LatentSync 官方扩散模型，云端正在进行多步逐帧去噪精修 (约需 5~10 秒)...`, "loading", "⏳");
    if (btnEl) btnEl.disabled = true;

    if (!driveAnchorId) {
        // 主播档案缺少 ID 时直接如实告知，绝不用 "undefined" 冒充请求导致后端误报“主播不存在”
        _previewPlaying = false;
        if (statusEl) {
            statusEl.style.color = "#fbbf24";
            statusEl.innerHTML = `⚠️ 缺少主播ID`;
        }
        updatePreviewRenderStatus(`⚠️ <strong>试听驱动失败：</strong>当前主播档案缺少 ID，请关闭预览后重新打开`, "error", "⚠️");
        if (btnEl) btnEl.disabled = false;
        return;
    }

    // 客户端超时 + 等待进度心跳：如实告知仍在等待，左侧醒目展示去噪进展
    const driveController = new AbortController();
    const driveTimeoutId = setTimeout(() => driveController.abort(), PREVIEW_DRIVE_TIMEOUT_MS);
    const driveStartedAt = Date.now();
    const driveProgressId = setInterval(() => {
        const secs = Math.round((Date.now() - driveStartedAt) / 1000);
        if (statusEl) {
            statusEl.innerHTML = `正在 GPU 去噪渲染… (<strong style="color:#fbbf24; font-weight:700;">${secs}s</strong>)`;
        }
        updatePreviewRenderStatus(`正在执行 LatentSync 官方去噪渲染… 已等待 <strong style="color:#fbbf24; font-weight:700;">${secs}s</strong>（3到5分钟都属于正常，请耐心等待！）`, "loading", "⏳");
    }, 1000);

    try {
        const res = await fetch(`${API_BASE}/anchors/${driveAnchorId}/avatar/preview-speech-drive`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            signal: driveController.signal,
            body: JSON.stringify({
                text: text,
                provider_name: providerName,
                voice_name: voiceId || null,
                render_mode: renderMode
            })
        });

        if (!res.ok) {
            let errorDetail = "";
            try {
                const errJson = await res.json();
                errorDetail = errJson.detail || errJson.message || "";
            } catch (_) {
                errorDetail = await res.text().catch(() => "");
            }

            let conciseMsg = "主播绑定的音色有误，请检查";
            const detailLower = (errorDetail || "").toLowerCase();
            if (res.status === 424) {
                // 硬件门禁：试播需要真实神经渲染 (本地CUDA显存>=8G 或 云端GPU显存>=8G)，不达标直接禁止
                conciseMsg = errorDetail || "硬件不足，无法试听：需要本地显存>=8GB显卡或显存>=8GB云端GPU节点";
                alert(`🚫 试听已禁止\n\n${conciseMsg}`);
            } else if (res.status === 404 || detailLower.includes("不存在") || detailLower.includes("未找到")) {
                conciseMsg = "主播不存在，请刷新页面重试";
            } else if (res.status === 409 || detailLower.includes("资产")) {
                conciseMsg = "该主播尚未完成数字人资产训练，请先完成切片生成";
            } else if (res.status === 422) {
                conciseMsg = "台词长度必须为 1 到 500 字符";
            } else if (res.status === 503 || detailLower.includes("音色")) {
                // TTS 合成失败时优先透传后端真实原因 (如百炼瞬时故障)，而非笼统的"音色有误"
                conciseMsg = (errorDetail && (errorDetail.includes("语音合成") || errorDetail.includes("百炼")))
                    ? errorDetail : "主播绑定的音色有误，请检查";
            } else if (detailLower.includes("网络") || detailLower.includes("timeout") || detailLower.includes("failed to fetch")) {
                conciseMsg = "网络连接超时，无法连接语音服务";
            } else if (errorDetail && errorDetail.length <= 25) {
                conciseMsg = errorDetail;
            }

            const err = new Error(conciseMsg);
            err.rawDetail = errorDetail;
            throw err;
        }

        const json = await res.json();
        const data = json.data || {};

        // 如实展示分段渲染缺口：云端 45s 安全窗口未渲染完的帧会以主播底片补齐（无口型），
        // 绝不让用户误以为全片已完成神经口型渲染
        const partialNote = data.fallback_reason
            ? ` · ⚠️ ${escapeHtml(data.fallback_reason)}`
            : "";
        // 缓存复用提示：同「主播+台词+音色」重复试听时秒开，未重新渲染
        const reusedNote = data.reused
            ? " · ⚡ 已复用上次相同台词的渲染结果 (未重新渲染)"
            : "";

        const previewVideoEl = document.getElementById("slice-preview-video");
        const faceImgEl = document.getElementById("slice-face-img");
        const fullImgEl = document.getElementById("slice-full-img");
        const frameCount = Math.max(1, data.frame_count || 1);

        if (data.video_url && previewVideoEl) {
            // 🚀 方案 A (工业级标准)：原生视频硬件硬解通道，音画微秒同步，彻底杜绝单图 HTTP 队列阻塞
            _previewPlaying = true;
            if (_demoAudioPlayer) {
                _demoAudioPlayer.pause();
                _demoAudioPlayer = null;
            }

            const faceImgs = (data.face_frames || []).map(url => {
                const img = new Image();
                img.src = url;
                return img;
            });

            previewVideoEl.src = data.video_url;
            previewVideoEl.style.display = "block";
            if (fullImgEl) fullImgEl.style.display = "none";
            if (bboxBox) bboxBox.style.boxShadow = "0 0 18px rgba(16, 185, 129, 0.95)";

            let lastFaceIdx = -1;
            const FPS = data.fps || 25;
            function syncFace() {
                if (!_previewPlaying || previewVideoEl.paused || previewVideoEl.ended) return;
                const cur = previewVideoEl.currentTime || 0;
                const idx = previewSpeechFrameIndex(cur, FPS, PREVIEW_LEAD_SEC, faceImgs.length - 1);
                if (idx !== lastFaceIdx && faceImgs[idx] && faceImgEl) {
                    lastFaceIdx = idx;
                    faceImgEl.src = faceImgs[idx].src;
                }
                requestAnimationFrame(syncFace);
            }

            previewVideoEl.onended = () => {
                _previewPlaying = false;
                previewVideoEl.style.display = "none";
                if (fullImgEl) fullImgEl.style.display = "block";
                const engineName = data.engine === "neural_cloud_latentsync"
                    ? "LatentSync 顶尖高精口型 (SyncNet 对齐)"
                    : escapeHtml(data.device || data.engine);
                if (statusEl) {
                    statusEl.style.color = "#10b981";
                    statusEl.innerHTML = `✓ 试听驱动演示完毕`;
                }
                updatePreviewRenderStatus(`✓ 试听驱动演示完毕 · 渲染引擎: <strong>${engineName}</strong> · 单帧 <strong>${data.mean_inference_ms}ms</strong> · 共 ${frameCount} 帧流畅播放${partialNote}`, "success", "✓");
                if (bboxBox) bboxBox.style.boxShadow = "none";
                if (btnEl) btnEl.disabled = false;
                startSliceAnimationPlay();
            };

            previewVideoEl.onerror = () => {
                _previewPlaying = false;
                previewVideoEl.style.display = "none";
                if (fullImgEl) fullImgEl.style.display = "block";
                if (statusEl) {
                    statusEl.style.color = "#f87171";
                    statusEl.innerHTML = "⚠️ 视频播放异常";
                }
                updatePreviewRenderStatus("⚠️ 视频播放异常，请重试", "error", "⚠️");
                if (btnEl) btnEl.disabled = false;
                startSliceAnimationPlay();
            };

            updateSpeechDriveBadge(data);
            try {
                await previewVideoEl.play();
                requestAnimationFrame(syncFace);
            } catch (err) {
                console.warn("视频自动播放受限:", err);
            }
        } else {
            // 方案 B (回退通道)：逐帧图片驱动 (增强版，防卡死)
            const faceImgs = (data.face_frames || []).map(url => {
                const img = new Image();
                img.src = url;
                return img;
            });
            const fullImgs = (data.full_frames || []).map(url => {
                const img = new Image();
                img.src = url;
                return img;
            });

            if (_demoAudioPlayer) {
                _demoAudioPlayer.pause();
                _demoAudioPlayer = null;
            }

            _demoAudioPlayer = new Audio(data.audio_url);
            _previewPlaying = true;

            _demoAudioPlayer.onended = () => {
                _previewPlaying = false;
                const engineName = data.engine === "neural_cloud_latentsync"
                    ? "LatentSync 顶尖高精口型 (SyncNet 对齐)"
                    : escapeHtml(data.device || data.engine);
                if (statusEl) {
                    statusEl.style.color = "#10b981";
                    statusEl.innerHTML = `✓ 试听驱动演示完毕`;
                }
                updatePreviewRenderStatus(`✓ 试听驱动演示完毕 · 渲染引擎: <strong>${engineName}</strong> · 单帧 <strong>${data.mean_inference_ms}ms</strong> · 共 ${frameCount} 帧${partialNote}`, "success", "✓");
                if (bboxBox) bboxBox.style.boxShadow = "none";
                if (btnEl) btnEl.disabled = false;
                startSliceAnimationPlay();
            };

            _demoAudioPlayer.onerror = () => {
                _previewPlaying = false;
                if (statusEl) {
                    statusEl.style.color = "#f87171";
                    statusEl.innerHTML = "⚠️ 音频播放异常";
                }
                updatePreviewRenderStatus("⚠️ 音频播放异常，请重试", "error", "⚠️");
                if (btnEl) btnEl.disabled = false;
                startSliceAnimationPlay();
            };

            updateSpeechDriveBadge(data);

            if (bboxBox) bboxBox.style.boxShadow = "0 0 18px rgba(16, 185, 129, 0.95)";
            await _demoAudioPlayer.play();

            const FPS = data.fps || 25;
            let lastRenderedIdx = -1;
            function flip() {
                if (!_previewPlaying || !_demoAudioPlayer || _demoAudioPlayer.paused) return;
                const curTime = _demoAudioPlayer.currentTime || 0;
                const maxFaceIdx = faceImgs.length > 0 ? faceImgs.length - 1 : frameCount - 1;
                const idx = previewSpeechFrameIndex(curTime, FPS, PREVIEW_LEAD_SEC, maxFaceIdx);
                if (idx !== lastRenderedIdx) {
                    lastRenderedIdx = idx;
                    if (faceImgs[idx] && faceImgEl) {
                        faceImgEl.src = faceImgs[idx].src;
                    }
                    if (fullImgs[idx] && fullImgEl) {
                        fullImgEl.src = fullImgs[idx].src;
                    }
                }
                requestAnimationFrame(flip);
            }
            requestAnimationFrame(flip);
        }

        const engineText = data.engine === "neural_cloud_latentsync" ? "LatentSync 官方高精扩散模型 (SyncNet顶尖对齐)"
            : data.engine === "neural_cloud_sidecar" ? "云端 GPU 神经渲染"
                : data.engine === "neural_local_onnx" ? "本地 ONNX 神经渲染"
                    : "微动态降级 (神经引擎未就绪)";
        if (statusEl) {
            statusEl.style.color = "#34d399";
            statusEl.innerHTML = `🔊 正在试听 (${data.mean_inference_ms}ms/帧)`;
        }
        updatePreviewRenderStatus(`🔊 <strong>正在试听发音驱动中:</strong> 渲染引擎: <strong>${escapeHtml(engineText)}</strong>${data.device ? ` · ${escapeHtml(formatGpuDeviceDisplayName(data.device))}` : ""} · 单帧 <strong>${data.mean_inference_ms}ms</strong> · 共 ${frameCount} 帧${reusedNote}${partialNote}`, "success", "🔊");
    } catch (e) {
        _previewPlaying = false;
        let msg = e.message || "主播绑定的音色有误，请检查";
        if (e.name === "AbortError") {
            msg = `等待超过 ${Math.round(PREVIEW_DRIVE_TIMEOUT_MS / 60000)} 分钟仍未完成，已中止本次请求。`
                + "通常是首次试听需上传约 10MB 主播资产到云端、隧道带宽过低所致；"
                + "已启用断点续传，再次点击只会补传云端缺失的分块。";
        } else if (msg === "Failed to fetch") {
            msg = "无法连接至后端服务，请确认服务已启动";
        }
        if (statusEl) {
            statusEl.style.color = "#fbbf24";
            const detailTip = e.rawDetail ? ` title="${escapeHtml(e.rawDetail)}"` : "";
            statusEl.innerHTML = `<span${detailTip}>⚠️ <strong>试听驱动失败</strong></span>`;
        }
        updatePreviewRenderStatus(`⚠️ <strong>试听驱动失败：</strong>${escapeHtml(msg)}`, "error", "⚠️");
        if (bboxBox) bboxBox.style.boxShadow = "none";
    } finally {
        // 必须清理定时器，否则会留下悬挂的 interval/timer 持续改写状态条
        clearTimeout(driveTimeoutId);
        clearInterval(driveProgressId);
        if (btnEl) btnEl.disabled = false;
    }
}

function updateSpeechDriveBadge(data) {
    const badge = document.getElementById("slice-face-gpu-badge");
    const syncBadge = document.getElementById("slice-face-sync-badge");
    if (!badge) return;

    if (data.engine === "neural_cloud_latentsync") {
        badge.style.background = "rgba(16, 185, 129, 0.15)";
        badge.style.borderColor = "rgba(16, 185, 129, 0.4)";
        badge.style.color = "#10b981";
        const shortDev = formatGpuDeviceDisplayName(data.device || "云端 GPU");
        badge.innerHTML = `💎 <strong>[LatentSync]</strong> ${escapeHtml(shortDev)} <span style="font-size:9.5px;opacity:0.85;">(${data.mean_inference_ms}ms)</span>`;
        badge.title = `💎 ByteDance LatentSync 官方扩散模型 (SyncNet顶尖对齐)\n设备型号: ${data.device || '云端GPU'}\n单帧耗时: ${data.mean_inference_ms}ms/帧`;
    } else if (data.engine === "neural_cloud_sidecar") {
        badge.style.background = "rgba(56, 189, 248, 0.15)";
        badge.style.borderColor = "rgba(56, 189, 248, 0.35)";
        badge.style.color = "#38bdf8";
        const shortDev = formatGpuDeviceDisplayName(data.device || "远端 GPU");
        badge.innerHTML = `⚡ <strong>[云端]</strong> ${escapeHtml(shortDev)} <span style="font-size:9.5px;opacity:0.85;">(${data.mean_inference_ms}ms)</span>`;
        badge.title = `云端 GPU 神经渲染通道\n设备型号: ${data.device || '远端GPU'}\n单帧耗时: ${data.mean_inference_ms}ms/帧`;
    } else if (data.engine === "neural_local_onnx") {
        badge.style.background = "rgba(16, 185, 129, 0.15)";
        badge.style.borderColor = "rgba(16, 185, 129, 0.35)";
        badge.style.color = "#34d399";
        const shortDev = formatGpuDeviceDisplayName(data.device || "ONNX");
        badge.innerHTML = `🟢 <strong>[独显]</strong> ${escapeHtml(shortDev)} <span style="font-size:9.5px;opacity:0.85;">(${data.mean_inference_ms}ms)</span>`;
        badge.title = `本地显卡深度学习加速\n设备型号: ${data.device || 'ONNX'}\n单帧耗时: ${data.mean_inference_ms}ms/帧`;
    } else {
        // 诚实降级：绝不声称 GPU，引导用户前往设置页
        badge.style.background = "rgba(251, 191, 36, 0.15)";
        badge.style.borderColor = "rgba(251, 191, 36, 0.35)";
        badge.style.color = "#fbbf24";
        const reason = data.fallback_reason === "model_not_installed"
            ? "模型未就绪"
            : (data.fallback_reason === "sidecar_unreachable" ? "云端未连通" : "引擎未就绪");
        badge.innerHTML = `⚠️ <strong>[已降级]</strong> ${escapeHtml(reason)}`;
        badge.title = "检测到当前未启用 GPU 加速，可在【GPU算力配置】中启用。";
    }

    if (syncBadge) {
        syncBadge.style.display = "block";
        syncBadge.innerHTML = `● 真实驱动中 (${data.frame_count || 0} 帧)`;
    }

    const tip = document.getElementById("slice-face-tip");
    if (tip) {
        tip.style.borderColor = "rgba(16, 185, 129, 0.4)";
        tip.style.background = "rgba(16, 185, 129, 0.1)";
        tip.style.color = "#34d399";
        const shortDev = formatGpuDeviceDisplayName(data.device || "");
        tip.innerHTML = `🔊 <strong style="color:#10b981;">神经重绘:</strong> <strong>LatentSync 官方扩散模型</strong>${shortDev ? ` · ${escapeHtml(shortDev)}` : ""} · 单帧 <strong>${data.mean_inference_ms}ms</strong>`;
        tip.title = `完整设备型号: ${data.device || 'N/A'}`;
    }
}


function closeAvatarPreviewModal() {
    _previewPlaying = false;
    stopSliceAnimationPlay();
    const modal = document.getElementById("modal-avatar-preview");
    if (modal) modal.style.display = "none";
    const videoEl = document.getElementById("modal-avatar-video");
    if (videoEl) {
        videoEl.pause();
        videoEl.currentTime = 0;
    }
    if (_demoAudioPlayer) {
        _demoAudioPlayer.pause();
        _demoAudioPlayer = null;
    }
    const badge = document.getElementById("slice-face-sync-badge");
    if (badge) badge.style.display = "none";
    if (modal) { /* 预览徽章在下次试播时由 updateSpeechDriveBadge 重新点亮 */ }
}

// -----------------------------------------------------------------------------
// 加载软硬件达标体检看板数据
// -----------------------------------------------------------------------------
async function loadDigitalHumanHardwareRequirements(forceRefresh = false) {
    const cloudBadge = document.getElementById("hw-cloud-status-badge");
    const cloudDot = document.getElementById("hw-cloud-status-dot");
    const cloudText = document.getElementById("hw-cloud-status-text");

    if (forceRefresh && cloudText) {
        cloudText.innerText = "云端 GPU 算力：正在握手探活...";
        if (cloudDot) cloudDot.style.background = "#38bdf8";
        if (cloudBadge) {
            cloudBadge.style.background = "rgba(56, 189, 248, 0.15)";
            cloudBadge.style.borderColor = "rgba(56, 189, 248, 0.35)";
            cloudBadge.style.color = "#38bdf8";
        }
    }

    try {
        const url = `${API_BASE}/anchors/hardware-requirements?t=${Date.now()}`;
        const res = await fetch(url);
        if (!res.ok) return;
        const json = await res.json();
        if (json.code !== 0 || !json.data) return;

        const data = json.data;
        const stages = data.stages || [];
        const s1 = stages.find(s => s.stage_id === "slicing");
        const s2 = stages.find(s => s.stage_id === "streaming");

        if (s1) {
            const badge1 = document.getElementById("hw-badge-slicing");
            const cur1 = document.getElementById("hw-slicing-current");
            if (badge1) badge1.innerText = s1.status_badge;
            if (cur1) cur1.innerText = `${s1.current_hardware} (${s1.is_qualified ? "性能强劲，大幅达标" : "不足"})`;
        }

        if (s2) {
            const badge2 = document.getElementById("hw-badge-streaming");
            const cur2 = document.getElementById("hw-streaming-current");
            if (badge2) {
                badge2.innerText = s2.status_badge;
                if (!s2.is_qualified) {
                    badge2.style.background = "rgba(245, 158, 11, 0.18)";
                    badge2.style.color = "#fbbf24";
                    badge2.style.border = "1px solid rgba(245, 158, 11, 0.4)";
                } else {
                    badge2.style.background = "rgba(16,185,129,0.2)";
                    badge2.style.color = "#34d399";
                    badge2.style.border = "1px solid rgba(16,185,129,0.4)";
                }
            }
            if (cur2) cur2.innerText = s2.current_hardware;
        }

        // 3. 动态更新云端 GPU 真实算力状态
        const cloud = data.cloud_hardware || {};

        if (cloudBadge && cloudText) {
            if (!cloud.configured) {
                cloudBadge.style.background = "rgba(255, 255, 255, 0.05)";
                cloudBadge.style.borderColor = "rgba(255, 255, 255, 0.15)";
                cloudBadge.style.color = "var(--text-muted)";
                if (cloudDot) cloudDot.style.background = "#94a3b8";
                cloudText.innerText = "云端 GPU 算力：未配置远端节点";
            } else if (cloud.is_reachable) {
                cloudBadge.style.background = "rgba(16, 185, 129, 0.15)";
                cloudBadge.style.borderColor = "rgba(16, 185, 129, 0.35)";
                cloudBadge.style.color = "#34d399";
                if (cloudDot) cloudDot.style.background = "#10b981";
                const hwLabel = (cloud.gpu_name && cloud.vram_gb) ? `${cloud.gpu_name} · ${cloud.vram_gb}GB 显存` : (cloud.display_label || cloud.gpu_name || cloud.device_info || cloud.provider_name || '已连通');
                cloudText.innerText = `云端 GPU 算力：在线就绪 (${hwLabel})`;
            } else {
                cloudBadge.style.background = "rgba(239, 68, 68, 0.12)";
                cloudBadge.style.borderColor = "rgba(239, 68, 68, 0.3)";
                cloudBadge.style.color = "#f87171";
                if (cloudDot) cloudDot.style.background = "#ef4444";
                const errBrief = cloud.error ? (cloud.error.includes("未开机") ? "未开机" : (cloud.error.includes("超时") ? "连接超时" : "离线未连通")) : "离线未连通";
                cloudText.innerText = `云端 GPU 算力：${errBrief} (开播需先启动)`;
            }
        }
    } catch (e) {
        console.warn("加载数字人软硬件达标看板失败:", e);
    }
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

        const formatLiveModeDisplay = (modeVal, platformVal) => {
            if (!modeVal) return "未配置 (请先完成开播向导)";
            let cleanMode = String(modeVal).trim();
            if (cleanMode === "local" || cleanMode === "A" || cleanMode.includes("纯本地")) {
                cleanMode = "纯本地硬件";
            } else if (cleanMode === "hybrid" || cleanMode === "B" || cleanMode === "C" || cleanMode === "D" || cleanMode.includes("云端")) {
                cleanMode = "本地云端混合";
            } else {
                cleanMode = cleanMode.replace(/\s*档\s*$/, "");
            }
            return platformVal ? `${cleanMode} · ${platformVal}` : cleanMode;
        };
        set("stat-mode", formatLiveModeDisplay(d.mode, d.platform));
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
        } catch (e) { }
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
            <td style="white-space: nowrap; padding: 10px 8px;">
                <div class="table-actions" style="display: inline-flex; align-items: center; gap: 4px; flex-wrap: nowrap; white-space: nowrap;">
                    <button class="btn btn-sm btn-primary" style="padding: 2px 7px; font-size: 11px; height: 26px; line-height: 20px; display: inline-flex; align-items: center; gap: 2px; white-space: nowrap; margin: 0;" onclick="testTriggerAvatarAction(${act.action_code})" title="手动触发测试状态机切换">
                        ▶ 测试
                    </button>
                    <button class="btn btn-sm" style="padding: 2px 7px; font-size: 11px; height: 26px; line-height: 20px; display: inline-flex; align-items: center; gap: 2px; white-space: nowrap; margin: 0; background: rgba(147, 51, 234, 0.12); color: #c084fc; border: 1px solid rgba(147, 51, 234, 0.35);" onclick="triggerUploadActionClip('${act.id}')" title="上传高清切片短视频并抽帧">
                        🎬 视频
                    </button>
                    <button class="btn btn-sm" style="padding: 2px 7px; font-size: 11px; height: 26px; line-height: 20px; display: inline-flex; align-items: center; gap: 2px; white-space: nowrap; margin: 0;" onclick="openActionEditCard('${act.id}')" title="编辑动作参数与关键词">
                        ✏ 编辑
                    </button>
                    ${act.action_code !== 0
                ? `<button class="btn btn-sm btn-danger" style="padding: 2px 7px; font-size: 11px; height: 26px; line-height: 20px; display: inline-flex; align-items: center; gap: 2px; white-space: nowrap; margin: 0;" onclick="deleteAvatarAction('${act.id}')" title="删除动作">🗑 删除</button>`
                : `<button class="btn btn-sm" disabled style="padding: 2px 7px; font-size: 11px; height: 26px; line-height: 20px; display: inline-flex; align-items: center; gap: 2px; white-space: nowrap; margin: 0; opacity: 0.25; cursor: not-allowed; border-style: dashed;" title="待机底模不可删除">🗑 删除</button>`
            }
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

// -----------------------------------------------------------------------------
// 形象资产记录 (训练产出登记簿)
// -----------------------------------------------------------------------------
// 背景：服务端 task_manager 每完成一次数字人切片训练就会往 avatars 表登记一条
// 记录（avatar_task_* 前缀），但此前前端没有任何界面能看到这张表 —— 训练测试
// 产生的冗余登记只增不减、无法清理。此处把服务端已有的
// GET /avatars/list 与 DELETE /avatars/{id} 接到界面，让用户能回溯训练产出并
// 安全清理。注意：这些记录**不参与直播渲染**，只有被正式加入主播档案
// (anchors 表) 的资产才会开播。
// -----------------------------------------------------------------------------

const AVATAR_ASSET_TYPE_MAP = {
    image: { label: "形象图", color: "#38bdf8" },
    video: { label: "训练视频", color: "#a78bfa" },
};

function formatAssetTime(iso) {
    if (!iso) return "—";
    // 后端返回 ISO 字符串；避免时区解析差异，仅取日期与时分
    const m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
    if (!m) return String(iso);
    return `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}`;
}

function renderAvatarAssetsTable(rows) {
    const tbody = document.getElementById("avatar-assets-tbody");
    if (!tbody) return;
    tbody.innerHTML = "";

    if (!Array.isArray(rows) || rows.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; color:var(--text-muted); padding: 20px;">暂无形象资产记录（完成一次数字人切片训练后会自动登记）</td></tr>';
        return;
    }

    for (const row of rows) {
        const id = String(row.id || "");
        const typeMeta = AVATAR_ASSET_TYPE_MAP[row.avatar_type] || { label: row.avatar_type || "未知", color: "#94a3b8" };
        const isTask = row.origin === "task";
        const exists = row.asset_exists === true;

        const originBadge = isTask
            ? '<span class="badge-recommend" style="font-size:10px; padding:1px 7px; background:rgba(167,139,250,0.15); color:#c4b5fd; border:1px solid rgba(167,139,250,0.3);">训练任务</span>'
            : '<span class="badge-recommend" style="font-size:10px; padding:1px 7px; background:rgba(56,189,248,0.15); color:#38bdf8; border:1px solid rgba(56,189,248,0.3);">手动上传</span>';

        // 资产状态：缺失的记录仅剩一条空壳登记，可安全清理
        const stateCell = exists
            ? '<span style="color:#34d399; font-size:12px;">● 资产在位</span>'
            : '<span style="color:#fbbf24; font-size:12px;" title="磁盘文件已不存在，仅剩一条登记记录">● 资产缺失</span>';

        tbody.insertAdjacentHTML("beforeend", `
            <tr>
                <td>
                    <div style="font-weight:600; color:var(--text-primary);">${escapeHtml(row.name || "(未命名)")}</div>
                    <div style="font-size:10.5px; color:var(--text-muted); font-family:monospace;">${escapeHtml(id)}</div>
                </td>
                <td><span style="color:${typeMeta.color}; font-size:12px;">${escapeHtml(typeMeta.label)}</span></td>
                <td>${originBadge}</td>
                <td>${stateCell}</td>
                <td style="font-size:12px; color:var(--text-muted);">${escapeHtml(formatAssetTime(row.created_at))}</td>
                <td style="white-space:nowrap;">
                    <button class="btn btn-sm btn-danger" onclick="deleteAvatarAssetRecord('${escapeHtml(id)}', '${escapeHtml(row.name || "")}')"
                        style="padding:2px 10px; font-size:11px;">删除登记</button>
                </td>
            </tr>
        `);
    }
}

async function loadAvatarAssets() {
    const tbody = document.getElementById("avatar-assets-tbody");
    const summary = document.getElementById("avatar-assets-summary");
    if (tbody) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; color:var(--text-muted); padding: 16px;">正在加载形象资产记录...</td></tr>';
    }
    try {
        const res = await fetch(`${API_BASE}/avatars/list`);
        const json = await res.json();
        if (json.code !== 0) {
            throw new Error(json.detail || json.message || "接口返回异常");
        }
        const rows = json.data || [];
        renderAvatarAssetsTable(rows);

        if (summary) {
            const missing = rows.filter((r) => r.asset_exists !== true).length;
            const fromTask = rows.filter((r) => r.origin === "task").length;
            summary.textContent = `共 ${rows.length} 条 · 训练登记 ${fromTask} 条 · 资产缺失 ${missing} 条`;
        }
    } catch (e) {
        console.error("加载形象资产记录失败:", e);
        if (tbody) {
            tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; color:#f87171; padding: 16px;">加载失败：${escapeHtml(String(e.message || e))}</td></tr>`;
        }
    }
}

async function deleteAvatarAssetRecord(avatarId, name) {
    if (!avatarId) return;
    if (!confirm(`确定要删除形象资产登记「${name || avatarId}」吗？\n\n· 仅删除登记条目与该条目指向的磁盘文件\n· 不影响【全部主播档案】中的主播，也不影响正在直播的数字人\n· 若该资产仍在使用，请勿删除`)) {
        return;
    }
    try {
        const res = await fetch(`${API_BASE}/avatars/${encodeURIComponent(avatarId)}`, {
            method: "DELETE",
        });
        const json = await res.json();
        if (json.code === 0) {
            alert("形象资产登记已删除");
            loadAvatarAssets();
        } else {
            alert(json.detail || json.message || "删除失败");
        }
    } catch (e) {
        alert("删除形象资产登记失败: " + e);
    }
}

// -----------------------------------------------------------------------------
// 绿幕 3 段式实拍 SOP 指南与 OBS/抖音直播伴侣导播助手交互
// -----------------------------------------------------------------------------

function openShootingGuideModal() {
    const modal = document.getElementById("modal-shooting-guide");
    if (modal) {
        modal.style.display = "flex";
    }
}

function closeShootingGuideModal() {
    const modal = document.getElementById("modal-shooting-guide");
    if (modal) {
        modal.style.display = "none";
    }
}

function openStudioHelperModal() {
    const modal = document.getElementById("modal-studio-helper");
    if (modal) {
        modal.style.display = "flex";
    }
}

function closeStudioHelperModal() {
    const modal = document.getElementById("modal-studio-helper");
    if (modal) {
        modal.style.display = "none";
    }
}

function copyObsSettings() {
    const params = `【OBS Studio 色度键最佳抠像推荐参数】\n` +
        `• 关键颜色类型: 绿色 (Green)\n` +
        `• 相似度 (Similarity): 400\n` +
        `• 平滑 (Smoothness): 80\n` +
        `• 主溢出减少 (Key Color Spill Reduction): 100\n` +
        `• 不透明度: 100%\n` +
        `• 对比度/亮度: 保持默认 0.00\n` +
        `• 音频人声闪避: 侧链监听(Sidechain) 阈值 -24dB, 比率 4:1, 衰减 15dB`;
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(params).then(() => {
            alert("📋 OBS 色度键与演播室参数已成功复制到剪贴板！可以直接粘贴参考。");
        }).catch(() => {
            prompt("请手动复制以下参数：", params);
        });
    } else {
        prompt("请手动复制以下参数：", params);
    }
}

// ============================================================================
// 🎭 数字人动作切片状态机与带货手势管理交互逻辑 (阶段三)
// ============================================================================

let _currentActionAnchorId = null;
let _currentActionAnchorName = "";

// 打开主播动作管理弹窗
async function openAnchorActionsModal(anchorId, anchorName) {
    _currentActionAnchorId = anchorId;
    _currentActionAnchorName = anchorName || "未命名主播";

    const nameEl = document.getElementById("modal-actions-anchor-name");
    if (nameEl) nameEl.textContent = _currentActionAnchorName;

    const modal = document.getElementById("modal-anchor-actions");
    if (modal) {
        modal.style.display = "flex";
    }

    switchActionInputTab("split");
    await loadAnchorActions(anchorId);
}

// 关闭主播动作管理弹窗
function closeAnchorActionsModal() {
    const modal = document.getElementById("modal-anchor-actions");
    if (modal) {
        modal.style.display = "none";
    }
}

// 切换添加切片的模式 Tab (split / upload)
function switchActionInputTab(tab) {
    const btnSplit = document.getElementById("btn-tab-split-segments");
    const btnUpload = document.getElementById("btn-tab-upload-single");
    const pnlSplit = document.getElementById("panel-action-split");
    const pnlUpload = document.getElementById("panel-action-upload");

    if (tab === "split") {
        if (btnSplit) {
            btnSplit.style.background = "rgba(56,189,248,0.15)";
            btnSplit.style.color = "#38bdf8";
            btnSplit.style.borderColor = "rgba(56,189,248,0.4)";
        }
        if (btnUpload) {
            btnUpload.style.background = "transparent";
            btnUpload.style.color = "var(--text-muted)";
            btnUpload.style.borderColor = "rgba(255,255,255,0.15)";
        }
        if (pnlSplit) pnlSplit.style.display = "block";
        if (pnlUpload) pnlUpload.style.display = "none";
    } else {
        if (btnUpload) {
            btnUpload.style.background = "rgba(56,189,248,0.15)";
            btnUpload.style.color = "#38bdf8";
            btnUpload.style.borderColor = "rgba(56,189,248,0.4)";
        }
        if (btnSplit) {
            btnSplit.style.background = "transparent";
            btnSplit.style.color = "var(--text-muted)";
            btnSplit.style.borderColor = "rgba(255,255,255,0.15)";
        }
        if (pnlSplit) pnlSplit.style.display = "none";
        if (pnlUpload) pnlUpload.style.display = "block";
    }
}

// 加载指定主播的动作切片列表
async function loadAnchorActions(anchorId) {
    const container = document.getElementById("modal-actions-list");
    const badge = document.getElementById("modal-actions-count-badge");
    if (!container) return;

    container.innerHTML = '<div style="color:var(--text-muted); font-size:12px; padding:20px; text-align:center; grid-column:1/-1;">正在加载主播动作切片...</div>';

    try {
        const res = await fetch(`${API_BASE}/anchors/${anchorId}/actions`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const json = await res.json();
        const actions = (json && json.data) ? json.data : [];

        if (badge) badge.textContent = `${actions.length} 组`;

        if (actions.length === 0) {
            container.innerHTML = `
                <div style="color:var(--text-muted); font-size:12px; padding:20px; text-align:center; grid-column:1/-1; background:rgba(0,0,0,0.2); border-radius:8px;">
                    暂无专属动作切片，系统正采用全局默认状态机规则。<br>
                    建议使用下方【单视频多时间戳智能拆解】一键生成待机、小黄车与致谢手势。
                </div>
            `;
            return;
        }

        container.innerHTML = actions.map(act => {
            const isIdle = act.action_code === 0;
            const codeTag = `<span style="font-family:monospace; background:rgba(255,255,255,0.08); padding:1px 6px; border-radius:4px; font-size:11px;">#${act.action_code}</span>`;
            const priorityTag = `<span style="font-size:10px; color:#38bdf8; background:rgba(56,189,248,0.1); padding:1px 5px; border-radius:3px;">优:${act.priority}</span>`;
            const durationTag = `<span style="font-size:10px; color:#fbbf24; background:rgba(245,158,11,0.1); padding:1px 5px; border-radius:3px;">${act.duration_sec}s</span>`;
            const framesTag = act.frames_count > 0
                ? `<span style="font-size:11px; color:#34d399;">✓ ${act.frames_count} 帧 (神经就绪)</span>`
                : `<span style="font-size:11px; color:var(--text-muted);">⚪ 待提取切片</span>`;

            const previewImg = act.preview_url
                ? `<img src="${act.preview_url}" style="width:100%; height:90px; object-fit:cover; border-radius:4px; margin-bottom:8px; border:1px solid rgba(255,255,255,0.08);" alt="动作预览">`
                : `<div style="width:100%; height:90px; background:rgba(0,0,0,0.4); border-radius:4px; margin-bottom:8px; display:flex; align-items:center; justify-content:center; color:var(--text-muted); font-size:24px; border:1px solid rgba(255,255,255,0.05);">🎬</div>`;

            const delBtn = isIdle
                ? `<span style="font-size:11px; color:var(--text-muted);">核心基底</span>`
                : `<button class="btn btn-sm btn-danger" style="padding:2px 6px; font-size:11px;" onclick="deleteAnchorAction('${act.id}')">删除</button>`;

            const kwText = act.trigger_keywords ? escapeHtml(act.trigger_keywords) : '<span style="color:var(--text-muted);">无</span>';

            return `
                <div style="background:rgba(0,0,0,0.3); border:1px solid rgba(255,255,255,0.08); border-radius:8px; padding:12px; display:flex; flex-direction:column; justify-content:space-between;">
                    <div>
                        ${previewImg}
                        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
                            <strong style="color:#f8fafc; font-size:13px;">${escapeHtml(act.action_name)}</strong>
                            <div style="display:flex; gap:4px; align-items:center;">${codeTag} ${priorityTag} ${durationTag}</div>
                        </div>
                        <div style="font-size:11px; color:var(--text-muted); margin-bottom:6px; line-height:1.4;">
                            触发词: ${kwText}
                        </div>
                    </div>
                    <div style="display:flex; justify-content:space-between; align-items:center; border-top:1px solid rgba(255,255,255,0.06); padding-top:8px; margin-top:6px;">
                        ${framesTag}
                        ${delBtn}
                    </div>
                </div>
            `;
        }).join("");
    } catch (e) {
        console.error("加载动作切片列表失败:", e);
        container.innerHTML = `<div style="color:#f87171; font-size:12px; padding:20px; text-align:center; grid-column:1/-1;">加载动作切片列表失败: ${e.message}</div>`;
    }
}

// 提交多时间戳智能拆解
async function submitSplitVideoActions() {
    if (!_currentActionAnchorId) {
        showToast("未指定有效主播", "error");
        return;
    }

    const idleStart = parseFloat(document.getElementById("split-idle-start")?.value || 0);
    const idleEnd = parseFloat(document.getElementById("split-idle-end")?.value || 25);
    const cartStart = parseFloat(document.getElementById("split-cart-start")?.value || 25);
    const cartEnd = parseFloat(document.getElementById("split-cart-end")?.value || 40);
    const thanksStart = parseFloat(document.getElementById("split-thanks-start")?.value || 40);
    const thanksEnd = parseFloat(document.getElementById("split-thanks-end")?.value || 55);

    const segments = [
        { action_code: 0, name: "待机呼吸循环", start_sec: idleStart, end_sec: idleEnd, keywords: "", priority: 0 },
        { action_code: 3, name: "促单指引小黄车", start_sec: cartStart, end_sec: cartEnd, keywords: "购物车,下单,左下角,抢购,拍下", priority: 5 },
        { action_code: 4, name: "大额打赏致谢", start_sec: thanksStart, end_sec: thanksEnd, keywords: "感谢,礼物,破费,大气,老板大气", priority: 9 }
    ];

    const btn = document.getElementById("btn-submit-split-actions");
    const origText = btn ? btn.textContent : "";
    if (btn) {
        btn.disabled = true;
        btn.textContent = "⏳ 正在并行裁剪并提取关键帧与口型坐标，请稍候...";
    }

    try {
        const res = await fetch(`${API_BASE}/anchors/${_currentActionAnchorId}/actions/split-segments`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ segments })
        });
        const json = await res.json();
        if (json.code === 0) {
            showToast(json.message || "多动作切片已成功拆解生成！", "success");
            await loadAnchorActions(_currentActionAnchorId);
            loadAnchors(); // 同步刷新主播表格
        } else {
            showToast(`拆解失败: ${json.detail || json.message || "未知错误"}`, "error");
        }
    } catch (e) {
        console.error("提交分段拆解异常:", e);
        showToast(`提交异常: ${e.message}`, "error");
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = origText;
        }
    }
}

// 提交上传单动作视频切片
async function submitSingleActionUpload() {
    if (!_currentActionAnchorId) {
        showToast("未指定有效主播", "error");
        return;
    }

    const fileInput = document.getElementById("single-action-file");
    if (!fileInput || !fileInput.files || fileInput.files.length === 0) {
        showToast("请选择视频切片文件", "warning");
        return;
    }

    const file = fileInput.files[0];
    const code = parseInt(document.getElementById("single-action-code")?.value || 3);
    const keywords = document.getElementById("single-action-keywords")?.value || "";

    const nameMap = {
        0: "基础待机呼吸",
        1: "热情挥手欢迎",
        2: "求关注与点赞",
        3: "促单指引小黄车",
        4: "大额打赏致谢"
    };

    const formData = new FormData();
    formData.append("file", file);
    formData.append("action_code", code);
    formData.append("action_name", nameMap[code] || `动作_${code}`);
    formData.append("trigger_keywords", keywords);
    formData.append("duration_sec", 3.5);
    formData.append("priority", code === 3 ? 5 : (code === 4 ? 9 : 2));

    const btn = document.getElementById("btn-submit-single-action");
    const origText = btn ? btn.textContent : "";
    if (btn) {
        btn.disabled = true;
        btn.textContent = "⏳ 正在提取神经切片与面部坐标...";
    }

    try {
        const res = await fetch(`${API_BASE}/anchors/${_currentActionAnchorId}/actions/upload`, {
            method: "POST",
            body: formData
        });
        const json = await res.json();
        if (json.code === 0) {
            showToast("动作切片上传并处理完成 ✓", "success");
            fileInput.value = "";
            await loadAnchorActions(_currentActionAnchorId);
            loadAnchors();
        } else {
            showToast(`上传失败: ${json.detail || json.message || "未知错误"}`, "error");
        }
    } catch (e) {
        console.error("上传单动作切片异常:", e);
        showToast(`上传异常: ${e.message}`, "error");
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = origText;
        }
    }
}

// 删除指定动作切片
async function deleteAnchorAction(actionId) {
    if (!confirm("确认删除该动作切片及其神经资产吗？")) return;

    try {
        const res = await fetch(`${API_BASE}/anchors/avatar/actions/${actionId}`, {
            method: "DELETE"
        });
        const json = await res.json();
        if (json.code === 0) {
            showToast("动作切片已删除", "info");
            if (_currentActionAnchorId) {
                await loadAnchorActions(_currentActionAnchorId);
            }
            loadAnchors();
        } else {
            showToast(`删除失败: ${json.detail || json.message || "未知错误"}`, "error");
        }
    } catch (e) {
        console.error("删除动作切片失败:", e);
        showToast(`删除异常: ${e.message}`, "error");
    }
}



