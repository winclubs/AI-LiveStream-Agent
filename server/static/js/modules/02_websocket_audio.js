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

