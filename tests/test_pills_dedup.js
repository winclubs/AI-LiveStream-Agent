const fs = require('fs');
const path = require('path');

const code = fs.readFileSync(path.join(__dirname, '../server/static/js/modules/10_settings_tts.js'), 'utf8');

// 模拟 DOM 容器
let renderedHtml = "";
const container = {
    set innerHTML(val) { renderedHtml = val; },
    get innerHTML() { return renderedHtml; }
};
const countStatusEl = { innerHTML: "" };
const voiceInput = { value: "" };

// 模拟数据库已有的 22 条数据（2 条克隆，1 条系统默认，19 条官方预设）
const mockDbVoices = [
    { id: "voice_default_female", name: "通用播报女声", provider_name: "edge_tts", voice_type: "preset" },
    { id: "qwen-audio-3.0-tts-plus-bailian-92e7e7c666354b4ca5264ba9178e2d98", name: "小琴琴", provider_name: "cosyvoice", voice_type: "cloned" },
    { id: "cosyvoice-v3.5-flash-cloned-b4d8abc6048a41aba20e01a8181ecf92", name: "小琴二", provider_name: "cosyvoice", voice_type: "cloned" },
    { id: "longxiaochun", name: "龙小春", provider_name: "cosyvoice", voice_type: "preset" },
    { id: "longxiaobai", name: "龙小白", provider_name: "cosyvoice", voice_type: "preset" },
    { id: "longxiaoxia", name: "龙小夏", provider_name: "cosyvoice", voice_type: "preset" },
    { id: "longxiaocheng", name: "龙小诚", provider_name: "cosyvoice", voice_type: "preset" },
    { id: "longlaotie", name: "龙老铁", provider_name: "cosyvoice", voice_type: "preset" },
    { id: "longwan", name: "龙婉", provider_name: "cosyvoice", voice_type: "preset" },
    { id: "longshu", name: "龙书", provider_name: "cosyvoice", voice_type: "preset" },
    { id: "longyue", name: "龙悦", provider_name: "cosyvoice", voice_type: "preset" },
    { id: "longanchong", name: "龙安沛", provider_name: "cosyvoice", voice_type: "preset" },
    { id: "longanran", name: "龙安然", provider_name: "cosyvoice", voice_type: "preset" },
    { id: "longanxuan", name: "龙安萱", provider_name: "cosyvoice", voice_type: "preset" },
    { id: "longanping", name: "龙安平", provider_name: "cosyvoice", voice_type: "preset" },
    { id: "longshuo", name: "龙硕", provider_name: "cosyvoice", voice_type: "preset" },
    { id: "longjielidou", name: "杰力豆", provider_name: "cosyvoice", voice_type: "preset" },
    { id: "longcheng", name: "龙橙", provider_name: "cosyvoice", voice_type: "preset" },
    { id: "longhua", name: "龙华", provider_name: "cosyvoice", voice_type: "preset" },
    { id: "longjing", name: "龙静", provider_name: "cosyvoice", voice_type: "preset" },
    { id: "loongstella", name: "Stella", provider_name: "cosyvoice", voice_type: "preset" },
    { id: "loongbella", name: "Bella", provider_name: "cosyvoice", voice_type: "preset" }
];

// 模拟 cosyvoice 的 meta.recommendedVoices (19 款官方音色)
const cosyMeta = {
    id: "cosyvoice",
    name: "CosyVoice",
    recommendedVoices: [
        { val: "longxiaochun", text: "龙小春 (知性女声 · 电商带货推荐)" },
        { val: "longxiaobai", text: "龙小白 (萌软邻家 · 治愈少女)" },
        { val: "longxiaoxia", text: "龙小夏 (元气少女 · 活泼助手)" },
        { val: "longxiaocheng", text: "龙小诚 (阳光男声 · 亲和专业)" },
        { val: "longlaotie", text: "龙老铁 (东北老铁 · 互动爆款)" },
        { val: "longwan", text: "龙婉 (温和对话 · 亲切邻家)" },
        { val: "longshu", text: "龙书 (知性故事 · 情感故事)" },
        { val: "longyue", text: "龙悦 (优雅舒缓 · 品质解说)" },
        { val: "longanchong", text: "龙安沛 (活力带货 · 食品零食)" },
        { val: "longanran", text: "龙安然 (甜播带货 · 美妆美甲)" },
        { val: "longanxuan", text: "龙安萱 (亲和带货 · 美妆日用)" },
        { val: "longanping", text: "龙安平 (科技时尚 · 数码家电)" },
        { val: "longshuo", text: "龙硕 (沉稳男声 · 品牌带货)" },
        { val: "longjielidou", text: "杰力豆 (活泼童声 · 母婴玩具)" },
        { val: "longcheng", text: "龙橙 (阳光朝气 · 青春男声)" },
        { val: "longhua", text: "龙华 (稳重商务 · 商务男声)" },
        { val: "longjing", text: "龙静 (文雅解说 · 舒缓女声)" },
        { val: "loongstella", text: "Stella (自然朗读 · 品质女主播)" },
        { val: "loongbella", text: "Bella (温柔知性 · 服饰带货)" }
    ]
};

const sandbox = {
    window: {},
    cachedAllConfigs: [],
    API_BASE: "/api/v1",
    resolveTTSProviderMeta: () => cosyMeta,
    escapeHtml: (s) => s,
    showToast: () => {},
    document: {
        getElementById: (id) => {
            if (id === "tts-fetched-voices-container") return container;
            if (id === "tts-voices-count-status") return countStatusEl;
            if (id === "tts-input-voice") return voiceInput;
            return null;
        },
        querySelectorAll: () => [],
        querySelector: () => null
    },
    fetch: async () => ({
        json: async () => ({ code: 0, data: mockDbVoices })
    })
};

const fn = new Function('sandbox', `
    with (sandbox) {
        ${code}
        return { fetchAndRenderTTSVoices };
    }
`);

const { fetchAndRenderTTSVoices } = fn(sandbox);

(async () => {
    const res = await fetchAndRenderTTSVoices(cosyMeta);
    console.log("克隆音色列表 (clonedVoices):", res.clonedVoices.map(v => v.name));
    console.log("官方推荐音色列表 (recommendedVoices 数量):", res.recommendedVoices.length);

    // 1. 验证克隆音色列表绝不能包含官方预设（如龙小春等）
    if (res.clonedVoices.length !== 2) {
        throw new Error(`克隆音色数量错误，应为 2 (小琴琴、小琴二)，实际为 ${res.clonedVoices.length}`);
    }
    const cloneNames = res.clonedVoices.map(v => v.name);
    if (!cloneNames.includes("小琴琴") || !cloneNames.includes("小琴二")) {
        throw new Error("克隆音色缺少小琴琴或小琴二");
    }

    // 2. 验证推荐音色数量为 19
    if (res.recommendedVoices.length !== 19) {
        throw new Error(`官方推荐音色数量错误，应为 19，实际为 ${res.recommendedVoices.length}`);
    }

    // 3. 验证渲染出的 HTML 药丸 ID 是否全局唯一
    const renderedIds = [];
    const matches = renderedHtml.matchAll(/data-voice="([^"]+)"/g);
    for (const m of matches) {
        renderedIds.push(m[1]);
    }
    console.log(`渲染药丸总数: ${renderedIds.length}`);
    if (renderedIds.length !== 21) {
        throw new Error(`渲染药丸总数必须为 21 (2 + 19)，实际为 ${renderedIds.length}`);
    }

    const idSet = new Set(renderedIds);
    if (idSet.size !== renderedIds.length) {
        throw new Error(`存在重复渲染的音色 ID！集合大小: ${idSet.size}，数组长度: ${renderedIds.length}`);
    }

    console.log("\n[SUCCESS] 药丸渲染防重测试 100% 通过！克隆 2 款，官方 19 款，总共 21 款，绝无重复！");
})();
