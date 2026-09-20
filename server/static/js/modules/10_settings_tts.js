// ============================================================================
// 语音合成与远程算力 (TTS 声音枢纽) 生态选型与多引擎管理体系
// ============================================================================
const BUILTIN_TTS_ECOSYSTEM = [
    {
        id: "moss_tts_nano",
        name: "MOSS-TTS-Nano",
        tagline: "系统自带 · 端侧神经克隆",
        officialUrl: "",
        officialAction: "",
        getKeyUrl: "",
        cardDocTitle: "",
        brandColor: "#0284C7",
        logoSvg: "/static/svg/tts_moss.svg",
        defaultBaseUrl: "http://127.0.0.1:9880",
        placeholderUrl: "系统原生内置，无需配置 Base URL",
        needKey: false,
        needUrl: false,
        recommendedVoices: [
            { text: "官方预置清亮女主播 (广播级 48kHz)", val: "moss_female_host_01" },
            { text: "官方预置阳光男主播 (活力亲和)", val: "moss_male_host_02" },
            { text: "温柔知性女主播 (服饰生活)", val: "moss_female_warm_03" },
            { text: "活力带货女主播 (食品零食)", val: "moss_female_lively_04" }
        ],
        desc: "基于复旦团队开源 MOSS-TTS-Nano 深度融合。仅 ~100M 参数、~500MB 显存开销，端侧极速生成 48kHz 广播级真人语音。优先调度本地或云端 GPU 进行极速零样本声音克隆，亦支持无显卡轻量运行。",
        urlPills: [
            { text: "⚡ 本地推理端点 (127.0.0.1:9880)", val: "http://127.0.0.1:9880", needKey: false }
        ]
    },
    {
        id: "cosyvoice",
        name: "CosyVoice",
        tagline: "官方大模型原生端点 · 极速流式 WebSocket",
        officialUrl: "https://bailian.console.aliyun.com/cn-beijing/model/experience/voice/sound-cloning",
        officialAction: "百炼声音复刻中心 ↗",
        getKeyUrl: "https://bailian.console.aliyun.com/",
        cardDocTitle: "前往阿里云百炼声音复刻中心 (在线录制/上传音频复刻并获取专属 Voice-ID)",
        brandColor: "#EA580C",
        logoSvg: "/static/svg/tts_cosyvoice.svg",
        defaultBaseUrl: "https://dashscope.aliyuncs.com/api/v1",
        placeholderUrl: "选择云端商用 API (填Key即用) 或 本地自建推理端口 (如 http://127.0.0.1:9233)",
        needKey: true,
        needUrl: true,
        recommendedVoices: [
            { text: "龙小春 (知性女声 · 电商带货推荐)", val: "longxiaochun" },
            { text: "龙小白 (清澈邻家 · 治愈少女)", val: "longxiaobai" },
            { text: "龙小夏 (元气少女 · 语音助手)", val: "longxiaoxia" },
            { text: "龙小诚 (阳光男声 · 沉稳专业)", val: "longxiaocheng" },
            { text: "龙老铁 (东北老铁 · 互动爆款)", val: "longlaotie" },
            { text: "龙婉 (温和对话 · 亲切邻家)", val: "longwan" },
            { text: "龙书 (磁性叙事 · 情感故事)", val: "longshu" },
            { text: "龙悦 (文雅舒缓 · 品质解说)", val: "longyue" },
            { text: "龙安冲 (活力带货 · 食品零食)", val: "longanchong" },
            { text: "龙安然 (燃播带货 · 激情促单)", val: "longanran" },
            { text: "龙安萱 (亲和带货 · 美妆日用)", val: "longanxuan" },
            { text: "龙安平 (科技沉稳 · 数码家电)", val: "longanping" },
            { text: "龙硕 (质感男声 · 品牌带货)", val: "longshuo" },
            { text: "杰力豆 (活泼童声 · 母婴玩具)", val: "longjielidou" },
            { text: "龙橙 (阳光朝气 · 青春男声)", val: "longcheng" },
            { text: "龙华 (成熟稳重 · 商务男声)", val: "longhua" },
            { text: "龙静 (文雅解说 · 舒缓女声)", val: "longjing" },
            { text: "Stella (自然解说 · 品质女主播)", val: "loongstella" },
            { text: "Bella (温柔知性 · 服饰带货)", val: "loongbella" }
        ],
        desc: "阿里通义开源大模型语音合成。商业云端调用推荐使用【阿里云百炼平台】开通账号并创建 API Key（Base URL 为 https://dashscope.aliyuncs.com/api/v1 或您的百炼专属服务端点），亦支持本地或局域网私有化 GPU 部署。",
        urlPills: []
    },
    {
        id: "elevenlabs",
        name: "ElevenLabs",
        tagline: "全球顶级情感克隆 · 电影级人声",
        officialUrl: "https://elevenlabs.io/app/developers/api-keys",
        officialAction: "前往开通并创建Key ↗",
        getKeyUrl: "https://elevenlabs.io/app/developers/api-keys",
        cardDocTitle: "前往 ElevenLabs 官网控制台 (开通账号、订阅充值并创建 API Key)",
        brandColor: "#F43F5E",
        logoSvg: "/static/svg/tts_elevenlabs.svg",
        defaultBaseUrl: "https://api.elevenlabs.io/v1",
        placeholderUrl: "https://api.elevenlabs.io/v1",
        needKey: true,
        needUrl: true,
        recommendedVoices: [
            { text: "Rachel (知性温婉)", val: "21m00Tcm4TlvDq8ikWAM" },
            { text: "Adam (沉稳男声)", val: "pNInz6obpgDQGcFmaJgB" },
            { text: "Bella (甜美轻快)", val: "EXAVITQu4vr4xnSDxMaL" },
            { text: "Antoni (富有感染力)", val: "ErXwobaYiN019PkySvjV" }
        ],
        desc: "行业公认全球顶尖拟真度，官方云端托管。前往 ElevenLabs 官网控制台开通账号、充值订阅并创建 API Key（官方 Base URL 为 https://api.elevenlabs.io/v1）。",
        urlPills: [
            { text: "⚡ ElevenLabs 官方云端", val: "https://api.elevenlabs.io/v1", needKey: true }
        ]
    }
];
window.BUILTIN_TTS_ECOSYSTEM = BUILTIN_TTS_ECOSYSTEM;

// 智能解析当前应当选中的 TTS 引擎与配置
// 严格原则：
// 1. 优先以用户激活的配置为准 (is_active === true)
// 2. 若无激活项但用户已配置过 TTS，以已有配置为准 (首选条目)
// 3. 若用户没有任何配置，才默认回退选中本地引擎 (moss_tts_nano)
function resolveActiveOrPreferredTTSProvider(configs = null) {
    const list = configs || (typeof cachedAllConfigs !== "undefined" ? cachedAllConfigs : []) || [];
    const ttsConfigs = list.filter(c => c && c.config_group === "tts");

    // 1. 优先以用户激活的配置为准
    const activeCfg = ttsConfigs.find(c => Boolean(c.is_active));
    if (activeCfg) {
        const meta = resolveTTSProviderMeta(activeCfg.provider_name, activeCfg);
        return { providerId: meta.id, config: activeCfg, reason: "active_config" };
    }

    // 2. 如果用户已有保存的 TTS 配置
    if (ttsConfigs.length > 0) {
        const preferredCfg = ttsConfigs[0];
        const meta = resolveTTSProviderMeta(preferredCfg.provider_name, preferredCfg);
        return { providerId: meta.id, config: preferredCfg, reason: "existing_config" };
    }

    // 3. 用户完全没有配置，默认选中本地引擎 moss_tts_nano
    return { providerId: "moss_tts_nano", config: null, reason: "fallback_local" };
}
window.resolveActiveOrPreferredTTSProvider = resolveActiveOrPreferredTTSProvider;

function renderTTSEcosystemGrid(selectedId = null) {
    const grid = document.getElementById("tts-ecosystem-grid");
    if (!grid) return;

    // 若未显式传入 selectedId，优先根据用户配置动态决议
    let targetId = selectedId;
    if (!targetId) {
        if (typeof currentSelectedTTSProvider !== "undefined" && currentSelectedTTSProvider) {
            targetId = currentSelectedTTSProvider;
        } else {
            const resolved = resolveActiveOrPreferredTTSProvider();
            targetId = resolved.providerId;
        }
    }

    grid.innerHTML = "";

    const ttsList = (typeof BUILTIN_TTS_ECOSYSTEM !== "undefined" && Array.isArray(BUILTIN_TTS_ECOSYSTEM))
        ? BUILTIN_TTS_ECOSYSTEM
        : (window.BUILTIN_TTS_ECOSYSTEM || []);

    ttsList.forEach(item => {
        const card = document.createElement("div");
        card.className = "tts-provider-card" + (item.id === targetId ? " selected" : "");
        card.setAttribute("data-provider", item.id);
        card.onclick = () => selectTTSProvider(item.id);

        const logoSrc = (item.logoSvg || "").startsWith("http") ? item.logoSvg : (item.logoSvg + "?v=20260918_v1");
        const officialLinkHtml = item.officialUrl ? `
                <a href="${item.officialUrl}" target="_blank" rel="noopener noreferrer" class="tts-card-official-link" title="${item.cardDocTitle}" onclick="event.stopPropagation();">
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path>
                        <polyline points="15 3 21 3 21 9"></polyline>
                        <line x1="10" y1="14" x2="21" y2="3"></line>
                    </svg>
                </a>` : "";
        card.innerHTML = `
                ${officialLinkHtml}
                <img src="${logoSrc}" class="tts-provider-logo" alt="${item.name}">
                <div class="tts-provider-name" title="${item.name}">${item.name}</div>
                <div class="tts-provider-tagline" title="${item.tagline}">${item.tagline}</div>
            `;
        grid.appendChild(card);
    });
    refreshTTSHardwareStatus();
}

async function refreshTTSHardwareStatus() {
    const pill = document.getElementById("tts-hw-status-pill");
    if (!pill) return;
    try {
        const res = await fetch(`${API_BASE}/live/hardware`);
        const json = await res.json();
        if (json.code === 0 && json.data) {
            const cap = json.data.gpu_capability || {};
            const localGpu = cap.local_gpu || json.data.gpu || {};
            if (cap.use_cloud) {
                pill.innerHTML = `⚡ 算力状态: 已连接远端 GPU 加速 (${escapeHtml(cap.cloud_gpu?.provider_name || '云端节点')})`;
                pill.style.color = "#38bdf8";
            } else if (cap.can_execute && !cap.is_low_spec_local) {
                pill.innerHTML = `🟢 算力状态: 本机 ${escapeHtml(localGpu.gpu_name || '独显')} (显存 ${localGpu.vram_total_gb || 0}GB 充足)`;
                pill.style.color = "#34d399";
            } else {
                pill.innerHTML = `🟡 算力状态: 本地显存不足，MOSS-TTS 自动走 CPU 极速多线程 (稳定保底)`;
                pill.style.color = "#fbbf24";
            }
        }
    } catch (_) {}
}

// 智能识别 TTS 品牌元数据
function resolveTTSProviderMeta(providerId, config = null) {
    const rawId = (providerId || "").toLowerCase().trim();
    if (rawId && rawId !== "custom" && rawId !== "custom_tts") {
        const hit = BUILTIN_TTS_ECOSYSTEM.find(p => p.id === rawId);
        if (hit) return hit;
    }

    const pName = (config && config.provider_name ? config.provider_name : rawId).toLowerCase();
    const bUrl = (config && config.base_url ? config.base_url : "").toLowerCase();
    const mName = (config && config.model_name ? config.model_name : "").toLowerCase();
    const combined = `${pName} ${bUrl} ${mName}`;

    if (combined.includes("moss") || combined.includes("nano")) return BUILTIN_TTS_ECOSYSTEM.find(p => p.id === "moss_tts_nano");
    if (combined.includes("cosy")) return BUILTIN_TTS_ECOSYSTEM.find(p => p.id === "cosyvoice");
    if (combined.includes("eleven")) return BUILTIN_TTS_ECOSYSTEM.find(p => p.id === "elevenlabs");

    return BUILTIN_TTS_ECOSYSTEM[0];
}

// 快速填入 TTS Base URL 并联动更新 Key 状态
function fillTTSBaseUrlAndSyncKey(url, needKey = null) {
    const urlInput = document.getElementById("tts-input-url");
    if (urlInput) {
        urlInput.value = url;
        urlInput.focus();
    }
    syncTTSKeyStatusByUrl(url, needKey);
}

// 依据当前输入的 URL 智能判断是云端 API (需 Key) 还是本地私有自建 (免 Key)，并联动显示官方开通获取 Key 链接
function syncTTSKeyStatusByUrl(url, explicitNeedKey = null) {
    const keyInput = document.getElementById("tts-input-key");
    const hintEl = document.getElementById("tts-key-status-hint");
    const getKeyLink = document.getElementById("tts-get-key-link");
    if (!keyInput) return;

    const trimmed = (url || "").trim().toLowerCase();
    const isLocal = trimmed.includes("127.0.0.1") || trimmed.includes("localhost") || trimmed.startsWith("http://192.168.") || trimmed.startsWith("http://10.");
    const isMoss = currentSelectedTTSProvider === "moss_tts_nano";

    const isCloud = explicitNeedKey === true || (!isLocal && !isMoss && (trimmed.startsWith("https://") || trimmed.includes("dashscope") || trimmed.includes("siliconflow") || trimmed.includes("elevenlabs") || trimmed.includes("api.")));

    const curMeta = BUILTIN_TTS_ECOSYSTEM.find(p => p.id === currentSelectedTTSProvider) || BUILTIN_TTS_ECOSYSTEM[0];

    if (isMoss) {
        if (hintEl) hintEl.innerText = "系统自带 · 免密钥开箱即用";
        keyInput.placeholder = "系统原生自带引擎，免 API 密钥，优先本地/云端 GPU 加速";
        if (getKeyLink) getKeyLink.style.display = "none";
    } else if (isCloud) {
        if (hintEl) hintEl.innerText = "云端商用托管 (需硬件安全加密密钥)";
        keyInput.placeholder = "输入云服务商 API 密钥 (如 sk-xxxx)";
        if (getKeyLink) {
            let targetUrl = curMeta.getKeyUrl || curMeta.officialUrl;
            if (trimmed.includes("siliconflow")) {
                targetUrl = "https://cloud.siliconflow.cn/account/ak";
            } else if (trimmed.includes("dashscope") || trimmed.includes("aliyun")) {
                targetUrl = "https://bailian.console.aliyun.com/";
            }
            if (targetUrl) {
                getKeyLink.href = targetUrl;
                getKeyLink.innerText = "开通账号/获取API Key ↗";
                getKeyLink.title = `前往官方控制台开通并获取 API Key (在新标签页打开)`;
                getKeyLink.style.display = "inline";
            } else {
                getKeyLink.style.display = "none";
            }
        }
    } else if (isLocal) {
        if (hintEl) hintEl.innerText = "本地私有端口免密钥直连";
        keyInput.placeholder = "当前为本地/局域网推理端口，免 API Key 直连";
        if (getKeyLink) getKeyLink.style.display = "none";
    } else {
        if (hintEl) hintEl.innerText = "根据端点自动适配密钥";
        keyInput.placeholder = "云端商用需填 sk-xxxx，本地端口可留空";
        if (getKeyLink && curMeta.getKeyUrl) {
            getKeyLink.href = curMeta.getKeyUrl;
            getKeyLink.innerText = "开通账号/获取API Key ↗";
            getKeyLink.style.display = "inline";
        } else if (getKeyLink) {
            getKeyLink.style.display = "none";
        }
    }
}

// 切换当前配置的语音引擎品牌
async function selectTTSProvider(providerId, existingConfig = null) {
    const meta = resolveTTSProviderMeta(providerId, existingConfig);
    currentSelectedTTSProvider = meta.id;
    window.currentSelectedTTSProvider = meta.id;

    if (!existingConfig && cachedAllConfigs && cachedAllConfigs.length > 0) {
        // 优先匹配当前已激活的该引擎配置
        let found = cachedAllConfigs.find(c => {
            if (c.config_group !== "tts") return false;
            const cMeta = resolveTTSProviderMeta(c.provider_name, c);
            return cMeta.id === meta.id && Boolean(c.is_active);
        });
        if (!found) {
            found = cachedAllConfigs.find(c => {
                if (c.config_group !== "tts") return false;
                const cMeta = resolveTTSProviderMeta(c.provider_name, c);
                return cMeta.id === meta.id;
            });
        }
        if (found) existingConfig = found;
    }

    // 高亮品牌卡片
    document.querySelectorAll(".tts-provider-card").forEach(c => {
        c.classList.toggle("selected", c.getAttribute("data-provider") === meta.id);
    });

    // 0 毫秒立即同步刷新底层第三方生效模型与通道协议视窗，彻底杜绝数据穿杂
    renderTTSThirdpartyModelInfoDirect(meta.id);

    // 编辑面板头部
    const logoEl = document.getElementById("tts-editor-logo");
    const titleEl = document.getElementById("tts-editor-title");
    const badgeEl = document.getElementById("tts-editor-badge");
    const descEl = document.getElementById("tts-editor-desc");
    const providerInput = document.getElementById("tts-editor-provider");
    const configIdInput = document.getElementById("tts-editor-config-id");

    const officialLinkEl = document.getElementById("tts-editor-official-link");

    if (logoEl) logoEl.src = (meta.logoSvg || "").startsWith("http") ? meta.logoSvg : (meta.logoSvg + "?v=20260917_v6");
    if (titleEl) titleEl.innerText = `配置 ${meta.name}`;
    if (badgeEl) badgeEl.innerText = (meta.tagline || "").includes("·") ? meta.tagline.split("·")[0].trim() : (meta.tagline || "官方推荐");
    if (descEl) descEl.innerText = meta.desc || "";
    if (providerInput) providerInput.value = meta.id;
    if (configIdInput) configIdInput.value = existingConfig ? existingConfig.id : "";
    if (officialLinkEl) {
        if (meta.officialUrl && meta.id !== "moss_tts_nano") {
            officialLinkEl.style.display = "inline-flex";
            officialLinkEl.href = meta.officialUrl;
            officialLinkEl.title = meta.cardDocTitle || `前往 ${meta.name} 官方控制台 (在新标签页打开)`;
            const spanEl = officialLinkEl.querySelector("span");
            if (spanEl) {
                spanEl.innerText = meta.officialAction || "官方控制台 ↗";
            }
        } else {
            officialLinkEl.style.display = "none";
        }
    }

    // 第一行 Base URL (双通道支持)
    const urlGroupEl = document.getElementById("tts-url-form-group");
    const keyGroupEl = document.getElementById("tts-key-form-group");
    if (meta.id === "moss_tts_nano") {
        if (urlGroupEl) urlGroupEl.style.display = "none";
        if (keyGroupEl) keyGroupEl.style.display = "none";
    } else {
        if (urlGroupEl) urlGroupEl.style.display = "";
        if (keyGroupEl) keyGroupEl.style.display = "";
    }

    const urlInput = document.getElementById("tts-input-url");
    const currentBaseUrl = existingConfig ? (existingConfig.base_url || meta.defaultBaseUrl) : meta.defaultBaseUrl;
    if (urlInput) {
        urlInput.value = currentBaseUrl || "";
        urlInput.placeholder = meta.placeholderUrl || (meta.needUrl ? "请输入服务 Base URL 地址" : "云端直接调用，无需填写 Base URL");
        urlInput.oninput = () => syncTTSKeyStatusByUrl(urlInput.value);
    }

    const urlPillsBox = document.getElementById("tts-editor-url-pills");
    if (urlPillsBox) {
        if (meta.urlPills && meta.urlPills.length > 0) {
            urlPillsBox.innerHTML = `
                <span style="font-size: 11px; color: var(--text-muted);">快捷端点:</span>
                ${meta.urlPills.map(p => `<span class="quick-pill" onclick="fillTTSBaseUrlAndSyncKey('${p.val}', ${p.needKey})">${p.text}</span>`).join("")}
            `;
            urlPillsBox.style.display = "";
        } else {
            urlPillsBox.innerHTML = "";
            urlPillsBox.style.display = "none";
        }
    }

    // 第二行 API Key：切换引擎时立即重置输入框，严格隔离各引擎密钥，杜绝跨引擎混入
    const keyInput = document.getElementById("tts-input-key");
    if (keyInput) {
        keyInput.value = "";
        keyInput.type = "password";
    }
    syncTTSKeyStatusByUrl(currentBaseUrl, meta.needKey);
    await autoFillRealTTSKeyIfConfigured(existingConfig ? existingConfig.id : null, existingConfig, meta);

    // 专属开通指引提示卡（如阿里云百炼 CosyVoice / Edge-TTS 等）
    const guideBox = document.getElementById("tts-provider-guide-box");
    if (guideBox) {
        if (meta.id === "cosyvoice") {
            guideBox.innerHTML = `
                <div style="background: rgba(15, 23, 42, 0.75); border: 1px solid rgba(234, 88, 12, 0.35); border-radius: 8px; padding: 12px 15px; font-size: 12.5px; color: #cbd5e1; line-height: 1.85;">
                    <div style="font-weight: 700; color: #fb923c; margin-bottom: 8px; display: flex; align-items: center; gap: 7px;">
                        <svg viewBox="0 0 24 24" style="width: 15px; height: 15px; stroke: #fb923c; fill: none; stroke-width: 2.2;"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
                        阿里云百炼 · CosyVoice 快速开通与发声指南：
                    </div>
                    <div style="display: flex; flex-direction: column; gap: 5px; padding-left: 2px;">
                        <div>1. 打开 <a href="https://bailian.console.aliyun.com/cn-beijing/model/experience/voice/sound-cloning" target="_blank" rel="noopener noreferrer" style="color: #38bdf8; text-decoration: underline; font-weight: 600;">阿里云百炼声音复刻中心 ↗</a>；</div>
                        <div>2. 上传或录制 10~20 秒清晰人声，生成您的专属 <strong>Voice-ID</strong>；</div>
                        <div>3. 点击左侧 <strong>API-Key</strong> 创建并复制您的百炼 API Key 和 OpenAI 兼容地址（Base URL）；</div>
                        <div>4. 回到中控台填写并登记 Voice-ID，系统将通过官方 WebSocket 协议由大模型实时发声！</div>
                    </div>
                </div>
            `;
            guideBox.style.display = "block";
        } else if (meta.id === "elevenlabs") {
            guideBox.innerHTML = `
                <div style="background: rgba(15, 23, 42, 0.65); border: 1px solid rgba(244, 63, 94, 0.35); border-radius: 8px; padding: 12px 15px; font-size: 12.5px; color: #cbd5e1; line-height: 1.8;">
                    <div style="font-weight: 700; color: #fb7185; margin-bottom: 6px; display: flex; align-items: center; gap: 7px;">
                        <svg viewBox="0 0 24 24" style="width: 15px; height: 15px; stroke: #fb7185; fill: none; stroke-width: 2.2;"><path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>
                        ElevenLabs 全球顶级情感语音服务：
                    </div>
                    <div style="display: flex; flex-direction: column; gap: 4px; padding-left: 2px;">
                        <div>1. 前往 <a href="https://elevenlabs.io/app/developers/api-keys" target="_blank" rel="noopener noreferrer" style="color: #38bdf8; text-decoration: underline; font-weight: 600;">ElevenLabs 开发者控制台 ↗</a> 开通并创建 API Key；</div>
                        <div>2. 粘贴 API Key 后点击上方「获取音色与模型」，系统将自动拉取官方丰富影视级音色库。</div>
                    </div>
                </div>
            `;
            guideBox.style.display = "block";
        } else {
            guideBox.innerHTML = "";
            guideBox.style.display = "none";
        }
    }

    // 第五行：初始化具体音色与选定状态（自动拉取专属克隆音色与引擎推荐音色，克隆置顶）
    const voiceInput = document.getElementById("tts-input-voice");
    const pingStatusEl = document.getElementById("tts-ping-latency-status");
    if (pingStatusEl) pingStatusEl.innerHTML = `握手测试: 未测试`;

    const selectedVoiceVal = (existingConfig && existingConfig.model_name) ? existingConfig.model_name : "";
    if (voiceInput && selectedVoiceVal) voiceInput.value = selectedVoiceVal;

    // 自动拉取克隆音色库与引擎预设音色并即时呈现
    fetchAndRenderTTSVoices(meta, selectedVoiceVal);

    const testResultBox = document.getElementById("tts-test-result");
    if (testResultBox) testResultBox.style.display = "none";

    // 自动通过 API 探测/拉取第三方服务商底层生效模型与协议
    const cachedKey = existingConfig ? (cachedDecryptedKeys[existingConfig.id] || "") : "";
    fetchAndRenderTTSThirdpartyModelInfo(currentBaseUrl, cachedKey, meta.id, existingConfig ? existingConfig.id : "");

    // 联动更新专属声音克隆定制工作台
    updateTTSCloneWorkbenchUI(meta.id);

    // 强化保障：无论如何自动确保 Base URL 与解密 API Key 填入输入框
    await ensureTTSKeyAndUrlFilled(meta.id, existingConfig);
}

// 确保输入框自动填入端点与解密后的 API Key
async function ensureTTSKeyAndUrlFilled(providerId, existingConfig = null) {
    const meta = resolveTTSProviderMeta(providerId, existingConfig);
    const urlInput = document.getElementById("tts-input-url");
    const keyInput = document.getElementById("tts-input-key");

    // 1. 自动填入 Base URL
    let targetUrl = (existingConfig && existingConfig.base_url) ? existingConfig.base_url : meta.defaultBaseUrl;
    if (urlInput && targetUrl) {
        urlInput.value = targetUrl;
        syncTTSKeyStatusByUrl(targetUrl, meta.needKey);
    }

    // 2. 自动填入真实解密后的 API Key
    if (keyInput) {
        await autoFillRealTTSKeyIfConfigured(existingConfig ? existingConfig.id : null, existingConfig, meta);
    }
}

// 切换声音克隆工作台的 Tab (上传克隆 vs 登记ID)
function switchTTSCloneTab(tab) {
    const tabUploadBtn = document.getElementById("btn-clone-tab-upload");
    const tabBindBtn = document.getElementById("btn-clone-tab-bind");
    const panelUpload = document.getElementById("tts-clone-panel-upload");
    const panelBind = document.getElementById("tts-clone-panel-bind");

    if (tab === "bind") {
        if (tabUploadBtn) tabUploadBtn.classList.remove("active");
        if (tabBindBtn) tabBindBtn.classList.add("active");
        if (panelUpload) panelUpload.style.display = "none";
        if (panelBind) panelBind.style.display = "block";
    } else {
        if (tabUploadBtn) tabUploadBtn.classList.add("active");
        if (tabBindBtn) tabBindBtn.classList.remove("active");
        if (panelUpload) panelUpload.style.display = "block";
        if (panelBind) panelBind.style.display = "none";
    }
}

// 依据当前选中的 TTS 引擎更新克隆工作台状态
function updateTTSCloneWorkbenchUI(providerId) {
    const pId = (providerId || currentSelectedTTSProvider || "moss_tts_nano").toLowerCase();
    const targetNameEl = document.getElementById("tts-clone-target-engine-name");
    const badgeEl = document.getElementById("tts-clone-engine-badge");
    const noticeEl = document.getElementById("tts-clone-unsupported-notice");
    const panelUpload = document.getElementById("tts-clone-panel-upload");
    const panelBind = document.getElementById("tts-clone-panel-bind");
    const tipEl = document.getElementById("tts-clone-upload-tip");
    const tabUploadBtn = document.getElementById("btn-clone-tab-upload");
    const tabBindBtn = document.getElementById("btn-clone-tab-bind");

    const meta = resolveTTSProviderMeta(pId);
    if (targetNameEl && meta) targetNameEl.innerText = meta.name;

    const aliyunCard = document.getElementById("tts-clone-aliyun-card");
    const mossCard = document.getElementById("tts-clone-moss-card");

    if (badgeEl) {
        badgeEl.className = "brand-badge green";
        badgeEl.innerText = "支持专属声音克隆";
    }
    if (noticeEl) noticeEl.style.display = "none";

    if (pId === "moss_tts_nano") {
        if (mossCard) mossCard.style.display = "block";
        if (aliyunCard) aliyunCard.style.display = "none";
        if (tabBindBtn) tabBindBtn.style.display = "none";
        if (tabUploadBtn) {
            tabUploadBtn.style.display = "inline-block";
            tabUploadBtn.innerText = "🎙️ 本地上传/零样本克隆";
        }
        switchTTSCloneTab("upload");
        if (tipEl) {
            tipEl.innerText = "⚡ 上传 5~30 秒清晰人声 WAV/MP3，MOSS-TTS-Nano 将通过端侧 GPU 快速提取声学特征并完成零样本声音克隆";
        }
    } else if (pId === "cosyvoice") {
        if (mossCard) mossCard.style.display = "none";
        if (aliyunCard) aliyunCard.style.display = "block";
        if (tabBindBtn) {
            tabBindBtn.style.display = "inline-block";
            tabBindBtn.innerText = "🔗 登记百炼 Voice-ID (官方推荐)";
        }
        if (tabUploadBtn) {
            tabUploadBtn.style.display = "inline-block";
            tabUploadBtn.innerText = "🎙️ 本地上传/公网音频复刻";
        }
        const isBindActive = tabBindBtn && tabBindBtn.classList.contains("active");
        if (panelUpload) panelUpload.style.display = isBindActive ? "none" : "block";
        if (panelBind) panelBind.style.display = isBindActive ? "block" : "none";
        if (tipEl) {
            tipEl.innerText = "⚡ 上传 120秒内清晰音频，系统将针对阿里云 CosyVoice (cosyvoice-v3.5-flash) 创建专属克隆声线";
        }
    } else if (pId === "elevenlabs") {
        if (mossCard) mossCard.style.display = "none";
        if (aliyunCard) aliyunCard.style.display = "none";
        if (tabBindBtn) tabBindBtn.style.display = "none";
        if (tabUploadBtn) {
            tabUploadBtn.style.display = "inline-block";
            tabUploadBtn.innerText = "🎙️ 本地上传音频克隆";
        }
        switchTTSCloneTab("upload");
        if (tipEl) {
            tipEl.innerText = "⚡ 上传音频将通过 ElevenLabs Instant Voice Cloning 官方接口创建电影级克隆音色";
        }
    } else {
        if (mossCard) mossCard.style.display = "none";
        if (aliyunCard) aliyunCard.style.display = "none";
        if (tabUploadBtn) tabUploadBtn.style.display = "inline-block";
        if (tabBindBtn) tabBindBtn.style.display = "none";
        switchTTSCloneTab("upload");
        if (tipEl) {
            tipEl.innerText = "⚡ 点击克隆后将自动生成声纹档案，并即刻加入上方音色栏供试听与开播";
        }
    }
}

// 执行本地音频一键克隆
async function handleExecuteTTSClone() {
    const btn = document.getElementById("btn-execute-clone");
    const nameInput = document.getElementById("tts-clone-voice-name");
    const fileInput = document.getElementById("tts-clone-voice-file");
    const speedInput = document.getElementById("tts-clone-speed");

    const voiceName = nameInput ? nameInput.value.trim() : "";
    if (!voiceName) {
        if (typeof showToast === "function") showToast("请输入克隆音色名称", "warning");
        if (nameInput) nameInput.focus();
        return;
    }

    if (!fileInput || !fileInput.files || fileInput.files.length === 0) {
        if (typeof showToast === "function") showToast("请选择一段 120秒内清晰人声 WAV/MP3 音频文件", "warning");
        return;
    }

    const file = fileInput.files[0];
    const speed = speedInput ? parseFloat(speedInput.value) || 1.0 : 1.0;

    const urlInput = document.getElementById("tts-input-url");
    const keyInput = document.getElementById("tts-input-key");
    const providerInput = document.getElementById("tts-editor-provider");
    const activeModelEl = document.getElementById("tts-thirdparty-active-model");

    const baseUrl = urlInput ? urlInput.value.trim() : "";
    const apiKey = keyInput ? keyInput.value.trim() : "";
    const provider = providerInput ? providerInput.value : currentSelectedTTSProvider;
    const targetModel = activeModelEl ? activeModelEl.innerText.trim() : "cosyvoice-v3.5-flash";

    const formData = new FormData();
    formData.append("name", voiceName);
    formData.append("audio_file", file);
    formData.append("speed", speed.toString());
    formData.append("volume", "1.0");
    formData.append("provider_name", provider);
    if (apiKey) formData.append("api_key", apiKey);
    if (baseUrl) formData.append("base_url", baseUrl);
    if (targetModel) formData.append("target_model", targetModel);

    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner-sm"></span> 正在提取声纹并克隆中...`;
    }

    try {
        const res = await fetch(`${API_BASE}/voices/clone`, {
            method: "POST",
            body: formData
        });
        const json = await res.json();
        if (res.ok && json.code === 0 && json.data) {
            const voiceId = json.data.voice_code || json.data.id;
            const displayName = json.data.name || voiceName;
            const serverMsg = json.message || "";

            // 成功呈现反馈提示
            if (typeof showToast === "function") {
                showToast(
                    `🎉 专属声音克隆成功: ${displayName}！已加入音色列表，正在为您试听。`,
                    "success",
                    3500
                );
            }

            // 【核心修复】：立即将刚刚克隆的主播音色追加到“获取到的具体音色”并自动选定与试听！
            injectClonedVoicePill(voiceId, `👑 [专属克隆] ${displayName}`);

            // 重置上传表单
            if (nameInput) nameInput.value = "";
            if (fileInput) fileInput.value = "";

            // 刷新下方资产库
            if (typeof loadVoiceTable === "function") loadVoiceTable();
        } else {
            if (typeof showToast === "function") {
                showToast(`克隆失败: ${json.detail || json.message || "未知错误"}`, "error");
            }
        }
    } catch (e) {
        if (typeof showToast === "function") showToast("上传克隆异常: " + e, "error");
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `
                <svg class="icon-sm" viewBox="0 0 24 24" style="width: 14px; height: 14px;"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>
                立即一键克隆并加入音色列表
            `;
        }
    }
}


// 输入 Voice-ID 时智能推导配对模型并实时同步
function handleVoiceIdInputAdaptModel(val) {
    if (!val) return;
    const clean = val.trim().toLowerCase();
    const hintEl = document.getElementById("tts-bind-model-hint");
    let matchedModel = "";
    if (clean.includes("qwen-audio-3.0-tts-plus")) {
        matchedModel = "qwen-audio-3.0-tts-plus";
    } else if (clean.includes("qwen-audio-3.0-tts-flash")) {
        matchedModel = "qwen-audio-3.0-tts-flash";
    } else if (clean.includes("cosyvoice-v3.5")) {
        matchedModel = "cosyvoice-v3.5-flash";
    } else if (clean.includes("cosyvoice-v1")) {
        matchedModel = "cosyvoice-v1";
    }

    if (matchedModel) {
        selectTTSRealModel(matchedModel);
        if (hintEl) {
            hintEl.innerHTML = `
                <span style="color: #34d399; font-weight: 600;">
                    ✓ 智能识别模型架构: <strong>${matchedModel}</strong> (已自动同步并配对)
                </span>
            `;
        }
    }
}

// 探测/同步阿里云百炼当前业务空间已复刻的音色
async function handleSyncDashscopeVoices() {
    const btn = document.getElementById("btn-sync-dashscope-voices");
    const container = document.getElementById("tts-cloud-voices-suggestion");
    const listEl = document.getElementById("tts-cloud-voices-list");
    const nameInput = document.getElementById("tts-bind-voice-name");
    const vidInput = document.getElementById("tts-bind-voice-id");

    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner-sm"></span> 探测百炼云端中...`;
    }

    try {
        const res = await fetch(`${API_BASE}/voices/dashscope-voices`);
        const json = await res.json();
        if (json.code === 0 && Array.isArray(json.data) && json.data.length > 0) {
            if (container) container.style.display = "block";
            if (listEl) {
                listEl.innerHTML = json.data.map(v => {
                    const vid = v.voice_id || "";
                    const m = v.target_model || "qwen-audio-3.0-tts-plus";
                    const shortId = vid.length > 25 ? (vid.substring(0, 10) + '...' + vid.substring(vid.length - 8)) : vid;
                    return `
                        <div class="quick-pill" style="cursor: pointer; border-color: #38bdf8; background: rgba(56, 189, 248, 0.15); color: #38bdf8; display: inline-flex; align-items: center; gap: 4px; padding: 4px 8px; font-size: 11px;"
                             title="点击自动填入 Voice-ID: ${escapeHtml(vid)}"
                             onclick="selectCloudDetectedVoice('${escapeHtml(vid)}', '${escapeHtml(m)}')">
                            <span>✨ ${escapeHtml(shortId)}</span>
                            <span style="opacity: 0.7; font-size: 10px;">(${escapeHtml(m)})</span>
                        </div>
                    `;
                }).join("");
            }
            if (typeof showToast === "function") {
                showToast(`已从阿里云百炼探测到 ${json.data.length} 个就绪克隆音色！`, "success");
            }
            // 若只有一个且输入框为空，自动贴心地帮用户预填
            if (json.data.length === 1 && vidInput && !vidInput.value.trim()) {
                selectCloudDetectedVoice(json.data[0].voice_id, json.data[0].target_model);
            }
        } else {
            if (container) container.style.display = "none";
            const msg = json.message || "未在百炼空间中探测到有效克隆音色";
            if (typeof showToast === "function") showToast(`百炼云端探测: ${msg}`, "info");
        }
    } catch (e) {
        console.warn("探测百炼云端音色异常:", e);
        if (typeof showToast === "function") showToast("探测云端音色异常: " + e, "error");
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `
                <svg viewBox="0 0 24 24" style="width: 12px; height: 12px; stroke: #34d399; fill: none; stroke-width: 2;"><path d="M21.5 15a4.5 4.5 0 0 0-4-6.5h-.5A7 7 0 0 0 3.5 11 5 5 0 0 0 2 15a5 5 0 0 0 5 5h14.5a4.5 4.5 0 0 0 0-9z"/></svg>
                ☁️ 探测/同步云端已复刻音色
            `;
        }
    }
}

// 选中云端探测到的音色
function selectCloudDetectedVoice(vid, targetModel) {
    const vidInput = document.getElementById("tts-bind-voice-id");
    const nameInput = document.getElementById("tts-bind-voice-name");
    if (vidInput) vidInput.value = vid;
    if (nameInput && !nameInput.value.trim()) {
        nameInput.value = "专属克隆音色";
    }
    handleVoiceIdInputAdaptModel(vid);
    if (targetModel) {
        selectTTSRealModel(targetModel);
    }
}

async function handleRegisterExternalVoiceId() {
    const btn = document.getElementById("btn-bind-voice");
    const nameInput = document.getElementById("tts-bind-voice-name");
    const vidInput = document.getElementById("tts-bind-voice-id");

    const voiceName = (nameInput ? nameInput.value.trim() : "") || "主播专属声线";
    const voiceId = vidInput ? vidInput.value.trim() : "";

    if (!voiceName) {
        if (typeof showToast === "function") showToast("请输入音色名称", "warning");
        if (nameInput) nameInput.focus();
        return;
    }
    if (!voiceId) {
        if (typeof showToast === "function") showToast("请输入在第三方平台生成的专属 Voice ID", "warning");
        if (vidInput) vidInput.focus();
        return;
    }

    const providerInput = document.getElementById("tts-editor-provider");
    const activeModelEl = document.getElementById("tts-thirdparty-active-model");
    const provider = providerInput ? providerInput.value : currentSelectedTTSProvider;
    
    // 智能推导模型：优先以 Voice-ID 前缀特征推导
    let targetModel = activeModelEl ? activeModelEl.innerText.trim() : "cosyvoice-v3.5-flash";
    const cleanVid = voiceId.toLowerCase();
    if (cleanVid.includes("qwen-audio-3.0-tts-plus")) {
        targetModel = "qwen-audio-3.0-tts-plus";
    } else if (cleanVid.includes("qwen-audio-3.0-tts-flash")) {
        targetModel = "qwen-audio-3.0-tts-flash";
    } else if (cleanVid.includes("cosyvoice-v3.5")) {
        targetModel = "cosyvoice-v3.5-flash";
    }

    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner-sm"></span> 官方实测发声校验中...`;
    }

    try {
        const res = await fetch(`${API_BASE}/voices/bind-id`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                name: voiceName,
                voice_id: voiceId,
                provider_name: provider,
                target_model: targetModel,
                speech_speed: 1.0,
                volume_gain: 1.0
            })
        });
        const json = await res.json();
        if (res.ok && json.code === 0 && json.data) {
            if (typeof showToast === "function") {
                showToast(`🎉 已成功实测发声并绑定专属 Voice-ID: ${voiceId}`, "success");
            }

            // 注入上方音色药丸并选定试听
            injectClonedVoicePill(voiceId, `👑 [专属绑定] ${voiceName}`);

            if (nameInput) nameInput.value = "";
            if (vidInput) vidInput.value = "";
            if (typeof loadVoiceTable === "function") loadVoiceTable();
            // 重新刷新上方音色药丸池，确保专属克隆持久置顶
            const curMeta = resolveTTSProviderMeta(currentSelectedTTSProvider);
            if (curMeta) fetchAndRenderTTSVoices(curMeta, voiceId);
        } else {
            const errDetail = json.detail || json.message || "服务商校验拒绝";
            if (typeof showToast === "function") {
                showToast(`登记失败: ${errDetail}`, "error", 8000);
            }
        }
    } catch (e) {
        if (typeof showToast === "function") showToast("绑定异常: " + e, "error");
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `
                <svg class="icon-sm" viewBox="0 0 24 24" style="width: 14px; height: 14px;"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>
                一键绑定并 WebSocket 生成试听
            `;
        }
    }
}

// 动态将新克隆的音色注入上方药丸容器首位并立即激活
function injectClonedVoicePill(voiceVal, displayLabel) {
    const container = document.getElementById("tts-fetched-voices-container");
    if (!container) return;

    // 清除容器中旧的“暂无音色”占位提示
    const emptyNotice = container.querySelector("div");
    if (emptyNotice && emptyNotice.innerText.includes("暂未获取音色")) {
        container.innerHTML = "";
    }

    // 检查是否已存在相同 voiceVal 的药丸
    let pill = container.querySelector(`[data-voice="${voiceVal}"]`);
    if (!pill) {
        pill = document.createElement("div");
        pill.className = "fetched-model-pill";
        pill.setAttribute("data-voice", voiceVal);
        pill.setAttribute("title", `点击试听并选定专属克隆音色: ${displayLabel}`);
        pill.style.borderColor = "#f59e0b";
        pill.style.color = "#fbbf24";
        pill.style.background = "rgba(245, 158, 11, 0.12)";
        pill.style.fontWeight = "600";
        pill.innerHTML = `
            <svg class="pill-play-icon" viewBox="0 0 24 24" style="width: 12px; height: 12px; stroke: #fbbf24; fill: #fbbf24;"><polygon points="5 3 19 12 5 21 5 3"/></svg>
            <span class="pill-voice-name">${escapeHtml(displayLabel)}</span>
        `;
        pill.onclick = () => selectFetchedTTSVoice(voiceVal, displayLabel);
        container.insertBefore(pill, container.firstChild);
    }

    // 彻底单选并选定该专属克隆音色！
    selectFetchedTTSVoice(voiceVal, displayLabel);
}

// 3 大主流语音引擎模型元数据与协议预设字典
const TTS_PROVIDER_MODELS_PRESETS = {
    moss_tts_nano: {
        activeModel: "MOSS-TTS-Nano (100M Ultra-Lightweight)",
        protocol: "MOSS-TTS-Nano 端侧神经引擎 (GPU 可用时自动加速 · 广播级 48kHz)",
        statusText: (hasKey) => "系统原生自带 · 免密钥开箱即用",
        badgeClass: (hasKey) => "brand-badge green",
        models: [
            "MOSS-TTS-Nano (100M Ultra-Lightweight)",
            "MOSS-TTS-Nano-Local (本地GPU)",
            "MOSS-TTS-Nano-Cloud (云端端点)"
        ]
    },
    cosyvoice: {
        activeModel: "cosyvoice-v3.5-flash",
        protocol: "阿里云百炼 DashScope 语音通道",
        statusText: (hasKey) => hasKey ? "已配置密钥 · 百炼云端通道就绪" : "待配置密钥 · 百炼官方通道",
        badgeClass: (hasKey) => hasKey ? "brand-badge green" : "brand-badge amber",
        models: [
            "cosyvoice-v3.5-flash",
            "cosyvoice-v3.5-plus",
            "cosyvoice-v3-flash",
            "cosyvoice-v3-plus",
            "cosyvoice-v2",
            "cosyvoice-v1"
        ]
    },
    elevenlabs: {
        activeModel: "eleven_multilingual_v2",
        protocol: "ElevenLabs 官方云端全球低延迟流式通道",
        statusText: (hasKey) => hasKey ? "已配置密钥 · ElevenLabs通道就绪" : "待配置密钥 · 官方云端通道",
        badgeClass: (hasKey) => hasKey ? "brand-badge green" : "brand-badge amber",
        models: [
            "eleven_multilingual_v2",
            "eleven_turbo_v2_5",
            "eleven_flash_v2_5",
            "eleven_monolingual_v1"
        ]
    }
};

// 同步即时渲染第三方模型与协议信息（0延迟，杜绝界面残留与跨引擎数据穿杂）
function renderTTSThirdpartyModelInfoDirect(provider, apiKey = "", baseUrl = "") {
    const modelEl = document.getElementById("tts-thirdparty-active-model");
    const protoEl = document.getElementById("tts-thirdparty-protocol");
    const badgeEl = document.getElementById("tts-thirdparty-status-badge");
    const pillsContainer = document.getElementById("tts-thirdparty-models-pills");
    const pillsRow = document.getElementById("tts-thirdparty-models-pills-row");
    if (!modelEl) return;

    const curProvider = (provider || currentSelectedTTSProvider || "moss_tts_nano").toLowerCase();
    let preset = TTS_PROVIDER_MODELS_PRESETS[curProvider];
    if (!preset) {
        if (curProvider.includes("moss") || curProvider.includes("nano")) preset = TTS_PROVIDER_MODELS_PRESETS.moss_tts_nano;
        else if (curProvider.includes("cosy")) preset = TTS_PROVIDER_MODELS_PRESETS.cosyvoice;
        else if (curProvider.includes("eleven")) preset = TTS_PROVIDER_MODELS_PRESETS.elevenlabs;
        else preset = TTS_PROVIDER_MODELS_PRESETS.moss_tts_nano;
    }

    const hasKey = Boolean(apiKey && apiKey.trim().length > 4);
    const activeModel = preset.activeModel;
    const protocolText = preset.protocol;
    const statusText = typeof preset.statusText === "function" ? preset.statusText(hasKey) : preset.statusText;
    const badgeClass = typeof preset.badgeClass === "function" ? preset.badgeClass(hasKey) : preset.badgeClass;

    modelEl.innerHTML = `<strong>${escapeHtml(activeModel)}</strong>`;
    if (protoEl) protoEl.innerText = protocolText;
    if (badgeEl) {
        badgeEl.className = badgeClass;
        badgeEl.innerText = statusText;
    }

    if (pillsContainer && pillsRow) {
        pillsRow.style.display = "flex";
        pillsContainer.innerHTML = preset.models.map(m => `
            <span class="quick-pill" data-model="${escapeHtml(m)}"
                  style="cursor: pointer; ${m === activeModel ? 'border-color: #38bdf8; background: rgba(56, 189, 248, 0.2); color: #38bdf8;' : ''}"
                  onclick="selectTTSRealModel('${escapeHtml(m)}')">
                ${escapeHtml(m)}
            </span>
        `).join("");
    }
}

// 选用第三方生效模型
function selectTTSRealModel(modelName) {
    if (!modelName) return;
    const modelEl = document.getElementById("tts-thirdparty-active-model");
    if (modelEl) {
        modelEl.innerHTML = `<strong>${escapeHtml(modelName)}</strong>`;
    }
    document.querySelectorAll("#tts-thirdparty-models-pills .quick-pill").forEach(p => {
        const isMatch = p.getAttribute("data-model") === modelName;
        p.style.borderColor = isMatch ? "#38bdf8" : "";
        p.style.background = isMatch ? "rgba(56, 189, 248, 0.2)" : "";
        p.style.color = isMatch ? "#38bdf8" : "";
    });
    if (typeof showToast === "function") {
        showToast(`已选用第三方底层生效模型: ${modelName}`, "info");
    }
}

// 请求版本序号（严格防竞态，丢弃过时网络返回）
let _ttsModelFetchId = 0;

// 实时通过 API 获取并渲染第三方服务商真实模型与通道协议
async function fetchAndRenderTTSThirdpartyModelInfo(baseUrl, apiKey, provider, configId) {
    const modelEl = document.getElementById("tts-thirdparty-active-model");
    const protoEl = document.getElementById("tts-thirdparty-protocol");
    const badgeEl = document.getElementById("tts-thirdparty-status-badge");
    const pillsContainer = document.getElementById("tts-thirdparty-models-pills");
    const pillsRow = document.getElementById("tts-thirdparty-models-pills-row");
    if (!modelEl) return;

    const curProvider = (provider || currentSelectedTTSProvider || "moss_tts_nano").toLowerCase();
    // 1. 同步即时渲染本地预设信息，确保 0 毫秒立即生效
    renderTTSThirdpartyModelInfoDirect(curProvider, apiKey, baseUrl);

    // 2. 发起异步探测
    const currentFetchId = ++_ttsModelFetchId;

    try {
        const res = await fetch(`${API_BASE}/settings/tts/models`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                base_url: baseUrl || null,
                api_key: apiKey || null,
                provider_name: curProvider,
                config_id: configId || null
            })
        });
        const json = await res.json();

        // 3. 严格防竞态丢弃校验：若当前请求已过期或用户已切走，绝不覆盖！
        if (currentFetchId !== _ttsModelFetchId || (currentSelectedTTSProvider && currentSelectedTTSProvider !== curProvider)) {
            return;
        }

        if (json.code === 0 && json.active_model) {
            const activeModel = json.active_model;
            modelEl.innerHTML = `<strong>${escapeHtml(activeModel)}</strong>`;
            if (json.protocol && protoEl) protoEl.innerText = json.protocol;
            if (badgeEl && json.status_text) badgeEl.innerText = json.status_text;

            const modelsList = Array.isArray(json.models) && json.models.length > 0
                ? json.models
                : [activeModel];

            if (pillsContainer && pillsRow) {
                pillsRow.style.display = "flex";
                pillsContainer.innerHTML = modelsList.map(m => `
                    <span class="quick-pill" data-model="${escapeHtml(m)}"
                          style="cursor: pointer; ${m === activeModel ? 'border-color: #38bdf8; background: rgba(56, 189, 248, 0.2); color: #38bdf8;' : ''}"
                          onclick="selectTTSRealModel('${escapeHtml(m)}')">
                        ${escapeHtml(m)}
                    </span>
                `).join("");
            }
        }
    } catch (e) {
        console.warn("探测第三方模型信息异常:", e);
    }
}

// 全局试听音频与网络请求单例控制器（彻底杜绝并发竞争导致多个声音同时播放）
window._ttsPreviewController = window._ttsPreviewController || {
    audio: null,
    abortController: null,
    sessionId: 0,
    stopAll() {
        this.sessionId++;
        if (this.abortController) {
            try { this.abortController.abort(); } catch (e) { }
            this.abortController = null;
        }
        if (this.audio) {
            try {
                this.audio.pause();
                this.audio.currentTime = 0;
                this.audio.src = "";
            } catch (e) { }
            this.audio = null;
        }
        if (typeof previewAudioEl !== "undefined" && previewAudioEl) {
            try { previewAudioEl.pause(); previewAudioEl = null; } catch (e) { }
        }
        document.querySelectorAll("#tts-fetched-voices-container .fetched-model-pill").forEach(el => el.classList.remove("playing"));
    }
};

// 即时播放指定音色的专属试听音频
async function playTTSVoicePreview(voiceVal, voiceLabel = "") {
    if (!voiceVal) return;

    // 1. 强力终止任何正在播放的音频与仍在进行的异步网络请求
    window._ttsPreviewController.stopAll();
    const currentSession = window._ttsPreviewController.sessionId;
    const abortCtrl = new AbortController();
    window._ttsPreviewController.abortController = abortCtrl;

    const urlInput = document.getElementById("tts-input-url");
    const keyInput = document.getElementById("tts-input-key");
    const providerInput = document.getElementById("tts-editor-provider");
    const pingStatusEl = document.getElementById("tts-ping-latency-status");

    const baseUrl = urlInput ? urlInput.value.trim() : "";
    const apiKey = keyInput ? keyInput.value.trim() : "";
    const provider = providerInput ? providerInput.value : currentSelectedTTSProvider;

    // 标记当前正在播放的音色药丸动效
    document.querySelectorAll("#tts-fetched-voices-container .fetched-model-pill").forEach(el => {
        el.classList.toggle("playing", el.getAttribute("data-voice") === voiceVal);
    });

    if (pingStatusEl) {
        pingStatusEl.innerHTML = `
            <span style="color: #38BDF8; font-weight: 600; display: inline-flex; align-items: center; gap: 5px;">
                <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #38BDF8; box-shadow: 0 0 6px #38BDF8;"></span>
                🔊 正在试听: <strong>${escapeHtml(voiceLabel || voiceVal)}</strong>...
            </span>
        `;
    }

    try {
        const activeModelEl = document.getElementById("tts-thirdparty-active-model");
        let currentActiveModel = activeModelEl ? activeModelEl.innerText.trim() : "";

        // 若是 CosyVoice 官方系统预置音色，自动纠偏模型为 cosyvoice-v1 (避免复刻专属模型 v3.5-flash 报 418)
        const isPreset = (voiceVal.startsWith("long") || voiceVal.startsWith("loong")) && !voiceVal.includes("cloned") && !voiceVal.includes("custom");
        if (provider === "cosyvoice" && isPreset) {
            currentActiveModel = "cosyvoice-v1";
        }

        const previewRes = await fetch(`${API_BASE}/settings/tts/preview`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            signal: abortCtrl.signal,
            body: JSON.stringify({
                provider_name: provider,
                model_name: currentActiveModel || null,
                voice_name: voiceVal,
                base_url: baseUrl || null,
                api_key: apiKey || null
            })
        });

        // 若网络返回时发现已经发起了新的试听请求，立即丢弃，绝不播放！
        if (window._ttsPreviewController.sessionId !== currentSession) return;

        if (previewRes.ok) {
            const blob = await previewRes.blob();
            if (window._ttsPreviewController.sessionId !== currentSession) return;

            const audioUrl = URL.createObjectURL(blob);
            const audio = new Audio(audioUrl);
            window._ttsPreviewController.audio = audio;

            audio.onended = () => {
                document.querySelectorAll("#tts-fetched-voices-container .fetched-model-pill").forEach(el => el.classList.remove("playing"));
                if (pingStatusEl) {
                    pingStatusEl.innerHTML = `
                        <span style="color: #10B981; font-weight: 600; display: inline-flex; align-items: center; gap: 5px;">
                            <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #10B981;"></span>
                            试听完成 · 当前已选定: <strong>${escapeHtml(voiceLabel || voiceVal)}</strong>
                        </span>
                    `;
                }
            };

            await audio.play();
        } else {
            const errJson = await previewRes.json().catch(() => ({}));
            document.querySelectorAll("#tts-fetched-voices-container .fetched-model-pill").forEach(el => el.classList.remove("playing"));
            if (pingStatusEl) {
                pingStatusEl.innerHTML = `
                    <span style="color: #ef4444; font-size: 11.5px;">
                        ⚠️ 试听受阻: ${escapeHtml(errJson.detail || "音色服务未响应")}
                    </span>
                `;
            }
        }
    } catch (e) {
        if (e.name === "AbortError") return; // 用户切换快速切换，正常中断
        console.warn("音色试听播放受阻:", e);
        document.querySelectorAll("#tts-fetched-voices-container .fetched-model-pill").forEach(el => el.classList.remove("playing"));
    }
}

// 异步拉取并渲染音色候选药丸（专属克隆音色档案与官方预设音色深度合并，专属克隆置顶展示）
async function fetchAndRenderTTSVoices(meta, selectedVoiceVal = "") {
    const container = document.getElementById("tts-fetched-voices-container");
    const countStatusEl = document.getElementById("tts-voices-count-status");
    const voiceInput = document.getElementById("tts-input-voice");
    if (!container || !meta) return { clonedVoices: [], recommendedVoices: [], currentSelected: "" };

    // 1. 获取该引擎的官方推荐音色
    const recommendedVoices = (meta.recommendedVoices || []).map(r => ({ ...r }));
    const recommendedIdSet = new Set(recommendedVoices.map(r => (r.val || "").trim().toLowerCase()));

    // 2. 获取后端已登记的声音档案 (VoiceProfile)，严格按引擎和类型进行区分，绝不把官方预设重复当成克隆音色
    let clonedVoices = [];
    try {
        const res = await fetch(`${API_BASE}/voices/list`);
        const json = await res.json();
        if (json.code === 0 && Array.isArray(json.data)) {
            const currentProvider = (meta.id || "").toLowerCase();
            const seenCloneIds = new Set();

            json.data.forEach(v => {
                const vid = (v.id || "").trim();
                const vidLower = vid.toLowerCase();
                if (!vid || vidLower === "voice_default_female") return;

                // 若该音色是官方预设音色（在官方预设推荐表中，或者 voice_type === 'preset'）
                if (recommendedIdSet.has(vidLower) || v.voice_type === "preset") {
                    // 若用户在音色资产库中为该官方音色改了名，同步更新推荐药丸的展示名称
                    const recItem = recommendedVoices.find(r => (r.val || "").trim().toLowerCase() === vidLower);
                    if (recItem && v.name && !v.name.startsWith("zh-CN-")) {
                        const oldDesc = recItem.text.includes("(") || recItem.text.includes("（")
                            ? (recItem.text.split(/[\(\（]/)[1] || "")
                            : "";
                        recItem.text = oldDesc ? `${v.name} (${oldDesc}` : v.name;
                    }
                    return; // 严禁将官方预设音色放入专属克隆列表！
                }

                // 所属引擎过滤：仅保留属于当前引擎的真正专属克隆音色
                const vProv = (v.provider_name || "").toLowerCase();
                const isMatchProvider = !vProv || vProv === currentProvider || (currentProvider === "cosyvoice" && (vid.includes("cosy") || vid.includes("bailian")));
                const isCloned = v.voice_type === "cloned" || v.is_clone || vid.startsWith("clone_") || vid.includes("bailian") || vid.includes("cloned");

                if (isMatchProvider && isCloned && !seenCloneIds.has(vid)) {
                    seenCloneIds.add(vid);
                    clonedVoices.push(v);
                }
            });
        }
    } catch (e) {
        console.warn("获取声音档案库列表异常:", e);
    }

    // 3. 决定当前选定的音色
    let currentSelected = selectedVoiceVal || (voiceInput ? voiceInput.value.trim() : "");
    const isCurrentInClones = clonedVoices.some(v => v.id === currentSelected);
    const isCurrentInRecs = recommendedVoices.some(v => v.val === currentSelected);

    // 若当前选中的 ID 已失效/不在列表中，自动选定有效音色
    if (!currentSelected || (!isCurrentInClones && !isCurrentInRecs)) {
        if (clonedVoices.length > 0) {
            currentSelected = clonedVoices[0].id;
        } else if (recommendedVoices.length > 0) {
            currentSelected = recommendedVoices[0].val;
        } else {
            currentSelected = "";
        }
    }
    if (voiceInput) voiceInput.value = currentSelected;

    // 4. 构建药丸 DOM (引入全局 ID 防重锁，100% 确保每个音色只呈现一次)
    let pillsHtml = "";
    const renderedVoiceIds = new Set();

    // 4.1 专属克隆音色（金色尊贵皇冠高亮，置顶显示，只展示真实专属克隆，绝不混入官方音色）
    clonedVoices.forEach(cv => {
        if (!cv.id || renderedVoiceIds.has(cv.id)) return;
        renderedVoiceIds.add(cv.id);

        const isSel = cv.id === currentSelected;
        pillsHtml += `
            <div class="fetched-model-pill cloned-voice-pill ${isSel ? 'selected' : ''}"
                 data-voice="${escapeHtml(cv.id)}"
                 data-is-clone="true"
                 title="点击选定并试听专属克隆音色: ${escapeHtml(cv.name)} (ID: ${escapeHtml(cv.id)})"
                 onclick="selectFetchedTTSVoice('${escapeHtml(cv.id)}', '${escapeHtml(cv.name)} (专属克隆)')"
                 style="border-color: rgba(245, 158, 11, 0.65); background: ${isSel ? 'rgba(245, 158, 11, 0.28)' : 'rgba(245, 158, 11, 0.12)'}; color: #FBBF24; font-weight: 600; display: inline-flex; align-items: center; gap: 6px; padding: 6px 10px;">
                <span style="font-size: 13px;">👑</span>
                <span style="font-weight: 600;">${escapeHtml(cv.name)}</span>
                <span class="btn-delete-clone-pill"
                      title="删除此克隆音色档案"
                      onclick="handleDeleteClonedVoice(event, '${escapeHtml(cv.id)}', '${escapeHtml(cv.name)}')"
                      style="display: inline-flex; align-items: center; justify-content: center; width: 16px; height: 16px; border-radius: 50%; background: rgba(239, 68, 68, 0.25); color: #f87171; margin-left: 4px; cursor: pointer; transition: all 0.2s;"
                      onmouseover="this.style.background='#ef4444';this.style.color='#ffffff';"
                      onmouseout="this.style.background='rgba(239, 68, 68, 0.25)';this.style.color='#f87171';">
                    <svg viewBox="0 0 24 24" style="width: 10px; height: 10px; stroke: currentColor; fill: none; stroke-width: 3;"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                </span>
            </div>
        `;
    });

    // 4.2 引擎预设官方音色（严格排重，已作为克隆展示的绝不在此重复）
    recommendedVoices.forEach(v => {
        if (!v.val || renderedVoiceIds.has(v.val)) return;
        renderedVoiceIds.add(v.val);

        const isSel = v.val === currentSelected;
        pillsHtml += `
            <div class="fetched-model-pill ${isSel ? 'selected' : ''}"
                 data-voice="${escapeHtml(v.val)}"
                 title="点击试听并选定官方音色: ${escapeHtml(v.text)}"
                 onclick="selectFetchedTTSVoice('${escapeHtml(v.val)}', '${escapeHtml(v.text)}')">
                ${escapeHtml(v.text)}
            </div>
        `;
    });

    if (!pillsHtml) {
        container.innerHTML = `<div style="color: var(--text-muted); font-size: 12px;">暂无可用的发音音色，请点击上方「获取音色与模型」。</div>`;
    } else {
        container.innerHTML = pillsHtml;
    }

    // 5. 更新状态与计数
    if (countStatusEl) {
        const total = renderedVoiceIds.size;
        const selectedMatch = clonedVoices.find(v => v.id === currentSelected);
        const selectedRec = recommendedVoices.find(v => v.val === currentSelected);
        let displaySelectedName = "";
        if (selectedMatch) {
            displaySelectedName = `${selectedMatch.name} (专属克隆)`;
        } else if (selectedRec) {
            displaySelectedName = selectedRec.text;
        } else {
            displaySelectedName = currentSelected ? "已选定音色" : "未选定 (请点击下方音色)";
        }

        countStatusEl.innerHTML = `
            <span style="color: #10B981; font-weight: 600; display: inline-flex; align-items: center; gap: 5px;">
                <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #10B981; box-shadow: 0 0 6px #10B981;"></span>
                共 ${total} 款可用音色 ${clonedVoices.length > 0 ? `(含 <strong style="color:#FBBF24;">${clonedVoices.length} 款专属克隆</strong>)` : ''} · 当前选定: <strong>${escapeHtml(displaySelectedName)}</strong>
            </span>
        `;
    }

    const filteredRecs = recommendedVoices.filter(v => v.val && !clonedVoices.some(c => c.id === v.val));
    window._latestFetchedVoices = {
        provider: meta.id,
        clonedVoices,
        recommendedVoices: filteredRecs,
        currentSelected
    };

    return { clonedVoices, recommendedVoices, currentSelected };
}

// 删除指定专属克隆音色（阻止冒泡、确认提示、后端安全删除并实时刷新）
async function handleDeleteClonedVoice(event, voiceId, voiceName) {
    if (event) {
        event.stopPropagation();
        event.preventDefault();
    }
    if (!voiceId) return;

    if (!confirm(`确认彻底删除专属克隆音色【${voiceName}】吗？\nID: ${voiceId}\n删除后对应的本地音频与云端绑定将一并移除，不可恢复。`)) {
        return;
    }

    try {
        const res = await fetch(`${API_BASE}/voices/${voiceId}`, { method: "DELETE" });
        const json = await res.json();
        if (json.code === 0) {
            if (typeof showToast === "function") {
                showToast(`已成功删除专属克隆音色【${voiceName}】`, "success");
            }
            // 若当前输入框选中的正是被删除的音色，清空并切换
            const voiceInput = document.getElementById("tts-input-voice");
            if (voiceInput && voiceInput.value === voiceId) {
                voiceInput.value = "";
            }
            // 实时重新拉取与渲染药丸容器
            const meta = resolveTTSProviderMeta(currentSelectedTTSProvider);
            await fetchAndRenderTTSVoices(meta);
            // 联动刷新下方音色资产库表格
            if (typeof loadVoiceTable === "function") {
                loadVoiceTable();
            }
        } else {
            alert("删除失败: " + (json.detail || json.message));
        }
    } catch (e) {
        alert("删除音色异常: " + e);
    }
}

// 切换选定音色 (点击立即自动发起试听并单选)
function selectFetchedTTSVoice(voiceVal, voiceLabel = "") {
    if (!voiceVal) return;
    const voiceInput = document.getElementById("tts-input-voice");
    if (voiceInput) voiceInput.value = voiceVal;

    // 清除全容器内所有药丸的 selected，只为当前匹配项保留 selected
    document.querySelectorAll("#tts-fetched-voices-container .fetched-model-pill").forEach(el => {
        const isMatch = el.getAttribute("data-voice") === voiceVal;
        el.classList.toggle("selected", isMatch);
        if (!isMatch) el.classList.remove("playing");
    });

    const countStatusEl = document.getElementById("tts-voices-count-status");
    let pureVoiceName = "";
    let fullVoiceLabel = voiceLabel;
    if (countStatusEl) {
        const total = document.querySelectorAll("#tts-fetched-voices-container .fetched-model-pill").length;
        let displayName = voiceLabel;
        if (!displayName || displayName === voiceVal) {
            const matchedPill = document.querySelector(`#tts-fetched-voices-container .fetched-model-pill[data-voice="${voiceVal}"]`);
            if (matchedPill) {
                const nameSpan = matchedPill.querySelector("span:not(.btn-delete-clone-pill)") || matchedPill;
                displayName = nameSpan.textContent.trim();
            } else {
                displayName = "已选定音色";
            }
        }
        fullVoiceLabel = displayName;
        pureVoiceName = displayName.replace(/^[👑\s]+/, "").split(/[\(\（]/)[0].trim() || displayName;
        countStatusEl.innerHTML = `
            <span style="color: #10B981; font-weight: 600; display: inline-flex; align-items: center; gap: 5px;">
                <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #10B981; box-shadow: 0 0 6px #10B981;"></span>
                共 ${total} 款可用音色 · 当前选定: <strong>${escapeHtml(displayName)}</strong>
            </span>
        `;
    } else if (voiceLabel) {
        pureVoiceName = voiceLabel.replace(/^[👑\s]+/, "").split(/[\(\（]/)[0].trim() || voiceLabel;
    }

    // 记录选中的纯净音色名称与完整标签，供保存配置时持久化
    window._currentSelectedTTSVoiceMeta = {
        voiceVal: voiceVal,
        voiceName: pureVoiceName || voiceVal,
        voiceLabel: fullVoiceLabel || voiceVal
    };

    const activeModelEl = document.getElementById("tts-thirdparty-active-model");
    if (activeModelEl && currentSelectedTTSProvider === "cosyvoice") {
        const matchedPill = document.querySelector(`#tts-fetched-voices-container .fetched-model-pill[data-voice="${voiceVal}"]`);
        const isClone = matchedPill && matchedPill.getAttribute("data-is-clone") === "true";
        const autoModel = isClone ? "cosyvoice-v3.5-flash" : "cosyvoice-v1";
        activeModelEl.innerHTML = `<strong>${autoModel}</strong>`;
        // 同步更新模型药丸高亮
        document.querySelectorAll("#tts-thirdparty-models-pills .quick-pill").forEach(p => {
            const pModel = p.getAttribute("data-model");
            const isMatch = pModel === autoModel;
            p.style.borderColor = isMatch ? "#38bdf8" : "";
            p.style.background = isMatch ? "rgba(56, 189, 248, 0.2)" : "";
            p.style.color = isMatch ? "#38bdf8" : "";
        });
    }

    // 用户切换到任意音色时，立即通过单例控制器触发该音色专属声线的即时试听
    playTTSVoicePreview(voiceVal, voiceLabel);
}

// 自动解密填入已配置过的 TTS 密钥并默认脱敏
async function autoFillRealTTSKeyIfConfigured(configId = null, existingConfig = null, meta = null) {
    const keyInput = document.getElementById("tts-input-key");
    const eyeBtn = document.getElementById("btn-toggle-tts-key-eye");
    const hintEl = document.getElementById("tts-key-status-hint");
    const curIdInput = document.getElementById("tts-editor-config-id");
    if (!keyInput) return "";

    const curProvider = (meta && meta.id) ? meta.id : currentSelectedTTSProvider;
    const isMoss = curProvider === "moss_tts_nano";

    // 若当前为免 Key 引擎（如 MOSS-TTS-Nano），无需填充密钥
    if (isMoss) {
        keyInput.value = "";
        keyInput.placeholder = "系统原生自带引擎，免 API 密钥，开箱即用";
        if (hintEl) hintEl.innerText = "系统原生自带 · 免密钥开箱即用";
        return "";
    }

    // 1. 尝试确定 configId
    let targetConfigId = configId || (curIdInput ? curIdInput.value.trim() : "");
    if (!targetConfigId && existingConfig && existingConfig.id) {
        targetConfigId = existingConfig.id;
    }

    // 2. 若仍未拿到 configId，从 cachedAllConfigs 中定位当前引擎的配置（激活项优先）
    if (!targetConfigId && typeof cachedAllConfigs !== "undefined" && Array.isArray(cachedAllConfigs) && cachedAllConfigs.length > 0) {
        const foundActive = cachedAllConfigs.find(c => {
            if (c.config_group !== "tts") return false;
            const cMeta = resolveTTSProviderMeta(c.provider_name, c);
            return cMeta.id === curProvider && Boolean(c.is_active);
        });
        const foundAny = foundActive || cachedAllConfigs.find(c => {
            if (c.config_group !== "tts") return false;
            const cMeta = resolveTTSProviderMeta(c.provider_name, c);
            return cMeta.id === curProvider;
        });
        if (foundAny) {
            targetConfigId = foundAny.id;
        }
    }

    // 3. 若 cachedAllConfigs 此时为空，主动通过 API 异步拉取一次所有配置以防网络时序竞态
    if (!targetConfigId) {
        try {
            const resConfigs = await fetch(`${API_BASE}/settings/configs`);
            const jsonConfigs = await resConfigs.json();
            if (jsonConfigs.code === 0 && Array.isArray(jsonConfigs.data)) {
                if (typeof cachedAllConfigs !== "undefined") {
                    cachedAllConfigs = jsonConfigs.data;
                }
                const hit = jsonConfigs.data.find(c => {
                    if (c.config_group !== "tts") return false;
                    const cMeta = resolveTTSProviderMeta(c.provider_name, c);
                    return cMeta.id === curProvider && Boolean(c.is_active);
                }) || jsonConfigs.data.find(c => {
                    if (c.config_group !== "tts") return false;
                    const cMeta = resolveTTSProviderMeta(c.provider_name, c);
                    return cMeta.id === curProvider;
                });
                if (hit) {
                    targetConfigId = hit.id;
                }
            }
        } catch (e) {
            console.warn("自动补拉 TTS 配置列表异常:", e);
        }
    }

    // 4. 若当前引擎尚未配置过，严格清空 Key 输入框并重置状态，绝不允许混入其他引擎的 Key
    if (!targetConfigId) {
        if (curIdInput) curIdInput.value = "";
        keyInput.value = "";
        keyInput.type = "password";
        if (hintEl) {
            hintEl.innerText = (meta && meta.needKey) ? "云端商用托管 (需硬件安全加密密钥)" : "此引擎免密钥";
        }
        return "";
    }

    if (curIdInput) {
        curIdInput.value = targetConfigId;
    }

    keyInput.type = "password";
    if (eyeBtn) {
        eyeBtn.innerHTML = `<svg viewBox="0 0 24 24"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`;
        eyeBtn.title = "点击显示真实密钥明文";
        eyeBtn.style.color = "";
    }

    // 5. 检查属于当前引擎 configId 的专属内存缓存
    let realKey = (typeof cachedDecryptedKeys !== "undefined") ? (cachedDecryptedKeys[targetConfigId] || "") : "";
    if (realKey) {
        keyInput.value = realKey;
        if (hintEl) hintEl.innerText = "已载入硬件加密密钥 (默认脱敏)";
        return realKey;
    }

    // 6. 调用后端硬件级 AES 解密接口，拉取属于当前引擎自身的真实密钥
    try {
        const res = await fetch(`${API_BASE}/settings/configs/${targetConfigId}/raw-key`);
        const json = await res.json();
        if (json.code === 0 && json.raw_key) {
            realKey = json.raw_key;
            if (typeof cachedDecryptedKeys !== "undefined") {
                cachedDecryptedKeys[targetConfigId] = realKey;
            }
            keyInput.value = realKey;
            keyInput.type = "password";
            if (hintEl) hintEl.innerText = "已载入硬件加密密钥 (默认脱敏)";

            // 顺带触发底层模型架构探测
            const urlInput = document.getElementById("tts-input-url");
            fetchAndRenderTTSThirdpartyModelInfo(
                urlInput ? urlInput.value : "",
                realKey,
                curProvider,
                targetConfigId
            );
            return realKey;
        }
    } catch (e) {
        console.warn("自动获取 TTS API Key 异常:", e);
    }

    // 若当前引擎有配置 ID 但未解析出 Key，保持输入框为空，严禁混用其他引擎 Key
    keyInput.value = "";
    return "";
}

// 切换 TTS 密钥眼睛图标
async function toggleTTSKeyVisibility() {
    const keyInput = document.getElementById("tts-input-key");
    const eyeBtn = document.getElementById("btn-toggle-tts-key-eye");
    const configIdInput = document.getElementById("tts-editor-config-id");
    const hintEl = document.getElementById("tts-key-status-hint");
    if (!keyInput || !eyeBtn) return;

    const eyeShowSvg = `<svg viewBox="0 0 24 24"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`;
    const eyeHideSvg = `<svg viewBox="0 0 24 24"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>`;

    const isPassword = keyInput.type === "password";
    const configId = configIdInput ? configIdInput.value.trim() : "";

    if (isPassword) {
        if (keyInput.value && keyInput.value.trim()) {
            keyInput.type = "text";
            eyeBtn.innerHTML = eyeHideSvg;
            eyeBtn.style.color = "#10B981";
            if (hintEl) hintEl.innerText = "已呈现输入明文";
            return;
        }

        if (configId) {
            if (cachedDecryptedKeys[configId]) {
                keyInput.value = cachedDecryptedKeys[configId];
                keyInput.type = "text";
                eyeBtn.innerHTML = eyeHideSvg;
                eyeBtn.style.color = "#10B981";
                if (hintEl) hintEl.innerText = "已解密呈现真实密钥明文";
                return;
            }

            eyeBtn.disabled = true;
            try {
                const res = await fetch(`${API_BASE}/settings/configs/${configId}/raw-key`);
                const json = await res.json();
                if (json.code === 0 && json.raw_key) {
                    cachedDecryptedKeys[configId] = json.raw_key;
                    keyInput.value = json.raw_key;
                    keyInput.type = "text";
                    eyeBtn.innerHTML = eyeHideSvg;
                    eyeBtn.style.color = "#10B981";
                    if (hintEl) hintEl.innerText = "已解密呈现真实密钥明文";
                }
            } catch (e) {
                alert("请求解密密钥异常: " + e);
            } finally {
                eyeBtn.disabled = false;
            }
        } else {
            keyInput.type = "text";
            eyeBtn.innerHTML = eyeHideSvg;
            eyeBtn.style.color = "#10B981";
        }
    } else {
        keyInput.type = "password";
        eyeBtn.innerHTML = eyeShowSvg;
        eyeBtn.style.color = "";
        if (hintEl) hintEl.innerText = "本地硬件级加密存储";
    }
}

// 点击【获取音色与模型】
async function handleFetchTTSVoices() {
    const btn = document.getElementById("btn-fetch-tts-voices");
    const meta = resolveTTSProviderMeta(currentSelectedTTSProvider);
    const voiceInput = document.getElementById("tts-input-voice");
    const urlInput = document.getElementById("tts-input-url");
    const keyInput = document.getElementById("tts-input-key");
    const configIdInput = document.getElementById("tts-editor-config-id");

    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner-sm"></span> 获取音色与模型中...`;
    }

    try {
        const currentSelected = (voiceInput && voiceInput.value) ? voiceInput.value.trim() : "";
        const result = await fetchAndRenderTTSVoices(meta, currentSelected);

        const realKey = (keyInput ? keyInput.value.trim() : "") || (configIdInput && cachedDecryptedKeys[configIdInput.value]) || "";

        // 联动发起第三方模型与协议信息探测
        fetchAndRenderTTSThirdpartyModelInfo(
            urlInput ? urlInput.value : "",
            realKey,
            meta.id,
            configIdInput ? configIdInput.value : ""
        );

        const totalCount = (result.clonedVoices?.length || 0) + (result.recommendedVoices?.length || 0);
        showTTSTestResult(true, `已成功加载【${meta.name}】可用音色（含 ${result.clonedVoices?.length || 0} 款专属克隆，共 ${totalCount} 款），并同步探测第三方底层模型架构！点击下方药丸即可自由试听与选定。`);
    } catch (e) {
        showTTSTestResult(false, "加载音色列表异常: " + e);
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `<svg class="icon-sm" viewBox="0 0 24 24" style="width: 14px; height: 14px;"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg> 获取音色与模型`;
        }
    }
}

// 点击【测试连通性与试听】
async function handleTestTTSConnectivity() {
    const btn = document.getElementById("btn-test-tts");
    const urlInput = document.getElementById("tts-input-url");
    const keyInput = document.getElementById("tts-input-key");
    const voiceInput = document.getElementById("tts-input-voice");
    const providerInput = document.getElementById("tts-editor-provider");
    const configIdInput = document.getElementById("tts-editor-config-id");
    const pingStatusEl = document.getElementById("tts-ping-latency-status");

    const baseUrl = urlInput ? urlInput.value.trim() : "";
    const apiKey = keyInput ? keyInput.value.trim() : "";
    const provider = providerInput ? providerInput.value : currentSelectedTTSProvider;
    const configId = configIdInput ? configIdInput.value : "";
    const selectedVoice = voiceInput ? voiceInput.value.trim() : "";

    // 停止上一次未播放完的试听 (通过全局试听单例控制器，杜绝未定义变量引用)
    if (window._ttsPreviewController && typeof window._ttsPreviewController.stopAll === "function") {
        window._ttsPreviewController.stopAll();
    }

    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner-sm"></span> 探测握手中...`;
    }
    if (pingStatusEl) {
        pingStatusEl.innerHTML = `握手测试: <span style="color: var(--sky);">探测中...</span>`;
    }

    try {
        // 步骤 1: 探测网络连通性与网络延迟
        const res = await fetch(`${API_BASE}/settings/test-connection`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                config_id: configId || null,
                config_group: "tts",
                provider_name: provider,
                base_url: baseUrl,
                api_key: apiKey || null
            })
        });
        const json = await res.json();
        showTTSTestResult(json.success, json.message);

        if (pingStatusEl) {
            if (json.success) {
                pingStatusEl.innerHTML = `
                    <span style="color: #10B981; font-weight: 600; display: inline-flex; align-items: center; gap: 5px;">
                        <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #10B981;"></span>
                        连通正常 · 延迟: ${json.latency_ms || 120}ms
                    </span>
                `;
                fetchAndRenderTTSThirdpartyModelInfo(baseUrl, apiKey, provider, configId);
            } else {
                pingStatusEl.innerHTML = `
                    <span style="color: #EF4444; font-weight: 500; display: inline-flex; align-items: center; gap: 5px;">
                        <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #EF4444;"></span>
                        连通失败 (${json.http_status || '超时/不可达'})
                    </span>
                `;
            }
        }

        // 步骤 2: 连通探测正常后，立即请求后端合成并播放试听语音
        if (json.success) {
            if (btn) {
                btn.innerHTML = `<span class="spinner-sm"></span> 正在合成试听语音...`;
            }
            try {
                const previewRes = await fetch(`${API_BASE}/settings/tts/preview`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        provider_name: provider,
                        voice_name: selectedVoice || null,
                        base_url: baseUrl || null,
                        api_key: apiKey || null,
                        text: "你好，欢迎来到直播间！这是当前语音引擎的实时试听效果，祝您开播顺利！"
                    })
                });

                if (previewRes.ok) {
                    if (window._ttsPreviewController && typeof window._ttsPreviewController.stopAll === "function") {
                        window._ttsPreviewController.stopAll();
                    }
                    const blob = await previewRes.blob();
                    const audioUrl = URL.createObjectURL(blob);
                    const audio = new Audio(audioUrl);
                    if (window._ttsPreviewController) {
                        window._ttsPreviewController.audio = audio;
                    }

                    if (btn) {
                        btn.innerHTML = `🔊 正在试听播报中...`;
                    }
                    if (pingStatusEl) {
                        pingStatusEl.innerHTML = `
                            <span style="color: #38BDF8; font-weight: 600; display: inline-flex; align-items: center; gap: 5px;">
                                <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #38BDF8;"></span>
                                正在播放试听效果 (${selectedVoice || '默认音色'}) · 延迟: ${json.latency_ms || 120}ms
                            </span>
                        `;
                    }

                    audio.onended = () => {
                        if (pingStatusEl) {
                            pingStatusEl.innerHTML = `
                                <span style="color: #10B981; font-weight: 600; display: inline-flex; align-items: center; gap: 5px;">
                                    <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #10B981;"></span>
                                    试听播报完成 · 服务连通正常 (${json.latency_ms || 120}ms)
                                </span>
                            `;
                        }
                    };

                    await audio.play();
                } else {
                    const err = await previewRes.json().catch(() => ({}));
                    console.warn("试听音频生成提示:", err);
                }
            } catch (audioErr) {
                console.warn("试听播放受阻 (如浏览器自动播放策略拦截):", audioErr);
            }
        }
    } catch (e) {
        showTTSTestResult(false, `测试 TTS 连通异常: ${e}`);
        if (pingStatusEl) pingStatusEl.innerHTML = `<span style="color: #EF4444;">握手异常</span>`;
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `<svg class="icon-sm" viewBox="0 0 24 24" style="width: 14px; height: 14px;"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/></svg> 测试连通性与试听`;
        }
    }
}

// 提示结果条
function showTTSTestResult(isSuccess, message) {
    const box = document.getElementById("tts-test-result");
    if (!box) return;
    box.style.display = "flex";
    box.className = "llm-test-result-box " + (isSuccess ? "success" : "error");
    box.innerHTML = `
        <span>${isSuccess ? '✔' : '✖'}</span>
        <span>${message}</span>
    `;
}

// 全局内置知名 TTS 引擎音色友好中文映射字典（彻底屏蔽底层英文/拼音 ID）
const TTS_VOICE_FRIENDLY_NAMES = {
    // Edge-TTS 官方音色
    "zh-CN-XiaoxiaoNeural": "晓晓 (超自然知性女声)",
    "zh-CN-YunxiNeural": "云希 (活力阳光男声)",
    "zh-CN-YunjianNeural": "云健 (激情带货男声)",
    "zh-CN-XiaoyiNeural": "晓伊 (亲切带货女声)",
    "zh-CN-YunyangNeural": "云扬 (专业新闻男声)",
    "zh-CN-XiaochenNeural": "晓辰 (开朗自然女声)",
    "zh-CN-XiaohanNeural": "晓涵 (知性温柔女声)",
    "zh-CN-XiaomengNeural": "晓梦 (甜美软萌女声)",
    "zh-CN-XiaomoNeural": "晓墨 (生动故事女声)",
    "zh-CN-XiaoqiuNeural": "晓秋 (沉稳知性女声)",
    "zh-CN-XiaoruiNeural": "晓睿 (阳光少儿女声)",
    "zh-CN-XiaoxuanNeural": "晓萱 (元气自信女声)",
    "zh-CN-XiaoyanNeural": "晓颜 (清脆悦耳女声)",
    "zh-CN-XiaoyouNeural": "晓悠 (灵动可爱童声)",
    "zh-CN-YunfengNeural": "云枫 (年轻阳光男声)",
    "zh-CN-YunhaoNeural": "云皓 (稳重磁性男声)",
    "zh-CN-YunxiaNeural": "云夏 (朝气清爽男声)",
    "zh-CN-YunyeNeural": "云野 (成熟沉稳男声)",
    "zh-CN-YunzeNeural": "云泽 (磁性故事男声)",
    "zh-HK-HiuMaanNeural": "晓曼 (粤语女声)",
    "zh-HK-WanLungNeural": "云龙 (粤语男声)",
    "zh-TW-HsiaoChenNeural": "晓臻 (台湾国语女声)",
    "zh-TW-YunJheNeural": "云哲 (台湾国语男声)",

    // 阿里云百炼 / CosyVoice
    "longxiaochun": "龙小春 (知性女声)",
    "longxiaoxia": "龙小夏 (甜美直播女声)",
    "longwan": "龙婉 (温柔女主播)",
    "longcheng": "龙诚 (稳重大气男声)",
    "longhua": "龙华 (阳光亲切男声)",
    "longshu": "龙书 (温和沉稳男声)",
    "longshuo": "龙硕 (激情带货男声)",
    "longjing": "龙静 (知性解说女声)",
    "longmiao": "龙妙 (甜美萌音女声)",
    "longyue": "龙悦 (温暖阳光女声)",
    "longyuan": "龙渊 (沉稳旁白男声)",
    "longfei": "龙飞 (活力主持男声)",
    "longjie": "龙杰 (干练解说男声)",
    "longling": "龙玲 (亲切客服女声)",
    "longtian": "龙天 (活力带货男声)",

    // ChatTTS
    "female_warm": "温暖知性女主播",
    "female_sweet": "甜美活力带货女声",
    "male_magnetic": "低沉磁性男主播",
    "male_narrator": "质感旁白男主播",

    // 系统通用
    "voice_default_female": "通用播报女声"
};

// 保存当前 TTS 配置
async function handleSaveCurrentTTS() {
    const saveBtn = document.getElementById("btn-save-tts");
    const statusTip = document.getElementById("tts-save-btn-status");
    const origBtnHtml = saveBtn ? saveBtn.innerHTML : "保存此语音配置";

    const urlInput = document.getElementById("tts-input-url");
    const voiceInput = document.getElementById("tts-input-voice");
    const keyInput = document.getElementById("tts-input-key");
    const providerInput = document.getElementById("tts-editor-provider");
    const configIdInput = document.getElementById("tts-editor-config-id");

    const baseUrl = urlInput ? urlInput.value.trim() : "";
    let voiceVal = voiceInput ? voiceInput.value.trim() : "";
    const apiKey = keyInput ? keyInput.value.trim() : "";
    const provider = providerInput ? providerInput.value : currentSelectedTTSProvider;
    const configId = configIdInput ? configIdInput.value : "";

    const meta = resolveTTSProviderMeta(provider);
    if (meta.needUrl && !baseUrl) {
        if (typeof showToast === "function") showToast("该语音引擎需填写服务 Base URL 地址！", "warning");
        else alert("该语音引擎需填写服务 Base URL 地址！");
        return;
    }

    // 若未选定音色，自动智能从页面药丸或预设推荐列表中选取第一个，杜绝拦截卡死
    if (!voiceVal) {
        const activePill = document.querySelector("#tts-fetched-voices-container .fetched-model-pill.selected") ||
                           document.querySelector("#tts-fetched-voices-container .fetched-model-pill");
        if (activePill) {
            voiceVal = activePill.getAttribute("data-voice") || "";
            activePill.classList.add("selected");
        } else if (meta.recommendedVoices && meta.recommendedVoices.length > 0) {
            voiceVal = meta.recommendedVoices[0].val;
        }
        if (voiceInput && voiceVal) {
            voiceInput.value = voiceVal;
        }
    }

    if (!voiceVal) {
        if (typeof showToast === "function") showToast("请先点击上方「获取音色与模型」加载可用音色后再进行保存！", "warning");
        else alert("请先点击上方「获取音色与模型」加载可用音色后再进行保存！");
        return;
    }

    // 切换按钮加载态
    if (saveBtn) {
        saveBtn.disabled = true;
        saveBtn.innerHTML = `<span style="display:inline-block;width:12px;height:12px;border:2px solid #fff;border-top-color:transparent;border-radius:50%;animation:spin 0.8s linear infinite;margin-right:6px;vertical-align:middle;"></span> 正在保存配置并同步入库...`;
    }
    if (statusTip) {
        statusTip.style.display = "inline";
        statusTip.style.color = "#38BDF8";
        statusTip.innerText = "正在保存并同步入库中...";
    }

    const ttsConfigs = cachedAllConfigs.filter(c => c.config_group === "tts");
    let isDefault = false;
    if (configId) {
        const exist = cachedAllConfigs.find(c => c.id === configId);
        isDefault = exist ? Boolean(exist.is_active) : false;
    } else {
        isDefault = ttsConfigs.length === 0;
    }

    // 智能解析选定音色的纯净中文名称与完整标签，存入 extra_params 持久化
    let chosenVoiceName = "";
    let chosenVoiceLabel = "";
    if (window._currentSelectedTTSVoiceMeta && window._currentSelectedTTSVoiceMeta.voiceVal === voiceVal) {
        chosenVoiceName = window._currentSelectedTTSVoiceMeta.voiceName;
        chosenVoiceLabel = window._currentSelectedTTSVoiceMeta.voiceLabel;
    } else {
        const resolved = resolveTTSVoiceInfo({ model_name: voiceVal, provider_name: provider });
        chosenVoiceName = resolved.name;
        chosenVoiceLabel = resolved.fullName;
    }

    const payload = {
        id: configId || null,
        config_group: "tts",
        provider_name: provider,
        base_url: baseUrl,
        model_name: voiceVal,
        is_active: isDefault,
        extra_params: {
            voice_name: chosenVoiceName || "",
            voice_label: chosenVoiceLabel || ""
        }
    };
    if (apiKey) payload.api_key = apiKey;

    try {
        const res = await fetch(`${API_BASE}/settings/configs/save`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        const json = await res.json();
        if (json.code === 0) {
            // 同步将获取到的全部具体音色（官方自带 + 专属克隆）批量保存到数据库（音色资产库）
            let syncedCount = 0;
            try {
                const voicesToSync = [];
                const seenIds = new Set();

                // 1. 优先从 window._latestFetchedVoices 收集
                const latest = window._latestFetchedVoices;
                if (latest && latest.provider === provider) {
                    (latest.clonedVoices || []).forEach(cv => {
                        if (cv.id && !seenIds.has(cv.id)) {
                            seenIds.add(cv.id);
                            voicesToSync.push({
                                id: cv.id,
                                name: cv.name || cv.id,
                                provider_name: provider,
                                voice_type: "cloned"
                            });
                        }
                    });
                    (latest.recommendedVoices || []).forEach(rv => {
                        if (rv.val && !seenIds.has(rv.val)) {
                            seenIds.add(rv.val);
                            const cleanName = (rv.text || "").replace(/^[👑\s]+/, "").split(/[\(\（]/)[0].trim() || rv.text;
                            voicesToSync.push({
                                id: rv.val,
                                name: cleanName || rv.val,
                                provider_name: provider,
                                voice_type: "preset"
                            });
                        }
                    });
                }

                // 2. 补漏：从 meta.recommendedVoices 收集官方预设
                (meta.recommendedVoices || []).forEach(rv => {
                    if (rv.val && !seenIds.has(rv.val)) {
                        seenIds.add(rv.val);
                        const cleanName = (rv.text || "").replace(/^[👑\s]+/, "").split(/[\(\（]/)[0].trim() || rv.text;
                        voicesToSync.push({
                            id: rv.val,
                            name: cleanName || rv.val,
                            provider_name: provider,
                            voice_type: "preset"
                        });
                    }
                });

                // 3. 从 DOM 药丸中收集用户专属绑定的音色
                document.querySelectorAll("#tts-fetched-voices-container .fetched-model-pill").forEach(p => {
                    const vid = p.getAttribute("data-voice");
                    if (vid && !seenIds.has(vid)) {
                        seenIds.add(vid);
                        const isClone = p.getAttribute("data-is-clone") === "true";
                        const nameSpan = p.querySelector("span:not(.btn-delete-clone-pill)") || p;
                        const rawText = nameSpan.textContent.trim();
                        const cleanName = rawText.replace(/^[👑\s]+/, "").split(/[\(\（]/)[0].trim() || rawText;
                        voicesToSync.push({
                            id: vid,
                            name: cleanName || vid,
                            provider_name: provider,
                            voice_type: isClone ? "cloned" : "preset"
                        });
                    }
                });

                if (voicesToSync.length > 0) {
                    const syncRes = await fetch(`${API_BASE}/voices/batch-sync`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            provider_name: provider,
                            voices: voicesToSync
                        })
                    });
                    const syncJson = await syncRes.json();
                    if (syncJson.code === 0) {
                        syncedCount = syncJson.total || voicesToSync.length;
                    }
                }
            } catch (errSync) {
                console.warn("同步保存音色资产库异常:", errSync);
            }

            const syncTip = syncedCount > 0 ? `，并已同步 ${syncedCount} 款音色入库至【音色资产库】` : "";
            const successMsg = `语音配置已成功保存${syncTip}！${isDefault ? '已设为当前直播默认发音。' : '可在下方列表卡片右上角随时设为默认发音。'}`;
            showTTSTestResult(true, successMsg);
            if (typeof showToast === "function") {
                showToast(successMsg, "success", 4500);
            }
            if (statusTip) {
                statusTip.style.color = "#10B981";
                statusTip.innerHTML = `✓ 语音配置已保存${syncedCount > 0 ? ` (已同步 ${syncedCount} 款音色)` : ''}`;
                setTimeout(() => { if (statusTip) statusTip.style.display = "none"; }, 6000);
            }

            await loadSettings();
            if (typeof loadVoiceTable === "function") {
                await loadVoiceTable();
            }
            if (typeof populateAnchorVoiceSelect === "function") {
                await populateAnchorVoiceSelect(window._voiceProfilesCache || (typeof voiceCache !== "undefined" ? voiceCache : []));
            }
        } else {
            showTTSTestResult(false, "保存失败: " + json.message);
            if (typeof showToast === "function") showToast("保存失败: " + json.message, "danger");
            if (statusTip) {
                statusTip.style.color = "#EF4444";
                statusTip.innerText = "保存失败: " + json.message;
            }
        }
    } catch (e) {
        showTTSTestResult(false, "保存语音引擎出错: " + e);
        if (typeof showToast === "function") showToast("保存语音引擎出错: " + e, "danger");
        if (statusTip) {
            statusTip.style.color = "#EF4444";
            statusTip.innerText = "保存异常: " + e;
        }
    } finally {
        if (saveBtn) {
            saveBtn.disabled = false;
            saveBtn.innerHTML = origBtnHtml;
        }
    }
}

// ============================================================================
// 全局解析指定 TTS 配置项的友好音色名称（彻底隐藏底层机器ID，以中文优雅名称展示）
// ============================================================================
function resolveTTSVoiceInfo(cfg, voiceProfiles = null) {
    const voiceVal = (cfg && cfg.model_name ? cfg.model_name : "").trim();
    const provider = (cfg && cfg.provider_name ? cfg.provider_name : "").toLowerCase();

    // 1. 优先读取 extra_params 中显式保存的友好名称
    let extra = {};
    if (cfg && cfg.extra_params) {
        try {
            extra = typeof cfg.extra_params === "string" ? JSON.parse(cfg.extra_params) : cfg.extra_params;
        } catch (e) { }
    }
    if (extra && extra.voice_name && typeof extra.voice_name === "string" && extra.voice_name.trim()) {
        const savedName = extra.voice_name.trim();
        if (!savedName.startsWith("zh-CN-") && !savedName.startsWith("zh-HK-") && !savedName.startsWith("zh-TW-")) {
            const isCloneVoice = Boolean(extra.is_clone || (voiceVal && (voiceVal.includes("cloned") || voiceVal.includes("bailian"))));
            return {
                name: savedName,
                fullName: extra.voice_label || savedName,
                isClone: isCloneVoice,
                voiceId: voiceVal
            };
        }
    }

    // 2. 尝试从系统登记的声音档案库 (VoiceProfile) 中匹配
    const profiles = Array.isArray(voiceProfiles) && voiceProfiles.length > 0
        ? voiceProfiles
        : (Array.isArray(window._voiceProfilesCache) && window._voiceProfilesCache.length > 0
            ? window._voiceProfilesCache
            : (typeof voiceCache !== "undefined" && Array.isArray(voiceCache) ? voiceCache : []));

    if (voiceVal) {
        const matchedProfile = profiles.find(v => (v.id && v.id === voiceVal) || (v.voice_id && v.voice_id === voiceVal));
        if (matchedProfile && matchedProfile.name && !matchedProfile.name.startsWith("zh-CN-")) {
            const isClone = matchedProfile.voice_type === "cloned" || matchedProfile.is_clone;
            return {
                name: matchedProfile.name.trim(),
                fullName: `${matchedProfile.name.trim()}${isClone ? ' (专属克隆)' : ' (官方预设)'}`,
                isClone: Boolean(isClone),
                voiceId: voiceVal
            };
        }
    }

    // 3. 从全局友好音色字典匹配 (TTS_VOICE_FRIENDLY_NAMES)
    if (voiceVal && TTS_VOICE_FRIENDLY_NAMES[voiceVal]) {
        const fullDesc = TTS_VOICE_FRIENDLY_NAMES[voiceVal];
        const cleanName = fullDesc.split(/[\(\（]/)[0].trim();
        return {
            name: cleanName,
            fullName: fullDesc,
            isClone: false,
            voiceId: voiceVal
        };
    }

    // 4. 尝试从预设生态推荐音色库中匹配 (recommendedVoices)
    const pMeta = typeof resolveTTSProviderMeta === "function" ? resolveTTSProviderMeta(provider, cfg) : null;
    const recs = (pMeta && pMeta.recommendedVoices) ? pMeta.recommendedVoices : [];
    let matchedRec = recs.find(v => v.val === voiceVal);

    if (!matchedRec && typeof BUILTIN_TTS_ECOSYSTEM !== "undefined") {
        for (const eco of BUILTIN_TTS_ECOSYSTEM) {
            matchedRec = (eco.recommendedVoices || []).find(v => v.val === voiceVal);
            if (matchedRec) break;
        }
    }

    if (matchedRec) {
        const cleanName = (matchedRec.text || "").replace(/^[👑\s]+/, "").split(/[\(\（]/)[0].trim() || matchedRec.text;
        return {
            name: cleanName,
            fullName: matchedRec.text,
            isClone: false,
            voiceId: voiceVal
        };
    }

    // 5. 针对 Edge-TTS 的正则中文名称智能提取 (例如 zh-CN-XiaoxiaoNeural -> 晓晓)
    if (voiceVal.startsWith("zh-")) {
        const matchZh = voiceVal.match(/zh-[A-Za-z]+-([A-Za-z]+)Neural/i);
        if (matchZh && matchZh[1]) {
            const nameRaw = matchZh[1];
            const pinyinMap = {
                "Xiaoxiao": "晓晓", "Yunxi": "云希", "Yunjian": "云健", "Xiaoyi": "晓伊",
                "Yunyang": "云扬", "Xiaochen": "晓辰", "Xiaohan": "晓涵", "Xiaomeng": "晓梦",
                "Xiaomo": "晓墨", "Xiaoqiu": "晓秋", "Xiaorui": "晓睿", "Xiaoxuan": "晓萱",
                "Xiaoyan": "晓颜", "Xiaoyou": "晓悠", "Yunfeng": "云枫", "Yunhao": "云皓",
                "Yunxia": "云夏", "Yunye": "云野", "Yunze": "云泽"
            };
            const zhName = pinyinMap[nameRaw] || nameRaw;
            return {
                name: zhName,
                fullName: `${zhName} (超自然微软云语音)`,
                isClone: false,
                voiceId: voiceVal
            };
        }
    }

    // 6. 针对百炼/CosyVoice 专属 Voice-ID 的智能语义识别
    const isBailianOrCosy = provider.includes("cosy") || voiceVal.includes("bailian") || voiceVal.includes("cosyvoice") || voiceVal.includes("qwen-audio");
    if (isBailianOrCosy && voiceVal) {
        const realClones = profiles.filter(v => (v.id || "").toLowerCase() !== "voice_default_female");
        if (realClones.length > 0) {
            if (realClones.length === 1 && realClones[0].name) {
                return {
                    name: realClones[0].name.trim(),
                    fullName: `${realClones[0].name.trim()} (专属克隆)`,
                    isClone: true,
                    voiceId: voiceVal
                };
            }
            const similar = realClones.find(v => (v.id && voiceVal.includes(v.id.substring(0, 16))) || (v.id && v.id.includes(voiceVal.substring(0, 16))));
            if (similar && similar.name) {
                return {
                    name: similar.name.trim(),
                    fullName: `${similar.name.trim()} (专属克隆)`,
                    isClone: true,
                    voiceId: voiceVal
                };
            }
        }
        return {
            name: "专属克隆音色",
            fullName: "百炼专属克隆音色",
            isClone: true,
            voiceId: voiceVal
        };
    }

    // 7. 终极保护：杜绝暴露生硬代码
    if (!voiceVal) {
        return { name: "默认音色", fullName: "默认音色", isClone: false, voiceId: "" };
    }
    return {
        name: "官方推荐音色",
        fullName: `官方推荐音色 (${voiceVal})`,
        isClone: false,
        voiceId: voiceVal
    };
}

// 渲染已配置的 TTS 清单 (100% 真实数据驱动)
function renderConfiguredTTS(configs) {
    const container = document.getElementById("configured-tts-list");
    const countBadge = document.getElementById("configured-tts-count");
    if (!container) return;

    // 异步确保声音档案库缓存就绪并静默更新
    if (!window._voiceProfilesCache && !window._fetchingVoiceProfiles) {
        window._fetchingVoiceProfiles = true;
        fetch(`${API_BASE}/voices/list`)
            .then(res => res.json())
            .then(json => {
                if (json.code === 0 && Array.isArray(json.data)) {
                    window._voiceProfilesCache = json.data;
                    if (typeof voiceCache !== "undefined") voiceCache = json.data;
                    renderConfiguredTTS(configs);
                }
            })
            .catch(() => {})
            .finally(() => {
                window._fetchingVoiceProfiles = false;
            });
    }

    container.style.display = "grid";
    container.style.gridTemplateColumns = "repeat(auto-fill, minmax(320px, 1fr))";
    container.style.gap = "14px";
    container.style.marginTop = "14px";

    const ttsConfigs = (configs || []).filter(c => c.config_group === "tts");
    if (countBadge) countBadge.innerText = `${ttsConfigs.length} 个已配置`;

    if (ttsConfigs.length === 0) {
        container.innerHTML = `
            <div style="grid-column: 1 / -1; text-align: center; padding: 28px 20px; color: var(--text-muted); background: rgba(0,0,0,0.18); border-radius: 8px; border: 1px dashed var(--border-subtle);">
                <div style="font-size: 14px; color: var(--text-secondary); margin-bottom: 6px; font-weight: 600;">暂无已配置的语音引擎</div>
                <div style="font-size: 12px;">系统绝不预设虚假配置。请在上方选择语音引擎（如 Edge-TTS、CosyVoice 等），点击第三行「获取音色」选定声线后，点击「保存此语音配置」。</div>
            </div>
        `;
        return;
    }

    container.innerHTML = "";
    ttsConfigs.forEach(cfg => {
        const tMeta = resolveTTSProviderMeta(cfg.provider_name, cfg);
        const card = document.createElement("div");
        card.className = "configured-tts-card" + (cfg.is_active ? " is-active" : "");

        const voiceInfo = resolveTTSVoiceInfo(cfg, window._voiceProfilesCache || (typeof voiceCache !== "undefined" ? voiceCache : []));

        const topCornerHtml = cfg.is_active
            ? `<div class="badge-active-brain">★ 默认生效发音</div>`
            : `<button class="btn-card-set-default-tts" onclick="handleSetActiveTTS('${cfg.id}')" title="设为当前直播默认发音">
                 <svg viewBox="0 0 24 24" style="width: 12px; height: 12px; stroke-width: 2.2;"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>
                 设为默认发音
               </button>`;

        const configuredLogoSrc = (tMeta.logoSvg || "").startsWith("http") ? tMeta.logoSvg : (tMeta.logoSvg + "?v=20260917_v6");
        
        // 音色名称药丸徽标：专属克隆使用尊贵金色高亮并带皇冠，官方使用自然微光绿徽标，悬停展示完整音色名与底层 ID
        const voiceBadgeHtml = voiceInfo.isClone
            ? `<span class="brand-badge green" style="font-size: 11px; padding: 2px 7px; font-weight: 600; border-color: rgba(245, 158, 11, 0.45); background: rgba(245, 158, 11, 0.12); color: #FBBF24; display: inline-flex; align-items: center; gap: 4px;" title="专属声音克隆: ${escapeHtml(voiceInfo.fullName)} | Voice-ID: ${escapeHtml(voiceInfo.voiceId || cfg.model_name)}">
                 <span style="font-size: 11px; line-height: 1;">👑</span>
                 <span>${escapeHtml(voiceInfo.name)}</span>
               </span>`
            : `<span class="brand-badge green" style="font-size: 11px; padding: 2px 7px; font-weight: 600;" title="发音音色: ${escapeHtml(voiceInfo.fullName)} | ID: ${escapeHtml(voiceInfo.voiceId || cfg.model_name)}">
                 ${escapeHtml(voiceInfo.name)}
               </span>`;

        card.innerHTML = `
            <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; width: 100%; min-width: 0; box-sizing: border-box;">
                <div class="configured-llm-info">
                    <img src="${configuredLogoSrc}" alt="${tMeta.name}" style="width: 42px; height: 42px; border-radius: 10px; object-fit: cover; flex-shrink: 0; background: transparent; border: none; padding: 0; box-shadow: 0 4px 12px rgba(0,0,0,0.35);">
                    <div style="min-width: 0; flex: 1;">
                        <div style="display: flex; align-items: center; gap: 6px; flex-wrap: wrap;">
                            <strong style="font-size: 14px; color: #FFFFFF;">${tMeta.name}</strong>
                            ${voiceBadgeHtml}
                        </div>
                        <div style="font-size: 11.5px; color: var(--text-muted); margin-top: 3px; font-family: var(--font-mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${escapeHtml(cfg.base_url || '内置直连')}">
                            ${escapeHtml(cfg.base_url || '微软云端直连免配置')}
                        </div>
                    </div>
                </div>
                <div>${topCornerHtml}</div>
            </div>

            <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 10px; padding-top: 10px; border-top: 1px dashed rgba(148, 163, 184, 0.15); width: 100%; box-sizing: border-box;">
                <button class="btn btn-xs" onclick="handlePingSingleConfig('${cfg.id}', this)" style="display: inline-flex; align-items: center; gap: 4px;">
                    <svg class="icon-sm" viewBox="0 0 24 24" style="width: 11px; height: 11px;"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
                    测速
                </button>
                <div style="display: flex; gap: 6px;">
                    <button class="btn btn-xs" onclick="handleEditConfiguredTTS('${cfg.id}')" title="载入上方编辑面板">
                        编辑
                    </button>
                    <button class="btn btn-xs" style="color: #F87171; border-color: rgba(248, 113, 113, 0.3);" onclick="handleDeleteConfig('${cfg.id}')" title="从数据库删除">
                        删除
                    </button>
                </div>
            </div>
        `;
        container.appendChild(card);
    });
}

// 切换默认激活的 TTS
async function handleSetActiveTTS(configId) {
    try {
        const res = await fetch(`${API_BASE}/settings/configs/set-active`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ config_id: configId, config_group: "tts" })
        });
        const json = await res.json();
        if (json.code === 0) {
            showToast("已切换直播默认发音引擎！", "success");
            await loadSettings();
            if (typeof populateAnchorVoiceSelect === "function") {
                await populateAnchorVoiceSelect(window._voiceProfilesCache || (typeof voiceCache !== "undefined" ? voiceCache : []));
            }
        } else {
            alert("设置失败: " + json.message);
        }
    } catch (e) {
        alert("操作异常: " + e);
    }
}

// 编辑已配置的 TTS
function handleEditConfiguredTTS(configId) {
    const target = cachedAllConfigs.find(c => c.id === configId);
    if (!target) return;
    const tMeta = resolveTTSProviderMeta(target.provider_name, target);
    selectTTSProvider(tMeta.id, target);

    const editor = document.getElementById("tts-editor-box");
    if (editor) editor.scrollIntoView({ behavior: "smooth", block: "center" });
}

// 展开/收起扩展自定义 / 远程 GPU 框
function toggleAddCustomTTSForm() {
    const box = document.getElementById("custom-tts-advanced-box");
    if (!box) return;
    box.style.display = box.style.display === "none" ? "block" : "none";
}

// 提交高级自定义服务商
async function submitNewCustomProvider() {
    const group = document.getElementById("new-cfg-group").value;
    const name = document.getElementById("new-provider-name").value.trim();
    const url = document.getElementById("new-base-url").value.trim();
    const model = document.getElementById("new-model-name").value.trim();
    const key = document.getElementById("new-api-key").value.trim();

    if (!name) {
        alert("请输入服务商标识（如 my_remote_gpu）");
        return;
    }

    try {
        const res = await fetch(`${API_BASE}/settings/configs/save`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                config_group: group,
                provider_name: name,
                base_url: url,
                model_name: model,
                api_key: key || null,
                is_active: true
            })
        });
        const json = await res.json();
        if (json.code === 0) {
            alert("自定义节点配置已成功添加！");
            toggleAddCustomTTSForm();
            loadSettings();
        } else {
            alert("添加失败: " + json.message);
        }
    } catch (e) {
        alert("提交异常: " + e);
    }
}
