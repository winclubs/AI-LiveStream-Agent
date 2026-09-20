let selectedWizardAnchorId = "";

async function initWizard() {
    await renderModeCards();
    await renderWizardRoleCards();
    await restoreWizardState();
    await renderWizardAnchorSelect(wizardRoleType);
}

async function restoreWizardState() {
    try {
        // 恢复今日直播主题
        try {
            const themeRes = await fetch(`${API_BASE}/settings/live-theme`);
            const themeJson = await themeRes.json();
            const themeInput = document.getElementById("wizard-theme");
            if (themeJson.code === 0 && themeInput) themeInput.value = themeJson.data.theme || "";
        } catch (e) { /* 主题恢复失败不影响主流程 */ }

        const res = await fetch(`${API_BASE}/settings/live-mode`);
        const json = await res.json();
        if (json.code === 0 && json.data) {
            currentMode = json.data.mode;
            if (json.data.selected_anchor_id) {
                selectedWizardAnchorId = json.data.selected_anchor_id;
            }
            if (json.data.mode_info) renderCurrentModeBadge(json.data.mode_info);
            if (json.data.mode) {
                // 已保存过模式：恢复用户上次的选择
                wizardMode = json.data.mode;
                document.querySelectorAll("#mode-cards .mode-card").forEach(c => {
                    c.classList.toggle("selected", c.getAttribute("data-mode") === json.data.mode);
                });
                // 向导已完成：直接刷新建议区
                refreshWizardSuggestions();
            } else {
                // 首次使用：根据硬件配置自动默认选中推荐模式
                await applyHardwareRecommendation();
            }
        }
    } catch (e) { console.warn("向导状态恢复失败", e); }
}

// 根据本机硬件自动默认选择运行模式 (首次进入向导时)
async function applyHardwareRecommendation() {
    try {
        const res = await fetch(`${API_BASE}/settings/recommended-mode`);
        const json = await res.json();
        if (json.code !== 0) return;
        const d = json.data;

        wizardMode = d.mode;
        document.querySelectorAll("#mode-cards .mode-card").forEach(c => {
            c.classList.toggle("selected", c.getAttribute("data-mode") === d.mode);
        });

        // 展示推荐理由横幅
        const banner = document.getElementById("hw-recommend-banner");
        const reason = document.getElementById("hw-recommend-reason");
        if (banner && reason) {
            const gpu = d.gpu || {};
            const gpuLine = gpu.gpu_name
                ? `硬件检测结果: ${gpu.gpu_name} (显存 ${gpu.vram_total_gb}GB · ${gpu.cuda_available ? "CUDA 可用" : "无 CUDA 加速"})`
                : "硬件检测结果: 未检测到可用的 NVIDIA 独立显卡";
            reason.innerHTML = `${gpuLine}<br>${d.reason}`;
            banner.style.display = "block";
        }
        refreshWizardSuggestions();
    } catch (e) {
        console.warn("硬件推荐获取失败，保持默认 A 档", e);
    }
}

async function renderModeCards() {
    const box = document.getElementById("mode-cards");
    if (!box) return;
    try {
        const res = await fetch(`${API_BASE}/settings/modes`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const json = await res.json();
        if (json.code !== 0) throw new Error(json.detail || json.message || "接口异常");
        box.innerHTML = "";
        json.data.forEach(m => {
            const card = document.createElement("div");
            card.className = "mode-card" + (m.code === wizardMode ? " selected" : "");
            card.setAttribute("data-mode", m.code);
            card.onclick = () => selectWizardMode(m.code);
            card.innerHTML = `
                <div style="display:flex; justify-content: space-between; align-items:center;">
                    <strong style="font-size:14px;">${m.code} · ${m.name}</strong>
                    ${m.code === "C" ? '<span class="badge-recommend">强烈推荐</span>' : (m.code === "A" ? '<span class="badge-optional">默认</span>' : '')}
                </div>
                <div class="mode-card-meta">
                    <div class="row">${svg("cpu", "icon")}<span>硬件: ${m.hardware}</span></div>
                    <div class="row">${svg("cloud", "icon")}<span>大脑: ${m.llm}</span></div>
                    <div class="row">${svg("mic", "icon")}<span>声音: ${m.tts}</span></div>
                    <div class="row">${svg("monitor", "icon")}<span>画面: ${m.avatar}</span></div>
                    <div class="row" style="color: var(--amber);">${svg("dollar", "icon")}<span>成本: ${m.cost}</span></div>
                    <div class="row" style="color: var(--text-muted);">${svg("user", "icon")}<span>适合: ${m.audience}</span></div>
                </div>
            `;
            box.appendChild(card);
        });
    } catch (e) {
        console.error("加载模式失败", e);
        box.innerHTML = `
            <div style="grid-column: 1 / -1; padding: 20px; text-align: center; color: var(--accent-danger); border: 1px dashed var(--accent-danger); border-radius: 10px;">
                <div style="font-weight: 700; margin-bottom: 8px;">直播模式列表加载失败 (${e.message})</div>
                <div style="font-size: 12px; color: var(--text-secondary); margin-bottom: 12px;">
                    多为本地服务未启动或为旧版本进程。请关闭旧的启动窗口，重新运行 run_agent.bat 后刷新页面。
                </div>
                <button class="btn btn-sm btn-primary" onclick="renderModeCards()">重新加载</button>
            </div>
        `;
    }
}

async function renderWizardRoleCards() {
    const box = document.getElementById("wizard-role-cards");
    if (!box) return;
    const roles = [
        {
            type: "ecommerce",
            icon: "orange",
            avatarSvg: "/static/svg/anchor_ecommerce.svg",
            name: "带货主播",
            color: "#F97316",
            desc: "促单逼单 · 商品轮播 · 优惠券逼单 · 实时库存播报"
        },
        {
            type: "entertainment",
            icon: "mic",
            avatarSvg: "/static/svg/anchor_entertainment.svg",
            name: "娱乐主播",
            color: "#2DD4A0",
            desc: "逗梗陪伴 · 高情商共情 · 脑筋急转弯 · 打赏花式答谢"
        },
        {
            type: "expert",
            icon: "plus-circle",
            avatarSvg: "/static/svg/anchor_expert.svg",
            name: "专业专家",
            color: "#EF4444",
            desc: "咨询法理前置免责 · 双路 RAG 检索 · 严谨分点解答"
        },
        {
            type: "chitchat",
            icon: "handshake",
            avatarSvg: "/static/svg/anchor_chitchat.svg",
            name: "闲聊扯淡",
            color: "#F59E0B",
            desc: "唠嗑搭子 · 顺话茬接梗 · 话题不断 · 轻松不冷场"
        }
    ];
    box.innerHTML = "";
    roles.forEach(r => {
        const card = document.createElement("div");
        card.className = "mode-card" + (r.type === wizardRoleType ? " selected" : "");
        card.setAttribute("data-role", r.type);
        card.onclick = () => selectWizardRole(r.type);

        const avatarEl = r.avatarSvg
            ? `<img src="${r.avatarSvg}" alt="${r.name}" style="width: 52px; height: 52px; border-radius: 50%; border: 2.5px solid ${r.color}; display: inline-block; box-shadow: 0 4px 12px ${r.color}33; background: #141416;">`
            : svg(r.icon, "icon-xl");

        card.innerHTML = `
            <div style="text-align: center; padding: 6px 0 3px 0;">
                ${avatarEl}
            </div>
            <div class="mode-card-name" style="text-align: center; color: ${r.color}; font-weight: 700; margin-top: 6px; margin-bottom: 6px;">
                ${r.name}
            </div>
            <div class="mode-card-desc" style="text-align: center; line-height: 1.5;">${r.desc}</div>
        `;
        box.appendChild(card);
    });
}

function selectWizardMode(code) {
    wizardMode = code;
    document.querySelectorAll("#mode-cards .mode-card").forEach(c => {
        c.classList.toggle("selected", c.getAttribute("data-mode") === code);
    });
    refreshWizardSuggestions();
}

const ROLE_DEFAULT_THEMES = {
    ecommerce: {
        theme: "爆款好物专场 · 限时直降与库存福利",
        placeholder: "如：爆款好物专场 · 限时直降手慢无 / 春季新品首发大促"
    },
    entertainment: {
        theme: "深夜情感树洞 · 聊聊你最近单曲循环的一首歌",
        placeholder: "如：深夜情感树洞 · 聊聊你最近单曲循环的一首歌 / 周末欢唱连麦"
    },
    expert: {
        theme: "法律热点剖析 · 劳动争议与维权必知法条解读",
        placeholder: "如：劳动争议与合同纠纷深度答疑 · 消费者权益保护普法专场"
    },
    chitchat: {
        theme: "轻松唠嗑茶话会 · 今天你遇到了什么开心的事",
        placeholder: "如：生活日常碎碎念 · 吐槽奇葩经历 / 聊聊各地家常美食"
    }
};

function selectWizardRole(roleType) {
    wizardRoleType = roleType;
    document.querySelectorAll("#wizard-role-cards .mode-card").forEach(c => {
        c.classList.toggle("selected", c.getAttribute("data-role") === roleType);
    });

    // 核心联动：选择不同主播角色，下方的“今日直播主题”输入框与占位提示自动切换
    const themeInput = document.getElementById("wizard-theme");
    if (themeInput) {
        const themeConfig = ROLE_DEFAULT_THEMES[roleType];
        if (themeConfig) {
            themeInput.value = themeConfig.theme;
            themeInput.placeholder = themeConfig.placeholder;
        }
    }

    refreshWizardSuggestions();
    renderWizardAnchorSelect(roleType);
}

async function renderWizardAnchorSelect(roleType) {
    const container = document.getElementById("wizard-anchor-select-container");
    if (!container) return;

    try {
        const res = await fetch(`${API_BASE}/anchors/list`);
        const json = await res.json();
        const allAnchors = (json.code === 0 && Array.isArray(json.data)) ? json.data : [];
        if (!selectedWizardAnchorId) {
            selectedWizardAnchorId = json.selected_anchor_id || window._currentLiveAnchorId || "";
        }

        const matchedAnchors = allAnchors.filter(a => {
            const t = (a.anchor_type || "").toLowerCase();
            if (roleType === "chitchat") return t === "chitchat" || t === "chat";
            return t === roleType.toLowerCase();
        });

        const typeLabels = {
            ecommerce: "带货主播",
            entertainment: "娱乐主播",
            expert: "专业专家",
            chitchat: "闲聊扯淡"
        };
        const currentTypeName = typeLabels[roleType] || "当前类型";

        if (matchedAnchors.length === 0) {
            container.innerHTML = `
                <div style="display: flex; justify-content: space-between; align-items: center; background: rgba(30, 41, 59, 0.4); border: 1px dashed rgba(148, 163, 184, 0.25); border-radius: 8px; padding: 12px 16px;">
                    <div style="font-size: 12.5px; color: var(--text-secondary);">
                        💡 当前「<strong style="color:var(--text-primary);">${currentTypeName}</strong>」下暂未录入专属主播档案，开播将直接采用系统默认人设与音色。
                    </div>
                    <button type="button" class="btn btn-sm" onclick="switchToTab('anchors')" style="font-size: 12px; color: var(--accent-emerald); border-color: rgba(16,185,129,0.3); padding: 4px 12px;">
                        前往「主播管理」添加 ↗
                    </button>
                </div>
            `;
            return;
        }

        if (!selectedWizardAnchorId || !matchedAnchors.some(a => a.id === selectedWizardAnchorId)) {
            selectedWizardAnchorId = matchedAnchors[0].id;
        }

        container.innerHTML = `
            <div style="margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
                <label style="font-size: 13px; font-weight: 600; color: var(--text-primary); display: flex; align-items: center; gap: 6px;">
                    <svg class="icon-sm" viewBox="0 0 24 24" style="width: 14px; height: 14px;"><circle cx="12" cy="7" r="4"/><path d="M6 21v-2a6 6 0 0 1 12 0v2"/></svg>
                    选择「${currentTypeName}」类型的出镜主播：
                </label>
                <span style="font-size: 12px; color: var(--text-muted);">共 ${matchedAnchors.length} 位对应类型主播，点击指定出镜档案</span>
            </div>
            <div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 10px;">
                ${matchedAnchors.map(a => {
                    const isSelected = a.id === selectedWizardAnchorId;
                    const avatarSrc = a.photo_portrait || a.photo_full_body || (ROLE_CARD_META[roleType] ? ROLE_CARD_META[roleType].avatarSvg : "/static/svg/default_avatar.svg");
                    return `
                        <div class="wizard-anchor-card ${isSelected ? 'selected' : ''}" onclick="selectWizardAnchor('${a.id}')" style="display: flex; align-items: center; gap: 10px; padding: 8px 12px; border-radius: 8px; cursor: pointer; transition: all 0.2s; background: ${isSelected ? 'rgba(16, 185, 129, 0.12)' : 'rgba(30, 41, 59, 0.5)'}; border: 1.5px solid ${isSelected ? '#10B981' : 'rgba(148, 163, 184, 0.2)'}; box-shadow: ${isSelected ? '0 2px 10px rgba(16, 185, 129, 0.2)' : 'none'};">
                            <img src="${avatarSrc}" style="width: 36px; height: 36px; border-radius: 50%; object-fit: cover; border: 1.5px solid ${isSelected ? '#10B981' : 'rgba(148, 163, 184, 0.3)'};">
                            <div style="flex: 1; min-width: 0;">
                                <div style="font-size: 13px; font-weight: 700; color: ${isSelected ? '#10B981' : 'var(--text-primary)'}; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">
                                    ${escapeHtml(a.name)}
                                </div>
                                <div style="font-size: 11px; color: var(--text-secondary); margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">
                                    音色: ${escapeHtml(a.voice_name || '默认音色')}
                                </div>
                            </div>
                            <div style="font-size: 12px; color: ${isSelected ? '#10B981' : 'transparent'}; font-weight: 700;">
                                ✓
                            </div>
                        </div>
                    `;
                }).join("")}
            </div>
        `;
    } catch (e) {
        console.error("加载向导主播列表失败", e);
    }
}

function selectWizardAnchor(anchorId) {
    selectedWizardAnchorId = anchorId;
    renderWizardAnchorSelect(wizardRoleType);
}

async function refreshWizardSuggestions() {
    const box = document.getElementById("wizard-suggestion-box");
    if (!box) return;
    try {
        const res = await fetch(`${API_BASE}/roles/suggestions?role_type=${wizardRoleType}&mode=${wizardMode}`);
        const json = await res.json();
        if (json.code !== 0) return;
        const d = json.data;
        box.innerHTML = `
            <div style="padding: 12px; background: rgba(16,185,129,0.08); border: 1px solid rgba(16,185,129,0.3); border-radius: 8px;">
                <div style="font-weight: 700; font-size: 13px; color: var(--accent-emerald);">【${d.label}】 × 模式 ${wizardMode} 运营建议:</div>
                <ul style="font-size: 12px; color: var(--text-secondary); margin: 8px 0 0 18px; line-height: 1.8;">
                    ${d.tips.map(t => `<li>${t}</li>`).join("")}
                </ul>
            </div>
        `;
        document.getElementById("wizard-constraint").value = d.constraint_prompt;

        const themeInput = document.getElementById("wizard-theme");
        if (themeInput) {
            if (d.default_theme) {
                themeInput.value = d.default_theme;
            }
            if (d.theme_placeholder) {
                themeInput.placeholder = d.theme_placeholder;
            }
        }
    } catch (e) { console.error("加载建议失败", e); }
}

async function completeWizard() {
    const status = document.getElementById("wizard-save-status");
    if (isLiveStreaming) {
        alert("直播进行中，禁止修改直播模式与角色配置！请先停止直播。");
        return;
    }
    const constraint = document.getElementById("wizard-constraint").value.trim();
    try {
        // 1. 保存直播模式
        const modeRes = await fetch(`${API_BASE}/settings/live-mode`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ mode: wizardMode })
        });
        const modeJson = await modeRes.json();
        if (modeJson.code !== 0) { alert(modeJson.detail || "模式保存失败"); return; }

        // 1.5 保存今日直播主题
        const themeInput = document.getElementById("wizard-theme");
        const themeVal = themeInput ? themeInput.value.trim() : "";
        await fetch(`${API_BASE}/settings/live-theme`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ theme: themeVal })
        });

        // 2. 将约束提示词写入所选角色的 system_prompt
        const listRes = await fetch(`${API_BASE}/roles/list`);
        const listJson = await listRes.json();
        const role = (listJson.data || []).find(r => r.role_type === wizardRoleType);
        if (role) {
            let prompt = role.system_prompt || "";
            // 移除旧的话题约束段落后追加最新约束
            prompt = prompt.split("【话题约束】")[0].trim();
            if (constraint) prompt = (prompt + "\n" + constraint).trim();
            await fetch(`${API_BASE}/roles/upsert`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    id: role.id, role_type: role.role_type, role_name: role.name,
                    system_prompt: prompt, speech_speed: role.speech_speed || 1.0,
                    pitch_shift: role.pitch_shift || 0.0, associated_guardrail_group: role.role_type
                })
            });
            // 3. 激活该角色为当前直播角色
            await fetch(`${API_BASE}/roles/switch`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ role_id: role.id })
            });
        }

        // 4. 持久化保存向导中选定的出镜主播档案
        if (selectedWizardAnchorId) {
            try {
                await fetch(`${API_BASE}/settings/selected-anchor`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ anchor_id: selectedWizardAnchorId })
                });
                window._currentLiveAnchorId = selectedWizardAnchorId;
            } catch (_) {}
        }

        if (status) status.innerText = "配置已保存：直播模式与主播角色约束已生效！";
        currentMode = wizardMode;
        const modes = await fetch(`${API_BASE}/settings/modes`);
        const modesJson = await modes.json();
        const modeInfo = (modesJson.data || []).find(m => m.code === wizardMode);
        if (modeInfo) renderCurrentModeBadge(modeInfo);
        loadSettings();  // 按新模式刷新配置中心
        loadRoles();
        loadLiveStats();

        // 完成配置 → 立即执行开播前真实检查，报告直接呈现在向导页 (需求: 保存后检查)
        if (status) status.innerText = "✓ 配置已保存，正在做开播前检查...";
        showToast("配置已保存！正在检查开播条件...", "info");
        await runPreflight({ auto: false });
        if (status) status.innerText = "";
    } catch (e) {
        alert("保存异常: " + e);
    }
}

// 编程式切换导航选项卡
function switchToTab(tabName) {
    document.querySelectorAll(".nav-item").forEach(n => n.classList.toggle("active", n.getAttribute("data-tab") === tabName));
    document.querySelectorAll(".tab-content").forEach(tc => tc.classList.remove("active"));
    const target = document.getElementById(`tab-${tabName}`);
    if (target) target.classList.add("active");
    if (tabName === "settings") loadSettings();
}

// ============================================================================
// 开播前检查 (Preflight)：真实探测开播条件，未通过不盲目开播 (v1.1.3)
// ============================================================================
let lastPreflightData = null;

async function runPreflight(opts = {}) {
    try {
        const res = await fetch(`${API_BASE}/live/preflight`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const json = await res.json();
        if (json.code !== 0 || !json.data || !Array.isArray(json.data.checks)) {
            throw new Error(json.detail || json.message || "检查响应格式无效");
        }
        lastPreflightData = json.data;

        const fails = json.data.checks.filter(c => c.status === "fail").length;
        const warns = json.data.checks.filter(c => c.status === "warn").length;

        // 开播触发的自动检查：全部通过时静默放行，否则弹报告让用户处理
        if (opts.auto && fails === 0 && warns === 0) {
            showToast("开播前检查全部通过 ✓ 正在启动直播...", "success");
            return json.data;
        }
        renderPreflightModal(json.data, opts);
        return json.data;
    } catch (e) {
        showToast("开播前检查请求异常: " + e, "error");
        return null;
    }
}

function pfRowHtml(c) {
    const icons = { pass: "check", warn: "warn", fail: "x" };
    const statusLabels = { pass: "检测通过", warn: "建议修复", fail: "必须修复" };
    const badgeClass = c.status === "pass" ? "pill-pass" : (c.status === "warn" ? "pill-warning" : "pill-missing");
    const fixHtml = (c.status !== "pass" && (c.fix_hint || c.action_tab))
        ? `<div class="pf-fix">
             <div class="pf-fix-left">
               ${svg("info", "icon-sm")}
               <span>${c.fix_hint || ""}</span>
             </div>
             ${c.action_tab ? `<button type="button" class="pf-btn-fix" onclick="preflightGoFix('${c.action_tab}')">去处理 →</button>` : ""}
           </div>`
        : "";
    return `
        <div class="pf-row pf-${c.status}">
            <div class="pf-icon">${svg(icons[c.status] || "info")}</div>
            <div style="flex: 1; min-width: 0;">
                <div style="display: flex; align-items: center; justify-content: space-between; gap: 8px;">
                    <div class="pf-title">${c.title}</div>
                    <span class="prereq-pill ${badgeClass}" style="font-size: 11px; padding: 2px 8px;">
                        <span class="prereq-dot"></span>
                        ${statusLabels[c.status] || c.status}
                    </span>
                </div>
                <div class="pf-msg">${c.message}</div>
                ${fixHtml}
            </div>
        </div>
    `;
}


function renderPreflightModal(data, opts = {}) {
    const modal = document.getElementById("preflight-modal");
    const checksBox = document.getElementById("preflight-modal-checks");
    const summaryEl = document.getElementById("preflight-modal-summary");
    const actionsEl = document.getElementById("preflight-modal-actions");
    if (!modal || !checksBox) return;

    checksBox.innerHTML = data.checks.map(pfRowHtml).join("");
    if (summaryEl) summaryEl.innerText = data.summary;

    // 操作按钮区按状态人性化组装
    let buttons = "";
    if (!isLiveStreaming && data.ready) {
        const label = data.checks.some(c => c.status === "warn")
            ? "仍有建议项，坚持开播"
            : "▶ 启动本地直播源";
        buttons += `<button class="btn btn-primary" style="font-weight: 700; padding: 7px 20px;" onclick="preflightStartLive()">${label}</button>`;
    }
    if (!data.ready) {
        buttons += `
            <div class="pf-modal-warning-bar" style="margin-right: auto;">
                ${svg("warn", "icon-sm")}
                <span>存在未通过项，请先处理上方未通过项再开播</span>
            </div>
        `;
    }
    buttons += `<button class="btn" style="font-weight: 600; padding: 7px 18px;" onclick="closePreflightModal()">稍后处理</button>`;
    actionsEl.innerHTML = buttons;

    modal.style.display = "flex";
    // 同时在向导页渲染一份报告卡片，保存配置后立即可见
    renderPreflightReportCard(data);
}

function renderPreflightReportCard(data) {
    const card = document.getElementById("preflight-report-card");
    const body = document.getElementById("preflight-report-body");
    if (!card || !body) return;
    body.innerHTML = `<div style="font-size:12px;color:var(--text-secondary);margin-bottom:10px;">${data.summary}</div>` + data.checks.map(pfRowHtml).join("");
    card.style.display = "block";
}

function closePreflightModal() {
    const modal = document.getElementById("preflight-modal");
    if (modal) modal.style.display = "none";
}

function preflightGoFix(tabName) {
    closePreflightModal();
    switchToTab(tabName);
}

async function preflightStartLive() {
    // 报告展示后环境可能已变化，确认开播时必须重新获取最新检查结果。
    const pf = await runPreflight({ auto: true });
    if (!pf || !pf.ready) {
        showToast("最新开播检查未通过，请先处理未通过项", "warning");
        return;
    }
    closePreflightModal();
    await startLiveDirect();
}

function renderCurrentModeBadge(modeInfo) {
    const badge = document.getElementById("current-mode-badge");
    const settingsName = document.getElementById("settings-mode-name");
    const settingsDesc = document.getElementById("settings-mode-desc");
    if (badge && modeInfo) {
        badge.style.display = "inline-block";
        badge.innerText = `模式 ${modeInfo.code} · ${modeInfo.name}`;
    }
    if (settingsName && modeInfo) settingsName.innerText = `${modeInfo.code}. ${modeInfo.name}`;
    if (settingsDesc && modeInfo) {
        settingsDesc.innerHTML = `${modeInfo.hardware} · ${modeInfo.llm} · ${modeInfo.tts} · ${modeInfo.cost}<br>
            <span style="color: var(--accent-emerald);">下方已按模式只呈现所需配置项:</span> ${modeInfo.required_configs.join(" / ")}`;
    }
}

// ============================================================================
// 主播管理 (需求4)：增删改查 + 四类照片 + 音色绑定 + 备注
// ============================================================================
let anchorCache = [];

