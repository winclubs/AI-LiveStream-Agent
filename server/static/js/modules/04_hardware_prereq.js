async function loadHardwareInfo() {
    try {
        const res = await fetch(`${API_BASE}/live/hardware`);
        const json = await res.json();
        if (json.code !== 0) return;
        const d = json.data;
        const gpu = d.gpu || {};

        const setText = (id, text) => { const el = document.getElementById(id); if (el) el.innerHTML = text; };

        // 显卡
        const cap = d.gpu_capability || {};
        if (gpu.gpu_name) {
            let statusSuffix = "";
            if (cap.use_cloud) {
                statusSuffix = ` · <span style="color: var(--accent-emerald); font-weight: 600;">⚡ 已优先调度云端显卡</span>`;
            } else if (cap.is_low_spec_local) {
                statusSuffix = ` · <span style="color: var(--accent-amber); font-weight: 600;">⚠️ 显存不足2G未配云端</span>`;
            }
            setText("hw-panel-gpu", gpu.gpu_name);
            setText("hw-panel-gpu-sub",
                `显存 ${gpu.vram_total_gb}GB${gpu.vram_total_gb > 0 ? ` · 已用 ${gpu.vram_used_gb}GB` : ""} · ${gpu.cuda_available ? '<span style="color: var(--accent-emerald);">CUDA 可用</span>' : '<span style="color: var(--accent-amber);">无 CUDA</span>'}${statusSuffix}`);
        } else {
            const cloudBadge = cap.use_cloud ? ' · <span style="color: var(--accent-emerald); font-weight: 600;">⚡ 已优先调度云端显卡</span>' : ' · <span style="color: var(--accent-amber);">⚠️ 未配置云端显卡</span>';
            setText("hw-panel-gpu", '<span style="color: var(--text-muted);">核显 / 未检测到独显</span>');
            setText("hw-panel-gpu-sub", `将使用轻量方案${cloudBadge}`);
        }

        // 处理器
        const cpuName = d.cpu_name || "未知处理器";
        const cpuShort = cpuName.length > 36 ? cpuName.slice(0, 36) + "..." : cpuName;
        setText("hw-panel-cpu", cpuShort);
        setText("hw-panel-cpu-sub",
            `实时占用 <strong style="color: ${d.cpu_percent > 85 ? "var(--accent-danger)" : "var(--accent-emerald)"};">${Math.round(d.cpu_percent)}%</strong> · ${d.cpu_cores || 0} 逻辑核心`);
        setText("hw-panel-cpu", `${cpuShort}`);

        // 内存
        setText("hw-panel-ram", `${d.ram_used_gb} / ${d.ram_total_gb} GB`);
        setText("hw-panel-ram-sub",
            `占用 <strong style="color: ${d.ram_percent > 85 ? "var(--accent-danger)" : "var(--accent-emerald)"};">${Math.round(d.ram_percent || 0)}%</strong> · 总量 ${d.ram_total_gb}GB`);

        // 系统推荐
        setText("hw-panel-tier", d.recommended_mode || "—");
        setText("hw-panel-tier-sub", "基于上方硬件自动评估，已同步至模式推荐");
        const tierBadge = document.getElementById("hw-panel-tier-badge");
        if (tierBadge && d.recommended_mode) tierBadge.innerText = `推荐: ${d.recommended_mode}`;
    } catch (e) {
        // 忽略轻微抖动
    }
}

// 3.1 直播推流必备软件生态检测 (OBS/虚拟摄像头/伴侣/多媒体底座)
async function loadSoftwarePrerequisites(isManual = false) {
    const container = document.getElementById("prereq-cards-container");
    const overallBadge = document.getElementById("prereq-overall-badge");
    const alertBanner = document.getElementById("prereq-alert-banner");
    const alertText = document.getElementById("prereq-alert-text");
    const recheckBtn = document.getElementById("btn-recheck-prereqs");

    if (isManual && recheckBtn) {
        recheckBtn.disabled = true;
        recheckBtn.innerHTML = `${svg("refresh", "icon-sm")} 正在扫描...`;
    }

    try {
        const res = await fetch(`${API_BASE}/system/prerequisites?t=${Date.now()}`);
        if (!res.ok) {
            throw new Error(`探测接口响应 ${res.status} (旧进程尚未热加载新路由)`);
        }
        const json = await res.json();
        if (json.code !== 0 || !json.data) {
            throw new Error(json.message || "探测数据异常");
        }

        const { summary, items } = json.data;

        // 1. 更新顶部汇总 Badge
        if (overallBadge) {
            overallBadge.className = "brand-badge " + (
                summary.overall_level === "success" ? "green" :
                summary.overall_level === "warning" ? "amber" : "red"
            );
            overallBadge.innerText = `${summary.overall_level === "success" ? "本机组件就绪" : summary.overall_level === "warning" ? "核心待完善" : "必备未安装"} (${summary.ready_count}/${summary.total})`;
        }

        // 2. 更新告警横幅
        if (alertBanner) {
            if (summary.critical_missing > 0) {
                alertBanner.className = "prereq-alert-banner " + (summary.critical_missing >= 2 ? "alert" : "warning");
                alertBanner.style.display = "flex";
                alertBanner.innerHTML = `
                  <div class="prereq-alert-dot"></div>
                  <div id="prereq-alert-text"><strong>本机媒体组件诊断：</strong>${summary.overall_text}。若使用虚拟摄像头推流至公域电商平台，建议按下方指引完善配置。</div>
                `;
            } else if (summary.missing_count > 0) {
                alertBanner.className = "prereq-alert-banner warning";
                alertBanner.style.display = "flex";
                alertBanner.innerHTML = `
                  <div class="prereq-alert-dot"></div>
                  <div id="prereq-alert-text"><strong>推流就绪提示：</strong>核心主控已就绪，尚有 ${summary.missing_count} 项配套伴侣或音频隔离设备待配置，建议完善以保证商用播出纯净度。</div>

                `;
            } else {
                alertBanner.className = "prereq-alert-banner success";
                alertBanner.style.display = "flex";
                alertBanner.innerHTML = `
                  <div class="prereq-alert-dot"></div>
                  <div id="prereq-alert-text"><strong>本机媒体组件健全：</strong>${summary.overall_text}</div>
                `;
            }
        }

        // 3. 渲染各个必备软件卡片 (演播室双列横向清单)
        if (container && Array.isArray(items)) {
            const iconMap = {
                obs: "camera",
                vcam_driver: "video",
                pyvirtualcam: "box",
                live_partner: "broadcast",
                media_core: "cpu",
                python_runtime: "terminal",
                audio_devices: "mic"
            };

            container.innerHTML = items.map(it => {
                let stateText = "";
                let pillClass = "";
                let statusClass = `status-${it.status}`;

                if (it.key === "audio_devices") {
                    const isCableReady = Boolean(it.has_cable || (it.badge && it.badge.includes("VB-Cable")) || (it.tip && it.tip.includes("包含推荐虚拟声卡 VB-Cable")));
                    if (it.status === "installed" && isCableReady) {
                        stateText = "检测通过";
                        pillClass = "pill-pass";
                        statusClass = "status-running";
                    } else if (it.status === "installed" || it.status === "running") {
                        stateText = "建议修复";
                        pillClass = "pill-warning";
                        statusClass = "status-warning";
                    } else {
                        stateText = "必须修复";
                        pillClass = "pill-missing";
                        statusClass = "status-missing";
                    }
                } else if (it.status === "running" || it.status === "installed") {
                    stateText = "检测通过";
                    pillClass = "pill-pass";
                    statusClass = it.status === "running" ? "status-running" : "status-installed";
                } else {
                    if (it.required) {
                        stateText = "必须修复";
                        pillClass = "pill-missing";
                        statusClass = "status-missing";
                    } else {
                        stateText = "建议修复";
                        pillClass = "pill-warning";
                        statusClass = "status-warning";
                    }
                }


                const tipClass = pillClass === "pill-pass" ? "tip-installed" : (pillClass === "pill-warning" ? "tip-warning" : "tip-missing");
                const iconName = iconMap[it.key] || "monitor";

                let actionBtnHtml = "";
                if (pillClass === "pill-pass") {
                    actionBtnHtml = `<span class="prereq-ok-label">${svg("check", "icon-sm")} 正常</span>`;
                } else if (it.action_type === "url" && it.url) {
                    actionBtnHtml = `<a href="${it.url}" target="_blank" class="prereq-btn btn-action-primary">${svg("external", "icon-sm")} ${it.action_text}</a>`;
                } else if (it.action_type === "copy" && it.command) {
                    actionBtnHtml = `<button class="prereq-btn" onclick="copyPrereqCommand('${it.command}', this)">${svg("copy", "icon-sm")} ${it.action_text}</button>`;
                } else if (it.action_type === "tip" && it.url) {
                    actionBtnHtml = `<a href="${it.url}" target="_blank" class="prereq-btn">${svg("info", "icon-sm")} ${it.action_text}</a>`;
                } else {
                    actionBtnHtml = `<span class="prereq-ok-label">${svg("check", "icon-sm")} 正常</span>`;
                }

                const tipIcon = pillClass === "pill-pass" ? "check" : (pillClass === "pill-warning" ? "warn" : "x");

                return `
                <div class="prereq-row ${statusClass}">
                  <!-- 第 1 列：图标 -->
                  <div class="prereq-row-icon">
                    ${svg(iconName, "icon")}
                  </div>

                  <!-- 第 2 列：软件名称与分类 Tag / 细节 -->
                  <div class="prereq-row-name-col">
                    <span class="prereq-row-name" title="${it.name}">${it.name}</span>
                    <div class="prereq-row-tags">
                      <span class="prereq-row-cat">${it.category}</span>
                      <span class="prereq-row-cat" style="color: var(--text-secondary);">${it.badge}</span>
                    </div>
                  </div>

                  <!-- 第 3 列：统一标准状态列 -->
                  <div class="prereq-row-status-col">
                    <span class="prereq-pill ${pillClass}">
                      <span class="prereq-dot"></span>
                      ${stateText}
                    </span>
                  </div>

                  <!-- 第 4 列：功能描述与实时诊断提示 -->
                  <div class="prereq-row-desc-col">
                    <div class="prereq-row-desc">${it.desc}</div>
                    <div class="prereq-row-tip ${tipClass}">
                      ${svg(tipIcon, "icon-sm")}
                      <span title="${it.tip}">${it.tip}</span>
                    </div>
                  </div>

                  <!-- 第 5 列：操作按钮 -->
                  <div class="prereq-row-action-col">
                    ${actionBtnHtml}
                  </div>
                </div>`;
            }).join("");

        }

    } catch (e) {
        console.warn("探测直播必备软件失败:", e);
        if (overallBadge) {
            overallBadge.className = "brand-badge amber";
            overallBadge.innerText = "需重启服务";
        }
        if (alertBanner && alertText) {
            alertBanner.className = "prereq-alert-banner warning";
            alertBanner.style.display = "flex";
            alertText.innerHTML = `<strong>本机媒体组件检测提醒：</strong>${e.message}。检测到当前运行的后端为旧进程，请重启本地服务（关闭旧终端后重新运行 run_agent.bat）即可生效。`;
        }
        if (container) {
            container.innerHTML = `
            <div class="hw-cell" style="grid-column: 1 / -1; text-align: center; padding: 24px;">
              <div style="font-size: 14px; font-weight: 600; color: var(--amber); margin-bottom: 8px;">
                ${svg("warn", "icon")} 未能连接到直播必备软件探测接口 (404)
              </div>
              <div style="font-size: 12px; color: var(--text-secondary); max-width: 520px; margin: 0 auto 16px; line-height: 1.6;">
                检测到您当前电脑后台正在运行未更新的旧版本后端进程（无法加载新增路由）。请关闭运行旧进程的黑底终端窗口，重新双击 <strong>run_agent.bat</strong> 启动服务，即可自动展现完整本机媒体组件。
              </div>
              <button class="btn btn-primary btn-sm" onclick="loadSoftwarePrerequisites(true)">
                ${svg("refresh", "icon-sm")} 重新检测
              </button>
            </div>`;
        }
    } finally {
        if (isManual && recheckBtn) {
            setTimeout(() => {
                recheckBtn.disabled = false;
                recheckBtn.innerHTML = `<svg class="icon-sm" viewBox="0 0 24 24" style="width: 12px; height: 12px;"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg> 重新检测`;
            }, 600);
        }
    }
}

// 复制安装命令小助手
window.copyPrereqCommand = function(cmd, btn) {
    if (!cmd) return;
    navigator.clipboard.writeText(cmd).then(() => {
        if (btn) {
            const originalHtml = btn.innerHTML;
            btn.innerHTML = `${svg("check", "icon-sm")} 已复制到剪切板`;
            btn.classList.add("action-green");
            setTimeout(() => {
                btn.innerHTML = originalHtml;
                btn.classList.remove("action-green");
            }, 2000);
        }
    }).catch(() => {
        prompt("请手动复制安装命令：", cmd);
    });
};

// 4. 主播设定 (需求3：卡片式角色选择 + 人设编辑 + 直播中禁止激活切换)
let roleCardsCache = [];
let selectedRoleId = null;

const ROLE_CARD_META = {
    "ecommerce": { icon: "orange", avatarSvg: "/static/svg/anchor_ecommerce.svg", tag: "橙色橘子·促单逼单", tagColor: "#F97316" },
    "entertainment": { icon: "mic", avatarSvg: "/static/svg/anchor_entertainment.svg", tag: "电光麦克风·逗梗陪伴", tagColor: "#2DD4A0" },
    "expert": { icon: "plus-circle", avatarSvg: "/static/svg/anchor_expert.svg", tag: "医疗+守护·专业解答", tagColor: "#EF4444" },
    "chitchat": { icon: "handshake", avatarSvg: "/static/svg/anchor_chitchat.svg", tag: "暖金握手·唠嗑搭子", tagColor: "#F59E0B" }
};

