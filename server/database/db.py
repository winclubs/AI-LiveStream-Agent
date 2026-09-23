import os
import json
import uuid
import struct
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select, update, event, text
from server.config import DATABASE_URL
from server.database.models import (
    Base, AnchorRole, ProhibitedWord, ApiProviderConfig, Avatar, VoiceProfile, KnowledgeChunk,
    LiveSessionRecord, utc_now
)

# 创建异步 SQLite 数据库引擎 (启用 WAL 模式)
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False}
)


@event.listens_for(engine.sync_engine, "connect")
def _configure_sqlite_connection(dbapi_connection, _connection_record):
    """所有连接统一启用引用约束和合理的锁等待。"""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()

AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)

async def get_db():
    """FastAPI 依赖注入：获取数据库会话"""
    async with AsyncSessionLocal() as session:
        yield session

async def init_db():
    """初始化数据库并注入预设默认数据"""
    from server.database.migrations import run_migrations
    async with engine.begin() as conn:
        # 启用 SQLite WAL 模式提升并发读写吞吐
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL;")
        await conn.run_sync(Base.metadata.create_all)
        # 执行版本化迁移脚本 (为旧库补列/索引，幂等安全)
        await run_migrations(conn)

    # 注入预设默认数据
    async with AsyncSessionLocal() as session:
        # 上次进程若异常退出，将遗留 live/starting 场次收敛为 interrupted。
        await session.execute(
            update(LiveSessionRecord)
            .where(LiveSessionRecord.status.in_(["starting", "live"]))
            .values(status="interrupted", end_time=utc_now())
        )
        # 1. 初始化预设角色 (增量注入：新增角色类型时旧库自动补插，不动用户自定义数据)
        existing_roles = await session.execute(select(AnchorRole))
        existing_role_ids = {r.id for r in existing_roles.scalars().all()}
        default_roles = [
            AnchorRole(
                id="role_ecommerce_default",
                role_type="ecommerce",
                role_name="金牌带货推荐官·艾米",
                system_prompt=(
                    "你是一位充满激情的顶流好物带货主播【艾米】。\n"
                    "【直播风格】：热情、亲切、节奏紧凑，善用'宝子们'、'家人们'等亲昵称呼。\n"
                    "【话术公式】：抓痛点 -> 抛卖点与专利背书 -> 竞品对比 -> 算超值优惠 -> 倒计时催付。\n"
                    "【禁忌红线】：严禁使用广告法极限词（如'全网第一'、'最'），严格依据商品库真实价格与库存解答。"
                ),
                speech_speed=1.1,
                pitch_shift=1.0,
                associated_guardrail_group="ecommerce",
                is_active=1
            ),
            AnchorRole(
                id="role_entertainment_default",
                role_type="entertainment",
                role_name="元气偶像陪伴主播·娜娜",
                system_prompt=(
                    "你是一位活泼幽默、高情商的陪伴型虚拟主播【娜娜】。\n"
                    "【直播风格】：亲切自然像老朋友，善于自嘲、接梗、共情，适度带入语气词（哈哈、哇塞、绝了）。\n"
                    "【互动循环】：遇到弹幕倾诉积极给予情绪价值；在冷场或观众要求时可发起脑筋急转弯或成语接龙互动；"
                    "遇到打赏第一时间热情鸣谢。"
                ),
                speech_speed=1.0,
                pitch_shift=0.0,
                associated_guardrail_group="entertainment",
                is_active=0
            ),
            AnchorRole(
                id="role_expert_default",
                role_type="expert",
                role_name="资深行业咨询专家·陈老师",
                system_prompt=(
                    "你是一位拥有15年从业经验的资深行业顾问【陈老师】。\n"
                    "【咨询风格】：严谨、儒雅、客观、逻辑清晰，回答问题采用分点阐述（背景焦点、法理/原理依据、执行建议）。\n"
                    "【合规前置】：回答个案问题时，前置附带'日常探讨建议，非正式代理/诊疗依据'温和说明。\n"
                    "【知识遵循】：严格基于企业 RAG 知识库内容回复，不确定时建议用户提供材料私信复核。"
                ),
                speech_speed=0.95,
                pitch_shift=-0.5,
                associated_guardrail_group="expert",
                is_active=0
            ),
            AnchorRole(
                id="role_chitchat_default",
                role_type="chitchat",
                role_name="闲聊扯淡搭子·老王",
                system_prompt=(
                    "你是一位特别能唠嗑的闲聊主播【老王】。\n"
                    "【直播风格】：像街坊邻居唠家常，轻松随意、大白话、想到啥聊啥，时事趣闻生活琐事都能扯。\n"
                    "【语言特征】：口语化接地气，常用'哎你说''我跟你说''可不是嘛'等唠嗑口头禅。\n"
                    "【互动原则】：观众聊什么就顺着接什么，主动抛新话题带动气氛绝不冷场；"
                    "涉及政治敏感、违法违规内容立即岔开话题。\n"
                    "【字数控制】：单次发言 30~50 字，像真人唠嗑一样自然。"
                ),
                speech_speed=1.0,
                pitch_shift=0.0,
                associated_guardrail_group="chitchat",
                is_active=0
            )
        ]
        for role in default_roles:
            if role.id not in existing_role_ids:
                session.add(role)

        # 2. 初始化预设广告法与多平台官方违禁词库 (通用 + 抖音 + 视频号 + 快手 + B站)
        words_check = await session.execute(select(ProhibitedWord))
        existing_words = {w.word for w in words_check.scalars().all()}

        system_preset_words = [
            # --- 【通用规则 (all)】广告法极限用语、虚假夸大、医疗功效 ---
            ProhibitedWord(id="pw_gen_1", word="全网第一", category="extreme", role_scope="all", platform="all", action_policy="substitute", replacement_word="深受大家喜爱"),
            ProhibitedWord(id="pw_gen_2", word="最顶尖", category="extreme", role_scope="all", platform="all", action_policy="substitute", replacement_word="非常出色"),
            ProhibitedWord(id="pw_gen_3", word="最好", category="extreme", role_scope="all", platform="all", action_policy="substitute", replacement_word="深得好评"),
            ProhibitedWord(id="pw_gen_4", word="顶级", category="extreme", role_scope="all", platform="all", action_policy="substitute", replacement_word="高品质"),
            ProhibitedWord(id="pw_gen_5", word="国家级", category="extreme", role_scope="all", platform="all", action_policy="drop", replacement_word=""),
            ProhibitedWord(id="pw_gen_6", word="销量第一", category="extreme", role_scope="all", platform="all", action_policy="substitute", replacement_word="热销爆款"),
            ProhibitedWord(id="pw_gen_7", word="天花板", category="extreme", role_scope="all", platform="all", action_policy="substitute", replacement_word="标杆之作"),
            ProhibitedWord(id="pw_gen_8", word="独一无二", category="extreme", role_scope="all", platform="all", action_policy="substitute", replacement_word="独具特色"),
            ProhibitedWord(id="pw_gen_9", word="100%有效", category="extreme", role_scope="all", platform="all", action_policy="substitute", replacement_word="多数用户反馈良好"),
            ProhibitedWord(id="pw_gen_10", word="全网最低价", category="extreme", role_scope="ecommerce", platform="all", action_policy="substitute", replacement_word="诚意超值福利"),
            ProhibitedWord(id="pw_gen_11", word="纯天然无毒副作用", category="medical", role_scope="all", platform="all", action_policy="substitute", replacement_word="甄选自然健康配料"),
            ProhibitedWord(id="pw_gen_12", word="根治", category="medical", role_scope="all", platform="all", action_policy="drop", replacement_word=""),
            ProhibitedWord(id="pw_gen_13", word="包治百病", category="medical", role_scope="all", platform="all", action_policy="drop", replacement_word=""),
            ProhibitedWord(id="pw_gen_14", word="降三高", category="medical", role_scope="all", platform="all", action_policy="drop", replacement_word=""),
            ProhibitedWord(id="pw_gen_15", word="抗癌", category="medical", role_scope="all", platform="all", action_policy="drop", replacement_word=""),
            ProhibitedWord(id="pw_gen_16", word="排毒养颜", category="medical", role_scope="all", platform="all", action_policy="substitute", replacement_word="焕发自然光采"),
            ProhibitedWord(id="pw_gen_17", word="包胜诉", category="expert", role_scope="expert", platform="all", action_policy="drop", replacement_word=""),

            # --- 【抖音规则 (douyin)】严打站外导流、谐音暗号规避、未报备低价与虚假紧迫感 ---
            ProhibitedWord(id="pw_dy_1", word="加我微信", category="traffic", role_scope="all", platform="douyin", action_policy="substitute", replacement_word="关注直播间或点击小黄车"),
            ProhibitedWord(id="pw_dy_2", word="私聊发微信", category="traffic", role_scope="all", platform="douyin", action_policy="drop", replacement_word=""),
            ProhibitedWord(id="pw_dy_3", word="看主页简介加V", category="traffic", role_scope="all", platform="douyin", action_policy="drop", replacement_word=""),
            ProhibitedWord(id="pw_dy_4", word="私下转账", category="traffic", role_scope="all", platform="douyin", action_policy="drop", replacement_word=""),
            ProhibitedWord(id="pw_dy_5", word="某宝", category="competitor", role_scope="all", platform="douyin", action_policy="drop", replacement_word=""),
            ProhibitedWord(id="pw_dy_6", word="拼夕夕", category="competitor", role_scope="all", platform="douyin", action_policy="drop", replacement_word=""),
            ProhibitedWord(id="pw_dy_7", word="抢疯了", category="extreme", role_scope="ecommerce", platform="douyin", action_policy="substitute", replacement_word="备受欢迎"),
            ProhibitedWord(id="pw_dy_8", word="随时涨价", category="extreme", role_scope="ecommerce", platform="douyin", action_policy="substitute", replacement_word="当前价格非常优惠"),
            ProhibitedWord(id="pw_dy_9", word="秒杀", category="extreme", role_scope="ecommerce", platform="douyin", action_policy="substitute", replacement_word="限时特惠"),

            # --- 【视频号规则 (wechat)】严禁私加个人号私下交易、严打玄学迷信与虚假疗效 ---
            ProhibitedWord(id="pw_wx_1", word="加我私人微信", category="traffic", role_scope="all", platform="wechat", action_policy="drop", replacement_word=""),
            ProhibitedWord(id="pw_wx_2", word="转账发货", category="traffic", role_scope="all", platform="wechat", action_policy="drop", replacement_word=""),
            ProhibitedWord(id="pw_wx_3", word="招财转运", category="sensitive", role_scope="all", platform="wechat", action_policy="drop", replacement_word=""),
            ProhibitedWord(id="pw_wx_4", word="旺夫", category="sensitive", role_scope="all", platform="wechat", action_policy="drop", replacement_word=""),
            ProhibitedWord(id="pw_wx_5", word="老中医秘方", category="medical", role_scope="all", platform="wechat", action_policy="drop", replacement_word=""),

            # --- 【快手规则 (kuaishou)】严打演戏剧本砍价、自残炒作、诱导打赏 ---
            ProhibitedWord(id="pw_ks_1", word="跟老板撕破脸", category="sensitive", role_scope="ecommerce", platform="kuaishou", action_policy="drop", replacement_word=""),
            ProhibitedWord(id="pw_ks_2", word="亏本自掏腰包", category="extreme", role_scope="ecommerce", platform="kuaishou", action_policy="substitute", replacement_word="拿出十足诚意让利"),
            ProhibitedWord(id="pw_ks_3", word="榜一大哥不刷不卖", category="traffic", role_scope="all", platform="kuaishou", action_policy="drop", replacement_word=""),

            # --- 【B站规则 (bilibili)】严打非报备外链商业广告、引战、账号私下倒卖 ---
            ProhibitedWord(id="pw_bili_1", word="私下交易账号", category="traffic", role_scope="all", platform="bilibili", action_policy="drop", replacement_word=""),
            ProhibitedWord(id="pw_bili_2", word="代练外挂", category="sensitive", role_scope="all", platform="bilibili", action_policy="drop", replacement_word=""),
            ProhibitedWord(id="pw_bili_3", word="加群私下购买", category="traffic", role_scope="all", platform="bilibili", action_policy="drop", replacement_word="")
        ]

        words_to_add = [w for w in system_preset_words if w.word not in existing_words]
        if words_to_add:
            session.add_all(words_to_add)

        # 3. 服务商配置 (LLM / TTS / 远程GPU 均为敏感生产配置，绝不预填任何虚假数据，完全由用户真实配置并保存)
        # 保持 api_provider_configs 真实为空，待用户在控制台显式配置并持久化

        # 4. 初始化目录结构 (未上传时保持真实文件系统干净，不伪造 1x1 或静音伪文件)
        from server.config import DATA_DIR
        os.makedirs(os.path.join(DATA_DIR, "avatars"), exist_ok=True)
        os.makedirs(os.path.join(DATA_DIR, "voices"), exist_ok=True)

        # 5. 初始化预设形象与音色记录 (无物理真实文件时设为 None，UI 引导真实上传；保证主播外键完整性)
        avatars_check = await session.execute(select(Avatar))
        if not avatars_check.scalars().first():
            session.add(Avatar(
                id="avatar_default_muse",
                name="标准虚拟主播形象·艾米",
                avatar_type="image",
                source_file_path=None,  # 未上传有效资产时诚实置为 None
                preprocessed_cache_path=None
            ))

        voices_check = await session.execute(select(VoiceProfile))
        if not voices_check.scalars().first():
            session.add(VoiceProfile(
                id="voice_default_female",
                name="通用亲和女声（官方预置）",
                sample_wav_path=None,  # 未上传真实录音采样时诚实置为 None
                embedding_npy_path=None,
                speech_speed=1.0,
                volume_gain=1.0
            ))

        # 数据清洗迁移：仅针对历史版本内置伪默认记录中指向不存在文件的路径进行重置，严禁误清空用户自定义数据
        legacy_avatar = await session.get(Avatar, "avatar_default_muse")
        if legacy_avatar and legacy_avatar.source_file_path:
            full_p = os.path.join(DATA_DIR, legacy_avatar.source_file_path) if not os.path.isabs(legacy_avatar.source_file_path) else legacy_avatar.source_file_path
            if not os.path.exists(full_p) and not os.path.exists(legacy_avatar.source_file_path):
                legacy_avatar.source_file_path = None
                legacy_avatar.preprocessed_cache_path = None

        legacy_voice = await session.get(VoiceProfile, "voice_default_female")
        if legacy_voice and legacy_voice.sample_wav_path:
            full_p = os.path.join(DATA_DIR, legacy_voice.sample_wav_path) if not os.path.isabs(legacy_voice.sample_wav_path) else legacy_voice.sample_wav_path
            if not os.path.exists(full_p) and not os.path.exists(legacy_voice.sample_wav_path):
                legacy_voice.sample_wav_path = None
                legacy_voice.embedding_npy_path = None

        # 6. 初始化预设专家知识库 (劳动法常用 FAQ，供 RAG 双路检索开箱可用)
        knowledge_check = await session.execute(select(KnowledgeChunk))
        if not knowledge_check.scalars().first():
            labor_faq = (
                "《劳动合同法》核心要点速查：\n"
                "经济补偿金按劳动者在本单位工作的年限计算，每满一年支付一个月工资，即 N；六个月以上不满一年的按一年计算；不满六个月的支付半个月工资。\n"
                "用人单位违法解除或终止劳动合同的，应按经济补偿标准的二倍向劳动者支付赔偿金，即 2N。\n"
                "用人单位自用工之日起超过一个月不满一年未与劳动者订立书面劳动合同的，应当向劳动者每月支付二倍的工资。\n"
                "劳动者申请劳动仲裁的时效期间为一年，从当事人知道或者应当知道其权利被侵害之日起计算。\n"
                "加班工资标准：工作日延长工作时间支付不低于工资的百分之一百五十；休息日安排工作又不能安排补休的支付不低于百分之二百；法定休假日安排工作的支付不低于百分之三百。"
            )
            contract_faq = (
                "合同纠纷核心要点速查：\n"
                "定金具有担保性质，给付定金一方不履行债务的无权请求返还，收受定金一方不履行债务的应当双倍返还定金；订金一般视为预付款，不适用定金罚则。\n"
                "当事人一方因不可抗力不能履行合同的，根据不可抗力的影响部分或者全部免除责任，但应当及时通知对方并在合理期限内提供证明。\n"
                "当事人一方迟延履行主要债务，经催告后在合理期限内仍未履行的，对方可以解除合同；主张解除合同应当通知对方，合同自通知到达对方时解除。"
            )
            for doc_name, body in [("劳动合同法核心要点", labor_faq), ("合同纠纷核心要点", contract_faq)]:
                doc_id = f"doc_{uuid.uuid4().hex[:8]}"
                for idx, para in enumerate([p.strip() for p in body.split("\n\n") if p.strip()] or [body]):
                    session.add(KnowledgeChunk(
                        id=f"kc_{uuid.uuid4().hex[:12]}",
                        doc_name=doc_name,
                        doc_id=doc_id,
                        chunk_index=idx,
                        content=para,
                        source_path="builtin_presets"
                    ))

        # 旧版本曾将全部预设角色标记为激活；首次升级时收敛为唯一激活项。
        active_rows = (await session.execute(
            select(AnchorRole).where(AnchorRole.is_active == 1).order_by(AnchorRole.created_at, AnchorRole.id)
        )).scalars().all()
        if not active_rows:
            fallback = await session.get(AnchorRole, "role_ecommerce_default")
            if fallback:
                fallback.is_active = 1
        elif len(active_rows) > 1:
            keep_id = active_rows[0].id
            await session.execute(
                update(AnchorRole).where(AnchorRole.id != keep_id).values(is_active=0)
            )

        await session.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_anchor_roles_single_active "
            "ON anchor_roles(is_active) WHERE is_active = 1"
        ))
        await session.commit()

    # 数据库是角色激活状态的权威源，启动时恢复全部自定义角色。
    from server.core.roles.role_manager import global_role_manager
    global_role_manager.reset_to_defaults()
    await global_role_manager.load_from_db()

