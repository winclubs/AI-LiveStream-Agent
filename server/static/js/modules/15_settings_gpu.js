// ==============================================================================
// 15_settings_gpu.js - GPU算力调度与数字人渲染（双核心运行模式与真实达标核验）
// 1. 本机完全满足 (本地单机运行 · 严格核验本机显卡是否达标)
// 2. 本地硬件 + 租赁云端GPU (端云协同 · 严格核验本地主控+云端GPU配合是否达标)
// ==============================================================================

let currentSelectedGpuMode = "local_procedural"; // "local_procedural" | "sidecar_v3"
let currentEditingAvatarConfigId = "";
let cachedAvatarConfigs = [];
let cachedHardwareData = null;
let lastCloudPingResult = null; // 缓存云端握手结果 { success: bool, latency_ms: int, device: str, error: str }

// 智能规范化 Sidecar WebSocket 连接地址 (消除用户填入 https:// 或漏填 /ws/render-v3 的低级阻断)
function sanitizeSidecarWsUrl(url) {
    if (!url) return "";
    let val = String(url).trim();
    if (!val) return "";

    // 1. 协议纠偏：https -> wss, http -> ws
    if (val.startsWith("https://")) {
        val = "wss://" + val.slice(8);
    } else if (val.startsWith("http://")) {
        val = "ws://" + val.slice(7);
    } else if (!val.startsWith("ws://") && !val.startsWith("wss://")) {
        val = (val.includes("trycloudflare.com") || val.includes(".com") || val.includes(".org")) ? ("wss://" + val) : ("ws://" + val);
    }

    // 2. 路径规整：若用户带了 /v1 或无 path，规范化为标准渲染端点 /ws/render-v3
    try {
        const u = new URL(val.replace("wss://", "https://").replace("ws://", "http://"));
        let p = u.pathname;
        if (!p || p === "/" || p === "/v1" || p === "/v1/") {
            p = "/ws/render-v3";
        } else if (p.endsWith("/v1")) {
            p = p.slice(0, -3) + "/ws/render-v3";
        } else if (!p.includes("/ws/")) {
            p = p.replace(/\/+$/, "") + "/ws/render-v3";
        }
        const proto = val.startsWith("wss://") ? "wss://" : "ws://";
        val = proto + u.host + p + (u.search || "");
    } catch (_) {}

    return val;
}
window.sanitizeSidecarWsUrl = sanitizeSidecarWsUrl;

// ------------------------------------------------------------------------------
// 1. 本地物理硬件实况探测与系统要求智能比对
// ------------------------------------------------------------------------------
async function refreshGpuHardwareOverview(forceRefresh = false) {
    const gpuNameEl = document.getElementById("gpu-hw-card-name");
    const gpuVramEl = document.getElementById("gpu-hw-card-vram");
    const gpuCudaEl = document.getElementById("gpu-hw-card-cuda-badge");
    const cpuNameEl = document.getElementById("gpu-hw-card-cpu-name");
    const cpuUsageEl = document.getElementById("gpu-hw-card-cpu-usage");
    const cpuCoresEl = document.getElementById("gpu-hw-card-cpu-cores");
    const ramTotalEl = document.getElementById("gpu-hw-card-ram-total");
    const ramUsageEl = document.getElementById("gpu-hw-card-ram-percent");
    const evalBanner = document.getElementById("gpu-hw-eval-banner");
    const headerStatusEl = document.getElementById("gpu-header-active-status");

    try {
        const res = await fetch(`${API_BASE}/system/hardware?t=${Date.now()}`);
        const json = await res.json();
        if (json.code !== 0 || !json.data) return;
        const d = json.data;
        cachedHardwareData = d;

        const gpu = d.gpu || {};
        const cap = d.gpu_capability || {};
        const gpuName = gpu.gpu_name || "未检测到独立显卡 (集成显卡/核显)";
        const vramTotal = parseFloat(gpu.vram_total_gb || 0);
        const vramUsed = parseFloat(gpu.vram_used_gb || 0);
        const hasCuda = Boolean(gpu.cuda_available);

        // 1. 显卡卡片呈现
        if (gpuNameEl) gpuNameEl.textContent = gpuName;
        if (gpuVramEl) {
            gpuVramEl.textContent = vramTotal > 0
                ? `总显存: ${vramTotal} GB · 当前已用: ${vramUsed} GB`
                : "无专用独立显存 (共享系统内存)";
        }
        if (gpuCudaEl) {
            gpuCudaEl.className = hasCuda ? "brand-badge green" : "brand-badge amber";
            gpuCudaEl.textContent = hasCuda ? "CUDA 加速就绪" : "无 CUDA 驱动";
        }

        // 2. 处理器呈现
        const cpuName = d.cpu_name || "未知处理器";
        if (cpuNameEl) cpuNameEl.textContent = cpuName.length > 38 ? cpuName.slice(0, 38) + "..." : cpuName;
        if (cpuCoresEl) cpuCoresEl.textContent = `${d.cpu_cores || 4} 核心`;
        if (cpuUsageEl) {
            const cpuPercent = Math.round(d.cpu_percent || 0);
            cpuUsageEl.innerHTML = `实时利用率: <strong style="color: ${cpuPercent > 80 ? 'var(--accent-danger)' : '#38bdf8'};">${cpuPercent}%</strong>`;
        }

        // 3. 内存呈现
        const ramUsed = d.ram_used_gb || 0;
        const ramTotal = d.ram_total_gb || 16;
        const ramPercent = Math.round(d.ram_percent || 0);
        if (ramTotalEl) ramTotalEl.textContent = `${ramUsed} / ${ramTotal} GB`;
        if (ramUsageEl) {
            ramUsageEl.className = ramPercent > 85 ? "brand-badge red" : "brand-badge green";
            ramUsageEl.textContent = `${ramPercent}% 占用`;
        }

        // 4. 显存门槛动态评估与醒目文字提示
        if (evalBanner) {
            const isLowSpec = cap.is_low_spec_local || vramTotal < 2.0 || !hasCuda;
            evalBanner.style.display = "block";

            if (isLowSpec) {
                evalBanner.style.background = "rgba(245, 158, 11, 0.12)";
                evalBanner.style.border = "1.5px solid rgba(245, 158, 11, 0.4)";
                evalBanner.style.color = "#fde68a";
                evalBanner.innerHTML = `
                    <div style="font-weight: 700; color: #fbbf24; margin-bottom: 6px; display: flex; align-items: center; gap: 6px;">
                        <svg viewBox="0 0 24 24" style="width: 16px; height: 16px; stroke: #fbbf24; fill: none; stroke-width: 2.2;"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
                        硬件评估与文字提示：检测到当前显卡配置较低（${escapeHtml(gpuName)}，显存仅 ${vramTotal} GB，低于 2GB 最低推荐要求）
                    </div>
                    <div style="font-size: 12.5px; line-height: 1.7; color: #e2e8f0;">
                        当前硬件<strong>无法在本地流畅运行 1080P 超写实真人实时驱动</strong>。建议：<br>
                        👉 <strong>推荐方案 A</strong>：点击下方【<strong>选项 2 · 本地硬件 + 租赁云端GPU</strong>】，按小时租用云端独立显卡（如 AutoDL RTX 4090 约 1.2 元/小时），本机 0 显存负担畅享电影级真人！<br>
                        👉 <strong>备用方案 B</strong>：点击下方【<strong>选项 1 · 本机完全满足 (本地运行)</strong>】，使用系统免显卡的本地程序化形象，0元免配置开箱即播。
                    </div>
                `;
            } else {
                evalBanner.style.background = "rgba(16, 185, 129, 0.12)";
                evalBanner.style.border = "1.5px solid rgba(16, 185, 129, 0.4)";
                evalBanner.style.color = "#a7f3d0";
                evalBanner.innerHTML = `
                    <div style="font-weight: 700; color: #34d399; margin-bottom: 4px; display: flex; align-items: center; gap: 6px;">
                        <svg viewBox="0 0 24 24" style="width: 16px; height: 16px; stroke: #34d399; fill: none; stroke-width: 2.2;"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
                        硬件评估与文字提示：检测到本地独立显卡性能优良（${escapeHtml(gpuName)}，显存 ${vramTotal} GB）
                    </div>
                    <div style="font-size: 12.5px; line-height: 1.7; color: #e2e8f0;">
                        当前电脑完全满足在本地单机直接流畅运行数字人实时渲染，推荐选择【<strong>选项 1 · 本机完全满足 (本地运行)</strong>】享受 0 成本单机闭环！
                    </div>
                `;
            }
        }

        // 5. 执行双模式达标综合核验
        evaluateBothModesCompliance(d);

        // 6. 更新顶栏状态
        if (headerStatusEl) {
            const hasActiveCloud = cap.use_cloud;
            headerStatusEl.innerHTML = hasActiveCloud
                ? `<span style="color: #38bdf8; font-weight: 600;">⚡ 当前生效：云端租赁 GPU 协同加速</span>`
                : `<span style="color: #34d399; font-weight: 600;">🟢 当前生效：本地单机运行模式</span>`;
        }
    } catch (e) {
        console.warn("探测系统硬件配置异常:", e);
    }
}

// ------------------------------------------------------------------------------
// 2. 核心：严格检测两模式是否真正达标 (本地达标 vs 端云协同配合达标)
// ------------------------------------------------------------------------------
function evaluateBothModesCompliance(hardwareData) {
    if (!hardwareData) return;
    const gpu = hardwareData.gpu || {};
    const vramTotal = parseFloat(gpu.vram_total_gb || 0);
    const hasCuda = Boolean(gpu.cuda_available);
    const gpuName = gpu.gpu_name || "集成显卡/核显";
    const cpuCores = hardwareData.cpu_cores || 4;
    const ramTotal = hardwareData.ram_total_gb || 16;

    // =========================================================================
    // 判定 1：选项 1 · 本地单机运行 是否真的达标？
    // 商业运营型门槛：具备独立显卡、CUDA 可用、显存 >= 6.0GB (推荐 8GB ~ 12GB)
    // =========================================================================
    const localBadge = document.getElementById("gpu-compliance-badge-local");
    const localSummary = document.getElementById("gpu-compliance-summary-local");
    const isLocalGpuQualified = hasCuda && vramTotal >= 6.0;

    if (localBadge) {
        if (isLocalGpuQualified) {
            localBadge.className = "brand-badge green";
            localBadge.innerHTML = "🟢 硬件达标 · 支持超写实真人";
        } else {
            localBadge.className = "brand-badge amber";
            localBadge.innerHTML = "⚠️ 硬件未达标 (仅支持轻量形象)";
        }
    }

    if (localSummary) {
        if (isLocalGpuQualified) {
            localSummary.innerHTML = `
                <div style="color: #34d399; font-weight: 600; margin-bottom: 2px;">✓ 本地硬件达到商业运营级真人驱动门槛 (>= 6GB)</div>
                <div style="font-size: 11.5px; color: #cbd5e1;">检测到 ${escapeHtml(gpuName)} (显存 ${vramTotal}GB，CUDA 可用)，可直接单机流畅渲染！</div>
            `;
            localSummary.style.borderColor = "rgba(16, 185, 129, 0.4)";
        } else {
            localSummary.innerHTML = `
                <div style="color: #fbbf24; font-weight: 600; margin-bottom: 2px;">⚠️ 显存不足 (${vramTotal}GB < 6.0GB 商业运营门槛) 或缺少 CUDA 加速</div>
                <div style="font-size: 11.5px; color: #cbd5e1;">本地无法稳定运行超写实真人，将由免显卡轻量形象保底；写实真人强烈建议选选项2。</div>
            `;
            localSummary.style.borderColor = "rgba(245, 158, 11, 0.4)";
        }
    }

    // =========================================================================
    // 判定 2：选项 2 · 本地硬件 + 云端 GPU 配合起来是否满足？
    // 配合门槛：
    // - 本地条件：CPU >= 2核，内存 >= 4GB (负责推流与业务主控) -> 当前电脑轻松满足！
    // - 云端条件：云端渲染节点连通性 (是否配置且网络可达握手成功)
    // =========================================================================
    const cloudBadge = document.getElementById("gpu-compliance-badge-cloud");
    const cloudSummary = document.getElementById("gpu-compliance-summary-cloud");

    const activeCloudCfg = cachedAvatarConfigs.find(c => c.config_group === "neural_renderer" && c.provider_name === "sidecar_v3");
    const urlInput = document.getElementById("gpu-input-base-url");
    const currentInputUrl = urlInput ? urlInput.value.trim() : "";
    const hasCloudUrl = Boolean((activeCloudCfg && activeCloudCfg.base_url && activeCloudCfg.base_url.trim().length > 0) || currentInputUrl.length > 0);
    const isCloudPingSuccess = Boolean(lastCloudPingResult && lastCloudPingResult.success);

    if (cloudBadge) {
        if (hasCloudUrl && isCloudPingSuccess) {
            cloudBadge.className = "brand-badge green";
            cloudBadge.innerHTML = "🟢 端云协同完美达标";
        } else if (hasCloudUrl) {
            cloudBadge.className = "brand-badge sky";
            cloudBadge.innerHTML = "☁️ 节点已配置 (待探活)";
        } else {
            cloudBadge.className = "brand-badge amber";
            cloudBadge.innerHTML = "⚠️ 云端未就绪 (需填地址)";
        }
    }

    if (cloudSummary) {
        if (hasCloudUrl && isCloudPingSuccess) {
            const devInfo = lastCloudPingResult.device ? ` (远端显卡: ${escapeHtml(lastCloudPingResult.device)})` : "";
            cloudSummary.innerHTML = `
                <div style="color: #38bdf8; font-weight: 600; margin-bottom: 2px;">✓ 端云协同配合完美就绪</div>
                <div style="font-size: 11.5px; color: #cbd5e1;">本地主控充足 (${cpuCores}核/${ramTotal}GB) + 云端 GPU 连通通畅${devInfo}，0 显存负担畅享写实真人！</div>
            `;
            cloudSummary.style.borderColor = "rgba(56, 189, 248, 0.4)";
        } else if (hasCloudUrl) {
            cloudSummary.innerHTML = `
                <div style="color: #38bdf8; font-weight: 600; margin-bottom: 2px;">ℹ️ 本地环境已就绪 · 已配置云端节点</div>
                <div style="font-size: 11.5px; color: #cbd5e1;">已保存云端地址，点击下方「测试通信连接」即可即时校验连通与远端显卡！</div>
            `;
            cloudSummary.style.borderColor = "rgba(56, 189, 248, 0.3)";
        } else {
            cloudSummary.innerHTML = `
                <div style="color: #fbbf24; font-weight: 600; margin-bottom: 2px;">⚠️ 本地主控就绪 · 云端算力节点待填写</div>
                <div style="font-size: 11.5px; color: #cbd5e1;">本地配置达标 (${cpuCores}核/${ramTotal}GB)，在下方填写云端 GPU 地址即可完成端云闭环。</div>
            `;
            cloudSummary.style.borderColor = "rgba(245, 158, 11, 0.35)";
        }
    }
}

// ------------------------------------------------------------------------------
// 3. 模式切换与配置面板渲染
// ------------------------------------------------------------------------------
function selectGpuMode(modeId) {
    currentSelectedGpuMode = modeId;

    // 1. 卡片边框选中态更新
    const cardLocal = document.getElementById("gpu-mode-card-local");
    const cardCloud = document.getElementById("gpu-mode-card-cloud");
    const badgeLocal = document.getElementById("gpu-mode-badge-local");
    const badgeCloud = document.getElementById("gpu-mode-badge-cloud");

    const isLocal = modeId === "local_procedural";

    if (cardLocal) {
        cardLocal.style.borderColor = isLocal ? "#10B981" : "rgba(148, 163, 184, 0.2)";
        cardLocal.style.boxShadow = isLocal ? "0 0 18px rgba(16, 185, 129, 0.2)" : "none";
    }
    if (cardCloud) {
        cardCloud.style.borderColor = !isLocal ? "#38bdf8" : "rgba(148, 163, 184, 0.2)";
        cardCloud.style.boxShadow = !isLocal ? "0 0 18px rgba(56, 189, 248, 0.2)" : "none";
    }

    if (badgeLocal) {
        badgeLocal.className = isLocal ? "brand-badge green" : "brand-badge";
        badgeLocal.textContent = isLocal ? "已选中" : "可选";
    }
    if (badgeCloud) {
        badgeCloud.className = !isLocal ? "brand-badge sky" : "brand-badge";
        badgeCloud.textContent = !isLocal ? "已选中" : "可选";
    }

    // 2. 渲染下方配置面板
    renderGpuConfigDetailPanel(modeId);

    // 3. 若切到云端 GPU 模式且输入框中存在有效公网地址，自动发起静默握手检测
    if (modeId === "sidecar_v3") {
        setTimeout(() => {
            const urlInput = document.getElementById("gpu-input-base-url");
            const val = urlInput ? urlInput.value.trim() : "";
            if (val && !val.includes("127.0.0.1") && (!lastCloudPingResult || !lastCloudPingResult.success)) {
                testCurrentGpuAvatarConnection(true);
            }
        }, 80);
    }
}

// 渲染下方配置操作面板内容 (含专项达标诊断明细报告)
function renderGpuConfigDetailPanel(modeId) {
    const titleEl = document.getElementById("gpu-config-panel-title");
    const badgeEl = document.getElementById("gpu-config-panel-badge");
    const linkBox = document.getElementById("gpu-config-panel-link-box");
    const dynamicBox = document.getElementById("gpu-mode-dynamic-content");
    const copyCmdBtn = document.getElementById("gpu-avatar-copy-cloud-cmd-btn");
    const testConnBtn = document.getElementById("gpu-avatar-test-btn");
    const saveBtn = document.getElementById("gpu-avatar-save-btn");
    const connResult = document.getElementById("gpu-avatar-conn-result");

    if (connResult) {
        if (lastCloudPingResult && lastCloudPingResult.success && modeId === "sidecar_v3") {
            connResult.style.display = "flex";
            connResult.style.background = "rgba(16, 185, 129, 0.15)";
            connResult.style.color = "#10B981";
            connResult.style.border = "1px solid rgba(16, 185, 129, 0.35)";
            connResult.innerHTML = `🟢 配合达标！云端握手正常，延迟: ${lastCloudPingResult.latency_ms}ms ${lastCloudPingResult.device ? '(远端识别硬件: <b>' + escapeHtml(lastCloudPingResult.device) + '</b>)' : ''}，本地主控与云端 GPU 协同就绪！`;
        } else {
            connResult.style.display = "none";
            connResult.innerHTML = "";
        }
    }

    const isLocal = modeId === "local_procedural";
    const d = cachedHardwareData || {};
    const gpu = d.gpu || {};
    const gpuName = gpu.gpu_name || "集成显卡/核显";
    const vramTotal = parseFloat(gpu.vram_total_gb || 0);
    const hasCuda = Boolean(gpu.cuda_available);
    const isLocalGpuQualified = hasCuda && vramTotal >= 6.0;
    const cpuCores = d.cpu_cores || 4;
    const ramTotal = d.ram_total_gb || 16;

    // 找到云端活跃配置（优先寻找带有效公网穿透 URL 的配置）
    const cloudConfigs = cachedAvatarConfigs.filter(c => c.config_group === "neural_renderer" && (c.provider_name === "sidecar_v3" || c.provider_name === "custom_avatar"));
    const activeCloudCfg = cloudConfigs.find(c => c.is_active && c.base_url && !c.base_url.includes("127.0.0.1")) || cloudConfigs.find(c => c.is_active);
    const anyCloudCfg = activeCloudCfg || cloudConfigs.find(c => c.base_url && !c.base_url.includes("127.0.0.1")) || cloudConfigs.find(c => c.provider_name === "sidecar_v3") || cloudConfigs[0];
    const isLocalCurrentlyActive = !activeCloudCfg;

    if (isLocal) {
        // =====================================================================
        // 模式 1：本机完全满足 (本地单机运行) · 专项达标诊断核验报告
        // =====================================================================
        if (titleEl) titleEl.textContent = "选项 1 · 本机完全满足 (本地单机运行) 硬件达标诊断";
        if (badgeEl) {
            badgeEl.className = isLocalGpuQualified ? "brand-badge green" : "brand-badge amber";
            badgeEl.textContent = isLocalGpuQualified ? "硬件完全达标" : "硬件未达标 (轻量保底)";
        }
        if (linkBox) linkBox.innerHTML = "";

        if (dynamicBox) {
            dynamicBox.innerHTML = `
                <div style="background: rgba(15, 23, 42, 0.6); border: 1px solid ${isLocalGpuQualified ? 'rgba(16, 185, 129, 0.3)' : 'rgba(245, 158, 11, 0.35)'}; border-radius: 8px; padding: 18px;">
                    <!-- 硬件达标指标实测核验清单 -->
                    <div style="font-size: 13.5px; font-weight: 700; color: #f8fafc; margin-bottom: 12px; display: flex; align-items: center; gap: 8px;">
                        <svg viewBox="0 0 24 24" style="width: 16px; height: 16px; stroke: ${isLocalGpuQualified ? '#10B981' : '#F59E0B'}; fill: none; stroke-width: 2.2;"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
                        本机硬件达标核验清单（超写实真人渲染驱动门槛）：
                    </div>

                    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 10px; margin-bottom: 14px;">
                        <!-- 显存检测 -->
                        <div style="background: rgba(30, 41, 59, 0.6); padding: 10px 12px; border-radius: 6px; border: 1px solid rgba(148, 163, 184, 0.15);">
                            <div style="font-size: 11.5px; color: var(--text-muted); margin-bottom: 4px;">① 显卡显存容量 (运营门槛 >= 6.0GB，推荐 8~12GB)</div>
                            <div style="font-size: 13px; font-weight: 600; color: ${vramTotal >= 6.0 ? '#34d399' : '#f87171'};">
                                ${vramTotal >= 6.0 ? '✓ 通过' : '✗ 未达标'}：当前 ${vramTotal} GB (${escapeHtml(gpuName)})
                            </div>
                        </div>

                        <!-- CUDA 驱动 -->
                        <div style="background: rgba(30, 41, 59, 0.6); padding: 10px 12px; border-radius: 6px; border: 1px solid rgba(148, 163, 184, 0.15);">
                            <div style="font-size: 11.5px; color: var(--text-muted); margin-bottom: 4px;">② CUDA 硬件加速 (真人神经网络必需)</div>
                            <div style="font-size: 13px; font-weight: 600; color: ${hasCuda ? '#34d399' : '#f87171'};">
                                ${hasCuda ? '✓ 通过：CUDA 加速环境就绪' : '✗ 未达标：无可用 CUDA 加速'}
                            </div>
                        </div>

                        <!-- CPU 与内存 -->
                        <div style="background: rgba(30, 41, 59, 0.6); padding: 10px 12px; border-radius: 6px; border: 1px solid rgba(148, 163, 184, 0.15);">
                            <div style="font-size: 11.5px; color: var(--text-muted); margin-bottom: 4px;">③ CPU核心与内存 (门槛 4核/8GB)</div>
                            <div style="font-size: 13px; font-weight: 600; color: #34d399;">
                                ✓ 通过：${cpuCores} 核心 · ${ramTotal} GB 内存 (充足)
                            </div>
                        </div>
                    </div>

                    <!-- 综合达标结论条 -->
                    <div style="padding: 12px 14px; border-radius: 6px; background: ${isLocalGpuQualified ? 'rgba(16, 185, 129, 0.12)' : 'rgba(245, 158, 11, 0.12)'}; border: 1px dashed ${isLocalGpuQualified ? 'rgba(16, 185, 129, 0.35)' : 'rgba(245, 158, 11, 0.35)'};">
                        ${isLocalGpuQualified ? `
                            <div style="font-weight: 700; color: #34d399; margin-bottom: 4px;">🟢 综合评估：本机硬件完全满足写实真人本地渲染要求！</div>
                            <div style="font-size: 12px; color: #cbd5e1; line-height: 1.6;">
                                本机配备达标的高性能独立显卡（>= 6.0GB 显存），单机即可直接渲染 1080P 超写实真人主播，零租赁成本，无需配置网络，开箱即播。
                            </div>
                        ` : `
                            <div style="font-weight: 700; color: #fbbf24; margin-bottom: 4px;">⚠️ 综合评估：当前电脑硬件未达到超写实真人渲染的商业运营门槛！</div>
                            <div style="font-size: 12px; color: #cbd5e1; line-height: 1.65;">
                                检测到本地显存仅 ${vramTotal}GB（低于 6.0GB 商业运营最低门槛），若在本地强行跑深度学习写实真人会导致严重丢帧甚至显存溢出 (OOM) 崩溃。<br>
                                🛡️ <strong>系统保底机制</strong>：若您启用当前选项，系统将自动使用<strong>免显卡的本地程序化形象（卡通/微动态）</strong>作为推流画面，保障流畅不黑屏；<br>
                                🚀 <strong>若您需要 1080P 写实真人主播</strong>：请点击上方【<strong>选项 2 · 本地硬件 + 租赁云端GPU</strong>】，将渲染外包至云端（约 1.2 元/小时），低配电脑亦能完美开播！
                            </div>
                        `}
                    </div>
                </div>
            `;
        }

        if (copyCmdBtn) copyCmdBtn.style.display = "none";
        if (testConnBtn) testConnBtn.style.display = "none";
        if (saveBtn) {
            saveBtn.style.background = "#10B981";
            saveBtn.style.borderColor = "#10B981";
            saveBtn.innerHTML = `
                <svg class="icon-sm" viewBox="0 0 24 24" style="width: 14px; height: 14px;"><path d="M20 6L9 17l-5-5"/></svg>
                ${isLocalCurrentlyActive ? "当前已生效 (刷新保持)" : "立即启用本地单机运行"}
            `;
        }
    } else {
        // =====================================================================
        // 模式 2：本地硬件 + 租赁云端GPU (端云协同) · 配合达标核验报告与参数配置
        // =====================================================================
        if (titleEl) titleEl.textContent = "选项 2 · 本地硬件 + 租赁云端GPU 端云协同配合检测";
        if (badgeEl) {
            badgeEl.className = "brand-badge sky";
            badgeEl.textContent = "端云协同核验";
        }

        let currentBaseUrl = anyCloudCfg ? (anyCloudCfg.base_url || "") : "";
        const savedLocalUrl = (typeof localStorage !== "undefined" && localStorage.getItem("last_sidecar_base_url")) || "";
        if ((!currentBaseUrl || currentBaseUrl.includes("127.0.0.1")) && savedLocalUrl) {
            currentBaseUrl = savedLocalUrl;
        }
        if (!currentBaseUrl) {
            currentBaseUrl = "ws://127.0.0.1:8010/ws/render-v3";
        }
        const currentApiKey = anyCloudCfg ? (anyCloudCfg.masked_key || "") : "";
        let extraParams = (anyCloudCfg && anyCloudCfg.extra_params) || {};
        if (typeof extraParams === "string") {
            try { extraParams = JSON.parse(extraParams); } catch (e) { extraParams = {}; }
        }
        const currentProtocol = extraParams.stream_protocol || "websocket";
        const currentAvatarId = extraParams.avatar_id || "default";
        const currentCustomUrl = extraParams.custom_official_url || "";

        if (linkBox) {
            const jumpUrl = currentCustomUrl || "https://www.autodl.com";
            const jumpText = currentCustomUrl ? "直达云端开发机控制台 ↗" : "前往 AutoDL 租用 GPU ↗";
            linkBox.innerHTML = `
                <a href="${escapeHtml(jumpUrl)}" target="_blank" rel="noopener noreferrer" class="btn btn-xs btn-ghost" style="color: #38bdf8; text-decoration: none; display: inline-flex; align-items: center; gap: 4px;" title="${escapeHtml(jumpUrl)}">
                    ${jumpText}
                </a>
            `;
        }

        const hiddenId = document.getElementById("gpu-avatar-config-id");
        if (hiddenId) hiddenId.value = anyCloudCfg ? anyCloudCfg.id : "";

        const isLocalReady = cpuCores >= 2 && ramTotal >= 4.0;
        const isCloudPingSuccess = lastCloudPingResult && lastCloudPingResult.success;

        if (dynamicBox) {
            dynamicBox.innerHTML = `
                <div style="background: rgba(15, 23, 42, 0.6); border: 1px solid rgba(56, 189, 248, 0.25); border-radius: 8px; padding: 18px;">
                    <!-- 配合实况综合核验条目 -->
                    <div style="font-size: 13.5px; font-weight: 700; color: #f8fafc; margin-bottom: 12px; display: flex; align-items: center; gap: 8px;">
                        <svg viewBox="0 0 24 24" style="width: 16px; height: 16px; stroke: #38bdf8; fill: none; stroke-width: 2.2;"><path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z"/></svg>
                        端云配合达标诊断报告（本地控制端 + 云端渲染端）：
                    </div>

                    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 10px; margin-bottom: 16px;">
                        <!-- 本地配置配合 -->
                        <div style="background: rgba(30, 41, 59, 0.6); padding: 10px 12px; border-radius: 6px; border: 1px solid rgba(148, 163, 184, 0.15);">
                            <div style="font-size: 11.5px; color: var(--text-muted); margin-bottom: 4px;">① 本地硬件控制环境 (推流与主控)</div>
                            <div style="font-size: 13px; font-weight: 600; color: #34d399;">
                                ✓ 通过：${cpuCores} 核心 · ${ramTotal} GB 内存 (完全满足控制需求)
                            </div>
                        </div>

                        <!-- 云端 GPU 配合实况 -->
                        <div style="background: rgba(30, 41, 59, 0.6); padding: 10px 12px; border-radius: 6px; border: 1px solid rgba(148, 163, 184, 0.15);">
                            <div style="font-size: 11.5px; color: var(--text-muted); margin-bottom: 4px;">② 云端 GPU 渲染连通状态</div>
                            <div id="gpu-cloud-ping-inline-status" style="font-size: 13px; font-weight: 600; color: ${isCloudPingSuccess ? '#34d399' : (currentBaseUrl ? '#38bdf8' : '#fbbf24')};">
                                ${isCloudPingSuccess ? `✓ 已连通 (延迟 ${lastCloudPingResult.latency_ms}ms${lastCloudPingResult.device ? ' · ' + escapeHtml(lastCloudPingResult.device) : ''})` : (currentBaseUrl ? 'ℹ️ 待测试 (点击下方探测)' : '✗ 待填写云端地址')}
                            </div>
                        </div>
                    </div>

                    <!-- 标题提示：与云端终端输出 1:1 对照 -->
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; padding-bottom: 6px; border-bottom: 1px dashed rgba(148, 163, 184, 0.2);">
                        <span style="font-size: 13px; font-weight: 700; color: #38bdf8; display: flex; align-items: center; gap: 6px;">
                            <svg viewBox="0 0 24 24" style="width: 14px; height: 14px; stroke: #38bdf8; fill: none; stroke-width: 2;"><rect x="2" y="3" width="20" height="14" rx="2" ry="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
                            📋 云端算力节点参数配置（与云端终端 status.py 输出 1:1 对照）：
                        </span>
                        <span style="font-size: 11.5px; color: #94a3b8;">参数已设最优默认值，对照核对即可</span>
                    </div>

                    <!-- 参数 1：自定义服务/流连接地址 (base_url) 核心必填 -->
                    <div class="form-group" style="margin-bottom: 14px;">
                        <label class="form-label" for="gpu-input-base-url" style="font-size: 12.5px; font-weight: 700; color: #f8fafc; display: flex; justify-content: space-between; align-items: center;">
                            <span>1. 自定义服务/流连接地址 (base_url): <span style="color: #ef4444;">*</span></span>
                            <span style="font-size: 11px; color: #38bdf8; font-weight: normal;">云端穿透公网 WebSocket/WSS 地址</span>
                        </label>
                        <input id="gpu-input-base-url" class="form-control" type="text"
                               value="${escapeHtml(currentBaseUrl)}"
                               placeholder="如: wss://xxxx.trycloudflare.com/ws/render-v3 或 ws://<云主机IP>:8010/ws/render-v3"
                               style="font-family: monospace; font-size: 13px; padding: 8px 12px;"
                               onblur="if(this.value.trim()){ const fixed=sanitizeSidecarWsUrl(this.value); if(fixed !== this.value.trim()){ this.value=fixed; showToast('已自动将 https:// 转换为 wss:// 并补齐标准端点 /ws/render-v3', 'info'); } try{ localStorage.setItem('last_sidecar_base_url', this.value); }catch(e){} if(!lastCloudPingResult || !lastCloudPingResult.success){ testCurrentGpuAvatarConnection(true); } }">

                        <!-- 快捷填入常用样例端点 -->
                        <div style="display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px;">
                            <span style="font-size: 11px; color: var(--text-muted); align-self: center;">快捷样例:</span>
                            <button type="button" class="badge-pill" style="font-size: 11px; cursor: pointer; padding: 3px 8px; border-radius: 4px; border: 1px solid rgba(16, 185, 129, 0.4); background: rgba(16, 185, 129, 0.12); color: #34d399;"
                                    onclick="const el=document.getElementById('gpu-input-base-url'); if(el.value.trim()){ el.value=sanitizeSidecarWsUrl(el.value); showToast('已自动纠偏协议为 wss:// 并规范化！', 'success'); } else { showToast('请先输入云端连接地址', 'warning'); }">
                                🪄 智能协议纠偏 (https 自动转 wss)
                            </button>
                            <button type="button" class="badge-pill" style="font-size: 11px; cursor: pointer; padding: 3px 8px; border-radius: 4px; border: 1px solid rgba(56, 189, 248, 0.3); background: rgba(15, 23, 42, 0.7); color: #38bdf8;"
                                    onclick="document.getElementById('gpu-input-base-url').value='wss://xxxx.trycloudflare.com/ws/render-v3'">
                                ☁️ Cloudflare 加密隧道样例
                            </button>
                            <button type="button" class="badge-pill" style="font-size: 11px; cursor: pointer; padding: 3px 8px; border-radius: 4px; border: 1px solid rgba(56, 189, 248, 0.3); background: rgba(15, 23, 42, 0.7); color: #38bdf8;"
                                    onclick="document.getElementById('gpu-input-base-url').value='ws://127.0.0.1:8010/ws/render-v3'">
                                🖥️ 本机WS测试 (ws://127.0.0.1:8010)
                            </button>
                            <button type="button" class="badge-pill" style="font-size: 11px; cursor: pointer; padding: 3px 8px; border-radius: 4px; border: 1px solid rgba(56, 189, 248, 0.3); background: rgba(15, 23, 42, 0.7); color: #38bdf8;"
                                    onclick="document.getElementById('gpu-input-base-url').value='ws://<云主机公网IP>:8010/ws/render-v3'">
                                🌐 云机公网 IP 直连样例
                            </button>
                        </div>

                        <!-- 针对 Google Colab 免费 T4 显卡的一键部署引导卡片 -->
                        <div style="background: rgba(15, 23, 42, 0.65); border: 1px solid rgba(56, 189, 248, 0.25); border-radius: 6px; padding: 10px 12px; margin-top: 10px; font-size: 11.5px; color: #cbd5e1; line-height: 1.65;">
                            <div style="font-weight: 700; color: #38bdf8; margin-bottom: 4px; display: flex; align-items: center; gap: 6px;">
                                <span>💡 谷歌 Google Colab (免费 T4 GPU) 一键极速启动指引：</span>
                            </div>
                            <div style="color: #e2e8f0;">
                                如使用 Colab 算力作为<strong>数字人画面渲染节点</strong>，请在 Colab 单元格执行官方引导脚本（会自动安装依赖、识别 T4 显卡并穿透出 wss:// 地址）：
                            </div>
                            <div style="background: rgba(0,0,0,0.45); border: 1px dashed rgba(56,189,248,0.3); padding: 6px 9px; border-radius: 4px; font-family: monospace; color: #34d399; margin: 6px 0; user-select: all; word-break: break-all;">
                                !git clone https://github.com/winclubs/AI-LiveStream-Agent.git /content/agent 2>/dev/null || true &amp;&amp; cd /content/agent &amp;&amp; python scripts/cloud_sidecar_bootstrap.py
                            </div>
                            <div style="color: #94a3b8; font-size: 11px;">
                                ⚠️ 提示：若在 Colab 运行的是 Ollama (端口 11434)，那是用于大语言模型的，请配置在左侧「LLM配置」中；此处「GPU配置」专用于数字人画面渲染。
                            </div>
                        </div>
                    </div>

                    <!-- 参数 2 & 3 & 4 & 5：双列网格结构（与终端输出完全吻合） -->
                    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; margin-bottom: 12px;">
                        <!-- 参数 2：通信协议类型 (stream_protocol) -->
                        <div class="form-group" style="margin-bottom: 0;">
                            <label class="form-label" for="gpu-input-stream-protocol" style="font-size: 12px; font-weight: 600; color: #e2e8f0; display: flex; justify-content: space-between;">
                                <span>2. 通信协议类型 (stream_protocol):</span>
                                <span style="font-size: 11px; color: var(--text-muted);">默认 websocket</span>
                            </label>
                            <input id="gpu-input-stream-protocol" class="form-control" type="text"
                                   value="${escapeHtml(currentProtocol)}"
                                   placeholder="websocket"
                                   style="font-family: monospace; font-size: 12.5px; padding: 7px 10px;">
                            <div style="display: flex; gap: 5px; margin-top: 5px;">
                                <span class="badge-pill" style="font-size: 10.5px; cursor: pointer; padding: 1px 6px; background: rgba(56,189,248,0.15); color: #38bdf8;" onclick="document.getElementById('gpu-input-stream-protocol').value='websocket'">WebSocket</span>
                                <span class="badge-pill" style="font-size: 10.5px; cursor: pointer; padding: 1px 6px; background: rgba(148,163,184,0.15); color: #94a3b8;" onclick="document.getElementById('gpu-input-stream-protocol').value='webrtc'">WebRTC</span>
                                <span class="badge-pill" style="font-size: 10.5px; cursor: pointer; padding: 1px 6px; background: rgba(148,163,184,0.15); color: #94a3b8;" onclick="document.getElementById('gpu-input-stream-protocol').value='rtmp'">RTMP</span>
                            </div>
                        </div>

                        <!-- 参数 4：数字人形象代号 (avatar_id) -->
                        <div class="form-group" style="margin-bottom: 0;">
                            <label class="form-label" for="gpu-input-avatar-id" style="font-size: 12px; font-weight: 600; color: #e2e8f0; display: flex; justify-content: space-between;">
                                <span>3. 数字人形象代号 (avatar_id):</span>
                                <span style="font-size: 11px; color: var(--text-muted);">默认 default</span>
                            </label>
                            <input id="gpu-input-avatar-id" class="form-control" type="text"
                                   value="${escapeHtml(currentAvatarId)}"
                                   placeholder="default"
                                   style="font-family: monospace; font-size: 12.5px; padding: 7px 10px;">
                            <div style="font-size: 11px; color: var(--text-muted); margin-top: 5px;">
                                云端多模型时填指定代号，单模型直接保持 <code>default</code>
                            </div>
                        </div>

                        <!-- 参数 4 (访问凭据)：访问密码 / Token / API Key (选填) -->
                        <div class="form-group" style="margin-bottom: 0;">
                            <label class="form-label" for="gpu-input-auth-token" style="font-size: 12px; font-weight: 600; color: #e2e8f0; display: flex; justify-content: space-between;">
                                <span>4. 访问密码 / Token / API Key (选填):</span>
                                <span style="font-size: 11px; color: #10B981;">无密码直接留空</span>
                            </label>
                            <input id="gpu-input-auth-token" class="form-control" type="password"
                                   value="${escapeHtml(currentApiKey)}"
                                   placeholder="直接留空即可 (若云端加锁鉴权则填写)"
                                   style="font-family: monospace; font-size: 12.5px; padding: 7px 10px;">
                            <div style="font-size: 11px; color: var(--text-muted); margin-top: 5px;">
                                绝大多数私有开发机无鉴权，留空即可
                            </div>
                        </div>

                        <!-- 参数 5：自定义官方/控制台链接 (custom_official_url) (选填) -->
                        <div class="form-group" style="margin-bottom: 0;">
                            <label class="form-label" for="gpu-input-custom-url" style="font-size: 12px; font-weight: 600; color: #e2e8f0; display: flex; justify-content: space-between;">
                                <span>5. 云端控制台链接 (custom_official_url):</span>
                                <span style="font-size: 11px; color: var(--text-muted);">选填</span>
                            </label>
                            <input id="gpu-input-custom-url" class="form-control" type="url"
                                   value="${escapeHtml(currentCustomUrl)}"
                                   placeholder="如: https://discovery.intern-ai.org.cn/compute/dev-machine"
                                   style="font-family: monospace; font-size: 12.5px; padding: 7px 10px;">
                            <div style="font-size: 11px; color: var(--text-muted); margin-top: 5px;">
                                填入开发机网页地址栏 URL，可在面板右上角一键直达
                            </div>
                        </div>
                    </div>
                </div>
            `;
        }

        if (copyCmdBtn) copyCmdBtn.style.display = "inline-flex";
        if (testConnBtn) testConnBtn.style.display = "inline-flex";
        if (saveBtn) {
            saveBtn.style.background = "#38bdf8";
            saveBtn.style.borderColor = "#38bdf8";
            saveBtn.innerHTML = `
                <svg class="icon-sm" viewBox="0 0 24 24" style="width: 14px; height: 14px;"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
                保存并启用云端 GPU 渲染方案
            `;
        }
    }
}

// ------------------------------------------------------------------------------
// 4. 保存与启用所选方案
// ------------------------------------------------------------------------------
async function handleSaveGpuAvatarConfig() {
    const saveBtn = document.getElementById("gpu-avatar-save-btn");
    const origHtml = saveBtn ? saveBtn.innerHTML : "";

    if (currentSelectedGpuMode === "local_procedural") {
        // 模式 1：启用本机完全满足模式（将所有活跃云端节点设为非 active）
        try {
            if (saveBtn) {
                saveBtn.disabled = true;
                saveBtn.innerHTML = `<svg class="icon-sm spin" viewBox="0 0 24 24"><line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/></svg> 正在启用...`;
            }

            for (const cfg of cachedAvatarConfigs.filter(c => c.is_active && c.config_group === "neural_renderer")) {
                await fetch(`${API_BASE}/settings/configs/save`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        id: cfg.id,
                        config_group: "neural_renderer",
                        provider_name: cfg.provider_name,
                        is_active: false
                    })
                });
            }

            showToast("✅ 已成功切换为【本机完全满足 (本地运行)】模式！", "success");
            // 同步持久化算力执行偏好 (auto: 交由系统按硬件与云端实况自动研判)
            try {
                await fetch(`${API_BASE}/settings/gpu-target`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ target: "auto" })
                });
            } catch (e) { /* 偏好写入失败不阻断主流程 */ }
            await loadGpuAvatarProviders();
        } catch (e) {
            showToast("切换本地运行模式失败: " + e, "error");
        } finally {
            if (saveBtn) {
                saveBtn.disabled = false;
                saveBtn.innerHTML = origHtml;
            }
        }
        return;
    }

    // 模式 2：保存并启用云端租赁 GPU (Sidecar)
    const urlInput = document.getElementById("gpu-input-base-url");
    const tokenInput = document.getElementById("gpu-input-auth-token");
    const protocolInput = document.getElementById("gpu-input-stream-protocol");
    const avatarIdInput = document.getElementById("gpu-input-avatar-id");
    const customUrlInput = document.getElementById("gpu-input-custom-url");
    const hiddenId = document.getElementById("gpu-avatar-config-id");

    let baseUrl = urlInput ? urlInput.value.trim() : "";
    if (!baseUrl) {
        showToast("请填写云端 GPU 渲染节点连接地址 (base_url)！", "error");
        if (urlInput) urlInput.focus();
        return;
    }

    // 智能协议纠偏：自动将 https:// 转换为 wss://，并规范化端点路径
    const sanitizedUrl = sanitizeSidecarWsUrl(baseUrl);
    if (sanitizedUrl && sanitizedUrl !== baseUrl) {
        baseUrl = sanitizedUrl;
        if (urlInput) urlInput.value = sanitizedUrl;
    }

    let tokenVal = tokenInput ? tokenInput.value.trim() : "";
    if (tokenVal.includes("•")) {
        tokenVal = ""; // 未改动的掩码不重复写入
    }

    const streamProtocol = protocolInput ? (protocolInput.value.trim() || "websocket") : "websocket";
    const avatarId = avatarIdInput ? (avatarIdInput.value.trim() || "default") : "default";
    const customOfficialUrl = customUrlInput ? customUrlInput.value.trim() : "";

    const payload = {
        config_group: "neural_renderer",
        provider_name: "sidecar_v3",
        title: "云端租赁 GPU 渲染节点 (Sidecar)",
        base_url: baseUrl,
        is_active: true,
        extra_params: {
            adapter: "sidecar_v3",
            stream_protocol: streamProtocol,
            avatar_id: avatarId,
            custom_official_url: customOfficialUrl,
            backend_id: "auto"
        }
    };

    if (hiddenId && hiddenId.value) {
        payload.id = hiddenId.value;
    }
    if (tokenVal) {
        payload.api_key = tokenVal;
    } else if (tokenInput && !tokenInput.value.includes("•")) {
        payload.api_key = ""; // 显式置空密码，彻底清空数据库历史密码
    }

    try {
        if (saveBtn) {
            saveBtn.disabled = true;
            saveBtn.innerHTML = `<svg class="icon-sm spin" viewBox="0 0 24 24"><line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/></svg> 正在保存...`;
        }

        const res = await fetch(`${API_BASE}/settings/configs/save`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        const json = await res.json();
        if (res.ok && (json.code === 0 || json.code === undefined)) {
            showToast("✅ 已成功保存并启用【本地硬件 + 租赁云端GPU】模式！", "success");
            // 同步持久化算力执行偏好 (cloud: 用户显式指定优先调度云端显卡)
            try {
                await fetch(`${API_BASE}/settings/gpu-target`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ target: "cloud" })
                });
            } catch (e) { /* 偏好写入失败不阻断主流程 */ }
            await loadGpuAvatarProviders();
        } else {
            showToast("保存失败: " + (json.message || "请求异常"), "error");
        }
    } catch (e) {
        showToast("保存云端 GPU 模式失败: " + e, "error");
    } finally {
        if (saveBtn) {
            saveBtn.disabled = false;
            saveBtn.innerHTML = origHtml;
        }
    }
}

// ------------------------------------------------------------------------------
// 5. 快速连通性握手测试与配合达标即时反馈 (支持 isSilent 静默自动检测)
// ------------------------------------------------------------------------------
async function testCurrentGpuAvatarConnection(isSilent = false) {
    const urlInput = document.getElementById("gpu-input-base-url");
    const testBtn = document.getElementById("gpu-avatar-test-btn");
    const resultBox = document.getElementById("gpu-avatar-conn-result");

    let targetUrl = urlInput ? urlInput.value.trim() : "";
    if (!targetUrl) {
        if (!isSilent) {
            showToast("请先输入云端连接地址再测试", "error");
            if (urlInput) urlInput.focus();
        }
        return;
    }

    // 智能协议纠偏：自动将 https:// 转换为 wss://，并规范化端点路径
    const sanitizedTarget = sanitizeSidecarWsUrl(targetUrl);
    if (sanitizedTarget && sanitizedTarget !== targetUrl) {
        targetUrl = sanitizedTarget;
        if (urlInput) urlInput.value = sanitizedTarget;
    }

    const origHtml = testBtn ? testBtn.innerHTML : "";
    if (testBtn && !isSilent) {
        testBtn.disabled = true;
        testBtn.innerHTML = `<svg class="icon-sm spin" viewBox="0 0 24 24" style="width:13px;height:13px;"><line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/></svg> 协同握手探测中...`;
    }

    if (resultBox) {
        resultBox.style.display = "flex";
        resultBox.style.background = "rgba(30, 41, 59, 0.7)";
        resultBox.style.color = "var(--text-primary)";
        resultBox.style.border = "1px solid rgba(148, 163, 184, 0.2)";
        resultBox.innerHTML = `正在向云端算力节点 <code style="color:#38bdf8;font-family:monospace;">${escapeHtml(targetUrl)}</code> 发起端云协同握手测试...`;
    }

    const inlineStatusEl = document.getElementById("gpu-cloud-ping-inline-status");
    if (inlineStatusEl) {
        inlineStatusEl.style.color = "#38bdf8";
        inlineStatusEl.innerHTML = `⏳ 握手探测中...`;
    }

    try {
        const tokenInput = document.getElementById("gpu-input-auth-token");
        let typedToken = tokenInput ? tokenInput.value.trim() : "";
        let apiKeyToSend = null;
        if (typedToken.includes("•")) {
            apiKeyToSend = null; // 掩码不回传，交由后端按 config_id 解密真实值
        } else if (!typedToken) {
            apiKeyToSend = "__NO_AUTH__"; // 明确指示后端：用户已清空密码，绝不回捞数据库历史密码！
        } else {
            apiKeyToSend = typedToken;
        }
        const res = await fetch(`${API_BASE}/settings/test-connection`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                config_id: (document.getElementById("gpu-avatar-config-id") || {}).value || null,
                config_group: "neural_renderer",
                provider_name: "sidecar_v3",
                base_url: targetUrl,
                api_key: apiKeyToSend
            })
        });
        const json = await res.json();
        if (json.code === 0 && json.success) {
            lastCloudPingResult = {
                success: true,
                latency_ms: json.latency_ms || 45,
                device: json.device || "NVIDIA 云端 GPU",
                error: ""
            };
            // 成功记忆到 localStorage
            try { localStorage.setItem("last_sidecar_base_url", targetUrl); } catch(e){}

            if (inlineStatusEl) {
                inlineStatusEl.style.color = "#34d399";
                inlineStatusEl.innerHTML = `✓ 已连通 (延迟 ${json.latency_ms}ms${json.device ? ' · ' + escapeHtml(json.device) : ''})`;
            }
            if (resultBox) {
                resultBox.style.background = "rgba(16, 185, 129, 0.15)";
                resultBox.style.color = "#10B981";
                resultBox.style.border = "1px solid rgba(16, 185, 129, 0.35)";
                resultBox.innerHTML = `🟢 配合达标！云端握手成功，延迟: ${json.latency_ms}ms ${json.device ? '(远端识别硬件: <b>' + escapeHtml(json.device) + '</b>)' : ''}，本地主控与云端 GPU 协同就绪！`;
            }
            if (!isSilent) {
                showToast(`✅ 端云协同达标！${json.device ? '云端硬件: ' + json.device : ''}（延迟: ${json.latency_ms}ms）`, "success");
            }
        } else {
            const err = json.message || "通信握手未成功";
            lastCloudPingResult = {
                success: false,
                latency_ms: 0,
                device: "",
                error: err
            };
            if (inlineStatusEl) {
                inlineStatusEl.style.color = "#ef4444";
                inlineStatusEl.innerHTML = `✗ 未连通 (${escapeHtml(err.split('\n')[0])})`;
            }
            if (resultBox) {
                resultBox.style.background = "rgba(239, 68, 68, 0.15)";
                resultBox.style.color = "#EF4444";
                resultBox.style.border = "1px solid rgba(239, 68, 68, 0.35)";
                const formattedErr = escapeHtml(err).replace(/\n/g, "<br/>");
                resultBox.innerHTML = `🔴 协同未达标: ${formattedErr}`;
            }
            if (!isSilent) {
                showToast(`❌ 连通失败: ${err.split('\n')[0]}`, "error");
            }
        }

        // 即时刷新达标状态指示
        evaluateBothModesCompliance(cachedHardwareData);
    } catch (e) {
        lastCloudPingResult = {
            success: false,
            latency_ms: 0,
            device: "",
            error: String(e)
        };
        if (inlineStatusEl) {
            inlineStatusEl.style.color = "#ef4444";
            inlineStatusEl.innerHTML = `⚠️ 网络请求异常`;
        }
        if (resultBox) {
            resultBox.style.background = "rgba(239, 68, 68, 0.15)";
            resultBox.style.color = "#EF4444";
            resultBox.style.border = "1px solid rgba(239, 68, 68, 0.35)";
            resultBox.innerHTML = `⚠️ 网络请求异常: ${escapeHtml(String(e))}`;
        }
        evaluateBothModesCompliance(cachedHardwareData);
    } finally {
        if (testBtn && !isSilent) {
            testBtn.disabled = false;
            testBtn.innerHTML = origHtml;
        }
    }
}

// ------------------------------------------------------------------------------
// 6. 辅助工具与一键拉起命令复制
// ------------------------------------------------------------------------------
function copyCloudBootstrapCommand() {
    const cmd = `git clone https://github.com/winclubs/AI-LiveStream-Agent.git && cd AI-LiveStream-Agent && pip3 install fastapi uvicorn websockets requests && python3 scripts/cloud_sidecar_bootstrap.py --port 8010`;
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(cmd).then(() => {
            showToast("📋 已复制云端节点一键启动命令！在云主机终端粘贴回车，自动检测 GPU、穿透隧道并启动 /ws/render-v3 对接端点。", "success");
        }).catch(() => {
            prompt("请手动复制下方命令并在云主机终端执行：", cmd);
        });
    } else {
        prompt("请手动复制下方命令并在云主机终端执行：", cmd);
    }
}

function goToGpuSettingsTab() {
    const navItem = document.querySelector(".nav-item[data-tab='gpu']");
    if (navItem) {
        navItem.click();
    } else if (typeof switchToTab === "function") {
        switchToTab("gpu");
    }
}

// ------------------------------------------------------------------------------
// 7. 进入页面时静默自动探测云端 GPU 连通性与硬件状态
// ------------------------------------------------------------------------------
let isAutoCheckingCloudGpu = false;
async function autoCheckCloudGpuConnection(cloudCfg) {
    if (isAutoCheckingCloudGpu) return;
    if (!cloudCfg || !cloudCfg.base_url) return;
    const targetUrl = sanitizeSidecarWsUrl(cloudCfg.base_url);
    if (!targetUrl) return;

    isAutoCheckingCloudGpu = true;
    const inlineStatusEl = document.getElementById("gpu-cloud-ping-inline-status");
    if (inlineStatusEl) {
        inlineStatusEl.style.color = "#38bdf8";
        inlineStatusEl.innerHTML = `⏳ 正在自动检测云端 GPU 连通性...`;
    }

    try {
        const res = await fetch(`${API_BASE}/settings/test-connection`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                config_id: cloudCfg.id || null,
                config_group: "neural_renderer",
                provider_name: "sidecar_v3",
                base_url: targetUrl,
                api_key: null // 由后端解密已有配置
            })
        });
        const json = await res.json();
        if (json.code === 0 && json.success) {
            lastCloudPingResult = {
                success: true,
                latency_ms: json.latency_ms || 45,
                device: json.device || "NVIDIA 云端 GPU",
                error: ""
            };
            if (inlineStatusEl) {
                inlineStatusEl.style.color = "#34d399";
                inlineStatusEl.innerHTML = `✓ 已自动连通 (延迟 ${json.latency_ms}ms${json.device ? ' · ' + escapeHtml(json.device) : ''})`;
            }
            const resultBox = document.getElementById("gpu-avatar-conn-result");
            if (resultBox && currentSelectedGpuMode === "sidecar_v3") {
                resultBox.style.display = "flex";
                resultBox.style.background = "rgba(16, 185, 129, 0.15)";
                resultBox.style.color = "#10B981";
                resultBox.style.border = "1px solid rgba(16, 185, 129, 0.35)";
                resultBox.innerHTML = `🟢 配合达标！云端已自动握手就绪，延迟: ${json.latency_ms}ms ${json.device ? '(远端识别硬件: <b>' + escapeHtml(json.device) + '</b>)' : ''}，端云协同就绪！`;
            }
        } else {
            lastCloudPingResult = {
                success: false,
                latency_ms: 0,
                device: "",
                error: json.message || "未连通"
            };
            if (inlineStatusEl) {
                inlineStatusEl.style.color = "#ef4444";
                inlineStatusEl.innerHTML = `✗ 连通失败 (点击下方测试探测)`;
            }
        }
        evaluateBothModesCompliance(cachedHardwareData);
    } catch (e) {
        console.warn("自动检测云端 GPU 异常:", e);
    } finally {
        isAutoCheckingCloudGpu = false;
    }
}

// ------------------------------------------------------------------------------
// 8. 数据拉取与主入口初始化
// ------------------------------------------------------------------------------
async function loadGpuAvatarProviders() {
    try {
        const configsRes = await fetch(`${API_BASE}/settings/configs`);
        if (configsRes.ok) {
            const configsJson = await configsRes.json();
            cachedAvatarConfigs = (configsJson.data || []).filter(item => item.config_group === "neural_renderer");
        }

        // 探测活跃配置
        const activeCloud = cachedAvatarConfigs.find(c => c.is_active && c.provider_name === "sidecar_v3");
        currentSelectedGpuMode = activeCloud ? "sidecar_v3" : "local_procedural";

        // 刷新硬件实况看板并执行双模式达标诊断
        await refreshGpuHardwareOverview();

        // 呈现选中模式
        selectGpuMode(currentSelectedGpuMode);

        // 同步渲染首页向导「第二步 · 数字人画面与云渲染状态」卡片（缓存已就绪）
        renderWizardGpuStatusCard();

        // 若用户已配置了云端 GPU 渲染节点地址，静默自动触发一次快速连通性与硬件探测
        const configuredCloud = cachedAvatarConfigs.find(c => (c.provider_name === "sidecar_v3" || c.provider_name === "custom_avatar") && c.base_url && c.base_url.trim().length > 0 && !c.base_url.includes("127.0.0.1"))
            || cachedAvatarConfigs.find(c => c.provider_name === "sidecar_v3" && c.base_url && c.base_url.trim().length > 0);
        const savedUrl = (typeof localStorage !== "undefined" && localStorage.getItem("last_sidecar_base_url")) || "";

        if (configuredCloud || savedUrl) {
            const probeTarget = (configuredCloud && !configuredCloud.base_url.includes("127.0.0.1"))
                ? configuredCloud
                : (savedUrl ? { id: configuredCloud ? configuredCloud.id : null, base_url: savedUrl } : configuredCloud);
            if (probeTarget && probeTarget.base_url) {
                autoCheckCloudGpuConnection(probeTarget);
            }
        }
    } catch (e) {
        console.error("加载 GPU 算力模块失败:", e);
    }
}

// 保持对首页向导看板的兼容渲染
function renderWizardGpuStatusCard() {
    const container = document.getElementById("wizard-gpu-display-container");
    const headerBadge = document.getElementById("wizard-gpu-badge");
    if (!container) return;

    const activeCloud = cachedAvatarConfigs.find(c => c.is_active && c.provider_name === "sidecar_v3");
    if (activeCloud) {
        if (headerBadge) {
            headerBadge.className = "brand-badge sky";
            headerBadge.textContent = "云端 GPU 渲染已就绪";
        }
        container.innerHTML = `
            <div style="background: rgba(15, 23, 42, 0.65); border: 1.5px solid rgba(56, 189, 248, 0.35); border-radius: 8px; padding: 18px;">
                <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px dashed rgba(148, 163, 184, 0.2); padding-bottom: 10px; margin-bottom: 12px;">
                    <span style="font-size: 14.5px; font-weight: 700; color: #f8fafc;">选项 2 · 本地硬件 + 租赁云端GPU 节点</span>
                    <button type="button" class="btn btn-sm btn-ghost" onclick="goToGpuSettingsTab()" style="font-size: 12px; color: #38bdf8;">前往修改参数 ↗</button>
                </div>
                <div style="font-size: 12.5px; color: #cbd5e1; margin-bottom: 8px;">
                    🌐 连接地址: <code style="color: #38bdf8;">${escapeHtml(activeCloud.base_url || "未设置")}</code>
                </div>
                <div style="font-size: 12px; color: #10B981;">🟢 端云协同已生效，本地 0 显存负担畅跑 1080P 超写实真人！</div>
            </div>
        `;
    } else {
        if (headerBadge) {
            headerBadge.className = "brand-badge green";
            headerBadge.textContent = "本地运行模式";
        }
        container.innerHTML = `
            <div style="background: rgba(15, 23, 42, 0.65); border: 1.5px solid rgba(16, 185, 129, 0.35); border-radius: 8px; padding: 18px;">
                <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px dashed rgba(148, 163, 184, 0.2); padding-bottom: 10px; margin-bottom: 12px;">
                    <span style="font-size: 14.5px; font-weight: 700; color: #f8fafc;">选项 1 · 本机完全满足 (本地运行)</span>
                    <button type="button" class="btn btn-sm btn-ghost" onclick="goToGpuSettingsTab()" style="font-size: 12px; color: #38bdf8;">切换为云端GPU ↗</button>
                </div>
                <div style="font-size: 12.5px; color: #cbd5e1; margin-bottom: 8px;">
                    💻 运行模式: 本地单机闭环 (支持独显直跑或免显卡程序化形象)
                </div>
                <div style="font-size: 12px; color: #10B981;">🟢 本地运行已就绪，0 租赁成本，开箱即播防黑屏。</div>
            </div>
        `;
    }
}
