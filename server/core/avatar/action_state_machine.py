# -*- coding: utf-8 -*-
"""
数字人动作切片状态机与电商带货场景智能联动引擎 (ActionStateMachine)
阶段三核心架构：
1. 平滑镜像索引循环算法 (mirror_index)：彻底消除短视频切片循环播放时的卡顿撕裂；
2. 动作切片帧序列加载与过渡插值混合 (Alpha Blending)；
3. 双轨驱动触发研判器 (Dual-Trigger Engine)：
   - 话术关键词智能研判 (促单逼单/点赞求关注/问候欢迎)；
   - 直播间实时场控事件驱动 (观众送礼打赏/进场欢迎/下单成交)；
4. 优先级抢占机制 (打赏致谢 P9 > 逼单促单 P5 > 进房欢迎 P2 > 待机呼吸 P0)；
5. 时长衰减机制 (指定秒数后平滑复位回待机态 0)；
6. 数据库 avatar_actions 配置热重载与多主播隔离。
"""
import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
from sqlalchemy import select

from server.config import DATA_DIR
from server.database.db import AsyncSessionLocal
from server.database.models import AvatarAction

logger = logging.getLogger("LiveAgent.ActionStateMachine")

ACTION_CLIPS_DIR = DATA_DIR / "avatar_actions"
ACTION_CLIPS_DIR.mkdir(parents=True, exist_ok=True)


def mirror_index(idx: int, total_len: int) -> int:
    """
    数学镜像平滑索引循环算法
    输入线性递增的绝对帧序号 idx 与动作切片总帧数 total_len，
    生成头尾无缝往返对称的序列索引 (0 -> 1 -> ... -> N-1 -> N-2 -> ... -> 0 -> 1 ...)，
    彻底消除动作首尾帧相接时的突变画面撕裂。
    """
    if total_len <= 1:
        return 0
    period = (total_len - 1) * 2
    rem = idx % period
    if rem < total_len:
        return rem
    return period - rem


MAX_ACTION_FRAMES = 3000  # 最大动作抽帧上限 (防长视频磁盘/内存耗尽爆机)


class ActionClip:
    """单个动作切片内存与磁盘句柄"""

    def __init__(
        self,
        action_code: int,
        action_name: str,
        video_path: str = "",
        frames_dir: str = "",
        duration_sec: float = 3.5,
        priority: int = 1,
        mirror_loop: bool = True,
        preload_memory: bool = False,
    ):
        self.action_code = action_code
        self.action_name = action_name
        self.video_path = video_path
        self.frames_dir = frames_dir
        self.duration_sec = duration_sec
        self.priority = priority
        self.mirror_loop = mirror_loop
        self.preload_memory = preload_memory
        self.frames: List[np.ndarray] = []
        self.frame_paths: List[Path] = []
        self.total_frames: int = 0

    def load_frames(self) -> int:
        """加载切片帧 (含防爆截断与低频 I/O 自动内存预加载)"""
        self.frames.clear()
        self.frame_paths.clear()

        # 1. 优先从已切片目录加载
        if self.frames_dir and Path(self.frames_dir).exists():
            p_list = sorted(
                Path(self.frames_dir).glob("*.jpg"),
                key=lambda p: int(p.stem) if p.stem.isdigit() else p.name,
            )
            if p_list:
                # 防爆截断：最多保留 MAX_ACTION_FRAMES 帧
                if len(p_list) > MAX_ACTION_FRAMES:
                    logger.warning(
                        f"动作切片 [{self.action_name}] 包含 {len(p_list)} 帧，已截断至前 {MAX_ACTION_FRAMES} 帧"
                    )
                    p_list = p_list[:MAX_ACTION_FRAMES]
                self.frame_paths = p_list
                self.total_frames = len(p_list)

                # 自动内存预加载：若切片 <= 300 帧或显式指定 preload，直接缓存进内存杜绝 I/O 掉帧
                if self.preload_memory or self.total_frames <= 300:
                    for p in self.frame_paths:
                        img = cv2.imread(str(p))
                        if img is not None:
                            self.frames.append(img)
                return self.total_frames

        # 2. 从 MP4 视频提取切片
        if self.video_path and Path(self.video_path).exists():
            cap = cv2.VideoCapture(str(self.video_path))
            if cap.isOpened():
                out_dir = Path(self.frames_dir) if self.frames_dir else (ACTION_CLIPS_DIR / f"clip_{self.action_code}")
                out_dir.mkdir(parents=True, exist_ok=True)
                frame_idx = 0
                while frame_idx < MAX_ACTION_FRAMES:
                    ret, frame = cap.read()
                    if not ret or frame is None:
                        break
                    f_path = out_dir / f"{frame_idx}.jpg"
                    cv2.imwrite(str(f_path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
                    self.frame_paths.append(f_path)
                    if self.preload_memory or frame_idx <= 300:
                        self.frames.append(frame)
                    frame_idx += 1
                if frame_idx >= MAX_ACTION_FRAMES:
                    logger.warning(
                        f"视频切片提取达到安全上限 ({MAX_ACTION_FRAMES} 帧)，已自动停止截断防爆"
                    )
                cap.release()
                self.frames_dir = out_dir.as_posix()
                self.total_frames = len(self.frame_paths)
                return self.total_frames

        return 0

    def get_frame(self, index: int, as_rgb: bool = False) -> Optional[np.ndarray]:
        """获取指定索引的切片画面 (支持内存帧与磁盘帧)"""
        frame = None
        if self.frames:
            target_idx = mirror_index(index, len(self.frames)) if self.mirror_loop else (index % len(self.frames))
            frame = self.frames[target_idx]
        elif self.frame_paths:
            target_idx = mirror_index(index, len(self.frame_paths)) if self.mirror_loop else (index % len(self.frame_paths))
            frame = cv2.imread(str(self.frame_paths[target_idx]))

        if frame is not None and as_rgb:
            return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        return frame


class ActionStateMachine:
    """数字人动作切片状态机"""

    def __init__(self):
        self.current_action: int = 0
        self.prev_action: int = 0
        self.current_priority: int = 0
        self.action_start_time: float = 0.0
        self.action_duration: float = 0.0
        self.active_anchor_id: Optional[str] = None

        # 内存切片序列库与触发规则字典
        self.clips: Dict[int, ActionClip] = {}
        self.keyword_rules: List[Dict[str, Any]] = []
        self.event_rules: Dict[str, Dict[str, Any]] = {}

        # 动作切换平滑过渡帧进度 (0.0:刚切换, 1.0:完全进入新动作)
        self.blend_step: int = 0
        self.blend_total_steps: int = 4

        # 初始化默认 5 大基础动作槽位
        self._init_default_actions()

    def _init_default_actions(self):
        """初始化系统默认 5 大标准带货动作槽位"""
        defaults = [
            (0, "待机呼吸循环", 0.0, 0, "", ""),
            (1, "热情挥手欢迎", 3.0, 2, "欢迎,来了,刚进,哈喽,晚上好", "welcome"),
            (2, "求关注与点赞", 3.5, 3, "点赞,关注,粉丝团,灯牌,双击", "follow"),
            (3, "促单指引购物车", 4.0, 5, "购物车,下单,左下角,抢购,手慢无,买一送,拍下", "order"),
            (4, "大额打赏致谢", 4.5, 9, "感谢,礼物,破费,大气,老板大气", "gift"),
        ]
        self.keyword_rules.clear()
        self.event_rules.clear()

        for code, name, dur, prio, kws, evts in defaults:
            clip = ActionClip(code, name, duration_sec=dur, priority=prio)
            self.clips[code] = clip
            if kws:
                self.keyword_rules.append({
                    "action_code": code,
                    "keywords": [k.strip() for k in kws.split(",") if k.strip()],
                    "priority": prio,
                    "duration": dur,
                })
            if evts:
                self.event_rules[evts.strip()] = {
                    "action_code": code,
                    "priority": prio,
                    "duration": dur,
                }

        # 关键词规则按优先级降序排序，高优词优先命中
        self.keyword_rules.sort(key=lambda r: r["priority"], reverse=True)

    async def load_configs_from_db(self, anchor_id: Optional[str] = None):
        """从 SQLite 数据库热重载动作配置与切片关联"""
        self.active_anchor_id = anchor_id
        async with AsyncSessionLocal() as db:
            # 优先加载针对指定主播的自定义动作，若无则使用全局通用动作 (anchor_id is NULL)
            stmt = select(AvatarAction).where(AvatarAction.is_active == 1)
            if anchor_id:
                stmt = stmt.where((AvatarAction.anchor_id == anchor_id) | (AvatarAction.anchor_id.is_(None)))
            else:
                stmt = stmt.where(AvatarAction.anchor_id.is_(None))
            stmt = stmt.order_by(AvatarAction.anchor_id.desc(), AvatarAction.priority.desc())

            res = await db.execute(stmt)
            actions = res.scalars().all()

        if not actions:
            return

        self.keyword_rules.clear()
        self.event_rules.clear()
        seen_codes = set()

        for act in actions:
            if act.action_code in seen_codes:
                continue
            seen_codes.add(act.action_code)

            clip = ActionClip(
                action_code=act.action_code,
                action_name=act.action_name,
                video_path=act.video_path or "",
                frames_dir=act.frames_dir or "",
                duration_sec=float(act.duration_sec or 3.5),
                priority=int(act.priority or 1),
                mirror_loop=bool(act.mirror_loop),
            )
            # 异步尝试扫描或切片帧
            clip.load_frames()
            self.clips[act.action_code] = clip

            # 注册关键词匹配
            if act.trigger_keywords and act.trigger_type in ("keyword", "both"):
                k_list = [k.strip() for k in act.trigger_keywords.split(",") if k.strip()]
                if k_list:
                    self.keyword_rules.append({
                        "action_code": act.action_code,
                        "keywords": k_list,
                        "priority": act.priority,
                        "duration": float(act.duration_sec or 3.5),
                    })

            # 注册事件触发匹配
            if act.trigger_events and act.trigger_type in ("event", "both"):
                for evt in act.trigger_events.split(","):
                    evt_clean = evt.strip()
                    if evt_clean:
                        self.event_rules[evt_clean] = {
                            "action_code": act.action_code,
                            "priority": act.priority,
                            "duration": float(act.duration_sec or 3.5),
                        }

        self.keyword_rules.sort(key=lambda r: r["priority"], reverse=True)
        logger.info(f"动作状态机已成功重载 {len(seen_codes)} 组动作切片配置")

    def trigger_action(
        self,
        action_code: int,
        source: str = "manual",
        duration: Optional[float] = None,
        priority: Optional[int] = None,
    ) -> bool:
        """
        触发动作切换 (带优先级判断与自动衰减重置)
        """
        self.update_tick()  # 先处理一次超时检查

        clip = self.clips.get(action_code)
        target_priority = priority if priority is not None else (clip.priority if clip else 1)
        target_duration = duration if duration is not None else (clip.duration_sec if clip else 3.5)

        # 待机动作 (0) 允许随时强制重置
        if action_code == 0:
            self.prev_action = self.current_action
            self.current_action = 0
            self.current_priority = 0
            self.action_start_time = 0.0
            self.action_duration = 0.0
            self.blend_step = 0
            logger.info(f"动作状态机已平滑复位至待机呼吸 (来源: {source})")
            return True

        # 优先级抢占校验：若当前正在执行动作且其优先级更高，则拒绝抢占
        if self.current_action != 0:
            if target_priority < self.current_priority:
                logger.debug(
                    f"动作抢占被忽略: 目标动作 {action_code} (P{target_priority}) 低于当前执行动作 {self.current_action} (P{self.current_priority})"
                )
                return False

        # 触发状态跃迁
        self.prev_action = self.current_action
        self.current_action = action_code
        self.current_priority = target_priority
        self.action_start_time = time.time()
        self.action_duration = target_duration
        self.blend_step = 0  # 启动淡入淡出混合

        logger.info(
            f"动作状态机切换 -> 动作代码 {action_code} [{clip.action_name if clip else '自定义'}] (优先级: P{target_priority}, 持续: {target_duration}s, 来源: {source})"
        )
        return True

    def evaluate_text(self, speak_text: str) -> Optional[int]:
        """
        根据口播话术智能研判动作代码
        """
        if not speak_text:
            return None

        for rule in self.keyword_rules:
            for kw in rule["keywords"]:
                if kw in speak_text:
                    action_code = rule["action_code"]
                    switched = self.trigger_action(
                        action_code,
                        source=f"keyword:{kw}",
                        duration=rule["duration"],
                        priority=rule["priority"],
                    )
                    return action_code if switched else None
        return None

    def evaluate_event(self, event_type: str, payload: Optional[Dict[str, Any]] = None) -> Optional[int]:
        """
        根据直播间实时交互事件智能研判动作代码
        """
        if not event_type:
            return None

        # 规范化事件类别 (gift/send_gift -> gift, join/welcome -> welcome)
        mapped_type = event_type.lower()
        if "gift" in mapped_type:
            mapped_type = "gift"
        elif any(w in mapped_type for w in ("welcome", "join", "enter")):
            mapped_type = "welcome"
        elif any(w in mapped_type for w in ("follow", "like", "fan")):
            mapped_type = "follow"
        elif any(w in mapped_type for w in ("order", "buy", "pay")):
            mapped_type = "order"

        rule = self.event_rules.get(mapped_type)
        if rule:
            action_code = rule["action_code"]
            switched = self.trigger_action(
                action_code,
                source=f"event:{mapped_type}",
                duration=rule["duration"],
                priority=rule["priority"],
            )
            return action_code if switched else None

        return None

    def update_tick(self) -> int:
        """
        时钟推进：检查当前动作是否达到持续时间，超时则自动衰减恢复回待机 0
        """
        if self.current_action != 0:
            if self.action_duration > 0 and (time.time() - self.action_start_time > self.action_duration):
                logger.info(f"动作 {self.current_action} 持续时长已满 ({self.action_duration}s)，自动平滑衰减回复待机态")
                self.prev_action = self.current_action
                self.current_action = 0
                self.current_priority = 0
                self.action_start_time = 0.0
                self.action_duration = 0.0
                self.blend_step = 0
        return self.current_action

    def get_frame(self, frame_idx: int, base_frame: Optional[np.ndarray] = None) -> Optional[np.ndarray]:
        """
        获取当前动作切片画面帧，并在切换瞬间执行 Alpha 渐变混合以消除突变
        """
        self.update_tick()
        clip = self.clips.get(self.current_action)

        curr_img = None
        if clip and clip.total_frames > 0:
            curr_img = clip.get_frame(frame_idx)

        # 若当前动作未配置专属视频帧，使用 base_frame 作为底图
        if curr_img is None:
            curr_img = base_frame

        # 如果处于过渡切换阶段且有上一动作底图，进行 Alpha 权重混合
        if self.blend_step < self.blend_total_steps and self.prev_action != self.current_action:
            prev_clip = self.clips.get(self.prev_action)
            prev_img = prev_clip.get_frame(frame_idx) if prev_clip and prev_clip.total_frames > 0 else base_frame
            self.blend_step += 1
            if prev_img is not None and curr_img is not None and prev_img.shape == curr_img.shape:
                alpha = self.blend_step / float(self.blend_total_steps)
                return cv2.addWeighted(curr_img, alpha, prev_img, 1.0 - alpha, 0)

        return curr_img

    def reset_to_idle(self) -> None:
        """强制平滑重置回待机态 0"""
        self.prev_action = self.current_action
        self.current_action = 0
        self.current_priority = 0
        self.action_start_time = 0.0
        self.action_duration = 0.0
        self.blend_step = 0

    def step_clock(self) -> int:
        """更新并推进动作衰减时钟 (update_tick 别名)"""
        return self.update_tick()

    def get_current_frame(self, frame_idx: int, base_frame: Optional[np.ndarray] = None) -> Optional[np.ndarray]:
        """获取当前帧 (get_frame 别名)"""
        return self.get_frame(frame_idx, base_frame=base_frame)

    def get_current_action_frame(
        self,
        frame_idx: int,
        width: int = 720,
        height: int = 960,
    ) -> Optional[np.ndarray]:
        """
        获取当前激活非待机动作的切片 RGB 帧画面 (若处于待机状态 0 则返回 None)
        支持 Alpha 跨动作平滑过渡与尺寸自适应对齐。
        """
        self.update_tick()
        if self.current_action == 0:
            return None

        clip = self.clips.get(self.current_action)
        if not clip or clip.total_frames <= 0:
            return None

        frame = self.get_frame(frame_idx)
        if frame is None:
            return None

        # 尺寸统一自适应
        if frame.shape[0] != height or frame.shape[1] != width:
            frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_LINEAR)

        # 统一输出 RGB 色彩空间供数字人合成引擎消费
        return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

    def preload_all_clips(self) -> int:
        """一键将全部动作切片载入内存，彻底消除实时磁盘 I/O"""
        loaded = 0
        for clip in self.clips.values():
            clip.preload_memory = True
            cnt = clip.load_frames()
            if cnt > 0:
                loaded += 1
        return loaded

    def get_status(self) -> Dict[str, Any]:
        """获取动作状态机当前遥测状态"""
        self.update_tick()
        clip = self.clips.get(self.current_action)
        time_left = max(0.0, self.action_duration - (time.time() - self.action_start_time)) if self.action_duration > 0 and self.current_action != 0 else 0.0

        return {
            "current_action": self.current_action,
            "action_code": self.current_action,
            "action_name": clip.action_name if clip else "待机呼吸",
            "priority": self.current_priority,
            "duration": self.action_duration,
            "remaining_seconds": round(time_left, 2),
            "time_remaining_sec": round(time_left, 2),
            "is_idle": self.current_action == 0,
            "total_configured_actions": len(self.clips),
            "loaded_clips_count": sum(1 for c in self.clips.values() if c.total_frames > 0),
        }


_global_action_state_machine: Optional[ActionStateMachine] = None


def get_action_state_machine() -> ActionStateMachine:
    """获取全局动作状态机单例"""
    global _global_action_state_machine
    if _global_action_state_machine is None:
        _global_action_state_machine = ActionStateMachine()
    return _global_action_state_machine
