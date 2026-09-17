
const KNOWN_AVATAR_PROVIDER_INFO = {
    local_procedural: {
        title: "选项 1 · 本地轻量卡通形象",
        logoSvg: "/static/svg/avatar_local_procedural.svg",
        badges: [
            { text: "0元完全免费", cls: "green" },
            { text: "免独立显卡", cls: "green" },
            { text: "开箱即用", cls: "sky" }
        ],
        summary: "由本机直接生成 2D 动效卡通形象，无需任何显卡或云端账号，即开即播。",
        visual: "2D 灵动卡通主播画面（自然微动态/呼吸/眨眼/口型同步，支持低配真人微动态羽化）",
        hardware: "无需独立显卡，任意双核以上普通办公电脑/轻薄本畅跑，0元完全免费",
        audience: "小白新手试播、无独显电脑、追求绝对稳定与 0 成本首选（强烈推荐）",
        plain: "💡 为什么强烈推荐新手先选这个？它就像开箱即用的“安全驾驶模式”，电脑无需任何昂贵显卡，打开就能直接开播；并且后续就算你换了更高级的真人服务，系统也会把它作为暗中守护的备用画面，一旦网络中断自动顶上，保障直播永不黑屏！"
    },
    sidecar_v3: {
        title: "选项 2 · 自建真人渲染 (Sidecar)",
        logoSvg: "/static/svg/avatar_sidecar_v3.svg",
        officialUrl: "https://www.autodl.com",
        officialLabel: "AutoDL算力 ↗",
        badges: [
            { text: "真人高保真", cls: "amber" },
            { text: "可租云GPU(约1.2元/h)", cls: "green" },
            { text: "逐帧口型对齐", cls: "sky" }
        ],
        summary: "将耗显卡的真人画面外包给独显电脑或租用云GPU(AutoDL等)，让低配电脑也能做高保真真人直播。",
        visual: "1080P 高清写实真人视频（精确逐帧音素级对齐发音口型）",
        hardware: "本机配备独立显卡(推荐 RTX 3060 6GB+) 或 按小时租用云端 GPU(约1.2元/小时)",
        audience: "追求真人主播质感、想用低配轻薄本通过租用云算力实现真人出镜的团队与个人",
        plain: "💡 什么是“自建云端渲染 / Sidecar”？让AI生成逼真写实的真人视频非常消耗显卡。所谓“Sidecar（外挂伴侣）”，就是把这个非常吃显卡的画面合成计算外包给一台带独显的机器或按小时租用的便宜云GPU（如 AutoDL，约1.2元/小时），计算完成后把高清真人视频实时传回。低配轻薄本花极低成本就能瞬间拥有顶配真人主播！"
    },
    liveavatar_lite: {
        title: "选项 3 · LiveAvatar 开放平台",
        logoSvg: "/static/svg/avatar_liveavatar.svg",
        officialUrl: "https://liveavatar.com",
        officialLabel: "平台官网 ↗",
        badges: [
            { text: "商业SaaS", cls: "sky" },
            { text: "免本地显卡", cls: "green" },
            { text: "云端直推", cls: "amber" }
        ],
        summary: "第三方商业数字人 SaaS 云服务，云端机房生成画面流，本地普通办公电脑即可开播。",
        visual: "商业级云端真人超写实视频画面（高拟真发丝与表情微动）",
        hardware: "本地电脑零显卡要求，需官方开通的商业授权 API Key",
        audience: "拥有商业授权账号、追求大厂现成数字人资产的专业团队",
        plain: "💡 适合已采购第三方成熟数字人 SaaS 服务的用户，画面由官方机房全权生成推流，本地电脑只需发送互动文字即可驱动，免除自建模型的运维成本。"
    },
    aliyun_avatar: {
        title: "选项 4 · 阿里云万相数字人",
        logoSvg: "/static/svg/avatar_aliyun.svg",
        officialUrl: "https://www.aliyun.com/product/ai/avatar",
        officialLabel: "阿里官网 ↗",
        badges: [
            { text: "阿里官方", cls: "sky" },
            { text: "完全免本地显卡", cls: "green" },
            { text: "云端直推RTMP", cls: "amber" }
        ],
        summary: "阿里云机房直接渲染并支持直推 RTMP 直播流，本地电脑零显卡消耗，超写实真人质感。",
        visual: "阿里云万相官方超写实逼真真人主播（工业级渲染质感）",
        hardware: "本地电脑零显卡要求，需阿里云企业商用认证与万相服务权限",
        audience: "品牌企业官方旗舰店、大厂商用客户",
        plain: "💡 阿里云企业级解决方案，依托阿里庞大算力机房直接生成推流直播源，适合品牌级企业开播需求。"
    },
    tencent_avatar: {
        title: "选项 5 · 腾讯云智能数智人",
        logoSvg: "/static/svg/avatar_tencent.svg",
        officialUrl: "https://cloud.tencent.com/product/ivh",
        officialLabel: "腾讯官网 ↗",
        badges: [
            { text: "腾讯官方数智人", cls: "sky" },
            { text: "云端渲染免显卡", cls: "green" },
            { text: "机房直推RTMP", cls: "amber" }
        ],
        summary: "腾讯云端机房完成声画实时合成并生成 RTMP 流，不占用本地显卡，在 OBS 拉流即可开播。",
        visual: "腾讯云 IVH 互动数智人超写实视频（细腻眼神交流与肢体动作）",
        hardware: "本地电脑零显卡要求，需腾讯云账号开通商用互动数智人权限",
        audience: "拥有腾讯云数智人商用授权的企业直播间",
        plain: "💡 腾讯官方数智人云端实时合流方案，支持直接推流至主流平台，画面质量卓越稳定。"
    },
    custom_avatar: {
        title: "选项 6 · 自定义数字人 / 远端流服务",
        logoSvg: "/static/svg/avatar_custom.svg",
        officialUrl: "https://github.com/winclubs/AI-LiveStream-Agent",
        officialLabel: "自定义入口 ↗",
        badges: [
            { text: "自定义服务", cls: "sky" },
            { text: "私有自建", cls: "green" },
            { text: "灵活扩展", cls: "amber" }
        ],
        summary: "自由对接您的自建数字人服务、私有 GPU 云机房、第三方未预置的流媒体服务或自定义 WebSocket 网关。",
        visual: "视您接入的外部自研服务或私有流媒体画面而定",
        hardware: "视自建服务器配置而定，本地电脑零显卡负担，支持 SSH 加密隧道",
        audience: "拥有自研渲染服务、私有机房或需接入私有直播流的进阶开发者与机构",
        plain: "💡 提供最大化自由度！无论您自研了数字人模型、搭建了私有渲染集群，还是有特殊的 RTMP/WebSocket 直播流地址，均可在此自由配置并受系统统一调度。"
    }
};

let globalAvatarCatalog = [];
let globalAvatarConfigs = [];
let selectedAvatarProviderId = "local_procedural";
let currentEditingAvatarConfigId = "";
let cachedAvatarDecryptedKeys = {};

async function toggleAvatarSecretVisibility(inputId, configId) {
    const input = document.getElementById(inputId);
    const eyeBtn = document.getElementById(`${inputId}-eye`);
    if (!input || !eyeBtn) return;
    const eyeShowSvg = `<svg viewBox="0 0 24 24" style="width: 15px; height: 15px; stroke: currentColor; fill: none; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round;"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`;
    const eyeHideSvg = `<svg viewBox="0 0 24 24" style="width: 15px; height: 15px; stroke: currentColor; fill: none; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round;"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>`;

    if (input.type === "password") {
        if (configId && (!input.value || input.value.includes("•"))) {
            if (cachedAvatarDecryptedKeys[configId]) {
                input.value = cachedAvatarDecryptedKeys[configId];
            } else {
                try {
                    eyeBtn.style.opacity = "0.5";
                    const res = await fetch(`${API_BASE}/settings/configs/${configId}/raw-key`);
                    const json = await res.json();
                    if (json.code === 0) {
                        const rk = json.raw_key || "";
                        cachedAvatarDecryptedKeys[configId] = rk;
                        input.value = rk;
                    }
                } catch (e) {
                    console.warn("拉取数字人密钥明文异常:", e);
                } finally {
                    eyeBtn.style.opacity = "1";
                }
            }
        }
        input.type = "text";
        eyeBtn.innerHTML = eyeHideSvg;
        eyeBtn.title = "点击隐藏密码";
        eyeBtn.style.color = "#10B981";
    } else {
        input.type = "password";
        eyeBtn.innerHTML = eyeShowSvg;
        eyeBtn.title = "点击显示明文";
        eyeBtn.style.color = "#94a3b8";
    }
}

async function fetchAvatarRegistryAndConfigs() {
    let registryJson = { code: 0, data: [] };
    let configsJson = { code: 0, data: [] };

    try {
        const [registryRes, configsRes] = await Promise.all([
            fetch(`${API_BASE}/settings/avatar/providers`),
            fetch(`${API_BASE}/settings/configs`)
        ]);
        if (registryRes.ok) registryJson = await registryRes.json();
        if (configsRes.ok) configsJson = await configsRes.json();
    } catch (err) {
        console.warn("拉取数字人注册表网络响应受阻，启用系统预置方案兜底:", err);
    }

    const order = { local_procedural: 0, sidecar_v3: 1, liveavatar_lite: 2, aliyun_avatar: 3, tencent_avatar: 4, custom_avatar: 5 };
    let catalog = Array.isArray(registryJson.data) && registryJson.data.length > 0 ? [...registryJson.data] : [];

    // 若接口尚未准备好或网络异常，通过本地已知元数据兜底构建 6 大主流方案
    if (catalog.length === 0) {
        catalog = Object.entries(KNOWN_AVATAR_PROVIDER_INFO).map(([pid, pInfo]) => ({
            id: pid,
            name: pInfo.title,
            status: "available",
            selectable: true,
            configuration_mode: pid === "local_procedural" ? "none" : "instance",
            official_url: pInfo.officialUrl || ""
        }));
    }

    if (!catalog.some(item => item.id === "custom_avatar")) {
        catalog.push({
            id: "custom_avatar",
            name: "选项 6 · 自定义数字人 / 远端流服务（灵活接入）",
            status: "available",
            selectable: true,
            configuration_mode: "instance",
            official_url: "https://github.com/winclubs/AI-LiveStream-Agent",
            ui_fields: [
                {
                    path: "base_url",
                    label: "自定义服务/流连接地址 (WebSocket / RTMP / HTTP):",
                    type: "url",
                    required: true,
                    default: "ws://127.0.0.1:8010/ws/render-v3",
                    tip: "您的自建数字人渲染服务、私有网关或 RTMP 流地址（支持 ws://, wss://, http://, https://, rtmp://）。",
                    pills: [
                        { text: "本地WS: ws://127.0.0.1:8010/ws/render-v3", val: "ws://127.0.0.1:8010/ws/render-v3" },
                        { text: "RTMP流: rtmp://127.0.0.1:1935/live/avatar", val: "rtmp://127.0.0.1:1935/live/avatar" },
                        { text: "远程WSS: wss://my-gpu-node.com/ws/render", val: "wss://my-gpu-node.com/ws/render" }
                    ]
                },
                {
                    path: "api_key",
                    label: "访问密码 / Token / API Key (选填):",
                    type: "secret",
                    required: false,
                    tip: "连接您自定义服务端所需的安全认证密钥，系统将加密存储；无鉴权可直接留空。"
                },
                {
                    path: "extra_params.stream_protocol",
                    label: "通信协议类型:",
                    type: "text",
                    default: "websocket",
                    tip: "与自定义服务通信所使用的协议（支持 websocket、rtmp、webrtc、http）。",
                    pills: [
                        { text: "WebSocket", val: "websocket" },
                        { text: "RTMP直播流", val: "rtmp" },
                        { text: "WebRTC低延时", val: "webrtc" }
                    ]
                }
            ]
        });
    }

    globalAvatarCatalog = catalog.sort((a, b) => (order[a.id] ?? 99) - (order[b.id] ?? 99));
    globalAvatarConfigs = (configsJson.data || []).filter(item => item.config_group === "neural_renderer");

    // 优先寻找既处于激活状态且校验通过的实例
    let active = globalAvatarConfigs.find(item => item.is_active && (!item.provider_validation || item.provider_validation.valid));
    if (!active) {
        // 容错兜底：若有 active 实例，优先展示该激活方案
        active = globalAvatarConfigs.find(item => item.is_active);
    }
    if (active && (active.adapter_id || active.provider_name)) {
        selectedAvatarProviderId = active.adapter_id || active.provider_name;
        currentEditingAvatarConfigId = active.id;
    }
}

async function loadGpuAvatarProviders() {
    try {
        await fetchAvatarRegistryAndConfigs();
        renderAvatarProvidersUI("gpu");
    } catch (e) {
        console.error("加载 GPU Avatar Providers 失败:", e);
        const cardsBox = document.getElementById("gpu-avatar-provider-cards");
        if (cardsBox) {
            cardsBox.innerHTML = `
                <div class="hw-cell" style="grid-column: 1 / -1; text-align: center; color: var(--signal-danger); padding: 24px;">
                    <div>⚠️ 加载数字人配置时遇到异常: ${escapeHtml(String(e))}</div>
                    <button class="btn btn-sm btn-secondary" style="margin-top: 10px;" onclick="loadGpuAvatarProviders()">点击重试加载</button>
                </div>
            `;
        }
    }
}

async function loadWizardAvatarProviders() {
    try {
        await fetchAvatarRegistryAndConfigs();
        renderWizardGpuStatusCard();
    } catch (e) {
        console.error("加载向导 GPU 状态看板失败:", e);
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

async function quickEnableLocalProceduralAvatar() {
    try {
        for (const cfg of globalAvatarConfigs.filter(c => c.is_active)) {
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
        showToast("已成功启用【本地轻量卡通】方案！", "success");
        await loadGpuAvatarProviders();
        await loadWizardAvatarProviders();
    } catch (e) {
        showToast("启用本地方案失败: " + e, "error");
    }
}

async function testWizardGpuConnection(targetUrl, adapterId) {
    const btn = document.getElementById("wizard-gpu-ping-btn");
    const statusBox = document.getElementById("wizard-gpu-ping-status");
    const origHtml = btn ? btn.innerHTML : "";

    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<svg class="icon-sm spin" viewBox="0 0 24 24" style="width:13px;height:13px;"><line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"/><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"/><line x1="2" y1="12" x2="6" y2="12"/><line x1="18" y1="12" x2="22" y2="12"/><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"/><line x1="16.24" y1="7.76" x2="19.07" y2="4.93"/></svg> 探测中...`;
    }

    if (statusBox) {
        statusBox.style.display = "inline-flex";
        statusBox.style.background = "rgba(30, 41, 59, 0.7)";
        statusBox.style.color = "var(--text-primary)";
        statusBox.style.border = "1px solid rgba(148, 163, 184, 0.2)";
        statusBox.innerHTML = `正在向 <code style="color:#38bdf8;font-family:monospace;">${escapeHtml(targetUrl)}</code> 发起握手探测...`;
    }

    try {
        const res = await fetch(`${API_BASE}/settings/test-connection`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                config_group: "neural_renderer",
                provider_name: adapterId,
                base_url: targetUrl
            })
        });
        const json = await res.json();
        if (json.code === 0 && json.success) {
            if (statusBox) {
                statusBox.style.background = "rgba(16, 185, 129, 0.15)";
                statusBox.style.color = "#10B981";
                statusBox.style.border = "1px solid rgba(16, 185, 129, 0.35)";
                statusBox.innerHTML = `🟢 握手成功！延迟: ${json.latency_ms}ms${json.device ? ' (硬件: ' + escapeHtml(json.device) + ')' : ''}`;
            }
            showToast(`✅ 通信正常！${json.device ? '已识别硬件：' + json.device : ''}（延迟: ${json.latency_ms}ms）`, "success");
        } else {
            const err = json.message || "通信握手未成功";
            if (statusBox) {
                statusBox.style.background = "rgba(239, 68, 68, 0.15)";
                statusBox.style.color = "#EF4444";
                statusBox.style.border = "1px solid rgba(239, 68, 68, 0.35)";
                statusBox.innerHTML = `🔴 连通异常: ${escapeHtml(err)}`;
            }
            showToast(`❌ 连通失败: ${err}`, "error");
        }
    } catch (e) {
        if (statusBox) {
            statusBox.style.background = "rgba(239, 68, 68, 0.15)";
            statusBox.style.color = "#EF4444";
            statusBox.style.border = "1px solid rgba(239, 68, 68, 0.35)";
            statusBox.innerHTML = `⚠️ 请求异常: ${escapeHtml(String(e))}`;
        }
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = origHtml;
        }
    }
}

function renderWizardGpuStatusCard() {
    const container = document.getElementById("wizard-gpu-display-container");
    const headerBadge = document.getElementById("wizard-gpu-badge");
    if (!container) return;

    // 查找当前激活的数字人/渲染配置
    const activeRemote = globalAvatarConfigs.find(item => item.is_active && (!item.provider_validation || item.provider_validation.valid)) ||
                         globalAvatarConfigs.find(item => item.is_active);

    // ==========================================
    // 场景 A: 已配置且激活了云端租赁/远端 GPU 算力
    // ==========================================
    if (activeRemote && activeRemote.adapter_id !== "local_procedural") {
        const adapterId = activeRemote.adapter_id || activeRemote.provider_name;
        const descriptor = globalAvatarCatalog.find(item => item.id === adapterId) || {};
        const info = KNOWN_AVATAR_PROVIDER_INFO[adapterId] || {};
        const baseUrl = activeRemote.base_url || "";
        const title = info.title || descriptor.name || activeRemote.title || "云端数字人算力节点";

        if (headerBadge) {
            headerBadge.className = "brand-badge green";
            headerBadge.textContent = "云端 GPU 渲染已就绪";
        }

        container.innerHTML = `
            <div style="background: rgba(15, 23, 42, 0.65); border: 1.5px solid rgba(16, 185, 129, 0.35); border-radius: 8px; padding: 18px; box-shadow: 0 4px 16px rgba(0,0,0,0.2);">
                <!-- 头部状态栏 -->
                <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px dashed rgba(148, 163, 184, 0.2); padding-bottom: 12px; margin-bottom: 14px; flex-wrap: wrap; gap: 8px;">
                    <div style="display: flex; align-items: center; gap: 10px;">
                        <span style="font-size: 14.5px; font-weight: 700; color: #f8fafc; display: flex; align-items: center; gap: 6px;">
                            <span style="display:inline-block; width:9px; height:9px; border-radius:50%; background:#10B981; box-shadow: 0 0 8px #10B981;"></span>
                            ${escapeHtml(title)}
                        </span>
                        <span class="brand-badge sky">云端租赁算力 (GPU)</span>
                    </div>
                    <button type="button" class="btn btn-sm btn-ghost" onclick="goToGpuSettingsTab()" style="font-size: 12px; color: #38bdf8; display: inline-flex; align-items: center; gap: 4px; padding: 4px 8px;" title="前往 GPU配置(2) 面板修改完整参数">
                        前往【GPU配置(2)】修改参数
                        <svg viewBox="0 0 24 24" style="width:13px;height:13px;stroke:currentColor;fill:none;stroke-width:2;"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
                    </button>
                </div>

                <!-- 核心参数指标速览（无需二次输入，直接呈现） -->
                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; margin-bottom: 14px;">
                    <div style="background: rgba(30, 41, 59, 0.6); padding: 10px 14px; border-radius: 6px; border: 1px solid rgba(148, 163, 184, 0.15);">
                        <div style="font-size: 11.5px; color: var(--text-muted); margin-bottom: 4px;">🚀 算力与硬件环境</div>
                        <div style="font-size: 13px; font-weight: 600; color: #e2e8f0;">
                            云端独立显卡加速 (如 NVIDIA A100 80GB) · 本机 0 显存负担
                        </div>
                    </div>

                    <div style="background: rgba(30, 41, 59, 0.6); padding: 10px 14px; border-radius: 6px; border: 1px solid rgba(148, 163, 184, 0.15);">
                        <div style="font-size: 11.5px; color: var(--text-muted); margin-bottom: 4px;">🌐 远端流服务连接地址 (base_url)</div>
                        <div style="font-size: 12.5px; font-family: monospace; color: #38bdf8; word-break: break-all;">
                            ${escapeHtml(baseUrl || "未填写连接地址")}
                        </div>
                    </div>
                </div>

                <!-- 连通性快速质检栏 -->
                <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; background: rgba(15, 23, 42, 0.5); padding: 8px 12px; border-radius: 6px;">
                    <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
                        <span style="font-size: 12px; color: var(--text-muted);">对接健康状态:</span>
                        <div id="wizard-gpu-ping-status" style="display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px; border-radius: 4px; font-size: 12px; background: rgba(16, 185, 129, 0.12); color: #10B981; border: 1px solid rgba(16, 185, 129, 0.25);">
                            🟢 已在 GPU 配置中完成对接与启用
                        </div>
                    </div>
                    ${baseUrl ? `
                    <button id="wizard-gpu-ping-btn" type="button" class="btn btn-sm btn-secondary" onclick="testWizardGpuConnection('${escapeHtml(baseUrl)}', '${escapeHtml(adapterId)}')" style="font-size: 12px; padding: 4px 12px; display: inline-flex; align-items: center; gap: 5px;">
                        <svg viewBox="0 0 24 24" style="width:13px;height:13px;stroke:currentColor;fill:none;stroke-width:2.2;"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
                        快速验证连通性
                    </button>` : ''}
                </div>
            </div>
        `;
        return;
    }

    // ==========================================
    // 场景 B: 正在使用本地轻量卡通方案 (选项 1)
    // ==========================================
    const isLocalProcedural = !activeRemote || (activeRemote && activeRemote.adapter_id === "local_procedural");
    if (isLocalProcedural) {
        if (headerBadge) {
            headerBadge.className = "brand-badge green";
            headerBadge.textContent = "本地卡通就绪";
        }

        container.innerHTML = `
            <div style="background: rgba(15, 23, 42, 0.65); border: 1.5px solid rgba(14, 165, 233, 0.35); border-radius: 8px; padding: 18px;">
                <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px dashed rgba(148, 163, 184, 0.2); padding-bottom: 12px; margin-bottom: 14px; flex-wrap: wrap; gap: 8px;">
                    <div style="display: flex; align-items: center; gap: 10px;">
                        <span style="font-size: 14.5px; font-weight: 700; color: #f8fafc; display: flex; align-items: center; gap: 6px;">
                            <span style="display:inline-block; width:9px; height:9px; border-radius:50%; background:#0ea5e9; box-shadow: 0 0 8px #0ea5e9;"></span>
                            选项 1 · 本地轻量卡通形象
                        </span>
                        <span class="brand-badge green">0元免显卡 · 开箱即用</span>
                    </div>
                    <button type="button" class="btn btn-sm btn-ghost" onclick="goToGpuSettingsTab()" style="font-size: 12px; color: #38bdf8; display: inline-flex; align-items: center; gap: 4px; padding: 4px 8px;">
                        切换为真人/云端GPU ↗
                    </button>
                </div>

                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; margin-bottom: 12px;">
                    <div style="background: rgba(30, 41, 59, 0.6); padding: 10px 14px; border-radius: 6px; border: 1px solid rgba(148, 163, 184, 0.15);">
                        <div style="font-size: 11.5px; color: var(--text-muted); margin-bottom: 4px;">💻 运行环境</div>
                        <div style="font-size: 13px; font-weight: 600; color: #e2e8f0;">
                            本机 CPU 动效渲染引擎（双核即可跑满 25fps，免网络握手）
                        </div>
                    </div>
                    <div style="background: rgba(30, 41, 59, 0.6); padding: 10px 14px; border-radius: 6px; border: 1px solid rgba(148, 163, 184, 0.15);">
                        <div style="font-size: 11.5px; color: var(--text-muted); margin-bottom: 4px;">🛡️ 安全底线机制</div>
                        <div style="font-size: 13px; font-weight: 600; color: #10B981;">
                            开箱即播，并作为断网时的暗中兜底画面（保障直播永不黑屏）
                        </div>
                    </div>
                </div>

                <div style="font-size: 12px; color: #94a3b8; line-height: 1.6;">
                    💡 当前正在使用本地方案。若您需要超写写真人主播或云端 A100 算力，可随时点击右上角前往【GPU配置(2)】完成一键对接。
                </div>
            </div>
        `;
        return;
    }

    // ==========================================
    // 场景 C: 尚未配置任何数字人渲染
    // ==========================================
    if (headerBadge) {
        headerBadge.className = "brand-badge amber";
        headerBadge.textContent = "待配置";
    }

    container.innerHTML = `
        <div style="background: rgba(245, 158, 11, 0.08); border: 1px dashed rgba(245, 158, 11, 0.35); border-radius: 8px; padding: 18px;">
            <div style="font-size: 14px; font-weight: 700; color: #F59E0B; margin-bottom: 6px; display: flex; align-items: center; gap: 8px;">
                <svg viewBox="0 0 24 24" style="width:16px;height:16px;stroke:#F59E0B;fill:none;stroke-width:2;"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
                尚未配置生效的数字人渲染方案
            </div>
            <div style="font-size: 12.5px; color: #cbd5e1; line-height: 1.7; margin-bottom: 14px;">
                开播需要数字人画面输出源。您可以直接一键使用免显卡的本地灵动卡通形象，或前往【GPU配置(2)】对接云端 GPU (如 NVIDIA A100)。
            </div>
            <div style="display: flex; gap: 10px; align-items: center; flex-wrap: wrap;">
                <button type="button" class="btn btn-sm btn-primary" onclick="goToGpuSettingsTab()" style="background: #10B981; border-color: #10B981; font-weight: 600; padding: 6px 16px;">
                    立即前往【GPU配置(2)】配置云端/远端GPU ↗
                </button>
                <button type="button" class="btn btn-sm btn-secondary" onclick="quickEnableLocalProceduralAvatar()" style="font-size: 12px; padding: 6px 14px;">
                    一键启用【本地轻量卡通】快速开播
                </button>
            </div>
        </div>
    `;
}

function renderAvatarProvidersUI(prefix) {
    const cardsBox = document.getElementById(`${prefix}-avatar-provider-cards`);
    if (!cardsBox) return;

    cardsBox.innerHTML = "";
    globalAvatarCatalog.forEach(provider => {
        const info = KNOWN_AVATAR_PROVIDER_INFO[provider.id] || {};
        const isSelected = provider.id === selectedAvatarProviderId;
        const selectable = provider.selectable !== false;

        const card = document.createElement("div");
        card.className = `avatar-compact-card${isSelected ? " selected" : ""}${selectable ? "" : " is-disabled"}`;
        card.setAttribute("data-avatar-provider", provider.id);

        const logoHtml = info.logoSvg ? `<img src="${info.logoSvg}" alt="${escapeHtml(info.title || provider.name)}" class="avatar-compact-logo">` : "";
        const primaryBadge = (info.badges && info.badges[0]) ? info.badges[0] : { text: "可用", cls: "green" };

        card.innerHTML = `
            ${logoHtml}
            <div class="avatar-compact-title">${escapeHtml(info.title || provider.name)}</div>
            <div>
                <span class="avatar-compact-badge brand-badge ${primaryBadge.cls}">${escapeHtml(primaryBadge.text)}</span>
            </div>
        `;

        if (selectable) {
            card.addEventListener("click", () => {
                selectAvatarProvider(provider.id, prefix);
            });
        }
        cardsBox.appendChild(card);
    });

    renderAvatarDetailCard(selectedAvatarProviderId, prefix);
}

function selectAvatarProvider(providerId, prefix) {
    selectedAvatarProviderId = providerId;
    const matchedConfig = globalAvatarConfigs.find(item => item.adapter_id === providerId);
    currentEditingAvatarConfigId = matchedConfig ? matchedConfig.id : "";

    // 同步更新所有卡片的 selected 状态
    document.querySelectorAll(".avatar-compact-card").forEach(card => {
        card.classList.toggle("selected", card.getAttribute("data-avatar-provider") === providerId);
    });

    renderAvatarDetailCard(providerId, "gpu");
    renderAvatarDetailCard(providerId, "wizard");
}

function renderAvatarDetailCard(providerId, prefix) {
    const titleEl = document.getElementById(`${prefix}-detail-title`);
    const badgeEl = document.getElementById(`${prefix}-detail-badge`);
    const linkBox = document.getElementById(`${prefix}-detail-official-link-box`);
    const visualEl = document.getElementById(`${prefix}-detail-visual`);
    const hardwareEl = document.getElementById(`${prefix}-detail-hardware`);
    const audienceEl = document.getElementById(`${prefix}-detail-audience`);
    const plainEl = document.getElementById(`${prefix}-detail-plain-text`);

    const info = KNOWN_AVATAR_PROVIDER_INFO[providerId] || {};
    const descriptor = globalAvatarCatalog.find(p => p.id === providerId) || {};

    if (titleEl) titleEl.textContent = info.title || descriptor.name || "数字人方案";
    if (badgeEl) {
        badgeEl.className = `brand-badge ${descriptor.status === "available" ? "green" : "amber"}`;
        badgeEl.textContent = descriptor.status === "available" ? "可用 · 当前选中" : "联调中";
    }

    if (linkBox) {
        const officialUrl = descriptor.official_url || info.officialUrl || "";
        const officialLabel = info.officialLabel || "官方入口 ↗";
        linkBox.innerHTML = officialUrl ? `
            <a href="${escapeHtml(officialUrl)}" target="_blank" rel="noopener noreferrer" class="avatar-official-link" title="访问官方平台">
                ${escapeHtml(officialLabel)}
            </a>
        ` : "";
    }

    // 动态注入整行独立指标
    if (visualEl) visualEl.textContent = info.visual || descriptor.visual_style || "高质量画面输出";
    if (hardwareEl) hardwareEl.textContent = info.hardware || descriptor.cost_hardware || "标准硬件兼容";
    if (audienceEl) audienceEl.textContent = info.audience || descriptor.target_audience || "全体用户";
    if (plainEl) plainEl.textContent = info.plain || descriptor.plain_explanation || info.summary || "开箱即用数字人方案。";

    // 渲染参数配置字段
    renderAvatarConfigFields(providerId, prefix);
}

function formatApiError(json, fallback = "操作失败") {
    if (!json) return fallback;
    if (typeof json === "string") return json;
    if (json.detail) {
        if (typeof json.detail === "string") return json.detail;
        if (Array.isArray(json.detail)) {
            return json.detail.map(item => {
                if (typeof item === "string") return item;
                if (item && item.msg) {
                    const loc = Array.isArray(item.loc) ? item.loc.filter(x => x !== "body").join(".") : "";
                    return loc ? `${loc}: ${item.msg}` : item.msg;
                }
                return JSON.stringify(item);
            }).join("; ");
        }
        if (typeof json.detail === "object") {
            return json.detail.msg || json.detail.message || JSON.stringify(json.detail);
        }
    }
    if (json.message && typeof json.message === "string") return json.message;
    return fallback;
}

function renderAvatarConfigFields(providerId, prefix) {
    const configPanel = document.getElementById(prefix === "gpu" ? "gpu-avatar-config-fields-panel" : "wizard-avatar-config");
    const fieldsBox = document.getElementById(prefix === "gpu" ? "gpu-avatar-fields-container" : "wizard-avatar-fields");
    const statusHint = document.getElementById(prefix === "gpu" ? "gpu-avatar-status-hint" : "wizard-avatar-status");
    const hiddenId = document.getElementById(prefix === "gpu" ? "gpu-avatar-config-id" : "wizard-avatar-config-id");

    if (!configPanel || !fieldsBox) return;

    const descriptor = globalAvatarCatalog.find(item => item.id === providerId);
    if (!descriptor) return;

    const activeRemote = globalAvatarConfigs.find(item => item.is_active && (!item.provider_validation || item.provider_validation.valid));
    const isLocalActive = !activeRemote;

    // 内置本地卡通形象模式
    if (descriptor.configuration_mode !== "instance") {
        configPanel.style.display = "block";
        fieldsBox.innerHTML = `
            <div style="grid-column: 1 / -1; padding: 14px 16px; background: rgba(16, 185, 129, 0.08); border: 1px dashed rgba(16, 185, 129, 0.3); border-radius: 8px; color: #cbd5e1; font-size: 13px; line-height: 1.7;">
                <div style="font-weight: 600; color: #10B981; margin-bottom: 6px; display: flex; align-items: center; gap: 6px;">
                    <svg class="icon-sm" viewBox="0 0 24 24"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
                    本地免显卡内置方案已就绪
                </div>
                无需配置任何外部 IP、端口或 API 密钥，当前电脑即可直接生成 2D 灵动卡通画面并保底防黑屏。<br>
                状态：${isLocalActive ? '<strong style="color:#10B981;">当前已生效（主推流画面）</strong>' : '<span style="color:var(--text-muted);">当前处于备用保底状态（主画面为远端节点）</span>'}
            </div>
        `;
        if (hiddenId) hiddenId.value = "";
        if (statusHint) {
            statusHint.textContent = isLocalActive ? "当前已生效" : "未生效（备用保底）";
        }
        const saveBtn = document.getElementById("gpu-avatar-save-btn");
        if (saveBtn) {
            saveBtn.innerHTML = `
                <svg class="icon-sm" viewBox="0 0 24 24" style="width: 14px; height: 14px;"><path d="M20 6L9 17l-5-5"/></svg>
                ${isLocalActive ? "刷新并保持本地方案" : "立即启用本地轻量卡通方案"}
            `;
        }
        return;
    }

    // 实例配置模式 (Sidecar, Tencent, Aliyun, LiveAvatar, Custom)
    configPanel.style.display = "block";
    fieldsBox.innerHTML = "";
    const resultBox = document.getElementById("gpu-avatar-conn-result");
    if (resultBox) {
        resultBox.style.display = "none";
        resultBox.innerHTML = "";
    }
    const saveBtn = document.getElementById("gpu-avatar-save-btn");
    if (saveBtn) {
        saveBtn.innerHTML = `
            <svg class="icon-sm" viewBox="0 0 24 24" style="width: 14px; height: 14px;"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
            保存并启用此数字人方案
        `;
    }

    const adapterId = descriptor.adapter_id || descriptor.id;
    const config = globalAvatarConfigs.find(item => item.id === currentEditingAvatarConfigId && item.provider_name === adapterId) ||
                   globalAvatarConfigs.find(item => item.provider_name === adapterId) || null;

    if (hiddenId) hiddenId.value = config ? config.id : "";

    const fields = Array.isArray(descriptor.ui_fields) ? descriptor.ui_fields : [];
    fields.forEach((field, idx) => {
        const inputId = `${prefix}-avatar-field-${idx}`;
        const isSecret = field.type === "secret";
        const wrapper = document.createElement("div");
        wrapper.className = "form-group";

        let val = "";
        if (config) {
            if (field.path === "base_url") {
                val = config.base_url || "";
            } else if (isSecret || field.path === "api_key") {
                if (cachedAvatarDecryptedKeys[config.id]) {
                    val = cachedAvatarDecryptedKeys[config.id];
                } else if (config.masked_key) {
                    val = config.masked_key;
                }
            } else if (field.path.startsWith("extra_params.")) {
                const k = field.path.replace("extra_params.", "");
                try {
                    const extra = typeof config.extra_params === "string" ? JSON.parse(config.extra_params) : (config.extra_params || {});
                    val = extra ? (extra[k] || "") : "";
                } catch (e) {}
            }
        }
        if (!val && field.default) val = field.default;

        // 异步预载入已保存密码真实明文，以便切换明文或保存时使用
        if (isSecret && config && config.id && !cachedAvatarDecryptedKeys[config.id]) {
            fetch(`${API_BASE}/settings/configs/${config.id}/raw-key`)
                .then(r => r.json())
                .then(json => {
                    if (json.code === 0) {
                        const rk = json.raw_key || "";
                        cachedAvatarDecryptedKeys[config.id] = rk;
                        const el = document.getElementById(inputId);
                        if (el) {
                            if (!rk) {
                                el.value = "";
                            } else if (!el.value || el.value.includes("•")) {
                                el.value = rk;
                            }
                        }
                    }
                })
                .catch(e => console.warn("预拉取密钥明文异常:", e));
        }

        let pillsHtml = "";
        if (Array.isArray(field.pills) && field.pills.length) {
            pillsHtml = `<div class="pills-container" style="display:flex;gap:6px;flex-wrap:wrap;margin-top:6px;">` +
                field.pills.map(p => `
                    <button type="button" class="badge-pill" style="font-size:11px;cursor:pointer;padding:2px 8px;border-radius:4px;border:1px solid rgba(148,163,184,0.25);background:rgba(30,41,59,0.7);color:#94a3b8;"
                            onclick="document.getElementById('${inputId}').value='${escapeHtml(p.val)}'">
                        ${escapeHtml(p.text)}
                    </button>
                `).join("") + `</div>`;
        }

        let inputHtml = "";
        if (isSecret) {
            inputHtml = `
                <div style="position: relative; display: flex; align-items: center; width: 100%;">
                    <input id="${inputId}" class="form-control" type="password"
                           value="${escapeHtml(val)}" placeholder="${escapeHtml(field.default || "")}"
                           data-avatar-path="${escapeHtml(field.path)}"
                           data-avatar-required="${field.required ? "true" : "false"}"
                           data-avatar-label="${escapeHtml(field.label || field.path)}"
                           style="padding-right: 40px; font-family: monospace;">
                    <button type="button" class="btn btn-sm btn-ghost avatar-eye-btn" id="${inputId}-eye"
                            style="position: absolute; right: 6px; top: 50%; transform: translateY(-50%); height: 28px; width: 28px; padding: 0; display: inline-flex; align-items: center; justify-content: center; background: transparent; border: none; color: #94a3b8; cursor: pointer; z-index: 2;"
                            title="点击显示明文"
                            onclick="toggleAvatarSecretVisibility('${inputId}', '${config ? config.id : ""}')">
                        <svg viewBox="0 0 24 24" style="width: 15px; height: 15px; stroke: currentColor; fill: none; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round;"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
                    </button>
                </div>
            `;
        } else {
            inputHtml = `
                <input id="${inputId}" class="form-control" type="text"
                       value="${escapeHtml(val)}" placeholder="${escapeHtml(field.default || "")}"
                       data-avatar-path="${escapeHtml(field.path)}"
                       data-avatar-required="${field.required ? "true" : "false"}"
                       data-avatar-label="${escapeHtml(field.label || field.path)}">
            `;
        }

        wrapper.innerHTML = `
            <label class="form-label" for="${inputId}">
                ${escapeHtml(field.label || field.path)}${field.required ? ' <span style="color:var(--red);">*</span>' : ""}
            </label>
            ${inputHtml}
            ${pillsHtml}
            ${field.tip ? `<div class="field-tip" style="margin-top:4px;">${escapeHtml(field.tip)}</div>` : ""}
        `;
        fieldsBox.appendChild(wrapper);
    });

    if (statusHint) {
        statusHint.textContent = config
            ? `正在编辑 ${descriptor.name} 实例 (${config.is_active ? "当前已生效" : "已保存未生效"})`
            : "保存时将为该 Provider 创建新的配置实例并立即生效";
    }
}

async function handleSaveGpuAvatarConfig() {
    const descriptor = globalAvatarCatalog.find(item => item.id === selectedAvatarProviderId);
    if (!descriptor) return;

    if (descriptor.configuration_mode !== "instance") {
        // 本地程序化：将所有已生效远端设为非 active
        try {
            for (const cfg of globalAvatarConfigs.filter(c => c.is_active)) {
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
            showToast("已成功启用【本地轻量卡通】方案！", "success");
            await loadGpuAvatarProviders();
        } catch (e) {
            showToast("切换失败: " + e, "error");
        }
        return;
    }

    const fieldsBox = document.getElementById("gpu-avatar-fields-container");
    const inputs = fieldsBox ? fieldsBox.querySelectorAll("input[data-avatar-path]") : [];
    
    const adapterId = descriptor.adapter_id || descriptor.id;
    const payload = {
        config_group: "neural_renderer",
        provider_name: adapterId,
        title: descriptor.name || descriptor.title || adapterId,
        is_active: true,
        extra_params: {
            adapter: adapterId
        }
    };

    const hiddenId = document.getElementById("gpu-avatar-config-id");
    if (hiddenId && hiddenId.value) {
        payload.id = hiddenId.value;
    }

    // 提取表单输入项与必填校验
    for (const input of inputs) {
        const path = input.getAttribute("data-avatar-path");
        const isRequired = input.getAttribute("data-avatar-required") === "true";
        const label = input.getAttribute("data-avatar-label") || path;
        let val = input.value.trim();

        // 默认回退值
        if (!val && input.placeholder) {
            val = input.placeholder.trim();
        }

        if (isRequired && !val) {
            showToast(`请填写必填项：${label}`, "error");
            input.focus();
            return;
        }

        if (path === "base_url") {
            payload.base_url = val;
        } else if (path === "api_key") {
            if (!val) {
                payload.api_key = "";
                if (payload.id) {
                    delete cachedAvatarDecryptedKeys[payload.id];
                }
            } else if (!val.includes("•")) {
                payload.api_key = val;
            } else if (payload.id && cachedAvatarDecryptedKeys[payload.id]) {
                payload.api_key = cachedAvatarDecryptedKeys[payload.id];
            }
        } else if (path.startsWith("extra_params.")) {
            const k = path.replace("extra_params.", "");
            payload.extra_params[k] = val;
        }
    }

    try {
        const res = await fetch(`${API_BASE}/settings/configs/save`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        const json = await res.json();
        if (res.ok && (json.code === 0 || json.code === undefined)) {
            // 将其他已有 active 实例置为非 active
            const savedId = payload.id || json.id || (json.data && json.data.id);
            if (savedId) {
                currentEditingAvatarConfigId = savedId;
                cachedAvatarDecryptedKeys[savedId] = payload.api_key || "";
            }
            const otherActiveConfigs = globalAvatarConfigs.filter(c => c.is_active && c.id !== savedId);
            for (const cfg of otherActiveConfigs) {
                try {
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
                } catch (_) {}
            }
            showToast(`已成功保存并启用【${descriptor.name}】！正在检测通信连通性...`, "success");
            await loadGpuAvatarProviders();

            // 保存后立即自动进行通信连通性测试并展示结果
            await testCurrentGpuAvatarConnection(true);

        } else {
            showToast("保存失败: " + formatApiError(json, "服务端验证未通过"), "error");
        }
    } catch (e) {
        showToast("保存异常: " + e, "error");
    }
}

async function testCurrentGpuAvatarConnection(isAutoAfterSave = false) {
    console.log("[GPU Avatar] 点击测试通信连接, 当前 selectedAvatarProviderId:", selectedAvatarProviderId);

    const testBtn = document.getElementById("gpu-avatar-test-btn");
    const resultBox = document.getElementById("gpu-avatar-conn-result");
    const fieldsBox = document.getElementById("gpu-avatar-fields-container");

    let descriptor = globalAvatarCatalog.find(item => item.id === selectedAvatarProviderId);
    if (!descriptor) {
        descriptor = {
            id: selectedAvatarProviderId || "custom_avatar",
            adapter_id: selectedAvatarProviderId || "custom_avatar",
            name: "自定义数字人 / 远端流服务",
            configuration_mode: "instance"
        };
    }

    if (descriptor.configuration_mode !== "instance") {
        showToast("本地轻量卡通方案直接运行于本机，免网络握手！", "info");
        if (resultBox) {
            resultBox.style.display = "flex";
            resultBox.style.background = "rgba(16, 185, 129, 0.12)";
            resultBox.style.border = "1px solid rgba(16, 185, 129, 0.35)";
            resultBox.style.color = "#10B981";
            resultBox.innerHTML = `<strong>本地免显卡方案：</strong>直接运行于本机 Python 进程，无需网络通信握手。`;
        }
        return;
    }

    const inputs = fieldsBox ? fieldsBox.querySelectorAll("input[data-avatar-path]") : [];

    let baseUrl = "";
    let apiKey = "";
    for (const input of inputs) {
        const path = input.getAttribute("data-avatar-path");
        const val = input.value.trim() || (input.placeholder ? input.placeholder.trim() : "");
        if (path === "base_url") {
            baseUrl = val;
        } else if (path === "api_key") {
            if (val && !val.includes("•")) {
                apiKey = val;
            } else {
                const hiddenId = document.getElementById("gpu-avatar-config-id");
                if (hiddenId && hiddenId.value && cachedAvatarDecryptedKeys[hiddenId.value]) {
                    apiKey = cachedAvatarDecryptedKeys[hiddenId.value];
                }
            }
        }
    }

    // 容错兜底：若从 data-avatar-path 没拿到，直接查找第一个 input 或历史激活配置
    if (!baseUrl) {
        const firstInput = fieldsBox ? fieldsBox.querySelector("input[type='text'], input[type='url']") : null;
        if (firstInput && firstInput.value.trim()) {
            baseUrl = firstInput.value.trim();
        } else {
            const activeCfg = globalAvatarConfigs.find(c => c.is_active && c.base_url);
            if (activeCfg) {
                baseUrl = activeCfg.base_url;
            }
        }
    }

    if (!baseUrl) {
        showToast("请先填写自定义服务连接地址 (base_url)！", "warning");
        if (resultBox) {
            resultBox.style.display = "flex";
            resultBox.style.background = "rgba(245, 158, 11, 0.12)";
            resultBox.style.border = "1px solid rgba(245, 158, 11, 0.35)";
            resultBox.style.color = "#F59E0B";
            resultBox.innerHTML = `<strong>提示：</strong>请先在上方输入框填写服务连接地址 (base_url)，例如：<code style="font-family:monospace;">ws://127.0.0.1:8010/ws/render-v3</code>`;
        }
        return;
    }

    const originalBtnHtml = testBtn ? testBtn.innerHTML : "";
    if (testBtn) {
        testBtn.disabled = true;
        testBtn.innerHTML = `
            <svg class="icon-sm spin" viewBox="0 0 24 24" style="width: 14px; height: 14px;"><line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"/><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"/><line x1="2" y1="12" x2="6" y2="12"/><line x1="18" y1="12" x2="22" y2="12"/><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"/><line x1="16.24" y1="7.76" x2="19.07" y2="4.93"/></svg>
            正在测试通信...
        `;
    }

    if (resultBox) {
        resultBox.style.display = "flex";
        resultBox.style.background = "rgba(30, 41, 59, 0.7)";
        resultBox.style.border = "1px solid rgba(148, 163, 184, 0.25)";
        resultBox.style.color = "var(--text-primary)";
        resultBox.innerHTML = `
            <span style="display:inline-block; width:8px; height:8px; border-radius:50%; background:#38bdf8; animation:pulse 1s infinite;"></span>
            正在与远端节点发起握手连通测试：<code style="color:#38bdf8; font-family:monospace;">${escapeHtml(baseUrl)}</code> ...
        `;
    }

    try {
        const adapterId = descriptor.adapter_id || descriptor.id;
        const res = await fetch(`${API_BASE}/settings/test-connection`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                config_group: "neural_renderer",
                provider_name: adapterId,
                base_url: baseUrl,
                api_key: apiKey
            })
        });
        const json = await res.json();
        if (json.code === 0 && json.success) {
            const latency = json.latency_ms ?? 0;
            let latencyGrade = "🟢 极速流畅";
            let latencyColor = "#10B981";
            if (latency > 300) {
                latencyGrade = "🟠 延迟较高";
                latencyColor = "#F59E0B";
            } else if (latency > 150) {
                latencyGrade = "🟡 良好稳定";
                latencyColor = "#FBBF24";
            }

            const device = json.device || "NVIDIA GPU 算力就绪";
            if (resultBox) {
                resultBox.style.display = "block";
                resultBox.style.background = "rgba(16, 185, 129, 0.08)";
                resultBox.style.border = "1.5px solid rgba(16, 185, 129, 0.35)";
                resultBox.style.padding = "14px";
                resultBox.style.borderRadius = "8px";
                resultBox.innerHTML = `
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; border-bottom:1px solid rgba(16,185,129,0.2); padding-bottom:6px;">
                        <div style="font-weight:700; color:#10B981; display:flex; align-items:center; gap:6px;">
                            <svg viewBox="0 0 24 24" style="width:16px;height:16px;stroke:#10B981;fill:none;stroke-width:2.5;"><polyline points="20 6 9 17 4 12"/></svg>
                            云端渲染节点通信对接成功
                        </div>
                        <span class="brand-badge green">全双工在线</span>
                    </div>
                    <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:10px; margin-top:8px;">
                        <div style="background:rgba(15,23,42,0.6); padding:8px 12px; border-radius:6px; border:1px solid rgba(148,163,184,0.15);">
                            <div style="font-size:11px; color:var(--text-muted);">🖥️ 远端 GPU 设备</div>
                            <div style="font-size:13px; font-weight:600; color:#f8fafc; margin-top:2px;">${escapeHtml(device)}</div>
                        </div>
                        <div style="background:rgba(15,23,42,0.6); padding:8px 12px; border-radius:6px; border:1px solid rgba(148,163,184,0.15);">
                            <div style="font-size:11px; color:var(--text-muted);">⚡ 端云往返时延 (Ping)</div>
                            <div style="font-size:13px; font-weight:700; color:${latencyColor}; margin-top:2px;">
                                ${latency}ms · ${latencyGrade}
                            </div>
                        </div>
                        <div style="background:rgba(15,23,42,0.6); padding:8px 12px; border-radius:6px; border:1px solid rgba(148,163,184,0.15);">
                            <div style="font-size:11px; color:var(--text-muted);">🛡️ 防黑屏保活状态</div>
                            <div style="font-size:12px; font-weight:500; color:#10B981; margin-top:2px;">已就绪 · 本地微动态兜底</div>
                        </div>
                    </div>
                `;
            }
            showToast(`✅ 通信对接成功！${device}（延迟: ${latency}ms）`, "success");
        } else {
            const errMsg = json.message || "通信测试失败，请检查地址或网络端口";
            if (resultBox) {
                resultBox.style.display = "block";
                resultBox.style.background = "rgba(239, 68, 68, 0.08)";
                resultBox.style.border = "1.5px solid rgba(239, 68, 68, 0.35)";
                resultBox.style.padding = "12px 14px";
                resultBox.style.borderRadius = "8px";
                resultBox.innerHTML = `
                    <div style="font-weight:700; color:#EF4444; display:flex; align-items:center; gap:6px; margin-bottom:6px;">
                        <svg viewBox="0 0 24 24" style="width:16px;height:16px;stroke:#EF4444;fill:none;stroke-width:2.5;"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
                        通信对接未通达
                    </div>
                    <div style="font-size:12.5px; color:#cbd5e1; line-height:1.6;">${escapeHtml(errMsg)}</div>
                    <div style="margin-top:8px; font-size:11.5px; color:#94a3b8; border-top:1px dashed rgba(148,163,184,0.2); padding-top:6px;">
                        💡 <strong>排查建议：</strong>若使用云端 GPU，请确认云端终端已运行 <code>scripts/cloud_sidecar_bootstrap.py</code>，并且 Cloudflare 隧道已成功分配公网域名。
                    </div>
                `;
            }
            showToast(isAutoAfterSave ? `⚠️ 配置已保存，但通信握手未成功: ${errMsg}` : `❌ 连通失败: ${errMsg}`, "error");
        }
    } catch (e) {
        if (resultBox) {
            resultBox.style.display = "block";
            resultBox.style.background = "rgba(239, 68, 68, 0.08)";
            resultBox.style.border = "1.5px solid rgba(239, 68, 68, 0.35)";
            resultBox.style.padding = "12px 14px";
            resultBox.style.borderRadius = "8px";
            resultBox.innerHTML = `
                <div style="font-weight:700; color:#EF4444; margin-bottom:4px;">请求异常</div>
                <div style="font-size:12px; color:#cbd5e1;">${escapeHtml(String(e))}</div>
            `;
        }
        showToast("连通测试异常: " + e, "error");
    } finally {
        if (testBtn) {
            testBtn.disabled = false;
            testBtn.innerHTML = originalBtnHtml;
        }
    }
}

// 复制云端 A100 一键启动命令
function copyCloudBootstrapCommand() {
    const cmd = "curl -sSL https://ghproxy.net/https://raw.githubusercontent.com/winclubs/AI-LiveStream-Agent/main/scripts/cloud_sidecar_bootstrap.py -o sidecar.py && python3 sidecar.py";
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(cmd).then(() => {
            showToast("📋 已复制云端一键启动命令！直接在云端开发机终端粘贴回车即可。", "success");
        }).catch(() => {
            prompt("请复制以下命令在云主机终端中执行：", cmd);
        });
    } else {
        prompt("请复制以下命令在云主机终端中执行：", cmd);
    }
}

// 显式挂载到 window 全局，确保 HTML 内联 onclick 能够直接调用
window.copyCloudBootstrapCommand = copyCloudBootstrapCommand;
window.testCurrentGpuAvatarConnection = testCurrentGpuAvatarConnection;
window.handleSaveGpuAvatarConfig = handleSaveGpuAvatarConfig;
window.loadGpuAvatarProviders = loadGpuAvatarProviders;
window.loadWizardAvatarProviders = loadWizardAvatarProviders;
window.selectAvatarProvider = selectAvatarProvider;
window.toggleAvatarSecretVisibility = toggleAvatarSecretVisibility;
window.goToGpuSettingsTab = goToGpuSettingsTab;
window.quickEnableLocalProceduralAvatar = quickEnableLocalProceduralAvatar;
window.testWizardGpuConnection = testWizardGpuConnection;


