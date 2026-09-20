#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MOSS-TTS-Nano 端侧零样本声音克隆引擎 (v3.0.0 官方神经模型驱动版)
遵循复旦大学开源 MOSS-TTS-Nano (https://github.com/OpenMOSS/MOSS-TTS-Nano)

核心实现：
1. 彻底告别第三方固定音色冒充与虚假滤波；
2. 完整集成官方 ONNX 神经运行时：
   - MOSS-Audio-Tokenizer-Nano (8层 RVQ 离散声学编解码器，24/48kHz)，精准提取声门闭合度、沙哑粗糙度、共鸣腔体态；
   - MOSS-TTS-Nano-100M (轻量端侧自回归 Transformer 语言模型)，顺应参考声学状态自回归生成新台词的 RVQ 特征；
3. 支持官方内置预置音色 (Junhao, Xiaoyu, Yuewen, Weiguo, Zhiming 等) 与 用户自定义录音 零样本复刻。
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional, Union, Dict, Any, List

logger = logging.getLogger("LiveAgent.MossNanoCloner")

PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_MODELS_DIR = PROJECT_ROOT / "data" / "models" / "moss_tts_nano"
DEFAULT_MODELS_DIR.mkdir(parents=True, exist_ok=True)


class MossNanoCloner:
    """MOSS-TTS-Nano 官方神经模型端侧零样本克隆执行器"""

    _runtime = None
    _runtime_lock = threading.Lock()

    def __init__(self, model_dir: Optional[Union[str, Path]] = None):
        self.model_dir = Path(model_dir or DEFAULT_MODELS_DIR)
        self.sample_rate = 48000

    def _get_runtime(self):
        """获取或懒加载官方 OnnxTtsRuntime 单例"""
        if MossNanoCloner._runtime is not None:
            return MossNanoCloner._runtime

        with MossNanoCloner._runtime_lock:
            if MossNanoCloner._runtime is None:
                logger.info(f"正在初始化官方 MOSS-TTS-Nano ONNX 运行时 (模型目录: {self.model_dir})...")
                try:
                    from server.core.audio.moss_nano.onnx_tts_runtime import OnnxTtsRuntime
                    from server.core.audio.moss_nano.ort_cpu_runtime import EXECUTION_PROVIDER_AUTO
                    runtime = OnnxTtsRuntime(
                        model_dir=str(self.model_dir),
                        execution_provider=EXECUTION_PROVIDER_AUTO,
                        thread_count=4
                    )
                    MossNanoCloner._runtime = runtime
                    # 采样参数调优：官方默认 audio_temperature 0.8，降至 0.7 减少随机伪影提升稳定性
                    gen_defaults = getattr(runtime, "manifest", {}).get("generation_defaults")
                    if isinstance(gen_defaults, dict):
                        gen_defaults["audio_temperature"] = 0.7
                    providers = getattr(runtime, "actual_providers", [])
                    is_gpu = any("cuda" in str(p).lower() for p in providers)
                    dev_desc = "🚀 [CUDA GPU 独显硬件加速]" if is_gpu else "⚡ [端侧多线程 CPU 高性能优化]"
                    logger.info(f"🎉 官方 MOSS-TTS-Nano 神经克隆引擎成功就绪！计算设备: {dev_desc} (激活 Providers: {providers})")
                except Exception as e:
                    logger.error(f"加载 MOSS-TTS-Nano 官方 ONNX 运行时失败: {e}", exc_info=True)
                    raise RuntimeError(f"加载 MOSS-TTS-Nano 官方模型失败: {e}")

        return MossNanoCloner._runtime

    def get_builtin_voices(self) -> List[Dict[str, Any]]:
        """获取官方内置声线清单"""
        try:
            runtime = self._get_runtime()
            manifest = getattr(runtime, "manifest", {}) or {}
            builtin = manifest.get("builtin_voices", [])
            results = []
            for item in builtin:
                results.append({
                    "id": item.get("voice"),
                    "name": item.get("display_name", item.get("voice")),
                    "gender": item.get("gender", "neutral"),
                    "group": item.get("group", "Standard")
                })
            return results
        except Exception as e:
            logger.warning(f"获取 MOSS-TTS 内置音色列表失败: {e}")
            return [
                {"id": "Xiaoyu", "name": "官方清亮女主播 (Xiaoyu)", "gender": "female", "group": "Chinese Female"},
                {"id": "Junhao", "name": "官方阳光男主播 (Junhao)", "gender": "male", "group": "Chinese Male"},
                {"id": "Yuewen", "name": "官方沉稳女主播 (Yuewen)", "gender": "female", "group": "Chinese Female"},
                {"id": "Weiguo", "name": "官方磁性男主播 (Weiguo)", "gender": "male", "group": "Chinese Male"},
            ]

    def _apply_audio_effects(
        self,
        src_path: Union[str, Path],
        dst_path: Union[str, Path],
        speed: float = 1.0,
        volume: float = 1.0
    ) -> None:
        """若需要变速/变调/变音量，调用 ffmpeg 处理，否则直接移动或复制"""
        src = Path(src_path)
        dst = Path(dst_path)
        dst.parent.mkdir(parents=True, exist_ok=True)

        need_filter = abs(speed - 1.0) > 0.03 or abs(volume - 1.0) > 0.03
        if not need_filter:
            if src.resolve() != dst.resolve():
                shutil.copy2(src, dst)
            return

        filters = []
        if abs(speed - 1.0) > 0.03:
            clamped_speed = max(0.5, min(2.0, speed))
            filters.append(f"atempo={clamped_speed:.3f}")
        if abs(volume - 1.0) > 0.03:
            clamped_vol = max(0.1, min(3.0, volume))
            filters.append(f"volume={clamped_vol:.3f}")

        filter_str = ",".join(filters)
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src),
            "-filter:a", filter_str,
            "-ar", "48000",
            str(dst)
        ]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception as e:
            logger.warning(f"ffmpeg 调整语速音量失败: {e}，直接保留原生成音频")
            if src.resolve() != dst.resolve():
                shutil.copy2(src, dst)

    def extract_voice_timbre_profile(self, sample_path: Union[str, Path]) -> Dict[str, Any]:
        """提取参考样本的声学指纹画像 (RVQ 编码诊断)：验证样本可编码性与帧数规模"""
        runtime = self._get_runtime()
        codes = runtime.encode_reference_audio(str(sample_path))
        codec_cfg = runtime.codec_meta.get("codec_config", {})
        sample_rate = int(codec_cfg.get("sample_rate", 48000))
        duration = round(len(codes) * codec_cfg.get("downsample_rate", 3840) / max(1, sample_rate), 2)
        return {
            "engine": "MOSS-TTS-Nano ONNX",
            "sample_rate": sample_rate,
            "channels": int(codec_cfg.get("channels", 2)),
            "num_quantizers": int(codec_cfg.get("num_quantizers", 16)),
            "prompt_frames": len(codes),
            "estimated_duration_sec": duration,
            "quality_hint": (
                "样本有效，条件化帧数充足" if len(codes) >= 60
                else "样本偏短，建议上传 10~30 秒清晰录音以获得更佳音色相似度"
            ),
        }

    async def stream_synthesize_chunks(
        self,
        text: str,
        prompt_audio_path: Optional[Union[str, Path]] = None,
        voice: Optional[str] = None,
    ):
        """
        真流式克隆合成：边自回归生成边产出 float32 波形块 (48kHz stereo)。
        首块延迟 = prefill + 首帧流式解码，远低于整段生成 (直播/试听显著降卡顿)。
        注意：流式路径不应用 speed/volume 效果 (非流式 clone_and_synthesize 才经 ffmpeg 处理)。
        """
        clean_text = (text or "").strip()
        if not clean_text:
            raise ValueError("MOSS-TTS-Nano 合成台词不能为空")

        valid_prompt_path = None
        if prompt_audio_path:
            p = Path(prompt_audio_path)
            if p.exists() and p.is_file() and p.stat().st_size > 1024:
                valid_prompt_path = str(p.resolve())

        selected_voice = voice
        if not valid_prompt_path and not selected_voice:
            selected_voice = "Junhao"

        import queue as _queue
        import numpy as np

        out_queue: "_queue.Queue" = _queue.Queue()
        _END = object()

        def _sync_produce() -> None:
            try:
                runtime = self._get_runtime()
                prompt_codes = runtime.resolve_prompt_audio_codes(
                    voice=(None if valid_prompt_path else selected_voice),
                    prompt_audio_path=valid_prompt_path,
                )
                prepared = runtime.prepare_synthesis_text(
                    text=clean_text,
                    voice=str(selected_voice or ""),
                    enable_wetext=True,
                    enable_normalize_tts_text=True,
                )
                text_chunks = runtime.split_voice_clone_text(str(prepared["text"]), max_tokens=75)
                sample_rate = int(runtime.codec_meta["codec_config"]["sample_rate"])
                channels = int(runtime.codec_meta["codec_config"]["channels"])

                for chunk_index, chunk_text in enumerate(text_chunks):
                    runtime.synthesize_single_chunk(
                        text=chunk_text,
                        prompt_audio_codes=prompt_codes,
                        streaming=True,
                        on_audio_chunk=lambda arr: out_queue.put(("audio", arr)),
                    )
                    if chunk_index < len(text_chunks) - 1:
                        pause_seconds = runtime.estimate_voice_clone_inter_chunk_pause_seconds(chunk_text)
                        pause_samples = max(0, int(round(sample_rate * pause_seconds)))
                        if pause_samples > 0:
                            out_queue.put(("audio", np.zeros((pause_samples, channels), dtype=np.float32)))
            except Exception as e:
                out_queue.put(("error", e))
            finally:
                out_queue.put(_END)

        producer_task = asyncio.create_task(asyncio.to_thread(_sync_produce))
        loop = asyncio.get_running_loop()
        try:
            while True:
                item = await loop.run_in_executor(None, out_queue.get)
                if item is _END:
                    break
                kind, payload = item
                if kind == "error":
                    raise payload
                yield payload
        finally:
            producer_task.cancel()

    async def clone_and_synthesize(
        self,
        text: str,
        prompt_audio_path: Optional[Union[str, Path]] = None,
        output_path: Optional[Union[str, Path]] = None,
        speed: float = 1.0,
        volume: float = 1.0,
        voice: Optional[str] = None
    ) -> Path:
        """
        核心克隆方法：
        使用复旦官方 MOSS-TTS-Nano 真正的 RVQ 神经网络声学编解码与自回归语言模型，
        从用户参考录音中提取专属声学表征并合成全新台词。

        【铁律】：
        1. 严禁使用任何固定第三方音色假冒！
        2. 严禁直接返回 prompt_audio_path 样本原件！
        3. 必须输出包含新台词全文的 48kHz 高清克隆音频！
        """
        clean_text = (text or "").strip()
        if not clean_text:
            raise ValueError("MOSS-TTS-Nano 合成台词不能为空")

        if output_path is None:
            import tempfile
            import uuid
            temp_dir = Path(tempfile.gettempdir()) / "ai_livestream_clones"
            temp_dir.mkdir(parents=True, exist_ok=True)
            out_file = temp_dir / f"moss_clone_{uuid.uuid4().hex[:12]}.wav"
        else:
            out_file = Path(output_path)
            out_file.parent.mkdir(parents=True, exist_ok=True)

        # 检查参考音频是否存在
        valid_prompt_path = None
        if prompt_audio_path:
            p = Path(prompt_audio_path)
            if p.exists() and p.is_file() and p.stat().st_size > 1024:
                valid_prompt_path = str(p.resolve())

        # 选定预置音色
        selected_voice = voice
        if not valid_prompt_path and not selected_voice:
            # 若既无录音也无指定音色，默认使用官方 Junhao 阳光男声
            selected_voice = "Junhao"

        logger.info(
            f"正在调用官方 MOSS-TTS-Nano 执行神经网络克隆合成: "
            f"文本='{clean_text[:25]}...', prompt_audio={'有' if valid_prompt_path else '无'}, voice={selected_voice}"
        )

        def _sync_infer() -> str:
            runtime = self._get_runtime()
            import tempfile
            import uuid
            raw_tmp_out = Path(tempfile.gettempdir()) / f"moss_raw_{uuid.uuid4().hex[:10]}.wav"

            kwargs: Dict[str, Any] = {
                "text": clean_text,
                "output_audio_path": str(raw_tmp_out),
                "enable_wetext": True,
                "streaming": False,
            }
            if valid_prompt_path:
                kwargs["prompt_audio_path"] = valid_prompt_path
            elif selected_voice:
                kwargs["voice"] = selected_voice

            # 执行自回归生成与声学解码
            res = runtime.synthesize(**kwargs)
            gen_out = res.get("output_path") or str(raw_tmp_out)
            if not Path(gen_out).exists() or Path(gen_out).stat().st_size < 512:
                raise RuntimeError("MOSS-TTS-Nano 神经网络推理未生成有效波形文件")

            return gen_out

        # 异步线程池运行，避免阻塞主事件循环
        raw_output_wav = await asyncio.to_thread(_sync_infer)

        # 调整语速与音量并输出到目标文件
        await asyncio.to_thread(
            self._apply_audio_effects,
            src_path=raw_output_wav,
            dst_path=out_file,
            speed=speed,
            volume=volume
        )

        # 清理中间临时文件
        try:
            if Path(raw_output_wav).exists() and Path(raw_output_wav).resolve() != out_file.resolve():
                Path(raw_output_wav).unlink(missing_ok=True)
        except Exception:
            pass

        if not out_file.exists() or out_file.stat().st_size < 512:
            raise RuntimeError(f"MOSS-TTS-Nano 输出文件校验失败: {out_file}")

        logger.info(f"🎉 官方 MOSS-TTS-Nano 声线克隆合成完毕: {out_file} (文件大小: {out_file.stat().st_size} 字节)")
        return out_file


# 全局单例
moss_cloner = MossNanoCloner()
