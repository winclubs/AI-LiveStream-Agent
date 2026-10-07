/**
 * 试播「试听台词驱动」音画同步前导补偿回归守护。
 *
 * 背景：逐帧播放通道（方案 B）与硬解视频伴随监视通道（方案 A）都用 rAF 按
 * `audio.currentTime` 推算应显示的帧序号。历史实现硬编码 `LEAD_SEC = 0.04`，
 * 未计入 img.src 惰性解码 + 合成上屏延迟（@60Hz 约 16~50ms），名义前导被管线
 * 吃掉后净偏移不可控，表现为用户感知的「音画同步略有瑕疵」。
 *
 * 方向纪律（ITU-R BT.1359-1）：正值 = 声音超前于画面。可检出阈值 +45 / −125ms，
 * 音频超前比滞后刺眼约 2.8 倍；现实中光快于声，观众习惯先看到嘴动后听到声，
 * 故**口型略微领先声音**才是安全方向。前导量必须为正且落在安全区内。
 *
 * 运行：node tests/test_preview_av_sync.js
 */
const fs = require('fs');
const path = require('path');

const code = fs.readFileSync(
    path.join(__dirname, '../server/static/js/modules/13_anchors.js'), 'utf8'
);

// ---------------------------------------------------------------------------
// 1. 提取音画同步前导常量
// ---------------------------------------------------------------------------
const leadMatch = code.match(/const\s+PREVIEW_LEAD_SEC\s*=\s*([\d.]+)/);
if (!leadMatch) {
    throw new Error("13_anchors.js 未定义 PREVIEW_LEAD_SEC 音画同步前导常量");
}
const PREVIEW_LEAD_SEC = parseFloat(leadMatch[1]);

// ---------------------------------------------------------------------------
// 2. 提取纯函数帧索引计算（无 DOM 依赖，可独立求值）
// ---------------------------------------------------------------------------
const fnMatch = code.match(
    /function\s+previewSpeechFrameIndex\s*\([^)]*\)\s*\{[^}]*\}/
);
if (!fnMatch) {
    throw new Error("13_anchors.js 未定义 previewSpeechFrameIndex 纯函数");
}
const previewSpeechFrameIndex = new Function(
    `${fnMatch[0]}\nreturn previewSpeechFrameIndex;`
)();

// ---------------------------------------------------------------------------
// 3. 断言
// ---------------------------------------------------------------------------
const checks = [];
function check(name, fn) {
    fn();
    checks.push(name);
}

// 3.1 前导量必须为正：口型领先声音是「光快于声」的自然安全方向
check("前导量为正（口型领先声音）", () => {
    if (!(PREVIEW_LEAD_SEC > 0)) {
        throw new Error(`前导量必须为正，实际 ${PREVIEW_LEAD_SEC}（负值会让口型滞后声音，落在最敏感方向）`);
    }
});

// 3.2 前导量必须落在 ITU-R BT.1359-1 安全区内
//     扣除 16~50ms 的解码+合成上屏延迟后，净口型前导须 > 0（方向正确）
//     且明显小于可检出阈值 125ms，避免被观众察觉
check("前导量覆盖上屏延迟且留有安全余量", () => {
    const MIN_NEED = 0.050;   // 最坏情况 3 帧 @60Hz 的合成延迟
    const MAX_SAFE = 0.120;   // 小于 BT.1359-1 的 −125ms 可检出阈值
    if (PREVIEW_LEAD_SEC < MIN_NEED) {
        throw new Error(
            `前导量 ${PREVIEW_LEAD_SEC}s 不足以对冲解码+合成延迟（需 ≥ ${MIN_NEED}s），` +
            `净口型会反转为滞后声音`
        );
    }
    if (PREVIEW_LEAD_SEC > MAX_SAFE) {
        throw new Error(
            `前导量 ${PREVIEW_LEAD_SEC}s 超过 BT.1359-1 可检出阈值 ${MAX_SAFE}s，会被察觉`
        );
    }
});

// 3.3 帧索引相对音频 currentTime 领先
check("帧索引领先音频时钟", () => {
    // t=1.0s, fps=25, lead=0.06 -> floor(1.06*25) = floor(26.5) = 26
    const idx = previewSpeechFrameIndex(1.0, 25, PREVIEW_LEAD_SEC, 100);
    if (idx !== 26) {
        throw new Error(`t=1.0s 应取帧 26（领先 40ms），实际 ${idx}`);
    }
});

// 3.4 片尾钳制到最后一帧，绝不越界
check("片尾钳制到最后一帧", () => {
    const idx = previewSpeechFrameIndex(99.0, 25, PREVIEW_LEAD_SEC, 5);
    if (idx !== 5) {
        throw new Error(`越过片尾时必须钳制到 5，实际 ${idx}`);
    }
});

// 3.5 起始前钳制到 0 帧
check("起始前钳制到 0 帧", () => {
    const idx = previewSpeechFrameIndex(-5.0, 25, PREVIEW_LEAD_SEC, 5);
    if (idx !== 0) {
        throw new Error(`负时间必须钳制到 0，实际 ${idx}`);
    }
});

// 3.6 单帧资产恒为 0（不产生越界）
check("单帧资产恒为 0", () => {
    const idx = previewSpeechFrameIndex(3.0, 25, PREVIEW_LEAD_SEC, 0);
    if (idx !== 0) {
        throw new Error(`maxIdx=0 时必须恒为 0，实际 ${idx}`);
    }
});

// 3.7 两个播放通道都必须使用该纯函数（防止只改一处导致两通道不同步）
check("方案 A/B 两个通道共用同一前导常量", () => {
    const uses = (code.match(/previewSpeechFrameIndex\s*\(/g) || []).length;
    // 1 次定义 + 2 处调用（syncFace 与 flip）
    if (uses < 3) {
        throw new Error(
            `previewSpeechFrameIndex 应至少出现 3 次（1 定义 + syncFace + flip），实际 ${uses} 次；` +
            `方案 A/B 可能仍有通道未统一`
        );
    }
    if (code.includes("LEAD_SEC = 0.04")) {
        throw new Error("仍存在历史硬编码 LEAD_SEC = 0.04，必须收敛到 PREVIEW_LEAD_SEC");
    }
});

console.log(`[SUCCESS] 试播音画同步前导守护通过：PREVIEW_LEAD_SEC=${PREVIEW_LEAD_SEC}s`);
checks.forEach((c) => console.log(`  ✓ ${c}`));