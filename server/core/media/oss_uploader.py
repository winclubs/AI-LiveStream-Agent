# -*- coding: utf-8 -*-
"""
阿里云 OSS 云端录像存储与分发引擎 (AliyunOSSUploader)
实现功能：
1. 阿里云 OSS 凭据安全读写与连通性探活；
2. 直播结束后音视频录像异步上传与断点续传；
3. 私有/公共读 Bucket 防盗链临时签名播放/下载 URL 生成；
4. 数据库 live_session_records 状态实时回写与异步重试机制。
"""
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from sqlalchemy import select, update

from server.config import decrypt_secret, encrypt_secret, mask_api_key
from server.database.db import AsyncSessionLocal
from server.database.models import ApiProviderConfig, LiveSessionRecord

logger = logging.getLogger("LiveAgent.OSSUploader")

# 尝试导入官方 oss2 SDK
try:
    import oss2
    OSS2_AVAILABLE = True
except ImportError:
    oss2 = None
    OSS2_AVAILABLE = False


class AliyunOSSUploader:
    """阿里云 OSS 云端存储管理器单例"""

    CONFIG_ID = "aliyun_oss_default"
    CONFIG_GROUP = "oss"
    PROVIDER_NAME = "aliyun_oss"

    def __init__(self):
        self._cached_config: Optional[Dict[str, Any]] = None
        self._cache_time: float = 0.0

    @staticmethod
    def normalize_endpoint(endpoint: str) -> str:
        """规范化 Endpoint 地址，自动补充 http/https 前缀"""
        if not endpoint:
            return ""
        ep = endpoint.strip()
        if not ep.startswith("http://") and not ep.startswith("https://"):
            ep = f"https://{ep}"
        return ep.rstrip("/")

    async def get_config(self, mask_secret: bool = True) -> Dict[str, Any]:
        """从数据库读取 OSS 配置"""
        async with AsyncSessionLocal() as db:
            stmt = select(ApiProviderConfig).where(
                ApiProviderConfig.config_group == self.CONFIG_GROUP,
                ApiProviderConfig.provider_name == self.PROVIDER_NAME,
            ).limit(1)
            res = await db.execute(stmt)
            cfg = res.scalar_one_or_none()

        if not cfg:
            return {
                "configured": False,
                "endpoint": "",
                "bucket": "",
                "access_key_id": "",
                "access_key_secret": "",
                "custom_domain": "",
                "prefix": "recordings/",
                "auto_upload": True,
                "is_active": False,
            }

        extra = {}
        if cfg.extra_params_json:
            try:
                extra = json.loads(cfg.extra_params_json)
            except Exception:
                pass

        raw_secret = ""
        if cfg.encrypted_api_key:
            try:
                raw_secret = decrypt_secret(cfg.encrypted_api_key)
            except Exception as e:
                logger.warning(f"解密 OSS Secret 失败: {e}")

        display_secret = mask_api_key(raw_secret) if mask_secret else raw_secret

        return {
            "configured": bool(cfg.base_url and cfg.model_name and extra.get("access_key_id")),
            "endpoint": cfg.base_url or "",
            "bucket": cfg.model_name or "",
            "access_key_id": extra.get("access_key_id", ""),
            "access_key_secret": display_secret,
            "custom_domain": extra.get("custom_domain", ""),
            "prefix": extra.get("prefix", "recordings/"),
            "auto_upload": bool(extra.get("auto_upload", True)),
            "is_active": bool(cfg.is_active),
            "sdk_installed": OSS2_AVAILABLE,
        }

    async def save_config(
        self,
        endpoint: str,
        bucket: str,
        access_key_id: str,
        access_key_secret: str,
        custom_domain: str = "",
        prefix: str = "recordings/",
        auto_upload: bool = True,
        is_active: bool = True,
    ) -> Dict[str, Any]:
        """保存或更新 OSS 配置到数据库 (密文加密存储)"""
        norm_endpoint = self.normalize_endpoint(endpoint)
        clean_bucket = bucket.strip()
        clean_ak_id = access_key_id.strip()
        clean_prefix = prefix.strip().strip("/") + "/" if prefix.strip() else "recordings/"

        # 若前端传回的是掩码（如包含 ***），保留数据库中的原始密钥
        existing_config = await self.get_config(mask_secret=False)
        final_secret = access_key_secret.strip()
        if "***" in final_secret or "•" in final_secret or not final_secret:
            final_secret = existing_config.get("access_key_secret", "")

        encrypted_secret = encrypt_secret(final_secret) if final_secret else ""

        extra_json = json.dumps({
            "access_key_id": clean_ak_id,
            "custom_domain": custom_domain.strip().rstrip("/"),
            "prefix": clean_prefix,
            "auto_upload": bool(auto_upload),
        }, ensure_ascii=False)

        async with AsyncSessionLocal() as db:
            stmt = select(ApiProviderConfig).where(
                ApiProviderConfig.config_group == self.CONFIG_GROUP,
                ApiProviderConfig.provider_name == self.PROVIDER_NAME,
            ).limit(1)
            res = await db.execute(stmt)
            record = res.scalar_one_or_none()

            if record:
                record.base_url = norm_endpoint
                record.model_name = clean_bucket
                record.encrypted_api_key = encrypted_secret
                record.extra_params_json = extra_json
                record.is_active = 1 if is_active else 0
            else:
                record = ApiProviderConfig(
                    id=self.CONFIG_ID,
                    config_group=self.CONFIG_GROUP,
                    provider_name=self.PROVIDER_NAME,
                    base_url=norm_endpoint,
                    model_name=clean_bucket,
                    encrypted_api_key=encrypted_secret,
                    extra_params_json=extra_json,
                    is_active=1 if is_active else 0,
                )
                db.add(record)
            await db.commit()

        logger.info(f"阿里云 OSS 配置已保存: Bucket={clean_bucket}, Endpoint={norm_endpoint}")
        return await self.get_config(mask_secret=True)

    async def test_connectivity(
        self,
        endpoint: str,
        bucket: str,
        access_key_id: str,
        access_key_secret: str,
    ) -> Tuple[bool, str]:
        """连通性与权限真实握手测试"""
        norm_endpoint = self.normalize_endpoint(endpoint)
        clean_bucket = bucket.strip()
        clean_ak = access_key_id.strip()
        clean_secret = access_key_secret.strip()

        # 若是掩码，读取库中真实秘钥
        if "***" in clean_secret or "•" in clean_secret or not clean_secret:
            stored = await self.get_config(mask_secret=False)
            clean_secret = stored.get("access_key_secret", "")

        if not norm_endpoint or not clean_bucket or not clean_ak or not clean_secret:
            return False, "请完整填写 Endpoint、Bucket 名称以及 AccessKey 信息"

        if not OSS2_AVAILABLE or oss2 is None:
            return False, "系统未安装 oss2 依赖库，请先执行 `pip install oss2`"

        def _sync_test():
            try:
                auth = oss2.Auth(clean_ak, clean_secret)
                bucket_obj = oss2.Bucket(auth, norm_endpoint, clean_bucket, connect_timeout=5.0)
                # 尝试获取 Bucket 基本信息或探测对象
                bucket_info = bucket_obj.get_bucket_info()
                return True, f"连接成功！存储桶地域: {bucket_info.location}, 存储类型: {bucket_info.storage_class}"
            except oss2.exceptions.NoSuchBucket:
                return False, f"存储桶 [{clean_bucket}] 不存在，请核对 Bucket 名称"
            except oss2.exceptions.AccessDenied:
                return False, "AccessKey 鉴权失败或没有该 Bucket 的访问权限 (AccessDenied)"
            except Exception as e:
                return False, f"OSS 握手失败: {str(e)}"

        return await asyncio.to_thread(_sync_test)

    async def upload_file(
        self,
        local_path: Path,
        object_key: str,
    ) -> Tuple[bool, str, str]:
        """
        上传本地文件到 OSS
        返回: (success: bool, url_or_msg: str, object_key: str)
        """
        if not local_path.exists():
            return False, f"本地文件不存在: {local_path}", ""

        config = await self.get_config(mask_secret=False)
        if not config.get("configured"):
            return False, "尚未配置阿里云 OSS 存储信息", ""

        if not OSS2_AVAILABLE or oss2 is None:
            return False, "系统未安装 oss2 依赖库 (pip install oss2)", ""

        endpoint = config["endpoint"]
        bucket_name = config["bucket"]
        ak = config["access_key_id"]
        secret = config["access_key_secret"]
        custom_domain = config.get("custom_domain", "")

        def _sync_upload():
            try:
                auth = oss2.Auth(ak, secret)
                bucket = oss2.Bucket(auth, endpoint, bucket_name)
                # 执行文件上传
                oss2.resumable_upload(
                    bucket,
                    object_key,
                    str(local_path),
                    store=oss2.ResumableStore(root="/tmp/.oss_upload_records"),
                    multipart_threshold=10 * 1024 * 1024,
                    part_size=5 * 1024 * 1024,
                    num_threads=2,
                )
                # 计算直链 URL
                if custom_domain:
                    final_url = f"{custom_domain.rstrip('/')}/{object_key.lstrip('/')}"
                else:
                    parsed = urlparse(endpoint)
                    host = f"{bucket_name}.{parsed.netloc}"
                    scheme = parsed.scheme or "https"
                    final_url = f"{scheme}://{host}/{object_key.lstrip('/')}"
                return True, final_url, object_key
            except Exception as e:
                logger.error(f"OSS 上传文件异常: {e}", exc_info=True)
                return False, str(e), object_key

        return await asyncio.to_thread(_sync_upload)

    async def generate_play_url(self, oss_url_or_key: str, expires_sec: int = 86400) -> str:
        """
        为私有存储桶生成带有防盗链签名的临时访问播放地址
        若已配置 CDN 或为公共读，则直接返回公网 URL
        """
        if not oss_url_or_key:
            return ""

        config = await self.get_config(mask_secret=False)
        if not config.get("configured") or not OSS2_AVAILABLE or oss2 is None:
            return oss_url_or_key

        endpoint = config["endpoint"]
        bucket_name = config["bucket"]
        ak = config["access_key_id"]
        secret = config["access_key_secret"]

        # 提取 object_key
        object_key = oss_url_or_key
        if "://" in oss_url_or_key:
            parsed = urlparse(oss_url_or_key)
            object_key = parsed.path.lstrip("/")

        def _sync_sign():
            try:
                auth = oss2.Auth(ak, secret)
                bucket = oss2.Bucket(auth, endpoint, bucket_name)
                signed_url = bucket.sign_url("GET", object_key, expires_sec)
                return signed_url
            except Exception as e:
                logger.warning(f"生成 OSS 签名 URL 失败: {e}")
                return oss_url_or_key

        return await asyncio.to_thread(_sync_sign)

    async def upload_session_recording_async(
        self,
        session_id: str,
        video_path: str,
        preview_path: str = "",
    ):
        """
        后台异步任务：将整场直播录像和封面安全上传到阿里云 OSS 并回写场次表
        """
        logger.info(f"启动场次 #{session_id} 直播录像 OSS 归档任务: {video_path}")
        cfg = await self.get_config(mask_secret=False)
        if not cfg.get("configured"):
            logger.info(f"OSS 未配置，场次 #{session_id} 录像保留本地: {video_path}")
            async with AsyncSessionLocal() as db:
                await db.execute(
                    update(LiveSessionRecord)
                    .where(LiveSessionRecord.session_id == session_id)
                    .values(
                        video_path=video_path,
                        preview_image_url=preview_path,
                        oss_upload_status="none",
                    )
                )
                await db.commit()
            return

        # 更新为上传中
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(LiveSessionRecord)
                .where(LiveSessionRecord.session_id == session_id)
                .values(
                    video_path=video_path,
                    oss_upload_status="uploading",
                )
            )
            await db.commit()

        prefix = cfg.get("prefix", "recordings/").strip("/")
        vid_p = Path(video_path)
        timestamp_str = time.strftime("%Y%m%d_%H%M%S")
        object_key = f"{prefix}/{session_id}_{timestamp_str}.mp4"

        # 上传主视频
        success, url_or_msg, _ = await self.upload_file(vid_p, object_key)

        preview_url = ""
        if preview_path and Path(preview_path).exists():
            prev_key = f"{prefix}/{session_id}_{timestamp_str}_cover.jpg"
            p_success, p_url, _ = await self.upload_file(Path(preview_path), prev_key)
            if p_success:
                preview_url = p_url

        file_size = vid_p.stat().st_size if vid_p.exists() else 0

        async with AsyncSessionLocal() as db:
            if success:
                logger.info(f"场次 #{session_id} 直播录像已归档至 OSS: {url_or_msg}")
                await db.execute(
                    update(LiveSessionRecord)
                    .where(LiveSessionRecord.session_id == session_id)
                    .values(
                        video_path=video_path,
                        oss_url=url_or_msg,
                        preview_image_url=preview_url or preview_path,
                        video_size_bytes=file_size,
                        oss_upload_status="uploaded",
                        oss_error_msg="",
                    )
                )
            else:
                logger.warning(f"场次 #{session_id} 直播录像上传 OSS 失败: {url_or_msg}")
                await db.execute(
                    update(LiveSessionRecord)
                    .where(LiveSessionRecord.session_id == session_id)
                    .values(
                        video_path=video_path,
                        preview_image_url=preview_path,
                        video_size_bytes=file_size,
                        oss_upload_status="failed",
                        oss_error_msg=url_or_msg,
                    )
                )
            await db.commit()


global_oss_uploader = AliyunOSSUploader()
