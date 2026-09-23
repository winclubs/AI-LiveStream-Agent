import datetime
from sqlalchemy import Column, String, Integer, Float, DateTime, Text, ForeignKey, CheckConstraint, UniqueConstraint
from sqlalchemy.orm import declarative_base

def utc_now():
    """获取当前 UTC 时间（替代已废弃的 utc_now）"""
    return datetime.datetime.now(datetime.timezone.utc)

Base = declarative_base()

class AnchorRole(Base):
    """主播角色人设表"""
    __tablename__ = "anchor_roles"

    id = Column(String(64), primary_key=True)
    role_type = Column(String(32), nullable=False)  # ecommerce / entertainment / expert
    role_name = Column(String(128), nullable=False)
    system_prompt = Column(Text, nullable=False)
    default_voice_id = Column(String(64), ForeignKey("voice_profiles.id", ondelete="SET NULL"), nullable=True)
    speech_speed = Column(Float, default=1.0)
    pitch_shift = Column(Float, default=0.0)
    associated_guardrail_group = Column(String(64), default="general")
    is_active = Column(Integer, default=0)
    created_at = Column(DateTime, default=utc_now)

class Avatar(Base):
    """主播形象配置表"""
    __tablename__ = "avatars"

    id = Column(String(64), primary_key=True)
    name = Column(String(128), nullable=False)
    avatar_type = Column(String(32), default="image")  # image / video / live2d
    source_file_path = Column(String(512), nullable=True)
    preprocessed_cache_path = Column(String(512), nullable=True)
    created_at = Column(DateTime, default=utc_now)

class VoiceProfile(Base):
    """声音克隆档案表"""
    __tablename__ = "voice_profiles"

    id = Column(String(64), primary_key=True)
    name = Column(String(128), nullable=False)
    sample_wav_path = Column(String(512), nullable=True)
    embedding_npy_path = Column(String(512), nullable=True)
    speech_speed = Column(Float, default=1.0)
    volume_gain = Column(Float, default=1.0)
    status = Column(String(32), default="ready")  # pending / cloning / ready
    provider_name = Column(String(64), default="", nullable=True)  # 所属语音合成引擎 (如 edge_tts, cosyvoice)
    voice_type = Column(String(32), default="preset", nullable=True)  # preset(官方预设) / cloned(专属克隆)
    created_at = Column(DateTime, default=utc_now)

class Anchor(Base):
    """主播管理表：主播形象 + 绑定音色 + 备注 (多张照片)"""
    __tablename__ = "anchors"

    id = Column(String(64), primary_key=True)
    name = Column(String(128), nullable=False)
    anchor_type = Column(String(32), default="ecommerce", nullable=False)  # ecommerce / entertainment / expert / chat
    voice_id = Column(String(64), ForeignKey("voice_profiles.id", ondelete="SET NULL"), nullable=True)  # 绑定音色档案
    remark = Column(Text, default="")                         # 备注信息
    photo_portrait = Column(String(512), default="")          # 形象照(正面)
    photo_full_body = Column(String(512), default="")         # 全身照
    photo_half_body = Column(String(512), default="")         # 半身照
    photo_side = Column(String(512), default="")              # 侧面照
    avatar_asset_dir = Column(String(512), default="")        # 数字人切片资产目录
    source_video = Column(String(512), default="")            # 原始训练视频
    created_at = Column(DateTime, default=utc_now)

class AvatarTask(Base):
    """数字人视频切片与训练异步任务表"""
    __tablename__ = "avatar_tasks"

    id = Column(String(64), primary_key=True)
    anchor_id = Column(String(64), ForeignKey("anchors.id", ondelete="SET NULL"), nullable=True)
    name = Column(String(128), nullable=False)
    status = Column(String(32), default="pending", nullable=False)  # pending / processing / completed / failed
    progress = Column(Integer, default=0, nullable=False)           # 0 ~ 100
    stage_message = Column(String(256), default="")
    video_path = Column(String(512), default="")
    output_dir = Column(String(512), default="")
    error_message = Column(Text, default="")
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)

class AvatarAction(Base):
    """数字人动作切片状态机与带货场景智能绑定表"""
    __tablename__ = "avatar_actions"

    id = Column(String(64), primary_key=True)
    anchor_id = Column(String(64), ForeignKey("anchors.id", ondelete="CASCADE"), nullable=True)
    action_code = Column(Integer, nullable=False)          # 0:待机, 1:欢迎, 2:点赞, 3:购物车, 4:致谢, 5+:自定义
    action_name = Column(String(128), nullable=False)
    video_path = Column(String(512), default="")
    frames_dir = Column(String(512), default="")
    trigger_type = Column(String(32), default="both")      # keyword / event / both / manual
    trigger_keywords = Column(Text, default="")            # 逗号分隔触发关键词
    trigger_events = Column(String(128), default="")       # gift / welcome / follow / order
    duration_sec = Column(Float, default=3.5)              # 动作持续秒数 (超时自动平滑回待机)
    priority = Column(Integer, default=1)                  # 动作优先级 (高优先可打断低优先)
    mirror_loop = Column(Integer, default=1)               # 是否启用镜像无缝平滑往返循环 (1:是, 0:否)
    is_active = Column(Integer, default=1)
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)

class AppSetting(Base):
    """应用级键值配置 (直播模式选择、向导完成标记等)"""
    __tablename__ = "app_settings"

    key = Column(String(64), primary_key=True)
    value = Column(Text, default="")
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)

class Product(Base):
    """电商商品表 (SKU Repository)"""
    __tablename__ = "products"

    id = Column(String(64), primary_key=True)
    sku_code = Column(String(64), unique=True, nullable=False)
    title = Column(String(256), nullable=False)
    category = Column(String(64), nullable=True)
    original_price = Column(Float, default=0.0)
    live_price = Column(Float, default=0.0)
    current_stock = Column(Integer, default=0)
    selling_points = Column(Text, default="[]")  # JSON 数组存储核心卖点
    faq_data = Column(Text, default="[]")        # JSON 存储常见问题
    size_chart = Column(Text, default="{}")       # JSON 存储尺码表
    coupon_script = Column(Text, default="")     # 催单逼电话术模版
    description = Column(Text, default="")       # 商品详细描述
    images = Column(Text, default="[]")          # 商品图片路径列表 (JSON)
    is_active = Column(Integer, default=1)

class ProhibitedWord(Base):
    """主播违禁词与合规规则表"""
    __tablename__ = "prohibited_words"

    id = Column(String(64), primary_key=True)
    word = Column(String(128), unique=True, nullable=False)
    category = Column(String(32), default="extreme")  # extreme / medical / traffic / competitor / sensitive
    role_scope = Column(String(32), default="all")    # all / ecommerce / entertainment / expert
    platform = Column(String(32), default="all", index=True)      # all / douyin / wechat / kuaishou / bilibili
    action_policy = Column(String(32), default="substitute")  # substitute / drop / alert
    replacement_word = Column(String(128), default="")
    is_enabled = Column(Integer, default=1)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)

class ProhibitedWordLog(Base):
    """违禁词触发审计日志表"""
    __tablename__ = "prohibited_word_logs"

    id = Column(String(64), primary_key=True)
    session_id = Column(String(64), nullable=True)
    matched_word = Column(String(128), nullable=False)
    category = Column(String(32), nullable=False)
    original_sentence = Column(Text, nullable=False)
    processed_sentence = Column(Text, nullable=True)
    action_taken = Column(String(32), nullable=False)
    created_at = Column(DateTime, default=utc_now)

class ApiProviderConfig(Base):
    """API 服务商与连接参数配置表"""
    __tablename__ = "api_provider_configs"

    id = Column(String(64), primary_key=True)
    config_group = Column(String(32), nullable=False)  # llm / tts / vision / remote_gpu / neural_renderer / live_fetcher
    provider_name = Column(String(64), nullable=False)
    is_active = Column(Integer, default=0)
    encrypted_api_key = Column(Text, default="")
    base_url = Column(String(512), default="")
    model_name = Column(String(128), default="")
    extra_params_json = Column(Text, default="{}")
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)

class BarrageLog(Base):
    """直播交互与弹幕审计日志表"""
    __tablename__ = "barrage_logs"

    id = Column(String(64), primary_key=True)
    session_id = Column(String(64), nullable=False)
    platform = Column(String(32), nullable=False)
    user_id = Column(String(64), nullable=True)
    user_nickname = Column(String(128), nullable=True)
    raw_message = Column(Text, nullable=False)
    event_type = Column(String(32), default="chat")  # chat / gift / follow
    priority_level = Column(Integer, default=2)
    ai_reply_text = Column(Text, nullable=True)
    response_latency_ms = Column(Integer, default=0)
    is_interrupted = Column(Integer, default=0)
    created_at = Column(DateTime, default=utc_now)

class KnowledgeChunk(Base):
    """本地 RAG 知识库文档分块表"""
    __tablename__ = "knowledge_chunks"

    id = Column(String(64), primary_key=True)
    doc_name = Column(String(256), nullable=False)     # 文档名称
    doc_id = Column(String(64), nullable=False, index=True)  # 同一文档的分块共享 ID
    chunk_index = Column(Integer, default=0)
    content = Column(Text, nullable=False)             # 分块正文
    source_path = Column(String(512), nullable=True)   # 原始文件路径
    created_at = Column(DateTime, default=utc_now)


class Order(Base):
    """直播成交订单；external_id 提供跨重试幂等语义。"""
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_orders_amount_nonnegative"),
        CheckConstraint("quantity > 0", name="ck_orders_quantity_positive"),
    )

    id = Column(String(64), primary_key=True)
    external_id = Column(String(128), unique=True, nullable=False, index=True)
    session_id = Column(String(64), ForeignKey("live_session_records.session_id", ondelete="CASCADE"), nullable=False, index=True)
    product_id = Column(String(64), ForeignKey("products.id", ondelete="SET NULL"), nullable=True)
    sku = Column(String(64), nullable=False)
    amount = Column(Float, nullable=False, default=0.0)
    quantity = Column(Integer, nullable=False, default=1)
    status = Column(String(24), nullable=False, default="completed")  # completed / cancelled / refunded
    refund_amount = Column(Float, nullable=False, default=0.0)
    note = Column(Text, default="")
    product_snapshot_json = Column(Text, default="{}")
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)


class InventoryMovement(Base):
    """库存变动审计流水，quantity_delta 为正表示回补。"""
    __tablename__ = "inventory_movements"

    id = Column(String(64), primary_key=True)
    order_id = Column(String(64), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id = Column(String(64), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True)
    quantity_delta = Column(Integer, nullable=False)
    reason = Column(String(32), nullable=False)
    created_at = Column(DateTime, default=utc_now)


class LiveSessionRecord(Base):
    """直播场次历史记录表。"""
    __tablename__ = "live_session_records"

    session_id = Column(String(64), primary_key=True)
    platform = Column(String(32), default="bilibili")
    theme = Column(String(128), default="")
    mode = Column(String(16), default="B")
    role_id = Column(String(64), ForeignKey("anchor_roles.id", ondelete="SET NULL"), nullable=True)
    voice_id = Column(String(64), ForeignKey("voice_profiles.id", ondelete="SET NULL"), nullable=True)
    anchor_id = Column(String(64), ForeignKey("anchors.id", ondelete="SET NULL"), nullable=True)
    product_snapshot_json = Column(Text, default="[]")
    status = Column(String(24), nullable=False, default="starting")  # starting / live / stopped / interrupted / failed
    start_time = Column(DateTime, default=utc_now)
    end_time = Column(DateTime, nullable=True)
    total_gmv = Column(Float, default=0.0)
    orders_count = Column(Integer, default=0)
    danmaku_count = Column(Integer, default=0)
    peak_viewers = Column(Integer, default=0)
    gift_income = Column(Float, default=0.0)

