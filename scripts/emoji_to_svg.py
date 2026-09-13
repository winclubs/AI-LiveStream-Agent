"""一次性维护脚本：console.js 动态渲染点 emoji → SVG/纯文本"""
import io

P = "server/static/js/console.js"
s = io.open(P, encoding="utf-8").read()

REPL = [
    ('"⚠️ 直播进行中：直播模式与主播角色已锁定，如需更换请先停止直播。"',
     '"直播进行中：直播模式与主播角色已锁定，如需更换请先停止直播。"'),
    ('💡 【${d.label}】运营建议', '【${d.label}】运营建议'),
    ('>⚡ 立即促单逼单</button>', '>促单逼单</button>'),
    ('logDanmaku("⚡ 运营促单"', 'logDanmaku(`${svg("zap", "icon-sm")} 运营促单`'),
    ('>▶ 在线试听</button>', '>在线试听</button>'),
    ('>🎯 一键克隆</button>', '>一键克隆</button>'),
    ('可点击 ▶在线试听', '可点击「在线试听」'),
    ('gotoBtn.innerText = "🧭 重新配置模式/角色"',
     'gotoBtn.innerHTML = svg("compass", "icon-sm") + " 重新配置模式/角色"'),
    ('gotoBtn.innerText = "🧭 去完成开播向导"',
     'gotoBtn.innerHTML = svg("compass", "icon-sm") + " 去完成开播向导"'),
    ('💾 保存此项配置修改</button>', '保存此项配置修改</button>'),
    ('⚡ 连通性测试 (探测延迟)</button>', '连通性测试 · 探测延迟</button>'),
    ('⚠️ 直播模式列表加载失败', '直播模式列表加载失败'),
    ('>🔄 重试加载</button>', '>重新加载</button>'),
    ('"⚠ 有建议项，仍要开播"', '"仍有建议项，坚持开播"'),
    ('请先处理上方 ❌ 项再开播', '请先处理上方未通过项再开播'),
    ('🖥️ ${modeInfo.hardware} · 🧠 ${modeInfo.llm} · 🎙️ ${modeInfo.tts} · 💰 ${modeInfo.cost}',
     '${modeInfo.hardware} · ${modeInfo.llm} · ${modeInfo.tts} · ${modeInfo.cost}'),
    ('logDanmaku("💰 成交登记"', 'logDanmaku(`${svg("dollar", "icon-sm")} 成交登记`'),
    ('"✓ 配置已保存：直播模式与主播角色约束已生效！"', '"配置已保存：直播模式与主播角色约束已生效！"'),
]

count = 0
for old, new in REPL:
    if old in s:
        s = s.replace(old, new)
        count += 1
    else:
        print("NOT FOUND:", old[:48])

io.open(P, "w", encoding="utf-8").write(s)
print("replaced:", count, "/", len(REPL))
