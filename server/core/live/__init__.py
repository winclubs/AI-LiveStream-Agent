# -*- coding: utf-8 -*-
"""直播会话控制器的关注点切分（由 server/routes/live.py 的 LiveSessionController 拆出）。

每个 mixin 负责一个关注点，``LiveSessionController`` 仅作组合壳。
引用「缝」时一律写在**函数体内**，从 ``server.routes.live`` 解析 ——
顶层 import 会把缝变成早绑定，使 patch 静默失效。
守卫见 server/tests/test_live_seam_late_binding.py。
"""
