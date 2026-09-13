import asyncio
from server.adapters.media.base_driver import BaseMediaDriver

class MockMediaDriver(BaseMediaDriver):
    """仿真媒体驱动，记录音频切片输入、输出打断日志，用于全链路无GPU联调"""
    def __init__(self):
        super().__init__()
        self.rendered_chunks_count = 0
        self.interrupt_count = 0
        self.last_interrupt_reason = ""
        self.current_audio_buffer = []

    async def feed_audio_chunk(self, audio_bytes: bytes, text_snippet: str):
        self.is_speaking = True
        self.current_audio_buffer.append(text_snippet)
        self.rendered_chunks_count += 1
        # 模拟 80ms 播放耗时
        await asyncio.sleep(0.08)

    async def interrupt(self, reason: str = "Barge-in"):
        self.is_speaking = False
        self.interrupt_count += 1
        self.last_interrupt_reason = reason
        # 瞬间清空待播音频缓冲区
        self.current_audio_buffer.clear()

    async def start(self):
        pass

    async def stop(self):
        self.is_speaking = False
        self.current_audio_buffer.clear()

# 全局媒体适配器单例
global_media_driver = MockMediaDriver()
