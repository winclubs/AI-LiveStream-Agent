// ==============================================================================
// 16_settings_oss.js - 阿里云 OSS 配置与直播录像复盘回放管理模块 (高质感 UI 交互版)
// ==============================================================================

// 页面加载或切换至 OSS 配置时初始化
async function initOssSettingsPage() {
    await loadOssConfig();
    await loadOssRecords();
    bindOssEvents();
}

// 1. 读取并渲染 OSS 配置
async function loadOssConfig() {
    try {
        const res = await fetch(`${API_BASE}/oss/config?t=${Date.now()}`);
        if (!res.ok) {
            updateOssStatusIndicator(false, "接口服务异常");
            return;
        }
        const json = await res.json();
        if (json.code !== 0 || !json.data) {
            updateOssStatusIndicator(false, "未配置存储");
            return;
        }
        const d = json.data;

        const epInput = document.getElementById("oss-endpoint");
        const bkInput = document.getElementById("oss-bucket");
        const akInput = document.getElementById("oss-ak-id");
        const skInput = document.getElementById("oss-ak-secret");
        const cdInput = document.getElementById("oss-custom-domain");
        const pfInput = document.getElementById("oss-prefix");
        const tgInput = document.getElementById("oss-auto-upload-toggle");

        if (epInput && d.endpoint) {
            epInput.value = d.endpoint;
            // 匹配药丸高亮
            document.querySelectorAll("#tab-oss .oss-pill").forEach(p => {
                p.classList.toggle("active", p.getAttribute("data-val") === d.endpoint);
            });
        }
        if (bkInput && d.bucket) bkInput.value = d.bucket;
        if (akInput && d.access_key_id) akInput.value = d.access_key_id;
        if (skInput && d.access_key_secret) skInput.value = d.access_key_secret;
        if (cdInput) cdInput.value = d.custom_domain || "";
        if (pfInput) pfInput.value = d.prefix || "recordings/";
        if (tgInput) tgInput.checked = Boolean(d.auto_upload);

        updateOssStatusIndicator(Boolean(d.configured), d.configured ? `已连接 (${d.bucket || "Bucket"})` : "未配置存储桶");
    } catch (err) {
        console.warn("加载 OSS 配置异常:", err);
        updateOssStatusIndicator(false, "网络连接异常");
    }
}

// 更新顶栏状态小圆点与文字
function updateOssStatusIndicator(isOnline, text) {
    const dot = document.getElementById("oss-status-dot");
    const label = document.getElementById("oss-status-label");
    if (dot) {
        dot.className = `status-dot ${isOnline ? "online" : ""}`;
        if (!isOnline) {
            dot.style.background = "var(--text-muted)";
            dot.style.boxShadow = "none";
        } else {
            dot.style.background = "var(--signal)";
            dot.style.boxShadow = "0 0 8px rgba(45, 212, 160, 0.8)";
        }
    }
    if (label) {
        label.textContent = text;
        label.style.color = isOnline ? "var(--signal)" : "var(--text-secondary)";
    }
}

// 2. 加载直播场次录像列表
async function loadOssRecords(page = 1) {
    const tbody = document.getElementById("oss-records-tbody");
    const countBadge = document.getElementById("oss-records-count-badge");
    if (!tbody) return;

    try {
        const res = await fetch(`${API_BASE}/oss/records?page=${page}&page_size=20&t=${Date.now()}`);
        if (!res.ok) {
            const errJson = await res.json().catch(() => ({}));
            const errMsg = errJson.detail || errJson.message || `HTTP ${res.status}`;
            renderRecordsError(tbody, `未能连接至录像归档服务: ${errMsg}`);
            return;
        }

        const json = await res.json();
        if (json.code !== 0 || !json.data) {
            renderRecordsError(tbody, json.message || "未能读取历史录像列表");
            return;
        }

        const items = json.data.items || [];
        const total = json.data.total ?? items.length;
        if (countBadge) {
            countBadge.textContent = `已记录 ${total} 场`;
        }

        if (items.length === 0) {
            renderRecordsEmpty(tbody);
            return;
        }

        let html = "";
        items.forEach(item => {
            const sidShort = (item.session_id || "").slice(-8);
            const durText = item.duration_formatted || `${item.duration_sec || 0}秒`;
            const sizeText = item.video_size_mb > 0 ? `${item.video_size_mb} MB` : (item.has_local_video ? "统计中" : "—");

            // 存储状态胶囊
            let statusBadge = "";
            if (item.oss_upload_status === "uploaded" || item.has_oss_video) {
                statusBadge = `<span class="oss-badge-capsule ready">● 云端 OSS 就绪</span>`;
            } else if (item.oss_upload_status === "uploading") {
                statusBadge = `<span class="oss-badge-capsule uploading">⏳ 正在同步云端...</span>`;
            } else if (item.oss_upload_status === "failed") {
                statusBadge = `<span class="oss-badge-capsule failed" title="${item.oss_error_msg || ''}">✕ 上传失败</span>`;
            } else if (item.has_local_video) {
                statusBadge = `<span class="oss-badge-capsule local">◒ 本地留存</span>`;
            } else {
                statusBadge = `<span class="oss-badge-capsule none">○ 无录像文件</span>`;
            }

            // 操作按钮组
            let opsHtml = "";
            if (item.playable) {
                opsHtml += `
                    <button type="button" class="btn btn-secondary btn-sm" onclick="openOssVideoModal('${item.session_id}', '${encodeURIComponent(item.theme || '直播复盘')}')" title="在线高清回放复盘">
                        <svg class="icon-sm" viewBox="0 0 24 24" style="stroke:var(--signal);"><polygon points="5 3 19 12 5 21 5 3"/></svg>
                        复盘
                    </button>
                    <button type="button" class="btn btn-secondary btn-sm" onclick="downloadOssVideo('${item.session_id}')" title="下载录像到本地">
                        <svg class="icon-sm" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                        下载
                    </button>
                `;
            }
            if (item.oss_upload_status === "failed" && item.has_local_video) {
                opsHtml += `
                    <button type="button" class="btn btn-secondary btn-sm" onclick="retryOssUpload('${item.session_id}')" style="color:var(--amber);" title="重新上传到 OSS">
                        <svg class="icon-sm" viewBox="0 0 24 24"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
                        重传
                    </button>
                `;
            }
            if (!opsHtml) {
                opsHtml = `<span style="color:var(--text-muted); font-size:12px;">未留存录像</span>`;
            }

            html += `
                <tr>
                    <td style="padding: 14px 18px;">
                        <div style="font-weight: 600; color: #f1f5f9; font-size: 13.5px; margin-bottom: 2px;">
                            ${escapeHtml(item.theme || "日常直播")}
                        </div>
                        <div style="font-size: 11.5px; color: var(--text-muted); font-family: var(--font-mono);">
                            #${sidShort} · <span style="text-transform: capitalize;">${item.platform || "bilibili"}</span>
                        </div>
                    </td>
                    <td style="padding: 14px 18px;">
                        <div style="color: var(--text-secondary); font-size: 12.5px; font-family: var(--font-mono);">
                            ${item.start_time || "—"}
                        </div>
                        <div style="font-size: 11.5px; color: #34d399; margin-top: 2px; font-family: var(--font-mono);">
                            时长: ${durText}
                        </div>
                    </td>
                    <td style="padding: 14px 18px;">
                        <div style="color: #f59e0b; font-weight: 700; font-family: var(--font-mono); font-size: 13.5px;">
                            ¥${item.total_gmv}
                        </div>
                        <div style="font-size: 11.5px; color: var(--text-muted); margin-top: 2px;">
                            已成单: ${item.orders_count} 单
                        </div>
                    </td>
                    <td style="padding: 14px 18px;">
                        <div style="color: var(--text-primary); font-size: 12.5px;">
                            弹幕: <span style="font-family: var(--font-mono); font-weight: 600;">${item.danmaku_count}</span>
                        </div>
                        <div style="font-size: 11.5px; color: var(--text-muted); margin-top: 2px;">
                            峰值: <span style="font-family: var(--font-mono);">${item.peak_viewers}</span> 人
                        </div>
                    </td>
                    <td style="padding: 14px 18px; color: var(--text-muted); font-family: var(--font-mono); font-size: 12.5px;">
                        ${sizeText}
                    </td>
                    <td style="padding: 14px 18px;">
                        ${statusBadge}
                    </td>
                    <td style="padding: 14px 18px; text-align: right;">
                        <div class="table-actions" style="justify-content: flex-end;">
                            ${opsHtml}
                        </div>
                    </td>
                </tr>
            `;
        });

        tbody.innerHTML = html;
    } catch (err) {
        console.error("加载直播场次列表失败:", err);
        renderRecordsError(tbody, `网络通讯异常，无法连接服务端 (${err.message})`);
    }
}

// 优雅的空状态展示
function renderRecordsEmpty(tbody) {
    tbody.innerHTML = `
        <tr>
            <td colspan="7" style="text-align: center; padding: 48px 24px;">
                <div style="max-width: 460px; margin: 0 auto; display: flex; flex-direction: column; align-items: center; gap: 12px;">
                    <div style="width: 52px; height: 52px; border-radius: 50%; background: rgba(148, 163, 184, 0.08); border: 1px solid rgba(148, 163, 184, 0.18); display: flex; align-items: center; justify-content: center; color: var(--text-muted);">
                        <svg class="icon-lg" viewBox="0 0 24 24" style="stroke: #94a3b8; width: 26px; height: 26px;">
                            <polygon points="23 7 16 12 23 17 23 7" />
                            <rect x="1" y="5" width="15" height="14" rx="2" ry="2" />
                        </svg>
                    </div>
                    <div style="font-size: 15px; font-weight: 600; color: var(--text-primary);">暂无历史直播录像</div>
                    <div style="font-size: 12.5px; color: var(--text-muted); line-height: 1.7;">
                        每次在【直播大屏】开播并下播后，系统将自动留存本场次的销售战绩指标与全量实况音视频，并同步归档至云端 OSS 供随时复盘回溯。
                    </div>
                    <button type="button" class="btn btn-secondary btn-sm" onclick="if(window.switchTab) window.switchTab('tab-live');" style="margin-top: 6px;">
                        前往【直播大屏】开始直播
                    </button>
                </div>
            </td>
        </tr>
    `;
}

// 优雅的异常状态展示 (带重试按钮)
function renderRecordsError(tbody, message) {
    tbody.innerHTML = `
        <tr>
            <td colspan="7" style="text-align: center; padding: 36px 20px;">
                <div style="display: inline-flex; flex-direction: column; align-items: center; gap: 10px; background: rgba(239, 68, 68, 0.08); border: 1px solid rgba(239, 68, 68, 0.25); border-radius: 8px; padding: 16px 28px;">
                    <div style="display: flex; align-items: center; gap: 8px; color: #f87171; font-size: 13.5px; font-weight: 600;">
                        <svg class="icon-sm" viewBox="0 0 24 24" style="stroke: #f87171;"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
                        <span>${escapeHtml(message)}</span>
                    </div>
                    <div style="font-size: 12px; color: var(--text-muted);">
                        若您刚刚更新代码，请确保后端服务已启动；点击下方按钮可快速重试。
                    </div>
                    <button type="button" class="btn btn-secondary btn-sm" onclick="loadOssRecords()" style="margin-top: 4px;">
                        重试加载
                    </button>
                </div>
            </td>
        </tr>
    `;
}

// 3. 事件绑定
function bindOssEvents() {
    // 快速填充 Endpoint 药丸标签
    document.querySelectorAll("#tab-oss .oss-pill[data-val]").forEach(tag => {
        tag.onclick = () => {
            const val = tag.getAttribute("data-val");
            const ep = document.getElementById("oss-endpoint");
            if (ep) {
                ep.value = val;
                document.querySelectorAll("#tab-oss .oss-pill").forEach(p => p.classList.remove("active"));
                tag.classList.add("active");
            }
        };
    });

    // 密钥可见性切换
    const toggleBtn = document.getElementById("btn-toggle-oss-secret");
    const secretInput = document.getElementById("oss-ak-secret");
    if (toggleBtn && secretInput) {
        toggleBtn.onclick = () => {
            const isPwd = secretInput.type === "password";
            secretInput.type = isPwd ? "text" : "password";
            toggleBtn.style.color = isPwd ? "var(--signal)" : "var(--text-muted)";
        };
    }

    // 连通性测试
    const btnTest = document.getElementById("btn-oss-test");
    const resBox = document.getElementById("oss-test-result-box");
    if (btnTest) {
        btnTest.onclick = async () => {
            const ep = document.getElementById("oss-endpoint")?.value.trim() || "";
            const bk = document.getElementById("oss-bucket")?.value.trim() || "";
            const ak = document.getElementById("oss-ak-id")?.value.trim() || "";
            const sk = document.getElementById("oss-ak-secret")?.value.trim() || "";

            if (!ep || !bk || !ak) {
                notifyUser("请先完整填写 Endpoint、Bucket 空间名和 AccessKey ID！", "warning");
                return;
            }

            btnTest.disabled = true;
            btnTest.innerHTML = `
                <svg class="icon-sm" viewBox="0 0 24 24" style="animation:spin 1s linear infinite;"><circle cx="12" cy="12" r="10" stroke-dasharray="32" stroke-dashoffset="12"/></svg>
                正在探测...
            `;
            if (resBox) resBox.style.display = "none";

            try {
                const res = await fetch(`${API_BASE}/oss/test`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ endpoint: ep, bucket: bk, access_key_id: ak, access_key_secret: sk }),
                });
                const json = await res.json();
                if (resBox) {
                    resBox.style.display = "block";
                    if (json.code === 0 && json.data?.connected) {
                        resBox.style.background = "rgba(16, 185, 129, 0.12)";
                        resBox.style.border = "1px solid rgba(16, 185, 129, 0.35)";
                        resBox.style.color = "#34d399";
                        resBox.innerHTML = `<strong>✓ 阿里云 OSS 握手成功：</strong>${escapeHtml(json.data.message)} (读写权限正常)`;
                        notifyUser("OSS 存储连通测试通过！", "success");
                    } else {
                        resBox.style.background = "rgba(239, 68, 68, 0.12)";
                        resBox.style.border = "1px solid rgba(239, 68, 68, 0.35)";
                        resBox.style.color = "#f87171";
                        resBox.innerHTML = `<strong>✕ 连通测试失败：</strong>${escapeHtml(json.message || "未能连接到指定的 Bucket，请核对 Endpoint 与密钥权限")}`;
                        notifyUser("OSS 连通失败，请检查配置", "error");
                    }
                }
            } catch (err) {
                if (resBox) {
                    resBox.style.display = "block";
                    resBox.style.background = "rgba(239, 68, 68, 0.12)";
                    resBox.style.border = "1px solid rgba(239, 68, 68, 0.35)";
                    resBox.style.color = "#f87171";
                    resBox.innerHTML = `<strong>✕ 探测请求超时：</strong>${escapeHtml(err.message)}`;
                }
                notifyUser(`探测异常: ${err.message}`, "error");
            } finally {
                btnTest.disabled = false;
                btnTest.innerHTML = `
                    <svg class="icon-sm" viewBox="0 0 24 24"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
                    连通性测试
                `;
            }
        };
    }

    // 保存配置
    const btnSave = document.getElementById("btn-oss-save");
    if (btnSave) {
        btnSave.onclick = async () => {
            const ep = document.getElementById("oss-endpoint")?.value.trim() || "";
            const bk = document.getElementById("oss-bucket")?.value.trim() || "";
            const ak = document.getElementById("oss-ak-id")?.value.trim() || "";
            const sk = document.getElementById("oss-ak-secret")?.value.trim() || "";
            const cd = document.getElementById("oss-custom-domain")?.value.trim() || "";
            const pf = document.getElementById("oss-prefix")?.value.trim() || "recordings/";
            const tg = document.getElementById("oss-auto-upload-toggle")?.checked ?? true;

            btnSave.disabled = true;
            btnSave.innerHTML = `
                <svg class="icon-sm" viewBox="0 0 24 24" style="animation:spin 1s linear infinite;"><circle cx="12" cy="12" r="10" stroke-dasharray="32" stroke-dashoffset="12"/></svg>
                正在保存...
            `;

            try {
                const res = await fetch(`${API_BASE}/oss/config`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        endpoint: ep,
                        bucket: bk,
                        access_key_id: ak,
                        access_key_secret: sk,
                        custom_domain: cd,
                        prefix: pf,
                        auto_upload: tg,
                        is_active: true,
                    }),
                });
                const json = await res.json();
                if (json.code === 0) {
                    notifyUser("阿里云 OSS 存储配置已成功持久化保存！", "success");
                    await loadOssConfig();
                } else {
                    notifyUser(`保存失败: ${json.message}`, "error");
                }
            } catch (err) {
                notifyUser(`保存请求异常: ${err.message}`, "error");
            } finally {
                btnSave.disabled = false;
                btnSave.innerHTML = `
                    <svg class="icon-sm" viewBox="0 0 24 24"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
                    保存配置
                `;
            }
        };
    }

    // 刷新场次列表
    const btnRefresh = document.getElementById("btn-refresh-oss-records");
    if (btnRefresh) {
        btnRefresh.onclick = () => {
            notifyUser("正在刷新录像列表...", "info");
            loadOssRecords();
        };
    }

    // 关闭模态框
    const btnCloseModal = document.getElementById("btn-close-oss-modal");
    const modal = document.getElementById("oss-video-modal");
    const videoPlayer = document.getElementById("oss-modal-video-player");
    if (btnCloseModal && modal) {
        btnCloseModal.onclick = () => {
            modal.style.display = "none";
            if (videoPlayer) {
                videoPlayer.pause();
                videoPlayer.src = "";
            }
        };
    }
}

// 4. 打开在线复盘视频播放器
async function openOssVideoModal(sessionId, themeEncoded) {
    const modal = document.getElementById("oss-video-modal");
    const titleEl = document.getElementById("oss-modal-title");
    const player = document.getElementById("oss-modal-video-player");
    const infoEl = document.getElementById("oss-modal-source-info");
    const downloadBtn = document.getElementById("oss-modal-download-btn");

    if (!modal || !player) return;

    const theme = decodeURIComponent(themeEncoded || "直播实况复盘");
    if (titleEl) titleEl.textContent = `【${theme}】直播实况高清复盘回放`;

    try {
        const res = await fetch(`${API_BASE}/oss/records/${sessionId}/play-url`);
        const json = await res.json();
        if (json.code !== 0 || !json.data?.play_url) {
            notifyUser(`获取视频回放源失败: ${json.message || "无可用视频"}`, "warning");
            return;
        }

        const playUrl = json.data.play_url;
        player.src = playUrl;
        if (infoEl) {
            infoEl.textContent = json.data.source === "oss" ? "视频源: 阿里云 OSS 专属加密防盗链直链" : "视频源: 本地 H.264+AAC 分片流";
        }
        if (downloadBtn) {
            downloadBtn.href = playUrl;
            downloadBtn.setAttribute("download", `live_record_${sessionId}.mp4`);
        }

        modal.style.display = "flex";
        player.play().catch(() => {});
    } catch (err) {
        notifyUser(`打开复盘视频失败: ${err.message}`, "error");
    }
}
window.openOssVideoModal = openOssVideoModal;

// 5. 下载视频
async function downloadOssVideo(sessionId) {
    try {
        const res = await fetch(`${API_BASE}/oss/records/${sessionId}/play-url`);
        const json = await res.json();
        if (json.code === 0 && json.data?.play_url) {
            const a = document.createElement("a");
            a.href = json.data.play_url;
            a.download = `live_record_${sessionId}.mp4`;
            a.target = "_blank";
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            notifyUser("已触发录像文件下载", "success");
        } else {
            notifyUser("下载失败: 录像文件未就绪", "warning");
        }
    } catch (err) {
        notifyUser(`下载请求失败: ${err.message}`, "error");
    }
}
window.downloadOssVideo = downloadOssVideo;

// 6. 重试上传
async function retryOssUpload(sessionId) {
    if (!confirm("确定重新将本场录像上传到阿里云 OSS 吗？")) return;
    try {
        const res = await fetch(`${API_BASE}/oss/records/${sessionId}/retry-upload`, { method: "POST" });
        const json = await res.json();
        notifyUser(json.message || "重传任务已派发", json.code === 0 ? "success" : "error");
        await loadOssRecords();
    } catch (err) {
        notifyUser(`重试派发失败: ${err.message}`, "error");
    }
}
window.retryOssUpload = retryOssUpload;

// 通用消息提醒 (优先使用系统级 showToast)
function notifyUser(msg, type = "info") {
    if (typeof showToast === "function") {
        showToast(msg, type);
    } else {
        alert(msg);
    }
}

// 辅助转义 HTML 防止 XSS
function escapeHtml(str) {
    if (!str) return "";
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

// 挂载到 window 供导航器调用
window.initOssSettingsPage = initOssSettingsPage;
