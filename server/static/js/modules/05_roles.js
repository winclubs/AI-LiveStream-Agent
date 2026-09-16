async function loadRoles() {
    try {
        const res = await fetch(`${API_BASE}/roles/list`);
        const json = await res.json();
        if (json.code !== 0) return;
        roleCardsCache = json.data || [];
        renderRoleCards();
    } catch (e) {
        console.error("加载角色失败", e);
    }
}

function renderRoleCards() {
    const box = document.getElementById("role-cards");
    if (!box) return;
    const activeRoleId = roleCardsCache.find(r => r.is_active)?.id;
    if (!selectedRoleId) selectedRoleId = activeRoleId || roleCardsCache[0]?.id;
    box.innerHTML = "";
    roleCardsCache.forEach(r => {
        const meta = ROLE_CARD_META[r.role_type] || { icon: "user", avatarSvg: "/static/svg/default_avatar.svg", tag: r.role_type, tagColor: "var(--text-muted)" };
                const avatarEl = meta.avatarSvg
            ? `<img src="${meta.avatarSvg}" style="width: 38px; height: 38px; border-radius: 50%; border: 2px solid ${meta.tagColor}; object-fit: cover; box-shadow: 0 2px 8px ${meta.tagColor}33;">`
            : svg(meta.icon, "icon-lg");
        const card = document.createElement("div");
        card.className = "mode-card" + (r.id === selectedRoleId ? " selected" : "");
        card.style.cursor = "pointer";
        card.onclick = () => selectRoleCard(r.id);
        card.innerHTML = `
            <div style="display:flex; justify-content: space-between; align-items: center;">
                ${avatarEl}
                ${r.is_active ? '<span class="badge-recommend" style="font-size:10px;">● 当前角色</span>' : ""}
            </div>
            <div class="mode-card-name">${escapeHtml(r.name)}</div>
            <div class="mode-card-desc" style="color: ${meta.tagColor}; font-weight: 600;">${escapeHtml(meta.tag)}</div>
        `;
        box.appendChild(card);
    });
    loadRolePrompt();
    loadRoleTips();
}

async function loadRoleTips() {
    const role = roleCardsCache.find(r => r.id === selectedRoleId);
    const tipsBox = document.getElementById("role-tips-box");
    if (!role || !tipsBox) return;
    try {
        const res = await fetch(`${API_BASE}/roles/suggestions?role_type=${role.role_type}&mode=${currentMode || "A"}`);
        const json = await res.json();
        if (json.code !== 0) return;
        const d = json.data;
        tipsBox.innerHTML = `
            <div style="padding: 12px; background: rgba(16,185,129,0.08); border: 1px solid rgba(16,185,129,0.3); border-radius: 8px;">
                <div style="font-weight: 700; font-size: 12px; color: var(--accent-emerald);">【${escapeHtml(d.label)}】运营建议${currentMode ? ` (当前模式 ${currentMode} 档)` : ""}:</div>
                <ul style="font-size: 12px; color: var(--text-secondary); margin: 6px 0 0 18px; line-height: 1.8;">
                    ${d.tips.map(t => `<li>${escapeHtml(t)}</li>`).join("")}
                </ul>
            </div>
        `;
    } catch (e) { /* 建议加载失败不影响编辑 */ }
}

function selectRoleCard(roleId) {
    selectedRoleId = roleId;
    renderRoleCards();
}

function loadRolePrompt() {
    const role = roleCardsCache.find(r => r.id === selectedRoleId);
    if (!role) return;
    const editor = document.getElementById("role-prompt-editor");
    if (editor) editor.value = role.system_prompt || "";
}

async function saveRolePrompt() {
    const role = roleCardsCache.find(r => r.id === selectedRoleId);
    const prompt = document.getElementById("role-prompt-editor").value;
    const status = document.getElementById("role-save-status");
    if (!role) { alert("请先选择角色"); return; }
    try {
        const res = await fetch(`${API_BASE}/roles/upsert`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                id: role.id,
                role_type: role.role_type,
                role_name: role.name,
                system_prompt: prompt,
                speech_speed: role.speech_speed || 1.0,
                pitch_shift: role.pitch_shift || 0.0,
                associated_guardrail_group: role.role_type
            })
        });
        const json = await res.json();
        if (json.code === 0) {
            if (status) status.innerText = "✓ 人设与约束提示词已保存生效";
            setTimeout(() => { if (status) status.innerText = ""; }, 4000);
            loadRoles();  // 刷新缓存
        } else {
            alert("保存失败: " + (json.detail || json.message));
        }
    } catch (e) {
        alert("保存异常: " + e);
    }
}

async function activateRole() {
    if (isLiveStreaming) {
        alert("直播进行中，禁止切换主播角色！\n如需更换主播请先在直播大屏停止直播。");
        return;
    }
    if (!selectedRoleId) { alert("请先选择要激活的角色卡片"); return; }
    try {
        const res = await fetch(`${API_BASE}/roles/switch`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ role_id: selectedRoleId })
        });
        const json = await res.json();
        if (json.code === 0) {
            alert("主播角色已激活: " + json.data.role_name);
            loadRoles();
        } else if (res.status === 409) {
            alert(json.detail || "直播进行中，禁止切换角色！");
        } else {
            alert("切换失败: " + (json.detail || json.message));
        }
    } catch (e) {
        alert("角色切换失败: " + e);
    }
}

// 5. 违禁词安全护栏
