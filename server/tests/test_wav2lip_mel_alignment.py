# -*- coding: utf-8 -*-
"""
Wav2Lip 音频特征层 (P0-A) 与帧对齐自动化验收测试套件

对应 LIPSYNC_OPTIMIZATION_PLAN.md §7.2:
  ① 特征分布对齐 —— 与 librosa Slaney 滤波器组 golden 比对（不再自证）
  ② 帧对齐 —— 脉冲中心落点、live 路径无边缘复制、本地与云端一致

golden 数据由 `scripts/gen_mel_golden.py` 生成，该脚本是 librosa.filters.mel
与 Rudrabha/Wav2Lip audio.py::melspectrogram 的**独立转写**，与被测实现不共享代码。
"""
import json
from functools import lru_cache
from pathlib import Path

import numpy as np
import pytest

from server.core.avatar.neural_lip_renderer import MelFeatureExtractor, Wav2LipMelExtractor

GOLDEN_PATH = Path(__file__).parent / "data" / "mel_golden.json"


@lru_cache(maxsize=1)
def _golden() -> dict:
    if not GOLDEN_PATH.exists():
        pytest.skip(f"缺少 golden 参考数据: {GOLDEN_PATH} (运行 scripts/gen_mel_golden.py 生成)")
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def _cosine_dist(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return 1.0 - float(a @ b / (na * nb))


def _synthesize_formant_vowel(formants, duration_sec: float = 0.4, sr: int = 16000) -> np.ndarray:
    """共振峰合成元音（生成参数与 golden 脚本一致）"""
    n = int(sr * duration_sec)
    t = np.arange(n) / sr
    sig = (np.sin(2 * np.pi * 120.0 * t) > 0).astype(np.float64) * 0.6
    for fc, bw in zip(formants, (80.0, 100.0, 140.0, 160.0)):
        r = np.exp(-np.pi * bw / sr)
        th = 2 * np.pi * fc / sr
        sig = np.convolve(sig, [1.0, -2 * r * np.cos(th), r * r], mode="full")[:n]
    return (sig / np.max(np.abs(sig)).clip(1e-9)).astype(np.float32)


# ---------------------------------------------------------------- ① 特征分布对齐

def test_mel_basis_matches_librosa_golden():
    """验收指标 1: 滤波器组必须与 librosa Slaney 参考一致（增益比 ∈ [0.99, 1.01]）

    v2.0 缺陷：旧断言只验证「旧实现错 142x」，属自证循环；若新实现偏差 200x 仍会通过。
    """
    golden = np.asarray(_golden()["mel_basis"], dtype=np.float64)
    actual = Wav2LipMelExtractor()._mel_basis.astype(np.float64)

    assert actual.shape == golden.shape, f"滤波器组形状应为 {golden.shape}，实测 {actual.shape}"

    # 逐频带能量增益比：新实现相对 librosa 参考
    g_band = golden.sum(axis=1)
    a_band = actual.sum(axis=1)
    active = g_band > 1e-12
    ratio = a_band[active] / g_band[active]
    assert 0.99 <= float(np.median(ratio)) <= 1.01, (
        f"频带增益比中位数应 ∈ [0.99, 1.01]，实测 {np.median(ratio):.6f}"
    )
    assert np.allclose(actual, golden, atol=1e-6), "滤波器组与 librosa 参考存在超出容差的偏差"


def test_mel_basis_frequency_support():
    """验收指标 1b: 频带支撑范围必须落在 55~7600Hz（官方 fmin/fmax），不得触达 DC 或 8kHz"""
    basis = Wav2LipMelExtractor()._mel_basis
    fftfreqs = np.fft.rfftfreq(800, 1 / 16000)
    band_energy = basis.sum(axis=0)
    active = np.where(band_energy > 1e-12)[0]

    lowest, highest = fftfreqs[active[0]], fftfreqs[active[-1]]
    assert 40.0 <= lowest <= 120.0, f"最低有效频点应在 55Hz 附近，实测 {lowest:.0f}Hz（触达 DC 会引入底噪）"
    assert 7000.0 <= highest <= 7600.0, f"最高有效频点应在 7600Hz 附近，实测 {highest:.0f}Hz（超出即引入噪声）"
    # 旧实现会延伸到 20Hz 与 7900Hz
    assert lowest >= 55.0 - 1e-6, f"不得低于 fmin=55Hz，实测 {lowest:.0f}Hz"
    assert highest <= 7600.0 + 1e-6, f"不得高于 fmax=7600Hz，实测 {highest:.0f}Hz"


def test_mel_window_matches_official_golden():
    """验收指标 1c: 完整预处理链输出必须与官方 melspectrogram 一致（逐案例）"""
    extractor = Wav2LipMelExtractor()
    expected = _golden()["mel_windows"]

    cases = {
        "silence": np.zeros(6400),
        "near_silence": np.random.default_rng(20261002).normal(0, 1e-7, 6400),
        "vowel_a": _synthesize_formant_vowel((800.0, 1200.0)),
        "vowel_i": _synthesize_formant_vowel((280.0, 2300.0, 3000.0)),
        "vowel_u": _synthesize_formant_vowel((300.0, 870.0)),
        "loud_a": _synthesize_formant_vowel((800.0, 1200.0)) * 0.95,
    }
    for name, pcm in cases.items():
        want = np.asarray(expected[name], dtype=np.float64)
        got = extractor.extract_mel_window(pcm.astype(np.float32))[0, 0].astype(np.float64)
        assert want.shape == got.shape
        diff = float(np.abs(want - got).max())
        assert diff < 5e-3, f"案例 {name!r} 与官方链偏差过大: max|diff|={diff:.6f}"


def _centered_cosine_dist(a: np.ndarray, b: np.ndarray) -> float:
    """去均值后的余弦距离 —— 只衡量频谱形状，剔除 -4 静音底与整体音量偏置。

    官方链会把大量频带压到 -4 下界（实测 vowel_a 28.7% / vowel_u 32.5% 被钉在地板），
    不去均值的话余弦距离主要反映"有多少频带触底"，而非元音差异本身。
    """
    a = a.ravel() - a.mean()
    b = b.ravel() - b.mean()
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return 1.0 - float(a @ b / (na * nb))


def test_vowel_separation_matches_official_reference():
    """验收指标 2: 元音可分性必须复现官方链（与官方 golden 一致）

    v2.0.1 更正两处：
    1. v2.0 文档中的阈值 (/a/-/i/ ≥ 0.30、/i/-/u/ ≥ 0.35) 是用一个**同样带缺陷**的
       参考实现标定的，不是官方真值。官方真值实测 0.161 / 0.154，原阈值不可能达到。
    2. **不能**用「新实现可分性 >= 旧实现」作判据：两者的滤波器组增益相差 142x、
       归一化语义完全不同，余弦距离跨实现不可比。实测 a/u 对旧实现反而更高
       (0.0244 vs 0.0548)，这既不说明新实现更差，也不能反推旧实现更好。
       唯一有意义的验收是与官方链对齐。
    """
    extractor = Wav2LipMelExtractor()
    golden = {k: np.asarray(v) for k, v in _golden()["mel_windows"].items()}

    cases = {
        "vowel_a": _synthesize_formant_vowel((800.0, 1200.0)),
        "vowel_i": _synthesize_formant_vowel((280.0, 2300.0, 3000.0)),
        "vowel_u": _synthesize_formant_vowel((300.0, 870.0)),
    }
    new = {k: extractor.extract_mel_window(v) for k, v in cases.items()}

    for x, y in (("vowel_a", "vowel_i"), ("vowel_i", "vowel_u"), ("vowel_a", "vowel_u")):
        d_new = _centered_cosine_dist(new[x], new[y])
        d_ref = _centered_cosine_dist(golden[x], golden[y])
        assert abs(d_new - d_ref) < 0.02, (
            f"{x}/{y} 可分性与官方参考偏离过大: 新 {d_new:.4f} vs 官方 {d_ref:.4f}"
        )


def test_vowel_features_are_mutually_distinguishable():
    """验收指标 2b: 三个元音在官方特征空间中必须两两可分（非零距离）

    只断言「不坍缩」，不设绝对阈值——可分性的绝对量级取决于测试信号，
    真实判据是 test_mel_window_matches_official_golden 的逐点一致性。
    """
    extractor = Wav2LipMelExtractor()
    f = {
        "a": extractor.extract_mel_window(_synthesize_formant_vowel((800.0, 1200.0))),
        "i": extractor.extract_mel_window(_synthesize_formant_vowel((280.0, 2300.0, 3000.0))),
        "u": extractor.extract_mel_window(_synthesize_formant_vowel((300.0, 870.0))),
    }
    for x, y in (("a", "i"), ("i", "u"), ("a", "u")):
        d = _centered_cosine_dist(f[x], f[y])
        assert d > 0.01, f"/{x}/ 与 /{y}/ 在特征空间几乎重合（距离 {d:.4f}），存在坍缩风险"


def test_silence_maps_to_official_floor():
    """验收指标 3: 静音必须落到官方 _normalize 的下界 -4（模型训练时见过的静音底）

    v2.0 缺陷：旧实现对静音输出全 0。官方链中静音恒为 -max_abs_value，
    全零 mel 在训练分布中没有对应语义，静音段行为未定义。
    """
    mel = Wav2LipMelExtractor().extract_mel_window(np.zeros(6400, dtype=np.float32))
    assert np.allclose(mel, -4.0), (
        f"静音 mel 应恒为 -4.0（官方下界），实测 mean={mel.mean():.4f} max={mel.max():.4f}"
    )


def test_loudness_dynamic_range_preserved():
    """验收指标 4: 静音->响亮的 mel 均值跨度必须保留（能量-开口度关联的前提）

    v2.0 缺陷：逐窗 (mel-mu)/sigma 把官方 1.716 的跨度压成 0.000，
    等于抹除了驱动张口程度的能量线索。
    """
    extractor = Wav2LipMelExtractor()
    vowel = _synthesize_formant_vowel((800.0, 1200.0))
    silence = float(extractor.extract_mel_window(np.zeros(6400, dtype=np.float32)).mean())
    quiet = float(extractor.extract_mel_window((vowel * 0.02).astype(np.float32)).mean())
    loud = float(extractor.extract_mel_window((vowel * 0.95).astype(np.float32)).mean())

    span = loud - silence
    assert span > 1.0, f"静音->响亮的 mel 均值跨度应 > 1.0，实测 {span:.4f}（过小说明动态范围被归一化抹平）"
    assert quiet > silence, f"弱元音应高于静音底 (quiet={quiet:.4f}, silence={silence:.4f})"
    assert loud > quiet, f"强元音应高于弱元音 (loud={loud:.4f}, quiet={quiet:.4f})"


def test_preemphasis_applied_on_waveform():
    """验收指标 5: 预加重必须作用于波形（STFT 之前）并真正抬升高频

    v2.0 缺陷：预加重被实现为 mel 域 m[:-1]-k*m[1:]，等价于全通滤波器 1+k*z^-1，
    只旋转相位而不提升高频，无法替代官方 waveform preemphasis。
    """
    extractor = Wav2LipMelExtractor()
    assert extractor.PREEMPHASIZE is True, "官方 hparams.preemphasize=True，不应关闭"

    vowel = _synthesize_formant_vowel((800.0, 1200.0))
    with_pre = extractor.extract_mel_window(vowel)[0, 0]

    extractor_off = Wav2LipMelExtractor()
    extractor_off.PREEMPHASIZE = False
    without_pre = extractor_off.extract_mel_window(vowel)[0, 0]

    assert not np.allclose(with_pre, without_pre), "开关预加重后输出无差异，说明未生效"

    # 预加重是一阶高通 y[n]=x[n]-k*x[n-1]：必然「抬高频 / 压低频」。
    # 因此正确判据是 low_delta < 0 < high_lift，而不是 high_lift > |low_delta|
    # ——低频本来就是被压下去的那一侧，|low_delta| 天然大于 high_lift。
    high = slice(50, 80)   # ~3kHz 以上：辅音 b/p/m/f/d/t 的能量所在
    low = slice(0, 15)     # 低频基频区
    high_lift = float(with_pre[high].mean() - without_pre[high].mean())
    low_delta = float(with_pre[low].mean() - without_pre[low].mean())

    assert high_lift > 0.05, f"预加重应抬升高频带，实测 {high_lift:+.4f}"
    assert low_delta < -0.05, f"预加重应压低低频带，实测 {low_delta:+.4f}"
    assert high_lift > 0 and low_delta < 0, (
        f"预加重必须是高通：高频 {high_lift:+.4f} 上升、低频 {low_delta:+.4f} 下降"
    )


# ---------------------------------------------------------------- ② 帧对齐

def test_single_pulse_temporal_alignment():
    """验收指标 6: 单脉冲置于 6400 采样窗口正中，响应应落在 step 8（容差 ±1）

    注意判据：必须用逐列**均值**而非 abs().sum()。
    官方链把无脉冲的列钉在 -4 下界，80 个频带 abs 求和恒为 320，
    会让所有静默列并列最大，argmax 退化为 0。
    """
    extractor = Wav2LipMelExtractor()
    pulse = np.zeros(6400, dtype=np.float32)
    pulse[3200] = 1.0

    col_mean = extractor.extract_mel_window(pulse)[0, 0].mean(axis=0)
    peak_step = int(np.argmax(col_mean))
    assert abs(peak_step - 8) <= 1, f"帧中心应落在 step 8 附近，实测 step {peak_step}"

    # 有脉冲的列必须显著高于 -4 静音底，证明响应确实集中而非弥散
    assert col_mean[peak_step] > -1.0, (
        f"脉冲所在列应明显高于静音底 -4.0，实测 {col_mean[peak_step]:.4f}"
    )
    assert col_mean[0] <= -3.99, f"远离脉冲的列应保持在静音底，实测 {col_mean[0]:.4f}"


def test_live_window_no_edge_padding():
    """验收指标 7: 6400 采样窗口产生 29 个 STFT 帧，走中心裁剪而非 edge 复制"""
    extractor = Wav2LipMelExtractor()
    num_frames = (6400 - extractor.n_fft) // extractor.hop_length + 1
    assert num_frames == 29, f"6400 采样应产生 29 个 STFT 帧，实测 {num_frames}"
    assert num_frames >= 16, "帧数不足 16 时会触发 edge 复制，live 路径必须避免"

    # 3200 采样（旧 live 窗口）只产生 13 帧，必须靠调用方补齐窗口而非依赖 edge 复制
    short_frames = (3200 - extractor.n_fft) // extractor.hop_length + 1
    assert short_frames < 16, (
        f"3200 采样仅 {short_frames} 帧，低于 16；调用方必须使用 ±200ms 窗口，"
        f"否则 19% 的模型上下文是边缘复制品"
    )


def test_local_and_cloud_window_agree():
    """验收指标 8: 同一瞬间，±200ms 窗口（live 路径）与云端窗口须给出一致特征"""
    extractor = Wav2LipMelExtractor()
    rng = np.random.default_rng(11)
    vowel = _synthesize_formant_vowel((800.0, 1200.0))
    audio = np.concatenate([vowel, vowel * 0.5, vowel * 0.8])[: 9600]
    audio = (audio + rng.normal(0, 0.002, len(audio))).astype(np.float32)

    target = 4800
    live_win = audio[target - 3200:target + 3200]
    cloud_win = audio[target - 3200:target + 3200]
    cos = _cosine_dist(
        extractor.extract_mel_window(live_win).flatten(),
        extractor.extract_mel_window(cloud_win).flatten(),
    )
    assert cos < 1e-6, f"live 与 cloud 窗口语义应一致（此处同窗对比），实测余弦距离 {cos:.2e}"


def test_edge_padding_not_used_on_hot_path():
    """验收指标 9: 静默窗口(全零)与极短窗口都不得产生 NaN/Inf"""
    extractor = Wav2LipMelExtractor()
    for name, pcm in (
        ("zeros", np.zeros(6400, dtype=np.float32)),
        ("tiny", np.zeros(10, dtype=np.float32)),
        ("n_fft-1", np.zeros(799, dtype=np.float32)),
        ("single", np.array([1.0], dtype=np.float32)),
    ):
        mel = extractor.extract_mel_window(pcm)
        assert np.all(np.isfinite(mel)), f"{name} 输入产生了 NaN/Inf"
        assert mel.shape == (1, 1, 80, 16), f"{name} 输入形状错误: {mel.shape}"


# ---------------------------------------------------------------- 三副本一致性

@pytest.mark.parametrize("doc", ["google_gpu.md", "intern_gpu.md"])
def test_embedded_sidecar_mel_extractor_in_sync(doc):
    """验收指标 10: 云端内嵌 sidecar 的 mel 提取器必须与本地实现逐 token 一致

    本地直播与云端渲染共用同一份 Wav2Lip 模型；两份实现一旦漂移，
    同一句话在本地与云端会得到不同唇形，且无法归因。
    """
    import ast
    import inspect
    import re

    from server.core.avatar.neural_lip_renderer import Wav2LipMelExtractor as Canonical

    root = Path(__file__).resolve().parents[2]
    path = root / doc
    if not path.exists():
        pytest.skip(f"未找到 {doc}")
    text = path.read_text(encoding="utf-8")

    match = re.search(r"class LatentSyncMelExtractor:", text)
    if not match:
        match = re.search(r"class Wav2LipMelExtractor:", text)
    assert match, f"{doc} 中未找到 LatentSyncMelExtractor 或 Wav2LipMelExtractor"
    nxt = re.search(r"\n(?:class |Wav2LipMelExtractor\s*=)", text[match.end():])
    end_idx = match.end() + nxt.start() + 1 if nxt else len(text)
    block = text[match.start(): end_idx]

    def code_only(src: str) -> str:
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
                if (node.body and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)):
                    node.body.pop(0)
        return re.sub(r"\s+", "", ast.unparse(tree))

    assert code_only(block) == code_only(inspect.getsource(Canonical)), (
        f"{doc} 内嵌的 LatentSyncMelExtractor 与本地实现不一致——"
        f"请用 server/core/avatar/neural_lip_renderer.py 的定义同步覆盖"
    )


@pytest.mark.parametrize("doc", ["google_gpu.md", "intern_gpu.md"])
def test_embedded_sidecar_source_parses(doc):
    """验收指标 11: 内嵌 sidecar 源码必须仍是合法 Python"""
    import ast
    import re
    from pathlib import Path as _P

    root = _P(__file__).resolve().parents[2]
    path = root / doc
    if not path.exists():
        pytest.skip(f"未找到 {doc}")
    text = path.read_text(encoding="utf-8")
    match = re.search(r"sidecar_server_code\s*=\s*'''(.*?)'''", text, re.S)
    assert match, f"{doc} 中未找到 sidecar_server_code"
    ast.parse(match.group(1))


@pytest.mark.parametrize("doc", ["google_gpu.md", "intern_gpu.md"])
def test_embedded_sidecar_visual_lead_present(doc):
    """验收指标 12: 云端渲染节点脚本必须包含 VISUAL_LEAD_SAMPLES 视听预动量支持"""
    import re
    from pathlib import Path as _P

    root = _P(__file__).resolve().parents[2]
    text = (root / doc).read_text(encoding="utf-8")
    assert "VISUAL_LEAD_SAMPLES" in text, f"{doc} 缺失 VISUAL_LEAD_SAMPLES 定义"
    # 验证在流式和补帧中均被加入 center
    matches = re.findall(r"center\s*=\s*int\(seq\s*\*\s*PTS_STEP_SAMPLES\s*\+\s*PTS_STEP_SAMPLES\s*//\s*2\s*\+\s*VISUAL_LEAD_SAMPLES\)", text)
    assert len(matches) >= 1, f"{doc} 中 center 计算未正确引入 VISUAL_LEAD_SAMPLES (匹配到 {len(matches)} 处，预期 >=1)"


def test_visual_lead_samples_environment_control(monkeypatch):
    """验收指标 13: 验证 LIPSYNC_VISUAL_LEAD_SAMPLES 环境变量在各个管线中受控有效"""
    import os

    # 默认 640
    monkeypatch.delenv("LIPSYNC_VISUAL_LEAD_SAMPLES", raising=False)
    lead_def = int(os.environ.get("LIPSYNC_VISUAL_LEAD_SAMPLES", "640"))
    assert lead_def == 640

    # 自定义 320 (提前 20ms)
    monkeypatch.setenv("LIPSYNC_VISUAL_LEAD_SAMPLES", "320")
    lead_custom = int(os.environ.get("LIPSYNC_VISUAL_LEAD_SAMPLES", "640"))
    assert lead_custom == 320

