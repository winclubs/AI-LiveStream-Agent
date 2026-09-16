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

        const obsCheck = document.getElementById("obs-auto-link-check");
        const obsAutoLink = obsCheck ? obsCheck.checked : false;

        const res = await fetch(`${API_BASE}/live/start`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ room_id: roomId, platform: platform, anchor_id: anchorId, obs_auto_link: obsAutoLink })
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
            logDanmaku("系统通知", `本地直播源已启动！${roomId ? `正在监听【${platform}】房间事件: ${roomId}` : '已启动仿真互动'}；[${obsText}]`, false);
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
