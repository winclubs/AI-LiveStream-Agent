/**
 * AI-LiveStream-Agent 现代控制台前端驱动引擎
 * 全双工 WebSocket 状态流 + RESTful API 联动
 */

const API_BASE = "/api/v1";
const FRONTEND_VERSION = "1.9.0";

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
// LLM 大模型生态元数据与全局配置状态 (全模块共享，此文件最先加载)
// ============================================================================
const BUILTIN_LLM_ECOSYSTEM = [
    {
        id: "deepseek",
        name: "DeepSeek",
        tagline: "国产性价比标杆 · 高情商促单",
        logoSvg: "/static/svg/model_deepseek.svg",
        defaultBaseUrl: "https://api.deepseek.com/v1",
        desc: "国产顶流高情商大模型，超低调用资费，高情商话术与实时弹幕互动首选。",
        urlPills: [
            { text: "DeepSeek 官方 (推荐)", val: "https://api.deepseek.com/v1" },
            { text: "备用直连端点", val: "https://api.deepseek.com" }
        ]
    },
    {
        id: "qwen",
        name: "Qwen 通义",
        tagline: "直播电商霸主 · 极速指令遵循",
        logoSvg: "https://img.alicdn.com/imgextra/i2/O1CN01TOFMg022PLymzwaSX_!!6000000007112-55-tps-40-40.svg",
        defaultBaseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1",
        desc: "阿里系模型适合电商直播话术生成，支持促销表达与商品解读，并可与 CosyVoice 配置组合使用。",
        urlPills: [
            { text: "阿里云 DashScope (官方兼容)", val: "https://dashscope.aliyuncs.com/compatible-mode/v1" },
            { text: "本地 Ollama Qwen2.5", val: "http://127.0.0.1:11434/v1" }
        ]
    },
    {
        id: "minimax",
        name: "MiniMax",
        tagline: "万亿长文本 · 细腻拟真共情",
        logoSvg: "/static/svg/model_minimax.svg",
        defaultBaseUrl: "https://api.minimax.chat/v1",
        desc: "全自研万亿长文本与拟真共情模型，情绪与音色表达出众，直播陪伴感强。",
        urlPills: [
            { text: "MiniMax 官方", val: "https://api.minimax.chat/v1" }
        ]
    },
    {
        id: "kimi",
        name: "Kimi 月暗",
        tagline: "超长上下文 · 直播选品记忆",
        logoSvg: "/static/svg/model_kimi.svg",
        defaultBaseUrl: "https://api.moonshot.cn/v1",
        desc: "无损超长上下文标杆，百款商品 SKU 参数、品牌白皮书与大促规则精准深度召回，零幻觉不乱编。",
        urlPills: [
            { text: "Moonshot 官方", val: "https://api.moonshot.cn/v1" }
        ]
    },
    {
        id: "glm",
        name: "GLM 智谱",
        tagline: "清华系标杆 · 合规安全极速",
        logoSvg: "/static/svg/model_glm.svg",
        defaultBaseUrl: "https://open.bigmodel.cn/api/paas/v4/",
        desc: "智谱自研成熟基座，中文语境深厚，安全审查与合规能力极强，glm-4-flash 免费且超快。",
        urlPills: [
            { text: "智谱开放平台官方", val: "https://open.bigmodel.cn/api/paas/v4/" }
        ]
    },
    {
        id: "gemini",
        name: "Gemini",
        tagline: "超快首包 · 原生多模态感知",
        logoSvg: "/static/svg/model_gemini.svg",
        defaultBaseUrl: "https://generativelanguage.googleapis.com/v1beta/openai/",
        desc: "Google 旗舰多模态大模型，flash 系列具备毫秒级首包极速生成，契合多模态眼见即所言。",
        urlPills: [
            { text: "Google 官方 OpenAI 兼容通道", val: "https://generativelanguage.googleapis.com/v1beta/openai/" }
        ]
    },
    {
        id: "chatgpt",
        name: "ChatGPT",
        tagline: "全球顶级旗舰 · 全能综合推理",
        logoSvg: "/static/svg/model_chatgpt.svg",
        defaultBaseUrl: "https://api.openai.com/v1",
        desc: "OpenAI 工业级基准大模型，具备出色的多任务理解、结构化输出与丰富插件兼容能力。",
        urlPills: [
            { text: "OpenAI 官方", val: "https://api.openai.com/v1" }
        ]
    },
    {
        id: "custom",
        name: "自定义/离线",
        tagline: "支持 Ollama / 硅基流动",
        logoSvg: "/static/svg/model_custom.svg",
        defaultBaseUrl: "https://api.siliconflow.cn/v1",
        desc: "任意符合 OpenAI API 规范的代理服务、云端 API 或本地 Ollama/vLLM 离线网关自由接入。",
        urlPills: [
            { text: "硅基流动 SiliconFlow", val: "https://api.siliconflow.cn/v1" },
            { text: "本地 Ollama 离线", val: "http://127.0.0.1:11434/v1" }
        ]
    }
];
window.BUILTIN_LLM_ECOSYSTEM = BUILTIN_LLM_ECOSYSTEM;

var currentSelectedLLMProvider = "deepseek";
var currentSelectedTTSProvider = null;
var cachedAllConfigs = [];
let voiceCache = [];
window.currentSelectedLLMProvider = currentSelectedLLMProvider;
window.currentSelectedTTSProvider = currentSelectedTTSProvider;
window.cachedAllConfigs = cachedAllConfigs;

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
    if (typeof loadGpuAvatarProviders === "function") loadGpuAvatarProviders();
    if (typeof loadWizardAvatarProviders === "function") loadWizardAvatarProviders();

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
