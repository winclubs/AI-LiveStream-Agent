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

