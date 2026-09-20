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
        const meta = ROLE_CARD_META[r.role_type] || {
            icon: "user",
            avatarSvg: "/static/svg/default_avatar.svg",
            title: r.role_type,
            tag: r.role_type,
            tagColor: "var(--text-muted)"
        };
        const avatarEl = meta.avatarSvg
            ? `<img src="${meta.avatarSvg}" style="width: 44px; height: 44px; border-radius: 50%; border: 2px solid ${meta.tagColor}; object-fit: cover; box-shadow: 0 2px 8px ${meta.tagColor}33; display: block;">`
            : svg(meta.icon, "icon-lg");
        const isSelected = r.id === selectedRoleId;
        const card = document.createElement("div");
        card.className = "role-type-card" + (isSelected ? " selected" : "");
        card.style.cssText = `
            display: flex;
            align-items: center;
            gap: 14px;
            padding: 12px 16px;
            border-radius: var(--radius-md);
            background: ${isSelected ? "rgba(16, 185, 129, 0.1)" : "rgba(30, 41, 59, 0.45)"};
            border: 1.5px solid ${isSelected ? "#10B981" : "rgba(148, 163, 184, 0.16)"};
            cursor: pointer;
            transition: all 0.2s ease;
            box-shadow: ${isSelected ? "0 4px 16px rgba(16, 185, 129, 0.18)" : "none"};
        `;
        card.onclick = () => selectRoleCard(r.id);
        card.innerHTML = `
            <div style="flex-shrink: 0;">
                ${avatarEl}
            </div>
            <div style="flex: 1; min-width: 0;">
                <div style="font-size: 14px; font-weight: 700; color: ${isSelected ? '#10B981' : 'var(--text-primary)'}; margin-bottom: 3px;">
                    ${escapeHtml(meta.title || r.name)}
                </div>
                <div style="font-size: 11.5px; color: ${meta.tagColor}; font-weight: 600; line-height: 1.4; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">
                    ${escapeHtml(meta.tag)}
                </div>
            </div>
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
