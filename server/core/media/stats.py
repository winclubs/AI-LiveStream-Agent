"""共享统计工具：小样本百分位计算（相邻秩线性插值口径，不依赖 numpy）"""

from typing import Sequence


def percentile(values: Sequence[float], p: float) -> float:
    """返回线性插值百分位；空序列返回 0.0。

    统一音画同步与监控链路的 p50/p95/p99 口径，与 numpy.percentile 默认插值方式一致。
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * p
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction
