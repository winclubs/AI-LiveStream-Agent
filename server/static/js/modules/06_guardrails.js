let currentGuardrailPlatformFilter = "all";
let currentGuardrailCategoryFilter = "all";

const GUARDRAIL_PLATFORM_MAP = {
    all: { name: "全部平台", label: "全部", badge: "全平台通用", color: "#94a3b8", bg: "rgba(148, 163, 184, 0.15)", border: "rgba(148, 163, 184, 0.3)" },
    common: { name: "全网通用底线", label: "通用", badge: "通用底线", color: "#94a3b8", bg: "rgba(148, 163, 184, 0.15)", border: "rgba(148, 163, 184, 0.3)" },
    douyin: { name: "抖音专属规则", label: "抖音", badge: "抖音专属", color: "#38bdf8", bg: "rgba(14, 165, 233, 0.15)", border: "rgba(14, 165, 233, 0.3)" },
    wechat: { name: "微信视频号专属", label: "视频号", badge: "视频号", color: "#4ade80", bg: "rgba(34, 197, 94, 0.15)", border: "rgba(34, 197, 94, 0.3)" },
    kuaishou: { name: "快手专属规则", label: "快手", badge: "快手专属", color: "#fb923c", bg: "rgba(249, 115, 22, 0.15)", border: "rgba(249, 115, 22, 0.3)" },
    bilibili: { name: "B站专属规则", label: "B站", badge: "B站专属", color: "#f472b6", bg: "rgba(236, 72, 153, 0.15)", border: "rgba(236, 72, 153, 0.3)" }
};

const GUARDRAIL_CATEGORY_MAP = {
    all: "全部分类",
    extreme: "广告法极限词",
    traffic: "站外私域导流",
    medical: "医疗功效药效",
    sensitive: "恶俗炒作剧本",
    competitor: "竞品平台暗号"
};

function switchGuardrailPlatform(platformKey, btnEl) {
    currentGuardrailPlatformFilter = platformKey;
    const tabContainer = document.getElementById("guardrail-platform-tabs");
    if (tabContainer) {
        tabContainer.querySelectorAll("button").forEach(b => {
            b.classList.remove("btn-primary");
            b.classList.add("btn-outline");
        });
    }
    if (btnEl) {
        btnEl.classList.remove("btn-outline");
        btnEl.classList.add("btn-primary");
    }
    const nameEl = document.getElementById("guardrail-current-platform-name");
    if (nameEl && GUARDRAIL_PLATFORM_MAP[platformKey]) {
        nameEl.innerText = GUARDRAIL_PLATFORM_MAP[platformKey].name;
    }
    loadGuardrailWords();
}

function switchGuardrailCategory(categoryKey, btnEl) {
    currentGuardrailCategoryFilter = categoryKey;
    const tabContainer = document.getElementById("guardrail-category-tabs");
    if (tabContainer) {
        tabContainer.querySelectorAll("button").forEach(b => {
            b.classList.remove("btn-primary");
            b.classList.add("btn-outline");
        });
    }
    if (btnEl) {
        btnEl.classList.remove("btn-outline");
        btnEl.classList.add("btn-primary");
    }
    const nameEl = document.getElementById("guardrail-current-category-name");
    if (nameEl && GUARDRAIL_CATEGORY_MAP[categoryKey]) {
        nameEl.innerText = GUARDRAIL_CATEGORY_MAP[categoryKey];
    }
    loadGuardrailWords();
}

async function loadGuardrailWords() {
    try {
        const params = new URLSearchParams();
        if (currentGuardrailPlatformFilter === "common") {
            params.append("platform", "all");
        } else if (currentGuardrailPlatformFilter !== "all") {
            params.append("platform", currentGuardrailPlatformFilter);
        }
        if (currentGuardrailCategoryFilter !== "all") {
            params.append("category", currentGuardrailCategoryFilter);
        }
        const qs = params.toString();
        const url = `${API_BASE}/guardrails/words${qs ? '?' + qs : ''}`;
        const res = await fetch(url);
        const json = await res.json();
        if (json.code === 0) {
            const tbody = document.getElementById("guardrail-tbody");
            if (!tbody) return;
            tbody.innerHTML = "";
            const countEl = document.getElementById("guardrail-count");
            if (countEl) countEl.innerText = json.data.length;

            if (json.data.length === 0) {
                tbody.innerHTML = `<tr><td colspan="6" style="text-align: center; color: var(--text-muted); padding: 24px;">当前分类下暂无违禁词规则</td></tr>`;
                return;
            }

            json.data.forEach(w => {
                const tr = document.createElement("tr");
                const platKey = (w.platform || "all").toLowerCase();
                const platInfo = GUARDRAIL_PLATFORM_MAP[platKey] || GUARDRAIL_PLATFORM_MAP.all;
                const catLabel = GUARDRAIL_CATEGORY_MAP[w.category] || w.category;

                tr.innerHTML = `
                    <td style="font-weight: 600;">${escapeHtml(w.word)}</td>
                    <td>
                        <span class="brand-badge" style="background: ${platInfo.bg}; color: ${platInfo.color}; border: 1px solid ${platInfo.border};">
                            ${platInfo.badge}
                        </span>
                    </td>
                    <td><span class="brand-badge" style="background: rgba(255,255,255,0.05);">${escapeHtml(catLabel)}</span></td>
                    <td>
                        ${w.action_policy === 'substitute' 
                            ? '<span style="color: var(--accent-emerald);">合规平替</span>' 
                            : '<span style="color: var(--accent-danger);">整句阻断</span>'}
                    </td>
                    <td style="color: ${w.replacement_word ? 'var(--accent-emerald)' : 'var(--text-muted)'};">
                        ${escapeHtml(w.replacement_word || '（无替换·整句阻断）')}
                    </td>
                    <td><button class="btn btn-sm btn-danger" onclick="deleteGuardrailWord('${escapeHtml(w.id)}')">删除</button></td>
                `;
                tbody.appendChild(tr);
            });
        }
    } catch (e) {
        console.error("加载违禁词失败", e);
    }
}

async function deleteGuardrailWord(wordId) {
    if (!confirm("确认删除该违禁词规则？")) return;
    try {
        const res = await fetch(`${API_BASE}/guardrails/words/${wordId}`, { method: "DELETE" });
        const json = await res.json();
        if (json.code === 0) loadGuardrailWords();
    } catch (e) { alert("删除失败: " + e); }
}

async function submitBatchWords() {
    const words = document.getElementById("batch-words").value.trim();
    const replacement = document.getElementById("batch-replacement").value.trim();
    const category = document.getElementById("batch-category").value;
    const platform = document.getElementById("batch-platform").value;
    const status = document.getElementById("batch-words-status");
    if (!words) { alert("请输入违禁词（多个用英文逗号分隔）"); return; }
    if (!replacement) {
        if (!confirm("未填写合规替换词，命中该词的整句将被 AI 阻断不说出。确认继续吗？")) return;
    }
    try {
        const res = await fetch(`${API_BASE}/guardrails/words/batch`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                words,
                replacement_word: replacement,
                category,
                platform,
                action_policy: replacement ? "substitute" : "drop"
            })
        });
        const json = await res.json();
        if (json.code === 0) {
            if (status) status.innerText = json.message;
            document.getElementById("batch-words").value = "";
            document.getElementById("batch-replacement").value = "";
            loadGuardrailWords();
        } else {
            alert("添加失败: " + (json.detail || json.message));
        }
    } catch (e) {
        alert("批量添加异常: " + e);
    }
}

async function testSanitize() {
    const input = document.getElementById("sanitize-input").value;
    const platform = document.getElementById("test-platform-select").value;
    const role = document.getElementById("test-role-select").value;
    if (!input) return;
    try {
        const res = await fetch(`${API_BASE}/guardrails/test-sanitize`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text: input, role_scope: role, platform: platform })
        });
        const json = await res.json();
        const resBox = document.getElementById("sanitize-result");
        if (json.code === 0) {
            const hitsHtml = json.hits.length > 0
                ? `<div style="font-size: 12px; color: var(--accent-amber); margin-top: 6px;">
                     <strong>命中敏感词规则：</strong>
                     ${json.hits.map(m => {
                         const plat = GUARDRAIL_PLATFORM_MAP[m.platform] || GUARDRAIL_PLATFORM_MAP.all;
                         return `<span class="brand-badge" style="background: ${plat.bg}; color: ${plat.color}; margin-right: 4px;">
                             [${plat.badge}] ${escapeHtml(m.matched_word)} → ${m.action === 'substitute' ? escapeHtml(m.replacement) : '整句阻断'}
                         </span>`;
                     }).join("")}
                   </div>`
                : '<div style="font-size: 12px; color: var(--accent-emerald); margin-top: 4px;">该平台及角色作用域下未发现违禁词，安全合规放行。</div>';

            resBox.innerHTML = `
                <div style="margin-top: 12px; padding: 12px 14px; background: rgba(0,0,0,0.3); border-radius: 6px; border: 1px solid var(--border-subtle);">
                    <div><span style="color: var(--text-muted);">过滤前原始文本：</span> ${escapeHtml(json.original_text)}</div>
                    <div style="margin-top: 6px; color: ${json.is_dropped ? 'var(--accent-danger)' : 'var(--accent-emerald)'};">
                        <strong>${json.is_dropped ? '【整句被阻断下架】' : '【合规平替后文本】'}</strong> ${escapeHtml(json.sanitized_text || '该整句已触发平台红线，安全阻断不予播报')}
                    </div>
                    ${hitsHtml}
                </div>
            `;
        }
    } catch (e) {
        alert("测试平替失败: " + e);
    }
}
