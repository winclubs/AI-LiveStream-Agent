/**
 * AI-LiveStream-Agent 现代控制台前端驱动引擎
 * 全双工 WebSocket 状态流 + RESTful API 联动
 */

const API_BASE = "/api/v1";
const FRONTEND_VERSION = "2.0.0";

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
function initNavigation() {
    const navItems = document.querySelectorAll(".nav-item");
    navItems.forEach(item => {
        item.addEventListener("click", () => {
            navItems.forEach(n => n.classList.remove("active"));
            item.classList.add("active");

            const tab = item.getAttribute("data-tab");
            try { localStorage.setItem("active_admin_tab", tab); } catch (e) {}
            document.querySelectorAll(".tab-content").forEach(tc => tc.classList.remove("active"));
            const target = document.getElementById(`tab-${tab}`);
            if (target) target.classList.add("active");

            if (tab === "live") { if (typeof refreshLiveGpuTelemetry === 'function') refreshLiveGpuTelemetry(); }
            if (tab === "settings") { loadSettings(); loadVisionConfig(); }
            if (tab === "gpu") { loadGpuAvatarProviders(); }
            if (tab === "wizard") { loadWizardAvatarProviders(); }
            if (tab === "knowledge") { loadKnowledgeList(); loadKnowledgeStatus(); }
            if (tab === "anchors") { if (typeof loadAnchors === 'function') loadAnchors(); }
            if (tab === "voices") {
                loadAudioDevices();
                loadVoiceTable();

                // 动态决议当前应当选中的 TTS 引擎（严格以用户配置为准，无配置时才默认本地引擎）
                const resolvedTTS = (typeof resolveActiveOrPreferredTTSProvider === "function")
                    ? resolveActiveOrPreferredTTSProvider(typeof cachedAllConfigs !== "undefined" ? cachedAllConfigs : null)
                    : { providerId: "moss_tts_nano", config: null };

                if (typeof renderTTSEcosystemGrid === 'function') {
                    renderTTSEcosystemGrid(resolvedTTS.providerId);
                }
                if (typeof loadSettings === 'function') {
                    loadSettings();
                } else if (typeof renderConfiguredTTS === 'function' && typeof cachedAllConfigs !== 'undefined') {
                    renderConfiguredTTS(cachedAllConfigs);
                }
            }
        });
    });

    // 页面刷新后自动恢复用户之前停留的 Tab (如 voices 面板)，绝不强制回退到向导
    try {
        const lastTab = localStorage.getItem("active_admin_tab");
        if (lastTab && lastTab !== "wizard") {
            const lastItem = document.querySelector(`.nav-item[data-tab="${lastTab}"]`);
            if (lastItem) {
                setTimeout(() => lastItem.click(), 50);
            }
        }
    } catch (e) {}
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
        refreshLiveGpuTelemetry();
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
    // 联动数字人监视器视窗视频流 (WebRTC / MJPEG 自适应)
    const selProto = document.getElementById("stream-protocol-select") ? document.getElementById("stream-protocol-select").value : "webrtc";
    const monitorFeed = document.getElementById("digital-human-video-feed");
    const webrtcVideo = document.getElementById("digital-human-webrtc-video");

    if (isLive) {
        if (selProto === "webrtc") {
            if (monitorFeed) monitorFeed.style.display = "none";
            if (webrtcVideo) webrtcVideo.style.display = "block";
            initWebRTCPlayer();
        } else {
            if (webrtcVideo) webrtcVideo.style.display = "none";
            if (monitorFeed) {
                monitorFeed.style.display = "block";
                monitorFeed.src = `${API_BASE}/live/stream/preview?t=${Date.now()}`;
            }
        }
    } else {
        stopWebRTCPlayer();
        if (webrtcVideo) webrtcVideo.style.display = "none";
        if (monitorFeed) {
            monitorFeed.style.display = "block";
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

        const obsCheck = document.getElementById("obs-auto-link-check");
        const obsAutoLink = obsCheck ? obsCheck.checked : false;
        const demoCheck = document.getElementById("demo-mode-check");
        const isDemo = (demoCheck && demoCheck.checked) || platform === "demo";
        const effectivePlatform = platform === "demo" ? "bilibili" : platform;

        const res = await fetch(`${API_BASE}/live/start`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                room_id: roomId,
                platform: effectivePlatform,
                anchor_id: anchorId,
                obs_auto_link: obsAutoLink,
                demo_mode: isDemo
            })
        });
        const json = await res.json();
        if (json.code === 0) {
            updateLiveStateUI(true);
            updateObsStatusUI();
            let obsText = "未联动 OBS";
            if (json.obs_linked) {
                obsText = "OBS 联动推流已启动";
            } else if (json.degraded) {
                obsText = `OBS 联动降级 (${json.obs_error || "未推流"})`;
            }
            const modeText = (!isDemo && roomId) ? `正在监听【${effectivePlatform}】房间事件: ${roomId}` : '已启动离线仿真演练与模拟观众互动';
            logDanmaku("系统通知", `本地直播源已启动！${modeText}；[${obsText}]`, false);
            showToast(`本地直播源已启动 (${obsText})`, json.degraded ? "warning" : "success", 5000);

            // 检查是否开启了内置 RTMP 直推联动
            const rtmpCheck = document.getElementById("rtmp-auto-link-check");
            if (rtmpCheck && rtmpCheck.checked) {
                const savedUrl = localStorage.getItem(RTMP_STORAGE_KEY_URL);
                const savedKey = localStorage.getItem(RTMP_STORAGE_KEY_KEY) || "";
                if (savedUrl) {
                    try {
                        const rtmpRes = await fetch(`${API_BASE}/live/rtmp/start`, {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({
                                rtmp_url: savedUrl,
                                stream_key: savedKey,
                                width: 720,
                                height: 960,
                                fps: 25,
                                bitrate_kbps: 2500
                            })
                        });
                        const rtmpJson = await rtmpRes.json();
                        if (rtmpJson.code === 0) {
                            showToast("内置 RTMP 直推引擎已联动启动！", "success", 4000);
                        } else {
                            showToast("RTMP 直推联动提示: " + (rtmpJson.message || rtmpJson.detail), "warning", 5000);
                        }
                        refreshRtmpStatus();
                    } catch (err) {
                        console.warn("RTMP 联动启动异常:", err);
                    }
                } else {
                    showToast("已开启内置直推，但未填写推流地址，请点击【直推设置】配置", "warn", 5000);
                }
            }
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
            updateObsStatusUI();
            refreshRtmpStatus();
        }
    } catch (e) {
        alert("操作异常: " + e);
    }
}

async function updateObsStatusUI() {
    const badge = document.getElementById("obs-status-badge");
    if (!badge) return;
    try {
        const res = await fetch(`${API_BASE}/live/obs/status`);
        const json = await res.json();
        if (json.code === 0 && json.data) {
            const data = json.data;
            if (data.is_connected) {
                if (data.is_stale) {
                    badge.style.color = "#F59E0B";
                    badge.innerText = "OBS: 信号重连中...";
                } else if (data.is_streaming) {
                    const stats = data.stats || {};
                    badge.style.color = "#10B981";
                    badge.innerText = `OBS: 推流中 (${stats.kbits_per_sec || 0}kbps, ${stats.fps || 0}fps)`;
                } else {
                    badge.style.color = "#38BDF8";
                    badge.innerText = "OBS: 已连接就绪";
                }
            } else {
                badge.style.color = "#94A3B8";
                badge.innerText = "OBS: 未连接";
            }
        }
    } catch (e) {
        badge.style.color = "#94A3B8";
        badge.innerText = "OBS: 未连接";
    }
}

// 自动启动 OBS 状态周期后台轮询 (每 3 秒刷新一次)
if (typeof window !== "undefined" && !window._obsStatusIntervalStarted) {
    window._obsStatusIntervalStarted = true;
    setInterval(updateObsStatusUI, 3000);
}

async function promptObsConnect() {
    const port = prompt("请输入本地 OBS-WebSocket 端口 (OBS -> 工具 -> WebSocket服务器设置):", "4455");
    if (!port) return;
    const pwd = prompt("请输入 OBS-WebSocket 密码 (若无密码请直接留空点确定):", "");
    try {
        showToast("正在建立与 OBS Studio 的通信...", "info");
        const res = await fetch(`${API_BASE}/live/obs/connect`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ host: "127.0.0.1", port: parseInt(port), password: pwd || "" })
        });
        const json = await res.json();
        if (json.code === 0 && json.connected) {
            showToast("成功连接到 OBS Studio！", "success");
            await updateObsStatusUI();
        } else {
            showToast("连接 OBS 失败: " + (json.message || "请检查 OBS 是否已开启 WebSocket"), "error");
        }
    } catch (e) {
        showToast("连接 OBS 发生异常: " + e.message, "error");
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

// 弹出独立无边框数字人绿幕视窗 (供抖音/快手/视频号直播伴侣窗口捕获与色度抠图)
function openAvatarViewport() {
    const url = "/avatar-viewport";
    const title = "AI_LiveStream_Avatar_Viewport";
    const features = "width=720,height=960,menubar=no,toolbar=no,location=no,status=no,resizable=yes,scrollbars=no";
    const win = window.open(url, title, features);
    if (win) {
        win.focus();
        showToast("🎥 独立绿幕视窗已弹出！可在抖音/快手/视频号直播伴侣中【添加窗口】并开启色度抠图。", "success");
    } else {
        showToast("弹窗被浏览器拦截，正在新标签页中打开数字人视窗...", "warning");
        window.open(url, "_blank");
    }
}
window.openAvatarViewport = openAvatarViewport;

// ============================================================================
// ⚡ 阶段四：原生 WebRTC (WHEP) 极速低延迟控制台预览
// ============================================================================
let _webrtcPeerConnection = null;
let _currentStreamProtocol = "webrtc";

function changeStreamProtocol(proto) {
    _currentStreamProtocol = proto;
    const monitorFeed = document.getElementById("digital-human-video-feed");
    const webrtcVideo = document.getElementById("digital-human-webrtc-video");

    if (proto === "webrtc") {
        if (monitorFeed) monitorFeed.style.display = "none";
        if (webrtcVideo) webrtcVideo.style.display = "block";
        if (isLiveStreaming) initWebRTCPlayer();
    } else {
        stopWebRTCPlayer();
        if (webrtcVideo) webrtcVideo.style.display = "none";
        if (monitorFeed) {
            monitorFeed.style.display = "block";
            monitorFeed.src = isLiveStreaming ? `${API_BASE}/live/stream/preview?t=${Date.now()}` : "/static/svg/standby_monitor.svg";
        }
    }
}
window.changeStreamProtocol = changeStreamProtocol;

async function initWebRTCPlayer() {
    stopWebRTCPlayer();
    const webrtcVideo = document.getElementById("digital-human-webrtc-video");
    if (!webrtcVideo) return;

    try {
        const pc = new RTCPeerConnection({
            iceServers: [{ urls: "stun:stun.l.google.com:19302" }]
        });
        _webrtcPeerConnection = pc;

        // 仅接收视频
        pc.addTransceiver("video", { direction: "recvonly" });

        pc.ontrack = (event) => {
            if (event.streams && event.streams[0]) {
                webrtcVideo.srcObject = event.streams[0];
                webrtcVideo.play().catch(() => {});
            }
        };

        const offer = await pc.createOffer();
        await pc.setLocalDescription(offer);

        // POST SDP Offer 到 WHEP 端点
        const res = await fetch(`${API_BASE}/live/webrtc/whep`, {
            method: "POST",
            headers: {
                "Content-Type": "application/sdp",
                "Accept": "application/sdp"
            },
            body: offer.sdp
        });

        if (res.ok || res.status === 201) {
            const answerSdp = await res.text();
            await pc.setRemoteDescription(new RTCSessionDescription({
                type: "answer",
                sdp: answerSdp
            }));
            const modeBadge = document.getElementById("stream-mode-badge");
            if (modeBadge) modeBadge.innerText = "⚡ WebRTC (极速低延迟)";
        } else {
            console.warn("WHEP 协商未成功，自动平滑降级至 MJPEG");
            changeStreamProtocol("mjpeg");
        }
    } catch (e) {
        console.warn("WebRTC 连接异常，已自动降级至 MJPEG:", e);
        changeStreamProtocol("mjpeg");
    }
}

function stopWebRTCPlayer() {
    if (_webrtcPeerConnection) {
        try {
            _webrtcPeerConnection.close();
        } catch (e) {}
        _webrtcPeerConnection = null;
    }
    const webrtcVideo = document.getElementById("digital-human-webrtc-video");
    if (webrtcVideo && webrtcVideo.srcObject) {
        try {
            webrtcVideo.srcObject.getTracks().forEach(t => t.stop());
        } catch (e) {}
        webrtcVideo.srcObject = null;
    }
}

// ============================================================================
// 🎙️ 阶段四：麦克风全双工监听与极速打断系统 (Full-Duplex Mic ASR)
// ============================================================================
let _micStream = null;
let _micAudioContext = null;
let _micWorkletNode = null;
let _asrWebSocket = null;
let _isMicListening = false;

async function toggleFullDuplexMic() {
    if (_isMicListening) {
        stopFullDuplexMic();
        showToast("已关闭麦克风全双工监听", "info");
    } else {
        await startFullDuplexMic();
    }
}
window.toggleFullDuplexMic = toggleFullDuplexMic;

async function startFullDuplexMic() {
    try {
        _micStream = await navigator.mediaDevices.getUserMedia({
            audio: {
                sampleRate: 16000,
                channelCount: 1,
                echoCancellation: true,
                noiseSuppression: true
            }
        });

        const wsProtocol = location.protocol === "https:" ? "wss:" : "ws:";
        const wsUrl = `${wsProtocol}//${location.host}/api/v1/live/asr/ws`;
        _asrWebSocket = new WebSocket(wsUrl);
        _asrWebSocket.binaryType = "arraybuffer";

        _asrWebSocket.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                if (data.type === "interrupted") {
                    showToast("⚡ 检测到人声开嗓，数字人已瞬间闭嘴打断！", "warning");
                    const bargeIn = document.getElementById("barge-in-badge");
                    if (bargeIn) {
                        bargeIn.innerText = "[现场麦克风打断]";
                        bargeIn.style.display = "block";
                        setTimeout(() => { bargeIn.style.display = "none"; }, 2000);
                    }
                } else if (data.type === "transcription") {
                    logDanmaku("🎙️ 麦克风现场提问", data.text, true, true);
                }
            } catch (e) {}
        };

        _micAudioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
        const source = _micAudioContext.createMediaStreamSource(_micStream);
        const scriptNode = _micAudioContext.createScriptProcessor(4096, 1, 1);

        scriptNode.onaudioprocess = (audioEvent) => {
            if (!_asrWebSocket || _asrWebSocket.readyState !== WebSocket.OPEN) return;
            const inputData = audioEvent.inputBuffer.getChannelData(0);
            // 转换为 16-bit PCM
            const pcmBuffer = new ArrayBuffer(inputData.length * 2);
            const pcmView = new DataView(pcmBuffer);
            for (let i = 0; i < inputData.length; i++) {
                const s = Math.max(-1, Math.min(1, inputData[i]));
                pcmView.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
            }
            _asrWebSocket.send(pcmBuffer);
        };

        source.connect(scriptNode);
        scriptNode.connect(_micAudioContext.destination);
        _micWorkletNode = scriptNode;

        _isMicListening = true;
        const btn = document.getElementById("btn-toggle-mic-asr");
        const badge = document.getElementById("mic-active-badge");
        if (btn) {
            btn.style.background = "rgba(59, 130, 246, 0.4)";
            btn.style.borderColor = "#60a5fa";
        }
        if (badge) badge.style.display = "inline-block";
        showToast("🎙️ 麦克风全双工监听已启动，说话可瞬间打断数字人播报！", "success");
    } catch (e) {
        console.error("启动麦克风失败:", e);
        showToast("无法开启麦克风: " + e.message, "error");
        stopFullDuplexMic();
    }
}

function stopFullDuplexMic() {
    _isMicListening = false;
    if (_micStream) {
        _micStream.getTracks().forEach(t => t.stop());
        _micStream = null;
    }
    if (_micWorkletNode) {
        try { _micWorkletNode.disconnect(); } catch (e) {}
        _micWorkletNode = null;
    }
    if (_micAudioContext) {
        try { _micAudioContext.close(); } catch (e) {}
        _micAudioContext = null;
    }
    if (_asrWebSocket) {
        try { _asrWebSocket.close(); } catch (e) {}
        _asrWebSocket = null;
    }

    const btn = document.getElementById("btn-toggle-mic-asr");
    const badge = document.getElementById("mic-active-badge");
    if (btn) {
        btn.style.background = "rgba(59, 130, 246, 0.15)";
        btn.style.borderColor = "rgba(59, 130, 246, 0.4)";
    }
    if (badge) badge.style.display = "none";
}

// ============================================================================
// 🎥 阶段四：短视频与带货切片一键录制导出系统 (/record)
// ============================================================================
let _isRecording = false;
let _recordingTimer = null;
let _recordStartSeconds = 0;

async function toggleRecording() {
    if (_isRecording) {
        await stopRecording();
    } else {
        await startRecording();
    }
}
window.toggleRecording = toggleRecording;

async function startRecording() {
    const title = prompt("请输入本段带货讲解切片的标题 (可留空自动命名):", "爆款商品精彩讲解");
    if (title === null) return;

    try {
        const res = await fetch(`${API_BASE}/live/record/start`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ title: title.trim() || "带货讲解切片" })
        });
        const json = await res.json();
        if (json.code === 0) {
            _isRecording = true;
            _recordStartSeconds = 0;
            const btn = document.getElementById("btn-toggle-recording");
            const dot = document.getElementById("recording-dot");
            const btnText = document.getElementById("recording-btn-text");
            const badge = document.getElementById("rec-duration-badge");

            if (btn) {
                btn.style.background = "rgba(239, 68, 68, 0.4)";
                btn.style.borderColor = "#ef4444";
            }
            if (dot) dot.style.display = "inline-block";
            if (btnText) btnText.innerText = "⏹ 停止录制";
            if (badge) {
                badge.style.display = "inline-block";
                badge.innerText = "REC 00:00";
            }

            clearInterval(_recordingTimer);
            _recordingTimer = setInterval(() => {
                _recordStartSeconds++;
                const m = String(Math.floor(_recordStartSeconds / 60)).padStart(2, "0");
                const s = String(_recordStartSeconds % 60).padStart(2, "0");
                if (badge) badge.innerText = `REC ${m}:${s}`;
            }, 1000);

            showToast("🎥 切片录制已开始，正在持续捕获音画...", "success");
        } else {
            showToast(json.detail || json.message || "开启录制失败", "error");
        }
    } catch (e) {
        showToast("录制异常: " + e, "error");
    }
}

async function stopRecording() {
    clearInterval(_recordingTimer);
    try {
        const res = await fetch(`${API_BASE}/live/record/stop`, {
            method: "POST"
        });
        const json = await res.json();
        if (json.code === 0) {
            showToast(json.message || "切片录制完成！", "success");
            openRecordingsModal();
        } else {
            showToast(json.detail || json.message || "停止录制失败", "error");
        }
    } catch (e) {
        showToast("停止录制异常: " + e, "error");
    } finally {
        _isRecording = false;
        const btn = document.getElementById("btn-toggle-recording");
        const dot = document.getElementById("recording-dot");
        const btnText = document.getElementById("recording-btn-text");
        const badge = document.getElementById("rec-duration-badge");
        if (btn) {
            btn.style.background = "rgba(239, 68, 68, 0.15)";
            btn.style.borderColor = "rgba(239, 68, 68, 0.4)";
        }
        if (dot) dot.style.display = "none";
        if (btnText) btnText.innerText = "🎥 录制切片";
        if (badge) badge.style.display = "none";
    }
}

function openRecordingsModal() {
    const modal = document.getElementById("recordings-modal");
    if (modal) {
        modal.style.display = "flex";
        loadRecordingsList();
    }
}
window.openRecordingsModal = openRecordingsModal;

function closeRecordingsModal() {
    const modal = document.getElementById("recordings-modal");
    if (modal) modal.style.display = "none";
}
window.closeRecordingsModal = closeRecordingsModal;

async function loadRecordingsList() {
    const tbody = document.getElementById("recordings-tbody");
    if (!tbody) return;
    try {
        const res = await fetch(`${API_BASE}/live/record/list`);
        const json = await res.json();
        if (json.code === 0 && Array.isArray(json.data)) {
            renderRecordingsTable(json.data);
        }
    } catch (e) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; color:var(--red); padding: 16px;">获取切片列表失败</td></tr>';
    }
}

function renderRecordingsTable(items) {
    const tbody = document.getElementById("recordings-tbody");
    if (!tbody) return;
    tbody.innerHTML = "";
    if (!items || items.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; color:var(--text-muted); padding: 20px;">暂无已录制切片，点击“录制切片”即可生成</td></tr>';
        return;
    }

    items.forEach(item => {
        const tr = document.createElement("tr");
        const thumb = item.preview_path
            ? `<img src="/static-file?path=${encodeURIComponent(item.preview_path)}" style="width: 64px; height: 36px; object-fit: cover; border-radius: 4px; border: 1px solid rgba(255,255,255,0.1);">`
            : `<div style="width:64px; height:36px; background:#27272a; border-radius:4px; display:flex; align-items:center; justify-content:center; font-size:10px; color:#a1a1aa;">无预览</div>`;
        const sizeMb = (item.file_size_bytes / 1024 / 1024).toFixed(1);

        tr.innerHTML = `
            <td>${thumb}</td>
            <td>
                <div style="font-weight: 600; font-size: 13px;">${escapeHtml(item.title)}</div>
                ${item.sku ? `<span style="font-size: 11px; color: #38bdf8;">SKU: ${escapeHtml(item.sku)}</span>` : ''}
            </td>
            <td><span style="font-family: var(--font-mono); font-size: 12px;">${item.duration_sec}s</span></td>
            <td><span style="font-size: 12px; color: var(--text-muted);">${sizeMb} MB</span></td>
            <td><span style="font-size: 11px; color: var(--text-muted);">${item.created_at}</span></td>
            <td>
                <div style="display: flex; gap: 6px;">
                    <a class="btn btn-sm btn-primary" href="/static-file?path=${encodeURIComponent(item.video_path)}" download style="padding: 2px 8px; font-size: 11px; text-decoration: none;">
                        ⬇ 下载
                    </a>
                    <button class="btn btn-sm btn-danger" style="padding: 2px 8px; font-size: 11px;" onclick="deleteRecording('${item.record_id}')">
                        🗑
                    </button>
                </div>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

async function deleteRecording(recordId) {
    if (!confirm("确定要删除该短视频切片文件吗？")) return;
    try {
        const res = await fetch(`${API_BASE}/live/record/${recordId}`, { method: "DELETE" });
        const json = await res.json();
        if (json.code === 0) {
            loadRecordingsList();
            showToast("切片已删除", "success");
        } else {
            showToast(json.message || "删除失败", "error");
        }
    } catch (e) {
        showToast("删除异常: " + e, "error");
    }
}
window.deleteRecording = deleteRecording;

async function refreshLiveGpuTelemetry() {
    try {
        const badge = document.getElementById("live-gpu-source-badge");
        const detail = document.getElementById("live-gpu-detail-text");
        const tip = document.getElementById("live-gpu-requirement-tip");
        if (!badge || !detail) return;

        const res = await fetch(`${API_BASE}/live/hardware`);
        const json = await res.json();
        if (json.code !== 0 || !json.data) return;

        const d = json.data;
        const cap = d.gpu_capability || {};
        const localGpu = cap.local_gpu || d.gpu || {};
        const cloudGpu = cap.cloud_gpu || {};

        if (cap.use_cloud && cap.can_execute) {
            // 模式 2: 远端租赁 GPU
            badge.className = "brand-badge sky";
            badge.innerText = "⚡ 远端租赁 GPU";
            const provider = cloudGpu.provider_name || "AutoDL/云端算力";
            detail.innerHTML = `已实测连通远端算力节点【${escapeHtml(provider)}】· <span style="color:#38bdf8;font-weight:600;">本地 0 显存负担</span> · 1080P 写实真人就绪`;
            if (tip) tip.innerHTML = `远端算力模式：本地仅需轻薄本 CPU 调度与推流`;
        } else if (cap.can_execute && !cap.is_low_spec_local) {
            // 模式 1: 本地高性能独立显卡
            badge.className = "brand-badge green";
            badge.innerText = "🟢 本地高性能显卡";
            const gpuName = localGpu.gpu_name || "NVIDIA 独显";
            const vramTotal = localGpu.vram_total_gb || 0;
            detail.innerHTML = `已启用本机独显【${escapeHtml(gpuName)}】(显存 ${vramTotal}GB) · <span style="color:#34d399;font-weight:600;">完全满足写实真人生产需求</span>`;
            if (tip) tip.innerHTML = `全单机闭环模式：0 租金支出，免外网带宽依赖`;
        } else {
            // 轻量免显卡 CPU 模式
            badge.className = "brand-badge amber";
            badge.innerText = "⚠️ 轻量 CPU 免显卡";
            const gpuName = localGpu.gpu_name || "轻薄本/核显";
            detail.innerHTML = `当前硬件：${escapeHtml(gpuName)} (显存不足 2.0GB) · <span style="color:#fbbf24;font-weight:600;">已安全切换免显卡程序化驱动</span>`;
            if (tip) tip.innerHTML = `如需 1080P 写实真人直播，建议前往配置远端租赁 GPU (约 1.2元/h)`;
        }
    } catch (e) {
        console.warn("更新直播大屏算力状态失败:", e);
    }
}
window.refreshLiveGpuTelemetry = refreshLiveGpuTelemetry;

async function loadHardwareInfo() {
    try {
        const res = await fetch(`${API_BASE}/live/hardware`);
        const json = await res.json();
        if (json.code !== 0) return;
        const d = json.data;
        const gpu = d.gpu || {};

        const setText = (id, text) => { const el = document.getElementById(id); if (el) el.innerHTML = text; };

        // 显卡
        const cap = d.gpu_capability || {};
        window.cachedHardwareData = d; // 供向导双卡片与算力诊断共享
        if (gpu.gpu_name) {
            let statusSuffix = "";
            if (cap.use_cloud) {
                statusSuffix = ` · <span style="color: var(--accent-emerald); font-weight: 600;">⚡ 已优先调度云端显卡</span>`;
            } else if (cap.is_low_spec_local) {
                statusSuffix = ` · <span style="color: var(--accent-amber); font-weight: 600;">⚠️ 显存不足2G未配云端</span>`;
            }
            setText("hw-panel-gpu", gpu.gpu_name);
            setText("hw-panel-gpu-sub",
                `显存 ${gpu.vram_total_gb}GB${gpu.vram_total_gb > 0 ? ` · 已用 ${gpu.vram_used_gb}GB` : ""} · ${gpu.cuda_available ? '<span style="color: var(--accent-emerald);">CUDA 可用</span>' : '<span style="color: var(--accent-amber);">无 CUDA</span>'}${statusSuffix}`);
        } else {
            const cloudBadge = cap.use_cloud ? ' · <span style="color: var(--accent-emerald); font-weight: 600;">⚡ 已优先调度云端显卡</span>' : ' · <span style="color: var(--accent-amber);">⚠️ 未配置云端显卡</span>';
            setText("hw-panel-gpu", '<span style="color: var(--text-muted);">核显 / 未检测到独显</span>');
            setText("hw-panel-gpu-sub", `将使用轻量方案${cloudBadge}`);
        }

        // 云端 GPU 状态呈现（有则展示具体设备/显存/延迟，无则显示暂无云端GPU）
        const cloudGpu = cap.cloud_gpu || {};
        const lastPing = window.lastCloudPingResult;
        if (cloudGpu.configured || (cloudGpu.base_url && cloudGpu.base_url.trim()) || (lastPing && lastPing.targetUrl)) {
            let cloudName = cloudGpu.gpu_name || (lastPing && lastPing.device) || "云端 GPU 算力节点";
            let vramText = cloudGpu.vram_total_gb ? ` (${cloudGpu.vram_total_gb}GB)` : "";
            let pingStatus = "";
            if (lastPing) {
                if (lastPing.success) {
                    pingStatus = ` · <span style="color: #34d399;">已连通 (${lastPing.latency_ms}ms)</span>`;
                } else {
                    pingStatus = ` · <span style="color: #ef4444;">未连通/离线</span>`;
                }
            }
            setText("hw-panel-gpu-cloud", `<span style="color: #38bdf8; font-weight: 600;">⚡ 云端:</span> <span style="color: #f1f5f9; font-weight: 500;">${escapeHtml(cloudName)}${vramText}</span>${pingStatus}`);
        } else {
            setText("hw-panel-gpu-cloud", `<span style="color: var(--text-muted);">云端: 暂无云端GPU</span>`);
        }

        // 同步通知向导第一步双卡片按最新硬件数据重绘
        if (typeof renderWizardGpuStatusCard === "function") {
            renderWizardGpuStatusCard(false);
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
        const res = await fetch(`${API_BASE}/system/prerequisites?t=${Date.now()}`);
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
                    const isCableReady = Boolean(it.has_cable || (it.badge && it.badge.includes("VB-Cable")) || (it.tip && it.tip.includes("包含推荐虚拟声卡 VB-Cable")));
                    if (it.status === "installed" && isCableReady) {
                        stateText = "检测通过";
                        pillClass = "pill-pass";
                        statusClass = "status-running";
                    } else if (it.status === "installed" || it.status === "running") {
                        stateText = "建议修复";
                        pillClass = "pill-warning";
                        statusClass = "status-warning";
                    } else {
                        stateText = "必须修复";
                        pillClass = "pill-missing";
                        statusClass = "status-missing";
                    }
                } else if (it.status === "running" || it.status === "installed") {
                    stateText = "检测通过";
                    pillClass = "pill-pass";
                    statusClass = it.status === "running" ? "status-running" : "status-installed";
                } else {
                    if (it.required) {
                        stateText = "必须修复";
                        pillClass = "pill-missing";
                        statusClass = "status-missing";
                    } else {
                        stateText = "建议修复";
                        pillClass = "pill-warning";
                        statusClass = "status-warning";
                    }
                }


                const tipClass = pillClass === "pill-pass" ? "tip-installed" : (pillClass === "pill-warning" ? "tip-warning" : "tip-missing");
                const iconName = iconMap[it.key] || "monitor";

                let actionBtnHtml = "";
                if (pillClass === "pill-pass") {
                    actionBtnHtml = `<span class="prereq-ok-label">${svg("check", "icon-sm")} 正常</span>`;
                } else if (it.action_type === "url" && it.url) {
                    actionBtnHtml = `<a href="${it.url}" target="_blank" class="prereq-btn btn-action-primary">${svg("external", "icon-sm")} ${it.action_text}</a>`;
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
    "ecommerce": { icon: "orange", avatarSvg: "/static/svg/anchor_ecommerce.svg", title: "带货主播", tag: "促单逼单 · 爆品推荐", tagColor: "#F97316" },
    "entertainment": { icon: "mic", avatarSvg: "/static/svg/anchor_entertainment.svg", title: "娱乐主播", tag: "逗梗陪伴 · 高情商共情", tagColor: "#2DD4A0" },
    "expert": { icon: "plus-circle", avatarSvg: "/static/svg/anchor_expert.svg", title: "专业专家", tag: "严谨解答 · 法理免责", tagColor: "#EF4444" },
    "chitchat": { icon: "handshake", avatarSvg: "/static/svg/anchor_chitchat.svg", title: "闲聊扯淡", tag: "唠嗑搭子 · 顺话不冷场", tagColor: "#F59E0B" }
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

let currentVoiceTableEngineFilter = "";

// 按引擎筛选音色资产库
function filterVoiceTableByEngine(engine, btn) {
    currentVoiceTableEngineFilter = (engine || "").trim().toLowerCase();
    document.querySelectorAll("#voice-engine-filters .voice-filter-btn").forEach(b => {
        b.classList.toggle("active", b === btn || (b.getAttribute("data-engine") || "") === currentVoiceTableEngineFilter);
    });
    renderVoiceTableRows();
}

// 格式化所属语音引擎徽标
function formatVoiceEngineBadge(providerName) {
    const p = (providerName || "").toLowerCase();
    if (p.includes("edge")) {
        return '<span class="brand-badge" style="background: rgba(2, 132, 199, 0.15); border: 1px solid rgba(2, 132, 199, 0.4); color: #38bdf8; font-size: 11px; padding: 2px 7px;">Edge-TTS</span>';
    } else if (p.includes("cosy")) {
        return '<span class="brand-badge" style="background: rgba(234, 88, 12, 0.15); border: 1px solid rgba(234, 88, 12, 0.4); color: #fb923c; font-size: 11px; padding: 2px 7px;">CosyVoice</span>';
    } else if (p.includes("sovits") || p.includes("gpt")) {
        return '<span class="brand-badge" style="background: rgba(14, 165, 233, 0.15); border: 1px solid rgba(14, 165, 233, 0.4); color: #7dd3fc; font-size: 11px; padding: 2px 7px;">GPT-SoVITS</span>';
    } else if (p.includes("eleven")) {
        return '<span class="brand-badge" style="background: rgba(244, 63, 94, 0.15); border: 1px solid rgba(244, 63, 94, 0.4); color: #fb7185; font-size: 11px; padding: 2px 7px;">ElevenLabs</span>';
    } else if (p.includes("custom") || p.includes("local") || p.includes("自建")) {
        return '<span class="brand-badge" style="background: rgba(168, 85, 247, 0.15); border: 1px solid rgba(168, 85, 247, 0.4); color: #c084fc; font-size: 11px; padding: 2px 7px;">本地自建</span>';
    } else if (p.includes("chat")) {
        return '<span class="brand-badge" style="background: rgba(5, 150, 105, 0.15); border: 1px solid rgba(5, 150, 105, 0.4); color: #34d399; font-size: 11px; padding: 2px 7px;">ChatTTS</span>';
    } else if (p) {
        return `<span class="brand-badge" style="background: rgba(148, 163, 184, 0.12); border: 1px solid rgba(148, 163, 184, 0.3); color: #cbd5e1; font-size: 11px; padding: 2px 7px;">${escapeHtml(providerName)}</span>`;
    }
    return '<span style="color: var(--text-muted); font-size: 11px;">通用引擎</span>';
}

// 渲染音色表格行
function renderVoiceTableRows() {
    const tbody = document.getElementById("voices-tbody");
    const countBadge = document.getElementById("voice-asset-count-badge");
    if (!tbody) return;

    let list = voiceCache || [];
    if (currentVoiceTableEngineFilter) {
        list = list.filter(v => {
            const vp = (v.provider_name || "").toLowerCase();
            const vid = (v.id || "").toLowerCase();
            if (currentVoiceTableEngineFilter === "moss_tts_nano") {
                return vp.includes("moss") || vp.includes("nano") || vid.includes("moss");
            } else if (currentVoiceTableEngineFilter === "cosyvoice") {
                return vp.includes("cosy") || vid.includes("bailian") || vid.includes("cosyvoice") || vid.startsWith("long");
            } else if (currentVoiceTableEngineFilter === "elevenlabs") {
                return vp.includes("eleven");
            }
            return vp === currentVoiceTableEngineFilter;
        });
    }

    if (countBadge) {
        countBadge.innerText = `${list.length} 款音色`;
    }

    tbody.innerHTML = "";
    if (list.length === 0) {
        tbody.innerHTML = `<tr><td colspan="5" style="text-align: center; color: var(--text-muted); padding: 24px;">暂无可显示的音色资产。请在「TTS配置」页面选定语音引擎并保存，即可自动批量同步入库！</td></tr>`;
        return;
    }

    list.forEach(v => {
        const isCloned = v.voice_type === "cloned" || v.is_clone || (v.id && (v.id.includes("bailian") || v.id.startsWith("clone_") || v.id.includes("cosyvoice-v")));
        const typeBadge = isCloned
            ? '<span style="color: #fbbf24; font-weight: 600; font-size: 11.5px; display: inline-flex; align-items: center; gap: 3px;"><span>👑</span> 专属克隆</span>'
            : '<span style="color: #94a3b8; font-size: 11.5px;">官方预设</span>';

        const tr = document.createElement("tr");
        tr.innerHTML = `
            <td style="font-weight: 600; color: #FFFFFF;">
                <span title="Voice-ID: ${escapeHtml(v.id)}">${escapeHtml(v.name)}</span>
            </td>
            <td>${formatVoiceEngineBadge(v.provider_name)}</td>
            <td>${typeBadge}</td>
            <td style="font-family: var(--font-mono);">${(v.speech_speed || 1.0).toFixed(2)}x</td>
            <td style="white-space: nowrap; text-align: right;">
                <div class="table-actions" style="justify-content: flex-end;">
                    <button class="btn btn-xs" onclick="previewVoice('${v.id}')" title="在线合成试听此音色">
                        ${typeof svg === "function" ? svg("play", "icon-sm") : "▶"} 试听
                    </button>
                    <button class="btn btn-xs" onclick="editVoice('${v.id}')" title="修改音色名称">
                        改名
                    </button>
                    <button class="btn btn-xs btn-danger" onclick="deleteVoice('${v.id}', '${escapeHtml(v.name)}')" title="从音色资产库移除">
                        删除
                    </button>
                </div>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

// 主播管理页音色下拉框按当前生效的语音合成引擎精准填充
async function populateAnchorVoiceSelect(voices) {
    const anchorVoiceSel = document.getElementById("anchor-voice");
    if (!anchorVoiceSel) return;

    // 1. 确保全局 API 配置已就绪，实时获取当前默认生效的 TTS 语音引擎
    if (typeof cachedAllConfigs === "undefined" || !Array.isArray(cachedAllConfigs) || cachedAllConfigs.length === 0) {
        try {
            const cfgRes = await fetch(`${API_BASE}/settings/configs`);
            const cfgJson = await cfgRes.json();
            if (cfgJson.code === 0 && Array.isArray(cfgJson.data)) {
                cachedAllConfigs = cfgJson.data;
            }
        } catch (errCfg) {
            console.warn("未能实时获取 TTS 生效配置:", errCfg);
        }
    }

    let activeEngine = "";
    let activeEngineLabel = "";

    if (typeof cachedAllConfigs !== "undefined" && Array.isArray(cachedAllConfigs)) {
        const activeTTSCfg = cachedAllConfigs.find(c => c.config_group === "tts" && c.is_active);
        if (activeTTSCfg) {
            const pMeta = (typeof resolveTTSProviderMeta === "function") ? resolveTTSProviderMeta(activeTTSCfg.provider_name, activeTTSCfg) : null;
            activeEngine = pMeta ? pMeta.id : (activeTTSCfg.provider_name || "").toLowerCase();
            activeEngineLabel = pMeta ? pMeta.name : (activeEngine.includes("cosy") ? "CosyVoice" : activeTTSCfg.provider_name);
        }
    }

    // 若无明确激活项，取配置列表中的首个 TTS 项，最后才以 Edge-TTS 兜底
    if (!activeEngine) {
        const anyTTS = (typeof cachedAllConfigs !== "undefined" && Array.isArray(cachedAllConfigs))
            ? cachedAllConfigs.find(c => c.config_group === "tts")
            : null;
        if (anyTTS) {
            const pMeta = (typeof resolveTTSProviderMeta === "function") ? resolveTTSProviderMeta(anyTTS.provider_name, anyTTS) : null;
            activeEngine = pMeta ? pMeta.id : (anyTTS.provider_name || "").toLowerCase();
            activeEngineLabel = pMeta ? pMeta.name : (activeEngine.includes("cosy") ? "CosyVoice" : anyTTS.provider_name);
        } else {
            activeEngine = "moss_tts_nano";
            activeEngineLabel = "MOSS-TTS-Nano";
        }
    }

    // 2. 根据当前语音引擎过滤音色
    const engineVoices = (voices || []).filter(v => {
        const vp = (v.provider_name || "").toLowerCase();
        if (activeEngine.includes("moss") || activeEngine.includes("nano")) {
            return vp.includes("moss") || vp.includes("nano") || (v.id && v.id.includes("moss"));
        } else if (activeEngine.includes("cosy")) {
            return vp.includes("cosy") || (v.id && (v.id.includes("bailian") || v.id.includes("cosyvoice")));
        } else if (activeEngine.includes("eleven")) {
            return vp.includes("eleven");
        } else if (vp) {
            return vp === activeEngine;
        }
        return true;
    });

    const curSelected = anchorVoiceSel.value;
    anchorVoiceSel.innerHTML = `<option value="">未绑定音色 (开播采用默认发音)</option>`;

function formatVoiceDisplayName(v) {
    if (!v) return "";
    const name = String(v.name || v.id || "").trim();
    // 判断名称是否已有明确的性别或人设说明括号
    const hasExplicitDesc = /[\(（].*?(女|男|童|妹|姐|叔|哥|少女|主播).*?[\)）]/.test(name);
    if (hasExplicitDesc) {
        return name;
    }

    // 智能推导性别 (优先读取后端下发的 gender，其次本地特征推导)
    let g = (v.gender || "").toLowerCase();
    if (!g || g === "unknown") {
        const lower = `${name} ${v.id || ''}`.toLowerCase();
        if (/女|girl|female|woman|少女|知性|萌音|姐|妹|娘|春|夏|婉|悦|玲|stella|bella|xiaoxiao|xiaoyi/.test(lower)) {
            g = "female";
        } else if (/男|boy|male|man|老铁|叔|哥|爷|诚|华|硕|渊|飞|杰|天|平|yunjian|yunxi/.test(lower)) {
            g = "male";
        }
    }

    if (g === "female") {
        return `${name} (女声)`;
    } else if (g === "male") {
        return `${name} (男声)`;
    }
    return name;
}

    // 若当前引擎下有音色，以分组方式填充
    const listToRender = engineVoices.length > 0 ? engineVoices : (voices || []);
    const clones = listToRender.filter(v => v.voice_type === "cloned" || v.is_clone);
    const presets = listToRender.filter(v => !(v.voice_type === "cloned" || v.is_clone));

    if (clones.length > 0) {
        const grpClone = document.createElement("optgroup");
        grpClone.label = "👑 专属声音克隆资产";
        clones.forEach(v => {
            const opt = document.createElement("option");
            opt.value = v.id;
            opt.innerText = `👑 ${formatVoiceDisplayName(v)} (专属克隆)`;
            grpClone.appendChild(opt);
        });
        anchorVoiceSel.appendChild(grpClone);
    }

    if (presets.length > 0) {
        const grpPreset = document.createElement("optgroup");
        grpPreset.label = `🎙️ ${activeEngineLabel} 官方预设音色`;
        presets.forEach(v => {
            const opt = document.createElement("option");
            opt.value = v.id;
            opt.innerText = `🎙️ ${formatVoiceDisplayName(v)}`;
            grpPreset.appendChild(opt);
        });
        anchorVoiceSel.appendChild(grpPreset);
    }

    // 维持原有选中状态
    if (curSelected) {
        anchorVoiceSel.value = curSelected;
    }

    // 提示当前绑定的所属引擎
    const hintEl = document.getElementById("anchor-voice-engine-hint");
    if (hintEl) {
        hintEl.innerHTML = `<span style="color: var(--accent-emerald); font-size: 11px;">💡 当前直播语音引擎: <strong>${escapeHtml(activeEngineLabel)}</strong> (下拉仅列出该引擎的音色资产)</span>`;
    }
}

async function loadVoiceTable() {
    try {
        const res = await fetch(`${API_BASE}/voices/list`);
        const json = await res.json();
        if (json.code !== 0) return;
        voiceCache = json.data || [];
        window._voiceProfilesCache = voiceCache;

        // 1. 渲染音色资产表格
        renderVoiceTableRows();

        // 2. 联动主播管理页的音色下拉（按当前生效引擎过滤）
        await populateAnchorVoiceSelect(voiceCache);
    } catch (e) {
        console.error("加载音色失败", e);
    }
}

// 在线试听：页内浮动播放器 (不新开标签页，即点即听)
let previewAudioEl = null;
async function previewVoice(voiceId) {
    if (!voiceId) return;
    if (window._ttsPreviewController && typeof window._ttsPreviewController.stopAll === "function") {
        window._ttsPreviewController.stopAll();
    }
    if (previewAudioEl) {
        try {
            previewAudioEl.pause();
            previewAudioEl.src = "";
        } catch (e) { /* 忽略 */ }
    }

    const audioUrl = `${API_BASE}/voices/${voiceId}/preview?t=${Date.now()}`;
    if (typeof showToast === "function") {
        showToast("正在加载试听音频...", "info", 1200);
    }

    try {
        const resp = await fetch(audioUrl, { method: "GET" });
        if (!resp.ok) {
            let errorMsg = `服务返回 HTTP ${resp.status}`;
            try {
                const errData = await resp.json();
                if (errData && errData.detail) errorMsg = errData.detail;
            } catch (_) {}
            if (typeof showToast === "function") {
                showToast(`试听失败: ${errorMsg}`, "warning", 5000);
            }
            return;
        }

        const blob = await resp.blob();
        const blobUrl = URL.createObjectURL(blob);
        previewAudioEl = new Audio(blobUrl);
        if (window._ttsPreviewController) {
            window._ttsPreviewController.audio = previewAudioEl;
        }

        previewAudioEl.onended = () => {
            URL.revokeObjectURL(blobUrl);
        };

        const playPromise = previewAudioEl.play();
        if (playPromise !== undefined) {
            playPromise.then(() => {
                if (typeof showToast === "function") showToast("正在播放声音样本...", "info", 2000);
            }).catch(err => {
                console.warn("试听播放异常:", err);
                if (err && err.name === "NotAllowedError") {
                    alert("浏览器阻止了自动播放，请先点击页面任意位置激活音频权限，再点一次试听");
                } else {
                    if (typeof showToast === "function") {
                        showToast("音频解码失败，请确认音频格式完整", "warning", 3000);
                    }
                }
            });
        }
    } catch (netErr) {
        console.warn("试听网络请求异常:", netErr);
        if (typeof showToast === "function") {
            showToast("网络请求异常，无法加载试听音频", "error", 3000);
        }
    }
}

async function editVoice(voiceId) {
    const v = voiceCache.find(x => x.id === voiceId);
    if (!v) return;
    const newName = prompt(`请输入音色【${v.name}】的新名称:`, v.name);
    if (newName === null) return;
    const cleanName = newName.trim();
    if (!cleanName) {
        if (typeof showToast === "function") showToast("音色名称不能为空", "warning");
        return;
    }
    try {
        const res = await fetch(`${API_BASE}/voices/update`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ id: voiceId, name: cleanName })
        });
        const json = await res.json();
        if (json.code === 0) {
            if (typeof showToast === "function") showToast(`已成功将音色重命名为【${cleanName}】`, "success");
            await loadVoiceTable();
            // 联动刷新已配置的 TTS 引擎卡片，立即呈现新音色名
            if (typeof renderConfiguredTTS === "function" && typeof cachedAllConfigs !== "undefined") {
                renderConfiguredTTS(cachedAllConfigs);
            }
        } else {
            alert("修改失败: " + (json.detail || json.message));
        }
    } catch (e) {
        alert("改名异常: " + e);
    }
}

async function deleteVoice(voiceId, name) {
    if (!confirm(`确认删除音色【${name}】？`)) return;
    try {
        const res = await fetch(`${API_BASE}/voices/${voiceId}`, { method: "DELETE" });
        const json = await res.json();
        if (json.code === 0) loadVoiceTable();
    } catch (e) { alert("删除失败: " + e); }
}

function getBuiltinLLMEcosystem() {
    if (typeof BUILTIN_LLM_ECOSYSTEM !== "undefined" && Array.isArray(BUILTIN_LLM_ECOSYSTEM)) {
        return BUILTIN_LLM_ECOSYSTEM;
    }
    if (window.BUILTIN_LLM_ECOSYSTEM && Array.isArray(window.BUILTIN_LLM_ECOSYSTEM)) {
        return window.BUILTIN_LLM_ECOSYSTEM;
    }
    return [];
}

function renderLLMEcosystemGrid(selectedId = "deepseek") {
    const grid = document.getElementById("llm-ecosystem-grid");
    if (!grid) return;
    grid.innerHTML = "";
    grid.style.display = "grid";
    grid.style.gridTemplateColumns = "repeat(8, minmax(0, 1fr))";
    grid.style.gap = "10px";
    grid.style.marginBottom = "22px";
    grid.style.width = "100%";

    const list = getBuiltinLLMEcosystem();
    list.forEach(item => {
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
    const list = getBuiltinLLMEcosystem();

    // 1. 若显式传入有效的具体品牌 ID（且不是 custom / openai_compatible 等通用词），直接命中
    if (rawId && rawId !== "custom" && rawId !== "openai_compatible" && rawId !== "local_ollama") {
        const hit = list.find(p => p.id === rawId);
        if (hit) return hit;
    }

    // 2. 根据特征词或配置对象的 base_url / model_name / provider_name 智能深度识别品牌
    const pName = (config && config.provider_name ? config.provider_name : rawId).toLowerCase();
    const bUrl = (config && config.base_url ? config.base_url : "").toLowerCase();
    const mName = (config && config.model_name ? config.model_name : "").toLowerCase();
    const combined = `${pName} ${bUrl} ${mName}`;

    if (combined.includes("deepseek")) {
        return list.find(p => p.id === "deepseek");
    }
    if (combined.includes("qwen") || combined.includes("dashscope") || combined.includes("aliyun")) {
        return list.find(p => p.id === "qwen");
    }
    if (combined.includes("minimax")) {
        return list.find(p => p.id === "minimax");
    }
    if (combined.includes("kimi") || combined.includes("moonshot")) {
        return list.find(p => p.id === "kimi");
    }
    if (combined.includes("gemini") || combined.includes("generativelanguage") || combined.includes("googleapis")) {
        return list.find(p => p.id === "gemini");
    }
    if (combined.includes("glm") || combined.includes("zhipu") || combined.includes("bigmodel")) {
        return list.find(p => p.id === "glm");
    }
    if (combined.includes("chatgpt") || combined.includes("openai.com") || (combined.includes("gpt-") && !combined.includes("deepseek"))) {
        return list.find(p => p.id === "chatgpt");
    }

    // 3. 若均无法匹配具体特征，但显式指定了 custom，返回 custom
    if (rawId === "custom") {
        return list.find(p => p.id === "custom") || list[0];
    }

    // 4. 兜底策略：若未识别且非空配置，优先返回首位 DeepSeek 或 custom
    return list.find(p => p.id === "deepseek") || list[0];
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
        id: "moss_tts_nano",
        name: "MOSS-TTS-Nano",
        tagline: "系统自带 · 端侧神经克隆",
        officialUrl: "",
        officialAction: "",
        getKeyUrl: "",
        cardDocTitle: "",
        brandColor: "#0284C7",
        logoSvg: "/static/svg/tts_moss.svg",
        defaultBaseUrl: "http://127.0.0.1:9880",
        placeholderUrl: "系统原生内置，无需配置 Base URL",
        needKey: false,
        needUrl: false,
        recommendedVoices: [
            { text: "官方预置清亮女主播 (广播级 48kHz)", val: "moss_female_host_01" },
            { text: "官方预置阳光男主播 (活力亲和)", val: "moss_male_host_02" },
            { text: "温柔知性女主播 (服饰生活)", val: "moss_female_warm_03" },
            { text: "活力带货女主播 (食品零食)", val: "moss_female_lively_04" }
        ],
        desc: "基于复旦团队开源 MOSS-TTS-Nano 深度融合。仅 ~100M 参数、~500MB 显存开销，端侧极速生成 48kHz 广播级真人语音。优先调度本地或云端 GPU 进行极速零样本声音克隆，亦支持无显卡轻量运行。",
        urlPills: [
            { text: "⚡ 本地推理端点 (127.0.0.1:9880)", val: "http://127.0.0.1:9880", needKey: false }
        ]
    },
    {
        id: "cosyvoice",
        name: "CosyVoice",
        tagline: "官方大模型原生端点 · 极速流式 WebSocket",
        officialUrl: "https://bailian.console.aliyun.com/cn-beijing/model/experience/voice/sound-cloning",
        officialAction: "百炼声音复刻中心 ↗",
        getKeyUrl: "https://bailian.console.aliyun.com/",
        cardDocTitle: "前往阿里云百炼声音复刻中心 (在线录制/上传音频复刻并获取专属 Voice-ID)",
        brandColor: "#EA580C",
        logoSvg: "/static/svg/tts_cosyvoice.svg",
        defaultBaseUrl: "https://dashscope.aliyuncs.com/api/v1",
        placeholderUrl: "选择云端商用 API (填Key即用) 或 本地自建推理端口 (如 http://127.0.0.1:9233)",
        needKey: true,
        needUrl: true,
        recommendedVoices: [
            { text: "龙小春 (知性女声 · 电商带货推荐)", val: "longxiaochun" },
            { text: "龙小白 (清澈邻家 · 治愈少女)", val: "longxiaobai" },
            { text: "龙小夏 (元气少女 · 语音助手)", val: "longxiaoxia" },
            { text: "龙小诚 (阳光男声 · 沉稳专业)", val: "longxiaocheng" },
            { text: "龙老铁 (东北老铁 · 互动爆款)", val: "longlaotie" },
            { text: "龙婉 (温和对话 · 亲切邻家)", val: "longwan" },
            { text: "龙书 (磁性叙事 · 情感故事)", val: "longshu" },
            { text: "龙悦 (文雅舒缓 · 品质解说)", val: "longyue" },
            { text: "龙安冲 (活力带货 · 食品零食)", val: "longanchong" },
            { text: "龙安然 (燃播带货 · 激情促单)", val: "longanran" },
            { text: "龙安萱 (亲和带货 · 美妆日用)", val: "longanxuan" },
            { text: "龙安平 (科技沉稳 · 数码家电)", val: "longanping" },
            { text: "龙硕 (质感男声 · 品牌带货)", val: "longshuo" },
            { text: "杰力豆 (活泼童声 · 母婴玩具)", val: "longjielidou" },
            { text: "龙橙 (阳光朝气 · 青春男声)", val: "longcheng" },
            { text: "龙华 (成熟稳重 · 商务男声)", val: "longhua" },
            { text: "龙静 (文雅解说 · 舒缓女声)", val: "longjing" },
            { text: "Stella (自然解说 · 品质女主播)", val: "loongstella" },
            { text: "Bella (温柔知性 · 服饰带货)", val: "loongbella" }
        ],
        desc: "阿里通义开源大模型语音合成。商业云端调用推荐使用【阿里云百炼平台】开通账号并创建 API Key（Base URL 为 https://dashscope.aliyuncs.com/api/v1 或您的百炼专属服务端点），亦支持本地或局域网私有化 GPU 部署。",
        urlPills: []
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
    }
];
window.BUILTIN_TTS_ECOSYSTEM = BUILTIN_TTS_ECOSYSTEM;

// 智能解析当前应当选中的 TTS 引擎与配置
// 严格原则：
// 1. 优先以用户激活的配置为准 (is_active === true)
// 2. 若无激活项但用户已配置过 TTS，以已有配置为准 (首选条目)
// 3. 若用户没有任何配置，才默认回退选中本地引擎 (moss_tts_nano)
function resolveActiveOrPreferredTTSProvider(configs = null) {
    const list = configs || (typeof cachedAllConfigs !== "undefined" ? cachedAllConfigs : []) || [];
    const ttsConfigs = list.filter(c => c && c.config_group === "tts");

    // 1. 优先以用户激活的配置为准
    const activeCfg = ttsConfigs.find(c => Boolean(c.is_active));
    if (activeCfg) {
        const meta = resolveTTSProviderMeta(activeCfg.provider_name, activeCfg);
        return { providerId: meta.id, config: activeCfg, reason: "active_config" };
    }

    // 2. 如果用户已有保存的 TTS 配置
    if (ttsConfigs.length > 0) {
        const preferredCfg = ttsConfigs[0];
        const meta = resolveTTSProviderMeta(preferredCfg.provider_name, preferredCfg);
        return { providerId: meta.id, config: preferredCfg, reason: "existing_config" };
    }

    // 3. 用户完全没有配置，默认选中本地引擎 moss_tts_nano
    return { providerId: "moss_tts_nano", config: null, reason: "fallback_local" };
}
window.resolveActiveOrPreferredTTSProvider = resolveActiveOrPreferredTTSProvider;

function renderTTSEcosystemGrid(selectedId = null) {
    const grid = document.getElementById("tts-ecosystem-grid");
    if (!grid) return;

    // 若未显式传入 selectedId，优先根据用户配置动态决议
    let targetId = selectedId;
    if (!targetId) {
        if (typeof currentSelectedTTSProvider !== "undefined" && currentSelectedTTSProvider) {
            targetId = currentSelectedTTSProvider;
        } else {
            const resolved = resolveActiveOrPreferredTTSProvider();
            targetId = resolved.providerId;
        }
    }

    grid.innerHTML = "";

    const ttsList = (typeof BUILTIN_TTS_ECOSYSTEM !== "undefined" && Array.isArray(BUILTIN_TTS_ECOSYSTEM))
        ? BUILTIN_TTS_ECOSYSTEM
        : (window.BUILTIN_TTS_ECOSYSTEM || []);

    ttsList.forEach(item => {
        const card = document.createElement("div");
        card.className = "tts-provider-card" + (item.id === targetId ? " selected" : "");
        card.setAttribute("data-provider", item.id);
        card.onclick = () => selectTTSProvider(item.id);

        const logoSrc = (item.logoSvg || "").startsWith("http") ? item.logoSvg : (item.logoSvg + "?v=20260918_v1");
        const officialLinkHtml = item.officialUrl ? `
                <a href="${item.officialUrl}" target="_blank" rel="noopener noreferrer" class="tts-card-official-link" title="${item.cardDocTitle}" onclick="event.stopPropagation();">
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path>
                        <polyline points="15 3 21 3 21 9"></polyline>
                        <line x1="10" y1="14" x2="21" y2="3"></line>
                    </svg>
                </a>` : "";
        card.innerHTML = `
                ${officialLinkHtml}
                <img src="${logoSrc}" class="tts-provider-logo" alt="${item.name}">
                <div class="tts-provider-name" title="${item.name}">${item.name}</div>
                <div class="tts-provider-tagline" title="${item.tagline}">${item.tagline}</div>
            `;
        grid.appendChild(card);
    });
    refreshTTSHardwareStatus();
}

async function refreshTTSHardwareStatus() {
    const pill = document.getElementById("tts-hw-status-pill");
    if (!pill) return;
    try {
        const res = await fetch(`${API_BASE}/live/hardware`);
        const json = await res.json();
        if (json.code === 0 && json.data) {
            const cap = json.data.gpu_capability || {};
            const localGpu = cap.local_gpu || json.data.gpu || {};
            if (cap.use_cloud) {
                pill.innerHTML = `⚡ 算力状态: 已连接远端 GPU 加速 (${escapeHtml(cap.cloud_gpu?.provider_name || '云端节点')})`;
                pill.style.color = "#38bdf8";
            } else if (cap.can_execute && !cap.is_low_spec_local) {
                pill.innerHTML = `🟢 算力状态: 本机 ${escapeHtml(localGpu.gpu_name || '独显')} (显存 ${localGpu.vram_total_gb || 0}GB 充足)`;
                pill.style.color = "#34d399";
            } else {
                pill.innerHTML = `🟡 算力状态: 本地显存不足，MOSS-TTS 自动走 CPU 极速多线程 (稳定保底)`;
                pill.style.color = "#fbbf24";
            }
        }
    } catch (_) {}
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

    if (combined.includes("moss") || combined.includes("nano")) return BUILTIN_TTS_ECOSYSTEM.find(p => p.id === "moss_tts_nano");
    if (combined.includes("cosy")) return BUILTIN_TTS_ECOSYSTEM.find(p => p.id === "cosyvoice");
    if (combined.includes("eleven")) return BUILTIN_TTS_ECOSYSTEM.find(p => p.id === "elevenlabs");

    return BUILTIN_TTS_ECOSYSTEM[0];
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
    const isMoss = currentSelectedTTSProvider === "moss_tts_nano";

    const isCloud = explicitNeedKey === true || (!isLocal && !isMoss && (trimmed.startsWith("https://") || trimmed.includes("dashscope") || trimmed.includes("siliconflow") || trimmed.includes("elevenlabs") || trimmed.includes("api.")));

    const curMeta = BUILTIN_TTS_ECOSYSTEM.find(p => p.id === currentSelectedTTSProvider) || BUILTIN_TTS_ECOSYSTEM[0];

    if (isMoss) {
        if (hintEl) hintEl.innerText = "系统自带 · 免密钥开箱即用";
        keyInput.placeholder = "系统原生自带引擎，免 API 密钥，优先本地/云端 GPU 加速";
        if (getKeyLink) getKeyLink.style.display = "none";
    } else if (isCloud) {
        if (hintEl) hintEl.innerText = "云端商用托管 (需硬件安全加密密钥)";
        keyInput.placeholder = "输入云服务商 API 密钥 (如 sk-xxxx)";
        if (getKeyLink) {
            let targetUrl = curMeta.getKeyUrl || curMeta.officialUrl;
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
async function selectTTSProvider(providerId, existingConfig = null) {
    const meta = resolveTTSProviderMeta(providerId, existingConfig);
    currentSelectedTTSProvider = meta.id;
    window.currentSelectedTTSProvider = meta.id;

    if (!existingConfig && cachedAllConfigs && cachedAllConfigs.length > 0) {
        // 优先匹配当前已激活的该引擎配置
        let found = cachedAllConfigs.find(c => {
            if (c.config_group !== "tts") return false;
            const cMeta = resolveTTSProviderMeta(c.provider_name, c);
            return cMeta.id === meta.id && Boolean(c.is_active);
        });
        if (!found) {
            found = cachedAllConfigs.find(c => {
                if (c.config_group !== "tts") return false;
                const cMeta = resolveTTSProviderMeta(c.provider_name, c);
                return cMeta.id === meta.id;
            });
        }
        if (found) existingConfig = found;
    }

    // 高亮品牌卡片
    document.querySelectorAll(".tts-provider-card").forEach(c => {
        c.classList.toggle("selected", c.getAttribute("data-provider") === meta.id);
    });

    // 0 毫秒立即同步刷新底层第三方生效模型与通道协议视窗，彻底杜绝数据穿杂
    renderTTSThirdpartyModelInfoDirect(meta.id);

    // 编辑面板头部
    const logoEl = document.getElementById("tts-editor-logo");
    const titleEl = document.getElementById("tts-editor-title");
    const badgeEl = document.getElementById("tts-editor-badge");
    const descEl = document.getElementById("tts-editor-desc");
    const providerInput = document.getElementById("tts-editor-provider");
    const configIdInput = document.getElementById("tts-editor-config-id");

    const officialLinkEl = document.getElementById("tts-editor-official-link");

    if (logoEl) logoEl.src = (meta.logoSvg || "").startsWith("http") ? meta.logoSvg : (meta.logoSvg + "?v=20260917_v6");
    if (titleEl) titleEl.innerText = `配置 ${meta.name}`;
    if (badgeEl) badgeEl.innerText = (meta.tagline || "").includes("·") ? meta.tagline.split("·")[0].trim() : (meta.tagline || "官方推荐");
    if (descEl) descEl.innerText = meta.desc || "";
    if (providerInput) providerInput.value = meta.id;
    if (configIdInput) configIdInput.value = existingConfig ? existingConfig.id : "";
    if (officialLinkEl) {
        if (meta.officialUrl && meta.id !== "moss_tts_nano") {
            officialLinkEl.style.display = "inline-flex";
            officialLinkEl.href = meta.officialUrl;
            officialLinkEl.title = meta.cardDocTitle || `前往 ${meta.name} 官方控制台 (在新标签页打开)`;
            const spanEl = officialLinkEl.querySelector("span");
            if (spanEl) {
                spanEl.innerText = meta.officialAction || "官方控制台 ↗";
            }
        } else {
            officialLinkEl.style.display = "none";
        }
    }

    // 第一行 Base URL (双通道支持)
    const urlGroupEl = document.getElementById("tts-url-form-group");
    const keyGroupEl = document.getElementById("tts-key-form-group");
    if (meta.id === "moss_tts_nano") {
        if (urlGroupEl) urlGroupEl.style.display = "none";
        if (keyGroupEl) keyGroupEl.style.display = "none";
    } else {
        if (urlGroupEl) urlGroupEl.style.display = "";
        if (keyGroupEl) keyGroupEl.style.display = "";
    }

    const urlInput = document.getElementById("tts-input-url");
    const currentBaseUrl = existingConfig ? (existingConfig.base_url || meta.defaultBaseUrl) : meta.defaultBaseUrl;
    if (urlInput) {
        urlInput.value = currentBaseUrl || "";
        urlInput.placeholder = meta.placeholderUrl || (meta.needUrl ? "请输入服务 Base URL 地址" : "云端直接调用，无需填写 Base URL");
        urlInput.oninput = () => syncTTSKeyStatusByUrl(urlInput.value);
    }

    const urlPillsBox = document.getElementById("tts-editor-url-pills");
    if (urlPillsBox) {
        if (meta.urlPills && meta.urlPills.length > 0) {
            urlPillsBox.innerHTML = `
                <span style="font-size: 11px; color: var(--text-muted);">快捷端点:</span>
                ${meta.urlPills.map(p => `<span class="quick-pill" onclick="fillTTSBaseUrlAndSyncKey('${p.val}', ${p.needKey})">${p.text}</span>`).join("")}
            `;
            urlPillsBox.style.display = "";
        } else {
            urlPillsBox.innerHTML = "";
            urlPillsBox.style.display = "none";
        }
    }

    // 第二行 API Key：切换引擎时立即重置输入框，严格隔离各引擎密钥，杜绝跨引擎混入
    const keyInput = document.getElementById("tts-input-key");
    if (keyInput) {
        keyInput.value = "";
        keyInput.type = "password";
    }
    syncTTSKeyStatusByUrl(currentBaseUrl, meta.needKey);
    await autoFillRealTTSKeyIfConfigured(existingConfig ? existingConfig.id : null, existingConfig, meta);

    // 专属开通指引提示卡（如阿里云百炼 CosyVoice / Edge-TTS 等）
    const guideBox = document.getElementById("tts-provider-guide-box");
    if (guideBox) {
        if (meta.id === "cosyvoice") {
            guideBox.innerHTML = `
                <div style="background: rgba(15, 23, 42, 0.75); border: 1px solid rgba(234, 88, 12, 0.35); border-radius: 8px; padding: 12px 15px; font-size: 12.5px; color: #cbd5e1; line-height: 1.85;">
                    <div style="font-weight: 700; color: #fb923c; margin-bottom: 8px; display: flex; align-items: center; gap: 7px;">
                        <svg viewBox="0 0 24 24" style="width: 15px; height: 15px; stroke: #fb923c; fill: none; stroke-width: 2.2;"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
                        阿里云百炼 · CosyVoice 快速开通与发声指南：
                    </div>
                    <div style="display: flex; flex-direction: column; gap: 5px; padding-left: 2px;">
                        <div>1. 打开 <a href="https://bailian.console.aliyun.com/cn-beijing/model/experience/voice/sound-cloning" target="_blank" rel="noopener noreferrer" style="color: #38bdf8; text-decoration: underline; font-weight: 600;">阿里云百炼声音复刻中心 ↗</a>；</div>
                        <div>2. 上传或录制 10~20 秒清晰人声，生成您的专属 <strong>Voice-ID</strong>；</div>
                        <div>3. 点击左侧 <strong>API-Key</strong> 创建并复制您的百炼 API Key 和 OpenAI 兼容地址（Base URL）；</div>
                        <div>4. 回到中控台填写并登记 Voice-ID，系统将通过官方 WebSocket 协议由大模型实时发声！</div>
                    </div>
                </div>
            `;
            guideBox.style.display = "block";
        } else if (meta.id === "elevenlabs") {
            guideBox.innerHTML = `
                <div style="background: rgba(15, 23, 42, 0.65); border: 1px solid rgba(244, 63, 94, 0.35); border-radius: 8px; padding: 12px 15px; font-size: 12.5px; color: #cbd5e1; line-height: 1.8;">
                    <div style="font-weight: 700; color: #fb7185; margin-bottom: 6px; display: flex; align-items: center; gap: 7px;">
                        <svg viewBox="0 0 24 24" style="width: 15px; height: 15px; stroke: #fb7185; fill: none; stroke-width: 2.2;"><path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>
                        ElevenLabs 全球顶级情感语音服务：
                    </div>
                    <div style="display: flex; flex-direction: column; gap: 4px; padding-left: 2px;">
                        <div>1. 前往 <a href="https://elevenlabs.io/app/developers/api-keys" target="_blank" rel="noopener noreferrer" style="color: #38bdf8; text-decoration: underline; font-weight: 600;">ElevenLabs 开发者控制台 ↗</a> 开通并创建 API Key；</div>
                        <div>2. 粘贴 API Key 后点击上方「获取音色与模型」，系统将自动拉取官方丰富影视级音色库。</div>
                    </div>
                </div>
            `;
            guideBox.style.display = "block";
        } else {
            guideBox.innerHTML = "";
            guideBox.style.display = "none";
        }
    }

    // 第五行：初始化具体音色与选定状态（自动拉取专属克隆音色与引擎推荐音色，克隆置顶）
    const voiceInput = document.getElementById("tts-input-voice");
    const pingStatusEl = document.getElementById("tts-ping-latency-status");
    if (pingStatusEl) pingStatusEl.innerHTML = `握手测试: 未测试`;

    const selectedVoiceVal = (existingConfig && existingConfig.model_name) ? existingConfig.model_name : "";
    if (voiceInput && selectedVoiceVal) voiceInput.value = selectedVoiceVal;

    // 自动拉取克隆音色库与引擎预设音色并即时呈现
    fetchAndRenderTTSVoices(meta, selectedVoiceVal);

    const testResultBox = document.getElementById("tts-test-result");
    if (testResultBox) testResultBox.style.display = "none";

    // 自动通过 API 探测/拉取第三方服务商底层生效模型与协议
    const cachedKey = existingConfig ? (cachedDecryptedKeys[existingConfig.id] || "") : "";
    fetchAndRenderTTSThirdpartyModelInfo(currentBaseUrl, cachedKey, meta.id, existingConfig ? existingConfig.id : "");

    // 联动更新专属声音克隆定制工作台
    updateTTSCloneWorkbenchUI(meta.id);

    // 强化保障：无论如何自动确保 Base URL 与解密 API Key 填入输入框
    await ensureTTSKeyAndUrlFilled(meta.id, existingConfig);
}

// 确保输入框自动填入端点与解密后的 API Key
async function ensureTTSKeyAndUrlFilled(providerId, existingConfig = null) {
    const meta = resolveTTSProviderMeta(providerId, existingConfig);
    const urlInput = document.getElementById("tts-input-url");
    const keyInput = document.getElementById("tts-input-key");

    // 1. 自动填入 Base URL
    let targetUrl = (existingConfig && existingConfig.base_url) ? existingConfig.base_url : meta.defaultBaseUrl;
    if (urlInput && targetUrl) {
        urlInput.value = targetUrl;
        syncTTSKeyStatusByUrl(targetUrl, meta.needKey);
    }

    // 2. 自动填入真实解密后的 API Key
    if (keyInput) {
        await autoFillRealTTSKeyIfConfigured(existingConfig ? existingConfig.id : null, existingConfig, meta);
    }
}

// 切换声音克隆工作台的 Tab (上传克隆 vs 登记ID)
function switchTTSCloneTab(tab) {
    const tabUploadBtn = document.getElementById("btn-clone-tab-upload");
    const tabBindBtn = document.getElementById("btn-clone-tab-bind");
    const panelUpload = document.getElementById("tts-clone-panel-upload");
    const panelBind = document.getElementById("tts-clone-panel-bind");

    if (tab === "bind") {
        if (tabUploadBtn) tabUploadBtn.classList.remove("active");
        if (tabBindBtn) tabBindBtn.classList.add("active");
        if (panelUpload) panelUpload.style.display = "none";
        if (panelBind) panelBind.style.display = "block";
    } else {
        if (tabUploadBtn) tabUploadBtn.classList.add("active");
        if (tabBindBtn) tabBindBtn.classList.remove("active");
        if (panelUpload) panelUpload.style.display = "block";
        if (panelBind) panelBind.style.display = "none";
    }
}

// 依据当前选中的 TTS 引擎更新克隆工作台状态
function updateTTSCloneWorkbenchUI(providerId) {
    const pId = (providerId || currentSelectedTTSProvider || "moss_tts_nano").toLowerCase();
    const targetNameEl = document.getElementById("tts-clone-target-engine-name");
    const badgeEl = document.getElementById("tts-clone-engine-badge");
    const noticeEl = document.getElementById("tts-clone-unsupported-notice");
    const panelUpload = document.getElementById("tts-clone-panel-upload");
    const panelBind = document.getElementById("tts-clone-panel-bind");
    const tipEl = document.getElementById("tts-clone-upload-tip");
    const tabUploadBtn = document.getElementById("btn-clone-tab-upload");
    const tabBindBtn = document.getElementById("btn-clone-tab-bind");

    const meta = resolveTTSProviderMeta(pId);
    if (targetNameEl && meta) targetNameEl.innerText = meta.name;

    const aliyunCard = document.getElementById("tts-clone-aliyun-card");
    const mossCard = document.getElementById("tts-clone-moss-card");

    if (badgeEl) {
        badgeEl.className = "brand-badge green";
        badgeEl.innerText = "支持专属声音克隆";
    }
    if (noticeEl) noticeEl.style.display = "none";

    if (pId === "moss_tts_nano") {
        if (mossCard) mossCard.style.display = "block";
        if (aliyunCard) aliyunCard.style.display = "none";
        if (tabBindBtn) tabBindBtn.style.display = "none";
        if (tabUploadBtn) {
            tabUploadBtn.style.display = "inline-block";
            tabUploadBtn.innerText = "🎙️ 本地上传/零样本克隆";
        }
        switchTTSCloneTab("upload");
        if (tipEl) {
            tipEl.innerText = "⚡ 上传 5~30 秒清晰人声 WAV/MP3，MOSS-TTS-Nano 将通过端侧 GPU 快速提取声学特征并完成零样本声音克隆";
        }
    } else if (pId === "cosyvoice") {
        if (mossCard) mossCard.style.display = "none";
        if (aliyunCard) aliyunCard.style.display = "block";
        if (tabBindBtn) {
            tabBindBtn.style.display = "inline-block";
            tabBindBtn.innerText = "🔗 登记百炼 Voice-ID (官方推荐)";
        }
        if (tabUploadBtn) {
            tabUploadBtn.style.display = "inline-block";
            tabUploadBtn.innerText = "🎙️ 本地上传/公网音频复刻";
        }
        const isBindActive = tabBindBtn && tabBindBtn.classList.contains("active");
        if (panelUpload) panelUpload.style.display = isBindActive ? "none" : "block";
        if (panelBind) panelBind.style.display = isBindActive ? "block" : "none";
        if (tipEl) {
            tipEl.innerText = "⚡ 上传 120秒内清晰音频，系统将针对阿里云 CosyVoice (cosyvoice-v3.5-flash) 创建专属克隆声线";
        }
    } else if (pId === "elevenlabs") {
        if (mossCard) mossCard.style.display = "none";
        if (aliyunCard) aliyunCard.style.display = "none";
        if (tabBindBtn) tabBindBtn.style.display = "none";
        if (tabUploadBtn) {
            tabUploadBtn.style.display = "inline-block";
            tabUploadBtn.innerText = "🎙️ 本地上传音频克隆";
        }
        switchTTSCloneTab("upload");
        if (tipEl) {
            tipEl.innerText = "⚡ 上传音频将通过 ElevenLabs Instant Voice Cloning 官方接口创建电影级克隆音色";
        }
    } else {
        if (mossCard) mossCard.style.display = "none";
        if (aliyunCard) aliyunCard.style.display = "none";
        if (tabUploadBtn) tabUploadBtn.style.display = "inline-block";
        if (tabBindBtn) tabBindBtn.style.display = "none";
        switchTTSCloneTab("upload");
        if (tipEl) {
            tipEl.innerText = "⚡ 点击克隆后将自动生成声纹档案，并即刻加入上方音色栏供试听与开播";
        }
    }
}

// 执行本地音频一键克隆
async function handleExecuteTTSClone() {
    const btn = document.getElementById("btn-execute-clone");
    const nameInput = document.getElementById("tts-clone-voice-name");
    const fileInput = document.getElementById("tts-clone-voice-file");
    const speedInput = document.getElementById("tts-clone-speed");

    const voiceName = nameInput ? nameInput.value.trim() : "";
    if (!voiceName) {
        if (typeof showToast === "function") showToast("请输入克隆音色名称", "warning");
        if (nameInput) nameInput.focus();
        return;
    }

    if (!fileInput || !fileInput.files || fileInput.files.length === 0) {
        if (typeof showToast === "function") showToast("请选择一段 120秒内清晰人声 WAV/MP3 音频文件", "warning");
        return;
    }

    const file = fileInput.files[0];
    const speed = speedInput ? parseFloat(speedInput.value) || 1.0 : 1.0;

    const urlInput = document.getElementById("tts-input-url");
    const keyInput = document.getElementById("tts-input-key");
    const providerInput = document.getElementById("tts-editor-provider");
    const activeModelEl = document.getElementById("tts-thirdparty-active-model");

    const baseUrl = urlInput ? urlInput.value.trim() : "";
    const apiKey = keyInput ? keyInput.value.trim() : "";
    const provider = providerInput ? providerInput.value : currentSelectedTTSProvider;
    const targetModel = activeModelEl ? activeModelEl.innerText.trim() : "cosyvoice-v3.5-flash";

    const formData = new FormData();
    formData.append("name", voiceName);
    formData.append("audio_file", file);
    formData.append("speed", speed.toString());
    formData.append("volume", "1.0");
    formData.append("provider_name", provider);
    if (apiKey) formData.append("api_key", apiKey);
    if (baseUrl) formData.append("base_url", baseUrl);
    if (targetModel) formData.append("target_model", targetModel);

    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner-sm"></span> 正在提取声纹并克隆中...`;
    }

    try {
        const res = await fetch(`${API_BASE}/voices/clone`, {
            method: "POST",
            body: formData
        });
        const json = await res.json();
        if (res.ok && json.code === 0 && json.data) {
            const voiceId = json.data.voice_code || json.data.id;
            const displayName = json.data.name || voiceName;
            const serverMsg = json.message || "";

            // 成功呈现反馈提示
            if (typeof showToast === "function") {
                showToast(
                    `🎉 专属声音克隆成功: ${displayName}！已加入音色列表，正在为您试听。`,
                    "success",
                    3500
                );
            }

            // 【核心修复】：立即将刚刚克隆的主播音色追加到“获取到的具体音色”并自动选定与试听！
            injectClonedVoicePill(voiceId, `👑 [专属克隆] ${displayName}`);

            // 重置上传表单
            if (nameInput) nameInput.value = "";
            if (fileInput) fileInput.value = "";

            // 刷新下方资产库
            if (typeof loadVoiceTable === "function") loadVoiceTable();
        } else {
            if (typeof showToast === "function") {
                showToast(`克隆失败: ${json.detail || json.message || "未知错误"}`, "error");
            }
        }
    } catch (e) {
        if (typeof showToast === "function") showToast("上传克隆异常: " + e, "error");
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `
                <svg class="icon-sm" viewBox="0 0 24 24" style="width: 14px; height: 14px;"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>
                立即一键克隆并加入音色列表
            `;
        }
    }
}


// 输入 Voice-ID 时智能推导配对模型并实时同步
function handleVoiceIdInputAdaptModel(val) {
    if (!val) return;
    const clean = val.trim().toLowerCase();
    const hintEl = document.getElementById("tts-bind-model-hint");
    let matchedModel = "";
    if (clean.includes("qwen-audio-3.0-tts-plus")) {
        matchedModel = "qwen-audio-3.0-tts-plus";
    } else if (clean.includes("qwen-audio-3.0-tts-flash")) {
        matchedModel = "qwen-audio-3.0-tts-flash";
    } else if (clean.includes("cosyvoice-v3.5")) {
        matchedModel = "cosyvoice-v3.5-flash";
    } else if (clean.includes("cosyvoice-v1")) {
        matchedModel = "cosyvoice-v1";
    }

    if (matchedModel) {
        selectTTSRealModel(matchedModel);
        if (hintEl) {
            hintEl.innerHTML = `
                <span style="color: #34d399; font-weight: 600;">
                    ✓ 智能识别模型架构: <strong>${matchedModel}</strong> (已自动同步并配对)
                </span>
            `;
        }
    }
}

// 探测/同步阿里云百炼当前业务空间已复刻的音色
async function handleSyncDashscopeVoices() {
    const btn = document.getElementById("btn-sync-dashscope-voices");
    const container = document.getElementById("tts-cloud-voices-suggestion");
    const listEl = document.getElementById("tts-cloud-voices-list");
    const nameInput = document.getElementById("tts-bind-voice-name");
    const vidInput = document.getElementById("tts-bind-voice-id");

    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner-sm"></span> 探测百炼云端中...`;
    }

    try {
        const res = await fetch(`${API_BASE}/voices/dashscope-voices`);
        const json = await res.json();
        if (json.code === 0 && Array.isArray(json.data) && json.data.length > 0) {
            if (container) container.style.display = "block";
            if (listEl) {
                listEl.innerHTML = json.data.map(v => {
                    const vid = v.voice_id || "";
                    const m = v.target_model || "qwen-audio-3.0-tts-plus";
                    const shortId = vid.length > 25 ? (vid.substring(0, 10) + '...' + vid.substring(vid.length - 8)) : vid;
                    return `
                        <div class="quick-pill" style="cursor: pointer; border-color: #38bdf8; background: rgba(56, 189, 248, 0.15); color: #38bdf8; display: inline-flex; align-items: center; gap: 4px; padding: 4px 8px; font-size: 11px;"
                             title="点击自动填入 Voice-ID: ${escapeHtml(vid)}"
                             onclick="selectCloudDetectedVoice('${escapeHtml(vid)}', '${escapeHtml(m)}')">
                            <span>✨ ${escapeHtml(shortId)}</span>
                            <span style="opacity: 0.7; font-size: 10px;">(${escapeHtml(m)})</span>
                        </div>
                    `;
                }).join("");
            }
            if (typeof showToast === "function") {
                showToast(`已从阿里云百炼探测到 ${json.data.length} 个就绪克隆音色！`, "success");
            }
            // 若只有一个且输入框为空，自动贴心地帮用户预填
            if (json.data.length === 1 && vidInput && !vidInput.value.trim()) {
                selectCloudDetectedVoice(json.data[0].voice_id, json.data[0].target_model);
            }
        } else {
            if (container) container.style.display = "none";
            const msg = json.message || "未在百炼空间中探测到有效克隆音色";
            if (typeof showToast === "function") showToast(`百炼云端探测: ${msg}`, "info");
        }
    } catch (e) {
        console.warn("探测百炼云端音色异常:", e);
        if (typeof showToast === "function") showToast("探测云端音色异常: " + e, "error");
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `
                <svg viewBox="0 0 24 24" style="width: 12px; height: 12px; stroke: #34d399; fill: none; stroke-width: 2;"><path d="M21.5 15a4.5 4.5 0 0 0-4-6.5h-.5A7 7 0 0 0 3.5 11 5 5 0 0 0 2 15a5 5 0 0 0 5 5h14.5a4.5 4.5 0 0 0 0-9z"/></svg>
                ☁️ 探测/同步云端已复刻音色
            `;
        }
    }
}

// 选中云端探测到的音色
function selectCloudDetectedVoice(vid, targetModel) {
    const vidInput = document.getElementById("tts-bind-voice-id");
    const nameInput = document.getElementById("tts-bind-voice-name");
    if (vidInput) vidInput.value = vid;
    if (nameInput && !nameInput.value.trim()) {
        nameInput.value = "专属克隆音色";
    }
    handleVoiceIdInputAdaptModel(vid);
    if (targetModel) {
        selectTTSRealModel(targetModel);
    }
}

async function handleRegisterExternalVoiceId() {
    const btn = document.getElementById("btn-bind-voice");
    const nameInput = document.getElementById("tts-bind-voice-name");
    const vidInput = document.getElementById("tts-bind-voice-id");

    const voiceName = (nameInput ? nameInput.value.trim() : "") || "主播专属声线";
    const voiceId = vidInput ? vidInput.value.trim() : "";

    if (!voiceName) {
        if (typeof showToast === "function") showToast("请输入音色名称", "warning");
        if (nameInput) nameInput.focus();
        return;
    }
    if (!voiceId) {
        if (typeof showToast === "function") showToast("请输入在第三方平台生成的专属 Voice ID", "warning");
        if (vidInput) vidInput.focus();
        return;
    }

    const providerInput = document.getElementById("tts-editor-provider");
    const activeModelEl = document.getElementById("tts-thirdparty-active-model");
    const provider = providerInput ? providerInput.value : currentSelectedTTSProvider;

    // 智能推导模型：优先以 Voice-ID 前缀特征推导
    let targetModel = activeModelEl ? activeModelEl.innerText.trim() : "cosyvoice-v3.5-flash";
    const cleanVid = voiceId.toLowerCase();
    if (cleanVid.includes("qwen-audio-3.0-tts-plus")) {
        targetModel = "qwen-audio-3.0-tts-plus";
    } else if (cleanVid.includes("qwen-audio-3.0-tts-flash")) {
        targetModel = "qwen-audio-3.0-tts-flash";
    } else if (cleanVid.includes("cosyvoice-v3.5")) {
        targetModel = "cosyvoice-v3.5-flash";
    }

    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner-sm"></span> 官方实测发声校验中...`;
    }

    try {
        const res = await fetch(`${API_BASE}/voices/bind-id`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                name: voiceName,
                voice_id: voiceId,
                provider_name: provider,
                target_model: targetModel,
                speech_speed: 1.0,
                volume_gain: 1.0
            })
        });
        const json = await res.json();
        if (res.ok && json.code === 0 && json.data) {
            if (typeof showToast === "function") {
                showToast(`🎉 已成功实测发声并绑定专属 Voice-ID: ${voiceId}`, "success");
            }

            // 注入上方音色药丸并选定试听
            injectClonedVoicePill(voiceId, `👑 [专属绑定] ${voiceName}`);

            if (nameInput) nameInput.value = "";
            if (vidInput) vidInput.value = "";
            if (typeof loadVoiceTable === "function") loadVoiceTable();
            // 重新刷新上方音色药丸池，确保专属克隆持久置顶
            const curMeta = resolveTTSProviderMeta(currentSelectedTTSProvider);
            if (curMeta) fetchAndRenderTTSVoices(curMeta, voiceId);
        } else {
            const errDetail = json.detail || json.message || "服务商校验拒绝";
            if (typeof showToast === "function") {
                showToast(`登记失败: ${errDetail}`, "error", 8000);
            }
        }
    } catch (e) {
        if (typeof showToast === "function") showToast("绑定异常: " + e, "error");
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `
                <svg class="icon-sm" viewBox="0 0 24 24" style="width: 14px; height: 14px;"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>
                一键绑定并 WebSocket 生成试听
            `;
        }
    }
}

// 动态将新克隆的音色注入上方药丸容器首位并立即激活
function injectClonedVoicePill(voiceVal, displayLabel) {
    const container = document.getElementById("tts-fetched-voices-container");
    if (!container) return;

    // 清除容器中旧的“暂无音色”占位提示
    const emptyNotice = container.querySelector("div");
    if (emptyNotice && emptyNotice.innerText.includes("暂未获取音色")) {
        container.innerHTML = "";
    }

    // 检查是否已存在相同 voiceVal 的药丸
    let pill = container.querySelector(`[data-voice="${voiceVal}"]`);
    if (!pill) {
        pill = document.createElement("div");
        pill.className = "fetched-model-pill";
        pill.setAttribute("data-voice", voiceVal);
        pill.setAttribute("title", `点击试听并选定专属克隆音色: ${displayLabel}`);
        pill.style.borderColor = "#f59e0b";
        pill.style.color = "#fbbf24";
        pill.style.background = "rgba(245, 158, 11, 0.12)";
        pill.style.fontWeight = "600";
        pill.innerHTML = `
            <svg class="pill-play-icon" viewBox="0 0 24 24" style="width: 12px; height: 12px; stroke: #fbbf24; fill: #fbbf24;"><polygon points="5 3 19 12 5 21 5 3"/></svg>
            <span class="pill-voice-name">${escapeHtml(displayLabel)}</span>
        `;
        pill.onclick = () => selectFetchedTTSVoice(voiceVal, displayLabel);
        container.insertBefore(pill, container.firstChild);
    }

    // 彻底单选并选定该专属克隆音色！
    selectFetchedTTSVoice(voiceVal, displayLabel);
}

// 3 大主流语音引擎模型元数据与协议预设字典
const TTS_PROVIDER_MODELS_PRESETS = {
    moss_tts_nano: {
        activeModel: "MOSS-TTS-Nano (100M Ultra-Lightweight)",
        protocol: "MOSS-TTS-Nano 端侧神经引擎 (GPU 可用时自动加速 · 广播级 48kHz)",
        statusText: (hasKey) => "系统原生自带 · 免密钥开箱即用",
        badgeClass: (hasKey) => "brand-badge green",
        models: [
            "MOSS-TTS-Nano (100M Ultra-Lightweight)",
            "MOSS-TTS-Nano-Local (本地GPU)",
            "MOSS-TTS-Nano-Cloud (云端端点)"
        ]
    },
    cosyvoice: {
        activeModel: "cosyvoice-v3.5-flash",
        protocol: "阿里云百炼 DashScope 语音通道",
        statusText: (hasKey) => hasKey ? "已配置密钥 · 百炼云端通道就绪" : "待配置密钥 · 百炼官方通道",
        badgeClass: (hasKey) => hasKey ? "brand-badge green" : "brand-badge amber",
        models: [
            "cosyvoice-v3.5-flash",
            "cosyvoice-v3.5-plus",
            "cosyvoice-v3-flash",
            "cosyvoice-v3-plus",
            "cosyvoice-v2",
            "cosyvoice-v1"
        ]
    },
    elevenlabs: {
        activeModel: "eleven_multilingual_v2",
        protocol: "ElevenLabs 官方云端全球低延迟流式通道",
        statusText: (hasKey) => hasKey ? "已配置密钥 · ElevenLabs通道就绪" : "待配置密钥 · 官方云端通道",
        badgeClass: (hasKey) => hasKey ? "brand-badge green" : "brand-badge amber",
        models: [
            "eleven_multilingual_v2",
            "eleven_turbo_v2_5",
            "eleven_flash_v2_5",
            "eleven_monolingual_v1"
        ]
    }
};

// 同步即时渲染第三方模型与协议信息（0延迟，杜绝界面残留与跨引擎数据穿杂）
function renderTTSThirdpartyModelInfoDirect(provider, apiKey = "", baseUrl = "") {
    const modelEl = document.getElementById("tts-thirdparty-active-model");
    const protoEl = document.getElementById("tts-thirdparty-protocol");
    const badgeEl = document.getElementById("tts-thirdparty-status-badge");
    const pillsContainer = document.getElementById("tts-thirdparty-models-pills");
    const pillsRow = document.getElementById("tts-thirdparty-models-pills-row");
    if (!modelEl) return;

    const curProvider = (provider || currentSelectedTTSProvider || "moss_tts_nano").toLowerCase();
    let preset = TTS_PROVIDER_MODELS_PRESETS[curProvider];
    if (!preset) {
        if (curProvider.includes("moss") || curProvider.includes("nano")) preset = TTS_PROVIDER_MODELS_PRESETS.moss_tts_nano;
        else if (curProvider.includes("cosy")) preset = TTS_PROVIDER_MODELS_PRESETS.cosyvoice;
        else if (curProvider.includes("eleven")) preset = TTS_PROVIDER_MODELS_PRESETS.elevenlabs;
        else preset = TTS_PROVIDER_MODELS_PRESETS.moss_tts_nano;
    }

    const hasKey = Boolean(apiKey && apiKey.trim().length > 4);
    const activeModel = preset.activeModel;
    const protocolText = preset.protocol;
    const statusText = typeof preset.statusText === "function" ? preset.statusText(hasKey) : preset.statusText;
    const badgeClass = typeof preset.badgeClass === "function" ? preset.badgeClass(hasKey) : preset.badgeClass;

    modelEl.innerHTML = `<strong>${escapeHtml(activeModel)}</strong>`;
    if (protoEl) protoEl.innerText = protocolText;
    if (badgeEl) {
        badgeEl.className = badgeClass;
        badgeEl.innerText = statusText;
    }

    if (pillsContainer && pillsRow) {
        pillsRow.style.display = "flex";
        pillsContainer.innerHTML = preset.models.map(m => `
            <span class="quick-pill" data-model="${escapeHtml(m)}"
                  style="cursor: pointer; ${m === activeModel ? 'border-color: #38bdf8; background: rgba(56, 189, 248, 0.2); color: #38bdf8;' : ''}"
                  onclick="selectTTSRealModel('${escapeHtml(m)}')">
                ${escapeHtml(m)}
            </span>
        `).join("");
    }
}

// 选用第三方生效模型
function selectTTSRealModel(modelName) {
    if (!modelName) return;
    const modelEl = document.getElementById("tts-thirdparty-active-model");
    if (modelEl) {
        modelEl.innerHTML = `<strong>${escapeHtml(modelName)}</strong>`;
    }
    document.querySelectorAll("#tts-thirdparty-models-pills .quick-pill").forEach(p => {
        const isMatch = p.getAttribute("data-model") === modelName;
        p.style.borderColor = isMatch ? "#38bdf8" : "";
        p.style.background = isMatch ? "rgba(56, 189, 248, 0.2)" : "";
        p.style.color = isMatch ? "#38bdf8" : "";
    });
    if (typeof showToast === "function") {
        showToast(`已选用第三方底层生效模型: ${modelName}`, "info");
    }
}

// 请求版本序号（严格防竞态，丢弃过时网络返回）
let _ttsModelFetchId = 0;

// 实时通过 API 获取并渲染第三方服务商真实模型与通道协议
async function fetchAndRenderTTSThirdpartyModelInfo(baseUrl, apiKey, provider, configId) {
    const modelEl = document.getElementById("tts-thirdparty-active-model");
    const protoEl = document.getElementById("tts-thirdparty-protocol");
    const badgeEl = document.getElementById("tts-thirdparty-status-badge");
    const pillsContainer = document.getElementById("tts-thirdparty-models-pills");
    const pillsRow = document.getElementById("tts-thirdparty-models-pills-row");
    if (!modelEl) return;

    const curProvider = (provider || currentSelectedTTSProvider || "moss_tts_nano").toLowerCase();
    // 1. 同步即时渲染本地预设信息，确保 0 毫秒立即生效
    renderTTSThirdpartyModelInfoDirect(curProvider, apiKey, baseUrl);

    // 2. 发起异步探测
    const currentFetchId = ++_ttsModelFetchId;

    try {
        const res = await fetch(`${API_BASE}/settings/tts/models`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                base_url: baseUrl || null,
                api_key: apiKey || null,
                provider_name: curProvider,
                config_id: configId || null
            })
        });
        const json = await res.json();

        // 3. 严格防竞态丢弃校验：若当前请求已过期或用户已切走，绝不覆盖！
        if (currentFetchId !== _ttsModelFetchId || (currentSelectedTTSProvider && currentSelectedTTSProvider !== curProvider)) {
            return;
        }

        if (json.code === 0 && json.active_model) {
            const activeModel = json.active_model;
            modelEl.innerHTML = `<strong>${escapeHtml(activeModel)}</strong>`;
            if (json.protocol && protoEl) protoEl.innerText = json.protocol;
            if (badgeEl && json.status_text) badgeEl.innerText = json.status_text;

            const modelsList = Array.isArray(json.models) && json.models.length > 0
                ? json.models
                : [activeModel];

            if (pillsContainer && pillsRow) {
                pillsRow.style.display = "flex";
                pillsContainer.innerHTML = modelsList.map(m => `
                    <span class="quick-pill" data-model="${escapeHtml(m)}"
                          style="cursor: pointer; ${m === activeModel ? 'border-color: #38bdf8; background: rgba(56, 189, 248, 0.2); color: #38bdf8;' : ''}"
                          onclick="selectTTSRealModel('${escapeHtml(m)}')">
                        ${escapeHtml(m)}
                    </span>
                `).join("");
            }
        }
    } catch (e) {
        console.warn("探测第三方模型信息异常:", e);
    }
}

// 全局试听音频与网络请求单例控制器（彻底杜绝并发竞争导致多个声音同时播放）
window._ttsPreviewController = window._ttsPreviewController || {
    audio: null,
    abortController: null,
    sessionId: 0,
    stopAll() {
        this.sessionId++;
        if (this.abortController) {
            try { this.abortController.abort(); } catch (e) { }
            this.abortController = null;
        }
        if (this.audio) {
            try {
                this.audio.pause();
                this.audio.currentTime = 0;
                this.audio.src = "";
            } catch (e) { }
            this.audio = null;
        }
        if (typeof previewAudioEl !== "undefined" && previewAudioEl) {
            try { previewAudioEl.pause(); previewAudioEl = null; } catch (e) { }
        }
        document.querySelectorAll("#tts-fetched-voices-container .fetched-model-pill").forEach(el => el.classList.remove("playing"));
    }
};

// 即时播放指定音色的专属试听音频
async function playTTSVoicePreview(voiceVal, voiceLabel = "") {
    if (!voiceVal) return;

    // 1. 强力终止任何正在播放的音频与仍在进行的异步网络请求
    window._ttsPreviewController.stopAll();
    const currentSession = window._ttsPreviewController.sessionId;
    const abortCtrl = new AbortController();
    window._ttsPreviewController.abortController = abortCtrl;

    const urlInput = document.getElementById("tts-input-url");
    const keyInput = document.getElementById("tts-input-key");
    const providerInput = document.getElementById("tts-editor-provider");
    const pingStatusEl = document.getElementById("tts-ping-latency-status");

    const baseUrl = urlInput ? urlInput.value.trim() : "";
    const apiKey = keyInput ? keyInput.value.trim() : "";
    const provider = providerInput ? providerInput.value : currentSelectedTTSProvider;

    // 标记当前正在播放的音色药丸动效
    document.querySelectorAll("#tts-fetched-voices-container .fetched-model-pill").forEach(el => {
        el.classList.toggle("playing", el.getAttribute("data-voice") === voiceVal);
    });

    if (pingStatusEl) {
        pingStatusEl.innerHTML = `
            <span style="color: #38BDF8; font-weight: 600; display: inline-flex; align-items: center; gap: 5px;">
                <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #38BDF8; box-shadow: 0 0 6px #38BDF8;"></span>
                🔊 正在试听: <strong>${escapeHtml(voiceLabel || voiceVal)}</strong>...
            </span>
        `;
    }

    try {
        const activeModelEl = document.getElementById("tts-thirdparty-active-model");
        let currentActiveModel = activeModelEl ? activeModelEl.innerText.trim() : "";

        // 若是 CosyVoice 官方系统预置音色，自动纠偏模型为 cosyvoice-v1 (避免复刻专属模型 v3.5-flash 报 418)
        const isPreset = (voiceVal.startsWith("long") || voiceVal.startsWith("loong")) && !voiceVal.includes("cloned") && !voiceVal.includes("custom");
        if (provider === "cosyvoice" && isPreset) {
            currentActiveModel = "cosyvoice-v1";
        }

        const previewRes = await fetch(`${API_BASE}/settings/tts/preview`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            signal: abortCtrl.signal,
            body: JSON.stringify({
                provider_name: provider,
                model_name: currentActiveModel || null,
                voice_name: voiceVal,
                base_url: baseUrl || null,
                api_key: apiKey || null
            })
        });

        // 若网络返回时发现已经发起了新的试听请求，立即丢弃，绝不播放！
        if (window._ttsPreviewController.sessionId !== currentSession) return;

        if (previewRes.ok) {
            const blob = await previewRes.blob();
            if (window._ttsPreviewController.sessionId !== currentSession) return;

            const audioUrl = URL.createObjectURL(blob);
            const audio = new Audio(audioUrl);
            window._ttsPreviewController.audio = audio;

            audio.onended = () => {
                document.querySelectorAll("#tts-fetched-voices-container .fetched-model-pill").forEach(el => el.classList.remove("playing"));
                if (pingStatusEl) {
                    pingStatusEl.innerHTML = `
                        <span style="color: #10B981; font-weight: 600; display: inline-flex; align-items: center; gap: 5px;">
                            <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #10B981;"></span>
                            试听完成 · 当前已选定: <strong>${escapeHtml(voiceLabel || voiceVal)}</strong>
                        </span>
                    `;
                }
            };

            await audio.play();
        } else {
            const errJson = await previewRes.json().catch(() => ({}));
            document.querySelectorAll("#tts-fetched-voices-container .fetched-model-pill").forEach(el => el.classList.remove("playing"));
            if (pingStatusEl) {
                pingStatusEl.innerHTML = `
                    <span style="color: #ef4444; font-size: 11.5px;">
                        ⚠️ 试听受阻: ${escapeHtml(errJson.detail || "音色服务未响应")}
                    </span>
                `;
            }
        }
    } catch (e) {
        if (e.name === "AbortError") return; // 用户切换快速切换，正常中断
        console.warn("音色试听播放受阻:", e);
        document.querySelectorAll("#tts-fetched-voices-container .fetched-model-pill").forEach(el => el.classList.remove("playing"));
    }
}

// 异步拉取并渲染音色候选药丸（专属克隆音色档案与官方预设音色深度合并，专属克隆置顶展示）
async function fetchAndRenderTTSVoices(meta, selectedVoiceVal = "") {
    const container = document.getElementById("tts-fetched-voices-container");
    const countStatusEl = document.getElementById("tts-voices-count-status");
    const voiceInput = document.getElementById("tts-input-voice");
    if (!container || !meta) return { clonedVoices: [], recommendedVoices: [], currentSelected: "" };

    // 1. 获取该引擎的官方推荐音色
    const recommendedVoices = (meta.recommendedVoices || []).map(r => ({ ...r }));
    const recommendedIdSet = new Set(recommendedVoices.map(r => (r.val || "").trim().toLowerCase()));

    // 2. 获取后端已登记的声音档案 (VoiceProfile)，严格按引擎和类型进行区分，绝不把官方预设重复当成克隆音色
    let clonedVoices = [];
    try {
        const res = await fetch(`${API_BASE}/voices/list`);
        const json = await res.json();
        if (json.code === 0 && Array.isArray(json.data)) {
            const currentProvider = (meta.id || "").toLowerCase();
            const seenCloneIds = new Set();

            json.data.forEach(v => {
                const vid = (v.id || "").trim();
                const vidLower = vid.toLowerCase();
                if (!vid || vidLower === "voice_default_female") return;

                // 若该音色是官方预设音色（在官方预设推荐表中，或者 voice_type === 'preset'）
                if (recommendedIdSet.has(vidLower) || v.voice_type === "preset") {
                    // 若用户在音色资产库中为该官方音色改了名，同步更新推荐药丸的展示名称
                    const recItem = recommendedVoices.find(r => (r.val || "").trim().toLowerCase() === vidLower);
                    if (recItem && v.name && !v.name.startsWith("zh-CN-")) {
                        const oldDesc = recItem.text.includes("(") || recItem.text.includes("（")
                            ? (recItem.text.split(/[\(\（]/)[1] || "")
                            : "";
                        recItem.text = oldDesc ? `${v.name} (${oldDesc}` : v.name;
                    }
                    return; // 严禁将官方预设音色放入专属克隆列表！
                }

                // 所属引擎过滤：仅保留属于当前引擎的真正专属克隆音色
                const vProv = (v.provider_name || "").toLowerCase();
                const isMatchProvider = !vProv || vProv === currentProvider || (currentProvider === "cosyvoice" && (vid.includes("cosy") || vid.includes("bailian")));
                const isCloned = v.voice_type === "cloned" || v.is_clone || vid.startsWith("clone_") || vid.includes("bailian") || vid.includes("cloned");

                if (isMatchProvider && isCloned && !seenCloneIds.has(vid)) {
                    seenCloneIds.add(vid);
                    clonedVoices.push(v);
                }
            });
        }
    } catch (e) {
        console.warn("获取声音档案库列表异常:", e);
    }

    // 3. 决定当前选定的音色
    let currentSelected = selectedVoiceVal || (voiceInput ? voiceInput.value.trim() : "");
    const isCurrentInClones = clonedVoices.some(v => v.id === currentSelected);
    const isCurrentInRecs = recommendedVoices.some(v => v.val === currentSelected);

    // 若当前选中的 ID 已失效/不在列表中，自动选定有效音色
    if (!currentSelected || (!isCurrentInClones && !isCurrentInRecs)) {
        if (clonedVoices.length > 0) {
            currentSelected = clonedVoices[0].id;
        } else if (recommendedVoices.length > 0) {
            currentSelected = recommendedVoices[0].val;
        } else {
            currentSelected = "";
        }
    }
    if (voiceInput) voiceInput.value = currentSelected;

    // 4. 构建药丸 DOM (引入全局 ID 防重锁，100% 确保每个音色只呈现一次)
    let pillsHtml = "";
    const renderedVoiceIds = new Set();

    // 4.1 专属克隆音色（金色尊贵皇冠高亮，置顶显示，只展示真实专属克隆，绝不混入官方音色）
    clonedVoices.forEach(cv => {
        if (!cv.id || renderedVoiceIds.has(cv.id)) return;
        renderedVoiceIds.add(cv.id);

        const isSel = cv.id === currentSelected;
        pillsHtml += `
            <div class="fetched-model-pill cloned-voice-pill ${isSel ? 'selected' : ''}"
                 data-voice="${escapeHtml(cv.id)}"
                 data-is-clone="true"
                 title="点击选定并试听专属克隆音色: ${escapeHtml(cv.name)} (ID: ${escapeHtml(cv.id)})"
                 onclick="selectFetchedTTSVoice('${escapeHtml(cv.id)}', '${escapeHtml(cv.name)} (专属克隆)')"
                 style="border-color: rgba(245, 158, 11, 0.65); background: ${isSel ? 'rgba(245, 158, 11, 0.28)' : 'rgba(245, 158, 11, 0.12)'}; color: #FBBF24; font-weight: 600; display: inline-flex; align-items: center; gap: 6px; padding: 6px 10px;">
                <span style="font-size: 13px;">👑</span>
                <span style="font-weight: 600;">${escapeHtml(cv.name)}</span>
                <span class="btn-delete-clone-pill"
                      title="删除此克隆音色档案"
                      onclick="handleDeleteClonedVoice(event, '${escapeHtml(cv.id)}', '${escapeHtml(cv.name)}')"
                      style="display: inline-flex; align-items: center; justify-content: center; width: 16px; height: 16px; border-radius: 50%; background: rgba(239, 68, 68, 0.25); color: #f87171; margin-left: 4px; cursor: pointer; transition: all 0.2s;"
                      onmouseover="this.style.background='#ef4444';this.style.color='#ffffff';"
                      onmouseout="this.style.background='rgba(239, 68, 68, 0.25)';this.style.color='#f87171';">
                    <svg viewBox="0 0 24 24" style="width: 10px; height: 10px; stroke: currentColor; fill: none; stroke-width: 3;"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                </span>
            </div>
        `;
    });

    // 4.2 引擎预设官方音色（严格排重，已作为克隆展示的绝不在此重复）
    recommendedVoices.forEach(v => {
        if (!v.val || renderedVoiceIds.has(v.val)) return;
        renderedVoiceIds.add(v.val);

        const isSel = v.val === currentSelected;
        pillsHtml += `
            <div class="fetched-model-pill ${isSel ? 'selected' : ''}"
                 data-voice="${escapeHtml(v.val)}"
                 title="点击试听并选定官方音色: ${escapeHtml(v.text)}"
                 onclick="selectFetchedTTSVoice('${escapeHtml(v.val)}', '${escapeHtml(v.text)}')">
                ${escapeHtml(v.text)}
            </div>
        `;
    });

    if (!pillsHtml) {
        container.innerHTML = `<div style="color: var(--text-muted); font-size: 12px;">暂无可用的发音音色，请点击上方「获取音色与模型」。</div>`;
    } else {
        container.innerHTML = pillsHtml;
    }

    // 5. 更新状态与计数
    if (countStatusEl) {
        const total = renderedVoiceIds.size;
        const selectedMatch = clonedVoices.find(v => v.id === currentSelected);
        const selectedRec = recommendedVoices.find(v => v.val === currentSelected);
        let displaySelectedName = "";
        if (selectedMatch) {
            displaySelectedName = `${selectedMatch.name} (专属克隆)`;
        } else if (selectedRec) {
            displaySelectedName = selectedRec.text;
        } else {
            displaySelectedName = currentSelected ? "已选定音色" : "未选定 (请点击下方音色)";
        }

        countStatusEl.innerHTML = `
            <span style="color: #10B981; font-weight: 600; display: inline-flex; align-items: center; gap: 5px;">
                <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #10B981; box-shadow: 0 0 6px #10B981;"></span>
                共 ${total} 款可用音色 ${clonedVoices.length > 0 ? `(含 <strong style="color:#FBBF24;">${clonedVoices.length} 款专属克隆</strong>)` : ''} · 当前选定: <strong>${escapeHtml(displaySelectedName)}</strong>
            </span>
        `;
    }

    const filteredRecs = recommendedVoices.filter(v => v.val && !clonedVoices.some(c => c.id === v.val));
    window._latestFetchedVoices = {
        provider: meta.id,
        clonedVoices,
        recommendedVoices: filteredRecs,
        currentSelected
    };

    return { clonedVoices, recommendedVoices, currentSelected };
}

// 删除指定专属克隆音色（阻止冒泡、确认提示、后端安全删除并实时刷新）
async function handleDeleteClonedVoice(event, voiceId, voiceName) {
    if (event) {
        event.stopPropagation();
        event.preventDefault();
    }
    if (!voiceId) return;

    if (!confirm(`确认彻底删除专属克隆音色【${voiceName}】吗？\nID: ${voiceId}\n删除后对应的本地音频与云端绑定将一并移除，不可恢复。`)) {
        return;
    }

    try {
        const res = await fetch(`${API_BASE}/voices/${voiceId}`, { method: "DELETE" });
        const json = await res.json();
        if (json.code === 0) {
            if (typeof showToast === "function") {
                showToast(`已成功删除专属克隆音色【${voiceName}】`, "success");
            }
            // 若当前输入框选中的正是被删除的音色，清空并切换
            const voiceInput = document.getElementById("tts-input-voice");
            if (voiceInput && voiceInput.value === voiceId) {
                voiceInput.value = "";
            }
            // 实时重新拉取与渲染药丸容器
            const meta = resolveTTSProviderMeta(currentSelectedTTSProvider);
            await fetchAndRenderTTSVoices(meta);
            // 联动刷新下方音色资产库表格
            if (typeof loadVoiceTable === "function") {
                loadVoiceTable();
            }
        } else {
            alert("删除失败: " + (json.detail || json.message));
        }
    } catch (e) {
        alert("删除音色异常: " + e);
    }
}

// 切换选定音色 (点击立即自动发起试听并单选)
function selectFetchedTTSVoice(voiceVal, voiceLabel = "") {
    if (!voiceVal) return;
    const voiceInput = document.getElementById("tts-input-voice");
    if (voiceInput) voiceInput.value = voiceVal;

    // 清除全容器内所有药丸的 selected，只为当前匹配项保留 selected
    document.querySelectorAll("#tts-fetched-voices-container .fetched-model-pill").forEach(el => {
        const isMatch = el.getAttribute("data-voice") === voiceVal;
        el.classList.toggle("selected", isMatch);
        if (!isMatch) el.classList.remove("playing");
    });

    const countStatusEl = document.getElementById("tts-voices-count-status");
    let pureVoiceName = "";
    let fullVoiceLabel = voiceLabel;
    if (countStatusEl) {
        const total = document.querySelectorAll("#tts-fetched-voices-container .fetched-model-pill").length;
        let displayName = voiceLabel;
        if (!displayName || displayName === voiceVal) {
            const matchedPill = document.querySelector(`#tts-fetched-voices-container .fetched-model-pill[data-voice="${voiceVal}"]`);
            if (matchedPill) {
                const nameSpan = matchedPill.querySelector("span:not(.btn-delete-clone-pill)") || matchedPill;
                displayName = nameSpan.textContent.trim();
            } else {
                displayName = "已选定音色";
            }
        }
        fullVoiceLabel = displayName;
        pureVoiceName = displayName.replace(/^[👑\s]+/, "").split(/[\(\（]/)[0].trim() || displayName;
        countStatusEl.innerHTML = `
            <span style="color: #10B981; font-weight: 600; display: inline-flex; align-items: center; gap: 5px;">
                <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #10B981; box-shadow: 0 0 6px #10B981;"></span>
                共 ${total} 款可用音色 · 当前选定: <strong>${escapeHtml(displayName)}</strong>
            </span>
        `;
    } else if (voiceLabel) {
        pureVoiceName = voiceLabel.replace(/^[👑\s]+/, "").split(/[\(\（]/)[0].trim() || voiceLabel;
    }

    // 记录选中的纯净音色名称与完整标签，供保存配置时持久化
    window._currentSelectedTTSVoiceMeta = {
        voiceVal: voiceVal,
        voiceName: pureVoiceName || voiceVal,
        voiceLabel: fullVoiceLabel || voiceVal
    };

    const activeModelEl = document.getElementById("tts-thirdparty-active-model");
    if (activeModelEl && currentSelectedTTSProvider === "cosyvoice") {
        const matchedPill = document.querySelector(`#tts-fetched-voices-container .fetched-model-pill[data-voice="${voiceVal}"]`);
        const isClone = matchedPill && matchedPill.getAttribute("data-is-clone") === "true";
        const autoModel = isClone ? "cosyvoice-v3.5-flash" : "cosyvoice-v1";
        activeModelEl.innerHTML = `<strong>${autoModel}</strong>`;
        // 同步更新模型药丸高亮
        document.querySelectorAll("#tts-thirdparty-models-pills .quick-pill").forEach(p => {
            const pModel = p.getAttribute("data-model");
            const isMatch = pModel === autoModel;
            p.style.borderColor = isMatch ? "#38bdf8" : "";
            p.style.background = isMatch ? "rgba(56, 189, 248, 0.2)" : "";
            p.style.color = isMatch ? "#38bdf8" : "";
        });
    }

    // 用户切换到任意音色时，立即通过单例控制器触发该音色专属声线的即时试听
    playTTSVoicePreview(voiceVal, voiceLabel);
}

// 自动解密填入已配置过的 TTS 密钥并默认脱敏
async function autoFillRealTTSKeyIfConfigured(configId = null, existingConfig = null, meta = null) {
    const keyInput = document.getElementById("tts-input-key");
    const eyeBtn = document.getElementById("btn-toggle-tts-key-eye");
    const hintEl = document.getElementById("tts-key-status-hint");
    const curIdInput = document.getElementById("tts-editor-config-id");
    if (!keyInput) return "";

    const curProvider = (meta && meta.id) ? meta.id : currentSelectedTTSProvider;
    const isMoss = curProvider === "moss_tts_nano";

    // 若当前为免 Key 引擎（如 MOSS-TTS-Nano），无需填充密钥
    if (isMoss) {
        keyInput.value = "";
        keyInput.placeholder = "系统原生自带引擎，免 API 密钥，开箱即用";
        if (hintEl) hintEl.innerText = "系统原生自带 · 免密钥开箱即用";
        return "";
    }

    // 1. 尝试确定 configId
    let targetConfigId = configId || (curIdInput ? curIdInput.value.trim() : "");
    if (!targetConfigId && existingConfig && existingConfig.id) {
        targetConfigId = existingConfig.id;
    }

    // 2. 若仍未拿到 configId，从 cachedAllConfigs 中定位当前引擎的配置（激活项优先）
    if (!targetConfigId && typeof cachedAllConfigs !== "undefined" && Array.isArray(cachedAllConfigs) && cachedAllConfigs.length > 0) {
        const foundActive = cachedAllConfigs.find(c => {
            if (c.config_group !== "tts") return false;
            const cMeta = resolveTTSProviderMeta(c.provider_name, c);
            return cMeta.id === curProvider && Boolean(c.is_active);
        });
        const foundAny = foundActive || cachedAllConfigs.find(c => {
            if (c.config_group !== "tts") return false;
            const cMeta = resolveTTSProviderMeta(c.provider_name, c);
            return cMeta.id === curProvider;
        });
        if (foundAny) {
            targetConfigId = foundAny.id;
        }
    }

    // 3. 若 cachedAllConfigs 此时为空，主动通过 API 异步拉取一次所有配置以防网络时序竞态
    if (!targetConfigId) {
        try {
            const resConfigs = await fetch(`${API_BASE}/settings/configs`);
            const jsonConfigs = await resConfigs.json();
            if (jsonConfigs.code === 0 && Array.isArray(jsonConfigs.data)) {
                if (typeof cachedAllConfigs !== "undefined") {
                    cachedAllConfigs = jsonConfigs.data;
                }
                const hit = jsonConfigs.data.find(c => {
                    if (c.config_group !== "tts") return false;
                    const cMeta = resolveTTSProviderMeta(c.provider_name, c);
                    return cMeta.id === curProvider && Boolean(c.is_active);
                }) || jsonConfigs.data.find(c => {
                    if (c.config_group !== "tts") return false;
                    const cMeta = resolveTTSProviderMeta(c.provider_name, c);
                    return cMeta.id === curProvider;
                });
                if (hit) {
                    targetConfigId = hit.id;
                }
            }
        } catch (e) {
            console.warn("自动补拉 TTS 配置列表异常:", e);
        }
    }

    // 4. 若当前引擎尚未配置过，严格清空 Key 输入框并重置状态，绝不允许混入其他引擎的 Key
    if (!targetConfigId) {
        if (curIdInput) curIdInput.value = "";
        keyInput.value = "";
        keyInput.type = "password";
        if (hintEl) {
            hintEl.innerText = (meta && meta.needKey) ? "云端商用托管 (需硬件安全加密密钥)" : "此引擎免密钥";
        }
        return "";
    }

    if (curIdInput) {
        curIdInput.value = targetConfigId;
    }

    keyInput.type = "password";
    if (eyeBtn) {
        eyeBtn.innerHTML = `<svg viewBox="0 0 24 24"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`;
        eyeBtn.title = "点击显示真实密钥明文";
        eyeBtn.style.color = "";
    }

    // 5. 检查属于当前引擎 configId 的专属内存缓存
    let realKey = (typeof cachedDecryptedKeys !== "undefined") ? (cachedDecryptedKeys[targetConfigId] || "") : "";
    if (realKey) {
        keyInput.value = realKey;
        if (hintEl) hintEl.innerText = "已载入硬件加密密钥 (默认脱敏)";
        return realKey;
    }

    // 6. 调用后端硬件级 AES 解密接口，拉取属于当前引擎自身的真实密钥
    try {
        const res = await fetch(`${API_BASE}/settings/configs/${targetConfigId}/raw-key`);
        const json = await res.json();
        if (json.code === 0 && json.raw_key) {
            realKey = json.raw_key;
            if (typeof cachedDecryptedKeys !== "undefined") {
                cachedDecryptedKeys[targetConfigId] = realKey;
            }
            keyInput.value = realKey;
            keyInput.type = "password";
            if (hintEl) hintEl.innerText = "已载入硬件加密密钥 (默认脱敏)";

            // 顺带触发底层模型架构探测
            const urlInput = document.getElementById("tts-input-url");
            fetchAndRenderTTSThirdpartyModelInfo(
                urlInput ? urlInput.value : "",
                realKey,
                curProvider,
                targetConfigId
            );
            return realKey;
        }
    } catch (e) {
        console.warn("自动获取 TTS API Key 异常:", e);
    }

    // 若当前引擎有配置 ID 但未解析出 Key，保持输入框为空，严禁混用其他引擎 Key
    keyInput.value = "";
    return "";
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

// 点击【获取音色与模型】
async function handleFetchTTSVoices() {
    const btn = document.getElementById("btn-fetch-tts-voices");
    const meta = resolveTTSProviderMeta(currentSelectedTTSProvider);
    const voiceInput = document.getElementById("tts-input-voice");
    const urlInput = document.getElementById("tts-input-url");
    const keyInput = document.getElementById("tts-input-key");
    const configIdInput = document.getElementById("tts-editor-config-id");

    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner-sm"></span> 获取音色与模型中...`;
    }

    try {
        const currentSelected = (voiceInput && voiceInput.value) ? voiceInput.value.trim() : "";
        const result = await fetchAndRenderTTSVoices(meta, currentSelected);

        const realKey = (keyInput ? keyInput.value.trim() : "") || (configIdInput && cachedDecryptedKeys[configIdInput.value]) || "";

        // 联动发起第三方模型与协议信息探测
        fetchAndRenderTTSThirdpartyModelInfo(
            urlInput ? urlInput.value : "",
            realKey,
            meta.id,
            configIdInput ? configIdInput.value : ""
        );

        const totalCount = (result.clonedVoices?.length || 0) + (result.recommendedVoices?.length || 0);
        showTTSTestResult(true, `已成功加载【${meta.name}】可用音色（含 ${result.clonedVoices?.length || 0} 款专属克隆，共 ${totalCount} 款），并同步探测第三方底层模型架构！点击下方药丸即可自由试听与选定。`);
    } catch (e) {
        showTTSTestResult(false, "加载音色列表异常: " + e);
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `<svg class="icon-sm" viewBox="0 0 24 24" style="width: 14px; height: 14px;"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg> 获取音色与模型`;
        }
    }
}

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

    // 停止上一次未播放完的试听 (通过全局试听单例控制器，杜绝未定义变量引用)
    if (window._ttsPreviewController && typeof window._ttsPreviewController.stopAll === "function") {
        window._ttsPreviewController.stopAll();
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
                fetchAndRenderTTSThirdpartyModelInfo(baseUrl, apiKey, provider, configId);
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
                    if (window._ttsPreviewController && typeof window._ttsPreviewController.stopAll === "function") {
                        window._ttsPreviewController.stopAll();
                    }
                    const blob = await previewRes.blob();
                    const audioUrl = URL.createObjectURL(blob);
                    const audio = new Audio(audioUrl);
                    if (window._ttsPreviewController) {
                        window._ttsPreviewController.audio = audio;
                    }

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

// 全局内置知名 TTS 引擎音色友好中文映射字典（彻底屏蔽底层英文/拼音 ID）
const TTS_VOICE_FRIENDLY_NAMES = {
    // Edge-TTS 官方音色
    "zh-CN-XiaoxiaoNeural": "晓晓 (超自然知性女声)",
    "zh-CN-YunxiNeural": "云希 (活力阳光男声)",
    "zh-CN-YunjianNeural": "云健 (激情带货男声)",
    "zh-CN-XiaoyiNeural": "晓伊 (亲切带货女声)",
    "zh-CN-YunyangNeural": "云扬 (专业新闻男声)",
    "zh-CN-XiaochenNeural": "晓辰 (开朗自然女声)",
    "zh-CN-XiaohanNeural": "晓涵 (知性温柔女声)",
    "zh-CN-XiaomengNeural": "晓梦 (甜美软萌女声)",
    "zh-CN-XiaomoNeural": "晓墨 (生动故事女声)",
    "zh-CN-XiaoqiuNeural": "晓秋 (沉稳知性女声)",
    "zh-CN-XiaoruiNeural": "晓睿 (阳光少儿女声)",
    "zh-CN-XiaoxuanNeural": "晓萱 (元气自信女声)",
    "zh-CN-XiaoyanNeural": "晓颜 (清脆悦耳女声)",
    "zh-CN-XiaoyouNeural": "晓悠 (灵动可爱童声)",
    "zh-CN-YunfengNeural": "云枫 (年轻阳光男声)",
    "zh-CN-YunhaoNeural": "云皓 (稳重磁性男声)",
    "zh-CN-YunxiaNeural": "云夏 (朝气清爽男声)",
    "zh-CN-YunyeNeural": "云野 (成熟沉稳男声)",
    "zh-CN-YunzeNeural": "云泽 (磁性故事男声)",
    "zh-HK-HiuMaanNeural": "晓曼 (粤语女声)",
    "zh-HK-WanLungNeural": "云龙 (粤语男声)",
    "zh-TW-HsiaoChenNeural": "晓臻 (台湾国语女声)",
    "zh-TW-YunJheNeural": "云哲 (台湾国语男声)",

    // 阿里云百炼 / CosyVoice
    "longxiaochun": "龙小春 (知性女声)",
    "longxiaoxia": "龙小夏 (甜美直播女声)",
    "longwan": "龙婉 (温柔女主播)",
    "longcheng": "龙诚 (稳重大气男声)",
    "longhua": "龙华 (阳光亲切男声)",
    "longshu": "龙书 (温和沉稳男声)",
    "longshuo": "龙硕 (激情带货男声)",
    "longjing": "龙静 (知性解说女声)",
    "longmiao": "龙妙 (甜美萌音女声)",
    "longyue": "龙悦 (温暖阳光女声)",
    "longyuan": "龙渊 (沉稳旁白男声)",
    "longfei": "龙飞 (活力主持男声)",
    "longjie": "龙杰 (干练解说男声)",
    "longling": "龙玲 (亲切客服女声)",
    "longtian": "龙天 (活力带货男声)",

    // ChatTTS
    "female_warm": "温暖知性女主播",
    "female_sweet": "甜美活力带货女声",
    "male_magnetic": "低沉磁性男主播",
    "male_narrator": "质感旁白男主播",

    // 系统通用
    "voice_default_female": "通用播报女声"
};

// 保存当前 TTS 配置
async function handleSaveCurrentTTS() {
    const saveBtn = document.getElementById("btn-save-tts");
    const statusTip = document.getElementById("tts-save-btn-status");
    const origBtnHtml = saveBtn ? saveBtn.innerHTML : "保存此语音配置";

    const urlInput = document.getElementById("tts-input-url");
    const voiceInput = document.getElementById("tts-input-voice");
    const keyInput = document.getElementById("tts-input-key");
    const providerInput = document.getElementById("tts-editor-provider");
    const configIdInput = document.getElementById("tts-editor-config-id");

    const baseUrl = urlInput ? urlInput.value.trim() : "";
    let voiceVal = voiceInput ? voiceInput.value.trim() : "";
    const apiKey = keyInput ? keyInput.value.trim() : "";
    const provider = providerInput ? providerInput.value : currentSelectedTTSProvider;
    const configId = configIdInput ? configIdInput.value : "";

    const meta = resolveTTSProviderMeta(provider);
    if (meta.needUrl && !baseUrl) {
        if (typeof showToast === "function") showToast("该语音引擎需填写服务 Base URL 地址！", "warning");
        else alert("该语音引擎需填写服务 Base URL 地址！");
        return;
    }

    // 若未选定音色，自动智能从页面药丸或预设推荐列表中选取第一个，杜绝拦截卡死
    if (!voiceVal) {
        const activePill = document.querySelector("#tts-fetched-voices-container .fetched-model-pill.selected") ||
                           document.querySelector("#tts-fetched-voices-container .fetched-model-pill");
        if (activePill) {
            voiceVal = activePill.getAttribute("data-voice") || "";
            activePill.classList.add("selected");
        } else if (meta.recommendedVoices && meta.recommendedVoices.length > 0) {
            voiceVal = meta.recommendedVoices[0].val;
        }
        if (voiceInput && voiceVal) {
            voiceInput.value = voiceVal;
        }
    }

    if (!voiceVal) {
        if (typeof showToast === "function") showToast("请先点击上方「获取音色与模型」加载可用音色后再进行保存！", "warning");
        else alert("请先点击上方「获取音色与模型」加载可用音色后再进行保存！");
        return;
    }

    // 切换按钮加载态
    if (saveBtn) {
        saveBtn.disabled = true;
        saveBtn.innerHTML = `<span style="display:inline-block;width:12px;height:12px;border:2px solid #fff;border-top-color:transparent;border-radius:50%;animation:spin 0.8s linear infinite;margin-right:6px;vertical-align:middle;"></span> 正在保存配置并同步入库...`;
    }
    if (statusTip) {
        statusTip.style.display = "inline";
        statusTip.style.color = "#38BDF8";
        statusTip.innerText = "正在保存并同步入库中...";
    }

    const ttsConfigs = cachedAllConfigs.filter(c => c.config_group === "tts");
    let isDefault = false;
    if (configId) {
        const exist = cachedAllConfigs.find(c => c.id === configId);
        isDefault = exist ? Boolean(exist.is_active) : false;
    } else {
        isDefault = ttsConfigs.length === 0;
    }

    // 智能解析选定音色的纯净中文名称与完整标签，存入 extra_params 持久化
    let chosenVoiceName = "";
    let chosenVoiceLabel = "";
    if (window._currentSelectedTTSVoiceMeta && window._currentSelectedTTSVoiceMeta.voiceVal === voiceVal) {
        chosenVoiceName = window._currentSelectedTTSVoiceMeta.voiceName;
        chosenVoiceLabel = window._currentSelectedTTSVoiceMeta.voiceLabel;
    } else {
        const resolved = resolveTTSVoiceInfo({ model_name: voiceVal, provider_name: provider });
        chosenVoiceName = resolved.name;
        chosenVoiceLabel = resolved.fullName;
    }

    const payload = {
        id: configId || null,
        config_group: "tts",
        provider_name: provider,
        base_url: baseUrl,
        model_name: voiceVal,
        is_active: isDefault,
        extra_params: {
            voice_name: chosenVoiceName || "",
            voice_label: chosenVoiceLabel || ""
        }
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
            // 同步将获取到的全部具体音色（官方自带 + 专属克隆）批量保存到数据库（音色资产库）
            let syncedCount = 0;
            try {
                const voicesToSync = [];
                const seenIds = new Set();

                // 1. 优先从 window._latestFetchedVoices 收集
                const latest = window._latestFetchedVoices;
                if (latest && latest.provider === provider) {
                    (latest.clonedVoices || []).forEach(cv => {
                        if (cv.id && !seenIds.has(cv.id)) {
                            seenIds.add(cv.id);
                            voicesToSync.push({
                                id: cv.id,
                                name: cv.name || cv.id,
                                provider_name: provider,
                                voice_type: "cloned"
                            });
                        }
                    });
                    (latest.recommendedVoices || []).forEach(rv => {
                        if (rv.val && !seenIds.has(rv.val)) {
                            seenIds.add(rv.val);
                            const cleanName = (rv.text || "").replace(/^[👑\s]+/, "").split(/[\(\（]/)[0].trim() || rv.text;
                            voicesToSync.push({
                                id: rv.val,
                                name: cleanName || rv.val,
                                provider_name: provider,
                                voice_type: "preset"
                            });
                        }
                    });
                }

                // 2. 补漏：从 meta.recommendedVoices 收集官方预设
                (meta.recommendedVoices || []).forEach(rv => {
                    if (rv.val && !seenIds.has(rv.val)) {
                        seenIds.add(rv.val);
                        const cleanName = (rv.text || "").replace(/^[👑\s]+/, "").split(/[\(\（]/)[0].trim() || rv.text;
                        voicesToSync.push({
                            id: rv.val,
                            name: cleanName || rv.val,
                            provider_name: provider,
                            voice_type: "preset"
                        });
                    }
                });

                // 3. 从 DOM 药丸中收集用户专属绑定的音色
                document.querySelectorAll("#tts-fetched-voices-container .fetched-model-pill").forEach(p => {
                    const vid = p.getAttribute("data-voice");
                    if (vid && !seenIds.has(vid)) {
                        seenIds.add(vid);
                        const isClone = p.getAttribute("data-is-clone") === "true";
                        const nameSpan = p.querySelector("span:not(.btn-delete-clone-pill)") || p;
                        const rawText = nameSpan.textContent.trim();
                        const cleanName = rawText.replace(/^[👑\s]+/, "").split(/[\(\（]/)[0].trim() || rawText;
                        voicesToSync.push({
                            id: vid,
                            name: cleanName || vid,
                            provider_name: provider,
                            voice_type: isClone ? "cloned" : "preset"
                        });
                    }
                });

                if (voicesToSync.length > 0) {
                    const syncRes = await fetch(`${API_BASE}/voices/batch-sync`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            provider_name: provider,
                            voices: voicesToSync
                        })
                    });
                    const syncJson = await syncRes.json();
                    if (syncJson.code === 0) {
                        syncedCount = syncJson.total || voicesToSync.length;
                    }
                }
            } catch (errSync) {
                console.warn("同步保存音色资产库异常:", errSync);
            }

            const syncTip = syncedCount > 0 ? `，并已同步 ${syncedCount} 款音色入库至【音色资产库】` : "";
            const successMsg = `语音配置已成功保存${syncTip}！${isDefault ? '已设为当前直播默认发音。' : '可在下方列表卡片右上角随时设为默认发音。'}`;
            showTTSTestResult(true, successMsg);
            if (typeof showToast === "function") {
                showToast(successMsg, "success", 4500);
            }
            if (statusTip) {
                statusTip.style.color = "#10B981";
                statusTip.innerHTML = `✓ 语音配置已保存${syncedCount > 0 ? ` (已同步 ${syncedCount} 款音色)` : ''}`;
                setTimeout(() => { if (statusTip) statusTip.style.display = "none"; }, 6000);
            }

            await loadSettings();
            if (typeof loadVoiceTable === "function") {
                await loadVoiceTable();
            }
            if (typeof populateAnchorVoiceSelect === "function") {
                await populateAnchorVoiceSelect(window._voiceProfilesCache || (typeof voiceCache !== "undefined" ? voiceCache : []));
            }
        } else {
            showTTSTestResult(false, "保存失败: " + json.message);
            if (typeof showToast === "function") showToast("保存失败: " + json.message, "danger");
            if (statusTip) {
                statusTip.style.color = "#EF4444";
                statusTip.innerText = "保存失败: " + json.message;
            }
        }
    } catch (e) {
        showTTSTestResult(false, "保存语音引擎出错: " + e);
        if (typeof showToast === "function") showToast("保存语音引擎出错: " + e, "danger");
        if (statusTip) {
            statusTip.style.color = "#EF4444";
            statusTip.innerText = "保存异常: " + e;
        }
    } finally {
        if (saveBtn) {
            saveBtn.disabled = false;
            saveBtn.innerHTML = origBtnHtml;
        }
    }
}

// ============================================================================
// 全局解析指定 TTS 配置项的友好音色名称（彻底隐藏底层机器ID，以中文优雅名称展示）
// ============================================================================
function resolveTTSVoiceInfo(cfg, voiceProfiles = null) {
    const voiceVal = (cfg && cfg.model_name ? cfg.model_name : "").trim();
    const provider = (cfg && cfg.provider_name ? cfg.provider_name : "").toLowerCase();

    // 1. 优先读取 extra_params 中显式保存的友好名称
    let extra = {};
    if (cfg && cfg.extra_params) {
        try {
            extra = typeof cfg.extra_params === "string" ? JSON.parse(cfg.extra_params) : cfg.extra_params;
        } catch (e) { }
    }
    if (extra && extra.voice_name && typeof extra.voice_name === "string" && extra.voice_name.trim()) {
        const savedName = extra.voice_name.trim();
        if (!savedName.startsWith("zh-CN-") && !savedName.startsWith("zh-HK-") && !savedName.startsWith("zh-TW-")) {
            const isCloneVoice = Boolean(extra.is_clone || (voiceVal && (voiceVal.includes("cloned") || voiceVal.includes("bailian"))));
            return {
                name: savedName,
                fullName: extra.voice_label || savedName,
                isClone: isCloneVoice,
                voiceId: voiceVal
            };
        }
    }

    // 2. 尝试从系统登记的声音档案库 (VoiceProfile) 中匹配
    const profiles = Array.isArray(voiceProfiles) && voiceProfiles.length > 0
        ? voiceProfiles
        : (Array.isArray(window._voiceProfilesCache) && window._voiceProfilesCache.length > 0
            ? window._voiceProfilesCache
            : (typeof voiceCache !== "undefined" && Array.isArray(voiceCache) ? voiceCache : []));

    if (voiceVal) {
        const matchedProfile = profiles.find(v => (v.id && v.id === voiceVal) || (v.voice_id && v.voice_id === voiceVal));
        if (matchedProfile && matchedProfile.name && !matchedProfile.name.startsWith("zh-CN-")) {
            const isClone = matchedProfile.voice_type === "cloned" || matchedProfile.is_clone;
            return {
                name: matchedProfile.name.trim(),
                fullName: `${matchedProfile.name.trim()}${isClone ? ' (专属克隆)' : ' (官方预设)'}`,
                isClone: Boolean(isClone),
                voiceId: voiceVal
            };
        }
    }

    // 3. 从全局友好音色字典匹配 (TTS_VOICE_FRIENDLY_NAMES)
    if (voiceVal && TTS_VOICE_FRIENDLY_NAMES[voiceVal]) {
        const fullDesc = TTS_VOICE_FRIENDLY_NAMES[voiceVal];
        const cleanName = fullDesc.split(/[\(\（]/)[0].trim();
        return {
            name: cleanName,
            fullName: fullDesc,
            isClone: false,
            voiceId: voiceVal
        };
    }

    // 4. 尝试从预设生态推荐音色库中匹配 (recommendedVoices)
    const pMeta = typeof resolveTTSProviderMeta === "function" ? resolveTTSProviderMeta(provider, cfg) : null;
    const recs = (pMeta && pMeta.recommendedVoices) ? pMeta.recommendedVoices : [];
    let matchedRec = recs.find(v => v.val === voiceVal);

    if (!matchedRec && typeof BUILTIN_TTS_ECOSYSTEM !== "undefined") {
        for (const eco of BUILTIN_TTS_ECOSYSTEM) {
            matchedRec = (eco.recommendedVoices || []).find(v => v.val === voiceVal);
            if (matchedRec) break;
        }
    }

    if (matchedRec) {
        const cleanName = (matchedRec.text || "").replace(/^[👑\s]+/, "").split(/[\(\（]/)[0].trim() || matchedRec.text;
        return {
            name: cleanName,
            fullName: matchedRec.text,
            isClone: false,
            voiceId: voiceVal
        };
    }

    // 5. 针对 Edge-TTS 的正则中文名称智能提取 (例如 zh-CN-XiaoxiaoNeural -> 晓晓)
    if (voiceVal.startsWith("zh-")) {
        const matchZh = voiceVal.match(/zh-[A-Za-z]+-([A-Za-z]+)Neural/i);
        if (matchZh && matchZh[1]) {
            const nameRaw = matchZh[1];
            const pinyinMap = {
                "Xiaoxiao": "晓晓", "Yunxi": "云希", "Yunjian": "云健", "Xiaoyi": "晓伊",
                "Yunyang": "云扬", "Xiaochen": "晓辰", "Xiaohan": "晓涵", "Xiaomeng": "晓梦",
                "Xiaomo": "晓墨", "Xiaoqiu": "晓秋", "Xiaorui": "晓睿", "Xiaoxuan": "晓萱",
                "Xiaoyan": "晓颜", "Xiaoyou": "晓悠", "Yunfeng": "云枫", "Yunhao": "云皓",
                "Yunxia": "云夏", "Yunye": "云野", "Yunze": "云泽"
            };
            const zhName = pinyinMap[nameRaw] || nameRaw;
            return {
                name: zhName,
                fullName: `${zhName} (超自然微软云语音)`,
                isClone: false,
                voiceId: voiceVal
            };
        }
    }

    // 6. 针对百炼/CosyVoice 专属 Voice-ID 的智能语义识别
    const isBailianOrCosy = provider.includes("cosy") || voiceVal.includes("bailian") || voiceVal.includes("cosyvoice") || voiceVal.includes("qwen-audio");
    if (isBailianOrCosy && voiceVal) {
        const realClones = profiles.filter(v => (v.id || "").toLowerCase() !== "voice_default_female");
        if (realClones.length > 0) {
            if (realClones.length === 1 && realClones[0].name) {
                return {
                    name: realClones[0].name.trim(),
                    fullName: `${realClones[0].name.trim()} (专属克隆)`,
                    isClone: true,
                    voiceId: voiceVal
                };
            }
            const similar = realClones.find(v => (v.id && voiceVal.includes(v.id.substring(0, 16))) || (v.id && v.id.includes(voiceVal.substring(0, 16))));
            if (similar && similar.name) {
                return {
                    name: similar.name.trim(),
                    fullName: `${similar.name.trim()} (专属克隆)`,
                    isClone: true,
                    voiceId: voiceVal
                };
            }
        }
        return {
            name: "专属克隆音色",
            fullName: "百炼专属克隆音色",
            isClone: true,
            voiceId: voiceVal
        };
    }

    // 7. 终极保护：杜绝暴露生硬代码
    if (!voiceVal) {
        return { name: "默认音色", fullName: "默认音色", isClone: false, voiceId: "" };
    }
    return {
        name: "官方推荐音色",
        fullName: `官方推荐音色 (${voiceVal})`,
        isClone: false,
        voiceId: voiceVal
    };
}

// 渲染已配置的 TTS 清单 (100% 真实数据驱动)
function renderConfiguredTTS(configs) {
    const container = document.getElementById("configured-tts-list");
    const countBadge = document.getElementById("configured-tts-count");
    if (!container) return;

    // 异步确保声音档案库缓存就绪并静默更新
    if (!window._voiceProfilesCache && !window._fetchingVoiceProfiles) {
        window._fetchingVoiceProfiles = true;
        fetch(`${API_BASE}/voices/list`)
            .then(res => res.json())
            .then(json => {
                if (json.code === 0 && Array.isArray(json.data)) {
                    window._voiceProfilesCache = json.data;
                    if (typeof voiceCache !== "undefined") voiceCache = json.data;
                    renderConfiguredTTS(configs);
                }
            })
            .catch(() => {})
            .finally(() => {
                window._fetchingVoiceProfiles = false;
            });
    }

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
        card.className = "configured-tts-card" + (cfg.is_active ? " is-active" : "");

        const voiceInfo = resolveTTSVoiceInfo(cfg, window._voiceProfilesCache || (typeof voiceCache !== "undefined" ? voiceCache : []));

        const topCornerHtml = cfg.is_active
            ? `<div class="badge-active-brain">★ 默认生效发音</div>`
            : `<button class="btn-card-set-default-tts" onclick="handleSetActiveTTS('${cfg.id}')" title="设为当前直播默认发音">
                 <svg viewBox="0 0 24 24" style="width: 12px; height: 12px; stroke-width: 2.2;"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>
                 设为默认发音
               </button>`;

        const configuredLogoSrc = (tMeta.logoSvg || "").startsWith("http") ? tMeta.logoSvg : (tMeta.logoSvg + "?v=20260917_v6");

        // 音色名称药丸徽标：专属克隆使用尊贵金色高亮并带皇冠，官方使用自然微光绿徽标，悬停展示完整音色名与底层 ID
        const voiceBadgeHtml = voiceInfo.isClone
            ? `<span class="brand-badge green" style="font-size: 11px; padding: 2px 7px; font-weight: 600; border-color: rgba(245, 158, 11, 0.45); background: rgba(245, 158, 11, 0.12); color: #FBBF24; display: inline-flex; align-items: center; gap: 4px;" title="专属声音克隆: ${escapeHtml(voiceInfo.fullName)} | Voice-ID: ${escapeHtml(voiceInfo.voiceId || cfg.model_name)}">
                 <span style="font-size: 11px; line-height: 1;">👑</span>
                 <span>${escapeHtml(voiceInfo.name)}</span>
               </span>`
            : `<span class="brand-badge green" style="font-size: 11px; padding: 2px 7px; font-weight: 600;" title="发音音色: ${escapeHtml(voiceInfo.fullName)} | ID: ${escapeHtml(voiceInfo.voiceId || cfg.model_name)}">
                 ${escapeHtml(voiceInfo.name)}
               </span>`;

        card.innerHTML = `
            <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; width: 100%; min-width: 0; box-sizing: border-box;">
                <div class="configured-llm-info">
                    <img src="${configuredLogoSrc}" alt="${tMeta.name}" style="width: 42px; height: 42px; border-radius: 10px; object-fit: cover; flex-shrink: 0; background: transparent; border: none; padding: 0; box-shadow: 0 4px 12px rgba(0,0,0,0.35);">
                    <div style="min-width: 0; flex: 1;">
                        <div style="display: flex; align-items: center; gap: 6px; flex-wrap: wrap;">
                            <strong style="font-size: 14px; color: #FFFFFF;">${tMeta.name}</strong>
                            ${voiceBadgeHtml}
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
            if (typeof populateAnchorVoiceSelect === "function") {
                await populateAnchorVoiceSelect(window._voiceProfilesCache || (typeof voiceCache !== "undefined" ? voiceCache : []));
            }
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

            // 1. 渲染大模型网格与多模型卡片列表 (高韧性防护)
            const selLLM = (typeof currentSelectedLLMProvider !== "undefined" && currentSelectedLLMProvider)
                ? currentSelectedLLMProvider
                : (window.currentSelectedLLMProvider || "deepseek");
            if (typeof renderLLMEcosystemGrid === "function") {
                renderLLMEcosystemGrid(selLLM);
            }
            if (typeof renderConfiguredLLMs === "function") {
                renderConfiguredLLMs(cachedAllConfigs);
            }

            // 若大模型编辑区尚未与激活项绑定过，优先把激活的大模型同步到编辑面板
            const activeLLM = cachedAllConfigs.find(c => c.config_group === "llm" && c.is_active);
            const llmEditorConfigEl = document.getElementById("llm-editor-config-id");
            if (typeof selectLLMProvider === "function") {
                if (activeLLM && llmEditorConfigEl && !llmEditorConfigEl.value) {
                    const pMeta = typeof resolveLLMProviderMeta === "function" ? resolveLLMProviderMeta(activeLLM.provider_name, activeLLM) : { id: activeLLM.provider_name };
                    selectLLMProvider(pMeta.id, activeLLM);
                } else if (llmEditorConfigEl && !llmEditorConfigEl.value) {
                    selectLLMProvider(selLLM);
                }
            }

            // 2. 渲染语音合成生态网格与已配置发音清单 (严格以用户端配置为准进行选中；无配置时默认本地引擎)
            const resolvedTTS = (typeof resolveActiveOrPreferredTTSProvider === "function")
                ? resolveActiveOrPreferredTTSProvider(cachedAllConfigs)
                : { providerId: "moss_tts_nano", config: null };

            const targetTTSId = resolvedTTS.providerId;
            const targetTTSConfig = resolvedTTS.config;

            window.currentSelectedTTSProvider = targetTTSId;
            if (typeof currentSelectedTTSProvider !== "undefined") currentSelectedTTSProvider = targetTTSId;

            if (typeof renderTTSEcosystemGrid === "function") renderTTSEcosystemGrid(targetTTSId);
            if (typeof renderConfiguredTTS === "function") renderConfiguredTTS(cachedAllConfigs);
            if (typeof selectTTSProvider === "function") {
                await selectTTSProvider(targetTTSId, targetTTSConfig);
            }

            // 联动刷新主播管理页的绑定音色下拉列表与当前生效引擎提示
            if (typeof populateAnchorVoiceSelect === "function") {
                const vList = window._voiceProfilesCache || (typeof voiceCache !== "undefined" ? voiceCache : []);
                populateAnchorVoiceSelect(vList);
            }

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
        alert("请输入服务商标识（如 my_service）");
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

let selectedWizardAnchorId = "";

async function initWizard() {
    await renderModeCards();
    await renderWizardRoleCards();
    await restoreWizardState();
    await renderWizardAnchorSelect(wizardRoleType);
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
            if (json.data.selected_anchor_id) {
                selectedWizardAnchorId = json.data.selected_anchor_id;
            }
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
            ? `<img src="${r.avatarSvg}" alt="${r.name}" style="width: 48px; height: 48px; border-radius: 50%; border: 2px solid ${r.color}; display: block; box-shadow: 0 3px 10px ${r.color}33; background: #141416; object-fit: cover;">`
            : svg(r.icon, "icon-xl");

        card.innerHTML = `
            <div style="flex-shrink: 0; display: flex; align-items: center; justify-content: center;">
                ${avatarEl}
            </div>
            <div style="flex: 1; min-width: 0; text-align: left;">
                <div class="mode-card-name" style="color: ${r.color}; font-weight: 700; margin: 0 0 4px 0; font-size: 15px; line-height: 1.35;">
                    ${r.name}
                </div>
                <div class="mode-card-desc" style="font-size: 11.5px; color: var(--text-secondary); line-height: 1.5; margin: 0; word-break: break-word;">
                    ${r.desc}
                </div>
            </div>
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
    renderWizardAnchorSelect(roleType);
}

async function renderWizardAnchorSelect(roleType) {
    const container = document.getElementById("wizard-anchor-select-container");
    if (!container) return;

    try {
        const res = await fetch(`${API_BASE}/anchors/list`);
        const json = await res.json();
        const allAnchors = (json.code === 0 && Array.isArray(json.data)) ? json.data : [];
        if (!selectedWizardAnchorId) {
            selectedWizardAnchorId = json.selected_anchor_id || window._currentLiveAnchorId || "";
        }

        const matchedAnchors = allAnchors.filter(a => {
            const t = (a.anchor_type || "").toLowerCase();
            if (roleType === "chitchat") return t === "chitchat" || t === "chat";
            return t === roleType.toLowerCase();
        });

        const typeLabels = {
            ecommerce: "带货主播",
            entertainment: "娱乐主播",
            expert: "专业专家",
            chitchat: "闲聊扯淡"
        };
        const currentTypeName = typeLabels[roleType] || "当前类型";

        if (matchedAnchors.length === 0) {
            container.innerHTML = `
                <div style="display: flex; justify-content: space-between; align-items: center; background: rgba(30, 41, 59, 0.4); border: 1px dashed rgba(148, 163, 184, 0.25); border-radius: 8px; padding: 12px 16px;">
                    <div style="font-size: 12.5px; color: var(--text-secondary);">
                        💡 当前「<strong style="color:var(--text-primary);">${currentTypeName}</strong>」下暂未录入专属主播档案，开播将直接采用系统默认人设与音色。
                    </div>
                    <button type="button" class="btn btn-sm" onclick="switchToTab('anchors')" style="font-size: 12px; color: var(--accent-emerald); border-color: rgba(16,185,129,0.3); padding: 4px 12px;">
                        前往「主播管理」添加 ↗
                    </button>
                </div>
            `;
            return;
        }

        if (!selectedWizardAnchorId || !matchedAnchors.some(a => a.id === selectedWizardAnchorId)) {
            selectedWizardAnchorId = matchedAnchors[0].id;
        }

        container.innerHTML = `
            <div style="margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
                <label style="font-size: 13px; font-weight: 600; color: var(--text-primary); display: flex; align-items: center; gap: 6px;">
                    <svg class="icon-sm" viewBox="0 0 24 24" style="width: 14px; height: 14px;"><circle cx="12" cy="7" r="4"/><path d="M6 21v-2a6 6 0 0 1 12 0v2"/></svg>
                    选择「${currentTypeName}」类型的出镜主播：
                </label>
                <span style="font-size: 12px; color: var(--text-muted);">共 ${matchedAnchors.length} 位对应类型主播，点击指定出镜档案</span>
            </div>
            <div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 10px;">
                ${matchedAnchors.map(a => {
            const isSelected = a.id === selectedWizardAnchorId;
            const avatarSrc = a.photo_portrait || a.photo_full_body || (ROLE_CARD_META[roleType] ? ROLE_CARD_META[roleType].avatarSvg : "/static/svg/default_avatar.svg");
            return `
                        <div class="wizard-anchor-card ${isSelected ? 'selected' : ''}" onclick="selectWizardAnchor('${a.id}')" style="display: flex; align-items: center; gap: 10px; padding: 8px 12px; border-radius: 8px; cursor: pointer; transition: all 0.2s; background: ${isSelected ? 'rgba(16, 185, 129, 0.12)' : 'rgba(30, 41, 59, 0.5)'}; border: 1.5px solid ${isSelected ? '#10B981' : 'rgba(148, 163, 184, 0.2)'}; box-shadow: ${isSelected ? '0 2px 10px rgba(16, 185, 129, 0.2)' : 'none'};">
                            <img src="${avatarSrc}" style="width: 38px; height: 38px; border-radius: 6px; flex-shrink: 0; object-fit: cover; border: 1px solid rgba(148, 163, 184, 0.25);">
                            <div style="flex: 1; min-width: 0;">
                                <div style="font-size: 13px; font-weight: 700; color: ${isSelected ? '#10B981' : 'var(--text-primary)'}; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">
                                    ${escapeHtml(a.name)}
                                </div>
                                <div style="font-size: 11px; color: var(--text-secondary); margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">
                                    音色: ${escapeHtml(a.voice_name || '默认音色')}
                                </div>
                            </div>
                            <div style="font-size: 12px; color: ${isSelected ? '#10B981' : 'transparent'}; font-weight: 700;">
                                ✓
                            </div>
                        </div>
                    `;
        }).join("")}
            </div>
        `;
    } catch (e) {
        console.error("加载向导主播列表失败", e);
    }
}

function selectWizardAnchor(anchorId) {
    selectedWizardAnchorId = anchorId;
    renderWizardAnchorSelect(wizardRoleType);
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

        // 4. 持久化保存向导中选定的出镜主播档案
        if (selectedWizardAnchorId) {
            try {
                await fetch(`${API_BASE}/settings/selected-anchor`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ anchor_id: selectedWizardAnchorId })
                });
                window._currentLiveAnchorId = selectedWizardAnchorId;
            } catch (_) { }
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
    if (tabName === "wizard" && typeof loadWizardAvatarProviders === "function") loadWizardAvatarProviders();
    if (tabName === "gpu" && typeof loadGpuAvatarProviders === "function") loadGpuAvatarProviders();
}
window.switchToTab = switchToTab;

// ============================================================================
// 开播前检查 (Preflight)：真实探测开播条件，未通过不盲目开播 (v1.1.3)
// ============================================================================
let lastPreflightData = null;

async function runPreflight(opts = {}) {
    try {
        // 如果是人工查看且未强制弹窗，立即平滑切换到独立全幅页面并呈现加载占位
        if (!opts.auto && !opts.useModal) {
            closePreflightModal();
            switchToTab("preflight");
            const checksBox = document.getElementById("tab-preflight-checks");
            if (checksBox) {
                checksBox.innerHTML = `
                    <div style="text-align: center; padding: 46px 20px; color: var(--text-secondary); background: rgba(15, 23, 42, 0.4); border-radius: 8px; border: 1px dashed rgba(148, 163, 184, 0.2);">
                        <svg class="icon-lg spin" viewBox="0 0 24 24" style="width: 28px; height: 28px; margin-bottom: 12px; color: var(--accent-emerald);"><line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"/><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"/></svg>
                        <div style="font-size: 14.5px; font-weight: 700; color: #F8FAFC;">正在对算力、大模型、声音与渲染通道执行全链路真实连通性体检...</div>
                        <div style="font-size: 12px; color: var(--text-muted); margin-top: 6px;">预计耗时 1~2 秒，请稍候</div>
                    </div>
                `;
            }
        }

        const res = await fetch(`${API_BASE}/live/preflight`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const json = await res.json();
        if (json.code !== 0 || !json.data || !Array.isArray(json.data.checks)) {
            throw new Error(json.detail || json.message || "检查响应格式无效");
        }
        lastPreflightData = json.data;

        const fails = json.data.checks.filter(c => c.status === "fail").length;
        const warns = json.data.checks.filter(c => c.status === "warn").length;

        // 开播触发的自动检查：全部通过时静默放行
        if (opts.auto && fails === 0 && warns === 0) {
            showToast("开播前检查全部通过 ✓ 正在启动直播...", "success");
            return json.data;
        }

        if (opts.useModal) {
            renderPreflightModal(json.data, opts);
        } else {
            // 核心：无弹窗，渲染到独立的体检大页面中
            renderPreflightTabPage(json.data, opts);
        }
        return json.data;
    } catch (e) {
        showToast("开播前检查请求异常: " + e, "error");
        return null;
    }
}

function pfRowHtml(c) {
    const icons = { pass: "check", warn: "warn", fail: "x" };
    const statusLabels = { pass: "检测通过", warn: "建议修复", fail: "必须修复" };
    const badgeClass = c.status === "pass" ? "pill-pass" : (c.status === "warn" ? "pill-warning" : "pill-missing");
    let fixHtml = "";
    if (c.status !== "pass" && (c.fix_hint || c.action_tab)) {
        const hintPart = c.fix_hint
            ? `<div class="pf-fix" style="flex: 1; min-width: 0; margin-top: 0;">
                 <div class="pf-fix-left">
                   ${svg("info", "icon-sm")}
                   <span>${c.fix_hint}</span>
                 </div>
               </div>`
            : "";
        const btnPart = c.action_tab
            ? `<button type="button" class="pf-btn-fix" style="${hintPart ? '' : 'margin-left: auto;'}" onclick="preflightGoFix('${c.action_tab}')">·前往处理</button>`
            : "";
        fixHtml = `
            <div style="display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 10px;">
                ${hintPart}
                ${btnPart}
            </div>
        `;
    }
    return `
        <div class="pf-row pf-${c.status}">
            <div class="pf-icon">${svg(icons[c.status] || "info")}</div>
            <div style="flex: 1; min-width: 0;">
                <div style="display: flex; align-items: center; justify-content: space-between; gap: 8px;">
                    <div class="pf-title">${c.title}</div>
                    <span class="prereq-pill ${badgeClass}" style="font-size: 11px; height: 22px; padding: 0 8px; box-sizing: border-box; display: inline-flex; align-items: center;">
                        <span class="prereq-dot"></span>
                        ${statusLabels[c.status] || c.status}
                    </span>
                </div>
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
    let startLiveBtn = "";
    if (!isLiveStreaming && data.ready) {
        const label = data.checks.some(c => c.status === "warn")
            ? "仍有建议项，确认启动直播源"
            : "▶ 启动本地直播源";
        startLiveBtn = `<button class="btn btn-primary" style="font-weight: 700; padding: 7px 20px;" onclick="preflightStartLive()">${label}</button>`;
    }
    let warningBar = "";
    if (!data.ready) {
        warningBar = `
            <div class="pf-modal-warning-bar" style="margin-right: auto;">
                ${svg("warn", "icon-sm")}
                <span>存在未通过项，请先处理上方未通过项再开播</span>
            </div>
        `;
    }
    const buttons = `
        ${warningBar}
        <div style="display: flex; gap: 10px; align-items: center; margin-left: auto; flex-wrap: wrap;">
            <button class="btn" style="font-weight: 600; padding: 7px 18px;" onclick="closePreflightModal()">稍后处理</button>
            <button class="btn btn-secondary" style="font-weight: 600; padding: 7px 18px;" onclick="runPreflight({ auto: false })">🔄 重新体检</button>
            ${startLiveBtn}
        </div>
    `;
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

function renderPreflightTabPage(data, opts = {}) {
    const checksBox = document.getElementById("tab-preflight-checks");
    const summaryEl = document.getElementById("tab-preflight-summary");
    const actionsEl = document.getElementById("tab-preflight-actions");
    const badgeEl = document.getElementById("tab-preflight-badge");
    const passCountEl = document.getElementById("tab-stat-pass-count");
    const warnCountEl = document.getElementById("tab-stat-warn-count");
    const failCountEl = document.getElementById("tab-stat-fail-count");

    if (!checksBox) return;

    const passes = data.checks.filter(c => c.status === "pass").length;
    const warns = data.checks.filter(c => c.status === "warn").length;
    const fails = data.checks.filter(c => c.status === "fail").length;

    if (passCountEl) passCountEl.innerText = passes;
    if (warnCountEl) warnCountEl.innerText = warns;
    if (failCountEl) failCountEl.innerText = fails;

    if (badgeEl) {
        if (fails > 0) {
            badgeEl.className = "brand-badge red";
            badgeEl.innerText = `🔴 ${fails} 项必须修复`;
        } else if (warns > 0) {
            badgeEl.className = "brand-badge amber";
            badgeEl.innerText = `🟡 ${warns} 项建议关注`;
        } else {
            badgeEl.className = "brand-badge green";
            badgeEl.innerText = "🟢 全部指标就绪";
        }
    }

    if (summaryEl) {
        summaryEl.innerText = data.summary || "全链路开播硬件、网络与服务连通性体检已完成。";
    }

    checksBox.innerHTML = data.checks.map(pfRowHtml).join("");

    // 底部主操作区组装：全部操作项靠右排列，启动按钮置于重新体检右侧
    let startLiveBtn = "";
    if (!isLiveStreaming && data.ready) {
        const label = warns > 0 ? "仍有建议项，确认启动直播源" : "▶ 启动本地直播源 (进入直播大屏)";
        startLiveBtn = `<button class="btn btn-primary" style="font-weight: 700; padding: 8px 22px; font-size: 13.5px;" onclick="preflightStartLiveFromTab()">${label}</button>`;
    }

    let warningBar = "";
    if (!data.ready) {
        warningBar = `
            <div class="pf-modal-warning-bar" style="margin-right: auto; padding: 6px 14px; border-radius: 6px; font-size: 13px;">
                ${svg("warn", "icon-sm")}
                <span>存在阻断开播的未通过项，请先点击对应项右侧的「前往处理」完成配置</span>
            </div>
        `;
    }

    const buttons = `
        ${warningBar}
        <div style="display: flex; gap: 10px; align-items: center; margin-left: auto; flex-wrap: wrap;">
            <button class="btn btn-ghost" style="font-weight: 600; padding: 7px 18px; font-size: 13px; border: 1px solid rgba(148, 163, 184, 0.25);" onclick="switchToTab('wizard')">← 返回开播向导</button>
            <button class="btn btn-secondary" style="font-weight: 600; padding: 7px 18px; font-size: 13px;" onclick="runPreflight({ auto: false, toTab: true })">🔄 重新体检</button>
            ${startLiveBtn}
        </div>
    `;
    if (actionsEl) actionsEl.innerHTML = buttons;

    // 平滑滚动回顶部
    window.scrollTo({ top: 0, behavior: "smooth" });
}
window.renderPreflightTabPage = renderPreflightTabPage;

async function preflightStartLiveFromTab() {
    const pf = await runPreflight({ auto: true });
    if (!pf || !pf.ready) {
        showToast("最新开播检查未通过，请先处理未通过项", "warning");
        return;
    }
    closePreflightModal();
    await startLiveDirect();
}
window.preflightStartLiveFromTab = preflightStartLiveFromTab;

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

// FAQ 问答卡片下拉展开/收起切换
function toggleFaqAnswer(id, btn) {
    const el = document.getElementById(id);
    if (!el) return;
    const isHidden = (el.style.display === "none" || !el.style.display);
    el.style.display = isHidden ? "flex" : "none";
    if (btn) {
        btn.innerText = isHidden ? "收起说明 ▴" : "查看说明 ▾";
    }
}

let _loadingAnchorsPromise = null;

async function loadAnchors() {
    // 1. [缓存优先秒开] 若内存中已有主播列表，0ms 瞬间渲染出表格，杜绝任何白屏等待
    if (Array.isArray(anchorCache) && anchorCache.length > 0) {
        renderAnchorsTable(anchorCache, window._currentLiveAnchorId || "");
    }

    // 2. [防抖去重] 若当前已有正在飞行的拉取请求，直接复用同一个 Promise，杜绝重复并发
    if (_loadingAnchorsPromise) {
        return _loadingAnchorsPromise;
    }

    _loadingAnchorsPromise = (async () => {
        try {
            // 3. [纯本地数据库直出] 单次请求直出全部主播数据、音色中文名与当前开播状态，零远程依赖，< 5ms 极速返回
            const res = await fetch(`${API_BASE}/anchors/list`);
            const json = await res.json();

            if (json.code === 0 && Array.isArray(json.data)) {
                anchorCache = json.data;
                const currentAnchorId = json.selected_anchor_id || window._currentLiveAnchorId || "";
                window._currentLiveAnchorId = currentAnchorId;

                // 4. 本地数据到达，立即毫秒级更新渲染表格
                renderAnchorsTable(anchorCache, currentAnchorId);
                // 同步数字人资产工场的主播下拉框
                syncAvatarTaskAnchorSelect(anchorCache);
                syncAvatarActionAnchorSelect(anchorCache);
            }

            // 5. [后台静默填充] 仅为上方表单的“绑定音色”下拉框提供选项，绝不阻塞主播表格展示
            syncAnchorVoiceOptions();
            // 6. 加载数字人视频切片任务列表
            loadAvatarTasks();
            // 7. 加载动作视频状态机与电商带货场景智能绑定配置
            loadAvatarActions();
            // 8. 加载数字人软硬件达标体检看板
            loadDigitalHumanHardwareRequirements();
        } catch (e) {
            console.error("加载主播失败", e);
        } finally {
            _loadingAnchorsPromise = null;
        }
    })();

    return _loadingAnchorsPromise;
}

// 独立的快速表格渲染函数 (纯本地数据计算，< 2ms 渲染完成)
function renderAnchorsTable(anchors, currentAnchorId) {
    const tbody = document.getElementById("anchors-tbody");
    if (!tbody) return;
    tbody.innerHTML = "";
    if (!anchors || anchors.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; color:var(--text-muted); padding: 20px;">暂无主播档案，请在上方创建</td></tr>';
        return;
    }

    const ANCHOR_TYPE_MAP = {
        ecommerce: {
            label: "带货主播",
            sub: "促单逼单",
            icon: "🛍️",
            style: "background-color: rgba(16, 185, 129, 0.14) !important; color: #34d399 !important; border: 1px solid rgba(16, 185, 129, 0.38) !important;",
            chevron: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%2334d399' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E"
        },
        entertainment: {
            label: "娱乐主播",
            sub: "逗梗陪伴",
            icon: "🎭",
            style: "background-color: rgba(236, 72, 153, 0.14) !important; color: #f472b6 !important; border: 1px solid rgba(236, 72, 153, 0.38) !important;",
            chevron: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23f472b6' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E"
        },
        expert: {
            label: "专业专家",
            sub: "前置免责",
            icon: "⚖️",
            style: "background-color: rgba(56, 189, 248, 0.14) !important; color: #38bdf8 !important; border: 1px solid rgba(56, 189, 248, 0.38) !important;",
            chevron: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%2338bdf8' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E"
        },
        chat: {
            label: "闲聊扯淡",
            sub: "唠嗑搭子",
            icon: "☕",
            style: "background-color: rgba(251, 146, 60, 0.14) !important; color: #fb923c !important; border: 1px solid rgba(251, 146, 60, 0.38) !important;",
            chevron: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23fb923c' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E"
        }
    };

    anchors.forEach(a => {
        // 直接使用本地数据库联表直出的音色中文名称，无需等待前端额外拉取音色库匹配
        const voiceDisplayName = a.voice_name || (voiceCache && voiceCache.find(v => v.id === a.voice_id)?.name) || (a.voice_id ? a.voice_id : "未绑定");
        const voiceName = escapeHtml(voiceDisplayName);
        const portrait = a.photos && a.photos.portrait;
        const photoCount = Object.values(a.photos || {}).filter(p => p).length;
        const thumb = portrait
            ? `<img src="/static-file?path=${encodeURIComponent(portrait)}" style="width: 38px; height: 38px; object-fit: cover; border-radius: 50%; border: 1px solid var(--border-subtle); vertical-align: middle; margin-right: 6px;" onerror="this.src='/static/svg/default_avatar.svg'"> `
            : `<img src="/static/svg/default_avatar.svg" style="width: 38px; height: 38px; object-fit: cover; border-radius: 50%; border: 1px solid var(--border-subtle); vertical-align: middle; margin-right: 6px;"> `;
        const isCurrent = a.is_current_live || (a.id === currentAnchorId);
        const currentTag = isCurrent
            ? '<span class="badge-recommend" style="font-size: 10px; padding: 2px 8px; margin-left: 6px;">● 开播主播</span>'
            : "";

        const typeCfg = ANCHOR_TYPE_MAP[a.anchor_type] || ANCHOR_TYPE_MAP.ecommerce;
        const currentType = a.anchor_type || "ecommerce";

        const typeSelectorHtml = `
            <select class="anchor-table-type-select"
                    onchange="quickChangeAnchorType('${a.id}', this.value, '${escapeHtml(a.name)}')"
                    title="点击可直接快速切换主播人设定位与话术风格 (实时保存)"
                    style="${typeCfg.style} background-image: url('${typeCfg.chevron}');">
                <option value="ecommerce" ${currentType === "ecommerce" ? "selected" : ""}>🛍️ 带货主播（促单逼单）</option>
                <option value="entertainment" ${currentType === "entertainment" ? "selected" : ""}>🎭 娱乐主播（逗梗陪伴）</option>
                <option value="expert" ${currentType === "expert" ? "selected" : ""}>⚖️ 专业专家（咨询法理前置免责）</option>
                <option value="chat" ${currentType === "chat" ? "selected" : ""}>☕ 闲聊扯淡（唠嗑搭子）</option>
            </select>
        `;

        const hasTrainedAvatar = Boolean(a.avatar_asset_dir);
        let statusBadgeHtml = '<span style="font-size:11px; color:var(--text-muted);">⚪ 待制作数字人</span>';
        let actionButtonsHtml = '';

        if (hasTrainedAvatar) {
            const meta = a.avatar_meta || {};
            const frameInfo = meta.frame_count ? `${meta.frame_count}帧 · ${meta.width || 1280}x${meta.height || 720}` : '25 FPS模型就绪';
            statusBadgeHtml = `
                <div style="display: flex; flex-direction: column; gap: 4px; white-space: nowrap;">
                    <div style="display: flex; align-items: center; gap: 6px; flex-wrap: nowrap; white-space: nowrap;">
                        <span class="badge-recommend" style="font-size: 11.5px; padding: 2px 8px; background: rgba(16,185,129,0.15); color: #34d399; border: 1px solid rgba(16,185,129,0.3); white-space: nowrap; flex-shrink: 0;" title="已关联连续帧切片、面部关键点与口型坐标资产">🎬 视频数字人 (就绪)</span>
                        <button class="btn btn-sm" style="background: rgba(56, 189, 248, 0.12); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.35); padding: 2px 8px; font-size: 11.5px; white-space: nowrap; flex-shrink: 0;" onclick="openAvatarPreviewModal('${a.id}')" title="预览该数字人的动态母轨视频与切片关键指标">👀 预览</button>
                        <button class="btn btn-sm" style="background: rgba(245, 158, 11, 0.12); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.35); padding: 2px 8px; font-size: 11.5px; white-space: nowrap; flex-shrink: 0;" onclick="openAnchorActionsModal('${a.id}', '${escapeHtml(a.name)}')" title="配置该主播的小黄车/致谢/欢迎手势与动作切片">🎭 动作切片</button>
                    </div>
                    <div style="display: flex; align-items: center; gap: 8px; font-size: 11px; white-space: nowrap; line-height: 1.4;">
                        <span style="color: var(--text-muted); font-family: monospace;">${frameInfo}</span>
                        <span style="color: #38bdf8;" title="开播需 GPU >= 4GB 显存或远端租赁 GPU 协同加速">⚡ 开播驱动需 GPU / 云端算力</span>
                    </div>
                </div>
            `;
            actionButtonsHtml = `
                <button class="btn btn-sm" onclick="editAnchor('${a.id}')" title="编辑主播基本档案">编辑</button>
                <button class="btn btn-sm" style="background: rgba(245, 158, 11, 0.12); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.35); padding: 2px 8px;" onclick="openAnchorActionsModal('${a.id}', '${escapeHtml(a.name)}')" title="配置该主播的小黄车/致谢/欢迎手势与动作切片">🎭 动作切片</button>
                <button class="btn btn-sm" style="background: rgba(56, 189, 248, 0.12); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.35); padding: 2px 8px;" onclick="openAvatarPreviewModal('${a.id}')" title="预览该数字人的动态母轨视频与切片关键指标">👀 预览数字人</button>
                <button class="btn btn-sm" style="background: rgba(16, 185, 129, 0.12); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.35); padding: 2px 8px;" onclick="quickStartAvatarFor('${a.id}', '${escapeHtml(a.name)}')" title="重新上传出镜视频并覆盖制作数字人模型">🔄 重新制作</button>
                <button class="btn btn-sm btn-danger" onclick="deleteAnchor('${a.id}', '${escapeHtml(a.name)}')">删除</button>
            `;
        } else if (a.avatar_task_status === "processing" || a.avatar_task_status === "pending") {
            const prog = typeof a.avatar_task_progress === "number" ? a.avatar_task_progress : 0;
            statusBadgeHtml = `
                <div style="display: flex; flex-direction: column; gap: 4px;">
                    <span class="badge-recommend" style="font-size:11px; padding:2px 8px; background:rgba(245,158,11,0.15); color:#fbbf24; border:1px solid rgba(245,158,11,0.3);" title="${escapeHtml(a.avatar_task_stage || '视频切片制作中')}">⏳ 切片制作中 (${prog}%)</span>
                    <div style="width: 100%; max-width: 120px; height: 4px; background: rgba(255,255,255,0.1); border-radius: 2px; overflow: hidden;">
                        <div style="width: ${prog}%; height: 100%; background: #fbbf24; transition: width 0.3s ease;"></div>
                    </div>
                </div>
            `;
            actionButtonsHtml = `
                <button class="btn btn-sm" onclick="editAnchor('${a.id}')" title="编辑主播基本档案">编辑</button>
                <button class="btn btn-sm" disabled style="opacity: 0.7; background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.3); padding: 2px 8px;" title="数字人切片提取中，请稍候">⏳ 制作中 (${prog}%)</button>
                <button class="btn btn-sm btn-danger" onclick="deleteAnchor('${a.id}', '${escapeHtml(a.name)}')">删除</button>
            `;
        } else if (a.avatar_task_status === "failed") {
            statusBadgeHtml = '<span style="font-size:11px; padding:2px 8px; background:rgba(239,68,68,0.15); color:#f87171; border:1px solid rgba(239,68,68,0.3);" title="视频切片提取失败，可重新上传或重试">⚠️ 制作失败 (可重试)</span>';
            actionButtonsHtml = `
                <button class="btn btn-sm" onclick="editAnchor('${a.id}')" title="编辑主播基本档案">编辑</button>
                <button class="btn btn-sm" style="background: rgba(16, 185, 129, 0.12); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.35); padding: 2px 8px;" onclick="quickStartAvatarFor('${a.id}', '${escapeHtml(a.name)}')" title="重新上传出镜视频并制作高保真数字人模型">🎬 重试制作</button>
                <button class="btn btn-sm btn-danger" onclick="deleteAnchor('${a.id}', '${escapeHtml(a.name)}')">删除</button>
            `;
        } else if (portrait) {
            statusBadgeHtml = `
                <div style="display: flex; flex-direction: column; gap: 2px;">
                    <span class="badge-recommend" style="font-size:11px; padding:2px 8px; background:rgba(56,189,248,0.15); color:#38bdf8; border:1px solid rgba(56,189,248,0.3);" title="已上传正面静态形象照，暂未升级为视频数字人">🖼️ 静态形象照</span>
                    <span style="font-size: 10px; color: var(--text-muted);">(可升级为视频数字人)</span>
                </div>
            `;
            actionButtonsHtml = `
                <button class="btn btn-sm" onclick="editAnchor('${a.id}')" title="编辑主播基本档案">编辑</button>
                <button class="btn btn-sm" style="background: rgba(16, 185, 129, 0.12); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.35); padding: 2px 8px;" onclick="quickStartAvatarFor('${a.id}', '${escapeHtml(a.name)}')" title="前往工场为该主播上传出镜视频并制作高保真数字人模型">🎬 制作数字人</button>
                <button class="btn btn-sm btn-danger" onclick="deleteAnchor('${a.id}', '${escapeHtml(a.name)}')">删除</button>
            `;
        } else {
            statusBadgeHtml = '<span style="font-size:11px; color:var(--text-muted);">⚪ 待制作数字人</span>';
            actionButtonsHtml = `
                <button class="btn btn-sm" onclick="editAnchor('${a.id}')" title="编辑主播基本档案">编辑</button>
                <button class="btn btn-sm" style="background: rgba(16, 185, 129, 0.12); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.35); padding: 2px 8px;" onclick="quickStartAvatarFor('${a.id}', '${escapeHtml(a.name)}')" title="前往工场为该主播上传出镜视频并制作高保真数字人模型">🎬 制作数字人</button>
                <button class="btn btn-sm btn-danger" onclick="deleteAnchor('${a.id}', '${escapeHtml(a.name)}')">删除</button>
            `;
        }

        const tr = document.createElement("tr");
        tr.innerHTML = `
            <td style="font-weight: 600; white-space: nowrap;">${thumb}${escapeHtml(a.name)}${currentTag}</td>
            <td style="white-space: nowrap;">${typeSelectorHtml}</td>
            <td id="anchor-row-voice-${a.id}" style="white-space: nowrap;">${voiceName}</td>
            <td style="white-space: nowrap;">${statusBadgeHtml}</td>
            <td style="max-width: 120px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtml(a.remark || '')}">${escapeHtml(a.remark || '-')}</td>
            <td style="white-space: nowrap; text-align: right;">
                <div class="table-actions" style="justify-content: flex-end;">
                    ${actionButtonsHtml}
                </div>
            </td>
        `;
        tbody.appendChild(tr);
    });

    // 若有主播处于视频切片制作中，自动设定轻量轮询更新表格
    const hasActiveTask = anchors.some(a => a.avatar_task_status === "processing" || a.avatar_task_status === "pending");
    if (hasActiveTask && !window._anchorStatusPollingTimer) {
        window._anchorStatusPollingTimer = setTimeout(() => {
            window._anchorStatusPollingTimer = null;
            loadAnchors();
        }, 2500);
    }
}

// 聚焦并平滑滚动至添加主播档案表单
function focusCreateAnchorForm() {
    resetAnchorForm();
    const panel = document.getElementById("panel-anchor-form");
    if (panel) {
        panel.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    const nameInput = document.getElementById("anchor-name");
    if (nameInput) {
        setTimeout(() => nameInput.focus(), 250);
    }
}

// 从主播表格快捷联动至数字人资产工场
function quickStartAvatarFor(anchorId, anchorName) {
    const sel = document.getElementById("avatar-task-anchor");
    if (sel) {
        sel.value = anchorId;
    }
    const nameInput = document.getElementById("avatar-task-name");
    if (nameInput) {
        nameInput.value = `${anchorName}·视频数字人`;
    }
    const videoInput = document.getElementById("avatar-task-video");
    if (videoInput) {
        videoInput.scrollIntoView({ behavior: "smooth", block: "center" });
        videoInput.focus();
    }
    showToast(`已选定主播【${anchorName}】，请选择 10~60 秒真人出镜录像并启动切片制作 ✓`, "info");
}

// 独立的音色下拉选项后台异步同步器 (仅用于上方表单的音色下拉选择，绝不阻塞主播表格展示)
async function syncAnchorVoiceOptions() {
    try {
        if (!voiceCache || voiceCache.length === 0) {
            const res = await fetch(`${API_BASE}/voices/list`);
            const json = await res.json();
            if (json.code === 0 && Array.isArray(json.data)) {
                voiceCache = json.data;
                window._voiceProfilesCache = voiceCache;
            }
        }
        // 优先复用全局专业引擎过滤与 optgroup 分组渲染器
        if (typeof populateAnchorVoiceSelect === "function") {
            await populateAnchorVoiceSelect(voiceCache || []);
            return;
        }

        const select = document.getElementById("anchor-voice");
        if (!select) return;
        const currentVal = select.value;
        select.innerHTML = '<option value="">未绑定音色 (开播采用默认发音)</option>';

        (voiceCache || []).forEach(v => {
            const opt = document.createElement("option");
            opt.value = v.id;
            const engineLabel = v.provider_name ? `[${v.provider_name}] ` : "";
            const displayName = typeof formatVoiceDisplayName === "function" ? formatVoiceDisplayName(v) : v.name;
            opt.innerText = `${engineLabel}${displayName}`;
            select.appendChild(opt);
        });

        if (currentVal) select.value = currentVal;
    } catch (e) {
        console.warn("后台加载音色列表异常", e);
    }
}

async function quickChangeAnchorType(anchorId, newType, anchorName) {
    try {
        const fd = new FormData();
        fd.append("id", anchorId);
        fd.append("anchor_type", newType);
        const res = await fetch(`${API_BASE}/anchors/update`, { method: "POST", body: fd });
        const json = await res.json();
        if (json.code === 0) {
            const typeLabels = {
                ecommerce: "🛍️ 带货主播（促单逼单）",
                entertainment: "🎭 娱乐主播（逗梗陪伴）",
                expert: "⚖️ 专业专家（咨询法理前置免责）",
                chat: "☕ 闲聊扯淡（唠嗑搭子）"
            };
            const typeLabel = typeLabels[newType] || newType;
            showToast(`已将主播【${anchorName}】类型调整为：${typeLabel} ✓`, "success");
            // 同步更新本地缓存
            const item = anchorCache.find(x => x.id === anchorId);
            if (item) item.anchor_type = newType;
            // 若上方正在编辑该主播，同步更新表单中的选择框
            const editId = document.getElementById("anchor-edit-id").value;
            if (editId === anchorId) {
                const formTypeEl = document.getElementById("anchor-type");
                if (formTypeEl) formTypeEl.value = newType;
            }
            loadAnchors();
        } else {
            alert("修改主播类型失败: " + (json.detail || json.message));
            loadAnchors();
        }
    } catch (e) {
        alert("网络或接口异常: " + e);
        loadAnchors();
    }
}

async function setLiveAnchor(anchorId, name) {
    try {
        const res = await fetch(`${API_BASE}/anchors/${anchorId}/set-live`, { method: "POST" });
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

    const anchorTypeEl = document.getElementById("anchor-type");
    const anchorType = anchorTypeEl ? anchorTypeEl.value : "ecommerce";

    const fd = new FormData();
    if (editId) fd.append("id", editId);
    fd.append("name", name);
    fd.append("anchor_type", anchorType);
    fd.append("voice_id", document.getElementById("anchor-voice").value || "");
    fd.append("remark", document.getElementById("anchor-remark").value.trim() || "");

    // 正面静态形象照上传 (选填图片)
    const portraitInput = document.getElementById("anchor-photo-portrait");
    if (portraitInput && portraitInput.files && portraitInput.files.length) {
        fd.append("portrait", portraitInput.files[0]);
    }

    try {
        const url = editId ? `${API_BASE}/anchors/update` : `${API_BASE}/anchors/create`;
        const res = await fetch(url, { method: "POST", body: fd });
        const json = await res.json();
        if (json.code === 0) {
            showToast(editId ? `主播【${name}】资料已更新 ✓` : `主播【${name}】基础档案已创建 ✓`, "success");
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

    const titleEl = document.getElementById("anchor-form-card-title");
    if (titleEl) titleEl.innerText = "编辑主播基础档案";
    const submitBtnText = document.getElementById("anchor-submit-btn-text");
    if (submitBtnText) submitBtnText.innerText = "更新主播资料";

    // 回显主播类型
    const anchorTypeEl = document.getElementById("anchor-type");
    if (anchorTypeEl) {
        anchorTypeEl.value = a.anchor_type || "ecommerce";
    }

    // 选中绑定的音色
    const voiceSelect = document.getElementById("anchor-voice");
    if (voiceSelect) {
        if (a.voice_id) {
            let opt = voiceSelect.querySelector(`option[value="${a.voice_id}"]`);
            if (!opt) {
                const vMatch = (voiceCache || []).find(v => v.id === a.voice_id);
                const optName = a.voice_name || (vMatch ? `${vMatch.name} (${vMatch.provider_name || '其他引擎'})` : a.voice_id);
                opt = document.createElement("option");
                opt.value = a.voice_id;
                opt.innerText = `${optName} [当前绑定/其他引擎]`;
                voiceSelect.appendChild(opt);
            }
            voiceSelect.value = a.voice_id;
        } else {
            voiceSelect.value = "";
        }
    }

    document.getElementById("anchor-remark").value = a.remark || "";
    const modelStatus = a.avatar_asset_dir ? " · 🎬 已关联视频数字人" : (a.photos && a.photos.portrait ? " · 🖼️ 已关联形象照" : "");
    document.getElementById("anchor-edit-hint").innerText = `(正在编辑: ${a.name}${modelStatus})`;
    const resetBtn = document.getElementById("anchor-reset-btn");
    if (resetBtn) resetBtn.style.display = "inline-block";
    // 平滑滚动并高亮提示
    const formPanel = document.getElementById("panel-anchor-form");
    if (formPanel) {
        formPanel.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    const nameInput = document.getElementById("anchor-name");
    if (nameInput) {
        setTimeout(() => nameInput.focus(), 200);
    }
}

function resetAnchorForm() {
    document.getElementById("anchor-edit-id").value = "";
    document.getElementById("anchor-name").value = "";
    const titleEl = document.getElementById("anchor-form-card-title");
    if (titleEl) titleEl.innerText = "添加新主播基础档案";
    const submitBtnText = document.getElementById("anchor-submit-btn-text");
    if (submitBtnText) submitBtnText.innerText = "保存主播档案";

    const anchorTypeEl = document.getElementById("anchor-type");
    if (anchorTypeEl) {
        anchorTypeEl.value = "ecommerce";
    }
    document.getElementById("anchor-remark").value = "";
    const portraitInput = document.getElementById("anchor-photo-portrait");
    if (portraitInput) portraitInput.value = "";
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
// 数字人视频切片与训练工作台 (Phase 2 Avatar Task Management)
// ============================================================================
let _activeAvatarTaskId = null;
let _avatarTaskPollingTimer = null;
let _avatarTasksCache = [];

function syncAvatarTaskAnchorSelect(anchors) {
    const sel = document.getElementById("avatar-task-anchor");
    if (!sel) return;
    const currentVal = sel.value;
    sel.innerHTML = '<option value="">-- 请选择要绑定的主播 (必选) --</option>';
    if (Array.isArray(anchors)) {
        anchors.forEach(a => {
            const opt = document.createElement("option");
            opt.value = a.id;
            opt.innerText = `${a.name} (${a.anchor_type || '带货主播'})`;
            sel.appendChild(opt);
        });
    }
    if (currentVal) sel.value = currentVal;
}

async function loadAvatarTasks() {
    const tbody = document.getElementById("avatar-tasks-tbody");
    if (!tbody) return;
    try {
        const res = await fetch(`${API_BASE}/anchors/avatar/tasks`);
        const json = await res.json();
        if (json.code === 0 && Array.isArray(json.data)) {
            _avatarTasksCache = json.data;
            renderAvatarTasksTable(_avatarTasksCache);

            // 如果有正在运行的任务且未在轮询，自动拉起轮询
            const runningTask = _avatarTasksCache.find(t => t.status === "processing" || t.status === "pending");
            if (runningTask && !_avatarTaskPollingTimer) {
                _activeAvatarTaskId = runningTask.id;
                startAvatarTaskPolling(runningTask.id);
            }
        }
    } catch (e) {
        console.warn("加载数字人切片任务失败", e);
    }
}

function renderAvatarTasksTable(tasks) {
    const tbody = document.getElementById("avatar-tasks-tbody");
    if (!tbody) return;
    tbody.innerHTML = "";
    if (!tasks || tasks.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" style="text-align:center; color:var(--text-muted); padding: 16px;">暂无数字人训练任务，可在上方上传真人视频开始制作</td></tr>';
        return;
    }

    const STATUS_MAP = {
        completed: { text: "已就绪 (100%)", badge: "background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3);" },
        processing: { text: "训练切片中", badge: "background: rgba(56, 189, 248, 0.15); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.3);" },
        pending: { text: "排队等待中", badge: "background: rgba(251, 146, 60, 0.15); color: #fb923c; border: 1px solid rgba(251, 146, 60, 0.3);" },
        failed: { text: "训练失败", badge: "background: rgba(239, 68, 68, 0.15); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3);" },
        cancelled: { text: "已取消", badge: "background: rgba(156, 163, 175, 0.15); color: #9ca3af; border: 1px solid rgba(156, 163, 175, 0.3);" },
    };

    tasks.forEach(t => {
        const st = STATUS_MAP[t.status] || STATUS_MAP.pending;
        const thumbHtml = t.preview_path
            ? `<img src="/static-file?path=${encodeURIComponent(t.preview_path)}" style="width: 38px; height: 38px; object-fit: cover; border-radius: 4px; border: 1px solid var(--border-subtle);" onerror="this.src='/static/svg/default_avatar.svg'">`
            : `<div style="width:38px; height:38px; border-radius:4px; background:rgba(255,255,255,0.05); display:flex; align-items:center; justify-content:center; font-size:11px; color:var(--text-muted);">无图</div>`;

        // 查找关联主播名称
        let anchorName = "通用资产 (未绑定)";
        if (t.anchor_id && Array.isArray(anchorCache)) {
            const m = anchorCache.find(a => a.id === t.anchor_id);
            if (m) anchorName = `${m.name}`;
            else anchorName = t.anchor_id;
        }

        const framesText = (t.meta && t.meta.frame_count) ? `${t.meta.frame_count} 帧` : "-";
        const timeText = t.created_at ? t.created_at.substring(0, 19).replace("T", " ") : "-";

        let actionHtml = "";
        if (t.status === "completed") {
            actionHtml = `
                <div class="table-actions">
                    <button class="btn btn-sm btn-primary" onclick="applyAvatarTaskToAnchorPrompt('${t.id}', '${escapeHtml(t.name)}')">应用至主播</button>
                    <button class="btn btn-sm btn-danger" onclick="deleteAvatarTask('${t.id}', '${escapeHtml(t.name)}')">删除</button>
                </div>
            `;
        } else if (t.status === "processing" || t.status === "pending") {
            actionHtml = `
                <div class="table-actions">
                    <button class="btn btn-sm btn-danger" onclick="cancelAvatarTaskById('${t.id}')">取消</button>
                </div>
            `;
        } else {
            actionHtml = `
                <div class="table-actions">
                    <button class="btn btn-sm btn-danger" onclick="deleteAvatarTask('${t.id}', '${escapeHtml(t.name)}')">删除</button>
                </div>
            `;
        }

        const tr = document.createElement("tr");
        tr.innerHTML = `
            <td>${thumbHtml}</td>
            <td style="font-weight: 600;">${escapeHtml(t.name)}</td>
            <td>${escapeHtml(anchorName)}</td>
            <td>${framesText}</td>
            <td><span class="badge-recommend" style="font-size:11px; padding:2px 8px; ${st.badge}">${st.text}${t.status === 'processing' ? ` (${t.progress}%)` : ''}</span></td>
            <td style="font-size: 12px; color: var(--text-muted);">${timeText}</td>
            <td style="white-space: nowrap;">${actionHtml}</td>
        `;
        tbody.appendChild(tr);
    });
}

async function submitAvatarTask() {
    const nameInput = document.getElementById("avatar-task-name");
    const anchorSelect = document.getElementById("avatar-task-anchor");
    const videoInput = document.getElementById("avatar-task-video");
    const hint = document.getElementById("avatar-task-hint");
    const submitBtn = document.getElementById("btn-submit-avatar-task");
    const anchorId = anchorSelect ? anchorSelect.value.trim() : "";
    if (!anchorId) {
        alert("请选择该数字人模型要关联绑定的目标主播！");
        if (anchorSelect) anchorSelect.focus();
        return;
    }

    const name = nameInput ? nameInput.value.trim() : "";
    if (!name) {
        alert("请输入数字人资产名称");
        if (nameInput) nameInput.focus();
        return;
    }

    if (!videoInput || !videoInput.files || videoInput.files.length === 0) {
        alert("请选择要上传制作切片的真人说话视频文件 (MP4/MOV)");
        return;
    }

    const file = videoInput.files[0];

    const fd = new FormData();
    fd.append("name", name);
    fd.append("file", file);
    if (anchorId) fd.append("anchor_id", anchorId);

    try {
        if (submitBtn) submitBtn.disabled = true;
        if (hint) hint.innerText = "正在上传视频并初始化后台切片流水线...";

        const res = await fetch(`${API_BASE}/anchors/avatar/task`, {
            method: "POST",
            body: fd,
        });
        const json = await res.json();
        if (json.code === 0 && json.data && json.data.task_id) {
            showToast(`数字人切片任务【${name}】已提交，后台开始流水线制作 ✓`, "success");
            if (videoInput) videoInput.value = "";
            if (nameInput) nameInput.value = "";
            if (hint) hint.innerText = "";

            _activeAvatarTaskId = json.data.task_id;
            startAvatarTaskPolling(json.data.task_id);
            loadAnchors();
            loadAvatarTasks();
        } else {
            alert("提交任务失败: " + (json.detail || json.message || "未知错误"));
            if (hint) hint.innerText = "";
        }
    } catch (e) {
        alert("请求异常: " + e);
        if (hint) hint.innerText = "";
    } finally {
        if (submitBtn) submitBtn.disabled = false;
    }
}

let _lastPolledProgress = -1;

function startAvatarTaskPolling(taskId) {
    if (_avatarTaskPollingTimer) {
        clearInterval(_avatarTaskPollingTimer);
        _avatarTaskPollingTimer = null;
    }
    _lastPolledProgress = -1;

    const box = document.getElementById("avatar-task-active-box");
    if (box) box.style.display = "block";

    const poll = async () => {
        try {
            const res = await fetch(`${API_BASE}/anchors/avatar/tasks/${taskId}`);
            if (!res.ok) return;
            const json = await res.json();
            if (json.code !== 0 || !json.data) return;

            const t = json.data;
            const percentEl = document.getElementById("active-task-percent");
            const barEl = document.getElementById("active-task-bar");
            const titleEl = document.getElementById("active-task-title");
            const stageEl = document.getElementById("active-task-stage");

            if (titleEl) titleEl.innerText = `正在处理: ${t.name}`;
            if (percentEl) percentEl.innerText = `${t.progress}%`;
            if (barEl) barEl.style.width = `${t.progress}%`;
            if (stageEl) stageEl.innerText = t.stage_message || "切片提取中...";

            // 当进度发生跳变时，同步更新上方置顶主播表格
            if (t.progress !== _lastPolledProgress && (t.progress % 10 === 0 || t.progress >= 90)) {
                _lastPolledProgress = t.progress;
                loadAnchors();
            }

            if (t.status === "completed") {
                clearInterval(_avatarTaskPollingTimer);
                _avatarTaskPollingTimer = null;
                showToast(`🎬 数字人资产【${t.name}】切片与特征提取完成！已就绪开播 ✓`, "success");
                loadAvatarTasks();
                loadAnchors();
                setTimeout(() => {
                    if (box) box.style.display = "none";
                }, 3000);
            } else if (t.status === "failed" || t.status === "cancelled") {
                clearInterval(_avatarTaskPollingTimer);
                _avatarTaskPollingTimer = null;
                if (t.status === "failed") {
                    showToast(`数字人制作失败: ${t.error_message || '未知异常'}`, "danger");
                }
                loadAvatarTasks();
                loadAnchors();
                setTimeout(() => {
                    if (box) box.style.display = "none";
                }, 3000);
            }
        } catch (e) {
            console.warn("轮询切片任务进度异常", e);
        }
    };

    poll();
    _avatarTaskPollingTimer = setInterval(poll, 1200);
}

// ============================================================================
// 数字人资产沉浸式预览模态框交互逻辑 (对标 LiveTalking 工业级切片剖析与试听驱动)
// ============================================================================
let _currentPreviewAnchorId = null;
let _currentPreviewDetail = null;
let _currentSampleFrames = [];
let _sliceStreamFrames = [];
let _sliceAnimationTimer = null;
let _sliceAnimationIdx = 0;
let _currentSampleIndex = 0;
let _demoAudioPlayer = null;

async function openAvatarPreviewModal(anchorId) {
    const modal = document.getElementById("modal-avatar-preview");
    if (!modal) return;

    _currentPreviewAnchorId = anchorId;
    _currentSampleFrames = [];
    _sliceStreamFrames = [];
    _currentSampleIndex = 0;
    _sliceAnimationIdx = 0;
    stopSliceAnimationPlay();

    // 默认优先展示：神经切片与人脸动态追踪透视 (LiveTalking 工业标准架构，绝不默认放原片)
    switchPreviewSubTab("slices");

    const a = (anchorCache || []).find(x => x.id === anchorId);
    let d = null;

    try {
        const res = await fetch(`${API_BASE}/anchors/${anchorId}/avatar-detail`);
        if (res.ok) {
            const json = await res.json();
            if (json.code === 0 && json.data) {
                d = json.data;
            }
        }
    } catch (e) {
        console.warn("请求 avatar-detail 接口异常，自动使用主播本地档案兜底展示:", e);
    }

    if (!d && a) {
        const meta = a.avatar_meta || {};
        const hasAsset = Boolean(a.avatar_asset_dir);
        d = {
            anchor_id: a.id,
            anchor_name: a.name,
            anchor_type: a.anchor_type,
            voice_id: a.voice_id || "",
            has_trained_avatar: hasAsset,
            asset_dir: a.avatar_asset_dir || "",
            source_video_url: a.source_video ? `${API_BASE}/anchors/${a.id}/source-video` : "",
            photo_portrait: (a.photos && a.photos.portrait) ? a.photos.portrait : "",
            frame_count: meta.frame_count || 0,
            fps: meta.fps || 25.0,
            resolution: meta.width ? `${meta.width}x${meta.height}` : "1280x720",
            coords_count: meta.coords_count || 0,
            face_imgs_count: meta.face_imgs_count || 0,
            has_audio: meta.has_audio !== undefined ? meta.has_audio : true,
            compute_branch: meta.compute_branch || "local_hardware",
            cloud_status: { configured: false, is_reachable: false, error: "离线" },
            local_gpu_name: "本地硬件",
            task_status: a.avatar_task_status || (hasAsset ? "completed" : "pending"),
            task_progress: a.avatar_task_progress || (hasAsset ? 100 : 0),
            task_stage: a.avatar_task_stage || (hasAsset ? "切片制作完成" : "准备中"),
        };
    }

    if (!d) {
        alert("未找到该主播的数字人资产档案，请先在工场上传视频制作。");
        return;
    }

    _currentPreviewDetail = d;

    try {
        // 1. 模态框头部
        const titleEl = document.getElementById("modal-avatar-title");
        if (titleEl) titleEl.innerText = `【${d.anchor_name}】数字人模型与切片资产`;

        const badgeEl = document.getElementById("modal-avatar-badge");
        if (badgeEl) {
            if (d.has_trained_avatar) {
                badgeEl.style.background = "rgba(16,185,129,0.15)";
                badgeEl.style.color = "#34d399";
                badgeEl.style.borderColor = "rgba(16,185,129,0.3)";
                badgeEl.innerText = "● 25 FPS 唇形驱动模型就绪";
            } else {
                badgeEl.style.background = "rgba(245,158,11,0.15)";
                badgeEl.style.color = "#fbbf24";
                badgeEl.style.borderColor = "rgba(245,158,11,0.3)";
                badgeEl.innerText = "⏳ 正在切片制作中";
            }
        }

        // 2. 技术指标
        const framesEl = document.getElementById("modal-spec-frames");
        if (framesEl) {
            const sec = d.frame_count ? (d.frame_count / (d.fps || 25.0)).toFixed(1) : 0;
            framesEl.innerText = d.frame_count ? `${d.frame_count} 帧 (${sec}s @ ${d.fps || 25} FPS)` : "制作中";
        }
        const resEl = document.getElementById("modal-spec-resolution");
        if (resEl) resEl.innerText = `${d.resolution} 原生母轨`;

        const coordsEl = document.getElementById("modal-spec-coords");
        if (coordsEl) coordsEl.innerText = d.coords_count ? `${d.coords_count} 组动态平滑包围盒` : (d.has_trained_avatar ? "已校准完成" : "未生成");

        const faceEl = document.getElementById("modal-spec-faceimgs");
        if (faceEl) faceEl.innerText = d.face_imgs_count ? `${d.face_imgs_count} 帧标准 256x256 对齐切片` : (d.has_trained_avatar ? "已对齐就绪" : "未生成");

        const pathEl = document.getElementById("modal-spec-path");
        if (pathEl) pathEl.innerText = d.asset_dir || "暂未关联切片目录";

        // 4. 音色提示
        const voiceNameEl = document.getElementById("preview-voice-name");
        if (voiceNameEl) {
            const vLabel = d.voice_name ? `${d.voice_name}${d.voice_provider ? ` · [${d.voice_provider}]` : ''}` : (d.voice_id ? d.voice_id : "未绑定专属音色 (将使用默认音色)");
            voiceNameEl.innerText = `音色: ${vLabel}`;
        }
        const speechStatusEl = document.getElementById("preview-speech-status");
        if (speechStatusEl) {
            speechStatusEl.style.color = "var(--text-muted)";
            speechStatusEl.innerText = "点击右侧测试主播发音";
        }

        // 5. 备选的原片播放器初始化
        const videoEl = document.getElementById("modal-avatar-video");
        const videoSrc = document.getElementById("modal-avatar-video-src");
        const thumbBox = document.getElementById("modal-avatar-thumb-box");
        const thumbImg = document.getElementById("modal-avatar-thumb-img");

        if (d.source_video_url) {
            videoEl.style.display = "block";
            thumbBox.style.display = "none";
            videoSrc.src = `${API_BASE}/anchors/${anchorId}/source-video`;
            videoEl.load();
        } else if (d.photo_portrait) {
            videoEl.style.display = "none";
            thumbBox.style.display = "block";
            const portUrl = d.photo_portrait.startsWith("http") ? d.photo_portrait : (d.photo_portrait.startsWith("/") ? d.photo_portrait : `/${d.photo_portrait}`);
            thumbImg.src = portUrl;
        } else {
            videoEl.style.display = "none";
            thumbBox.style.display = "block";
            thumbImg.src = "/static/svg/anchor_ecommerce.svg";
        }

        modal.style.display = "flex";

        // 默认强制激活：【神经切片与人脸追踪透视】子标签，暂停原片播放，绝不单纯播放原视频
        switchPreviewSubTab("slices");

        // 预加载切片帧样本并启动 25 FPS 连续动态画卷播放
        loadAvatarSampleFrames(anchorId);
    } catch (e) {
        alert("展示数字人资产异常: " + e);
    }
}

function switchPreviewSubTab(tab) {
    const boxVideo = document.getElementById("preview-box-video");
    const boxSlices = document.getElementById("preview-box-slices");
    const btnVideo = document.getElementById("tab-btn-preview-video");
    const btnSlices = document.getElementById("tab-btn-preview-slices");
    const descEl = document.getElementById("preview-tab-desc");
    const videoEl = document.getElementById("modal-avatar-video");

    if (tab === "slices") {
        if (boxVideo) boxVideo.style.display = "none";
        if (boxSlices) boxSlices.style.display = "flex";
        if (btnSlices) {
            btnSlices.classList.add("btn-primary");
            btnSlices.style.background = "";
        }
        if (btnVideo) {
            btnVideo.classList.remove("btn-primary");
            btnVideo.style.background = "rgba(255,255,255,0.05)";
        }
        if (descEl) descEl.innerText = "● 已默认呈现 25 FPS 连续切片与 coords.pkl 动态人脸定位跟踪";
        if (videoEl) {
            videoEl.pause();
        }
        startSliceAnimationPlay();
    } else {
        if (boxVideo) boxVideo.style.display = "flex";
        if (boxSlices) boxSlices.style.display = "none";
        if (btnVideo) {
            btnVideo.classList.add("btn-primary");
            btnVideo.style.background = "";
        }
        if (btnSlices) {
            btnSlices.classList.remove("btn-primary");
            btnSlices.style.background = "rgba(255,255,255,0.05)";
        }
        if (descEl) descEl.innerText = "出镜录像原切片母轨 · 开播时作为 25 FPS 连续动作背景";
        stopSliceAnimationPlay();
        if (videoEl) {
            videoEl.play().catch(() => {});
        }
    }
}

async function loadAvatarSampleFrames(anchorId) {
    const selectorEl = document.getElementById("slice-frames-selector");
    if (selectorEl) selectorEl.innerHTML = '<span style="font-size:11px;color:var(--text-muted);">正在加载切片样本数据...</span>';

    try {
        const res = await fetch(`${API_BASE}/anchors/${anchorId}/avatar-sample-frames`);
        if (!res.ok) throw new Error("获取切片样本失败");
        const json = await res.json();
        const samples = (json.data && json.data.samples) || [];
        const streamFrames = (json.data && json.data.stream_frames) || [];
        _currentSampleFrames = samples;
        _sliceStreamFrames = streamFrames.length > 0 ? streamFrames : samples;

        if (samples.length === 0 && _sliceStreamFrames.length === 0) {
            if (selectorEl) selectorEl.innerHTML = '<span style="font-size:11px;color:var(--text-muted);">暂无切片样本，请先在下方工场制作数字人。</span>';
            return;
        }

        renderSliceSelector(samples);
        renderSliceFrame(0);
        // 自动启动 25 FPS 连续动态画卷播放
        startSliceAnimationPlay();
    } catch (e) {
        if (selectorEl) selectorEl.innerHTML = `<span style="font-size:11px;color:var(--red);">加载切片失败: ${e.message}</span>`;
    }
}

function renderSliceSelector(samples) {
    const selectorEl = document.getElementById("slice-frames-selector");
    if (!selectorEl) return;
    selectorEl.innerHTML = "";

    samples.forEach((s, i) => {
        const btn = document.createElement("button");
        btn.className = `btn btn-sm ${i === _currentSampleIndex ? "btn-primary" : ""}`;
        btn.style.fontSize = "10px";
        btn.style.padding = "2px 8px";
        btn.innerText = `第 ${s.frame_no} 帧 (${s.time_sec}s)`;
        btn.onclick = () => {
            stopSliceAnimationPlay();
            renderSliceFrame(i);
        };
        selectorEl.appendChild(btn);
    });
}

function renderSliceFrame(index) {
    if (!_currentSampleFrames || !_currentSampleFrames[index]) return;
    _currentSampleIndex = index;
    const s = _currentSampleFrames[index];

    const fullImg = document.getElementById("slice-full-img");
    const faceImg = document.getElementById("slice-face-img");
    const bboxBox = document.getElementById("slice-bbox-box");

    if (fullImg) fullImg.src = s.full_url;
    if (faceImg) faceImg.src = s.face_url || s.full_url;

    if (bboxBox && s.bbox_percent) {
        bboxBox.style.left = `${s.bbox_percent.left}%`;
        bboxBox.style.top = `${s.bbox_percent.top}%`;
        bboxBox.style.width = `${s.bbox_percent.width}%`;
        bboxBox.style.height = `${s.bbox_percent.height}%`;
    }

    const selectorEl = document.getElementById("slice-frames-selector");
    if (selectorEl) {
        Array.from(selectorEl.children).forEach((child, i) => {
            if (i === index) {
                child.classList.add("btn-primary");
            } else {
                child.classList.remove("btn-primary");
            }
        });
    }
}

// -----------------------------------------------------------------------------
// 25 FPS 连续切片动态播放器与平滑追踪动效
// -----------------------------------------------------------------------------
function startSliceAnimationPlay() {
    if (_sliceAnimationTimer) clearInterval(_sliceAnimationTimer);
    if (!_sliceStreamFrames || _sliceStreamFrames.length === 0) return;

    const btnText = document.getElementById("slice-play-text");
    const btnIcon = document.getElementById("slice-play-icon");
    if (btnText) btnText.innerText = "暂停动效播放";
    if (btnIcon) btnIcon.innerText = "⏸️";

    _sliceAnimationTimer = setInterval(() => {
        _sliceAnimationIdx = (_sliceAnimationIdx + 1) % _sliceStreamFrames.length;
        renderStreamFrameAt(_sliceAnimationIdx);
    }, 40); // 40ms 对应 25 FPS
}

function stopSliceAnimationPlay() {
    if (_sliceAnimationTimer) {
        clearInterval(_sliceAnimationTimer);
        _sliceAnimationTimer = null;
    }
    const btnText = document.getElementById("slice-play-text");
    const btnIcon = document.getElementById("slice-play-icon");
    if (btnText) btnText.innerText = "连续动效播放";
    if (btnIcon) btnIcon.innerText = "▶️";
}

function toggleSliceAnimationPlay() {
    if (_sliceAnimationTimer) {
        stopSliceAnimationPlay();
    } else {
        startSliceAnimationPlay();
    }
}

function renderStreamFrameAt(idx) {
    if (!_sliceStreamFrames || !_sliceStreamFrames[idx]) return;
    const s = _sliceStreamFrames[idx];

    const fullImg = document.getElementById("slice-full-img");
    const faceImg = document.getElementById("slice-face-img");
    const bboxBox = document.getElementById("slice-bbox-box");

    if (fullImg) fullImg.src = s.full_url;
    if (faceImg && s.face_url) faceImg.src = s.face_url;

    if (bboxBox && s.bbox_percent) {
        bboxBox.style.left = `${s.bbox_percent.left}%`;
        bboxBox.style.top = `${s.bbox_percent.top}%`;
        bboxBox.style.width = `${s.bbox_percent.width}%`;
        bboxBox.style.height = `${s.bbox_percent.height}%`;
    }
}

// -----------------------------------------------------------------------------
// 试听主播台词驱动演示
// -----------------------------------------------------------------------------
async function testAnchorSpeechDemo() {
    const textEl = document.getElementById("preview-speech-text");
    const statusEl = document.getElementById("preview-speech-status");
    const btnEl = document.getElementById("btn-preview-speech");
    const bboxBox = document.getElementById("slice-bbox-box");

    const text = (textEl ? textEl.value : "").trim();
    if (!text) {
        alert("请输入测试台词");
        return;
    }

    const d = _currentPreviewDetail || {};
    let voiceId = d.voice_id || "";
    let providerName = d.voice_provider || "";

    // 智能匹配引擎：若主播未带 provider_name，在全局音色缓存中按 voice_id 精准查找
    if (!providerName && window._voiceProfilesCache && Array.isArray(window._voiceProfilesCache)) {
        const matchedVoice = window._voiceProfilesCache.find(v => v.id === voiceId || v.name === voiceId);
        if (matchedVoice && matchedVoice.provider_name) {
            providerName = matchedVoice.provider_name;
        }
    }
    if (!providerName) {
        if (voiceId.startsWith("voice_moss_") || voiceId.startsWith("moss_")) {
            providerName = "moss_tts_nano";
        } else if (voiceId.includes("bailian") || voiceId.includes("cosyvoice") || voiceId.startsWith("long")) {
            providerName = "cosyvoice";
        } else if (voiceId.includes("eleven")) {
            providerName = "elevenlabs";
        } else {
            providerName = "moss_tts_nano"; // 系统自带 MOSS-TTS-Nano 极速保底，优先调用 GPU
        }
    }

    if (statusEl) {
        statusEl.style.color = "#38bdf8";
        statusEl.innerText = "⏳ 正在合成语音并驱动切片口型...";
    }
    if (btnEl) btnEl.disabled = true;

    try {
        const res = await fetch(`${API_BASE}/settings/tts/preview`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                provider_name: providerName,
                voice_name: voiceId || null,
                anchor_id: d.id || null,
                text: text
            })
        });

        if (!res.ok) {
            let errorDetail = "";
            try {
                const errJson = await res.json();
                errorDetail = errJson.detail || errJson.message || "";
            } catch (_) {
                errorDetail = await res.text().catch(() => "");
            }

            // 智能提炼为简短易懂的业务提示（如：主播绑定的音色有误，请检查）
            let conciseMsg = "主播绑定的音色有误，请检查";
            const detailLower = (errorDetail || "").toLowerCase();

            if (res.status === 404 || detailLower.includes("resourcenotexist") || detailLower.includes("不存在") || detailLower.includes("未找到") || detailLower.includes("voice-id")) {
                conciseMsg = "主播绑定的音色有误，请检查";
            } else if (res.status === 401 || detailLower.includes("api key") || detailLower.includes("未配置") || detailLower.includes("凭证")) {
                conciseMsg = "语音引擎 API Key 未配置或失效，请检查设置";
            } else if (detailLower.includes("网络") || detailLower.includes("timeout") || detailLower.includes("failed to fetch")) {
                conciseMsg = "网络连接超时，无法连接语音服务";
            } else if (errorDetail && errorDetail.length <= 25) {
                conciseMsg = errorDetail;
            }

            const err = new Error(conciseMsg);
            err.rawDetail = errorDetail;
            throw err;
        }

        const blob = await res.blob();
        if (!blob || blob.size === 0) {
            throw new Error("语音引擎返回音频为空，请检查音色配置");
        }
        const audioUrl = URL.createObjectURL(blob);

        if (_demoAudioPlayer) {
            _demoAudioPlayer.pause();
            _demoAudioPlayer = null;
        }

        _demoAudioPlayer = new Audio(audioUrl);
        _demoAudioPlayer.onended = () => {
            if (statusEl) {
                statusEl.style.color = "#10b981";
                statusEl.innerHTML = "✓ 试听驱动演示完毕";
            }
            if (bboxBox) bboxBox.style.boxShadow = "none";
            if (btnEl) btnEl.disabled = false;
        };

        // 伴随声音开始播放，启动 25 FPS 连续切片动态画卷播放
        startSliceAnimationPlay();
        if (bboxBox) bboxBox.style.boxShadow = "0 0 18px rgba(16, 185, 129, 0.95)";

        await _demoAudioPlayer.play();
        if (statusEl) {
            statusEl.style.color = "#34d399";
            statusEl.innerHTML = `🔊 正在播放试听发音 (声线: <strong>${escapeHtml(d.voice_name || voiceId || '默认')}</strong>，切片联动中)...`;
        }
    } catch (e) {
        let msg = e.message || "主播绑定的音色有误，请检查";
        if (msg === "Failed to fetch") {
            msg = "无法连接至后端服务，请确认服务已启动";
        }
        if (statusEl) {
            statusEl.style.color = "#fbbf24";
            const detailTip = e.rawDetail ? ` title="${escapeHtml(e.rawDetail)}"` : "";
            statusEl.innerHTML = `<span${detailTip}>⚠️ <strong>试听驱动失败：</strong>${escapeHtml(msg)}</span>`;
        }
        if (bboxBox) bboxBox.style.boxShadow = "none";
    } finally {
        if (btnEl) btnEl.disabled = false;
    }
}

function closeAvatarPreviewModal() {
    stopSliceAnimationPlay();
    const modal = document.getElementById("modal-avatar-preview");
    if (modal) modal.style.display = "none";
    const videoEl = document.getElementById("modal-avatar-video");
    if (videoEl) {
        videoEl.pause();
        videoEl.currentTime = 0;
    }
    if (_demoAudioPlayer) {
        _demoAudioPlayer.pause();
        _demoAudioPlayer = null;
    }
}

// -----------------------------------------------------------------------------
// 加载软硬件达标体检看板数据
// -----------------------------------------------------------------------------
async function loadDigitalHumanHardwareRequirements(forceRefresh = false) {
    const cloudBadge = document.getElementById("hw-cloud-status-badge");
    const cloudDot = document.getElementById("hw-cloud-status-dot");
    const cloudText = document.getElementById("hw-cloud-status-text");

    if (forceRefresh && cloudText) {
        cloudText.innerText = "云端 GPU 算力：正在握手探活...";
        if (cloudDot) cloudDot.style.background = "#38bdf8";
        if (cloudBadge) {
            cloudBadge.style.background = "rgba(56, 189, 248, 0.15)";
            cloudBadge.style.borderColor = "rgba(56, 189, 248, 0.35)";
            cloudBadge.style.color = "#38bdf8";
        }
    }

    try {
        const url = `${API_BASE}/anchors/hardware-requirements?t=${Date.now()}`;
        const res = await fetch(url);
        if (!res.ok) return;
        const json = await res.json();
        if (json.code !== 0 || !json.data) return;

        const data = json.data;
        const stages = data.stages || [];
        const s1 = stages.find(s => s.stage_id === "slicing");
        const s2 = stages.find(s => s.stage_id === "streaming");

        if (s1) {
            const badge1 = document.getElementById("hw-badge-slicing");
            const cur1 = document.getElementById("hw-slicing-current");
            if (badge1) badge1.innerText = s1.status_badge;
            if (cur1) cur1.innerText = `${s1.current_hardware} (${s1.is_qualified ? "性能强劲，大幅达标" : "不足"})`;
        }

        if (s2) {
            const badge2 = document.getElementById("hw-badge-streaming");
            const cur2 = document.getElementById("hw-streaming-current");
            if (badge2) {
                badge2.innerText = s2.status_badge;
                if (!s2.is_qualified) {
                    badge2.style.background = "rgba(245, 158, 11, 0.18)";
                    badge2.style.color = "#fbbf24";
                    badge2.style.border = "1px solid rgba(245, 158, 11, 0.4)";
                } else {
                    badge2.style.background = "rgba(16,185,129,0.2)";
                    badge2.style.color = "#34d399";
                    badge2.style.border = "1px solid rgba(16,185,129,0.4)";
                }
            }
            if (cur2) cur2.innerText = s2.current_hardware;
        }

        // 3. 动态更新云端 GPU 真实算力状态
        const cloud = data.cloud_hardware || {};

        if (cloudBadge && cloudText) {
            if (!cloud.configured) {
                cloudBadge.style.background = "rgba(255, 255, 255, 0.05)";
                cloudBadge.style.borderColor = "rgba(255, 255, 255, 0.15)";
                cloudBadge.style.color = "var(--text-muted)";
                if (cloudDot) cloudDot.style.background = "#94a3b8";
                cloudText.innerText = "云端 GPU 算力：未配置远端节点";
            } else if (cloud.is_reachable) {
                cloudBadge.style.background = "rgba(16, 185, 129, 0.15)";
                cloudBadge.style.borderColor = "rgba(16, 185, 129, 0.35)";
                cloudBadge.style.color = "#34d399";
                if (cloudDot) cloudDot.style.background = "#10b981";
                const hwLabel = (cloud.gpu_name && cloud.vram_gb) ? `${cloud.gpu_name} · ${cloud.vram_gb}GB 显存` : (cloud.display_label || cloud.gpu_name || cloud.device_info || cloud.provider_name || '已连通');
                cloudText.innerText = `云端 GPU 算力：在线就绪 (${hwLabel})`;
            } else {
                cloudBadge.style.background = "rgba(239, 68, 68, 0.12)";
                cloudBadge.style.borderColor = "rgba(239, 68, 68, 0.3)";
                cloudBadge.style.color = "#f87171";
                if (cloudDot) cloudDot.style.background = "#ef4444";
                const errBrief = cloud.error ? (cloud.error.includes("未开机") ? "未开机" : (cloud.error.includes("超时") ? "连接超时" : "离线未连通")) : "离线未连通";
                cloudText.innerText = `云端 GPU 算力：${errBrief} (开播需先启动)`;
            }
        }
    } catch (e) {
        console.warn("加载数字人软硬件达标看板失败:", e);
    }
}



async function cancelActiveAvatarTask() {
    if (!_activeAvatarTaskId) return;
    if (!confirm("确定取消当前正在执行的切片任务吗？")) return;
    await cancelAvatarTaskById(_activeAvatarTaskId);
}

async function cancelAvatarTaskById(taskId) {
    try {
        const res = await fetch(`${API_BASE}/anchors/avatar/tasks/${taskId}/cancel`, {
            method: "POST"
        });
        const json = await res.json();
        if (json.code === 0) {
            showToast("已成功取消切片训练任务", "info");
            loadAvatarTasks();
            const box = document.getElementById("avatar-task-active-box");
            if (box && _activeAvatarTaskId === taskId) {
                box.style.display = "none";
                if (_avatarTaskPollingTimer) clearInterval(_avatarTaskPollingTimer);
            }
        } else {
            alert("取消失败: " + (json.detail || json.message));
        }
    } catch (e) {
        alert("取消异常: " + e);
    }
}

async function applyAvatarTaskToAnchorPrompt(taskId, taskName) {
    if (!Array.isArray(anchorCache) || anchorCache.length === 0) {
        alert("当前尚无主播档案，请先在上方创建主播后再绑定");
        return;
    }

    let msg = `请选择要将数字人模型【${taskName}】绑定至哪位主播：\n\n`;
    anchorCache.forEach((a, idx) => {
        msg += `${idx + 1}. ${a.name} (ID: ${a.id})\n`;
    });
    msg += `\n请输入序号 (1-${anchorCache.length})：`;

    const choice = prompt(msg, "1");
    if (!choice) return;
    const index = parseInt(choice, 10) - 1;
    if (isNaN(index) || index < 0 || index >= anchorCache.length) {
        alert("输入序号无效");
        return;
    }

    const targetAnchor = anchorCache[index];
    try {
        const res = await fetch(`${API_BASE}/anchors/avatar/tasks/${taskId}/apply`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ anchor_id: targetAnchor.id }),
        });
        const json = await res.json();
        if (json.code === 0) {
            showToast(`已成功将数字人模型绑定至主播【${targetAnchor.name}】✓`, "success");
            loadAnchors();
            loadAvatarTasks();
        } else {
            alert("绑定失败: " + (json.detail || json.message));
        }
    } catch (e) {
        alert("绑定异常: " + e);
    }
}

async function deleteAvatarTask(taskId, name) {
    if (!confirm(`确认删除数字人切片任务【${name}】及其磁盘缓存产物？`)) return;
    try {
        const res = await fetch(`${API_BASE}/anchors/avatar/tasks/${taskId}`, {
            method: "DELETE"
        });
        const json = await res.json();
        if (json.code === 0) {
            showToast("数字人切片任务已删除 ✓", "success");
            loadAvatarTasks();
        } else {
            alert("删除失败: " + (json.detail || json.message));
        }
    } catch (e) {
        alert("删除异常: " + e);
    }
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
// 🎭 阶段三：数字人动作状态机与电商带货场景智能联动 (Action State Machine)
// ============================================================================
let _avatarActionsCache = [];
let _pendingUploadActionId = null;

function syncAvatarActionAnchorSelect(anchors) {
    const sel = document.getElementById("edit-action-anchor");
    if (!sel) return;
    const oldVal = sel.value;
    sel.innerHTML = '<option value="">-- 全局通用 (所有主播生效) --</option>';
    if (Array.isArray(anchors)) {
        anchors.forEach(a => {
            sel.innerHTML += `<option value="${escapeHtml(a.id)}">${escapeHtml(a.name)}</option>`;
        });
    }
    if (oldVal) sel.value = oldVal;
}

async function loadAvatarActions() {
    try {
        const res = await fetch(`${API_BASE}/anchors/avatar/actions`);
        const json = await res.json();
        if (json.code === 0 && Array.isArray(json.data)) {
            _avatarActionsCache = json.data;
            renderAvatarActionsTable(_avatarActionsCache, json.current_status);
        }
    } catch (e) {
        console.error("加载动作状态机配置失败:", e);
        const tbody = document.getElementById("avatar-actions-tbody");
        if (tbody) {
            tbody.innerHTML = '<tr><td colspan="8" style="text-align:center; color:var(--red); padding:16px;">加载动作配置失败</td></tr>';
        }
    }
}

function renderAvatarActionsTable(actions, currentStatus) {
    const tbody = document.getElementById("avatar-actions-tbody");
    if (!tbody) return;

    // 1. 更新遥测条
    if (currentStatus) {
        const curEl = document.getElementById("action-telemetry-current");
        const prioEl = document.getElementById("action-telemetry-priority");
        const cdEl = document.getElementById("action-telemetry-countdown");
        if (curEl) {
            const curName = currentStatus.action_name || (currentStatus.action_code === 0 ? "待机呼吸" : `动作 ${currentStatus.action_code}`);
            curEl.innerText = `Action ${currentStatus.action_code}: ${curName}`;
            curEl.style.color = currentStatus.action_code === 0 ? "#34d399" : "#fbbf24";
        }
        if (prioEl) {
            prioEl.innerText = `P${currentStatus.priority || 0}`;
        }
        if (cdEl) {
            if (currentStatus.action_code === 0) {
                cdEl.innerText = "循环常驻";
                cdEl.style.color = "var(--text-primary)";
            } else {
                const rem = typeof currentStatus.remaining_seconds === "number" ? currentStatus.remaining_seconds.toFixed(1) : "0.0";
                cdEl.innerText = `${rem} 秒后复位`;
                cdEl.style.color = "#34d399";
            }
        }
    }

    // 2. 渲染表格
    tbody.innerHTML = "";
    if (!actions || actions.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" style="text-align:center; color:var(--text-muted); padding:16px;">暂无动作配置</td></tr>';
        return;
    }

    const TRIGGER_TYPE_MAP = {
        "both": '<span class="badge-recommend" style="font-size:10px; background:rgba(59,130,246,0.15); color:#60a5fa; border:1px solid rgba(59,130,246,0.3);">双轨驱动</span>',
        "keyword": '<span class="badge-recommend" style="font-size:10px; background:rgba(16,185,129,0.15); color:#34d399; border:1px solid rgba(16,185,129,0.3);">话术关键词</span>',
        "event": '<span class="badge-recommend" style="font-size:10px; background:rgba(245,158,11,0.15); color:#fbbf24; border:1px solid rgba(245,158,11,0.3);">实时场控</span>',
        "manual": '<span class="badge-recommend" style="font-size:10px; background:rgba(156,163,175,0.15); color:#9ca3af; border:1px solid rgba(156,163,175,0.3);">手动触发</span>',
    };

    const EVENT_NAME_MAP = {
        "welcome": "进房欢迎 (welcome)",
        "follow": "点赞关注 (follow)",
        "order": "下单成交 (order)",
        "gift": "礼物致谢 (gift)",
    };

    actions.forEach(act => {
        const tr = document.createElement("tr");
        const isCurrent = currentStatus && currentStatus.action_code === act.action_code;
        if (isCurrent) {
            tr.style.backgroundColor = "rgba(245, 158, 11, 0.08)";
        }

        const typeBadge = TRIGGER_TYPE_MAP[act.trigger_type] || act.trigger_type;
        const kwText = act.trigger_keywords ? `<div style="font-size:11px; color:var(--text-muted); margin-top:2px;">匹配: ${escapeHtml(act.trigger_keywords)}</div>` : '';
        const evtText = act.trigger_events ? (EVENT_NAME_MAP[act.trigger_events] || act.trigger_events) : '<span style="color:var(--text-muted); font-size:11px;">未绑定</span>';

        const framesBadge = act.frames_count > 0
            ? `<span class="badge-recommend" style="font-size:11px; background:rgba(16,185,129,0.15); color:#34d399; border:1px solid rgba(16,185,129,0.3);">🎬 ${act.frames_count} 帧就绪</span>`
            : `<span style="font-size:11px; color:var(--text-muted);">未提取视频</span>`;

        const anchorLabel = act.anchor_id ? `<span style="font-size:11px; color:#38bdf8;">专用主播</span>` : `<span style="font-size:11px; color:var(--text-muted);">全局通用</span>`;

        tr.innerHTML = `
            <td>
                <span style="font-weight:700; font-size:13px; color:${act.action_code === 0 ? '#34d399' : '#fbbf24'};">
                    #${act.action_code}
                </span>
                ${isCurrent ? '<span style="display:inline-block; width:6px; height:6px; border-radius:50%; background:#10b981; margin-left:4px;" title="当前运行中"></span>' : ''}
            </td>
            <td>
                <div style="font-weight:600; font-size:13px;">${escapeHtml(act.action_name)}</div>
                <div style="font-size:10px; color:var(--text-muted);">${act.mirror_loop ? '🔁 镜像循环' : '单向循环'}</div>
            </td>
            <td>${anchorLabel}</td>
            <td>
                <div>${typeBadge}</div>
                ${kwText}
            </td>
            <td><span style="font-size:12px; font-weight:500;">${evtText}</span></td>
            <td>
                <div style="font-size:12px; font-weight:600;">${act.duration_sec}s</div>
                <div style="font-size:10px; color:#fbbf24;">优先级 P${act.priority}</div>
            </td>
            <td>${framesBadge}</td>
            <td style="white-space: nowrap; padding: 10px 8px;">
                <div class="table-actions" style="display: inline-flex; align-items: center; gap: 4px; flex-wrap: nowrap; white-space: nowrap;">
                    <button class="btn btn-sm btn-primary" style="padding: 2px 7px; font-size: 11px; height: 26px; line-height: 20px; display: inline-flex; align-items: center; gap: 2px; white-space: nowrap; margin: 0;" onclick="testTriggerAvatarAction(${act.action_code})" title="手动触发测试状态机切换">
                        ▶ 测试
                    </button>
                    <button class="btn btn-sm" style="padding: 2px 7px; font-size: 11px; height: 26px; line-height: 20px; display: inline-flex; align-items: center; gap: 2px; white-space: nowrap; margin: 0; background: rgba(147, 51, 234, 0.12); color: #c084fc; border: 1px solid rgba(147, 51, 234, 0.35);" onclick="triggerUploadActionClip('${act.id}')" title="上传高清切片短视频并抽帧">
                        🎬 视频
                    </button>
                    <button class="btn btn-sm" style="padding: 2px 7px; font-size: 11px; height: 26px; line-height: 20px; display: inline-flex; align-items: center; gap: 2px; white-space: nowrap; margin: 0;" onclick="openActionEditCard('${act.id}')" title="编辑动作参数与关键词">
                        ✏ 编辑
                    </button>
                    ${act.action_code !== 0
                        ? `<button class="btn btn-sm btn-danger" style="padding: 2px 7px; font-size: 11px; height: 26px; line-height: 20px; display: inline-flex; align-items: center; gap: 2px; white-space: nowrap; margin: 0;" onclick="deleteAvatarAction('${act.id}')" title="删除动作">🗑 删除</button>`
                        : `<button class="btn btn-sm" disabled style="padding: 2px 7px; font-size: 11px; height: 26px; line-height: 20px; display: inline-flex; align-items: center; gap: 2px; white-space: nowrap; margin: 0; opacity: 0.25; cursor: not-allowed; border-style: dashed;" title="待机底模不可删除">🗑 删除</button>`
                    }
                </div>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

function openActionEditCard(actionId) {
    const card = document.getElementById("action-edit-card");
    if (!card) return;
    const act = _avatarActionsCache.find(a => a.id === actionId);
    if (!act) return;

    document.getElementById("action-edit-title").innerText = `编辑动作槽位: #${act.action_code} - ${act.action_name}`;
    document.getElementById("edit-action-id").value = act.id;
    document.getElementById("edit-action-code").value = act.action_code;
    document.getElementById("edit-action-code").disabled = (act.action_code === 0);
    document.getElementById("edit-action-name").value = act.action_name;
    document.getElementById("edit-action-anchor").value = act.anchor_id || "";
    document.getElementById("edit-action-trigger-type").value = act.trigger_type || "both";
    document.getElementById("edit-action-duration").value = act.duration_sec;
    document.getElementById("edit-action-priority").value = act.priority;
    document.getElementById("edit-action-keywords").value = act.trigger_keywords || "";
    document.getElementById("edit-action-events").value = act.trigger_events || "";
    document.getElementById("edit-action-mirror").checked = !!act.mirror_loop;
    document.getElementById("edit-action-active").checked = !!act.is_active;

    card.style.display = "block";
    card.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function closeActionEditCard() {
    const card = document.getElementById("action-edit-card");
    if (card) card.style.display = "none";
}

async function submitSaveAvatarAction() {
    const id = document.getElementById("edit-action-id").value;
    const action_code = parseInt(document.getElementById("edit-action-code").value, 10);
    const action_name = document.getElementById("edit-action-name").value.trim();
    if (isNaN(action_code) || action_code < 0) {
        alert("请输入有效的动作槽位编号 (>= 0)");
        return;
    }
    if (!action_name) {
        alert("动作名称不可为空");
        return;
    }

    const payload = {
        id: id || undefined,
        action_code: action_code,
        action_name: action_name,
        anchor_id: document.getElementById("edit-action-anchor").value || null,
        trigger_type: document.getElementById("edit-action-trigger-type").value,
        duration_sec: parseFloat(document.getElementById("edit-action-duration").value) || 3.5,
        priority: parseInt(document.getElementById("edit-action-priority").value, 10) || 1,
        trigger_keywords: document.getElementById("edit-action-keywords").value.trim(),
        trigger_events: document.getElementById("edit-action-events").value.trim(),
        mirror_loop: document.getElementById("edit-action-mirror").checked ? 1 : 0,
        is_active: document.getElementById("edit-action-active").checked ? 1 : 0,
    };

    try {
        const res = await fetch(`${API_BASE}/anchors/avatar/actions`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const json = await res.json();
        if (json.code === 0) {
            closeActionEditCard();
            loadAvatarActions();
        } else {
            alert(json.detail || json.message || "保存失败");
        }
    } catch (e) {
        alert("保存动作配置异常: " + e);
    }
}

function triggerUploadActionClip(actionId) {
    _pendingUploadActionId = actionId;
    const uploader = document.getElementById("action-clip-uploader");
    if (uploader) {
        uploader.value = "";
        uploader.click();
    }
}

async function handleActionClipFileSelected(input) {
    if (!input.files || input.files.length === 0 || !_pendingUploadActionId) return;
    const file = input.files[0];
    const actId = _pendingUploadActionId;
    _pendingUploadActionId = null;

    const fd = new FormData();
    fd.append("file", file);

    const btn = document.activeElement;
    if (btn && btn.tagName === "BUTTON") {
        btn.disabled = true;
        btn.innerText = "上传中...";
    }

    try {
        const res = await fetch(`${API_BASE}/anchors/avatar/actions/${actId}/upload-clip`, {
            method: "POST",
            body: fd,
        });
        const json = await res.json();
        if (json.code === 0) {
            alert(json.message || "切片视频已成功解析并加载！");
            loadAvatarActions();
        } else {
            alert(json.detail || json.message || "上传解析失败");
        }
    } catch (e) {
        alert("上传切片视频异常: " + e);
    } finally {
        if (btn && btn.tagName === "BUTTON") {
            btn.disabled = false;
            btn.innerText = "🎬 视频";
        }
    }
}

async function testTriggerAvatarAction(actionCode) {
    try {
        const res = await fetch(`${API_BASE}/anchors/avatar/actions/test-trigger`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action_code: actionCode }),
        });
        const json = await res.json();
        if (json.code === 0) {
            renderAvatarActionsTable(_avatarActionsCache, json.current_status);
        } else {
            alert(json.message || "抢占被忽略");
        }
    } catch (e) {
        console.error("调试触发动作异常:", e);
    }
}

async function deleteAvatarAction(actionId) {
    if (!confirm("确定要删除该动作配置及已关联的切片视频吗？")) return;
    try {
        const res = await fetch(`${API_BASE}/anchors/avatar/actions/${actionId}`, {
            method: "DELETE",
        });
        const json = await res.json();
        if (json.code === 0) {
            loadAvatarActions();
        } else {
            alert(json.detail || json.message || "删除失败");
        }
    } catch (e) {
        alert("删除动作失败: " + e);
    }
}

// -----------------------------------------------------------------------------
// 绿幕 3 段式实拍 SOP 指南与 OBS/抖音直播伴侣导播助手交互
// -----------------------------------------------------------------------------

function openShootingGuideModal() {
    const modal = document.getElementById("modal-shooting-guide");
    if (modal) {
        modal.style.display = "flex";
    }
}

function closeShootingGuideModal() {
    const modal = document.getElementById("modal-shooting-guide");
    if (modal) {
        modal.style.display = "none";
    }
}

function openStudioHelperModal() {
    const modal = document.getElementById("modal-studio-helper");
    if (modal) {
        modal.style.display = "flex";
    }
}

function closeStudioHelperModal() {
    const modal = document.getElementById("modal-studio-helper");
    if (modal) {
        modal.style.display = "none";
    }
}

function copyObsSettings() {
    const params = `【OBS Studio 色度键最佳抠像推荐参数】\n` +
        `• 关键颜色类型: 绿色 (Green)\n` +
        `• 相似度 (Similarity): 400\n` +
        `• 平滑 (Smoothness): 80\n` +
        `• 主溢出减少 (Key Color Spill Reduction): 100\n` +
        `• 不透明度: 100%\n` +
        `• 对比度/亮度: 保持默认 0.00\n` +
        `• 音频人声闪避: 侧链监听(Sidechain) 阈值 -24dB, 比率 4:1, 衰减 15dB`;
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(params).then(() => {
            alert("📋 OBS 色度键与演播室参数已成功复制到剪贴板！可以直接粘贴参考。");
        }).catch(() => {
            prompt("请手动复制以下参数：", params);
        });
    } else {
        prompt("请手动复制以下参数：", params);
    }
}

// ============================================================================
// 🎭 数字人动作切片状态机与带货手势管理交互逻辑 (阶段三)
// ============================================================================

let _currentActionAnchorId = null;
let _currentActionAnchorName = "";

// 打开主播动作管理弹窗
async function openAnchorActionsModal(anchorId, anchorName) {
    _currentActionAnchorId = anchorId;
    _currentActionAnchorName = anchorName || "未命名主播";

    const nameEl = document.getElementById("modal-actions-anchor-name");
    if (nameEl) nameEl.textContent = _currentActionAnchorName;

    const modal = document.getElementById("modal-anchor-actions");
    if (modal) {
        modal.style.display = "flex";
    }

    switchActionInputTab("split");
    await loadAnchorActions(anchorId);
}

// 关闭主播动作管理弹窗
function closeAnchorActionsModal() {
    const modal = document.getElementById("modal-anchor-actions");
    if (modal) {
        modal.style.display = "none";
    }
}

// 切换添加切片的模式 Tab (split / upload)
function switchActionInputTab(tab) {
    const btnSplit = document.getElementById("btn-tab-split-segments");
    const btnUpload = document.getElementById("btn-tab-upload-single");
    const pnlSplit = document.getElementById("panel-action-split");
    const pnlUpload = document.getElementById("panel-action-upload");

    if (tab === "split") {
        if (btnSplit) {
            btnSplit.style.background = "rgba(56,189,248,0.15)";
            btnSplit.style.color = "#38bdf8";
            btnSplit.style.borderColor = "rgba(56,189,248,0.4)";
        }
        if (btnUpload) {
            btnUpload.style.background = "transparent";
            btnUpload.style.color = "var(--text-muted)";
            btnUpload.style.borderColor = "rgba(255,255,255,0.15)";
        }
        if (pnlSplit) pnlSplit.style.display = "block";
        if (pnlUpload) pnlUpload.style.display = "none";
    } else {
        if (btnUpload) {
            btnUpload.style.background = "rgba(56,189,248,0.15)";
            btnUpload.style.color = "#38bdf8";
            btnUpload.style.borderColor = "rgba(56,189,248,0.4)";
        }
        if (btnSplit) {
            btnSplit.style.background = "transparent";
            btnSplit.style.color = "var(--text-muted)";
            btnSplit.style.borderColor = "rgba(255,255,255,0.15)";
        }
        if (pnlSplit) pnlSplit.style.display = "none";
        if (pnlUpload) pnlUpload.style.display = "block";
    }
}

// 加载指定主播的动作切片列表
async function loadAnchorActions(anchorId) {
    const container = document.getElementById("modal-actions-list");
    const badge = document.getElementById("modal-actions-count-badge");
    if (!container) return;

    container.innerHTML = '<div style="color:var(--text-muted); font-size:12px; padding:20px; text-align:center; grid-column:1/-1;">正在加载主播动作切片...</div>';

    try {
        const res = await fetch(`${API_BASE}/anchors/${anchorId}/actions`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const json = await res.json();
        const actions = (json && json.data) ? json.data : [];

        if (badge) badge.textContent = `${actions.length} 组`;

        if (actions.length === 0) {
            container.innerHTML = `
                <div style="color:var(--text-muted); font-size:12px; padding:20px; text-align:center; grid-column:1/-1; background:rgba(0,0,0,0.2); border-radius:8px;">
                    暂无专属动作切片，系统正采用全局默认状态机规则。<br>
                    建议使用下方【单视频多时间戳智能拆解】一键生成待机、小黄车与致谢手势。
                </div>
            `;
            return;
        }

        container.innerHTML = actions.map(act => {
            const isIdle = act.action_code === 0;
            const codeTag = `<span style="font-family:monospace; background:rgba(255,255,255,0.08); padding:1px 6px; border-radius:4px; font-size:11px;">#${act.action_code}</span>`;
            const priorityTag = `<span style="font-size:10px; color:#38bdf8; background:rgba(56,189,248,0.1); padding:1px 5px; border-radius:3px;">优:${act.priority}</span>`;
            const durationTag = `<span style="font-size:10px; color:#fbbf24; background:rgba(245,158,11,0.1); padding:1px 5px; border-radius:3px;">${act.duration_sec}s</span>`;
            const framesTag = act.frames_count > 0
                ? `<span style="font-size:11px; color:#34d399;">✓ ${act.frames_count} 帧 (神经就绪)</span>`
                : `<span style="font-size:11px; color:var(--text-muted);">⚪ 待提取切片</span>`;

            const previewImg = act.preview_url
                ? `<img src="${act.preview_url}" style="width:100%; height:90px; object-fit:cover; border-radius:4px; margin-bottom:8px; border:1px solid rgba(255,255,255,0.08);" alt="动作预览">`
                : `<div style="width:100%; height:90px; background:rgba(0,0,0,0.4); border-radius:4px; margin-bottom:8px; display:flex; align-items:center; justify-content:center; color:var(--text-muted); font-size:24px; border:1px solid rgba(255,255,255,0.05);">🎬</div>`;

            const delBtn = isIdle
                ? `<span style="font-size:11px; color:var(--text-muted);">核心基底</span>`
                : `<button class="btn btn-sm btn-danger" style="padding:2px 6px; font-size:11px;" onclick="deleteAnchorAction('${act.id}')">删除</button>`;

            const kwText = act.trigger_keywords ? escapeHtml(act.trigger_keywords) : '<span style="color:var(--text-muted);">无</span>';

            return `
                <div style="background:rgba(0,0,0,0.3); border:1px solid rgba(255,255,255,0.08); border-radius:8px; padding:12px; display:flex; flex-direction:column; justify-content:space-between;">
                    <div>
                        ${previewImg}
                        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
                            <strong style="color:#f8fafc; font-size:13px;">${escapeHtml(act.action_name)}</strong>
                            <div style="display:flex; gap:4px; align-items:center;">${codeTag} ${priorityTag} ${durationTag}</div>
                        </div>
                        <div style="font-size:11px; color:var(--text-muted); margin-bottom:6px; line-height:1.4;">
                            触发词: ${kwText}
                        </div>
                    </div>
                    <div style="display:flex; justify-content:space-between; align-items:center; border-top:1px solid rgba(255,255,255,0.06); padding-top:8px; margin-top:6px;">
                        ${framesTag}
                        ${delBtn}
                    </div>
                </div>
            `;
        }).join("");
    } catch (e) {
        console.error("加载动作切片列表失败:", e);
        container.innerHTML = `<div style="color:#f87171; font-size:12px; padding:20px; text-align:center; grid-column:1/-1;">加载动作切片列表失败: ${e.message}</div>`;
    }
}

// 提交多时间戳智能拆解
async function submitSplitVideoActions() {
    if (!_currentActionAnchorId) {
        showToast("未指定有效主播", "error");
        return;
    }

    const idleStart = parseFloat(document.getElementById("split-idle-start")?.value || 0);
    const idleEnd = parseFloat(document.getElementById("split-idle-end")?.value || 25);
    const cartStart = parseFloat(document.getElementById("split-cart-start")?.value || 25);
    const cartEnd = parseFloat(document.getElementById("split-cart-end")?.value || 40);
    const thanksStart = parseFloat(document.getElementById("split-thanks-start")?.value || 40);
    const thanksEnd = parseFloat(document.getElementById("split-thanks-end")?.value || 55);

    const segments = [
        { action_code: 0, name: "待机呼吸循环", start_sec: idleStart, end_sec: idleEnd, keywords: "", priority: 0 },
        { action_code: 3, name: "促单指引小黄车", start_sec: cartStart, end_sec: cartEnd, keywords: "购物车,下单,左下角,抢购,拍下", priority: 5 },
        { action_code: 4, name: "大额打赏致谢", start_sec: thanksStart, end_sec: thanksEnd, keywords: "感谢,礼物,破费,大气,老板大气", priority: 9 }
    ];

    const btn = document.getElementById("btn-submit-split-actions");
    const origText = btn ? btn.textContent : "";
    if (btn) {
        btn.disabled = true;
        btn.textContent = "⏳ 正在并行裁剪并提取关键帧与口型坐标，请稍候...";
    }

    try {
        const res = await fetch(`${API_BASE}/anchors/${_currentActionAnchorId}/actions/split-segments`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ segments })
        });
        const json = await res.json();
        if (json.code === 0) {
            showToast(json.message || "多动作切片已成功拆解生成！", "success");
            await loadAnchorActions(_currentActionAnchorId);
            loadAnchors(); // 同步刷新主播表格
        } else {
            showToast(`拆解失败: ${json.detail || json.message || "未知错误"}`, "error");
        }
    } catch (e) {
        console.error("提交分段拆解异常:", e);
        showToast(`提交异常: ${e.message}`, "error");
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = origText;
        }
    }
}

// 提交上传单动作视频切片
async function submitSingleActionUpload() {
    if (!_currentActionAnchorId) {
        showToast("未指定有效主播", "error");
        return;
    }

    const fileInput = document.getElementById("single-action-file");
    if (!fileInput || !fileInput.files || fileInput.files.length === 0) {
        showToast("请选择视频切片文件", "warning");
        return;
    }

    const file = fileInput.files[0];
    const code = parseInt(document.getElementById("single-action-code")?.value || 3);
    const keywords = document.getElementById("single-action-keywords")?.value || "";

    const nameMap = {
        0: "基础待机呼吸",
        1: "热情挥手欢迎",
        2: "求关注与点赞",
        3: "促单指引小黄车",
        4: "大额打赏致谢"
    };

    const formData = new FormData();
    formData.append("file", file);
    formData.append("action_code", code);
    formData.append("action_name", nameMap[code] || `动作_${code}`);
    formData.append("trigger_keywords", keywords);
    formData.append("duration_sec", 3.5);
    formData.append("priority", code === 3 ? 5 : (code === 4 ? 9 : 2));

    const btn = document.getElementById("btn-submit-single-action");
    const origText = btn ? btn.textContent : "";
    if (btn) {
        btn.disabled = true;
        btn.textContent = "⏳ 正在提取神经切片与面部坐标...";
    }

    try {
        const res = await fetch(`${API_BASE}/anchors/${_currentActionAnchorId}/actions/upload`, {
            method: "POST",
            body: formData
        });
        const json = await res.json();
        if (json.code === 0) {
            showToast("动作切片上传并处理完成 ✓", "success");
            fileInput.value = "";
            await loadAnchorActions(_currentActionAnchorId);
            loadAnchors();
        } else {
            showToast(`上传失败: ${json.detail || json.message || "未知错误"}`, "error");
        }
    } catch (e) {
        console.error("上传单动作切片异常:", e);
        showToast(`上传异常: ${e.message}`, "error");
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = origText;
        }
    }
}

// 删除指定动作切片
async function deleteAnchorAction(actionId) {
    if (!confirm("确认删除该动作切片及其神经资产吗？")) return;

    try {
        const res = await fetch(`${API_BASE}/anchors/avatar/actions/${actionId}`, {
            method: "DELETE"
        });
        const json = await res.json();
        if (json.code === 0) {
            showToast("动作切片已删除", "info");
            if (_currentActionAnchorId) {
                await loadAnchorActions(_currentActionAnchorId);
            }
            loadAnchors();
        } else {
            showToast(`删除失败: ${json.detail || json.message || "未知错误"}`, "error");
        }
    } catch (e) {
        console.error("删除动作切片失败:", e);
        showToast(`删除异常: ${e.message}`, "error");
    }
}



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

/* =========================================================================
   内置 RTMP 直推引擎前端控制与联动 (脱离 OBS 一键推流)
   ========================================================================= */

const RTMP_STORAGE_KEY_URL = "ai_live_rtmp_url";
const RTMP_STORAGE_KEY_KEY = "ai_live_rtmp_key";

function toggleRtmpModal() {
    const modal = document.getElementById("rtmp-settings-modal");
    if (!modal) return;
    const isHidden = modal.style.display === "none" || !modal.style.display;
    modal.style.display = isHidden ? "flex" : "none";
    if (isHidden) {
        const urlInput = document.getElementById("rtmp-input-url");
        const keyInput = document.getElementById("rtmp-input-key");
        if (urlInput && !urlInput.value) {
            urlInput.value = localStorage.getItem(RTMP_STORAGE_KEY_URL) || "rtmp://live-push.bilivideo.com/live-bvc/";
        }
        if (keyInput && !keyInput.value) {
            keyInput.value = localStorage.getItem(RTMP_STORAGE_KEY_KEY) || "";
        }
        refreshRtmpStatus();
    }
}

function fillRtmpPreset(platform) {
    const urlInput = document.getElementById("rtmp-input-url");
    if (!urlInput) return;
    switch (platform) {
        case "bilibili":
            urlInput.value = "rtmp://live-push.bilivideo.com/live-bvc/";
            break;
        case "douyin":
            urlInput.value = "rtmp://live-push.douyincdn.com/live/";
            break;
        case "kuaishou":
            urlInput.value = "rtmp://live-push.kuaishou.com/live/";
            break;
        case "channels":
            urlInput.value = "rtmp://channels.weixin.qq.com/live/";
            break;
    }
    showToast(`已填入 ${platform} 常用推流前缀，请在下方补充直播码`, "info");
}

async function refreshRtmpStatus() {
    try {
        const res = await fetch(`${API_BASE}/live/rtmp/status`);
        const json = await res.json();
        if (json.code === 0 && json.data) {
            updateRtmpUI(json.data);
        }
    } catch (e) {
        console.warn("读取 RTMP 状态失败:", e);
    }
}

function updateRtmpUI(status) {
    const indicator = document.getElementById("rtmp-status-indicator");
    const badge = document.getElementById("rtmp-stream-badge");
    const btn = document.getElementById("btn-toggle-rtmp");
    const durEl = document.getElementById("rtmp-metric-duration");
    const frameEl = document.getElementById("rtmp-metric-frames");
    const audioEl = document.getElementById("rtmp-metric-audio");
    const errBanner = document.getElementById("rtmp-error-banner");

    const isStreaming = Boolean(status.is_streaming);

    if (indicator) {
        if (isStreaming) {
            indicator.style.color = "#10B981";
            indicator.textContent = "● 直推传输中 (25 FPS)";
        } else if (status.state === "error") {
            indicator.style.color = "#ef4444";
            indicator.textContent = "✕ 推流异常";
        } else {
            indicator.style.color = "#94a3b8";
            indicator.textContent = "○ 未启动";
        }
    }

    if (badge) {
        badge.style.display = isStreaming ? "inline-flex" : "none";
        if (isStreaming) {
            badge.textContent = `● RTMP直推 (${status.duration_seconds}s)`;
        }
    }

    if (btn) {
        if (isStreaming) {
            btn.style.background = "#ef4444";
            btn.style.borderColor = "#ef4444";
            btn.textContent = "⏹ 停止直推";
        } else {
            btn.style.background = "#10B981";
            btn.style.borderColor = "#10B981";
            btn.textContent = "▶ 立即开始直推";
        }
    }

    if (durEl) {
        const secs = Math.floor(status.duration_seconds || 0);
        const h = String(Math.floor(secs / 3600)).padStart(2, "0");
        const m = String(Math.floor((secs % 3600) / 60)).padStart(2, "0");
        const s = String(secs % 60).padStart(2, "0");
        durEl.textContent = `${h}:${m}:${s}`;
    }

    if (frameEl) frameEl.textContent = String(status.frames_sent || 0);
    if (audioEl) {
        const kb = Math.round((status.audio_bytes_sent || 0) / 1024);
        audioEl.textContent = `${kb} KB`;
    }

    if (errBanner) {
        if (status.last_error && !isStreaming) {
            errBanner.style.display = "block";
            errBanner.textContent = "最后推流提示: " + status.last_error;
        } else {
            errBanner.style.display = "none";
        }
    }
}

async function toggleRtmpStreaming() {
    const btn = document.getElementById("btn-toggle-rtmp");
    const isStopping = btn && btn.textContent.includes("停止");

    if (isStopping) {
        try {
            const res = await fetch(`${API_BASE}/live/rtmp/stop`, { method: "POST" });
            const json = await res.json();
            showToast(json.message || "推流已安全停止", "info");
            refreshRtmpStatus();
        } catch (e) {
            showToast("停止推流请求失败: " + e, "error");
        }
    } else {
        const urlInput = document.getElementById("rtmp-input-url");
        const keyInput = document.getElementById("rtmp-input-key");
        const rtmpUrl = (urlInput ? urlInput.value : "").trim();
        const streamKey = (keyInput ? keyInput.value : "").trim();

        if (!rtmpUrl) {
            showToast("请先填写推流服务器地址 (RTMP URL)", "warn");
            return;
        }

        localStorage.setItem(RTMP_STORAGE_KEY_URL, rtmpUrl);
        localStorage.setItem(RTMP_STORAGE_KEY_KEY, streamKey);

        try {
            const res = await fetch(`${API_BASE}/live/rtmp/start`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    rtmp_url: rtmpUrl,
                    stream_key: streamKey,
                    width: 720,
                    height: 960,
                    fps: 25,
                    bitrate_kbps: 2500
                })
            });
            const json = await res.json();
            if (json.code === 0) {
                showToast("内置 RTMP 直推引擎已启动！", "success");
                refreshRtmpStatus();
            } else {
                showToast("启动推流失败: " + (json.message || json.detail), "error");
                refreshRtmpStatus();
            }
        } catch (e) {
            showToast("请求启动推流异常: " + e, "error");
        }
    }
}

/* =========================================================================
   GPU配置(2) 与数字人画面选型：一行多卡片与独立多行描述卡片联动
   ========================================================================= */
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
    } catch (_) { }

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
// 5. 通用公共方法：真实探测云端 GPU 连通性与显卡状态 (全局可复用)
// ------------------------------------------------------------------------------
/**
 * 全局公共方法：真实探测云端 GPU 渲染节点连通性与显卡状态
 * @param {Object} options
 * @param {string} [options.config_id] 配置 ID（可选，提供则交由后端按加密存储自动解析）
 * @param {string} [options.base_url] 云端 WebSocket / HTTP 目标地址
 * @param {string} [options.api_key] 认证 Token / 密码
 * @param {boolean} [options.isSilent=false] 是否静默检测（不弹 Toast）
 * @returns {Promise<{ success: boolean, latency_ms: number, device: string, error: string, targetUrl: string }>}
 */
async function checkCloudGpuConnection(options = {}) {
    let targetUrl = (options.base_url || "").trim();
    if (!targetUrl) {
        const urlInput = document.getElementById("gpu-input-base-url");
        if (urlInput) targetUrl = urlInput.value.trim();
    }
    if (!targetUrl && typeof localStorage !== "undefined") {
        targetUrl = localStorage.getItem("last_sidecar_base_url") || "";
    }
    if (!targetUrl) {
        const res = {
            success: false,
            latency_ms: 0,
            device: "",
            error: "未配置云端连接地址",
            targetUrl: ""
        };
        lastCloudPingResult = res;
        window.lastCloudPingResult = res;
        return res;
    }

    // 智能协议纠偏：自动转为标准 wss://.../ws/render-v3
    const sanitizedTarget = sanitizeSidecarWsUrl(targetUrl);
    if (sanitizedTarget) {
        targetUrl = sanitizedTarget;
    }

    let apiKeyToSend = options.api_key;
    if (apiKeyToSend === undefined || apiKeyToSend === null) {
        const tokenInput = document.getElementById("gpu-input-auth-token");
        let typedToken = tokenInput ? tokenInput.value.trim() : "";
        if (typedToken.includes("•")) {
            apiKeyToSend = null; // 掩码交由后端安全解密
        } else if (!typedToken) {
            apiKeyToSend = "__NO_AUTH__";
        } else {
            apiKeyToSend = typedToken;
        }
    }

    let configId = options.config_id || (document.getElementById("gpu-avatar-config-id") || {}).value || null;

    try {
        const res = await fetch(`${API_BASE}/settings/test-connection`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                config_id: configId,
                config_group: "neural_renderer",
                provider_name: "sidecar_v3",
                base_url: targetUrl,
                api_key: apiKeyToSend
            })
        });
        function parseDeviceVramGb(deviceStr) {
            if (!deviceStr || typeof deviceStr !== "string") return 0;
            const mMiB = deviceStr.match(/(\d+)\s*(?:MiB|MB)/i);
            if (mMiB) {
                return Math.round(parseInt(mMiB[1], 10) / 1024);
            }
            const mGB = deviceStr.match(/(\d+(?:\.\d+)?)\s*GB/i);
            if (mGB) {
                return Math.round(parseFloat(mGB[1]));
            }
            return 0;
        }
        window.parseDeviceVramGb = parseDeviceVramGb;

        const json = await res.json();
        const isOk = (json.code === 0 && Boolean(json.success));
        let realVramGb = 0;
        if (isOk) {
            if (json.vram_gb) {
                realVramGb = Math.round(json.vram_gb);
            } else if (json.device) {
                realVramGb = parseDeviceVramGb(json.device);
            }
        }
        let errMsg = isOk ? "" : (json.message || "握手通信失败或云端未响应");
        if (errMsg) {
            errMsg = errMsg.replace(/\s*\(ConnectionResetError\)/g, "");
        }
        const result = {
            success: isOk,
            latency_ms: isOk ? (json.latency_ms || 45) : 0,
            device: isOk ? (json.device || "NVIDIA 云端 GPU") : "",
            vram_gb: realVramGb,
            error: errMsg,
            targetUrl: targetUrl
        };

        lastCloudPingResult = result;
        window.lastCloudPingResult = result;

        if (isOk) {
            try { localStorage.setItem("last_sidecar_base_url", targetUrl); } catch (e) { }
        }
        return result;
    } catch (e) {
        const result = {
            success: false,
            latency_ms: 0,
            device: "",
            vram_gb: 0,
            error: `网络连接异常: ${e.message || e}`,
            targetUrl: targetUrl
        };
        lastCloudPingResult = result;
        window.lastCloudPingResult = result;
        return result;
    }
}
window.checkCloudGpuConnection = checkCloudGpuConnection;

// 快速连通性握手测试与配合达标即时反馈 (在 GPU 配置界面点击测试按钮时触发)
async function testCurrentGpuAvatarConnection(isSilent = false) {
    const urlInput = document.getElementById("gpu-input-base-url");
    const testBtn = document.getElementById("gpu-avatar-test-btn");
    const resultBox = document.getElementById("gpu-avatar-conn-result");
    const inlineStatusEl = document.getElementById("gpu-cloud-ping-inline-status");

    let targetUrl = urlInput ? urlInput.value.trim() : "";
    if (!targetUrl) {
        if (!isSilent) {
            showToast("请先输入云端连接地址再测试", "error");
            if (urlInput) urlInput.focus();
        }
        return;
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

    if (inlineStatusEl) {
        inlineStatusEl.style.color = "#38bdf8";
        inlineStatusEl.innerHTML = `⏳ 握手探测中...`;
    }

    try {
        // 直接复用全局公共检测方法
        const pingRes = await checkCloudGpuConnection({ base_url: targetUrl, isSilent });

        if (pingRes.success) {
            if (inlineStatusEl) {
                inlineStatusEl.style.color = "#34d399";
                inlineStatusEl.innerHTML = `✓ 已连通 (延迟 ${pingRes.latency_ms}ms${pingRes.device ? ' · ' + escapeHtml(pingRes.device) : ''})`;
            }
            if (resultBox) {
                resultBox.style.background = "rgba(16, 185, 129, 0.15)";
                resultBox.style.color = "#10B981";
                resultBox.style.border = "1px solid rgba(16, 185, 129, 0.35)";
                resultBox.innerHTML = `🟢 配合达标！云端握手成功，延迟: ${pingRes.latency_ms}ms ${pingRes.device ? '(远端识别硬件: <b>' + escapeHtml(pingRes.device) + '</b>)' : ''}，本地主控与云端 GPU 协同就绪！`;
            }
            if (!isSilent) {
                showToast(`✅ 端云协同达标！${pingRes.device ? '云端硬件: ' + pingRes.device : ''}（延迟: ${pingRes.latency_ms}ms）`, "success");
            }
        } else {
            const err = pingRes.error || "通信握手未成功";
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

        // 同步刷新向导卡片（若向导卡片在 DOM 中）
        renderWizardGpuStatusCard(false);
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
// 7. 静默自动探测云端 GPU 连通性与硬件状态
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
        const pingRes = await checkCloudGpuConnection({ config_id: cloudCfg.id || null, base_url: targetUrl, isSilent: true });
        if (pingRes.success) {
            if (inlineStatusEl) {
                inlineStatusEl.style.color = "#34d399";
                inlineStatusEl.innerHTML = `✓ 已自动连通 (延迟 ${pingRes.latency_ms}ms${pingRes.device ? ' · ' + escapeHtml(pingRes.device) : ''})`;
            }
            const resultBox = document.getElementById("gpu-avatar-conn-result");
            if (resultBox && currentSelectedGpuMode === "sidecar_v3") {
                resultBox.style.display = "flex";
                resultBox.style.background = "rgba(16, 185, 129, 0.15)";
                resultBox.style.color = "#10B981";
                resultBox.style.border = "1px solid rgba(16, 185, 129, 0.35)";
                resultBox.innerHTML = `🟢 配合达标！云端已自动握手就绪，延迟: ${pingRes.latency_ms}ms ${pingRes.device ? '(远端识别硬件: <b>' + escapeHtml(pingRes.device) + '</b>)' : ''}，端云协同就绪！`;
            }
        } else {
            lastCloudPingResult = {
                success: false,
                latency_ms: 0,
                device: "",
                error: pingRes.error || "未连通"
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
// 8. 开播向导第二步专用：一键切换本地模式（开播防黑屏避险）
// ------------------------------------------------------------------------------
async function quickSwitchToLocalModeFromWizard() {
    try {
        const activeCloudConfigs = (cachedAvatarConfigs || []).filter(c => c.is_active && c.config_group === "neural_renderer");
        for (const cfg of activeCloudConfigs) {
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
            cfg.is_active = false;
        }

        // 同步持久化算力偏好为 auto 本地运行
        try {
            await fetch(`${API_BASE}/settings/gpu-target`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ target: "auto" })
            });
        } catch (e) { }

        showToast("✅ 已一键切换为【本地运行模式】！开播将优先保障流畅稳定防黑屏", "success");
        currentSelectedGpuMode = "local_procedural";
        await renderWizardGpuStatusCard(false);
    } catch (e) {
        showToast("切换本地运行模式失败: " + e, "error");
    }
}
window.quickSwitchToLocalModeFromWizard = quickSwitchToLocalModeFromWizard;

// ------------------------------------------------------------------------------
// 9. 开播向导第二步卡片真实状态渲染（直接复用 checkCloudGpuConnection 真实探测）
// ------------------------------------------------------------------------------
let isWizardGpuProbing = false;

async function renderWizardGpuStatusCard(forceProbe = false) {
    const container = document.getElementById("wizard-gpu-display-container");
    const headerBadge = document.getElementById("wizard-gpu-badge");
    if (!container) return;

    // 优先确保配置缓存有数据
    if (!cachedAvatarConfigs || cachedAvatarConfigs.length === 0) {
        try {
            const configsRes = await fetch(`${API_BASE}/settings/configs`);
            if (configsRes.ok) {
                const configsJson = await configsRes.json();
                cachedAvatarConfigs = (configsJson.data || []).filter(item => item.config_group === "neural_renderer");
            }
        } catch (e) {
            console.warn("向导拉取 GPU 配置失败:", e);
        }
    }

    // 确保真实本地硬件信息已加载（严禁硬编码默认 CPU 核心数）
    if (!window.cachedHardwareData || !window.cachedHardwareData.cpu_cores) {
        try {
            const hwRes = await fetch(`${API_BASE}/live/hardware`);
            if (hwRes.ok) {
                const hwJson = await hwRes.json();
                if (hwJson.code === 0 && hwJson.data) {
                    window.cachedHardwareData = hwJson.data;
                }
            }
        } catch (e) {
            console.warn("向导拉取硬件实况数据异常:", e);
        }
    }

    // 提取硬件指标动态计算 X (CPU核数), Y (本地显存), Z (云端显存)
    const hw = window.cachedHardwareData || {};
    const localGpu = hw.gpu || {};
    // 真实 CPU 核心数：实测是多少就是多少，绝不默认 6
    const cpuCores = hw.cpu_cores || 0;
    // 本地显存：实测数值
    const localVram = (localGpu.vram_total_gb !== undefined && localGpu.vram_total_gb !== null) ? Math.round(localGpu.vram_total_gb) : 0;

    // 云端配置与实测显存获取（实事求是：严禁任何写死/虚标，没有就是 0）
    const activeCloud = (cachedAvatarConfigs || []).find(c => c.is_active && c.provider_name === "sidecar_v3");
    const anyConfiguredCloud = (cachedAvatarConfigs || []).find(c => c.provider_name === "sidecar_v3" && c.base_url);
    const targetCloud = activeCloud || anyConfiguredCloud;
    const rawUrl = targetCloud ? (targetCloud.base_url || "").trim() : "";
    const isCloudActive = Boolean(activeCloud);

    let cloudVram = 0;
    const lastPing = window.lastCloudPingResult;
    // 关键准则：只有在云端真实连通并成功探测到显存时才显示具体数值；离线、关机、未配置或未探通一律严格为 0
    if (isCloudActive && lastPing && lastPing.success) {
        if (lastPing.vram_gb) {
            cloudVram = Math.round(lastPing.vram_gb);
        } else if (lastPing.device) {
            cloudVram = parseDeviceVramGb(lastPing.device);
        }
    }

    // 顶部徽标
    if (headerBadge) {
        if (isCloudActive) {
            if (lastPing && !lastPing.success) {
                headerBadge.className = "brand-badge red";
                headerBadge.style.background = "rgba(239, 68, 68, 0.2)";
                headerBadge.style.color = "#ef4444";
                headerBadge.style.border = "1px solid rgba(239, 68, 68, 0.4)";
                headerBadge.textContent = "🔴 云端离线/未连通";
            } else {
                headerBadge.className = "brand-badge sky";
                headerBadge.style.background = "";
                headerBadge.style.color = "";
                headerBadge.style.border = "";
                headerBadge.textContent = "⚡ 端云协同模式";
            }
        } else {
            headerBadge.className = "brand-badge green";
            headerBadge.style.background = "";
            headerBadge.style.color = "";
            headerBadge.style.border = "";
            headerBadge.textContent = "💻 本地运行模式";
        }
    }

    // 若当前激活了云端模式，且需要探测 / 尚未探测过
    if (isCloudActive && (forceProbe || !lastCloudPingResult) && !isWizardGpuProbing && rawUrl) {
        isWizardGpuProbing = true;
        checkCloudGpuConnection({
            config_id: activeCloud.id || null,
            base_url: rawUrl,
            isSilent: true
        }).then(pingRes => {
            isWizardGpuProbing = false;
            renderWizardGpuStatusCard(false);
        }).catch(() => {
            isWizardGpuProbing = false;
            renderWizardGpuStatusCard(false);
        });
    }

    // 选项 1：本机完全满足 (本地运行)
    const card1Selected = !isCloudActive;
    const card1Border = card1Selected ? "2px solid #10B981" : "1.5px solid rgba(148, 163, 184, 0.2)";
    const card1Bg = card1Selected ? "rgba(15, 23, 42, 0.75)" : "rgba(15, 23, 42, 0.45)";
    const card1Badge = card1Selected
        ? `<span class="brand-badge green" style="font-weight: 700;">🟢 当前生效模式</span>`
        : `<span class="brand-badge" style="background: rgba(148, 163, 184, 0.15); color: #94a3b8; border: 1px solid rgba(148, 163, 184, 0.25);">未启用 (备选)</span>`;
    const card1Action = card1Selected
        ? `<div style="font-size: 12px; color: #34d399; margin-top: 10px; display: flex; align-items: center; gap: 6px;">✓ 0 租赁成本 · 本地闭环，断网亦可流畅直播</div>`
        : `<div style="margin-top: 10px;"><button type="button" class="btn btn-xs btn-ghost" onclick="goToGpuSettingsTab()" style="font-size: 11.5px; color: #34d399; border: 1px solid rgba(16,185,129,0.3);">前往「GPU配置(2)」启用此模式 ↗</button></div>`;

    let card1SubText = "";
    if (localVram >= 6) {
        card1SubText = `本地 CPU ${cpuCores}核 · 本地独显 ${localVram}G · 0 租赁支出`;
    } else if (localVram > 0) {
        card1SubText = `本地 CPU ${cpuCores}核 · 本地显存 ${localVram}G (自适应加速/轻量渲染) · 0 租赁支出`;
    } else {
        card1SubText = `本地 CPU ${cpuCores}核 · 纯 CPU 程序化渲染 · 0 显存门槛 · 0 租赁支出`;
    }

    // 选项 2：本地硬件 + 租赁云端GPU
    const card2Selected = isCloudActive;
    const card2Border = card2Selected
        ? ((lastPing && !lastPing.success) ? "2px solid #ef4444" : "2px solid #38bdf8")
        : "1.5px solid rgba(148, 163, 184, 0.2)";
    const card2Bg = card2Selected
        ? ((lastPing && !lastPing.success) ? "rgba(35, 18, 22, 0.75)" : "rgba(15, 23, 42, 0.75)")
        : "rgba(15, 23, 42, 0.45)";
    const card2Badge = card2Selected
        ? ((lastPing && !lastPing.success)
            ? `<span class="brand-badge red" style="background: rgba(239, 68, 68, 0.2); color: #ef4444; border: 1px solid rgba(239, 68, 68, 0.4); font-weight: 700;">⚠️ 当前生效 · 云端未连通</span>`
            : `<span class="brand-badge sky" style="font-weight: 700;">⚡ 当前生效模式</span>`)
        : `<span class="brand-badge" style="background: rgba(148, 163, 184, 0.15); color: #94a3b8; border: 1px solid rgba(148, 163, 184, 0.25);">未启用 (备选)</span>`;

    let card2StatusBody = "";
    if (card2Selected) {
        if (isWizardGpuProbing) {
            card2StatusBody = `
                <div style="font-size: 12px; color: #38bdf8; margin-top: 10px; display: flex; align-items: center; gap: 8px;">
                    正在发起端云协同握手测试，核验云端 GPU 是否开机...
                </div>
            `;
        } else if (lastPing && lastPing.success) {
            card2StatusBody = `
                <div style="font-size: 12px; color: #34d399; margin-top: 10px; line-height: 1.6;">
                    🟢 <b>端云协同就绪！</b> 延迟: <strong>${lastPing.latency_ms}ms</strong>
                    ${lastPing.device ? ` · 远端显卡: <b style="color:#38bdf8;">${escapeHtml(lastPing.device)}</b>` : ""}，本地 0 显存畅跑 1080P！
                    <button type="button" class="btn btn-xs btn-ghost" onclick="renderWizardGpuStatusCard(true)" style="margin-left: 8px; font-size: 11px; padding: 1px 6px; color: #38bdf8;">🔄 重新测速</button>
                </div>
            `;
        } else {
            const err = (lastPing && lastPing.error) ? lastPing.error : "云端算力节点未响应握手 (已关机或断开)";
            card2StatusBody = `
                <div style="margin-top: 10px; padding: 8px 10px; background: rgba(239, 68, 68, 0.15); border-radius: 6px; font-size: 11.5px; color: #fca5a5;">
                    <div>${escapeHtml(err)}</div>

                    <div style="display: flex; gap: 8px; margin-top: 6px; flex-wrap: wrap; align-items: center;margin-top:6px;">
                        <button type="button" class="btn btn-xs btn-secondary" onclick="renderWizardGpuStatusCard(true)" style="font-size: 11px; padding: 2px 8px;">
                            🔄 重新检测
                        </button>
                        <button type="button" class="btn btn-xs btn-ghost" onclick="goToGpuSettingsTab()" style="font-size: 11px; color: #38bdf8; border: 1px solid rgba(56,189,248,0.3); padding: 2px 8px;">
                            跳转到 GPU配置(2) ↗
                        </button>
                    </div>
                </div>
            `;
        }
    } else {
        if (!targetCloud || !targetCloud.base_url) {
            card2StatusBody = `
                <div style="margin-top: 10px; font-size: 12px; color: var(--text-muted); display: flex; justify-content: space-between; align-items: center;">
                    <span>💡 尚未配置云端 GPU 渲染节点</span>
                    <button type="button" class="btn btn-xs btn-ghost" onclick="goToGpuSettingsTab()" style="font-size: 11.5px; color: #38bdf8; border: 1px solid rgba(56,189,248,0.3);">前往「GPU配置(2)」录入 ↗</button>
                </div>
            `;
        } else {
            card2StatusBody = `
                <div style="margin-top: 10px; display: flex; justify-content: space-between; align-items: center;">
                    <span style="font-size: 12px; color: #cbd5e1;">已保存节点: <code style="color:#38bdf8;">${escapeHtml(targetCloud.base_url)}</code></span>
                    <button type="button" class="btn btn-xs btn-ghost" onclick="goToGpuSettingsTab()" style="font-size: 11.5px; color: #38bdf8; border: 1px solid rgba(56,189,248,0.3);">前往「GPU配置(2)」启用此模式 ↗</button>
                </div>
            `;
        }
    }

    container.innerHTML = `
        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px;">
            <!-- 选项 1：本机完全满足 (本地运行) -->
            <div class="wizard-gpu-mode-card ${card1Selected ? 'active-mode' : 'inactive-mode'}"
                 style="background: ${card1Bg}; border: ${card1Border}; border-radius: 10px; padding: 18px; position: relative; cursor: default; transition: all 0.2s ease;">
                <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8px;">
                    <div style="display: flex; align-items: center; gap: 10px;">
                        <div style="width: 34px; height: 34px; border-radius: 8px; background: rgba(16, 185, 129, 0.15); display: flex; align-items: center; justify-content: center;">
                            <svg viewBox="0 0 24 24" style="width: 18px; height: 18px; stroke: #10B981; fill: none; stroke-width: 2;"><rect x="2" y="3" width="20" height="14" rx="2" ry="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
                        </div>
                        <div>
                            <div style="font-size: 14.5px; font-weight: 700; color: #f8fafc;">选项 1 · 本机运行 (本地闭环)</div>
                            <div style="font-size: 12px; color: #34d399; margin-top: 2px; font-weight: 500;">
                                ${card1SubText}
                            </div>
                        </div>
                    </div>
                    ${card1Badge}
                </div>
                <div style="font-size: 12.5px; color: #94a3b8; line-height: 1.6; margin-top: 8px;">
                    全链路在当前电脑单机闭环运行。本地独显直跑或选用系统免显卡程序化形象，0 算力支出，断网亦可推流。
                </div>
                ${card1Action}
            </div>

            <!-- 选项 2：本地硬件 + 租赁云端GPU -->
            <div class="wizard-gpu-mode-card ${card2Selected ? 'active-mode' : 'inactive-mode'}"
                 style="background: ${card2Bg}; border: ${card2Border}; border-radius: 10px; padding: 18px; position: relative; cursor: default; transition: all 0.2s ease;">
                <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8px;">
                    <div style="display: flex; align-items: center; gap: 10px;">
                        <div style="width: 34px; height: 34px; border-radius: 8px; background: rgba(56, 189, 248, 0.15); display: flex; align-items: center; justify-content: center;">
                            <svg viewBox="0 0 24 24" style="width: 18px; height: 18px; stroke: #38bdf8; fill: none; stroke-width: 2;"><path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z"/></svg>
                        </div>
                        <div>
                            <div style="font-size: 14.5px; font-weight: 700; color: #f8fafc;">选项 2 · 本地硬件 + 租赁云端GPU</div>
                            <div style="font-size: 12px; color: #38bdf8; margin-top: 2px; font-weight: 500;">
                                本地 CPU ${cpuCores}核，本地GPU ${localVram}G+云端${cloudVram}G
                            </div>
                        </div>
                    </div>
                    ${card2Badge}
                </div>
                <div style="font-size: 12.5px; color: #94a3b8; line-height: 1.6; margin-top: 8px;">
                    本地仅负责交互调度，数字人 1080P 超写实真人渲染外包给云端 GPU，本地 0 显存开销。
                </div>
                ${card2StatusBody}
            </div>
        </div>
    `;
}
window.renderWizardGpuStatusCard = renderWizardGpuStatusCard;

// ------------------------------------------------------------------------------
// 10. 数据拉取与主入口初始化
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
        renderWizardGpuStatusCard(false);

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

// ------------------------------------------------------------------------------
// 11. 全局向导加载入口：供导航切至 wizard 时自动拉取并真实探测 GPU 状态
// ------------------------------------------------------------------------------
async function loadWizardAvatarProviders() {
    try {
        const configsRes = await fetch(`${API_BASE}/settings/configs`);
        if (configsRes.ok) {
            const configsJson = await configsRes.json();
            cachedAvatarConfigs = (configsJson.data || []).filter(item => item.config_group === "neural_renderer");
        }
    } catch (e) {
        console.warn("加载向导 GPU 配置数据失败:", e);
    }
    // 强制触发一次真实连通性握手探测，确保向导呈现真实状态
    await renderWizardGpuStatusCard(true);
}
window.loadWizardAvatarProviders = loadWizardAvatarProviders;
