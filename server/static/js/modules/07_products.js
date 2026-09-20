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

