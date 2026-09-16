async function loadGuardrailWords() {
    try {
        const res = await fetch(`${API_BASE}/guardrails/words`);
        const json = await res.json();
        if (json.code === 0) {
            const tbody = document.getElementById("guardrail-tbody");
            if (!tbody) return;
            tbody.innerHTML = "";
            const countEl = document.getElementById("guardrail-count");
            if (countEl) countEl.innerText = json.data.length;
            json.data.forEach(w => {
                const tr = document.createElement("tr");
                tr.innerHTML = `
                    <td style="font-weight: 600;">${escapeHtml(w.word)}</td>
                    <td><span class="brand-badge">${escapeHtml(w.category)}</span></td>
                    <td>${w.action_policy === 'substitute' ? '自动平替' : '整句阻断'}</td>
                    <td style="color: var(--accent-emerald);">${escapeHtml(w.replacement_word || '-')}</td>
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
    const status = document.getElementById("batch-words-status");
    if (!words) { alert("请输入违禁词（多个用英文逗号分隔）"); return; }
    if (!replacement) {
        if (!confirm("未填写合规替换词，命中该词的整句将被 AI 阻断不说出。确认继续吗？")) return;
    }
    try {
        const res = await fetch(`${API_BASE}/guardrails/words/batch`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ words, replacement_word: replacement, category, action_policy: replacement ? "substitute" : "drop" })
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
    if (!input) return;
    try {
        const res = await fetch(`${API_BASE}/guardrails/test-sanitize`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text: input, role_scope: "all" })
        });
        const json = await res.json();
        const resBox = document.getElementById("sanitize-result");
        if (json.code === 0) {
            resBox.innerHTML = `
                <div style="margin-top: 10px; padding: 10px; background: rgba(0,0,0,0.3); border-radius: 6px; border: 1px solid var(--border-subtle);">
                    <div><strong>过滤前:</strong> ${escapeHtml(json.original_text)}</div>
                    <div style="margin-top: 4px; color: ${json.is_dropped ? 'var(--accent-danger)' : 'var(--accent-emerald)'};">
                        <strong>${json.is_dropped ? '[整句被阻断]' : '合规平替后:'}</strong> ${escapeHtml(json.sanitized_text || '整句已安全阻断下架')}
                    </div>
                    ${json.hits.length > 0 ? `<div style="font-size: 11px; color: var(--accent-amber); margin-top: 4px;">命中敏感词: ${json.hits.map(m => escapeHtml(m.matched_word)).join(", ")}</div>` : ''}
                </div>
            `;
        }
    } catch (e) {
        alert("测试平替失败: " + e);
    }
}

// 6. 商品管理 (需求6)：增删改查 + 多图 + 立即促单逼单
