"""独立 GPU 神经渲染 sidecar；不内置任何模型实现或权重。"""

from gpu_sidecar.backend import Wav2LipBackend
from gpu_sidecar.contracts import (
    BackendDescriptor,
    PluginEngine,
    PluginFactory,
    PluginFrameResult,
    PluginOOMError,
    PluginSession,
)

__all__ = [
    "BackendDescriptor",
    "PluginEngine",
    "PluginFactory",
    "PluginFrameResult",
    "PluginOOMError",
    "PluginSession",
    "Wav2LipBackend",
]

__version__ = "1.0.0"
