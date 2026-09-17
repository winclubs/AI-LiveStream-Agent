// 验证 resolveTTSVoiceInfo 在各种常见发音音色下的解析效果
const fs = require('fs');
const path = require('path');

// 读取 10_settings_tts.js 内容并提取 resolveTTSVoiceInfo
const code = fs.readFileSync(path.join(__dirname, '../server/static/js/modules/10_settings_tts.js'), 'utf8');

// 简单 eval 提取环境
const sandbox = {
    window: {},
    cachedAllConfigs: [],
    API_BASE: "/api/v1",
    resolveTTSProviderMeta: () => ({ recommendedVoices: [] }),
    showToast: () => {},
    document: {
        getElementById: () => null,
        querySelectorAll: () => [],
        querySelector: () => null
    }
};

const fn = new Function('sandbox', `
    with (sandbox) {
        ${code}
        return { resolveTTSVoiceInfo, TTS_VOICE_FRIENDLY_NAMES };
    }
`);

const { resolveTTSVoiceInfo } = fn(sandbox);

// 1. 测试 Edge-TTS 经典音色
const edgeXiaoxiao = resolveTTSVoiceInfo({ model_name: "zh-CN-XiaoxiaoNeural", provider_name: "edge_tts" });
console.log("Edge Xiaoxiao ->", edgeXiaoxiao);
if (!edgeXiaoxiao.name.includes("晓晓") || edgeXiaoxiao.name.includes("zh-CN")) {
    throw new Error("Edge Xiaoxiao 解析失败: " + JSON.stringify(edgeXiaoxiao));
}

const edgeYunxi = resolveTTSVoiceInfo({ model_name: "zh-CN-YunxiNeural", provider_name: "edge_tts" });
console.log("Edge Yunxi ->", edgeYunxi);
if (!edgeYunxi.name.includes("云希") || edgeYunxi.name.includes("zh-CN")) {
    throw new Error("Edge Yunxi 解析失败: " + JSON.stringify(edgeYunxi));
}

// 2. 测试 CosyVoice 官方音色
const cosyChun = resolveTTSVoiceInfo({ model_name: "longxiaochun", provider_name: "cosyvoice" });
console.log("Cosy Chun ->", cosyChun);
if (!cosyChun.name.includes("龙小春")) {
    throw new Error("Cosy Chun 解析失败: " + JSON.stringify(cosyChun));
}

// 3. 测试带 extra_params 自定义名称
const customNamed = resolveTTSVoiceInfo({
    model_name: "zh-CN-XiaoxiaoNeural",
    provider_name: "edge_tts",
    extra_params: JSON.stringify({ voice_name: "艾米专属知性音" })
});
console.log("Custom Named ->", customNamed);
if (customNamed.name !== "艾米专属知性音") {
    throw new Error("Custom Named 解析失败: " + JSON.stringify(customNamed));
}

// 4. 测试与 voiceProfiles 缓存匹配
const mockProfiles = [
    { id: "cosy-custom-clone-99", name: "小琴琴温柔声", voice_type: "cloned" }
];
const cloneMatch = resolveTTSVoiceInfo({
    model_name: "cosy-custom-clone-99",
    provider_name: "cosyvoice"
}, mockProfiles);
console.log("Clone Match ->", cloneMatch);
if (cloneMatch.name !== "小琴琴温柔声" || !cloneMatch.isClone) {
    throw new Error("Clone Match 解析失败: " + JSON.stringify(cloneMatch));
}

console.log("\n[SUCCESS] resolveTTSVoiceInfo 所有测试用例 100% 通过！绝无机器 ID 裸露！");
