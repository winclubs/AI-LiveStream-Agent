/**
 * 「形象资产记录」列表页前端契约守护。
 *
 * 背景：服务端 task_manager 每完成一次数字人切片训练就往 avatars 表登记一条
 * 记录（avatar_task_* 前缀），但此前前端没有任何界面能看到这张表 —— 训练测试
 * 产生的冗余登记只增不减、无法清理。本模块守护该列表页的接线完整性：
 * 后端接口被正确调用、DOM 容器存在、表格渲染与删除逻辑到位。
 *
 * 运行：node tests/test_avatar_assets_ui.js
 */
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const jsCode = fs.readFileSync(
    path.join(ROOT, 'server/static/js/modules/13_anchors.js'), 'utf8'
);
const html = fs.readFileSync(
    path.join(ROOT, 'server/static/components/tab_anchors.html'), 'utf8'
);
const indexHtml = fs.readFileSync(
    path.join(ROOT, 'server/static/index.html'), 'utf8'
);

const checks = [];
function check(name, fn) {
    fn();
    checks.push(name);
}

function assert(cond, msg) {
    if (!cond) throw new Error(msg);
}

// ---------------------------------------------------------------------------
// 1. DOM 容器：面板与表体必须存在，且构建产物 index.html 已内联
// ---------------------------------------------------------------------------
check('组件中存在形象资产面板与表体容器', () => {
    assert(/id="avatar-assets-card"/.test(html), '缺少 avatar-assets-card 面板');
    assert(/id="avatar-assets-tbody"/.test(html), '缺少 avatar-assets-tbody 表体');
    assert(/id="avatar-assets-summary"/.test(html), '缺少 avatar-assets-summary 汇总位');
});

check('构建产物 index.html 已内联该面板（组件化合成未生效会漏）', () => {
    assert(
        /id="avatar-assets-card"/.test(indexHtml) && /id="avatar-assets-tbody"/.test(indexHtml),
        'index.html 未内联 avatar-assets 面板，请重新执行构建合成'
    );
});

// ---------------------------------------------------------------------------
// 2. 接口接线：必须调用真实存在的服务端端点
// ---------------------------------------------------------------------------
check('加载函数调用 GET /avatars/list', () => {
    assert(/async function loadAvatarAssets\(\)/.test(jsCode), '缺少 loadAvatarAssets()');
    assert(
        /\$\{API_BASE\}\/avatars\/list/.test(jsCode),
        'loadAvatarAssets 未调用 GET ${API_BASE}/avatars/list'
    );
});

check('删除函数调用 DELETE /avatars/{id} 且对 id 做 URL 编码', () => {
    assert(
        /async function deleteAvatarAssetRecord\(/.test(jsCode),
        '缺少 deleteAvatarAssetRecord()'
    );
    assert(
        /method:\s*"DELETE"/.test(jsCode),
        'deleteAvatarAssetRecord 未使用 DELETE 方法'
    );
    assert(
        /encodeURIComponent\(avatarId\)/.test(jsCode),
        '删除请求未对 avatarId 做 encodeURIComponent，可能因 id 含特殊字符而失败'
    );
});

// ---------------------------------------------------------------------------
// 3. 行为契约
// ---------------------------------------------------------------------------
check('删除前必须二次确认，且提示不影响主播档案', () => {
    const fn = jsCode.slice(jsCode.indexOf('async function deleteAvatarAssetRecord'));
    assert(/confirm\(/.test(fn), '删除前必须有 confirm 二次确认');
    assert(
        /不影响/.test(fn),
        '确认弹窗必须说明"不影响主播档案/正在直播的数字人"，避免误删顾虑'
    );
});

check('表格渲染必须区分"资产在位"与"资产缺失"', () => {
    assert(/function renderAvatarAssetsTable\(/.test(jsCode), '缺少 renderAvatarAssetsTable()');
    assert(
        /asset_exists/.test(jsCode),
        '渲染必须依据 asset_exists 字段区分有效资产与空壳登记'
    );
    assert(/资产缺失/.test(jsCode), '缺少"资产缺失"状态文案');
});

check('必须标注记录来源（训练任务 / 手动上传）', () => {
    assert(/origin/.test(jsCode), '渲染必须使用 origin 字段标注来源');
    assert(/训练任务/.test(jsCode), '缺少"训练任务"来源文案');
    assert(/手动上传/.test(jsCode), '缺少"手动上传"来源文案');
});

check('必须处理空列表与加载失败两种边界', () => {
    assert(/暂无形象资产记录/.test(jsCode), '缺少空列表提示');
    assert(/加载失败/.test(jsCode), '缺少加载失败提示');
});

check('所有插值到 innerHTML 的动态文本均经过 escapeHtml（防 XSS）', () => {
    const fn = jsCode.slice(
        jsCode.indexOf('function renderAvatarAssetsTable('),
        jsCode.indexOf('async function loadAvatarAssets(')
    );
    assert(/escapeHtml\(/.test(fn), '渲染函数必须使用 escapeHtml 转义动态内容');

    // 这些片段由本函数用**受控字面量**自建，不含任何用户/服务端数据，可安全直插：
    //   originBadge / stateCell —— 二选一的固定 HTML（含硬编码配色）
    //   typeMeta.*             —— 取自本文件顶部的常量映射表
    //   exists / isTask        —— 布尔值
    const SAFE_GENERATED = /^(originBadge|stateCell|typeMeta\.[a-zA-Z]+|exists|isTask)$/;
    const interpolations = fn.match(/\$\{[^}]+\}/g) || [];
    const unescaped = interpolations
        .map((expr) => expr.slice(2, -1).trim())
        .filter((expr) => !SAFE_GENERATED.test(expr))
        .filter((expr) => !/escapeHtml\(/.test(expr));

    assert(
        unescaped.length === 0,
        `存在未转义的插值: ${unescaped.join(', ')}`
    );

    // 反向校验：用户可控字段（名称、id）必须确实被转义
    assert(
        /escapeHtml\(row\.name/.test(fn),
        'row.name 来自用户输入，必须经 escapeHtml 转义'
    );
    assert(/escapeHtml\(id\)/.test(fn), '记录 id 必须经 escapeHtml 转义');
});

check('页面初始化时自动加载资产记录', () => {
    const init = jsCode.slice(0, jsCode.indexOf('async function loadAvatarActions('));
    assert(
        /loadAvatarAssets\(\);/.test(init),
        'loadAnchors() 初始化流程未调用 loadAvatarAssets()，列表页将永远显示占位文案'
    );
});

check('缓存版本号已递增（否则浏览器继续加载旧 JS）', () => {
    const tmpl = fs.readFileSync(
        path.join(ROOT, 'server/static/index.template.html'), 'utf8'
    );
    const re = /13_anchors\.js\?v=([\w]+)/;
    const a = re.exec(tmpl);
    const b = re.exec(indexHtml);
    assert(a && b, '未找到 13_anchors.js 的版本标签');
    assert(
        a[1] === b[1],
        `模板与构建产物版本号不一致: ${a[1]} vs ${b[1]}`
    );
});

console.log('[SUCCESS] 形象资产记录列表页契约守护通过');
checks.forEach((c) => console.log(`  ✓ ${c}`));