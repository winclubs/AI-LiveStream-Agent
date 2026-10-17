async function loadProducts() {
    try {
        const res = await fetch(`${API_BASE}/products/list`);
        const contentType = res.headers.get("content-type") || "";
        if (!contentType.includes("application/json")) {
            const body = (await res.text()).trim();
            throw new Error(`商品接口返回异常 (HTTP ${res.status}): ${body || "响应不是 JSON"}`);
        }
        const json = await res.json();
        if (!res.ok) {
            throw new Error(json.detail || json.message || `商品接口请求失败 (HTTP ${res.status})`);
        }
        if (json.code === 0) {
            const tbody = document.getElementById("products-tbody");
            if (!tbody) return;
            tbody.innerHTML = "";
            json.data.forEach(p => {
                const tr = document.createElement("tr");
                const firstImg = (p.images && p.images.length > 0) ? p.images[0] : "";
                const thumb = firstImg
                    ? `<img src="/static-file?path=${encodeURIComponent(firstImg)}" style="width: 38px; height: 38px; object-fit: cover; border-radius: 6px; border: 1px solid var(--border-subtle); vertical-align: middle; margin-right: 6px;" onerror="this.src='/static/svg/default_product.svg'">`
                    : `<img src="/static/svg/default_product.svg" style="width: 38px; height: 38px; object-fit: cover; border-radius: 6px; border: 1px solid var(--border-subtle); vertical-align: middle; margin-right: 6px;">`;
                const safeTitle = escapeHtml(p.title).replace(/'/g, "\\'");
                tr.innerHTML = `
                    <td style="font-family: monospace;">${escapeHtml(p.sku_code)}</td>
                    <td style="font-weight: 600; white-space: nowrap;">${thumb} ${escapeHtml(p.title)}</td>
                    <td style="color: var(--accent-amber); font-weight: bold;">¥${Number(p.live_price) || 0}</td>
                    <td>${Number(p.current_stock) || 0} 件</td>
                    <td>${(p.images || []).length} 张</td>
                    <td style="max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${escapeHtml(p.description || '-')}</td>
                    <td style="white-space: nowrap;">
                        <div class="table-actions">
                            <button class="btn btn-sm btn-primary" onclick="flashSaleProduct('${escapeHtml(p.id)}', '${safeTitle}')">${svg("zap", "icon-sm")} 促单逼单</button>
                            <button class="btn btn-sm" onclick="triggerOverlayCoupon('${escapeHtml(p.sku_code || p.id)}', '${safeTitle}', '¥${Number(p.live_price) || 0}')" title="在推流画面弹出画中画优惠券">🎟️ 优惠券</button>
                            <button class="btn btn-sm" onclick="triggerOverlayUrgency('${escapeHtml(p.sku_code || p.id)}', '${safeTitle}')" title="在推流画面弹出逼单倒计时">⏱️ 倒计时</button>
                            <button class="btn btn-sm btn-secondary" onclick="viewProductSlices('${escapeHtml(p.id)}', '${safeTitle}')" style="border: 1px solid var(--accent-cyan, #00f2fe); color: var(--accent-cyan, #00f2fe);">🎬 切片资产</button>
                            <button class="btn btn-sm" onclick="editProduct('${escapeHtml(p.id)}')">编辑</button>
                            <button class="btn btn-sm btn-danger" onclick="deleteProduct('${escapeHtml(p.id)}', '${safeTitle}')">删除</button>
                        </div>
                    </td>
                `;
                tbody.appendChild(tr);
            });
        }
    } catch (e) {
        console.error("加载商品失败", e);
    }
}

async function flashSaleProduct(prodId, title) {
    if (!isLiveStreaming) {
        alert("直播间尚未开播！请先在【直播大屏】一键开播，再执行促单逼单。");
        return;
    }
    if (!confirm(`确认立即抢占话术通道，强制播报【${title}】促单话术？`)) return;
    try {
        const res = await fetch(`${API_BASE}/products/${prodId}/flash-sale`, { method: "POST" });
        const json = await res.json();
        if (json.code === 0) {
            triggerBargeInVisual(`促单逼单: ${title}`);
            logDanmaku(`${svg("zap", "icon-sm")} 运营促单`, json.message, true, true);
        } else {
            alert(json.detail || json.message || "促单失败");
        }
    } catch (e) { alert("促单异常: " + e); }
}

async function triggerOverlayCoupon(sku, title, priceText) {
    try {
        const res = await fetch(`${API_BASE}/live/overlay/coupon`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                sku: sku,
                title: `${title} 专属券`,
                amount: `特惠价 ${priceText}`,
                desc: "直播间专属福利，领券立减！",
                seconds: 25,
            }),
        });
        const json = await res.json();
        if (json.code === 0) {
            logDanmaku("🎟️ 运营画层", `已触发【${title}】画中画优惠券 (25s)`, true, true);
        } else {
            alert(json.message || "触发失败");
        }
    } catch (e) {
        alert("网络异常: " + e.message);
    }
}

async function triggerOverlayUrgency(sku, title) {
    try {
        const res = await fetch(`${API_BASE}/live/overlay/urgency`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                sku: sku,
                title: "🔥 限量抢购 倒计时",
                desc: `【${title}】库存仅剩少量，手慢无！`,
                seconds: 60,
            }),
        });
        const json = await res.json();
        if (json.code === 0) {
            logDanmaku("⏱️ 运营画层", `已触发【${title}】逼单倒计时 (60s)`, true, true);
        } else {
            alert(json.message || "触发失败");
        }
    } catch (e) {
        alert("网络异常: " + e.message);
    }
}

let productEditImages = [];  // 编辑中的商品图片路径

function parseProductJsonField(elementId, fallback, label) {
    const raw = document.getElementById(elementId).value.trim();
    if (!raw) return fallback;
    try {
        const value = JSON.parse(raw);
        if (Array.isArray(fallback) ? !Array.isArray(value) : (!value || Array.isArray(value) || typeof value !== "object")) {
            throw new Error("数据结构不符合要求");
        }
        return value;
    } catch (error) {
        throw new Error(`${label} JSON 格式错误：${error.message}`);
    }
}

async function submitProduct() {
    const editId = document.getElementById("product-edit-id").value;
    const title = document.getElementById("product-title").value.trim();
    const sku = document.getElementById("product-sku").value.trim();
    if (!title || !sku) { alert("商品标题与 SKU 编码必填"); return; }

    let faqData;
    let sizeChart;
    try {
        faqData = parseProductJsonField("product-faq-data", [], "常见问答");
        sizeChart = parseProductJsonField("product-size-chart", {}, "尺码表");
    } catch (error) {
        alert(error.message);
        return;
    }

    // 先上传新选择的图片
    const files = document.getElementById("product-images").files;
    const uploadedPaths = [];
    for (const f of files) {
        const fd = new FormData();
        fd.append("file", f);
        try {
            const upRes = await fetch(`${API_BASE}/products/upload-image`, { method: "POST", body: fd });
            const upJson = await upRes.json();
            if (upJson.code === 0) uploadedPaths.push(upJson.data.path);
        } catch (e) { console.warn("图片上传失败", e); }
    }
    const images = uploadedPaths.length > 0 ? uploadedPaths : productEditImages;

    const payload = {
        id: editId || null,
        sku_code: sku,
        title,
        category: document.getElementById("product-category").value.trim() || "通用",
        original_price: parseFloat(document.getElementById("product-original-price").value) || 0,
        live_price: parseFloat(document.getElementById("product-live-price").value) || 0,
        current_stock: parseInt(document.getElementById("product-stock").value) || 0,
        selling_points: document.getElementById("product-selling-points").value
            .split(/\r?\n/).map(item => item.trim()).filter(Boolean),
        faq_data: faqData,
        size_chart: sizeChart,
        coupon_script: document.getElementById("product-coupon-script").value.trim(),
        description: document.getElementById("product-description").value.trim(),
        images
    };
    try {
        const res = await fetch(`${API_BASE}/products/upsert`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        const json = await res.json();
        if (json.code === 0) {
            resetProductForm();
            loadProducts();
        } else {
            alert("保存失败: " + (json.detail || json.message));
        }
    } catch (e) { alert("保存异常: " + e); }
}

async function editProduct(prodId) {
    try {
        const res = await fetch(`${API_BASE}/products/list`);
        const json = await res.json();
        const p = (json.data || []).find(x => x.id === prodId);
        if (!p) { alert("未找到商品"); return; }
        document.getElementById("product-edit-id").value = p.id;
        document.getElementById("product-title").value = p.title;
        document.getElementById("product-sku").value = p.sku_code;
        document.getElementById("product-category").value = p.category || "通用";
        document.getElementById("product-original-price").value = p.original_price;
        document.getElementById("product-live-price").value = p.live_price;
        document.getElementById("product-stock").value = p.current_stock;
        document.getElementById("product-selling-points").value = (p.selling_points || []).join("\n");
        document.getElementById("product-faq-data").value = JSON.stringify(p.faq_data || [], null, 2);
        document.getElementById("product-size-chart").value = JSON.stringify(p.size_chart || {}, null, 2);
        document.getElementById("product-coupon-script").value = p.coupon_script || "";
        document.getElementById("product-description").value = p.description || "";
        productEditImages = p.images || [];
        renderProductImagePreviews();
        document.getElementById("product-edit-hint").innerText = `(正在编辑: ${p.title})`;
        document.getElementById("product-reset-btn").style.display = "inline-block";
    } catch (e) { alert("加载商品失败: " + e); }
}

function renderProductImagePreviews() {
    const box = document.getElementById("product-image-previews");
    if (!box) return;
    box.innerHTML = productEditImages.map(p => `<img src="/static-file?path=${encodeURIComponent(p)}" style="width: 46px; height: 46px; object-fit: cover; border-radius: 6px; border: 1px solid var(--border-subtle);" onerror="this.style.display='none'">`).join("");
}

function resetProductForm() {
    document.getElementById("product-edit-id").value = "";
    ["product-title", "product-sku", "product-description", "product-selling-points", "product-faq-data", "product-size-chart", "product-coupon-script"].forEach(id => document.getElementById(id).value = "");
    document.getElementById("product-category").value = "通用";
    document.getElementById("product-original-price").value = 0;
    document.getElementById("product-live-price").value = 0;
    document.getElementById("product-stock").value = 100;
    document.getElementById("product-images").value = "";
    productEditImages = [];
    renderProductImagePreviews();
    document.getElementById("product-edit-hint").innerText = "";
    document.getElementById("product-reset-btn").style.display = "none";
}

async function deleteProduct(prodId, title) {
    if (!confirm(`确认删除商品【${title}】？`)) return;
    try {
        const res = await fetch(`${API_BASE}/products/${prodId}`, { method: "DELETE" });
        const json = await res.json();
        if (json.code === 0) loadProducts();
    } catch (e) { alert("删除失败: " + e); }
}

async function viewProductSlices(prodId, title) {
    try {
        const res = await fetch(`${API_BASE}/products/${prodId}/slices`);
        const json = await res.json();
        if (json.code !== 0) {
            alert("获取切片失败: " + (json.detail || json.message));
            return;
        }
        const slices = json.data || [];
        let modal = document.getElementById("sku-slices-modal");
        if (!modal) {
            modal = document.createElement("div");
            modal.id = "sku-slices-modal";
            modal.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,0.7);display:flex;align-items:center;justify-content:center;z-index:9999;";
            document.body.appendChild(modal);
        }

        const slotMap = {
            intro: "01 登场介绍",
            selling_point_1: "02 核心卖点",
            coupon: "03 促单立减",
            faq_size: "04 尺码答疑",
            urgency: "05 逼单催付"
        };

        let rowsHtml = slices.map(s => {
            // 架构诚实：只有真实神经渲染产出的切片才算"已就绪"，
            // 占位闭环（无唇形同步）必须显式标红，避免被误当作高精带货切片播出。
            const isPlaceholder = s.render_engine === 'placeholder_loop';
            const badge = isPlaceholder
                ? `<span class="badge" style="background:rgba(244,63,94,0.15);color:#f43f5e;font-size:11px;padding:3px 8px;border-radius:4px;" title="未配置可达云端 GPU，此切片为主播原始闭口帧循环，无唇形同步">无唇形（占位）</span>`
                : `<span class="badge" style="background:rgba(16,231,183,0.15);color:#10e7b7;font-size:11px;padding:3px 8px;border-radius:4px;">已就绪</span>`;
            const warn = isPlaceholder
                ? `<div style="font-size:12px;color:#f43f5e;margin-top:4px;">⚠ 该切片无唇形同步，开播体检将阻断正式档位。请配置云端 GPU 后重新生成。</div>`
                : '';
            return `
            <div style="display:flex;justify-content:space-between;align-items:center;padding:10px;background:rgba(255,255,255,0.04);border-radius:6px;margin-bottom:8px;">
                <div>
                    <span style="font-weight:600;color:var(--accent-cyan, #00f2fe);">${slotMap[s.slot_code] || s.slot_code}</span>
                    <span style="font-size:12px;color:var(--text-muted);margin-left:8px;">时长: ${s.duration_sec}s | 响度: ${s.loudness_lufs} LUFS | 引擎: ${s.render_engine || '未知'}</span>
                    <div style="font-size:13px;color:var(--text-primary);margin-top:4px;max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">台词: ${escapeHtml(s.script_text)}</div>
                    ${warn}
                </div>
                <div>
                    ${badge}
                </div>
            </div>
        `;
        }).join("");

        if (!slices.length) {
            rowsHtml = `<div style="text-align:center;color:var(--text-muted);padding:24px 0;">该商品暂无预渲染切片，请点击下方按钮一键生成。</div>`;
        }

        modal.innerHTML = `
            <div style="background:var(--bg-card, #121824);border:1px solid rgba(0,242,254,0.3);border-radius:12px;width:560px;max-width:90vw;padding:24px;box-shadow:0 12px 32px rgba(0,0,0,0.6);">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
                    <h3 style="margin:0;font-size:17px;color:#fff;">🎬 商品切片资产：${escapeHtml(title)}</h3>
                    <button class="btn btn-sm" onclick="document.getElementById('sku-slices-modal').style.display='none'">✕</button>
                </div>
                <div style="font-size:12.5px;color:var(--text-muted);margin-bottom:14px;">
                    动静分离架构：离线采用 ByteDance LatentSync 官方扩散模型预渲染 5 大高精切片，弹幕打断自动支持句首断点续播。
                </div>
                <div style="max-height:300px;overflow-y:auto;margin-bottom:16px;">
                    ${rowsHtml}
                </div>
                <div style="display:flex;justify-content:space-between;align-items:center;">
                    <button class="btn btn-primary" onclick="triggerGenerateSlices('${escapeHtml(prodId)}', '${escapeHtml(title)}')">⚡ 一键批量离线渲染切片</button>
                    <button class="btn btn-secondary" onclick="document.getElementById('sku-slices-modal').style.display='none'">关闭</button>
                </div>
            </div>
        `;
        modal.style.display = "flex";
    } catch (e) {
        alert("加载切片异常: " + e);
    }
}

async function triggerGenerateSlices(prodId, title) {
    const anchorId = prompt("请输入渲染使用的主播形象 ID (如 anchor_default_01 或当前激活主播):", "anchor_default_01");
    if (!anchorId) return;
    try {
        const res = await fetch(`${API_BASE}/products/${prodId}/slices/generate`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ anchor_id: anchorId })
        });
        const json = await res.json();
        if (json.code === 0) {
            alert(json.message || "切片批量生成完成！");
            viewProductSlices(prodId, title);
        } else {
            alert("生成失败: " + (json.detail || json.message));
        }
    } catch (e) {
        alert("请求异常: " + e);
    }
}

