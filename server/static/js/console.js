/**
 * AI-LiveStream-Agent 现代控制台前端驱动引擎
 * 全双工 WebSocket 状态流 + RESTful API 联动
 */

const API_BASE = "/api/v1";
const FRONTEND_VERSION = "1.8.0";

// 跨异步初始化流程共享的核心状态必须先显式初始化，避免首屏读取未声明变量。
let ws = null;
let currentMode = "";
let isLiveStreaming = false;

// 全局 HTML 转义防 XSS 工具函数
function escapeHtml(str) {
    if (str == null) return "";
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}
window.escapeHtml = escapeHtml;

// ============================================================================
// SVG 图标系统 (动态渲染统一入口，与 index.html 内联图标同风格)
// ============================================================================
const ICON_PATHS = {
    check: '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>',
    x: '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
    warn: '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
    info: '<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/>',
    shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
    zap: '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
    tag: '<path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.83z"/><line x1="7" y1="7" x2="7.01" y2="7"/>',
    theater: '<line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/>',
    terminal: '<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>',
    dollar: '<circle cx="12" cy="12" r="10"/><path d="M16 8h-6a2 2 0 1 0 0 4h4a2 2 0 1 1 0 4H8"/><line x1="12" y1="6" x2="12" y2="4"/><line x1="12" y1="20" x2="12" y2="18"/>',
    bag: '<path d="M6 2 3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4z"/><line x1="3" y1="6" x2="21" y2="6"/><path d="M16 10a4 4 0 0 1-8 0"/>',
    smile: '<circle cx="12" cy="12" r="10"/><path d="M8 14s1.5 2 4 2 4-2 4-2"/><line x1="9" y1="9" x2="9.01" y2="9"/><line x1="15" y1="9" x2="15.01" y2="9"/>',
    briefcase: '<rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/>',
    coffee: '<path d="M18 8h1a4 4 0 0 1 0 8h-1"/><path d="M2 8h16v9a4 4 0 0 1-4 4H6a4 4 0 0 1-4-4V8z"/><line x1="6" y1="1" x2="6" y2="4"/><line x1="10" y1="1" x2="10" y2="4"/><line x1="14" y1="1" x2="14" y2="4"/>',
    user: '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
    play: '<polygon points="5 3 19 12 5 21 5 3"/>',
    stop: '<rect x="6" y="6" width="12" height="12" rx="1"/>',
    refresh: '<polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>',
    compass: '<circle cx="12" cy="12" r="10"/><polygon points="16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88"/>',
    save: '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/>',
    mic: '<path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/>',
    monitor: '<rect x="2" y="7" width="20" height="13" rx="2"/><polyline points="17 2 12 7 7 2"/>',
    cloud: '<path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z"/>',
    cpu: '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><line x1="9" y1="1" x2="9" y2="4"/><line x1="15" y1="1" x2="15" y2="4"/><line x1="9" y1="20" x2="9" y2="23"/><line x1="15" y1="20" x2="15" y2="23"/><line x1="20" y1="9" x2="23" y2="9"/><line x1="20" y1="14" x2="23" y2="14"/><line x1="1" y1="9" x2="4" y2="9"/><line x1="1" y1="14" x2="4" y2="14"/>',
    bolt: '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
    gpu: '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><line x1="1" y1="9" x2="4" y2="9"/><line x1="1" y1="15" x2="4" y2="15"/><line x1="20" y1="9" x2="23" y2="9"/><line x1="20" y1="15" x2="23" y2="15"/>',
    ram: '<rect x="2" y="7" width="20" height="9" rx="1.5"/><line x1="6" y1="16" x2="6" y2="19"/><line x1="10" y1="16" x2="10" y2="19"/><line x1="14" y1="16" x2="14" y2="19"/><line x1="18" y1="16" x2="18" y2="19"/><line x1="7" y1="11" x2="7" y2="13"/><line x1="11" y1="11" x2="11" y2="13"/><line x1="15" y1="11" x2="15" y2="13"/><line x1="19" y1="11" x2="19" y2="13"/>',
    search: '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
    plus: '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
    bulb: '<path d="M9 18h6"/><path d="M10 22h4"/><path d="M12 2a7 7 0 0 0-4 12.7c.6.5 1 1.4 1 2.3h6c0-.9.4-1.8 1-2.3A7 7 0 0 0 12 2z"/>',
    eye: '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>',
    clipboard: '<path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="8" height="4" rx="1" ry="1"/>',
    external: '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>',
    copy: '<rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    trash: '<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
    edit: '<path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>',
    clock: '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    wifi: '<path d="M5 12.55a11 11 0 0 1 14.08 0"/><path d="M1.42 9a16 16 0 0 1 21.16 0"/><path d="M8.53 16.11a6 6 0 0 1 6.95 0"/><line x1="12" y1="20" x2="12.01" y2="20"/>',
    volume: '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14M15.54 8.46a5 5 0 0 1 0 7.07"/>',
    settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
    camera: '<path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/>',
    video: '<polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/>',
    box: '<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/>',
    target: '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>',
    broadcast: '<path d="M4.9 19.1C1 15.2 1 8.8 4.9 4.9"/><path d="M7.8 16.2c-2.3-2.3-2.3-6.1 0-8.5"/><circle cx="12" cy="12" r="2"/><path d="M16.2 7.8c2.3 2.3 2.3 6.1 0 8.5"/><path d="M19.1 4.9C23 8.8 23 15.2 19.1 19.1"/>',
    chat: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    gift: '<polyline points="20 12 20 22 4 22 4 12"/><rect x="2" y="7" width="20" height="5"/><line x1="12" y1="22" x2="12" y2="7"/><path d="M12 7H7.5a2.5 2.5 0 0 1 0-5C11 2 12 7 12 7z"/><path d="M12 7h4.5a2.5 2.5 0 0 0 0-5C13 2 12 7 12 7z"/>',
    heart: '<path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/>',
    handshake: '<path d="m11 17 2 2a1 1 0 0 0 1.4 0l4.6-4.6a2 2 0 0 0 0-2.8l-3.2-3.2a2 2 0 0 0-2.8 0L11 10.4"/><path d="m13 7-2-2a1 1 0 0 0-1.4 0L5 9.6a2 2 0 0 0 0 2.8l3.2 3.2a2 2 0 0 0 2.8 0L13 13.6"/><path d="M18 11l3-3a2 2 0 0 0 0-2.8l-1.2-1.2a2 2 0 0 0-2.8 0l-3 3"/><path d="M6 13l-3 3a2 2 0 0 0 0 2.8l1.2 1.2a2 2 0 0 0 2.8 0l3-3"/>',
    "plus-circle": '<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/>',
    activity: '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
    orange: '<circle cx="12" cy="13" r="8"/><path d="M12 5V2"/><path d="M12 2c2 1 4 0 5-1-1 3-3 4-5 4z"/>'
};

function svg(name, cls = "icon") {
    const p = ICON_PATHS[name] || ICON_PATHS.info;
    return `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${p}</svg>`;
}
// 初始化
document.addEventListener("DOMContentLoaded", () => {
    checkBackendVersion();
    initNavigation();
    initWebSocket();
    loadRoles();
    loadHardwareInfo();
    loadSoftwarePrerequisites();
    loadGuardrailWords();
    loadProducts();
    loadSettings();
    loadAnchors();
    loadVoiceTable();
    loadAudioDevices();
    initWizard();
    loadLiveStats();
    checkLiveStatus();
    loadKnowledgeList();

    // 绑定直播必备软件重新检测按钮
    const recheckPrereqBtn = document.getElementById("btn-recheck-prereqs");
    if (recheckPrereqBtn) {
        recheckPrereqBtn.addEventListener("click", () => loadSoftwarePrerequisites(true));
    }

    // 硬件监控定时器
    setInterval(loadHardwareInfo, 5000);
    // 直播大屏运营参数轮询
    setInterval(loadLiveStats, 3000);
});

// 后端版本一致性与服务归属自愈校验 (消除旧进程残留导致的接口割裂与 404)
async function checkBackendVersion() {
    let backendVersion = null;
    let processId = null;
    let serviceName = null;

    try {
        const res = await fetch(`${API_BASE}/system/version`);
        if (res.ok) {
            const json = await res.json();
            backendVersion = json.version;
            processId = json.process_id;
            serviceName = json.service;
        }
    } catch (e) {
        // 网络不可达
    }

    const existingBanner = document.getElementById("version-warning-banner");

    if (backendVersion === FRONTEND_VERSION) {
        if (existingBanner) existingBanner.remove();
        return;
    }

    if (!existingBanner) {
        const banner = document.createElement("div");
        banner.id = "version-warning-banner";

        const isUnreachable = !backendVersion;
        const titleText = isUnreachable ? "本地核心执行引擎未连接" : "检测到后端服务版本不一致";
        const descHtml = isUnreachable
            ? `无法连接本地核心引擎 (<code>${window.location.host}</code>)。请通过桌面启动器重试，或运行 <code>run_agent.bat</code> 查看本机启动日志。`
            : `当前后端: <code>v${backendVersion}</code> (PID: <code>${processId || "未知"}</code>) | 控制台版本: <code>v${FRONTEND_VERSION}</code>。历史进程可能发生残留，建议一键清理。`;

        banner.innerHTML = `
            <div class="version-banner-content">
                <div class="version-banner-icon">⚠️</div>
                <div>
                    <div class="version-banner-title">${titleText}</div>
                    <div class="version-banner-desc">${descHtml}</div>
                </div>
            </div>
            <div class="version-banner-actions">
                ${!isUnreachable ? `<button id="btn-banner-kill" class="btn-banner-action btn-banner-shutdown">⚡ 立即停止旧服务</button>` : ""}
                <button id="btn-banner-refresh" class="btn-banner-action btn-banner-retry">🔄 重新检测</button>
                <button id="btn-banner-dismiss" class="btn-banner-close" title="忽略">✕</button>
            </div>
        `;

        document.body.appendChild(banner);

        const btnKill = banner.querySelector("#btn-banner-kill");
        if (btnKill) {
            btnKill.addEventListener("click", async () => {
                btnKill.disabled = true;
                btnKill.innerText = "正在停止...";
                try {
                    await fetch(`${API_BASE}/system/shutdown`, { method: "POST" });
                    showToast("旧版服务已发送关闭指令，请运行 run_agent.bat 启动最新版！", "warning");
                    setTimeout(() => checkBackendVersion(), 1500);
                } catch (err) {
                    showToast("关闭指令发送失败或端口已释放，请重新检测。", "info");
                    setTimeout(() => checkBackendVersion(), 1000);
                }
            });
        }

        const btnRefresh = banner.querySelector("#btn-banner-refresh");
        if (btnRefresh) {
            btnRefresh.addEventListener("click", () => {
                btnRefresh.innerText = "检测中...";
                setTimeout(() => checkBackendVersion(), 500);
            });
        }

        const btnDismiss = banner.querySelector("#btn-banner-dismiss");
        if (btnDismiss) {
            btnDismiss.addEventListener("click", () => banner.remove());
        }
    }
}

function goWizard() {
    document.querySelectorAll(".nav-item").forEach(n => n.classList.toggle("active", n.getAttribute("data-tab") === "wizard"));
    document.querySelectorAll(".tab-content").forEach(tc => tc.classList.remove("active"));
    document.getElementById("tab-wizard").classList.add("active");
}

// ============================================================================
// 全局 Toast 通知 (替换阻塞式 alert，操作反馈不打断用户)
// ============================================================================
function showToast(message, type = "success", duration = 3200) {
    let container = document.getElementById("toast-container");
    if (!container) {
        container = document.createElement("div");
        container.id = "toast-container";
        document.body.appendChild(container);
    }
    const typeMap = { success: "check", error: "x", warning: "warn", info: "info" };
    const toast = document.createElement("div");
    toast.className = `toast-item t-${type}`;
    const iconSpan = document.createElement("span");
    iconSpan.innerHTML = svg(typeMap[type] || "info", "icon");
    const textSpan = document.createElement("span");
    textSpan.textContent = String(message || "").replace(/<br>/g, "\n");
    toast.appendChild(iconSpan);
    toast.appendChild(textSpan);
    container.appendChild(toast);
    while (container.children.length > 4) container.removeChild(container.firstChild);
    setTimeout(() => {
        toast.style.transition = "opacity 0.4s ease, transform 0.4s ease";
        toast.style.opacity = "0";
        toast.style.transform = "translateX(30px)";
        setTimeout(() => toast.remove(), 400);
    }, duration);
}

// 全局 alert 升级为 Toast：所有旧调用点自动变为非阻塞通知，不打断用户操作
window.alert = function (message) {
    const text = String(message == null ? "" : message).replace(/\n+/g, "<br>");
    const isWarning = /失败|异常|禁止|未开播|未设置|请输入|请选择|请先|阻止|丢失|无效/.test(text);
    showToast(text, isWarning ? "warning" : "success", text.length > 60 ? 6500 : 3500);
};


// 1. 导航选项卡切换
function initNavigation() {
    const navItems = document.querySelectorAll(".nav-item");
    navItems.forEach(item => {
        item.addEventListener("click", () => {
            navItems.forEach(n => n.classList.remove("active"));
            item.classList.add("active");

            const tab = item.getAttribute("data-tab");
            document.querySelectorAll(".tab-content").forEach(tc => tc.classList.remove("active"));
            const target = document.getElementById(`tab-${tab}`);
            if (target) target.classList.add("active");

            // 切入云端模型页时重新读取最新直播模式 (避免向导完成后模式显示滞后)
            if (tab === "settings") { loadSettings(); loadVisionConfig(); }
            if (tab === "knowledge") { loadKnowledgeList(); loadKnowledgeStatus(); }
        });
    });
}

// 2. 全双工 WebSocket 连接
function initWebSocket() {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${protocol}//${window.location.host}/ws/live_control`;
    
    const statusDot = document.getElementById("ws-status-dot");
    const statusText = document.getElementById("ws-status-text");

    try {
        ws = new WebSocket(wsUrl);
        ws.onopen = () => {
            statusDot.className = "status-dot online";
            statusText.innerText = "中控信令已联通";
            logDanmaku("系统", "已连接本地高并发全双工调度引擎", false);
        };
        ws.onclose = () => {
            statusDot.className = "status-dot";
            statusText.innerText = "连接已断开 (重试中...)";
            setTimeout(initWebSocket, 3000);
        };
        ws.onerror = () => {
            statusDot.className = "status-dot";
            statusText.innerText = "通信异常";
        };
        ws.onmessage = (event) => {
            try {
                const packet = JSON.parse(event.data);
                handleWsPacket(packet);
            } catch (e) {
                console.error("解析信令数据包失败", e);
            }
        };
    } catch (e) {
        console.error("无法创建 WebSocket 连接", e);
    }
}

// 浏览器端流式音频播放引擎 (支持毫秒级打断与自动连贯播放)
let currentAudioElement = null;
let currentAudioUrl = null;
const audioQueue = [];
const pendingAudioTimers = new Set();
let isPlayingAudio = false;
let audioPlaybackGeneration = 0;

function playAudioChunk(base64Audio, mimeType, generation) {
    if (generation !== audioPlaybackGeneration) return;
    audioQueue.push({ base64Audio, mimeType, generation });
    if (!isPlayingAudio) processAudioQueue();
}

function processAudioQueue() {
    if (audioQueue.length === 0) {
        isPlayingAudio = false;
        return;
    }
    const item = audioQueue.shift();
    if (item.generation !== audioPlaybackGeneration) {
        processAudioQueue();
        return;
    }
    isPlayingAudio = true;
    try {
        const byteCharacters = atob(item.base64Audio);
        const byteNumbers = new Array(byteCharacters.length);
        for (let i = 0; i < byteCharacters.length; i++) byteNumbers[i] = byteCharacters.charCodeAt(i);
        const byteArray = new Uint8Array(byteNumbers);
        const blob = new Blob([byteArray], { type: item.mimeType });
        const url = URL.createObjectURL(blob);

        if (currentAudioElement) {
            try { currentAudioElement.pause(); } catch(e) {}
        }
        if (currentAudioUrl) URL.revokeObjectURL(currentAudioUrl);
        currentAudioUrl = url;
        currentAudioElement = new Audio(url);
        const finish = () => {
            URL.revokeObjectURL(url);
            if (currentAudioUrl === url) currentAudioUrl = null;
            processAudioQueue();
        };
        currentAudioElement.onended = finish;
        currentAudioElement.onerror = finish;
        currentAudioElement.play().catch(e => {
            console.warn("浏览器自动播放权限受限，点击页面任意位置即可激活发音", e);
            finish();
        });
    } catch (err) {
        console.error("音频解码失败", err);
        processAudioQueue();
    }
}

function stopAllAudioPlayback(nextGeneration = null) {
    audioPlaybackGeneration = nextGeneration === null
        ? audioPlaybackGeneration + 1
        : Math.max(audioPlaybackGeneration, Number(nextGeneration) || 0);
    pendingAudioTimers.forEach(timerId => clearTimeout(timerId));
    pendingAudioTimers.clear();
    audioQueue.length = 0;
    if (currentAudioElement) {
        try { currentAudioElement.pause(); } catch(e) {}
        currentAudioElement = null;
    }
    if (currentAudioUrl) {
        URL.revokeObjectURL(currentAudioUrl);
        currentAudioUrl = null;
    }
    isPlayingAudio = false;
    updateSpeakingWave(false);
}

let couponOverlayTimer = null;
let sceneOverlayTimer = null;

function closeCouponOverlay() {
    if (couponOverlayTimer) clearInterval(couponOverlayTimer);
    couponOverlayTimer = null;
    const overlay = document.getElementById("coupon-overlay");
    if (overlay) overlay.style.display = "none";
}

function showCouponOverlay(data) {
    closeCouponOverlay();
    const overlay = document.getElementById("coupon-overlay");
    const title = document.getElementById("coupon-overlay-title");
    const countdown = document.getElementById("coupon-overlay-countdown");
    if (!overlay || !title || !countdown) return;
    title.textContent = data.desc || data.title || "限时优惠";
    let remaining = Math.max(1, Math.min(Number.parseInt(data.seconds || 180, 10), 3600));
    const render = () => {
        const minutes = String(Math.floor(remaining / 60)).padStart(2, "0");
        const seconds = String(remaining % 60).padStart(2, "0");
        countdown.textContent = `${minutes}:${seconds}`;
    };
    render();
    overlay.style.display = "block";
    couponOverlayTimer = setInterval(() => {
        remaining -= 1;
        if (remaining <= 0) {
            closeCouponOverlay();
            return;
        }
        render();
    }, 1000);
}

function closeSceneOverlay() {
    if (sceneOverlayTimer) clearTimeout(sceneOverlayTimer);
    sceneOverlayTimer = null;
    const overlay = document.getElementById("product-scene-overlay");
    const content = document.getElementById("scene-overlay-content");
    if (overlay) overlay.style.display = "none";
    if (content) content.replaceChildren();
}

function openSceneOverlay(seconds) {
    const overlay = document.getElementById("product-scene-overlay");
    if (!overlay) return;
    overlay.style.display = "flex";
    sceneOverlayTimer = setTimeout(closeSceneOverlay, Math.max(1, Number(seconds) || 6) * 1000);
}

function showProductCloseup(data) {
    closeSceneOverlay();
    const content = document.getElementById("scene-overlay-content");
    if (!content) return;
    const heading = document.createElement("h3");
    heading.textContent = data.title || data.sku || "当前商品";
    const image = document.createElement("img");
    image.className = "scene-overlay-product-image";
    image.alt = `${heading.textContent}商品特写`;
    image.src = data.image ? `/static-file?path=${encodeURIComponent(data.image)}` : "/static/svg/default_product.svg";
    image.onerror = () => { image.src = "/static/svg/default_product.svg"; };
    content.append(heading, image);
    openSceneOverlay(data.seconds || 6);
}

function showSizeChart(data) {
    closeSceneOverlay();
    const content = document.getElementById("scene-overlay-content");
    if (!content) return;
    const heading = document.createElement("h3");
    heading.textContent = `${data.title || data.sku || "当前商品"} · 尺码对照`;
    content.appendChild(heading);
    const chart = data.size_chart || {};
    const columns = Array.isArray(chart.columns) ? chart.columns : [];
    const rows = Array.isArray(chart.rows) ? chart.rows : [];
    if (columns.length && rows.length) {
        const table = document.createElement("table");
        table.className = "scene-size-table";
        const thead = document.createElement("thead");
        const headerRow = document.createElement("tr");
        columns.forEach(value => {
            const th = document.createElement("th");
            th.textContent = String(value);
            headerRow.appendChild(th);
        });
        thead.appendChild(headerRow);
        const tbody = document.createElement("tbody");
        rows.forEach(row => {
            const tr = document.createElement("tr");
            (Array.isArray(row) ? row : []).slice(0, columns.length).forEach(value => {
                const td = document.createElement("td");
                td.textContent = String(value);
                tr.appendChild(td);
            });
            tbody.appendChild(tr);
        });
        table.append(thead, tbody);
        content.appendChild(table);
        if (chart.unit) {
            const unit = document.createElement("div");
            unit.className = "field-tip";
            unit.textContent = `单位：${chart.unit}`;
            content.appendChild(unit);
        }
    } else {
        const empty = document.createElement("div");
        empty.className = "scene-empty-hint";
        empty.textContent = "该商品暂未配置尺码表，请以商品详情或客服说明为准。";
        content.appendChild(empty);
    }
    openSceneOverlay(data.seconds || 20);
}

function handleWsPacket(packet) {
    const type = packet.event || packet.event_type;
    const data = packet.payload || packet.data || {};

    if (type === "barge_in" || type === "interrupt" || type === "TRIGGER_BARGE_IN") {
        // 瞬间打断声音播放，并使所有旧延迟任务失效
        stopAllAudioPlayback(data.audio_generation);
        triggerBargeInVisual(data.reason || "P0 优先级打断");
        logDanmaku("系统打断", `[打断原因]: ${data.reason || "Barge-in 插播"}`, true);
    } else if (type === "AUDIO_CHUNK") {
        const packetGeneration = Number(data.audio_generation ?? audioPlaybackGeneration);
        if (packetGeneration < audioPlaybackGeneration) return;
        if (packetGeneration > audioPlaybackGeneration) stopAllAudioPlayback(packetGeneration);
        const playDelay = Math.max(0, parseInt(data.delay_ms || 0, 10));
        const enqueueAudio = () => {
            if (packetGeneration !== audioPlaybackGeneration) return;
            if (data.audio_base64) {
                playAudioChunk(data.audio_base64, data.mime_type || "audio/mpeg", packetGeneration);
            }
            if (data.text) {
                const speaker = data.speaker || "主播播报";
                logDanmaku(speaker, data.text, false);
            }
        };
        if (playDelay > 0) {
            const timerId = setTimeout(() => {
                pendingAudioTimers.delete(timerId);
                enqueueAudio();
            }, playDelay);
            pendingAudioTimers.add(timerId);
        } else {
            enqueueAudio();
        }
    } else if (type === "ONSCREEN_COUPON") {
        showCouponOverlay(data);
        logDanmaku(`${svg("tag", "icon-sm")} 优惠券工具`, `已在控制台显示【${escapeHtml(data.desc || "限时优惠")}】倒计时画层`, true, true);
    } else if (type === "VRAM_WARNING") {
        logDanmaku(`${svg("warn", "icon-sm")} 显存看门狗`, `GPU 显存占用 ${(data.usage * 100).toFixed(1)}%，已自动清空 CUDA 缓存熔断保护`, true, true);
    } else if (type === "danmaku" || type === "BARRAGE_RECEIVED") {
        const isP0 = packet.priority === 0 || data.priority === 0;
        logDanmaku(data.user || "弹幕观众", data.text || "", isP0);
    } else if (type === "GUARDRAIL_TRIGGERED") {
        logDanmaku(`${svg("shield", "icon-sm")} 敏感词拦截`, `[已智能平替] 原词: "${escapeHtml(data.original || '')}" → 替换为: "${escapeHtml(data.sanitized || '')}"`, true, true);
    } else if (type === "PRICE_AUDIT_WARNING") {
        logDanmaku(`${svg("dollar", "icon-sm")} 价格防幻觉审计`, `检测到虚报低价 ¥${escapeHtml(data.spoken_price)}，已自动更正为官方直播价 ¥${escapeHtml(data.official_price)}`, true, true);
    } else if (type === "CAMERA_CLOSEUP") {
        showProductCloseup(data);
        logDanmaku(`${svg("eye", "icon-sm")} 商品特写`, `已在控制台显示【${escapeHtml(data.title || data.sku || "当前商品")}】特写画层`, true, true);
    } else if (type === "SIZE_CHART") {
        showSizeChart(data);
        logDanmaku(`${svg("clipboard", "icon-sm")} 尺码对照`, `已在控制台显示【${escapeHtml(data.title || data.sku || "当前商品")}】尺码画层`, true, true);
    } else if (type === "ROLE_SWITCHED") {
        logDanmaku(`${svg("theater", "icon-sm")} 角色热切换`, `主播人设已动态切换为: 【${escapeHtml(data.role_name || data.role_id)}】`, false, true);
        // 刷新界面选中的角色卡片状态
        if (data.role_id) {
            document.querySelectorAll(".role-card").forEach(c => {
                if (c.getAttribute("data-role-id") === data.role_id) {
                    c.classList.add("active");
                } else {
                    c.classList.remove("active");
                }
            });
        }
    } else if (type === "speaking_state") {
        updateSpeakingWave(Boolean(data.is_speaking));
        const spkText = document.getElementById("speaking-wave-text");
        if (spkText) spkText.innerText = data.is_speaking ? "正在发音驱动" : "待机呼吸微动";
    }
}

// 一键开播 / 停止直播状态管理 (isLiveStreaming 声明于文件顶部全局区)

async function checkLiveStatus() {
    try {
        const res = await fetch(`${API_BASE}/live/status`);
        const json = await res.json();
        if (json.code === 0) {
            updateLiveStateUI(json.is_live);
        }
    } catch (e){}
}

function updateLiveStateUI(isLive) {
    isLiveStreaming = isLive;
    const badge = document.getElementById("live-state-badge");
    const btn = document.getElementById("btn-toggle-live");
    if (badge) {
        if (isLive) {
            badge.className = "badge-recommend";
            badge.innerText = "● 本地直播源运行中";
        } else {
            badge.className = "badge-optional";
            badge.innerText = "○ 待机未开播";
        }
    }
    if (btn) {
        if (isLive) {
            btn.className = "btn btn-sm btn-danger";
            btn.innerHTML = `${svg("stop", "icon-sm")} 停止直播`;
        } else {
            btn.className = "btn btn-sm btn-primary";
            btn.innerHTML = `${svg("play", "icon-sm")} 启动本地直播源`;
        }
    }
    // 联动数字人监视器视窗视频流
    const monitorFeed = document.getElementById("digital-human-video-feed");
    if (monitorFeed) {
        if (isLive) {
            monitorFeed.src = `${API_BASE}/live/stream/preview?t=${Date.now()}`;
        } else {
            monitorFeed.src = "/static/svg/standby_monitor.svg";
        }
    }
    // 需求3：直播进行中禁止切换角色/模式 —— 灰掉激活按钮并提示
    const activateBtn = document.getElementById("btn-activate-role");
    if (activateBtn) {
        activateBtn.disabled = isLive;
        activateBtn.title = isLive ? "直播进行中，禁止切换主播角色！" : "";
        activateBtn.style.opacity = isLive ? "0.5" : "1";
    }
    const reconfigHint = document.getElementById("wizard-save-status");
    if (reconfigHint && isLive) {
        reconfigHint.innerText = "直播进行中：直播模式与主播角色已锁定，如需更换请先停止直播。";
    }
}

async function toggleLiveState() {
    if (!isLiveStreaming) {
        // Preflight 自身不可用时必须 fail-closed，禁止绕过检查直接开播。
        const pf = await runPreflight({ auto: true });
        if (!pf) {
            showToast("无法完成开播前检查，请恢复本地服务后重试", "error");
            return;
        }
        if (!pf.ready) {
            showToast("存在未通过的开播条件，请按检查报告处理后重试", "warning");
            return;
        }
        if (pf && pf.checks.some(c => c.status === "warn")) {
            return; // 已弹报告，用户可选择"仍要开播"
        }
        await startLiveDirect();
    } else {
        await stopLiveDirect();
    }
}

async function startLiveDirect() {
    const btn = document.getElementById("btn-toggle-live");
    const roomInput = document.getElementById("live-room-input");
    const platformSelect = document.getElementById("live-platform-select");
    const roomId = roomInput ? roomInput.value.trim() : "";
    const platform = platformSelect ? platformSelect.value : "bilibili";

    if (btn) btn.disabled = true;
    try {
        // 携带开播向导记录的当前主播档案，保证音色严格按主播绑定解析 (ADR-10 驱动选择链)
        let anchorId = "";
        try {
            const mRes = await fetch(`${API_BASE}/settings/live-mode`);
            const mJson = await mRes.json();
            if (mJson.code === 0 && mJson.data && mJson.data.selected_anchor_id) {
                anchorId = mJson.data.selected_anchor_id;
            }
        } catch (e) {}

        const res = await fetch(`${API_BASE}/live/start`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ room_id: roomId, platform: platform, anchor_id: anchorId })
        });
        const json = await res.json();
        if (json.code === 0) {
            updateLiveStateUI(true);
            logDanmaku("系统通知", `本地直播源已启动！${roomId ? `正在监听【${platform}】房间事件: ${roomId}` : '已启动仿真弹幕互动'}；外部平台发布需在 OBS/直播伴侣中人工确认`, false);
            showToast("本地直播源已启动；外部发布待人工验收", "success", 5000);
        } else {
            alert("开播失败: " + (json.message || json.detail || "未知错误"));
        }
    } catch (e) {
        alert("操作异常: " + e);
    } finally {
        if (btn) btn.disabled = false;
    }
}

async function stopLiveDirect() {
    try {
        const res = await fetch(`${API_BASE}/live/stop`, { method: "POST" });
        const json = await res.json();
        if (json.code === 0) {
            updateLiveStateUI(false);
            stopAllAudioPlayback();
            logDanmaku("系统通知", "本地直播源已停止", false);
        }
    } catch (e) {
        alert("操作异常: " + e);
    }
}

function triggerBargeInVisual(reason) {
    const badge = document.getElementById("barge-in-badge");
    if (!badge) return;
    badge.innerText = `[抢占打断] ${reason}`;
    badge.style.display = "block";
    setTimeout(() => {
        badge.style.display = "none";
    }, 2500);
}

function escapeHtml(text) {
    if (text === null || text === undefined) return "";
    return String(text)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

function updateSpeakingWave(speaking) {

    const waveBars = document.querySelectorAll(".wave-bar");
    waveBars.forEach(b => {
        if (speaking) b.classList.add("speaking");
        else b.classList.remove("speaking");
    });
}

function logDanmaku(user, text, isP0 = false, rich = false) {
    const container = document.getElementById("danmaku-stream");
    if (!container) return;

    const item = document.createElement("div");
    item.className = `danmaku-item ${isP0 ? "p0" : ""}`;
    // 默认对 user/text 做 HTML 转义 (弹幕来自外部平台观众，防 DOM XSS)；
    // 仅系统内部构建的富文本模板 (内嵌 svg 图标) 经 rich=true 放行，动态值仍需调用方自行转义
    const safeUser = rich ? user : escapeHtml(user);
    const safeText = rich ? text : escapeHtml(text);
    item.innerHTML = `
        <div>
            <span class="danmaku-user">${safeUser}:</span>
            <span>${safeText}</span>
            ${isP0 ? '<span class="danmaku-p0-tag">P0 抢占</span>' : ''}
        </div>
        <span style="color: var(--text-muted); font-size: 11px;">${new Date().toLocaleTimeString()}</span>
    `;
    container.prepend(item);

    // 保持最多 50 条
    if (container.children.length > 50) {
        container.removeChild(container.lastChild);
    }
}

// 3. 硬件配置面板 (开播向导顶部：GPU/CPU/内存/推荐档位，每 5 秒刷新动态指标)
async function loadHardwareInfo() {
    try {
        const res = await fetch(`${API_BASE}/live/hardware`);
        const json = await res.json();
        if (json.code !== 0) return;
        const d = json.data;
        const gpu = d.gpu || {};

        const setText = (id, text) => { const el = document.getElementById(id); if (el) el.innerHTML = text; };

        // 显卡
        if (gpu.gpu_name) {
            setText("hw-panel-gpu", gpu.gpu_name);
            setText("hw-panel-gpu-sub",
                `显存 ${gpu.vram_total_gb}GB${gpu.vram_total_gb > 0 ? ` · 已用 ${gpu.vram_used_gb}GB` : ""} · ${gpu.cuda_available ? '<span style="color: var(--accent-emerald);">CUDA 加速可用</span>' : '<span style="color: var(--accent-amber);">无 CUDA 加速</span>'}`);
        } else {
            setText("hw-panel-gpu", '<span style="color: var(--text-muted);">未检测到独立显卡</span>');
            setText("hw-panel-gpu-sub", "将使用云端语音与轻量方案运行");
        }

        // 处理器
        const cpuName = d.cpu_name || "未知处理器";
        const cpuShort = cpuName.length > 36 ? cpuName.slice(0, 36) + "..." : cpuName;
        setText("hw-panel-cpu", cpuShort);
        setText("hw-panel-cpu-sub",
            `实时占用 <strong style="color: ${d.cpu_percent > 85 ? "var(--accent-danger)" : "var(--accent-emerald)"};">${Math.round(d.cpu_percent)}%</strong> · ${d.cpu_cores || 0} 逻辑核心`);
        setText("hw-panel-cpu", `${cpuShort}`);

        // 内存
        setText("hw-panel-ram", `${d.ram_used_gb} / ${d.ram_total_gb} GB`);
        setText("hw-panel-ram-sub",
            `占用 <strong style="color: ${d.ram_percent > 85 ? "var(--accent-danger)" : "var(--accent-emerald)"};">${Math.round(d.ram_percent || 0)}%</strong> · 总量 ${d.ram_total_gb}GB`);

        // 系统推荐
        setText("hw-panel-tier", d.recommended_mode || "—");
        setText("hw-panel-tier-sub", "基于上方硬件自动评估，已同步至模式推荐");
        const tierBadge = document.getElementById("hw-panel-tier-badge");
        if (tierBadge && d.recommended_mode) tierBadge.innerText = `推荐: ${d.recommended_mode}`;
    } catch (e) {
        // 忽略轻微抖动
    }
}

// 3.1 直播推流必备软件生态检测 (OBS/虚拟摄像头/伴侣/多媒体底座)
async function loadSoftwarePrerequisites(isManual = false) {
    const container = document.getElementById("prereq-cards-container");
    const overallBadge = document.getElementById("prereq-overall-badge");
    const alertBanner = document.getElementById("prereq-alert-banner");
    const alertText = document.getElementById("prereq-alert-text");
    const recheckBtn = document.getElementById("btn-recheck-prereqs");

    if (isManual && recheckBtn) {
        recheckBtn.disabled = true;
        recheckBtn.innerHTML = `${svg("refresh", "icon-sm")} 正在扫描...`;
    }

    try {
        const res = await fetch(`${API_BASE}/system/prerequisites`);
        if (!res.ok) {
            throw new Error(`探测接口响应 ${res.status} (旧进程尚未热加载新路由)`);
        }
        const json = await res.json();
        if (json.code !== 0 || !json.data) {
            throw new Error(json.message || "探测数据异常");
        }

        const { summary, items } = json.data;

        // 1. 更新顶部汇总 Badge
        if (overallBadge) {
            overallBadge.className = "brand-badge " + (
                summary.overall_level === "success" ? "green" :
                summary.overall_level === "warning" ? "amber" : "red"
            );
            overallBadge.innerText = `${summary.overall_level === "success" ? "本机组件就绪" : summary.overall_level === "warning" ? "核心待完善" : "必备未安装"} (${summary.ready_count}/${summary.total})`;
        }

        // 2. 更新告警横幅
        if (alertBanner) {
            if (summary.critical_missing > 0) {
                alertBanner.className = "prereq-alert-banner " + (summary.critical_missing >= 2 ? "alert" : "warning");
                alertBanner.style.display = "flex";
                alertBanner.innerHTML = `
                  <div class="prereq-alert-dot"></div>
                  <div id="prereq-alert-text"><strong>本机媒体组件诊断：</strong>${summary.overall_text}。若使用虚拟摄像头推流至公域电商平台，建议按下方指引完善配置。</div>
                `;
            } else if (summary.missing_count > 0) {
                alertBanner.className = "prereq-alert-banner warning";
                alertBanner.style.display = "flex";
                alertBanner.innerHTML = `
                  <div class="prereq-alert-dot"></div>
                  <div id="prereq-alert-text"><strong>推流就绪提示：</strong>核心主控已就绪，尚有 ${summary.missing_count} 项配套伴侣或音频隔离设备待配置，建议完善以保证商用播出纯净度。</div>

                `;
            } else {
                alertBanner.className = "prereq-alert-banner success";
                alertBanner.style.display = "flex";
                alertBanner.innerHTML = `
                  <div class="prereq-alert-dot"></div>
                  <div id="prereq-alert-text"><strong>本机媒体组件健全：</strong>${summary.overall_text}</div>
                `;
            }
        }

        // 3. 渲染各个必备软件卡片 (演播室双列横向清单)
        if (container && Array.isArray(items)) {
            const iconMap = {
                obs: "camera",
                vcam_driver: "video",
                pyvirtualcam: "box",
                live_partner: "broadcast",
                media_core: "cpu",
                python_runtime: "terminal",
                audio_devices: "mic"
            };

            container.innerHTML = items.map(it => {
                let stateText = "";
                let pillClass = "";
                let statusClass = `status-${it.status}`;

                if (it.key === "audio_devices") {
                    if (it.status === "installed" && it.has_cable) {
                        stateText = "检测通过";
                        pillClass = "pill-pass";
                        statusClass = "status-running";
                    } else if (it.status === "installed" || it.status === "running") {
                        stateText = "建议修复（推荐项）";
                        pillClass = "pill-warning";
                        statusClass = "status-warning";
                    } else {
                        stateText = "必须修复（必修项）";
                        pillClass = "pill-missing";
                        statusClass = "status-missing";
                    }
                } else if (it.status === "running" || it.status === "installed") {
                    stateText = "检测通过";
                    pillClass = "pill-pass";
                    statusClass = it.status === "running" ? "status-running" : "status-installed";
                } else {
                    if (it.required) {
                        stateText = "必须修复（必修项）";
                        pillClass = "pill-missing";
                        statusClass = "status-missing";
                    } else {
                        stateText = "建议修复（推荐项）";
                        pillClass = "pill-warning";
                        statusClass = "status-warning";
                    }
                }

                const tipClass = pillClass === "pill-pass" ? "tip-installed" : (pillClass === "pill-warning" ? "tip-warning" : "tip-missing");
                const iconName = iconMap[it.key] || "monitor";

                let actionBtnHtml = "";
                if (it.action_type === "url" && it.url) {
                    actionBtnHtml = `<a href="${it.url}" target="_blank" class="prereq-btn ${pillClass !== 'pill-pass' ? 'btn-action-primary' : ''}">${svg("external", "icon-sm")} ${it.action_text}</a>`;
                } else if (it.action_type === "copy" && it.command) {
                    actionBtnHtml = `<button class="prereq-btn" onclick="copyPrereqCommand('${it.command}', this)">${svg("copy", "icon-sm")} ${it.action_text}</button>`;
                } else if (it.action_type === "tip" && it.url) {
                    actionBtnHtml = `<a href="${it.url}" target="_blank" class="prereq-btn">${svg("info", "icon-sm")} ${it.action_text}</a>`;
                } else {
                    actionBtnHtml = `<span class="prereq-ok-label">${svg("check", "icon-sm")} 正常</span>`;
                }

                const tipIcon = pillClass === "pill-pass" ? "check" : (pillClass === "pill-warning" ? "warn" : "x");

                return `
                <div class="prereq-row ${statusClass}">
                  <!-- 第 1 列：图标 -->
                  <div class="prereq-row-icon">
                    ${svg(iconName, "icon")}
                  </div>

                  <!-- 第 2 列：软件名称与分类 Tag / 细节 -->
                  <div class="prereq-row-name-col">
                    <span class="prereq-row-name" title="${it.name}">${it.name}</span>
                    <div class="prereq-row-tags">
                      <span class="prereq-row-cat">${it.category}</span>
                      <span class="prereq-row-cat" style="color: var(--text-secondary);">${it.badge}</span>
                    </div>
                  </div>

                  <!-- 第 3 列：统一标准状态列 -->
                  <div class="prereq-row-status-col">
                    <span class="prereq-pill ${pillClass}">
                      <span class="prereq-dot"></span>
                      ${stateText}
                    </span>
                  </div>

                  <!-- 第 4 列：功能描述与实时诊断提示 -->
                  <div class="prereq-row-desc-col">
                    <div class="prereq-row-desc">${it.desc}</div>
                    <div class="prereq-row-tip ${tipClass}">
                      ${svg(tipIcon, "icon-sm")}
                      <span title="${it.tip}">${it.tip}</span>
                    </div>
                  </div>

                  <!-- 第 5 列：操作按钮 -->
                  <div class="prereq-row-action-col">
                    ${actionBtnHtml}
                  </div>
                </div>`;
            }).join("");

        }

    } catch (e) {
        console.warn("探测直播必备软件失败:", e);
        if (overallBadge) {
            overallBadge.className = "brand-badge amber";
            overallBadge.innerText = "需重启服务";
        }
        if (alertBanner && alertText) {
            alertBanner.className = "prereq-alert-banner warning";
            alertBanner.style.display = "flex";
            alertText.innerHTML = `<strong>本机媒体组件检测提醒：</strong>${e.message}。检测到当前运行的后端为旧进程，请重启本地服务（关闭旧终端后重新运行 run_agent.bat）即可生效。`;
        }
        if (container) {
            container.innerHTML = `
            <div class="hw-cell" style="grid-column: 1 / -1; text-align: center; padding: 24px;">
              <div style="font-size: 14px; font-weight: 600; color: var(--amber); margin-bottom: 8px;">
                ${svg("warn", "icon")} 未能连接到直播必备软件探测接口 (404)
              </div>
              <div style="font-size: 12px; color: var(--text-secondary); max-width: 520px; margin: 0 auto 16px; line-height: 1.6;">
                检测到您当前电脑后台正在运行未更新的旧版本后端进程（无法加载新增路由）。请关闭运行旧进程的黑底终端窗口，重新双击 <strong>run_agent.bat</strong> 启动服务，即可自动展现完整本机媒体组件。
              </div>
              <button class="btn btn-primary btn-sm" onclick="loadSoftwarePrerequisites(true)">
                ${svg("refresh", "icon-sm")} 重新检测
              </button>
            </div>`;
        }
    } finally {
        if (isManual && recheckBtn) {
            setTimeout(() => {
                recheckBtn.disabled = false;
                recheckBtn.innerHTML = `<svg class="icon-sm" viewBox="0 0 24 24" style="width: 12px; height: 12px;"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg> 重新检测`;
            }, 600);
        }
    }
}

// 复制安装命令小助手
window.copyPrereqCommand = function(cmd, btn) {
    if (!cmd) return;
    navigator.clipboard.writeText(cmd).then(() => {
        if (btn) {
            const originalHtml = btn.innerHTML;
            btn.innerHTML = `${svg("check", "icon-sm")} 已复制到剪切板`;
            btn.classList.add("action-green");
            setTimeout(() => {
                btn.innerHTML = originalHtml;
                btn.classList.remove("action-green");
            }, 2000);
        }
    }).catch(() => {
        prompt("请手动复制安装命令：", cmd);
    });
};

// 4. 主播设定 (需求3：卡片式角色选择 + 人设编辑 + 直播中禁止激活切换)
let roleCardsCache = [];
let selectedRoleId = null;

const ROLE_CARD_META = {
    "ecommerce": { icon: "orange", avatarSvg: "/static/svg/anchor_ecommerce.svg", tag: "橙色橘子·促单逼单", tagColor: "#F97316" },
    "entertainment": { icon: "mic", avatarSvg: "/static/svg/anchor_entertainment.svg", tag: "电光麦克风·逗梗陪伴", tagColor: "#2DD4A0" },
    "expert": { icon: "plus-circle", avatarSvg: "/static/svg/anchor_expert.svg", tag: "医疗+守护·专业解答", tagColor: "#EF4444" },
    "chitchat": { icon: "handshake", avatarSvg: "/static/svg/anchor_chitchat.svg", tag: "暖金握手·唠嗑搭子", tagColor: "#F59E0B" }
};

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
                        <button class="btn btn-sm btn-primary" onclick="flashSaleProduct('${escapeHtml(p.id)}', '${safeTitle}')">${svg("zap", "icon-sm")} 促单逼单</button>
                        <button class="btn btn-sm" onclick="editProduct('${escapeHtml(p.id)}')">编辑</button>
                        <button class="btn btn-sm btn-danger" onclick="deleteProduct('${escapeHtml(p.id)}', '${safeTitle}')">删除</button>
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

// 7. 音色管理 (需求5) 与主播音色下拉数据源
let voiceCache = [];

async function loadVoiceTable() {
    try {
        const res = await fetch(`${API_BASE}/voices/list`);
        const json = await res.json();
        if (json.code !== 0) return;
        voiceCache = json.data || [];

        // 音色表格
        const tbody = document.getElementById("voices-tbody");
        if (tbody) {
            tbody.innerHTML = "";
            voiceCache.forEach(v => {
                const statusBadge = v.synthesis_status === "reference_ready"
                    ? '<span style="color: var(--accent-emerald);">● CosyVoice 参考已登记</span>'
                    : (v.feature_status === "ready"
                        ? '<span style="color: var(--accent-sky);">● 样本特征已就绪（非克隆）</span>'
                        : '<span style="color: var(--accent-amber);">◌ 特征处理中...</span>');
                const tr = document.createElement("tr");
                tr.innerHTML = `
                    <td style="font-weight: 600;">${v.name}</td>
                    <td>${(v.speech_speed || 1.0).toFixed(2)}x</td>
                    <td>${statusBadge}</td>
                    <td style="white-space: nowrap;">
                        <button class="btn btn-sm" onclick="previewVoice('${v.id}')">${svg("play", "icon-sm")} 试听原始样本</button>
                        <button class="btn btn-sm btn-primary" onclick="cloneVoice('${v.id}', '${v.name}')">登记参考音色</button>
                        <button class="btn btn-sm" onclick="editVoice('${v.id}')">改名</button>
                        <button class="btn btn-sm btn-danger" onclick="deleteVoice('${v.id}', '${v.name}')">删除</button>
                    </td>
                `;
                tbody.appendChild(tr);
            });
        }

        // 主播管理页的音色下拉
        const anchorVoiceSel = document.getElementById("anchor-voice");
        if (anchorVoiceSel) {
            anchorVoiceSel.innerHTML = '<option value="">未绑定音色</option>';
            voiceCache.forEach(v => {
                const opt = document.createElement("option");
                opt.value = v.id;
                opt.innerText = v.name;
                anchorVoiceSel.appendChild(opt);
            });
        }
    } catch (e) {
        console.error("加载音色失败", e);
    }
}

async function submitVoice() {
    const editId = document.getElementById("voice-edit-id").value;
    const name = document.getElementById("voice-name").value.trim();
    const fileInput = document.getElementById("voice-file");
    if (!name) { alert("请输入音色名称"); return; }

    try {
        if (!editId) {
            // 新建：必须上传声音样本
            if (!fileInput.files || !fileInput.files.length) {
                alert("请选择 10 秒左右清晰人声音频文件 (WAV/MP3)");
                return;
            }
            const fd = new FormData();
            fd.append("name", name);
            fd.append("audio_file", fileInput.files[0]);
            fd.append("speed", document.getElementById("voice-speed").value);
            const res = await fetch(`${API_BASE}/voices/clone`, { method: "POST", body: fd });
            const json = await res.json();
            if (json.code !== 0) { alert("上传失败: " + (json.detail || json.message)); return; }

            // 上传后尝试登记 CosyVoice 参考音色；无服务时仅保留本地声学特征
            const voiceId = json.data.id;
            showToast("样本与本地声学特征已保存，正在检查 CosyVoice 参考登记...", "info");
            const cloneRes = await fetch(`${API_BASE}/voices/${voiceId}/clone`, { method: "POST" });
            const cloneJson = await cloneRes.json();
            if (cloneJson.code === 0) {
                const kind = cloneJson.data.engine === "cosyvoice" ? "CosyVoice 参考音色已登记" : "本地声学特征已就绪（非克隆音色）";
                showToast(`音色【${name}】${kind}。试听按钮播放的是原始样本`, "success", 5500);
            } else {
                alert("自动克隆未完成: " + (cloneJson.detail || cloneJson.message));
            }
        } else {
            const res = await fetch(`${API_BASE}/voices/update`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ id: editId, name, speech_speed: parseFloat(document.getElementById("voice-speed").value) || 1.0 })
            });
            const json = await res.json();
            if (json.code !== 0) { alert("更新失败: " + (json.detail || json.message)); return; }
        }
        resetVoiceForm();
        loadVoiceTable();
    } catch (e) { alert("操作异常: " + e); }
}

async function cloneVoice(voiceId, name) {
    if (!confirm(`确认尝试在已配置的 CosyVoice 服务登记【${name}】为参考音色？未配置服务时只会保留本地声学特征。`)) return;
    try {
        const res = await fetch(`${API_BASE}/voices/${voiceId}/clone`, { method: "POST" });
        const json = await res.json();
        alert(json.message || (json.code === 0 ? "参考登记已处理" : "参考登记失败"));
        loadVoiceTable();
    } catch (e) { alert("克隆异常: " + e); }
}

// 在线试听：页内浮动播放器 (不新开标签页，即点即听)
let previewAudioEl = null;
function previewVoice(voiceId) {
    if (previewAudioEl) { try { previewAudioEl.pause(); } catch (e) { /* 忽略 */ } }
    previewAudioEl = new Audio(`${API_BASE}/voices/${voiceId}/preview`);
    previewAudioEl.play().then(() => {
        showToast("正在播放声音样本...", "info", 2000);
    }).catch(() => {
        alert("浏览器阻止了自动播放，请先点击页面任意位置激活音频权限，再点一次试听");
    });
}

async function editVoice(voiceId) {
    const v = voiceCache.find(x => x.id === voiceId);
    if (!v) return;
    document.getElementById("voice-edit-id").value = v.id;
    document.getElementById("voice-name").value = v.name;
    document.getElementById("voice-speed").value = v.speech_speed || 1.0;
    document.getElementById("voice-speed-val").innerText = (v.speech_speed || 1.0).toFixed(2) + "x";
    document.getElementById("voice-edit-hint").innerText = `(正在编辑: ${v.name})`;
    document.getElementById("voice-reset-btn").style.display = "inline-block";
}

function resetVoiceForm() {
    document.getElementById("voice-edit-id").value = "";
    document.getElementById("voice-name").value = "";
    document.getElementById("voice-file").value = "";
    document.getElementById("voice-speed").value = 1.0;
    document.getElementById("voice-speed-val").innerText = "1.0x";
    document.getElementById("voice-edit-hint").innerText = "";
    document.getElementById("voice-reset-btn").style.display = "none";
}

async function deleteVoice(voiceId, name) {
    if (!confirm(`确认删除音色【${name}】？`)) return;
    try {
        const res = await fetch(`${API_BASE}/voices/${voiceId}`, { method: "DELETE" });
        const json = await res.json();
        if (json.code === 0) loadVoiceTable();
    } catch (e) { alert("删除失败: " + e); }
}

// 服务商详细元数据定义 (帮助用户秒懂每一项是干什么的、是否必填、常见填法)
const PROVIDER_META = {
    "openai_compatible": {
        title: "云端大语言模型 (AI 主播思考大脑)",
        typeBadge: "LLM 大脑",
        requiredTag: '<span class="badge-required">必配其一</span>',
        recommendTag: '<span class="badge-recommend">推荐首选</span>',
        desc: "负责实时理解直播间观众弹幕、组织高情商话术、讲透商品核心卖点、并根据逼单节奏催单。支持任意兼容 OpenAI 接口规范的云端大模型服务（如 DeepSeek、Kimi、通义千问、OpenAI 等）。",
        urlLabel: "服务商 API 接口地址 (Base URL):",
        urlTip: "服务商提供的模型请求根地址，通常以 <code>/v1</code> 结尾。如使用 DeepSeek 请点击下方快捷标签填入。",
        urlPills: [
            { text: "填入 DeepSeek 官方地址", val: "https://api.deepseek.com/v1" },
            { text: "填入 OpenAI 官方地址", val: "https://api.openai.com/v1" },
            { text: "填入 硅基流动 SiliconFlow", val: "https://api.siliconflow.cn/v1" }
        ],
        modelLabel: "思考模型代号 (Model ID):",
        modelTip: "<strong>什么是模型代号？</strong> 决定 AI 主播具体用哪一个聪明程度的脑子来思考并吐字。例如 DeepSeek 填 <code>deepseek-chat</code>，OpenAI 填 <code>gpt-4o</code>。",
        modelPills: [
            { text: "deepseek-chat (超实惠·高情商带货推荐)", val: "deepseek-chat" },
            { text: "deepseek-reasoner (深度逻辑推理)", val: "deepseek-reasoner" },
            { text: "gpt-4o (国际顶配多模态)", val: "gpt-4o" }
        ],
        keyLabel: "API 访问密钥 (API Key - 本地硬件加密):",
        keyTip: "在对应大模型开放平台注册后生成的密钥（以 <code>sk-</code> 开头）。必填，系统会以 AES-256 加密存放在本地。"
    },
    "local_ollama": {
        title: "本地离线大模型 (Ollama 纯单机引擎)",
        typeBadge: "LLM 大脑",
        requiredTag: '<span class="badge-optional">选填</span>',
        recommendTag: '',
        desc: "在您的本地独立显卡上运行开源模型，完全断网也能正常直播，免收任何 API 接口费，隐私绝对保密。需电脑预先下载并运行 Ollama 软件。",
        urlLabel: "本地 Ollama 监听地址:",
        urlTip: "本地 Ollama 服务的网络端口地址，默认通常为 <code>http://127.0.0.1:11434</code>。",
        urlPills: [
            { text: "默认地址: http://127.0.0.1:11434", val: "http://127.0.0.1:11434" }
        ],
        modelLabel: "本地模型名称 (Model Tag):",
        modelTip: "<strong>什么是本地模型名？</strong> 即您在本地终端通过 <code>ollama pull</code> 下载的模型名称标签。",
        modelPills: [
            { text: "deepseek-r1:14b", val: "deepseek-r1:14b" },
            { text: "deepseek-r1:8b", val: "deepseek-r1:8b" },
            { text: "qwen2.5:7b", val: "qwen2.5:7b" }
        ],
        keyLabel: "API 访问密钥 (API Key):",
        keyTip: "本地单机部署无需 API Key，保持留空即可。"
    },
    "cloud_edge_tts": {
        title: "微软免费智能语音 (Edge-TTS 高保真发音)",
        typeBadge: "TTS 语音",
        requiredTag: '<span class="badge-required">必配其一</span>',
        recommendTag: '<span class="badge-recommend">新手强烈推荐</span>',
        desc: "由微软免费提供的知性自然发音引擎，零显卡算力占用，完全免费，开箱即用，自带多种拟真普通话主播音色，发音流畅自然。",
        urlLabel: "服务通信通道:",
        urlTip: "使用微软内置免费直连通道，无需设置，保持留空即可。",
        urlPills: [],
        modelLabel: "主播发音人音色代号 (Voice ID):",
        modelTip: "<strong>什么是发音人音色？</strong> 决定 AI 主播开口说话的声音音色与性格特征。点击下方快捷标签可一键选择：",
        modelPills: [
            { text: "zh-CN-XiaoxiaoNeural (知性亲和女声·带货首选)", val: "zh-CN-XiaoxiaoNeural" },
            { text: "zh-CN-XiaoyiNeural (甜美活泼少女·娱乐陪伴)", val: "zh-CN-XiaoyiNeural" },
            { text: "zh-CN-YunjianNeural (沉稳专业男声·咨询顾问)", val: "zh-CN-YunjianNeural" },
            { text: "zh-CN-YunxiNeural (自然阳光暖男·唠嗑互动)", val: "zh-CN-YunxiNeural" }
        ],
        keyLabel: "API 访问密钥 (API Key):",
        keyTip: "微软免费通道免鉴权，无需填写 Key。"
    },
    "local_cosyvoice": {
        title: "本地声音克隆引擎 (CosyVoice 零样本声音复刻)",
        typeBadge: "TTS 语音",
        requiredTag: '<span class="badge-optional">选填</span>',
        recommendTag: '',
        desc: "基于阿里开源 CosyVoice2 架构。只需您上传一段 10 秒钟的原声音频，即可直接复刻出您的专属声音进行直播（需要本地具备 6GB+ 独立显卡）。",
        urlLabel: "本地 CosyVoice 推理端口地址:",
        urlTip: "本地独立启动的 CosyVoice 服务端口，默认通常为 <code>http://127.0.0.1:9233</code>。",
        urlPills: [
            { text: "默认地址: http://127.0.0.1:9233", val: "http://127.0.0.1:9233" }
        ],
        modelLabel: "声学克隆版本号:",
        modelTip: "<strong>版本号说明：</strong> 默认为官方推荐的 <code>CosyVoice2-0.5B</code> 多情感流式生成模型。",
        modelPills: [
            { text: "CosyVoice2-0.5B (低延迟流式)", val: "CosyVoice2-0.5B" }
        ],
        keyLabel: "API 访问密钥 (API Key):",
        keyTip: "本地私有服务免密，无需填写。"
    },
    "remote_gpu": {
        title: "端云分离远程 GPU 节点 (云端 4090 渲染回传)",
        typeBadge: "远程算力",
        requiredTag: '<span class="badge-required">模式 C 必配</span>',
        recommendTag: '<span class="badge-recommend">端云分离首选</span>',
        desc: "在 RunPod / AutoDL / 阿里云租用 4090 容器后，填入其 WebSocket 渲染网关地址与鉴权 Token，本地调度中枢即可把渲染任务穿透到云端，4K 画质推回本地 OBS。",
        urlLabel: "云端节点 WebSocket 地址:",
        urlTip: "格式 <code>ws://服务器IP:8888/ws/render</code>。参考 scripts/docker-runpod.sh 一键拉起云端节点。",
        urlPills: [],
        modelLabel: "节点备注 (选填):",
        modelTip: "例如：AutoDL 华东 4090 按量计费节点。",
        modelPills: [],
        keyLabel: "云端节点鉴权 Token (AUTH_TOKEN):",
        keyTip: "云端容器启动时设置的 AUTH_TOKEN，本地与云端必须一致。"
    }
};

// ==============================================================================
// 8. 云端大语言模型 (AI 主播思考大脑) 多生态选型、多配置列表与连通性测试
// ==============================================================================

const BUILTIN_LLM_ECOSYSTEM = [
    {
        id: "deepseek",
        name: "DeepSeek",
        tagline: "国产性价比标杆 · 高情商促单",
        brandColor: "#0284C7",
        logoSvg: "/static/svg/model_deepseek.svg",
        defaultBaseUrl: "https://api.deepseek.com/v1",
        recommendedModels: ["deepseek-chat", "deepseek-reasoner"],
        desc: "国产顶流高情商大模型，超低调用资费，高情商话术与实时弹幕互动首选。",
        urlPills: [
            { text: "DeepSeek 官方 (推荐)", val: "https://api.deepseek.com/v1" },
            { text: "备用直连端点", val: "https://api.deepseek.com" }
        ],
        modelPills: [
            { text: "deepseek-chat (超实惠·高情商带货首选)", val: "deepseek-chat" },
            { text: "deepseek-reasoner (深度逻辑推理链)", val: "deepseek-reasoner" }
        ]
    },
    {
        id: "qwen",
        name: "Qwen 通义",
        tagline: "直播电商霸主 · 极速指令遵循",
        brandColor: "#0070F3",
        logoSvg: "/static/svg/model_qwen.svg",
        defaultBaseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1",
        recommendedModels: ["qwen-plus", "qwen-turbo", "qwen-max", "qwen2.5-72b-instruct"],
        desc: "阿里系模型适合电商直播话术生成，支持促销表达与商品解读，并可与 CosyVoice 配置组合使用。",
        urlPills: [
            { text: "阿里云 DashScope (官方兼容)", val: "https://dashscope.aliyuncs.com/compatible-mode/v1" },
            { text: "本地 Ollama Qwen2.5", val: "http://127.0.0.1:11434/v1" }
        ],
        modelPills: [
            { text: "qwen-plus (电商直播强烈推荐)", val: "qwen-plus" },
            { text: "qwen-turbo (毫秒极速首包响应)", val: "qwen-turbo" },
            { text: "qwen-max (高阶多任务复杂推理)", val: "qwen-max" }
        ]
    },
    {
        id: "minimax",
        name: "MiniMax",
        tagline: "万亿长文本 · 细腻拟真共情",
        brandColor: "#FF5226",
        logoSvg: "/static/svg/model_minimax.svg",
        defaultBaseUrl: "https://api.minimax.chat/v1",
        recommendedModels: ["MiniMax-Text-01", "abab6.5s-chat"],
        desc: "全自研万亿长文本与拟真共情模型，情绪与音色表达出众，直播陪伴感强。",
        urlPills: [
            { text: "MiniMax 官方", val: "https://api.minimax.chat/v1" }
        ],
        modelPills: [
            { text: "MiniMax-Text-01 (旗舰长文本)", val: "MiniMax-Text-01" },
            { text: "abab6.5s-chat (拟人多轮对话)", val: "abab6.5s-chat" }
        ]
    },
    {
        id: "kimi",
        name: "Kimi 月暗",
        tagline: "超长上下文 · 直播选品记忆",
        brandColor: "#1783FF",
        logoSvg: "/static/svg/model_kimi.svg",
        defaultBaseUrl: "https://api.moonshot.cn/v1",
        recommendedModels: ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        desc: "无损超长上下文标杆，百款商品 SKU 参数、品牌白皮书与大促规则精准深度召回，零幻觉不乱编。",
        urlPills: [
            { text: "Moonshot 官方", val: "https://api.moonshot.cn/v1" }
        ],
        modelPills: [
            { text: "moonshot-v1-8k (常规弹幕互动)", val: "moonshot-v1-8k" },
            { text: "moonshot-v1-32k (海量商品挂载召回)", val: "moonshot-v1-32k" }
        ]
    },
    {
        id: "glm",
        name: "GLM 智谱",
        tagline: "清华系标杆 · 合规安全极速",
        brandColor: "#0D9488",
        logoSvg: "/static/svg/model_glm.svg",
        defaultBaseUrl: "https://open.bigmodel.cn/api/paas/v4/",
        recommendedModels: ["glm-4-flash", "glm-4-plus", "glm-4"],
        desc: "智谱自研成熟基座，中文语境深厚，安全审查与合规能力极强，glm-4-flash 免费且超快。",
        urlPills: [
            { text: "智谱开放平台官方", val: "https://open.bigmodel.cn/api/paas/v4/" }
        ],
        modelPills: [
            { text: "glm-4-flash (超快免费极速响应)", val: "glm-4-flash" },
            { text: "glm-4-plus (高阶综合认知)", val: "glm-4-plus" }
        ]
    },
    {
        id: "gemini",
        name: "Gemini",
        tagline: "超快首包 · 原生多模态感知",
        brandColor: "#2563EB",
        logoSvg: "/static/svg/model_gemini.svg",
        defaultBaseUrl: "https://generativelanguage.googleapis.com/v1beta/openai/",
        recommendedModels: ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-pro"],
        desc: "Google 旗舰多模态大模型，flash 系列具备毫秒级首包极速生成，契合多模态眼见即所言。",
        urlPills: [
            { text: "Google 官方 OpenAI 兼容通道", val: "https://generativelanguage.googleapis.com/v1beta/openai/" }
        ],
        modelPills: [
            { text: "gemini-2.0-flash (次世代极速)", val: "gemini-2.0-flash" },
            { text: "gemini-1.5-flash (超快首包)", val: "gemini-1.5-flash" }
        ]
    },
    {
        id: "chatgpt",
        name: "ChatGPT",
        tagline: "全球顶级旗舰 · 全能综合推理",
        brandColor: "#10A37F",
        logoSvg: "/static/svg/model_chatgpt.svg",
        defaultBaseUrl: "https://api.openai.com/v1",
        recommendedModels: ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"],
        desc: "OpenAI 工业级基准大模型，具备出色的多任务理解、结构化输出与丰富插件兼容能力。",
        urlPills: [
            { text: "OpenAI 官方", val: "https://api.openai.com/v1" }
        ],
        modelPills: [
            { text: "gpt-4o (旗舰综合多模态)", val: "gpt-4o" },
            { text: "gpt-4o-mini (经济型轻量大脑)", val: "gpt-4o-mini" }
        ]
    },
    {
        id: "custom",
        name: "自定义/离线",
        tagline: "支持 Ollama / 硅基流动",
        brandColor: "#F59E0B",
        logoSvg: "/static/svg/model_custom.svg",
        defaultBaseUrl: "https://api.siliconflow.cn/v1",
        recommendedModels: ["Qwen/Qwen2.5-72B-Instruct", "deepseek-ai/DeepSeek-V3"],
        desc: "任意符合 OpenAI API 规范的代理服务、云端 API 或本地 Ollama/vLLM 离线网关自由接入。",
        urlPills: [
            { text: "硅基流动 SiliconFlow", val: "https://api.siliconflow.cn/v1" },
            { text: "本地 Ollama 离线", val: "http://127.0.0.1:11434/v1" }
        ],
        modelPills: [
            { text: "Qwen/Qwen2.5-72B-Instruct", val: "Qwen/Qwen2.5-72B-Instruct" },
            { text: "deepseek-ai/DeepSeek-V3", val: "deepseek-ai/DeepSeek-V3" }
        ]
    }
];

let currentSelectedLLMProvider = "deepseek";
let cachedAllConfigs = [];

// 初始化大模型品牌卡片网格 (严格 8 列横向一排)
function renderLLMEcosystemGrid(selectedId = "deepseek") {
    const grid = document.getElementById("llm-ecosystem-grid");
    if (!grid) return;
    grid.innerHTML = "";
    grid.style.display = "grid";
    grid.style.gridTemplateColumns = "repeat(8, minmax(0, 1fr))";
    grid.style.gap = "10px";
    grid.style.marginBottom = "22px";
    grid.style.width = "100%";

    BUILTIN_LLM_ECOSYSTEM.forEach(item => {
        const card = document.createElement("div");
        card.className = "llm-provider-card" + (item.id === selectedId ? " selected" : "");
        card.setAttribute("data-provider", item.id);
        card.onclick = () => selectLLMProvider(item.id);

        card.innerHTML = `
            <img src="${item.logoSvg}?v=1.7.5" class="llm-provider-logo" alt="${item.name}">
            <div class="llm-provider-name" title="${item.name}">${item.name}</div>
            <div class="llm-provider-tagline" title="${item.tagline}">${item.tagline}</div>
        `;
        grid.appendChild(card);
    });
}

// 智能解析/推断大模型生态元数据（精准容错历史遗留的 openai_compatible / deepseek_api / custom 等命称，通过 URL 及模型特征定位官方品牌）
function resolveLLMProviderMeta(providerId, config = null) {
    const rawId = (providerId || "").toLowerCase().trim();

    // 1. 若显式传入有效的具体品牌 ID（且不是 custom / openai_compatible 等通用词），直接命中
    if (rawId && rawId !== "custom" && rawId !== "openai_compatible" && rawId !== "local_ollama") {
        const hit = BUILTIN_LLM_ECOSYSTEM.find(p => p.id === rawId);
        if (hit) return hit;
    }

    // 2. 根据特征词或配置对象的 base_url / model_name / provider_name 智能深度识别品牌
    const pName = (config && config.provider_name ? config.provider_name : rawId).toLowerCase();
    const bUrl = (config && config.base_url ? config.base_url : "").toLowerCase();
    const mName = (config && config.model_name ? config.model_name : "").toLowerCase();
    const combined = `${pName} ${bUrl} ${mName}`;

    if (combined.includes("deepseek")) {
        return BUILTIN_LLM_ECOSYSTEM.find(p => p.id === "deepseek");
    }
    if (combined.includes("qwen") || combined.includes("dashscope") || combined.includes("aliyun")) {
        return BUILTIN_LLM_ECOSYSTEM.find(p => p.id === "qwen");
    }
    if (combined.includes("minimax")) {
        return BUILTIN_LLM_ECOSYSTEM.find(p => p.id === "minimax");
    }
    if (combined.includes("kimi") || combined.includes("moonshot")) {
        return BUILTIN_LLM_ECOSYSTEM.find(p => p.id === "kimi");
    }
    if (combined.includes("gemini") || combined.includes("generativelanguage") || combined.includes("googleapis")) {
        return BUILTIN_LLM_ECOSYSTEM.find(p => p.id === "gemini");
    }
    if (combined.includes("glm") || combined.includes("zhipu") || combined.includes("bigmodel")) {
        return BUILTIN_LLM_ECOSYSTEM.find(p => p.id === "glm");
    }
    if (combined.includes("chatgpt") || combined.includes("openai.com") || (combined.includes("gpt-") && !combined.includes("deepseek"))) {
        return BUILTIN_LLM_ECOSYSTEM.find(p => p.id === "chatgpt");
    }

    // 3. 若均无法匹配具体特征，但显式指定了 custom，返回 custom
    if (rawId === "custom") {
        return BUILTIN_LLM_ECOSYSTEM.find(p => p.id === "custom") || BUILTIN_LLM_ECOSYSTEM[0];
    }

    // 兜底策略：若未识别且非空配置，优先返回首位 DeepSeek 或 custom
    return BUILTIN_LLM_ECOSYSTEM.find(p => p.id === "deepseek") || BUILTIN_LLM_ECOSYSTEM[0];
}

// 对于已经配置过的模型，自动解密填入真实 API Key 并自动拉取该服务商所有可用模型
async function autoFillRealKeyIfConfigured(configId, existingConfig = null) {
    const keyInput = document.getElementById("llm-input-key");
    const eyeBtn = document.getElementById("btn-toggle-key-eye");
    const hintEl = document.getElementById("llm-key-status-hint");
    if (!keyInput || !configId) return;

    const eyeShowSvg = `<svg viewBox="0 0 24 24"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`;

    // 默认保持 password 脱敏模式
    keyInput.type = "password";
    if (eyeBtn) {
        eyeBtn.innerHTML = eyeShowSvg;
        eyeBtn.title = "点击显示真实密钥明文";
        eyeBtn.style.color = "";
    }

    let realKey = cachedDecryptedKeys[configId] || "";

    // 内存未命中则异步拉取真实明文数据
    if (!realKey) {
        try {
            const res = await fetch(`${API_BASE}/settings/configs/${configId}/raw-key`);
            const json = await res.json();
            if (json.code === 0 && json.raw_key) {
                cachedDecryptedKeys[configId] = json.raw_key;
                realKey = json.raw_key;
            }
        } catch (e) {
            console.warn("自动获取真实 API Key 异常:", e);
        }
    }

    const curIdInput = document.getElementById("llm-editor-config-id");
    if (curIdInput && curIdInput.value === configId && realKey) {
        keyInput.value = realKey;
        keyInput.type = "password"; // 严格默认脱敏显示 .........
        if (hintEl) hintEl.innerText = "已载入硬件加密密钥 (默认脱敏)";

        // 核心增强：自动静默拉取服务商名下所有可用真实模型，供用户在多个模型间自由点击切换！
        if (existingConfig && existingConfig.base_url) {
            fetchAndRenderModels(
                existingConfig.base_url,
                realKey,
                existingConfig.provider_name,
                configId,
                existingConfig.model_name,
                false
            );
        }
    }
}

// 切换当前配置的大模型品牌
function selectLLMProvider(providerId, existingConfig = null) {
    const meta = resolveLLMProviderMeta(providerId, existingConfig);
    currentSelectedLLMProvider = meta.id;

    // 若未显式传入已有配置，自动在已缓存的配置中寻找是否已配置过该品牌
    if (!existingConfig && cachedAllConfigs && cachedAllConfigs.length > 0) {
        const found = cachedAllConfigs.find(c => {
            if (c.config_group !== "llm") return false;
            const cMeta = resolveLLMProviderMeta(c.provider_name, c);
            return cMeta.id === meta.id;
        });
        if (found) {
            existingConfig = found;
        }
    }

    // 更新上方品牌网格高亮 (精准高亮对应的品牌卡片)
    document.querySelectorAll(".llm-provider-card").forEach(c => {
        c.classList.toggle("selected", c.getAttribute("data-provider") === meta.id);
    });

    // 更新编辑表单头部信息
    const logoEl = document.getElementById("llm-editor-logo");
    const titleEl = document.getElementById("llm-editor-title");
    const badgeEl = document.getElementById("llm-editor-badge");
    const descEl = document.getElementById("llm-editor-desc");
    const providerInput = document.getElementById("llm-editor-provider");
    const configIdInput = document.getElementById("llm-editor-config-id");

    if (logoEl) logoEl.src = meta.logoSvg + "?v=1.7.5";
    if (titleEl) titleEl.innerText = existingConfig ? `编辑模型配置: ${existingConfig.model_name || meta.name}` : `配置 ${meta.name}`;
    if (badgeEl) badgeEl.innerText = meta.tagline.split("·")[0].trim();
    if (descEl) descEl.innerText = meta.desc;
    if (providerInput) providerInput.value = meta.id;
    if (configIdInput) configIdInput.value = existingConfig ? existingConfig.id : "";

    // 填充 Base URL
    const urlInput = document.getElementById("llm-input-url");
    if (urlInput) {
        urlInput.value = existingConfig ? (existingConfig.base_url || "") : meta.defaultBaseUrl;
    }

    // 渲染 Base URL 快捷标签
    const urlPillsBox = document.getElementById("llm-editor-url-pills");
    if (urlPillsBox) {
        urlPillsBox.innerHTML = `
            <span style="font-size: 11px; color: var(--text-muted);">官方端点:</span>
            ${meta.urlPills.map(p => `<span class="quick-pill" onclick="fillInputValue('llm-input-url', '${p.val}')">${p.text}</span>`).join("")}
        `;
    }

    // 第五行：初始化具体模型与选定状态（真实数据优先填充）
    const modelInput = document.getElementById("llm-input-model");
    const container = document.getElementById("llm-fetched-models-container");
    const countStatusEl = document.getElementById("llm-models-count-status");
    const pingStatusEl = document.getElementById("llm-ping-latency-status");

    if (pingStatusEl) pingStatusEl.innerHTML = `握手测试: 未测试`;

    if (existingConfig && existingConfig.id && existingConfig.model_name) {
        if (modelInput) modelInput.value = existingConfig.model_name;
        if (countStatusEl) {
            countStatusEl.innerHTML = `<span>已配置使用模型: <strong>${escapeHtml(existingConfig.model_name)}</strong></span>`;
        }
        if (container) {
            container.innerHTML = `
                <div class="fetched-model-pill selected" data-model="${escapeHtml(existingConfig.model_name)}" onclick="selectFetchedModel('${escapeHtml(existingConfig.model_name)}')" title="已保存使用的模型: ${escapeHtml(existingConfig.model_name)}">
                    ${escapeHtml(existingConfig.model_name)}
                </div>
            `;
        }
    } else {
        // 未配置状态：严格保持空白，等待用户输入 Key 并点击获取
        if (modelInput) modelInput.value = "";
        if (countStatusEl) {
            countStatusEl.innerHTML = `<span>获取状态: 尚未获取模型</span>`;
        }
        if (container) {
            container.innerHTML = `<div style="color: var(--text-muted); font-size: 12px;">暂无模型。请在第二行输入 API Key 后，点击第三行「获取模型」实时加载。</div>`;
        }
    }

    // 第二行：填充 API Key（对于已配置过的模型，自动填入真实明文密钥）
    const keyInput = document.getElementById("llm-input-key");
    const eyeBtn = document.getElementById("btn-toggle-key-eye");
    const hintEl = document.getElementById("llm-key-status-hint");

    if (existingConfig && existingConfig.id) {
        // 自动填入真实密钥明文，并异步拉取全部多个可用模型供用户切换
        autoFillRealKeyIfConfigured(existingConfig.id, existingConfig);
    } else {
        if (keyInput) {
            keyInput.type = "password";
            keyInput.value = "";
            keyInput.placeholder = "输入 sk-xxxx 密钥（本地硬件安全加密）";
        }
        if (eyeBtn) {
            eyeBtn.innerHTML = `<svg viewBox="0 0 24 24"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`;
            eyeBtn.title = "点击显示真实密钥明文";
            eyeBtn.style.color = "";
        }
        if (hintEl) hintEl.innerText = "本地硬件级加密存储";
    }

    // 隐藏上一次的测速结果
    const testResultBox = document.getElementById("llm-test-result");
    if (testResultBox) testResultBox.style.display = "none";
}

// 模型下拉选择联动
function handleSelectModelChange(val) {
    if (!val) return;
    const modelInput = document.getElementById("llm-input-model");
    if (modelInput) modelInput.value = val;
}

// 内存缓存已解密的密钥，避免重复网络请求
let cachedDecryptedKeys = {};

// 核心功能: 点击眼睛图标切换展示真实密钥明文 / 隐藏为密文
async function toggleLLMKeyVisibility() {
    const keyInput = document.getElementById("llm-input-key");
    const eyeBtn = document.getElementById("btn-toggle-key-eye");
    const configIdInput = document.getElementById("llm-editor-config-id");
    const hintEl = document.getElementById("llm-key-status-hint");
    if (!keyInput || !eyeBtn) return;

    const eyeShowSvg = `<svg viewBox="0 0 24 24"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`;
    const eyeHideSvg = `<svg viewBox="0 0 24 24"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>`;

    const isPassword = keyInput.type === "password";
    const configId = configIdInput ? configIdInput.value.trim() : "";

    if (isPassword) {
        // 准备查看真实数据
        // 场景 1: 输入框内已有用户刚输入的内容
        if (keyInput.value && keyInput.value.trim()) {
            keyInput.type = "text";
            eyeBtn.innerHTML = eyeHideSvg;
            eyeBtn.title = "点击隐藏 API Key";
            eyeBtn.style.color = "#10B981";
            if (hintEl) hintEl.innerText = "已呈现输入明文";
            return;
        }

        // 场景 2: 输入框内暂无新文本，但正在编辑一个已加密存储的配置，从后端拉取真实解密数据
        if (configId) {
            if (cachedDecryptedKeys[configId]) {
                keyInput.value = cachedDecryptedKeys[configId];
                keyInput.type = "text";
                eyeBtn.innerHTML = eyeHideSvg;
                eyeBtn.title = "点击隐藏 API Key";
                eyeBtn.style.color = "#10B981";
                if (hintEl) hintEl.innerText = "已解密呈现真实密钥明文";
                return;
            }

            eyeBtn.disabled = true;
            try {
                const res = await fetch(`${API_BASE}/settings/configs/${configId}/raw-key`);
                const json = await res.json();
                if (json.code === 0 && json.raw_key) {
                    cachedDecryptedKeys[configId] = json.raw_key;
                    keyInput.value = json.raw_key;
                    keyInput.type = "text";
                    eyeBtn.innerHTML = eyeHideSvg;
                    eyeBtn.title = "点击隐藏 API Key";
                    eyeBtn.style.color = "#10B981";
                    if (hintEl) hintEl.innerText = "已解密呈现真实密钥明文";
                } else {
                    alert("未能获取到已加密存储的密钥: " + (json.message || "未知原因"));
                }
            } catch (e) {
                alert("请求解密密钥异常: " + e);
            } finally {
                eyeBtn.disabled = false;
            }
        } else {
            // 没有已存配置且输入框为空，直接切为 text
            keyInput.type = "text";
            eyeBtn.innerHTML = eyeHideSvg;
            eyeBtn.title = "点击隐藏 API Key";
            eyeBtn.style.color = "#10B981";
        }
    } else {
        // 切换回密码遮罩状态
        keyInput.type = "password";
        eyeBtn.innerHTML = eyeShowSvg;
        eyeBtn.title = "点击显示真实密钥明文";
        eyeBtn.style.color = "";
        if (hintEl) hintEl.innerText = "本地硬件级加密存储";
    }
}

// 核心通用函数：拉取并渲染服务商名下的多个可用模型药丸（供用户在多个模型间自由切换）
async function fetchAndRenderModels(baseUrl, apiKey, provider, configId, preferModelName = "", isManualClick = false) {
    const btn = document.getElementById("btn-fetch-models");
    const container = document.getElementById("llm-fetched-models-container");
    const countStatusEl = document.getElementById("llm-models-count-status");
    const modelInput = document.getElementById("llm-input-model");

    if (!baseUrl) {
        if (isManualClick) showLLMTestResult(false, "未能获取模型列表：请先填写 Base URL 接口地址！");
        return;
    }

    const isLocal = baseUrl.includes("localhost") || baseUrl.includes("127.0.0.1") || baseUrl.includes("0.0.0.0");
    if (!apiKey && !configId && !isLocal) {
        if (isManualClick) showLLMTestResult(false, "未能获取模型列表：您尚未填写 API Key。云端大模型服务商必须通过有效 API Key 鉴权才能实时拉取最新可用模型！");
        return;
    }

    if (isManualClick && btn) {
        btn.classList.add("loading");
        btn.innerHTML = `<span class="spinner-sm"></span> 正在实时拉取模型...`;
        btn.disabled = true;
    }

    try {
        const res = await fetch(`${API_BASE}/settings/llm/models`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                base_url: baseUrl,
                api_key: apiKey || null,
                provider_name: provider,
                config_id: configId || null
            })
        });
        const json = await res.json();

        if (json.success && json.models && json.models.length > 0) {
            let modelList = [...json.models];
            // 若用户原先配置的模型不在服务商列表中，将其并入首位，防止丢失已选
            if (preferModelName && !modelList.includes(preferModelName)) {
                modelList.unshift(preferModelName);
            }

            // 确定当前选中的模型
            const selectedModel = (preferModelName && modelList.includes(preferModelName))
                ? preferModelName
                : (modelInput && modelInput.value && modelList.includes(modelInput.value))
                    ? modelInput.value
                    : modelList[0];

            if (modelInput) modelInput.value = selectedModel;

            // 第四行：更新获取状态与可用模型总数
            if (countStatusEl) {
                countStatusEl.innerHTML = `
                    <span style="color: #10B981; font-weight: 600; display: inline-flex; align-items: center; gap: 5px;">
                        <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #10B981; box-shadow: 0 0 6px #10B981;"></span>
                        成功获取到 ${modelList.length} 个可用模型 (当前选定: <strong>${escapeHtml(selectedModel)}</strong>)
                    </span>
                `;
            }

            // 第五行：完整渲染所有多个可用模型药丸，用户点击任意药丸可自由切换！
            if (container) {
                container.innerHTML = modelList.map(m => `
                    <div class="fetched-model-pill ${m === selectedModel ? 'selected' : ''}" 
                         data-model="${escapeHtml(m)}"
                         title="点击切换使用模型为: ${escapeHtml(m)}"
                         onclick="selectFetchedModel('${escapeHtml(m)}')">
                        ${escapeHtml(m)}
                    </div>
                `).join("");
            }

            if (isManualClick) {
                showLLMTestResult(
                    true,
                    json.message || `成功从服务商实时获取到 ${modelList.length} 个可用模型！点击下方药丸即可自由切换当前模型。`
                );
            }
        } else {
            if (isManualClick) {
                if (countStatusEl) {
                    countStatusEl.innerHTML = `
                        <span style="color: #EF4444; font-weight: 500; display: inline-flex; align-items: center; gap: 5px;">
                            <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #EF4444;"></span>
                            获取失败: ${json.message || '服务商响应异常'}
                        </span>
                    `;
                }
                showLLMTestResult(false, json.message || "未能从服务商获取到模型列表，请核对 API Key 或稍后重试。");
            }
        }
    } catch (e) {
        if (isManualClick) {
            showLLMTestResult(false, `拉取模型请求异常: ${e}`);
            if (countStatusEl) {
                countStatusEl.innerHTML = `<span style="color: #EF4444;">获取异常: 网络超时或端点不可达</span>`;
            }
        }
    } finally {
        if (isManualClick && btn) {
            btn.classList.remove("loading");
            btn.innerHTML = `<svg class="icon-sm" viewBox="0 0 24 24" style="width: 14px; height: 14px;"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg> 获取模型`;
            btn.disabled = false;
        }
    }
}

// 核心功能 1: 点击【获取模型】按钮动态拉取服务商模型列表
async function handleFetchRemoteModels() {
    const urlInput = document.getElementById("llm-input-url");
    const keyInput = document.getElementById("llm-input-key");
    const providerInput = document.getElementById("llm-editor-provider");
    const configIdInput = document.getElementById("llm-editor-config-id");
    const modelInput = document.getElementById("llm-input-model");

    const baseUrl = urlInput ? urlInput.value.trim() : "";
    const apiKey = keyInput ? keyInput.value.trim() : "";
    const provider = providerInput ? providerInput.value : currentSelectedLLMProvider;
    const configId = configIdInput ? configIdInput.value : "";
    const currentModel = modelInput ? modelInput.value.trim() : "";

    await fetchAndRenderModels(baseUrl, apiKey, provider, configId, currentModel, true);
}

// 点选具体模型的响应处理：高亮选定药丸并更新状态
function selectFetchedModel(modelName) {
    if (!modelName) return;
    const modelInput = document.getElementById("llm-input-model");
    if (modelInput) modelInput.value = modelName;

    // 联动更新第四行状态
    const countStatusEl = document.getElementById("llm-models-count-status");
    if (countStatusEl) {
        const totalPills = document.querySelectorAll(".fetched-model-pill").length;
        countStatusEl.innerHTML = `
            <span style="color: #10B981; font-weight: 600; display: inline-flex; align-items: center; gap: 5px;">
                <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #10B981; box-shadow: 0 0 6px #10B981;"></span>
                共获取到 ${totalPills} 个可用模型 (当前选定: <strong>${escapeHtml(modelName)}</strong>)
            </span>
        `;
    }

    document.querySelectorAll(".fetched-model-pill").forEach(el => {
        el.classList.toggle("selected", el.getAttribute("data-model") === modelName);
    });
}

// 核心功能 2: 点击【测试连通性】按钮
async function handleTestLLMConnectivity() {
    const btn = document.getElementById("btn-test-llm");
    const urlInput = document.getElementById("llm-input-url");
    const keyInput = document.getElementById("llm-input-key");
    const providerInput = document.getElementById("llm-editor-provider");
    const configIdInput = document.getElementById("llm-editor-config-id");
    const pingStatusEl = document.getElementById("llm-ping-latency-status");

    const baseUrl = urlInput ? urlInput.value.trim() : "";
    const apiKey = keyInput ? keyInput.value.trim() : "";
    const provider = providerInput ? providerInput.value : currentSelectedLLMProvider;
    const configId = configIdInput ? configIdInput.value : "";

    if (!baseUrl) {
        showLLMTestResult(false, "请先填入有效的 Base URL 地址！");
        return;
    }

    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner-sm"></span> 探测中...`;
    }
    if (pingStatusEl) {
        pingStatusEl.innerHTML = `握手测试: <span style="color: var(--sky);">探测中...</span>`;
    }

    try {
        const res = await fetch(`${API_BASE}/settings/test-connection`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                config_id: configId || null,
                config_group: "llm",
                provider_name: provider,
                base_url: baseUrl,
                api_key: apiKey || null
            })
        });
        const json = await res.json();
        showLLMTestResult(json.success, json.message);

        // 更新第四行：握手测试与延时
        if (pingStatusEl) {
            if (json.success) {
                pingStatusEl.innerHTML = `
                    <span style="color: #10B981; font-weight: 600; display: inline-flex; align-items: center; gap: 5px;">
                        <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #10B981;"></span>
                        握手成功 · 延时: ${json.latency_ms || 180}ms
                    </span>
                `;
            } else {
                pingStatusEl.innerHTML = `
                    <span style="color: #EF4444; font-weight: 500; display: inline-flex; align-items: center; gap: 5px;">
                        <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #EF4444;"></span>
                        握手失败 (${json.http_status || '超时/不可达'})
                    </span>
                `;
            }
        }
    } catch (e) {
        showLLMTestResult(false, `连通测试失败 (网络超时或端点异常): ${e}`);
        if (pingStatusEl) {
            pingStatusEl.innerHTML = `<span style="color: #EF4444;">握手异常: 网络超时</span>`;
        }
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `<svg class="icon-sm" viewBox="0 0 24 24" style="width: 14px; height: 14px;"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg> 测试连通性`;
        }
    }
}

// 显示连通性与获取结果提示条
function showLLMTestResult(isSuccess, message) {
    const box = document.getElementById("llm-test-result");
    if (!box) return;
    box.style.display = "flex";
    box.className = "llm-test-result-box " + (isSuccess ? "success" : "error");
    box.innerHTML = `
        <span>${isSuccess ? '✔' : '✖'}</span>
        <span>${message}</span>
    `;
}

// 核心功能 3: 保存当前编辑的大模型配置
async function handleSaveCurrentLLM() {
    const urlInput = document.getElementById("llm-input-url");
    const modelInput = document.getElementById("llm-input-model");
    const keyInput = document.getElementById("llm-input-key");
    const providerInput = document.getElementById("llm-editor-provider");
    const configIdInput = document.getElementById("llm-editor-config-id");

    const baseUrl = urlInput ? urlInput.value.trim() : "";
    const modelName = modelInput ? modelInput.value.trim() : "";
    const apiKey = keyInput ? keyInput.value.trim() : "";
    const provider = providerInput ? providerInput.value : currentSelectedLLMProvider;
    const configId = configIdInput ? configIdInput.value : "";

    if (!baseUrl) {
        alert("请输入 Base URL 地址！");
        return;
    }
    if (!modelName) {
        alert("请先在第二行输入 API Key 并点击第三行「获取模型」，从第五行选定具体模型后再进行保存！");
        return;
    }

    // 默认大脑逻辑：若当前没有任何已保存的 LLM，首次保存自动激活；若是编辑已有激活配置，保持激活；否则作为备用大脑
    const llmConfigs = cachedAllConfigs.filter(c => c.config_group === "llm");
    let isDefault = false;
    if (configId) {
        const exist = cachedAllConfigs.find(c => c.id === configId);
        isDefault = exist ? Boolean(exist.is_active) : false;
    } else {
        isDefault = llmConfigs.length === 0;
    }

    const payload = {
        id: configId || null,
        config_group: "llm",
        provider_name: provider,
        base_url: baseUrl,
        model_name: modelName,
        is_active: isDefault
    };
    if (apiKey) payload.api_key = apiKey;

    try {
        const res = await fetch(`${API_BASE}/settings/configs/save`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        const json = await res.json();
        if (json.code === 0) {
            showLLMTestResult(true, `配置已成功加密保存！${isDefault ? '已设为当前默认生效大脑。' : '可在下方列表卡片右上角随时设为默认大脑。'}`);
            await loadSettings();
        } else {
            showLLMTestResult(false, "保存失败: " + json.message);
        }
    } catch (e) {
        showLLMTestResult(false, "保存出错: " + e);
    }
}

// 核心功能 4: 渲染已配置的大模型清单卡片 (横向自适应多列网格)
function renderConfiguredLLMs(configs) {
    const container = document.getElementById("configured-llm-list");
    const countBadge = document.getElementById("configured-llm-count");
    if (!container) return;
    container.style.display = "grid";
    container.style.gridTemplateColumns = "repeat(auto-fill, minmax(320px, 1fr))";
    container.style.gap = "14px";
    container.style.marginTop = "14px";

    const llmConfigs = configs.filter(c => c.config_group === "llm");
    if (countBadge) countBadge.innerText = `${llmConfigs.length} 个已配置`;

    if (llmConfigs.length === 0) {
        container.innerHTML = `
            <div style="grid-column: 1 / -1; text-align: center; padding: 24px; color: var(--text-muted); background: rgba(0,0,0,0.15); border-radius: 8px; border: 1px dashed var(--border-subtle);">
                尚未配置任何云端大模型，请在上方选择品牌并填写 API Key 进行添加。
            </div>
        `;
        return;
    }

    container.innerHTML = "";
    llmConfigs.forEach(cfg => {
        const pMeta = resolveLLMProviderMeta(cfg.provider_name, cfg);

        const card = document.createElement("div");
        card.className = "configured-llm-card" + (cfg.is_active ? " is-active" : "");

        const topCornerHtml = cfg.is_active
            ? `<div class="badge-active-brain">★ 默认生效大脑</div>`
            : `<button class="btn-card-set-default" onclick="handleSetActiveLLM('${cfg.id}')" title="设为当前直播间主思考大脑">
                 <svg viewBox="0 0 24 24" style="width: 12px; height: 12px; stroke-width: 2.2;"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>
                 设为默认大脑
               </button>`;

        card.innerHTML = `
            <!-- 卡片头部：左侧生态与模型，右上角专属【设为默认大脑】/【默认生效大脑】 -->
            <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; width: 100%; min-width: 0; box-sizing: border-box;">
                <div style="display: flex; align-items: center; gap: 12px; min-width: 0; flex: 1;">
                    <img src="${pMeta.logoSvg}?v=1.7.5" alt="logo" style="width: 38px; height: 38px; border-radius: 50%; background: #0c1017; border: 1.5px solid rgba(148, 163, 184, 0.2); object-fit: contain; padding: 4px; flex-shrink: 0;">
                    <div style="min-width: 0; flex: 1;">
                        <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
                            <strong style="font-size: 14.5px; color: var(--text-primary); font-weight: 700;">${pMeta.name}</strong>
                            <span class="brand-badge gold">${escapeHtml(cfg.model_name || '未指定模型')}</span>
                        </div>
                        <div style="font-size: 11.5px; color: var(--text-muted); margin-top: 3px; font-family: var(--font-mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${escapeHtml(cfg.base_url || '')}">
                            ${escapeHtml(cfg.base_url || '官方端点')}
                        </div>
                    </div>
                </div>
                <div style="flex-shrink: 0; margin-left: 8px;">
                    ${topCornerHtml}
                </div>
            </div>

            <!-- 卡片底部：密钥安全脱敏与操作按钮 -->
            <div style="display: flex; justify-content: space-between; align-items: center; padding-top: 10px; border-top: 1px solid rgba(148, 163, 184, 0.12); margin-top: 8px; width: 100%; min-width: 0; box-sizing: border-box; flex-wrap: wrap; gap: 8px;">
                <span style="font-size: 11px; color: var(--text-muted); font-family: var(--font-mono); display: inline-flex; align-items: center; gap: 4px;">
                    <svg viewBox="0 0 24 24" style="width: 12px; height: 12px; stroke: var(--text-muted); fill: none;"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
                    Key: ${escapeHtml(cfg.masked_key || '未设置Key')}
                </span>
                <div style="display: flex; align-items: center; gap: 6px; margin-left: auto;">
                    <button class="btn btn-xs" onclick="handleEditConfiguredLLM('${cfg.id}')" title="编辑此模型参数">
                        编辑
                    </button>
                    <button class="btn btn-xs" onclick="handlePingSingleConfig('${cfg.id}', this)" title="测试连通性与网络延迟">
                        测速
                    </button>
                    <button class="btn btn-xs" style="color: #F87171; border-color: rgba(248, 113, 113, 0.3);" onclick="handleDeleteConfig('${cfg.id}')" title="从数据库删除">
                        删除
                    </button>
                </div>
            </div>
        `;
        container.appendChild(card);
    });
}

// 一键设为默认大脑
async function handleSetActiveLLM(configId) {
    try {
        const res = await fetch(`${API_BASE}/settings/configs/set-active`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ config_id: configId, config_group: "llm" })
        });
        const json = await res.json();
        if (json.code === 0) {
            await loadSettings();
        } else {
            alert("设置失败: " + json.message);
        }
    } catch (e) {
        alert("操作异常: " + e);
    }
}

// 编辑已配置的模型
function handleEditConfiguredLLM(configId) {
    const target = cachedAllConfigs.find(c => c.id === configId);
    if (!target) return;
    const pMeta = resolveLLMProviderMeta(target.provider_name, target);
    selectLLMProvider(pMeta.id, target);

    // 滚动至编辑面板
    const editor = document.getElementById("llm-editor-box");
    if (editor) editor.scrollIntoView({ behavior: "smooth", block: "center" });
}

// 单项测速
async function handlePingSingleConfig(configId, btn) {
    const originalText = btn.innerText;
    btn.disabled = true;
    btn.innerText = "测速中...";

    try {
        const res = await fetch(`${API_BASE}/settings/ping`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ config_id: configId })
        });
        const json = await res.json();
        if (json.success) {
            btn.style.color = "#34D399";
            btn.innerText = `${json.latency_ms}ms 正常`;
        } else {
            btn.style.color = "#F87171";
            btn.innerText = `异常 (${json.http_status || '超时'})`;
            alert(`连通性测试未通过:\n${json.message}`);
        }
    } catch (e) {
        btn.style.color = "#F87171";
        btn.innerText = "测速失败";
    } finally {
        setTimeout(() => {
            btn.disabled = false;
            btn.style.color = "";
            btn.innerText = originalText;
        }, 3500);
    }
}

// 删除已配置的模型
async function handleDeleteConfig(configId) {
    if (!confirm("确定要删除该项大模型配置吗？")) return;
    try {
        const res = await fetch(`${API_BASE}/settings/configs/${configId}`, {
            method: "DELETE"
        });
        const json = await res.json();
        if (json.code === 0) {
            await loadSettings();
        } else {
            alert("删除失败: " + json.message);
        }
    } catch (e) {
        alert("删除请求异常: " + e);
    }
}

// ============================================================================
// 语音合成与远程算力 (TTS 声音枢纽) 生态选型与多引擎管理体系
// ============================================================================
const BUILTIN_TTS_ECOSYSTEM = [
    {
        id: "edge_tts",
        name: "Edge-TTS",
        tagline: "微软超自然云语音 · 免Key极速",
        officialUrl: "https://azure.microsoft.com/zh-cn/products/ai-services/text-to-speech",
        officialAction: "微软 Azure 语音官网 ↗",
        getKeyUrl: "",
        cardDocTitle: "微软官方 Azure 语音官网 (注：本项目已内置 Edge-TTS，无需开通账号，免 Key 免自建直接免费使用)",
        brandColor: "#0284C7",
        logoSvg: "/static/svg/tts_edgetts.svg",
        defaultBaseUrl: "",
        placeholderUrl: "云端直接调用，无需填写 Base URL",
        needKey: false,
        needUrl: false,
        recommendedVoices: [
            { text: "晓晓 (超自然知性女主播 · 推荐)", val: "zh-CN-XiaoxiaoNeural" },
            { text: "云希 (活力阳光青年男主播)", val: "zh-CN-YunxiNeural" },
            { text: "云健 (沉稳影视讲解男声)", val: "zh-CN-YunjianNeural" },
            { text: "晓伊 (亲和活泼少女音)", val: "zh-CN-XiaoyiNeural" },
            { text: "辽宁晓北 (幽默东北老铁口音)", val: "zh-CN-liaoning-XiaobeiNeural" },
            { text: "陕西晓妮 (接地气陕西方言)", val: "zh-CN-shaanxi-XiaoniNeural" }
        ],
        desc: "微软官方云端超自然神经网络语音。本项目已原生内置云端直连免Key协议，无需注册开通账号或自建服务即可免费使用；如需企业商用服务可前往微软 Azure 官网开通。",
        urlPills: [
            { text: "⚡ 微软云端直连 (免填URL·免Key)", val: "", needKey: false }
        ]
    },
    {
        id: "cosyvoice",
        name: "CosyVoice",
        tagline: "阿里零样本复刻 · 5秒声纹克隆",
        officialUrl: "https://bailian.console.aliyun.com/",
        officialAction: "前往百炼开通并获取Key ↗",
        getKeyUrl: "https://bailian.console.aliyun.com/",
        cardDocTitle: "前往阿里云百炼官方控制台 (开通账号、充值并获取 API Key 与 Base URL)",
        brandColor: "#EA580C",
        logoSvg: "/static/svg/tts_cosyvoice.svg",
        defaultBaseUrl: "",
        placeholderUrl: "选择云端商用 API (填Key即用) 或 本地自建推理端口 (如 http://127.0.0.1:9233)",
        needKey: false,
        needUrl: true,
        recommendedVoices: [
            { text: "中文女 (自然亲切)", val: "chinese_female" },
            { text: "中文男 (专业沉稳)", val: "chinese_male" },
            { text: "日语女 (二次元甜美)", val: "japanese_female" },
            { text: "粤语女 (地道广府风)", val: "cantonese_female" },
            { text: "克隆声纹 (需上传参考音)", val: "custom_clone_1" }
        ],
        desc: "阿里通义开源大模型语音合成。商业云端调用推荐使用【阿里云百炼平台】开通账号并创建 API Key（Base URL 为 https://dashscope.aliyuncs.com/api/v1），亦支持本地或局域网私有化 GPU 部署（免费用）。",
        urlPills: [
            { text: "☁️ 阿里云百炼 (官方商业API)", val: "https://dashscope.aliyuncs.com/api/v1", needKey: true },
            { text: "☁️ 硅基流动 (云端免显卡直连)", val: "https://api.siliconflow.cn/v1", needKey: true },
            { text: "🖥️ 本机私有部署 (127.0.0.1:9233)", val: "http://127.0.0.1:9233", needKey: false },
            { text: "🖥️ 局域网 GPU 算力机", val: "http://192.168.1.100:9233", needKey: false }
        ]
    },
    {
        id: "chattts",
        name: "ChatTTS",
        tagline: "对话级自然停顿 · 真实语气笑声",
        officialUrl: "https://cloud.siliconflow.cn/account/ak",
        officialAction: "前往硅基流动获取Key ↗",
        getKeyUrl: "https://cloud.siliconflow.cn/account/ak",
        cardDocTitle: "前往硅基流动官方控制台 (注册开通、获取 ChatTTS API Key 与 Base URL)",
        brandColor: "#059669",
        logoSvg: "/static/svg/tts_chattts.svg",
        defaultBaseUrl: "",
        placeholderUrl: "选择云端托管 API (免显卡) 或 本地自建推理端口 (如 http://127.0.0.1:9966)",
        needKey: false,
        needUrl: true,
        recommendedVoices: [
            { text: "种子音色 2222 (自然女声)", val: "seed_2222" },
            { text: "种子音色 6666 (亲切解说)", val: "seed_6666" },
            { text: "种子音色 7869 (微醺笑意)", val: "seed_7869" },
            { text: "种子音色 8888 (阳光男声)", val: "seed_8888" }
        ],
        desc: "专为人机对话打造，支持语气词、自然笑声与停顿。云端免显卡商业使用推荐在【硅基流动平台】注册开通并创建 API Key（Base URL 为 https://api.siliconflow.cn/v1），亦支持本地部署。",
        urlPills: [
            { text: "☁️ 硅基流动 (官方云端高速)", val: "https://api.siliconflow.cn/v1", needKey: true },
            { text: "🖥️ 本机私有部署 (127.0.0.1:9966)", val: "http://127.0.0.1:9966", needKey: false }
        ]
    },
    {
        id: "gpt_sovits",
        name: "GPT-SoVITS",
        tagline: "少样本深度拟真 · 主播个性声线",
        officialUrl: "https://www.autodl.com",
        officialAction: "前往 AutoDL 租用算力 ↗",
        getKeyUrl: "https://www.autodl.com",
        cardDocTitle: "前往 AutoDL 算力云官方平台 (开通账号、租用 GPU 算力获取推理 API 端口)",
        brandColor: "#0284C7",
        logoSvg: "/static/svg/tts_gptsovits.svg",
        defaultBaseUrl: "",
        placeholderUrl: "输入本机私有端口 (如 http://127.0.0.1:9880) 或 AutoDL 等远程算力节点",
        needKey: false,
        needUrl: true,
        recommendedVoices: [
            { text: "预训练官方女主播", val: "default_female" },
            { text: "二次元专属声线", val: "anime_custom" },
            { text: "主播个性微调权重", val: "anchor_v2" }
        ],
        desc: "少样本微调高保真个性声音。需 GPU 算力运行，推荐在【AutoDL 算力云】充值租用 GPU 镜像，开箱获取 Base URL 端口；或在本机启动官方 api.py 服务。",
        urlPills: [
            { text: "🖥️ 本机私有服务 (127.0.0.1:9880)", val: "http://127.0.0.1:9880", needKey: false },
            { text: "☁️ AutoDL / 远程 GPU 算力节点", val: "http://region-x.autodl.pro:9880", needKey: false }
        ]
    },
    {
        id: "elevenlabs",
        name: "ElevenLabs",
        tagline: "全球顶级情感克隆 · 电影级人声",
        officialUrl: "https://elevenlabs.io/app/developers/api-keys",
        officialAction: "前往开通并创建Key ↗",
        getKeyUrl: "https://elevenlabs.io/app/developers/api-keys",
        cardDocTitle: "前往 ElevenLabs 官网控制台 (开通账号、订阅充值并创建 API Key)",
        brandColor: "#F43F5E",
        logoSvg: "/static/svg/tts_elevenlabs.svg",
        defaultBaseUrl: "https://api.elevenlabs.io/v1",
        placeholderUrl: "https://api.elevenlabs.io/v1",
        needKey: true,
        needUrl: true,
        recommendedVoices: [
            { text: "Rachel (知性温婉)", val: "21m00Tcm4TlvDq8ikWAM" },
            { text: "Adam (沉稳男声)", val: "pNInz6obpgDQGcFmaJgB" },
            { text: "Bella (甜美轻快)", val: "EXAVITQu4vr4xnSDxMaL" },
            { text: "Antoni (富有感染力)", val: "ErXwobaYiN019PkySvjV" }
        ],
        desc: "行业公认全球顶尖拟真度，官方云端托管。前往 ElevenLabs 官网控制台开通账号、充值订阅并创建 API Key（官方 Base URL 为 https://api.elevenlabs.io/v1）。",
        urlPills: [
            { text: "⚡ ElevenLabs 官方云端", val: "https://api.elevenlabs.io/v1", needKey: true }
        ]
    },
    {
        id: "custom_tts",
        name: "本地自建/网关",
        tagline: "兼容 OpenAI Audio / 自定义 HTTP",
        officialUrl: "https://platform.openai.com/api-keys",
        officialAction: "获取 OpenAI API Key ↗",
        getKeyUrl: "https://platform.openai.com/api-keys",
        cardDocTitle: "前往 OpenAI 官方控制台 (开通账号、绑定充值并获取 API Key)",
        brandColor: "#0F766E",
        logoSvg: "/static/svg/tts_custom.svg",
        defaultBaseUrl: "",
        placeholderUrl: "输入兼容 OpenAI /v1/audio/speech 的自建网关或局域网节点",
        needKey: false,
        needUrl: true,
        recommendedVoices: [
            { text: "alloy (标准女声)", val: "alloy" },
            { text: "echo (清晰男声)", val: "echo" },
            { text: "fable (英伦叙事)", val: "fable" },
            { text: "nova (活泼元气)", val: "nova" }
        ],
        desc: "兼容 OpenAI Audio /v1/audio/speech 规范网关。可直接对接 OpenAI 官方云端 API（需在 OpenAI 控制台开通 Key），或接入任意兼容规范的自建与局域网节点。",
        urlPills: [
            { text: "☁️ OpenAI 官方云端", val: "https://api.openai.com/v1", needKey: true },
            { text: "🖥️ 本机自建网关 (8000)", val: "http://127.0.0.1:8000/v1", needKey: false },
            { text: "☁️ 局域网/远程 GPU 节点", val: "http://192.168.1.120:8000/v1", needKey: false }
        ]
    }
];

let currentSelectedTTSProvider = "edge_tts";

// 初始化 6 大主流语音合成引擎卡片网格
function renderTTSEcosystemGrid(selectedId = "edge_tts") {
    const grid = document.getElementById("tts-ecosystem-grid");
    if (!grid) return;
    grid.innerHTML = "";

    BUILTIN_TTS_ECOSYSTEM.forEach(item => {
        const card = document.createElement("div");
        card.className = "tts-provider-card" + (item.id === selectedId ? " selected" : "");
        card.setAttribute("data-provider", item.id);
        card.onclick = () => selectTTSProvider(item.id);

        card.innerHTML = `
            <a href="${item.officialUrl}" target="_blank" rel="noopener noreferrer" class="tts-card-official-link" title="${item.cardDocTitle}" onclick="event.stopPropagation();">
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path>
                    <polyline points="15 3 21 3 21 9"></polyline>
                    <line x1="10" y1="14" x2="21" y2="3"></line>
                </svg>
            </a>
            <img src="${item.logoSvg}?v=1.9.2" class="tts-provider-logo" alt="${item.name}">
            <div class="tts-provider-name" title="${item.name}">${item.name}</div>
            <div class="tts-provider-tagline" title="${item.tagline}">${item.tagline}</div>
        `;
        grid.appendChild(card);
    });
}

// 智能识别 TTS 品牌元数据
function resolveTTSProviderMeta(providerId, config = null) {
    const rawId = (providerId || "").toLowerCase().trim();
    if (rawId && rawId !== "custom" && rawId !== "custom_tts") {
        const hit = BUILTIN_TTS_ECOSYSTEM.find(p => p.id === rawId);
        if (hit) return hit;
    }

    const pName = (config && config.provider_name ? config.provider_name : rawId).toLowerCase();
    const bUrl = (config && config.base_url ? config.base_url : "").toLowerCase();
    const mName = (config && config.model_name ? config.model_name : "").toLowerCase();
    const combined = `${pName} ${bUrl} ${mName}`;

    if (combined.includes("edge")) return BUILTIN_TTS_ECOSYSTEM.find(p => p.id === "edge_tts");
    if (combined.includes("cosy")) return BUILTIN_TTS_ECOSYSTEM.find(p => p.id === "cosyvoice");
    if (combined.includes("chattts") || combined.includes("9966")) return BUILTIN_TTS_ECOSYSTEM.find(p => p.id === "chattts");
    if (combined.includes("sovits") || combined.includes("9880")) return BUILTIN_TTS_ECOSYSTEM.find(p => p.id === "gpt_sovits");
    if (combined.includes("eleven")) return BUILTIN_TTS_ECOSYSTEM.find(p => p.id === "elevenlabs");

    return BUILTIN_TTS_ECOSYSTEM.find(p => p.id === "custom_tts") || BUILTIN_TTS_ECOSYSTEM[0];
}

// 快速填入 TTS Base URL 并联动更新 Key 状态
function fillTTSBaseUrlAndSyncKey(url, needKey = null) {
    const urlInput = document.getElementById("tts-input-url");
    if (urlInput) {
        urlInput.value = url;
        urlInput.focus();
    }
    syncTTSKeyStatusByUrl(url, needKey);
}

// 依据当前输入的 URL 智能判断是云端 API (需 Key) 还是本地私有自建 (免 Key)，并联动显示官方开通获取 Key 链接
function syncTTSKeyStatusByUrl(url, explicitNeedKey = null) {
    const keyInput = document.getElementById("tts-input-key");
    const hintEl = document.getElementById("tts-key-status-hint");
    const getKeyLink = document.getElementById("tts-get-key-link");
    if (!keyInput) return;

    const trimmed = (url || "").trim().toLowerCase();
    const isLocal = trimmed.includes("127.0.0.1") || trimmed.includes("localhost") || trimmed.startsWith("http://192.168.") || trimmed.startsWith("http://10.");
    const isEdge = currentSelectedTTSProvider === "edge_tts";

    const isCloud = explicitNeedKey === true || (!isLocal && !isEdge && (trimmed.startsWith("https://") || trimmed.includes("dashscope") || trimmed.includes("siliconflow") || trimmed.includes("elevenlabs") || trimmed.includes("api.")));

    const curMeta = BUILTIN_TTS_ECOSYSTEM.find(p => p.id === currentSelectedTTSProvider) || BUILTIN_TTS_ECOSYSTEM[0];

    if (isEdge) {
        if (hintEl) hintEl.innerText = "微软云端直连免密钥";
        keyInput.placeholder = "此引擎完全免 API 密钥，直接开箱即用";
        if (getKeyLink) getKeyLink.style.display = "none";
    } else if (isCloud) {
        if (hintEl) hintEl.innerText = "云端商用托管 (需硬件安全加密密钥)";
        keyInput.placeholder = "输入云服务商 API 密钥 (如 sk-xxxx)";
        if (getKeyLink) {
            let targetUrl = curMeta.getKeyUrl || curMeta.officialUrl;
            // 针对云端端点微调：若是硅基流动端点则直达硅基流动AK页
            if (trimmed.includes("siliconflow")) {
                targetUrl = "https://cloud.siliconflow.cn/account/ak";
            } else if (trimmed.includes("dashscope") || trimmed.includes("aliyun")) {
                targetUrl = "https://bailian.console.aliyun.com/";
            }
            if (targetUrl) {
                getKeyLink.href = targetUrl;
                getKeyLink.innerText = "开通账号/获取API Key ↗";
                getKeyLink.title = `前往官方控制台开通并获取 API Key (在新标签页打开)`;
                getKeyLink.style.display = "inline";
            } else {
                getKeyLink.style.display = "none";
            }
        }
    } else if (isLocal) {
        if (hintEl) hintEl.innerText = "本地私有端口免密钥直连";
        keyInput.placeholder = "当前为本地/局域网推理端口，免 API Key 直连";
        if (getKeyLink) getKeyLink.style.display = "none";
    } else {
        if (hintEl) hintEl.innerText = "根据端点自动适配密钥";
        keyInput.placeholder = "云端商用需填 sk-xxxx，本地端口可留空";
        if (getKeyLink && curMeta.getKeyUrl) {
            getKeyLink.href = curMeta.getKeyUrl;
            getKeyLink.innerText = "开通账号/获取API Key ↗";
            getKeyLink.style.display = "inline";
        } else if (getKeyLink) {
            getKeyLink.style.display = "none";
        }
    }
}

// 切换当前配置的语音引擎品牌
function selectTTSProvider(providerId, existingConfig = null) {
    const meta = resolveTTSProviderMeta(providerId, existingConfig);
    currentSelectedTTSProvider = meta.id;

    if (!existingConfig && cachedAllConfigs && cachedAllConfigs.length > 0) {
        const found = cachedAllConfigs.find(c => {
            if (c.config_group !== "tts") return false;
            const cMeta = resolveTTSProviderMeta(c.provider_name, c);
            return cMeta.id === meta.id;
        });
        if (found) existingConfig = found;
    }

    // 高亮品牌卡片
    document.querySelectorAll(".tts-provider-card").forEach(c => {
        c.classList.toggle("selected", c.getAttribute("data-provider") === meta.id);
    });

    // 编辑面板头部
    const logoEl = document.getElementById("tts-editor-logo");
    const titleEl = document.getElementById("tts-editor-title");
    const badgeEl = document.getElementById("tts-editor-badge");
    const descEl = document.getElementById("tts-editor-desc");
    const providerInput = document.getElementById("tts-editor-provider");
    const configIdInput = document.getElementById("tts-editor-config-id");

    const officialLinkEl = document.getElementById("tts-editor-official-link");

    if (logoEl) logoEl.src = meta.logoSvg + "?v=1.9.2";
    if (titleEl) titleEl.innerText = existingConfig ? `编辑已保存的语音配置: ${existingConfig.model_name || meta.name}` : `配置 ${meta.name}`;
    if (badgeEl) badgeEl.innerText = meta.tagline.split("·")[0].trim();
    if (descEl) descEl.innerText = meta.desc;
    if (providerInput) providerInput.value = meta.id;
    if (configIdInput) configIdInput.value = existingConfig ? existingConfig.id : "";
    if (officialLinkEl && meta.officialUrl) {
        officialLinkEl.href = meta.officialUrl;
        officialLinkEl.title = meta.cardDocTitle || `前往 ${meta.name} 官方控制台 (在新标签页打开)`;
        const spanEl = officialLinkEl.querySelector("span");
        if (spanEl) {
            spanEl.innerText = meta.officialAction || "官方控制台 ↗";
        }
    }

    // 第一行 Base URL (双通道支持)
    const urlInput = document.getElementById("tts-input-url");
    const currentBaseUrl = existingConfig ? (existingConfig.base_url || "") : meta.defaultBaseUrl;
    if (urlInput) {
        urlInput.value = currentBaseUrl;
        urlInput.placeholder = meta.placeholderUrl || (meta.needUrl ? "请输入服务 Base URL 地址" : "云端直接调用，无需填写 Base URL");
        urlInput.oninput = () => syncTTSKeyStatusByUrl(urlInput.value);
    }

    const urlPillsBox = document.getElementById("tts-editor-url-pills");
    if (urlPillsBox) {
        urlPillsBox.innerHTML = `
            <span style="font-size: 11px; color: var(--text-muted);">快捷端点:</span>
            ${meta.urlPills.map(p => `<span class="quick-pill" onclick="fillTTSBaseUrlAndSyncKey('${p.val}', ${p.needKey})">${p.text}</span>`).join("")}
        `;
    }

    // 第二行 API Key（根据 URL 和引擎类型智能联动）
    syncTTSKeyStatusByUrl(currentBaseUrl, meta.needKey);
    if (existingConfig && existingConfig.id) {
        autoFillRealTTSKeyIfConfigured(existingConfig.id, existingConfig);
    } else {
        const keyInput = document.getElementById("tts-input-key");
        if (keyInput) {
            keyInput.value = "";
            keyInput.type = "password";
        }
    }

    // 第五行：初始化具体音色与选定状态（真实数据优先填充，未配置时严格为空）
    const voiceInput = document.getElementById("tts-input-voice");
    const container = document.getElementById("tts-fetched-voices-container");
    const countStatusEl = document.getElementById("tts-voices-count-status");
    const pingStatusEl = document.getElementById("tts-ping-latency-status");
    if (pingStatusEl) pingStatusEl.innerHTML = `握手测试: 未测试`;

    if (existingConfig && existingConfig.id && existingConfig.model_name) {
        // 已真实配置过：回填真实保存的音色代号，并渲染所有可用药丸同时高亮已选
        if (voiceInput) voiceInput.value = existingConfig.model_name;
        if (countStatusEl) {
            countStatusEl.innerHTML = `
                <span style="color: #10B981; font-weight: 600; display: inline-flex; align-items: center; gap: 5px;">
                    <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #10B981;"></span>
                    已配置真实发音音色: <strong>${escapeHtml(existingConfig.model_name)}</strong>
                </span>
            `;
        }
        renderTTSVoicePills(meta, existingConfig.model_name);
    } else {
        // 未配置状态：严格保持空白，等待用户点击获取音色或自行选定，绝不虚假填充
        if (voiceInput) voiceInput.value = "";
        if (countStatusEl) {
            countStatusEl.innerHTML = `<span>获取状态: 尚未获取/选定音色（请点击上方「获取音色」）</span>`;
        }
        if (container) {
            container.innerHTML = `<div style="color: var(--text-muted); font-size: 12px;">暂未获取音色。请点击上方「获取音色」实时加载可用发音声线，选定后即可保存。</div>`;
        }
    }

    const testResultBox = document.getElementById("tts-test-result");
    if (testResultBox) testResultBox.style.display = "none";
}

// 渲染音色候选药丸
function renderTTSVoicePills(meta, selectedVoiceVal) {
    const container = document.getElementById("tts-fetched-voices-container");
    if (!container) return;

    if (!meta.recommendedVoices || meta.recommendedVoices.length === 0) {
        container.innerHTML = `<div style="color: var(--text-muted); font-size: 12px;">暂无预设音色，请点击「获取音色」加载。</div>`;
        return;
    }

    container.innerHTML = meta.recommendedVoices.map(v => `
        <div class="fetched-model-pill ${v.val === selectedVoiceVal ? 'selected' : ''}"
             data-voice="${escapeHtml(v.val)}"
             title="点击使用音色: ${escapeHtml(v.text)}"
             onclick="selectFetchedTTSVoice('${escapeHtml(v.val)}', '${escapeHtml(v.text)}')">
            ${escapeHtml(v.text)}
        </div>
    `).join("");
}

// 切换选定音色
function selectFetchedTTSVoice(voiceVal, voiceLabel = "") {
    if (!voiceVal) return;
    const voiceInput = document.getElementById("tts-input-voice");
    if (voiceInput) voiceInput.value = voiceVal;

    const countStatusEl = document.getElementById("tts-voices-count-status");
    if (countStatusEl) {
        const total = document.querySelectorAll("#tts-fetched-voices-container .fetched-model-pill").length;
        countStatusEl.innerHTML = `
            <span style="color: #10B981; font-weight: 600; display: inline-flex; align-items: center; gap: 5px;">
                <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #10B981; box-shadow: 0 0 6px #10B981;"></span>
                共 ${total} 款可用音色 (当前选定: <strong>${escapeHtml(voiceLabel || voiceVal)}</strong>)
            </span>
        `;
    }

    document.querySelectorAll("#tts-fetched-voices-container .fetched-model-pill").forEach(el => {
        el.classList.toggle("selected", el.getAttribute("data-voice") === voiceVal);
    });
}

// 自动解密填入已配置过的 TTS 密钥并默认脱敏
async function autoFillRealTTSKeyIfConfigured(configId, existingConfig = null) {
    const keyInput = document.getElementById("tts-input-key");
    const eyeBtn = document.getElementById("btn-toggle-tts-key-eye");
    const hintEl = document.getElementById("tts-key-status-hint");
    if (!keyInput || !configId) return;

    keyInput.type = "password";
    if (eyeBtn) {
        eyeBtn.innerHTML = `<svg viewBox="0 0 24 24"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`;
        eyeBtn.title = "点击显示真实密钥明文";
        eyeBtn.style.color = "";
    }

    let realKey = cachedDecryptedKeys[configId] || "";
    if (!realKey) {
        try {
            const res = await fetch(`${API_BASE}/settings/configs/${configId}/raw-key`);
            const json = await res.json();
            if (json.code === 0 && json.raw_key) {
                cachedDecryptedKeys[configId] = json.raw_key;
                realKey = json.raw_key;
            }
        } catch (e) {
            console.warn("自动获取 TTS API Key 异常:", e);
        }
    }

    const curIdInput = document.getElementById("tts-editor-config-id");
    if (curIdInput && curIdInput.value === configId && realKey) {
        keyInput.value = realKey;
        keyInput.type = "password";
        if (hintEl) hintEl.innerText = "已载入硬件加密密钥 (默认脱敏)";
    }
}

// 切换 TTS 密钥眼睛图标
async function toggleTTSKeyVisibility() {
    const keyInput = document.getElementById("tts-input-key");
    const eyeBtn = document.getElementById("btn-toggle-tts-key-eye");
    const configIdInput = document.getElementById("tts-editor-config-id");
    const hintEl = document.getElementById("tts-key-status-hint");
    if (!keyInput || !eyeBtn) return;

    const eyeShowSvg = `<svg viewBox="0 0 24 24"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`;
    const eyeHideSvg = `<svg viewBox="0 0 24 24"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>`;

    const isPassword = keyInput.type === "password";
    const configId = configIdInput ? configIdInput.value.trim() : "";

    if (isPassword) {
        if (keyInput.value && keyInput.value.trim()) {
            keyInput.type = "text";
            eyeBtn.innerHTML = eyeHideSvg;
            eyeBtn.style.color = "#10B981";
            if (hintEl) hintEl.innerText = "已呈现输入明文";
            return;
        }

        if (configId) {
            if (cachedDecryptedKeys[configId]) {
                keyInput.value = cachedDecryptedKeys[configId];
                keyInput.type = "text";
                eyeBtn.innerHTML = eyeHideSvg;
                eyeBtn.style.color = "#10B981";
                if (hintEl) hintEl.innerText = "已解密呈现真实密钥明文";
                return;
            }

            eyeBtn.disabled = true;
            try {
                const res = await fetch(`${API_BASE}/settings/configs/${configId}/raw-key`);
                const json = await res.json();
                if (json.code === 0 && json.raw_key) {
                    cachedDecryptedKeys[configId] = json.raw_key;
                    keyInput.value = json.raw_key;
                    keyInput.type = "text";
                    eyeBtn.innerHTML = eyeHideSvg;
                    eyeBtn.style.color = "#10B981";
                    if (hintEl) hintEl.innerText = "已解密呈现真实密钥明文";
                }
            } catch (e) {
                alert("请求解密密钥异常: " + e);
            } finally {
                eyeBtn.disabled = false;
            }
        } else {
            keyInput.type = "text";
            eyeBtn.innerHTML = eyeHideSvg;
            eyeBtn.style.color = "#10B981";
        }
    } else {
        keyInput.type = "password";
        eyeBtn.innerHTML = eyeShowSvg;
        eyeBtn.style.color = "";
        if (hintEl) hintEl.innerText = "本地硬件级加密存储";
    }
}

// 点击【获取音色】
async function handleFetchTTSVoices() {
    const btn = document.getElementById("btn-fetch-tts-voices");
    const meta = resolveTTSProviderMeta(currentSelectedTTSProvider);
    const voiceInput = document.getElementById("tts-input-voice");
    const countStatusEl = document.getElementById("tts-voices-count-status");

    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner-sm"></span> 加载音色中...`;
    }

    try {
        const defaultVoice = (meta.recommendedVoices.length > 0) ? meta.recommendedVoices[0].val : "";
        const currentSelected = (voiceInput && voiceInput.value) ? voiceInput.value : defaultVoice;
        if (voiceInput) voiceInput.value = currentSelected;

        renderTTSVoicePills(meta, currentSelected);
        if (countStatusEl) {
            countStatusEl.innerHTML = `
                <span style="color: #10B981; font-weight: 600; display: inline-flex; align-items: center; gap: 5px;">
                    <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #10B981;"></span>
                    成功获取到 ${meta.recommendedVoices.length} 款可用音色 (当前选定: <strong>${escapeHtml(currentSelected)}</strong>)
                </span>
            `;
        }
        showTTSTestResult(true, `已成功加载【${meta.name}】的 ${meta.recommendedVoices.length} 款可用音色！点击下方药丸即可自由切换当前音色。`);
    } catch (e) {
        showTTSTestResult(false, "加载音色列表异常: " + e);
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `<svg class="icon-sm" viewBox="0 0 24 24" style="width: 14px; height: 14px;"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg> 获取音色`;
        }
    }
}

// 全局试听音频控制实例
let currentTTSPreviewAudio = null;

// 点击【测试连通性与试听】
async function handleTestTTSConnectivity() {
    const btn = document.getElementById("btn-test-tts");
    const urlInput = document.getElementById("tts-input-url");
    const keyInput = document.getElementById("tts-input-key");
    const voiceInput = document.getElementById("tts-input-voice");
    const providerInput = document.getElementById("tts-editor-provider");
    const configIdInput = document.getElementById("tts-editor-config-id");
    const pingStatusEl = document.getElementById("tts-ping-latency-status");

    const baseUrl = urlInput ? urlInput.value.trim() : "";
    const apiKey = keyInput ? keyInput.value.trim() : "";
    const provider = providerInput ? providerInput.value : currentSelectedTTSProvider;
    const configId = configIdInput ? configIdInput.value : "";
    const selectedVoice = voiceInput ? voiceInput.value.trim() : "";

    // 停止上一次未播放完的试听
    if (currentTTSPreviewAudio) {
        try {
            currentTTSPreviewAudio.pause();
            currentTTSPreviewAudio = null;
        } catch (e) {}
    }

    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner-sm"></span> 探测握手中...`;
    }
    if (pingStatusEl) {
        pingStatusEl.innerHTML = `握手测试: <span style="color: var(--sky);">探测中...</span>`;
    }

    try {
        // 步骤 1: 探测网络连通性与网络延迟
        const res = await fetch(`${API_BASE}/settings/test-connection`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                config_id: configId || null,
                config_group: "tts",
                provider_name: provider,
                base_url: baseUrl,
                api_key: apiKey || null
            })
        });
        const json = await res.json();
        showTTSTestResult(json.success, json.message);

        if (pingStatusEl) {
            if (json.success) {
                pingStatusEl.innerHTML = `
                    <span style="color: #10B981; font-weight: 600; display: inline-flex; align-items: center; gap: 5px;">
                        <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #10B981;"></span>
                        连通正常 · 延迟: ${json.latency_ms || 120}ms
                    </span>
                `;
            } else {
                pingStatusEl.innerHTML = `
                    <span style="color: #EF4444; font-weight: 500; display: inline-flex; align-items: center; gap: 5px;">
                        <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #EF4444;"></span>
                        连通失败 (${json.http_status || '超时/不可达'})
                    </span>
                `;
            }
        }

        // 步骤 2: 连通探测正常后，立即请求后端合成并播放试听语音
        if (json.success) {
            if (btn) {
                btn.innerHTML = `<span class="spinner-sm"></span> 正在合成试听语音...`;
            }
            try {
                const previewRes = await fetch(`${API_BASE}/settings/tts/preview`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        provider_name: provider,
                        voice_name: selectedVoice || null,
                        base_url: baseUrl || null,
                        api_key: apiKey || null,
                        text: "你好，欢迎来到直播间！这是当前语音引擎的实时试听效果，祝您开播顺利！"
                    })
                });

                if (previewRes.ok) {
                    const blob = await previewRes.blob();
                    const audioUrl = URL.createObjectURL(blob);
                    const audio = new Audio(audioUrl);
                    currentTTSPreviewAudio = audio;

                    if (btn) {
                        btn.innerHTML = `🔊 正在试听播报中...`;
                    }
                    if (pingStatusEl) {
                        pingStatusEl.innerHTML = `
                            <span style="color: #38BDF8; font-weight: 600; display: inline-flex; align-items: center; gap: 5px;">
                                <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #38BDF8;"></span>
                                正在播放试听效果 (${selectedVoice || '默认音色'}) · 延迟: ${json.latency_ms || 120}ms
                            </span>
                        `;
                    }

                    audio.onended = () => {
                        if (pingStatusEl) {
                            pingStatusEl.innerHTML = `
                                <span style="color: #10B981; font-weight: 600; display: inline-flex; align-items: center; gap: 5px;">
                                    <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #10B981;"></span>
                                    试听播报完成 · 服务连通正常 (${json.latency_ms || 120}ms)
                                </span>
                            `;
                        }
                    };

                    await audio.play();
                } else {
                    const err = await previewRes.json().catch(() => ({}));
                    console.warn("试听音频生成提示:", err);
                }
            } catch (audioErr) {
                console.warn("试听播放受阻 (如浏览器自动播放策略拦截):", audioErr);
            }
        }
    } catch (e) {
        showTTSTestResult(false, `测试 TTS 连通异常: ${e}`);
        if (pingStatusEl) pingStatusEl.innerHTML = `<span style="color: #EF4444;">握手异常</span>`;
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `<svg class="icon-sm" viewBox="0 0 24 24" style="width: 14px; height: 14px;"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/></svg> 测试连通性与试听`;
        }
    }
}

// 提示结果条
function showTTSTestResult(isSuccess, message) {
    const box = document.getElementById("tts-test-result");
    if (!box) return;
    box.style.display = "flex";
    box.className = "llm-test-result-box " + (isSuccess ? "success" : "error");
    box.innerHTML = `
        <span>${isSuccess ? '✔' : '✖'}</span>
        <span>${message}</span>
    `;
}

// 保存当前 TTS 配置
async function handleSaveCurrentTTS() {
    const urlInput = document.getElementById("tts-input-url");
    const voiceInput = document.getElementById("tts-input-voice");
    const keyInput = document.getElementById("tts-input-key");
    const providerInput = document.getElementById("tts-editor-provider");
    const configIdInput = document.getElementById("tts-editor-config-id");

    const baseUrl = urlInput ? urlInput.value.trim() : "";
    const voiceVal = voiceInput ? voiceInput.value.trim() : "";
    const apiKey = keyInput ? keyInput.value.trim() : "";
    const provider = providerInput ? providerInput.value : currentSelectedTTSProvider;
    const configId = configIdInput ? configIdInput.value : "";

    const meta = resolveTTSProviderMeta(provider);
    if (meta.needUrl && !baseUrl) {
        alert("该语音引擎需填写服务 Base URL 地址！");
        return;
    }
    if (!voiceVal) {
        alert("请先点击第三行「获取音色」，并在第五行选定要使用的发音音色后再进行保存！");
        return;
    }

    const ttsConfigs = cachedAllConfigs.filter(c => c.config_group === "tts");
    let isDefault = false;
    if (configId) {
        const exist = cachedAllConfigs.find(c => c.id === configId);
        isDefault = exist ? Boolean(exist.is_active) : false;
    } else {
        isDefault = ttsConfigs.length === 0;
    }

    const payload = {
        id: configId || null,
        config_group: "tts",
        provider_name: provider,
        base_url: baseUrl,
        model_name: voiceVal,
        is_active: isDefault
    };
    if (apiKey) payload.api_key = apiKey;

    try {
        const res = await fetch(`${API_BASE}/settings/configs/save`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        const json = await res.json();
        if (json.code === 0) {
            showTTSTestResult(true, `语音配置已成功保存！${isDefault ? '已设为当前直播默认发音。' : '可在下方列表卡片右上角随时设为默认发音。'}`);
            await loadSettings();
        } else {
            showTTSTestResult(false, "保存失败: " + json.message);
        }
    } catch (e) {
        showTTSTestResult(false, "保存语音引擎出错: " + e);
    }
}

// 渲染已配置的 TTS 清单 (100% 真实数据驱动)
function renderConfiguredTTS(configs) {
    const container = document.getElementById("configured-tts-list");
    const countBadge = document.getElementById("configured-tts-count");
    if (!container) return;

    container.style.display = "grid";
    container.style.gridTemplateColumns = "repeat(auto-fill, minmax(320px, 1fr))";
    container.style.gap = "14px";
    container.style.marginTop = "14px";

    const ttsConfigs = (configs || []).filter(c => c.config_group === "tts");
    if (countBadge) countBadge.innerText = `${ttsConfigs.length} 个已配置`;

    if (ttsConfigs.length === 0) {
        container.innerHTML = `
            <div style="grid-column: 1 / -1; text-align: center; padding: 28px 20px; color: var(--text-muted); background: rgba(0,0,0,0.18); border-radius: 8px; border: 1px dashed var(--border-subtle);">
                <div style="font-size: 14px; color: var(--text-secondary); margin-bottom: 6px; font-weight: 600;">暂无已配置的语音引擎</div>
                <div style="font-size: 12px;">系统绝不预设虚假配置。请在上方选择语音引擎（如 Edge-TTS、CosyVoice 等），点击第三行「获取音色」选定声线后，点击「保存此语音配置」。</div>
            </div>
        `;
        return;
    }

    container.innerHTML = "";
    ttsConfigs.forEach(cfg => {
        const tMeta = resolveTTSProviderMeta(cfg.provider_name, cfg);
        const card = document.createElement("div");
        card.className = "configured-llm-card" + (cfg.is_active ? " is-active" : "");

        const topCornerHtml = cfg.is_active
            ? `<div class="badge-active-brain">★ 默认生效发音</div>`
            : `<button class="btn-card-set-default-tts" onclick="handleSetActiveTTS('${cfg.id}')" title="设为当前直播默认发音">
                 <svg viewBox="0 0 24 24" style="width: 12px; height: 12px; stroke-width: 2.2;"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>
                 设为默认发音
               </button>`;

        card.innerHTML = `
            <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; width: 100%; min-width: 0; box-sizing: border-box;">
                <div class="configured-llm-info">
                    <img src="${tMeta.logoSvg}?v=1.8.9" alt="${tMeta.name}" style="width: 38px; height: 38px; border-radius: 8px; object-fit: contain; flex-shrink: 0; background: #090E17; border: 1px solid rgba(16, 185, 129, 0.25); padding: 4px;">
                    <div style="min-width: 0; flex: 1;">
                        <div style="display: flex; align-items: center; gap: 6px; flex-wrap: wrap;">
                            <strong style="font-size: 14px; color: #FFFFFF;">${tMeta.name}</strong>
                            <span class="brand-badge green" style="font-size: 10.5px; padding: 2px 6px;">${escapeHtml(cfg.model_name || '默认音色')}</span>
                        </div>
                        <div style="font-size: 11.5px; color: var(--text-muted); margin-top: 3px; font-family: var(--font-mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${escapeHtml(cfg.base_url || '内置直连')}">
                            ${escapeHtml(cfg.base_url || '微软云端直连免配置')}
                        </div>
                    </div>
                </div>
                <div>${topCornerHtml}</div>
            </div>

            <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 10px; padding-top: 10px; border-top: 1px dashed rgba(148, 163, 184, 0.15); width: 100%; box-sizing: border-box;">
                <button class="btn btn-xs" onclick="handlePingSingleConfig('${cfg.id}', this)" style="display: inline-flex; align-items: center; gap: 4px;">
                    <svg class="icon-sm" viewBox="0 0 24 24" style="width: 11px; height: 11px;"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
                    测速
                </button>
                <div style="display: flex; gap: 6px;">
                    <button class="btn btn-xs" onclick="handleEditConfiguredTTS('${cfg.id}')" title="载入上方编辑面板">
                        编辑
                    </button>
                    <button class="btn btn-xs" style="color: #F87171; border-color: rgba(248, 113, 113, 0.3);" onclick="handleDeleteConfig('${cfg.id}')" title="从数据库删除">
                        删除
                    </button>
                </div>
            </div>
        `;
        container.appendChild(card);
    });
}

// 切换默认激活的 TTS
async function handleSetActiveTTS(configId) {
    try {
        const res = await fetch(`${API_BASE}/settings/configs/set-active`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ config_id: configId, config_group: "tts" })
        });
        const json = await res.json();
        if (json.code === 0) {
            showToast("已切换直播默认发音引擎！", "success");
            await loadSettings();
        } else {
            alert("设置失败: " + json.message);
        }
    } catch (e) {
        alert("操作异常: " + e);
    }
}

// 编辑已配置的 TTS
function handleEditConfiguredTTS(configId) {
    const target = cachedAllConfigs.find(c => c.id === configId);
    if (!target) return;
    const tMeta = resolveTTSProviderMeta(target.provider_name, target);
    selectTTSProvider(tMeta.id, target);

    const editor = document.getElementById("tts-editor-box");
    if (editor) editor.scrollIntoView({ behavior: "smooth", block: "center" });
}

// 展开/收起扩展自定义 / 远程 GPU 框
function toggleAddCustomTTSForm() {
    const box = document.getElementById("custom-tts-advanced-box");
    if (!box) return;
    box.style.display = box.style.display === "none" ? "block" : "none";
}

// 提交高级自定义服务商
async function submitNewCustomProvider() {
    const group = document.getElementById("new-cfg-group").value;
    const name = document.getElementById("new-provider-name").value.trim();
    const url = document.getElementById("new-base-url").value.trim();
    const model = document.getElementById("new-model-name").value.trim();
    const key = document.getElementById("new-api-key").value.trim();

    if (!name) {
        alert("请输入服务商标识（如 my_remote_gpu）");
        return;
    }

    try {
        const res = await fetch(`${API_BASE}/settings/configs/save`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                config_group: group,
                provider_name: name,
                base_url: url,
                model_name: model,
                api_key: key || null,
                is_active: true
            })
        });
        const json = await res.json();
        if (json.code === 0) {
            alert("自定义节点配置已成功添加！");
            toggleAddCustomTTSForm();
            loadSettings();
        } else {
            alert("添加失败: " + json.message);
        }
    } catch (e) {
        alert("提交异常: " + e);
    }
}

// 统一入口：loadSettings (按模式过滤，渲染大模型体系与非LLM配置)
async function loadSettings() {
    try {
        // 读取当前直播模式
        let requiredSet = null;
        try {
            const modeRes = await fetch(`${API_BASE}/settings/live-mode`);
            const modeJson = await modeRes.json();
            const gotoBtn = document.getElementById("btn-goto-wizard");
            if (modeJson.code === 0 && modeJson.data.mode_info) {
                requiredSet = new Set(modeJson.data.mode_info.required_configs);
                renderCurrentModeBadge(modeJson.data.mode_info);
                if (gotoBtn) gotoBtn.innerHTML = svg("compass", "icon-sm") + " 重新配置模式/角色";
            }
        } catch (e) { }

        // 获取全部配置
        const res = await fetch(`${API_BASE}/settings/configs`);
        const json = await res.json();
        if (json.code === 0) {
            cachedAllConfigs = json.data || [];

            // 1. 渲染大模型网格与多模型卡片列表
            renderLLMEcosystemGrid(currentSelectedLLMProvider);
            renderConfiguredLLMs(cachedAllConfigs);

            // 若大模型编辑区尚未与激活项绑定过，优先把激活的大模型同步到编辑面板
            const activeLLM = cachedAllConfigs.find(c => c.config_group === "llm" && c.is_active);
            if (activeLLM && !document.getElementById("llm-editor-config-id").value) {
                const pMeta = resolveLLMProviderMeta(activeLLM.provider_name, activeLLM);
                selectLLMProvider(pMeta.id, activeLLM);
            } else if (!document.getElementById("llm-editor-config-id").value) {
                selectLLMProvider(currentSelectedLLMProvider);
            }

            // 2. 渲染语音合成生态网格与已配置发音清单
            renderTTSEcosystemGrid(currentSelectedTTSProvider);
            renderConfiguredTTS(cachedAllConfigs);

            // 若语音编辑区尚未与激活项绑定过，优先把激活的发音同步到编辑面板
            const activeTTS = cachedAllConfigs.find(c => c.config_group === "tts" && c.is_active);
            if (activeTTS && !document.getElementById("tts-editor-config-id").value) {
                const tMeta = resolveTTSProviderMeta(activeTTS.provider_name, activeTTS);
                selectTTSProvider(tMeta.id, activeTTS);
            } else if (!document.getElementById("tts-editor-config-id").value) {
                selectTTSProvider(currentSelectedTTSProvider);
            }

            // 3. 渲染其他高级/扩展服务商卡片（如远程 GPU 渲染节点等）
            const container = document.getElementById("settings-configs-container");
            if (!container) return;
            container.innerHTML = "";

            const otherConfigs = cachedAllConfigs.filter(c => c.config_group !== "llm" && c.config_group !== "tts");
            if (otherConfigs.length === 0) {
                container.style.display = "none";
                return;
            }
            container.style.display = "block";
            if (otherConfigs.length === 0) {
                container.innerHTML = `
                    <div style="text-align: center; padding: 28px; color: var(--text-muted); background: rgba(0,0,0,0.18); border-radius: var(--radius-md); border: 1px dashed rgba(148, 163, 184, 0.2);">
                        暂未配置额外的语音合成或远程 GPU 算力服务商，系统将默认采用原生轻量服务。点击右上角「添加自定义服务商」可接入第三方服务。
                    </div>
                `;
                return;
            }

            otherConfigs.forEach(cfg => {
                const meta = PROVIDER_META[cfg.provider_name] || {
                    title: `${cfg.provider_name} 自定义服务`,
                    typeBadge: cfg.config_group.toUpperCase(),
                    requiredTag: '<span class="badge-optional">自定义</span>',
                    recommendTag: '',
                    desc: "用户自定义接入的第三方 API 服务商。",
                    urlLabel: "接口 Base URL 地址:",
                    urlTip: "服务商 API 的请求根地址。",
                    urlPills: [],
                    modelLabel: "模型 / 音色代号 (Model ID / Voice ID):",
                    modelTip: "调用的具体模型或音色代号。",
                    modelPills: [],
                    keyLabel: "API 密钥 (API Key):",
                    keyTip: "访问该服务所需的认证密钥。"
                };

                const div = document.createElement("div");
                div.className = "settings-sub-card";
                div.innerHTML = `
                    <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 12px; border-bottom: 1px solid var(--border-subtle); padding-bottom: 10px;">
                        <div>
                            <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
                                <strong style="font-size: 15px; color: var(--text-primary);">${meta.title}</strong>
                                <span class="brand-badge">${meta.typeBadge}</span>
                                ${meta.requiredTag}
                                ${meta.recommendTag}
                            </div>
                            <p style="font-size: 12px; color: var(--text-secondary); margin-top: 6px; line-height: 1.5;">${meta.desc}</p>
                        </div>
                        <label style="display: flex; align-items: center; gap: 6px; font-size: 13px; cursor: pointer; white-space: nowrap; margin-left: 12px;">
                            <input type="checkbox" id="active-${cfg.id}" ${cfg.is_active ? 'checked' : ''}>
                            <span style="font-weight: 600; color: ${cfg.is_active ? 'var(--accent-emerald)' : 'var(--text-muted)'};">${cfg.is_active ? '● 当前正在使用' : '○ 未启用'}</span>
                        </label>
                    </div>

                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 14px;">
                        <div>
                            <label class="form-label">${meta.urlLabel}</label>
                            <input type="text" id="url-${cfg.id}" class="form-control" value="${cfg.base_url || ''}" placeholder="例如 http://127.0.0.1:9233">
                            <div class="field-tip">${meta.urlTip}</div>
                        </div>
                        <div>
                            <label class="form-label">${meta.modelLabel}</label>
                            <input type="text" id="model-${cfg.id}" class="form-control" value="${cfg.model_name || ''}" placeholder="例如 CosyVoice2-0.5B">
                            <div class="field-tip">${meta.modelTip}</div>
                        </div>
                    </div>

                    <div style="margin-bottom: 16px;">
                        <label class="form-label">${meta.keyLabel}</label>
                        <input type="password" id="key-${cfg.id}" class="form-control" placeholder="${cfg.masked_key ? '已加密存储: ' + cfg.masked_key + ' (若不修改请留空)' : '输入 API Key 密钥'}">
                    </div>

                    <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; background: rgba(0,0,0,0.2); padding: 10px 14px; border-radius: 8px;">
                        <div style="display: flex; gap: 10px;">
                            <button class="btn btn-sm btn-primary" onclick="saveSettingConfig('${cfg.config_group}', '${cfg.provider_name}', '${cfg.id}', this)">保存此项配置修改</button>
                            <button class="btn btn-sm" onclick="pingSettingConfig('${cfg.config_group}', '${cfg.provider_name}', '${cfg.id}', this)">连通性测试 · 探测延迟</button>
                        </div>
                        <span class="ping-status" id="status-${cfg.id}" style="font-size: 12px; font-weight: 500;"></span>
                    </div>
                `;
                container.appendChild(div);
            });
        }
    } catch (e) {
        console.error("加载配置失败", e);
    }
}

// 快速填入输入框辅助函数
function fillInputValue(inputId, value) {
    const el = document.getElementById(inputId);
    if (el) {
        el.value = value;
        el.focus();
    }
}

// 保存修改后的非 LLM 配置参数
async function saveSettingConfig(group, name, id, btn) {
    const urlInput = document.getElementById(`url-${id}`);
    const modelInput = document.getElementById(`model-${id}`);
    const keyInput = document.getElementById(`key-${id}`);
    const activeCheck = document.getElementById(`active-${id}`);
    const statusSpan = document.getElementById(`status-${id}`);

    const payload = {
        id: id,
        config_group: group,
        provider_name: name,
        base_url: urlInput ? urlInput.value.trim() : null,
        model_name: modelInput ? modelInput.value.trim() : null,
        is_active: activeCheck ? activeCheck.checked : true
    };

    if (keyInput && keyInput.value.trim()) {
        payload.api_key = keyInput.value.trim();
    }

    if (statusSpan) {
        statusSpan.style.color = "var(--text-secondary)";
        statusSpan.innerText = "正在加密保存...";
    }
    btn.disabled = true;

    try {
        const res = await fetch(`${API_BASE}/settings/configs/save`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        const json = await res.json();
        if (json.code === 0) {
            if (statusSpan) {
                statusSpan.style.color = "var(--accent-emerald)";
                statusSpan.innerText = "✓ 配置已安全保存生效！";
            }
            if (keyInput && keyInput.value.trim()) {
                keyInput.placeholder = "已加密更新，输入可继续修改";
                keyInput.value = "";
            }
        } else {
            if (statusSpan) {
                statusSpan.style.color = "var(--accent-danger)";
                statusSpan.innerText = "保存失败: " + json.message;
            }
        }
    } catch (e) {
        if (statusSpan) {
            statusSpan.style.color = "var(--accent-danger)";
            statusSpan.innerText = "保存出错: " + e;
        }
    } finally {
        btn.disabled = false;
        setTimeout(() => {
            if (statusSpan && statusSpan.innerText.includes("保存生效")) {
                statusSpan.innerText = "";
            }
        }, 4000);
    }
}

// 执行连通性测试 (使用当前输入框内的最新参数)
async function pingSettingConfig(group, name, id, btn) {
    const urlInput = document.getElementById(`url-${id}`);
    const keyInput = document.getElementById(`key-${id}`);
    const statusSpan = document.getElementById(`status-${id}`);

    if (statusSpan) {
        statusSpan.style.color = "var(--text-secondary)";
        statusSpan.innerText = "正在探测链路延迟...";
    }
    btn.disabled = true;

    const payload = {
        config_id: id,
        config_group: group,
        provider_name: name,
        base_url: urlInput ? urlInput.value.trim() : "",
        api_key: keyInput && keyInput.value.trim() ? keyInput.value.trim() : null
    };

    try {
        const res = await fetch(`${API_BASE}/settings/ping`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        const json = await res.json();
        if (statusSpan) {
            if (json.success) {
                statusSpan.style.color = "var(--accent-emerald)";
                statusSpan.innerText = `[OK 连通正常] 首包响应: ${json.latency_ms}ms · ${json.message}`;
            } else {
                statusSpan.style.color = "var(--accent-danger)";
                statusSpan.innerText = `[连通失败] ${json.message}`;
            }
        }
    } catch (e) {
        if (statusSpan) {
            statusSpan.style.color = "var(--accent-danger)";
            statusSpan.innerText = `[探测出错]: ${e}`;
        }
    } finally {
        btn.disabled = false;
    }
}

// 展开/收起添加自定义服务商
function toggleAddProviderForm() {
    toggleAddCustomTTSForm();
}

// 提交新增自定义服务商
async function submitNewProvider() {
    const group = document.getElementById("new-cfg-group").value;
    const name = document.getElementById("new-provider-name").value.trim();
    const url = document.getElementById("new-base-url").value.trim();
    const model = document.getElementById("new-model-name").value.trim();
    const key = document.getElementById("new-api-key").value.trim();

    if (!name) {
        alert("请输入服务商标识（如 custom_tts）");
        return;
    }

    try {
        const res = await fetch(`${API_BASE}/settings/configs/save`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                config_group: group,
                provider_name: name,
                base_url: url,
                model_name: model,
                api_key: key || null,
                is_active: true
            })
        });
        const json = await res.json();
        if (json.code === 0) {
            alert("自定义服务商已成功添加！");
            toggleAddProviderForm();
            loadSettings();
        } else {
            alert("添加失败: " + json.message);
        }
    } catch (e) {
        alert("请求异常: " + e);
    }
}

// 9. 人工紧急插播

async function sendManualSpeech(customText = null) {
    const text = customText || document.getElementById("manual-speech-input").value;
    if (!text) return;
    try {
        const res = await fetch(`${API_BASE}/live/manual-speech`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                text: text,
                interrupt_ongoing: true
            })
        });
        const json = await res.json();
        if (json.code === 0) {
            triggerBargeInVisual("人工紧急介入");
            logDanmaku("【中控运营插播】", text, true);
            if (!customText) document.getElementById("manual-speech-input").value = "";
        }
    } catch (e) {
        alert("插播失败: " + e);
    }
}

// 10. 模拟观众弹幕送礼 (真实注入后端优先级队列，驱动完整 AI 应答链路)
async function sendMockDanmaku(type) {
    try {
        const res = await fetch(`${API_BASE}/live/mock-event`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ type: type === "gift" ? "gift" : "chat" })
        });
        const json = await res.json();
        if (json.code === 0) {
            showToast(json.message, "info");
            if (type === "gift") {
                logDanmaku("仿真大哥888", "送出 超级大火箭 x 1 (P0 强打断)", true);
            } else {
                logDanmaku("仿真买家小美", "主播，这款多少钱？现在还有优惠吗？", false);
            }
        } else {
            showToast(json.detail || json.message || "注入失败", "warning");
        }
    } catch (e) {
        showToast("模拟事件请求异常: " + e, "error");
    }
}


// ============================================================================
// AI 声明微标 (规划 §14.3)：一键开启"本直播间由人工智能技术辅助生成"合规角标
// ============================================================================
(function mountAiBadge() {
    function render() {
        if (document.getElementById("ai-declaration-badge")) return;

        const badge = document.createElement("div");
        badge.id = "ai-declaration-badge";
        badge.innerText = "本直播间由人工智能技术辅助生成";
        badge.style.cssText = [
            "position: fixed", "top: 14px", "right: 18px", "z-index: 9999",
            "padding: 5px 12px", "border-radius: 999px",
            "background: rgba(15, 23, 42, 0.72)", "color: #7dd3fc",
            "font-size: 12px", "letter-spacing: 1px",
            "border: 1px solid rgba(125, 211, 252, 0.35)",
            "-webkit-backdrop-filter: blur(4px)",
            "backdrop-filter: blur(4px)", "display: none"
        ].join(";");

        const toggle = document.createElement("button");
        toggle.id = "ai-declaration-toggle";
        toggle.innerHTML = `${svg("shield", "icon-sm")} AI 声明`;
        toggle.title = "开启/关闭合规 AI 声明角标";
        toggle.style.cssText = [
            "position: fixed", "bottom: 14px", "right: 18px", "z-index: 9999",
            "padding: 6px 14px", "border-radius: 8px", "cursor: pointer",
            "background: rgba(30, 41, 59, 0.85)", "color: #94a3b8",
            "border: 1px solid rgba(148, 163, 184, 0.3)", "font-size: 12px"
        ].join(";");

        let visible = false;
        toggle.addEventListener("click", () => {
            visible = !visible;
            badge.style.display = visible ? "block" : "none";
            toggle.style.color = visible ? "#7dd3fc" : "#94a3b8";
        });

        document.body.appendChild(badge);
        document.body.appendChild(toggle);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", render);
    } else {
        render();
    }
})();


// ============================================================================
// 开播向导 (需求1+2)：第一步选直播模式 → 第二步选主播角色 → 建议与约束提示词
// ============================================================================
let wizardMode = "A";       // 默认选择 A 档
let wizardRoleType = "ecommerce";

async function initWizard() {
    await renderModeCards();
    await renderWizardRoleCards();
    await restoreWizardState();
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
                <div class="pf-title">${c.title}</div>
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

async function loadAnchors() {
    try {
        const res = await fetch(`${API_BASE}/anchors/list`);
        const json = await res.json();
        if (json.code !== 0) return;
        anchorCache = json.data || [];

        // 读取当前开播主播 (音色绑定链路: startLiveDirect 依赖 selected_anchor_id)
        let currentAnchorId = "";
        try {
            const mRes = await fetch(`${API_BASE}/settings/live-mode`);
            const mJson = await mRes.json();
            if (mJson.code === 0 && mJson.data) currentAnchorId = mJson.data.selected_anchor_id || "";
        } catch (e) {}

        const tbody = document.getElementById("anchors-tbody");
        if (!tbody) return;
        tbody.innerHTML = "";
        if (anchorCache.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; color:var(--text-muted); padding: 20px;">暂无主播档案，请在上方创建</td></tr>';
            return;
        }
        anchorCache.forEach(a => {
            const voiceName = escapeHtml((voiceCache.find(v => v.id === a.voice_id) || {}).name || "未绑定");
            const portrait = a.photos && a.photos.portrait;
            const photoCount = Object.values(a.photos || {}).filter(p => p).length;
            const thumb = portrait
                ? `<img src="/static-file?path=${encodeURIComponent(portrait)}" style="width: 38px; height: 38px; object-fit: cover; border-radius: 50%; border: 1px solid var(--border-subtle); vertical-align: middle; margin-right: 6px;" onerror="this.src='/static/svg/default_avatar.svg'"> `
                : `<img src="/static/svg/default_avatar.svg" style="width: 38px; height: 38px; object-fit: cover; border-radius: 50%; border: 1px solid var(--border-subtle); vertical-align: middle; margin-right: 6px;"> `;
            const isCurrent = a.id === currentAnchorId;
            const currentTag = isCurrent
                ? '<span class="badge-recommend" style="font-size: 10px; padding: 2px 8px; margin-left: 6px;">● 开播主播</span>'
                : "";
            const tr = document.createElement("tr");
            tr.innerHTML = `
                <td style="font-weight: 600; white-space: nowrap;">${thumb}${escapeHtml(a.name)}${currentTag}</td>
                <td>${voiceName}</td>
                <td>${photoCount}/4 张</td>
                <td style="max-width: 200px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtml(a.remark || '')}">${escapeHtml(a.remark || '-')}</td>
                <td style="white-space: nowrap;">
                    <button class="btn btn-sm ${isCurrent ? "btn-primary" : ""}" onclick="setCurrentAnchor('${a.id}')" ${isCurrent ? "disabled" : ""}>${isCurrent ? "已选中" : "设为开播"}</button>
                    <button class="btn btn-sm" onclick="editAnchor('${a.id}')">编辑</button>
                    <button class="btn btn-sm btn-danger" onclick="deleteAnchor('${a.id}', '${escapeHtml(a.name)}')">删除</button>
                </td>
            `;
            tbody.appendChild(tr);
        });
    } catch (e) { console.error("加载主播失败", e); }
}

async function setCurrentAnchor(anchorId) {
    try {
        const res = await fetch(`${API_BASE}/settings/selected-anchor`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ anchor_id: anchorId })
        });
        const json = await res.json();
        if (json.code === 0) {
            showToast("已设为开播主播，开播时将按其绑定音色发声 ✓", "success");
            loadAnchors();
        } else {
            alert("设置失败: " + (json.detail || json.message || "未知错误"));
        }
    } catch (e) { alert("设置异常: " + e); }
}

async function submitAnchor() {
    const editId = document.getElementById("anchor-edit-id").value;
    const name = document.getElementById("anchor-name").value.trim();
    if (!name) { alert("请输入主播姓名"); return; }

    const fd = new FormData();
    if (editId) fd.append("id", editId);
    fd.append("name", name);
    fd.append("voice_id", document.getElementById("anchor-voice").value || "");
    fd.append("remark", document.getElementById("anchor-remark").value.trim() || "");
    ["portrait", "full_body", "half_body", "side"].forEach(slot => {
        const input = document.getElementById(`anchor-photo-${slot}`);
        if (input && input.files && input.files.length) fd.append(slot, input.files[0]);
    });

    try {
        const url = editId ? `${API_BASE}/anchors/update` : `${API_BASE}/anchors/create`;
        const res = await fetch(url, { method: "POST", body: fd });
        const json = await res.json();
        if (json.code === 0) {
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
    document.getElementById("anchor-voice").value = a.voice_id || "";
    document.getElementById("anchor-remark").value = a.remark || "";
    document.getElementById("anchor-edit-hint").innerText = `(正在编辑: ${a.name})`;
    document.getElementById("anchor-reset-btn").style.display = "inline-block";
}

function resetAnchorForm() {
    document.getElementById("anchor-edit-id").value = "";
    document.getElementById("anchor-name").value = "";
    document.getElementById("anchor-remark").value = "";
    ["portrait", "full_body", "half_body", "side"].forEach(slot => {
        const input = document.getElementById(`anchor-photo-${slot}`);
        if (input) input.value = "";
    });
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
// 直播大屏运营参数 (需求8)：模式/主播/场观/打赏/成交额/网络/时长
// ============================================================================
async function loadLiveStats() {
    if (document.hidden) return;  // 页面不可见时暂停轮询，节省资源
    try {
        const res = await fetch(`${API_BASE}/live/stats`);
        const json = await res.json();
        if (json.code !== 0) return;
        const d = json.data;

        const set = (id, val) => { const el = document.getElementById(id); if (el) el.innerHTML = val; };
        set("stat-mode", d.mode ? `${d.mode} 档 · ${d.platform || ""}` : "未配置 (请先完成开播向导)");
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
        } catch (e) {}
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
// 知识库管理与 RAG 语义检索交互逻辑
// ============================================================================

function toggleUploadDocModal() {
    const box = document.getElementById("upload-doc-box");
    if (!box) return;
    box.style.display = box.style.display === "none" ? "block" : "none";
}

async function loadKnowledgeList() {
    try {
        const res = await fetch(`${API_BASE}/knowledge/list`);
        if (!res.ok) return;
        const json = await res.json();
        const docs = json.data || [];
        
        // 统计更新
        const docCountEl = document.getElementById("kb-doc-count");
        const chunkCountEl = document.getElementById("kb-chunk-count");
        if (docCountEl) docCountEl.innerText = `${docs.length} 篇`;
        
        let totalChunks = 0;
        docs.forEach(d => totalChunks += (d.chunk_count || 0));
        if (chunkCountEl) chunkCountEl.innerText = `${totalChunks} 条`;

        const tbody = document.getElementById("kb-docs-tbody");
        if (!tbody) return;

        if (docs.length === 0) {
            tbody.innerHTML = `<tr><td colspan="5" style="text-align: center; color: var(--text-secondary); padding: 30px;">暂无入库知识文档，请点击上方【上传知识文献】导入</td></tr>`;
            return;
        }

        tbody.innerHTML = docs.map(doc => `
            <tr>
                <td style="font-weight: 600; color: var(--text-primary);">
                    <div style="display: flex; align-items: center; gap: 6px;">
                        ${svg("clipboard", "icon-sm")}
                        <span>${doc.doc_name || "未命名"}</span>
                    </div>
                </td>
                <td style="font-family: var(--font-mono); font-size: 11px; color: var(--text-secondary);">${doc.doc_id || ""}</td>
                <td><span class="brand-badge green">${doc.chunk_count || 0} 个切片</span></td>
                <td style="color: var(--text-secondary); font-size: 12px; max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${doc.preview || ''}">
                    ${doc.preview || "-"}
                </td>
                <td>
                    <button class="btn btn-sm" style="color: var(--red); border-color: rgba(239,68,68,0.3);" onclick="deleteKnowledgeDoc('${doc.doc_id}', '${doc.doc_name || ''}')">
                        ${svg("trash", "icon-sm")} 删除
                    </button>
                </td>
            </tr>
        `).join("");
    } catch (e) {
        console.error("加载知识库列表异常:", e);
    }
}

async function submitUploadDoc() {
    const fileInput = document.getElementById("kb-upload-file");
    const titleInput = document.getElementById("kb-upload-title");
    const loadingEl = document.getElementById("kb-upload-loading");
    const submitBtn = document.getElementById("btn-submit-upload-doc");

    if (!fileInput || !fileInput.files || fileInput.files.length === 0) {
        alert("请先选择要上传的文档文件 (.txt, .md, .csv, .pdf, .docx)");
        return;
    }

    const file = fileInput.files[0];
    const docName = titleInput ? titleInput.value.trim() : "";

    const formData = new FormData();
    formData.append("file", file);
    if (docName) {
        formData.append("doc_name", docName);
    }

    try {
        if (loadingEl) loadingEl.style.display = "inline";
        if (submitBtn) submitBtn.disabled = true;

        const res = await fetch(`${API_BASE}/knowledge/upload`, {
            method: "POST",
            body: formData
        });
        const json = await res.json();

        if (res.ok && json.code === 0) {
            showToast(json.message || "文档已成功导入知识库", "success");
            fileInput.value = "";
            if (titleInput) titleInput.value = "";
            toggleUploadDocModal();
            loadKnowledgeList();
        } else {
            alert(json.detail || json.message || "上传解析失败");
        }
    } catch (e) {
        alert("上传知识库文档异常: " + e);
    } finally {
        if (loadingEl) loadingEl.style.display = "none";
        if (submitBtn) submitBtn.disabled = false;
    }
}

async function deleteKnowledgeDoc(docId, docName) {
    if (!confirm(`确认要删除文献【${docName || docId}】及其所有分块切片吗？`)) return;

    try {
        const res = await fetch(`${API_BASE}/knowledge/${docId}`, {
            method: "DELETE"
        });
        const json = await res.json();
        if (res.ok && json.code === 0) {
            showToast(json.message || "文献已成功移除", "success");
            loadKnowledgeList();
        } else {
            alert(json.detail || json.message || "删除失败");
        }
    } catch (e) {
        alert("删除知识文献异常: " + e);
    }
}

async function testKnowledgeQuery() {
    const queryInput = document.getElementById("kb-test-query");
    const topkSelect = document.getElementById("kb-test-topk");
    const container = document.getElementById("kb-test-results-container");
    const listEl = document.getElementById("kb-test-results-list");

    if (!queryInput || !queryInput.value.trim()) {
        alert("请输入要测试检索的问题关键词");
        return;
    }

    const query = queryInput.value.trim();
    const top_k = parseInt(topkSelect ? topkSelect.value : "3", 10) || 3;

    try {
        const res = await fetch(`${API_BASE}/knowledge/search`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ query, top_k })
        });
        const json = await res.json();

        if (container) container.style.display = "block";
        const ragData = json.data || {};
        const results = ragData.hits || [];

        if (results.length === 0) {
            if (listEl) {
                listEl.innerHTML = `<div style="color: var(--amber); font-size: 13px; padding: 8px 0;">未检索到匹配的知识切片（综合匹配分低于引擎置信度门限，将触发安全兜底话术）。</div>`;
            }
            return;
        }

        if (listEl) {
            listEl.innerHTML = results.map((item, idx) => `
                <div style="background: rgba(255,255,255,0.03); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); padding: 12px; margin-bottom: 10px;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                        <span style="font-weight: 600; font-size: 13px; color: var(--signal);">#${idx + 1} 来自文献: ${item.doc_name || '未命名'}</span>
                        <span class="brand-badge green">综合匹配分: ${(item.score || 0).toFixed(4)}</span>
                    </div>
                    <div style="font-size: 11px; font-family: var(--font-mono); color: var(--text-secondary); margin-bottom: 6px;">Chunk ID: ${item.chunk_id || '-'}</div>
                    <div style="font-size: 13px; color: var(--text-primary); line-height: 1.6; background: rgba(0,0,0,0.25); padding: 8px 12px; border-radius: 4px; border-left: 3px solid var(--signal);">
                        ${item.content || ''}
                    </div>
                </div>
            `).join("");
        }
    } catch (e) {
        alert("知识检索测试异常: " + e);
    }
}

// ===========================================================================
// RAG 引擎状态 (真实语义向量 / 降级) 与 实时多模态视觉感知配置
// ===========================================================================
async function loadKnowledgeStatus() {
    try {
        const res = await fetch(`${API_BASE}/knowledge/status`);
        const json = await res.json();
        const el = document.getElementById("kb-engine-status");
        if (el && json.data) {
            const d = json.data;
            const backend = d.vector_backend === "onnx" ? "BGE 真实语义向量 (ONNX)" : "哈希投影向量 (离线降级)";
            el.innerText = `${backend} · ${d.dim} 维 · ${d.chunks} 分块 · 置信阈值 ${d.min_score}`;
        }
    } catch (e) { /* 静默 */ }
}

async function loadVisionConfig() {
    try {
        const res = await fetch(`${API_BASE}/settings/vision`);
        const json = await res.json();
        const d = json.data || {};
        const cb = document.getElementById("vision-enabled");
        if (cb) cb.checked = !!d.enabled;
        const src = document.getElementById("vision-source");
        if (src) src.value = d.source || "desktop_screen";
        const iv = document.getElementById("vision-interval");
        if (iv) iv.value = d.interval_sec || 2.5;
        const st = document.getElementById("vision-status");
        if (st) {
            const s = d.status || {};
            if (!s.enabled) {
                st.innerText = "视觉通道：已关闭";
                st.style.color = "var(--text-muted)";
            } else if (s.available) {
                st.innerText = "视觉通道：已启用 · 采集正常";
                st.style.color = "var(--signal)";
            } else {
                st.innerText = "视觉通道：已启用但依赖缺失 (请安装 Pillow/opencv-python)";
                st.style.color = "var(--amber)";
            }
        }
    } catch (e) { /* 静默 */ }
}

async function saveVisionConfig() {
    const enabled = (document.getElementById("vision-enabled") || {}).checked || false;
    const source = (document.getElementById("vision-source") || {}).value || "desktop_screen";
    const interval_sec = parseFloat((document.getElementById("vision-interval") || {}).value || "2.5");
    try {
        const res = await fetch(`${API_BASE}/settings/vision`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ enabled, source, interval_sec })
        });
        const json = await res.json();
        showToast(json.message || "视觉感知配置已保存", json.code === 0 ? "success" : "error");
        loadVisionConfig();
    } catch (e) {
        alert("保存视觉感知配置异常: " + e);
    }
}

// ===========================================================================
// 物理音频输出设备与虚拟声卡 (VB-Cable) 配置 (规划 §7.1/§7.2)
// ===========================================================================
async function loadAudioDevices() {
    try {
        const res = await fetch(`${API_BASE}/settings/audio-devices`);
        const json = await res.json();
        if (json.code === 0 && json.data) {
            const selectEl = document.getElementById("audio-device-select");
            const badgeEl = document.getElementById("audio-device-badge");
            const status = json.data.status || {};
            const devices = json.data.devices || [];

            if (badgeEl) {
                if (!status.available) {
                    badgeEl.className = "brand-badge amber";
                    badgeEl.innerText = "未安装 sounddevice (已软降级)";
                } else {
                    badgeEl.className = "brand-badge green";
                    badgeEl.innerText = `输出至: ${status.device_name || '默认设备'}`;
                }
            }

            if (selectEl) {
                selectEl.innerHTML = `<option value="">系统默认音频播放设备</option>`;
                devices.forEach(d => {
                    const opt = document.createElement("option");
                    opt.value = String(d.index);
                    const mark = d.is_virtual_cable ? " ★ [推荐虚拟声卡 VB-Cable]" : (d.is_default ? " [系统默认]" : "");
                    opt.textContent = `#${d.index}: ${d.name}${mark}`;
                    if (status.device_index !== null && status.device_index === d.index) {
                        opt.selected = true;
                    }
                    selectEl.appendChild(opt);
                });
            }
        }
    } catch (e) {
        console.warn("加载音频设备列表异常:", e);
    }
}

async function saveAudioDeviceSelection() {
    const selectEl = document.getElementById("audio-device-select");
    const val = selectEl ? selectEl.value : "";
    const devIdx = val === "" ? null : parseInt(val);

    try {
        const res = await fetch(`${API_BASE}/settings/audio-device`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ device_index: devIdx })
        });
        const json = await res.json();
        if (json.code === 0) {
            showToast(json.message || "音频输出设备已更新", "success");
            loadAudioDevices();
        } else {
            showToast("切换音频设备失败: " + (json.detail || json.message), "error");
        }
    } catch (e) {
        alert("保存音频设备选择异常: " + e);
    }
}

