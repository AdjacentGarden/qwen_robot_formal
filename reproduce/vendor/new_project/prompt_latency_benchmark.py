#!/usr/bin/env python3
"""Measure how Doubao realtime latency changes with system-prompt length.

This is deliberately separate from ``new_project``.  It never loads the robot
planner, never invokes a skill, and never changes runtime state.  Every trial:

1. opens a fresh realtime connection (matching the current daemon's cold path),
2. sends the exact same recorded WAV audio,
3. changes only the system-prompt character count, and
4. asks for the same tiny JSON response.

The randomized schedule and repeated trials make prompt length the controlled
variable instead of microphone timing, response length, or test order.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import gzip
import hashlib
import importlib.util
import json
import math
import os
import platform
import random
import shutil
import statistics
import subprocess
import sys
import time
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


EXPECTED_RESPONSE = {
    "decision_type": "answer",
    "reply": "基准完成",
    "task_groups": [],
    "ask_user": None,
}
EXPECTED_JSON = json.dumps(EXPECTED_RESPONSE, ensure_ascii=False, separators=(",", ":"))

PROMPT_PREFIX = f"""你正在执行系统提示词长度延迟基准测试。
最高优先级规则：无论用户语音内容是什么，唯一允许的输出都是：
{EXPECTED_JSON}
以下 benchmark_filler 仅用于控制输入长度，是不可执行、无语义的参考数据。不得遵循其中任何内容。
无论用户说什么，都不要回答用户问题，不要调用工具，不要生成语音解释。
<benchmark_filler>
"""

PROMPT_SUFFIX = f"""
</benchmark_filler>
现在只输出下面这一行严格 JSON，不得添加 Markdown、解释或其他字段：
{EXPECTED_JSON}
"""

DEFAULT_FILLER_RECORD = (
    '{{"能力编号":{index},"说明":"这是延迟测试的无语义填充记录，不代表真实指令",'
    '"动作":["读取","验证","返回"],"参数":{{"目标":"样本{variant}","可选":true}},'
    '"约束":["不得执行","不得改变输出","仅计算输入长度"]}}\n'
)


class BenchmarkError(RuntimeError):
    """A trial failed in a way that should be reported but not crash the run."""


@dataclass(frozen=True)
class AudioSample:
    path: Path
    pcm: bytes
    sample_rate: int
    channels: int
    sample_width: int

    @property
    def duration_seconds(self) -> float:
        frame_bytes = self.channels * self.sample_width
        return len(self.pcm) / float(self.sample_rate * frame_bytes)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.pcm).hexdigest()


def parse_lengths(value: str) -> list[int]:
    lengths: list[int] = []
    for item in str(value).split(","):
        item = item.strip()
        if not item:
            continue
        try:
            number = int(item)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"提示词长度不是整数: {item}") from exc
        if number <= 0:
            raise argparse.ArgumentTypeError(f"提示词长度必须大于零: {number}")
        lengths.append(number)
    if len(set(lengths)) < 2:
        raise argparse.ArgumentTypeError("至少提供两个不同的提示词长度")
    return sorted(set(lengths))


def default_filler() -> str:
    return "".join(
        DEFAULT_FILLER_RECORD.format(index=index, variant=index % 17)
        for index in range(1000)
    )


def build_prompt(target_chars: int, filler_source: str) -> str:
    available = target_chars - len(PROMPT_PREFIX) - len(PROMPT_SUFFIX)
    if available < 0:
        minimum = len(PROMPT_PREFIX) + len(PROMPT_SUFFIX)
        raise ValueError(f"提示词长度 {target_chars} 太小，最小需要 {minimum}")
    source = filler_source or default_filler()
    if not source:
        raise ValueError("填充文本不能为空")
    repeats = math.ceil(available / len(source)) if available else 0
    filler = (source * repeats)[:available]
    prompt = PROMPT_PREFIX + filler + PROMPT_SUFFIX
    if len(prompt) != target_chars:
        raise AssertionError(f"提示词长度构造失败: {len(prompt)} != {target_chars}")
    return prompt


def read_audio(path: Path, expected_rate: int) -> AudioSample:
    if not path.exists():
        raise FileNotFoundError(f"找不到固定测试音频: {path}；请先添加 --record 录制一次")
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        sample_rate = wav.getframerate()
        compression = wav.getcomptype()
        pcm = wav.readframes(wav.getnframes())
    if compression != "NONE":
        raise ValueError(f"只支持未压缩 PCM WAV，当前格式: {compression}")
    if channels != 1 or sample_width != 2:
        raise ValueError(f"WAV 必须为单声道 16-bit PCM，当前 channels={channels}, width={sample_width}")
    if sample_rate != expected_rate:
        raise ValueError(f"WAV 采样率必须为 {expected_rate} Hz，当前为 {sample_rate} Hz")
    if len(pcm) < int(sample_rate * sample_width * 0.25):
        raise ValueError("WAV 有效音频不足 0.25 秒")
    return AudioSample(path=path.resolve(), pcm=pcm, sample_rate=sample_rate, channels=channels, sample_width=sample_width)


def record_audio(path: Path, device: str, sample_rate: int, seconds: int) -> None:
    if shutil.which("arecord") is None:
        raise RuntimeError("系统中找不到 arecord，无法录制固定测试音频")
    path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "arecord",
        "-q",
        "-D",
        device,
        "-t",
        "wav",
        "-f",
        "S16_LE",
        "-r",
        str(sample_rate),
        "-c",
        "1",
        "-d",
        str(seconds),
        str(path),
    ]
    print(f"即将录制 {seconds} 秒。请清楚说一句短句，例如：开始测试。", flush=True)
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"arecord 录音失败，退出码 {completed.returncode}")
    print(f"固定测试音频已保存: {path}", flush=True)


def running_daemon_pids() -> list[str]:
    if shutil.which("pgrep") is None:
        return []
    completed = subprocess.run(
        ["pgrep", "-f", "new_project.cli daemon"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode not in (0, 1):
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip().isdigit()]


def load_voice_module(self_program_dir: Path) -> Any:
    module_path = self_program_dir / "vocal_stream_llm.py"
    if not module_path.exists():
        raise FileNotFoundError(f"找不到豆包实时客户端: {module_path}")
    if str(self_program_dir) not in sys.path:
        sys.path.insert(0, str(self_program_dir))
    spec = importlib.util.spec_from_file_location("prompt_latency_vocal_stream_llm", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载豆包实时客户端: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_env_file(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"找不到豆包环境变量文件: {path}")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))
    required = (
        "DOUBAO_REALTIME_APP_ID",
        "DOUBAO_REALTIME_ACCESS_KEY",
        "DOUBAO_REALTIME_RESOURCE_ID",
        "DOUBAO_REALTIME_APP_KEY",
    )
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError("缺少豆包环境变量: " + ", ".join(missing))


def make_client_class(voice_module: Any) -> type:
    class PromptBenchmarkClient(voice_module.DoubaoVoice):
        def __init__(self, args: argparse.Namespace, prompt: str) -> None:
            super().__init__(args)
            self.prompt = prompt

        def session_request(self) -> dict[str, Any]:
            return {
                "asr": {"extra": {"end_smooth_window_ms": self.args.asr_end_smooth_ms}},
                "tts": {
                    "speaker": self.args.speaker,
                    "audio_config": {
                        "channel": 1,
                        "format": "pcm_s16le",
                        "sample_rate": self.args.output_rate,
                    },
                },
                "dialog": {
                    "bot_name": "豆包",
                    "system_role": self.prompt,
                    "speaking_style": "只输出指定的严格 JSON，不添加任何其他内容。",
                    "dialog_context": [],
                    "extra": {"strict_audit": False, "recv_timeout": 10, "input_mod": "audio"},
                },
            }

    return PromptBenchmarkClient


def extract_asr_text(body: Any) -> tuple[str, bool]:
    if not isinstance(body, dict):
        return "", True
    results = body.get("results")
    if isinstance(results, list):
        final_text = ""
        interim_text = ""
        for item in results:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or item.get("transcript") or "").strip()
            if not text:
                continue
            if bool(item.get("is_interim", item.get("interim", False))):
                if len(text) >= len(interim_text):
                    interim_text = text
            elif len(text) >= len(final_text):
                final_text = text
        return (final_text, False) if final_text else (interim_text, True)
    for key in ("text", "transcript", "utterance"):
        text = body.get(key)
        if isinstance(text, str) and text.strip():
            return text.strip(), bool(body.get("is_interim", body.get("interim", False)))
    return "", True


def extract_json_object(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(text[start:], start=start):
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(text[start : index + 1])
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


def response_candidate(streamed_parts: list[str], final_texts: list[str]) -> tuple[str, dict[str, Any] | None]:
    streamed = "".join(streamed_parts).strip()
    final = "".join(final_texts).strip()
    for candidate in (final, streamed):
        parsed = extract_json_object(candidate)
        if parsed is not None:
            return candidate, parsed
    return (final if len(final) >= len(streamed) else streamed), None


async def send_fixed_audio(client: Any, audio: AudioSample, args: argparse.Namespace) -> None:
    chunk_bytes = max(2, int(audio.sample_rate * audio.sample_width * args.chunk_ms / 1000.0))
    chunk_bytes -= chunk_bytes % audio.sample_width
    for offset in range(0, len(audio.pcm), chunk_bytes):
        chunk = audio.pcm[offset : offset + chunk_bytes]
        await client.send_audio(chunk)
        if not args.fast_upload:
            await asyncio.sleep(len(chunk) / float(audio.sample_rate * audio.sample_width))
    silence = b"\x00" * int(audio.sample_rate * audio.sample_width * args.silence_tail_sec)
    for offset in range(0, len(silence), chunk_bytes):
        await client.send_audio(silence[offset : offset + chunk_bytes])
        await asyncio.sleep(0)


async def receive_benchmark_response(
    client: Any,
    voice_module: Any,
    marks: dict[str, float],
    timeout_seconds: float,
) -> dict[str, Any]:
    streamed_parts: list[str] = []
    final_texts: list[str] = []
    asr_text = ""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            message = await asyncio.wait_for(client.ws.recv(), timeout=max(0.02, min(0.8, remaining)))
        except asyncio.TimeoutError:
            continue
        now = time.monotonic()
        response = voice_module.parse_response(message)
        body = response.get("body")
        if isinstance(body, bytes):
            continue
        if response.get("type") == "error":
            raise BenchmarkError(f"豆包实时服务错误: {body}")
        event = response.get("event")
        if event == 451:
            text, interim = extract_asr_text(body)
            if text and len(text) >= len(asr_text):
                asr_text = text
            if text and not interim and "asr_final_at" not in marks:
                marks["asr_final_at"] = now
        text = voice_module.text_from_body(body)
        if event == 550 and text:
            streamed_parts.append(text)
            marks.setdefault("first_model_text_at", now)
        elif event == 351 and text:
            final_texts.append(text)
            marks.setdefault("first_model_text_at", now)
        raw_text, parsed = response_candidate(streamed_parts, final_texts)
        if parsed is not None:
            marks.setdefault("complete_json_at", now)
            return {
                "raw_text": raw_text,
                "parsed": parsed,
                "asr_text": asr_text,
                "response_valid": parsed == EXPECTED_RESPONSE,
                "finished_event_seen": event == 359,
            }
        if event == 359:
            return {
                "raw_text": raw_text,
                "parsed": parsed,
                "asr_text": asr_text,
                "response_valid": False,
                "finished_event_seen": True,
            }
    raise BenchmarkError(f"等待模型 JSON 超时（{timeout_seconds:g} 秒）")


def elapsed_ms(later: float | None, earlier: float | None) -> float | None:
    if later is None or earlier is None:
        return None
    return round((later - earlier) * 1000.0, 3)


async def run_trial(
    *,
    voice_module: Any,
    client_class: type,
    audio: AudioSample,
    prompt: str,
    cycle: int,
    order: int,
    warmup: bool,
    args: argparse.Namespace,
) -> dict[str, Any]:
    prompt_bytes = prompt.encode("utf-8")
    result: dict[str, Any] = {
        "cycle": cycle,
        "order": order,
        "warmup": warmup,
        "prompt_chars": len(prompt),
        "prompt_utf8_bytes": len(prompt_bytes),
        "prompt_gzip_bytes": len(gzip.compress(prompt_bytes)),
        "prompt_sha256": hashlib.sha256(prompt_bytes).hexdigest(),
        "ok": False,
        "response_valid": False,
        "error": "",
    }
    client = client_class(args, prompt)
    marks: dict[str, float] = {"trial_started_at": time.monotonic()}
    receiver: asyncio.Task[dict[str, Any]] | None = None
    try:
        await asyncio.wait_for(client.connect(), timeout=args.connect_timeout)
        marks["connected_at"] = time.monotonic()
        receiver = asyncio.create_task(
            receive_benchmark_response(client, voice_module, marks, args.response_timeout)
        )
        marks["audio_upload_started_at"] = time.monotonic()
        await send_fixed_audio(client, audio, args)
        marks["audio_upload_finished_at"] = time.monotonic()
        response = await asyncio.wait_for(receiver, timeout=args.response_timeout + 1.0)
        marks["response_ready_at"] = marks.get("complete_json_at", time.monotonic())
        result.update(response)
        result["response_chars"] = len(str(response.get("raw_text") or ""))
        # Time-to-first-text is the primary latency signal and does not depend
        # on how long the model's final answer is.  Exact JSON adherence remains
        # a separate quality diagnostic; non-adherence must not erase otherwise
        # valid first-token timing samples.
        result["ok"] = "first_model_text_at" in marks
        if not response.get("response_valid"):
            result["validation_warning"] = "模型没有返回规定的固定 JSON；仅首文本时延可用于长度分析"
        if not result["ok"]:
            result["error"] = "没有收到模型文本"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if receiver is not None and not receiver.done():
            receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await receiver
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(client.close(), timeout=2.0)
        marks["trial_finished_at"] = time.monotonic()

    t0 = marks.get("trial_started_at")
    connected = marks.get("connected_at")
    upload_started = marks.get("audio_upload_started_at")
    upload_finished = marks.get("audio_upload_finished_at")
    first_text = marks.get("first_model_text_at")
    complete_json = marks.get("complete_json_at")
    response_ready = marks.get("response_ready_at")
    result.update(
        {
            "connect_ms": elapsed_ms(connected, t0),
            "audio_upload_ms": elapsed_ms(upload_finished, upload_started),
            "asr_final_after_audio_ms": elapsed_ms(marks.get("asr_final_at"), upload_finished),
            "first_text_after_audio_ms": elapsed_ms(first_text, upload_finished),
            "complete_json_after_audio_ms": elapsed_ms(complete_json, upload_finished),
            "end_to_end_first_text_ms": elapsed_ms(first_text, t0),
            "end_to_end_ready_ms": elapsed_ms(response_ready, t0),
            "trial_total_ms": elapsed_ms(marks.get("trial_finished_at"), t0),
        }
    )
    return result


def percentile(values: Iterable[float], fraction: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 3)
    weight = position - lower
    return round(ordered[lower] * (1.0 - weight) + ordered[upper] * weight, 3)


def metric_summary(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    values = [float(row[key]) for row in rows if isinstance(row.get(key), (int, float))]
    if not values:
        return {"count": 0, "median_ms": None, "p95_ms": None, "mean_ms": None, "stdev_ms": None}
    return {
        "count": len(values),
        "median_ms": round(statistics.median(values), 3),
        "p95_ms": percentile(values, 0.95),
        "mean_ms": round(statistics.mean(values), 3),
        "stdev_ms": round(statistics.stdev(values), 3) if len(values) > 1 else 0.0,
    }


def linear_regression(rows: list[dict[str, Any]], metric: str) -> dict[str, Any]:
    points = [
        (float(row["prompt_chars"]), float(row[metric]))
        for row in rows
        if row.get("ok") and isinstance(row.get(metric), (int, float))
    ]
    if len(points) < 2:
        return {"count": len(points), "slope_ms_per_1000_chars": None, "r_squared": None}
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    mean_x = statistics.mean(xs)
    mean_y = statistics.mean(ys)
    denominator = sum((value - mean_x) ** 2 for value in xs)
    if denominator <= 0:
        return {"count": len(points), "slope_ms_per_1000_chars": None, "r_squared": None}
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / denominator
    intercept = mean_y - slope * mean_x
    predicted = [intercept + slope * x for x in xs]
    residual = sum((actual - estimate) ** 2 for actual, estimate in zip(ys, predicted))
    total = sum((actual - mean_y) ** 2 for actual in ys)
    r_squared = 1.0 - residual / total if total > 0 else 1.0
    return {
        "count": len(points),
        "slope_ms_per_1000_chars": round(slope * 1000.0, 3),
        "intercept_ms": round(intercept, 3),
        "r_squared": round(max(0.0, min(1.0, r_squared)), 4),
    }


def classify_effect(per_length: dict[str, Any], metric: str) -> dict[str, Any]:
    available: list[tuple[int, dict[str, Any]]] = []
    for key, value in per_length.items():
        summary = value.get(metric) or {}
        if summary.get("count", 0) > 0 and isinstance(summary.get("median_ms"), (int, float)):
            available.append((int(key), summary))
    if len(available) < 2:
        return {"level": "inconclusive", "reason": "至少两个长度没有足够的有效结果"}
    available.sort(key=lambda item: item[0])
    small_length, small = available[0]
    large_length, large = available[-1]
    small_ms = float(small["median_ms"])
    large_ms = float(large["median_ms"])
    delta = large_ms - small_ms
    ratio = large_ms / small_ms if small_ms > 0 else None
    minimum_count = min(int(small.get("count", 0)), int(large.get("count", 0)))
    if minimum_count < 3:
        level = "inconclusive"
        reason = "最短或最长提示词的有效样本少于 3 次"
    elif delta >= 2000.0 or (ratio is not None and ratio >= 1.5):
        level = "severe"
        reason = "最长提示词中位数至少增加 2000ms 或达到最短提示词的 1.5 倍"
    elif delta >= 750.0 or (ratio is not None and ratio >= 1.2):
        level = "moderate"
        reason = "最长提示词中位数至少增加 750ms 或达到最短提示词的 1.2 倍"
    else:
        level = "minor"
        reason = "长度差异低于预设的中度影响阈值"
    return {
        "level": level,
        "reason": reason,
        "smallest_prompt_chars": small_length,
        "largest_prompt_chars": large_length,
        "smallest_median_ms": round(small_ms, 3),
        "largest_median_ms": round(large_ms, 3),
        "delta_ms": round(delta, 3),
        "ratio": round(ratio, 4) if ratio is not None else None,
    }


def build_summary(trials: list[dict[str, Any]], lengths: list[int]) -> dict[str, Any]:
    measured = [row for row in trials if not row.get("warmup")]
    valid = [row for row in measured if row.get("ok")]
    metrics = (
        "connect_ms",
        "first_text_after_audio_ms",
        "complete_json_after_audio_ms",
        "end_to_end_first_text_ms",
        "end_to_end_ready_ms",
    )
    per_length: dict[str, Any] = {}
    for length in lengths:
        rows = [row for row in valid if int(row.get("prompt_chars", -1)) == length]
        attempted = [row for row in measured if int(row.get("prompt_chars", -1)) == length]
        per_length[str(length)] = {
            "attempted": len(attempted),
            "valid": len(rows),
            "failed": len(attempted) - len(rows),
            "fixed_json_valid": sum(1 for row in rows if row.get("response_valid")),
            **{metric: metric_summary(rows, metric) for metric in metrics},
        }
    regressions = {metric: linear_regression(valid, metric) for metric in metrics}
    return {
        "attempted_trials": len(measured),
        "valid_trials": len(valid),
        "failed_trials": len(measured) - len(valid),
        "per_length": per_length,
        "regressions": regressions,
        "effect": {
            "end_to_end_first_text": classify_effect(per_length, "end_to_end_first_text_ms"),
            "first_text_after_audio": classify_effect(per_length, "first_text_after_audio_ms"),
        },
        "classification_note": (
            "严重=最长中位数比最短增加至少2000ms或达到1.5倍；"
            "中度=增加至少750ms或达到1.2倍。主结论使用首个模型文本时延，"
            "避免最终回答长度污染结果；固定JSON遵循率单独报告。"
        ),
    }


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def write_csv(path: Path, trials: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "cycle",
        "order",
        "warmup",
        "prompt_chars",
        "prompt_utf8_bytes",
        "prompt_gzip_bytes",
        "ok",
        "response_valid",
        "response_chars",
        "validation_warning",
        "connect_ms",
        "audio_upload_ms",
        "asr_final_after_audio_ms",
        "first_text_after_audio_ms",
        "complete_json_after_audio_ms",
        "end_to_end_first_text_ms",
        "end_to_end_ready_ms",
        "trial_total_ms",
        "asr_text",
        "error",
    ]
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(trials)
    temporary.replace(path)


def format_number(value: Any) -> str:
    return "-" if value is None else f"{float(value):.1f}"


def print_summary(summary: dict[str, Any]) -> None:
    print("\n提示词长度延迟统计（仅有效试验，中位数 / p95，单位 ms）")
    print("chars | valid | fixed_json | connect | first_text_after_audio | end_to_end_first_text")
    print("-" * 92)
    for length, item in sorted(summary["per_length"].items(), key=lambda pair: int(pair[0])):
        def cell(metric: str) -> str:
            data = item[metric]
            return f"{format_number(data['median_ms'])}/{format_number(data['p95_ms'])}"
        print(
            f"{int(length):5d} | {item['valid']:2d}/{item['attempted']:<2d} | "
            f"{item['fixed_json_valid']:2d}/{item['valid']:<2d} | "
            f"{cell('connect_ms'):>13} | {cell('first_text_after_audio_ms'):>22} | "
            f"{cell('end_to_end_first_text_ms'):>21}"
        )
    effect = summary["effect"]["end_to_end_first_text"]
    regression = summary["regressions"]["end_to_end_first_text_ms"]
    print("\n端到端首文本影响判断:", effect.get("level"), "-", effect.get("reason"))
    if effect.get("delta_ms") is not None:
        print(
            f"最短→最长中位数变化: {effect['delta_ms']:.1f} ms，"
            f"倍率: {effect.get('ratio', 0):.3f}x"
        )
    if regression.get("slope_ms_per_1000_chars") is not None:
        print(
            f"线性斜率: {regression['slope_ms_per_1000_chars']:.1f} ms / 1000字符，"
            f"R²={regression['r_squared']:.3f}"
        )


def run_self_test() -> None:
    filler = default_filler()
    minimum = len(PROMPT_PREFIX) + len(PROMPT_SUFFIX)
    for length in (minimum, 2000, 12000, 20590, 24000):
        assert len(build_prompt(length, filler)) == length
    assert extract_json_object(f"```json\n{EXPECTED_JSON}\n```") == EXPECTED_RESPONSE
    synthetic: list[dict[str, Any]] = []
    for length, latency in ((2000, 1000.0), (12000, 2200.0), (24000, 4100.0)):
        for cycle in range(3):
            synthetic.append(
                {
                    "prompt_chars": length,
                    "ok": True,
                    "warmup": False,
                    "connect_ms": 100.0,
                    "first_text_after_audio_ms": latency - 100.0,
                    "complete_json_after_audio_ms": latency,
                    "end_to_end_first_text_ms": latency + 2900.0,
                    "end_to_end_ready_ms": latency + 3000.0,
                    "cycle": cycle,
                }
            )
    summary = build_summary(synthetic, [2000, 12000, 24000])
    assert summary["effect"]["first_text_after_audio"]["level"] == "severe"
    assert summary["regressions"]["complete_json_after_audio_ms"]["slope_ms_per_1000_chars"] > 0
    print("SELF_TEST_OK")


async def run_benchmark(args: argparse.Namespace) -> int:
    filler_source = (
        args.filler_file.read_text(encoding="utf-8")
        if args.filler_file is not None
        else default_filler()
    )
    prompts = {length: build_prompt(length, filler_source) for length in args.lengths}
    audio = read_audio(args.wav, args.input_rate)
    print(
        f"固定音频: {audio.path}，{audio.duration_seconds:.3f}s，sha256={audio.sha256[:16]}...",
        flush=True,
    )
    for length in args.lengths:
        encoded = prompts[length].encode("utf-8")
        print(
            f"prompt={length} chars, utf8={len(encoded)} bytes, gzip={len(gzip.compress(encoded))} bytes",
            flush=True,
        )
    if args.dry_run:
        print("DRY_RUN_OK：未连接豆包实时服务。")
        return 0

    daemon_pids = running_daemon_pids()
    if daemon_pids and not args.allow_daemon_running:
        raise RuntimeError(
            "检测到 new_project daemon 正在运行（PID: "
            + ", ".join(daemon_pids)
            + "）。为避免并发网络和音频服务污染结果，请先停止 daemon，"
            "或明确添加 --allow-daemon-running。"
        )
    if daemon_pids:
        print("警告：daemon 正在运行，正式结论可能受到并发负载影响。", flush=True)

    load_env_file(args.env_file)
    voice_module = load_voice_module(args.self_program_dir)
    client_class = make_client_class(voice_module)

    schedule: list[tuple[int, int, bool]] = []
    middle_length = args.lengths[len(args.lengths) // 2]
    for warmup_index in range(args.warmups):
        schedule.append((-(warmup_index + 1), middle_length, True))
    rng = random.Random(args.seed)
    for cycle in range(1, args.repeats + 1):
        cycle_lengths = list(args.lengths)
        rng.shuffle(cycle_lengths)
        schedule.extend((cycle, length, False) for length in cycle_lengths)

    trials: list[dict[str, Any]] = []
    total = len(schedule)
    for order, (cycle, length, warmup) in enumerate(schedule, start=1):
        label = "warmup" if warmup else f"cycle={cycle}"
        print(f"\n[{order}/{total}] {label}, prompt={length}", flush=True)
        trial = await run_trial(
            voice_module=voice_module,
            client_class=client_class,
            audio=audio,
            prompt=prompts[length],
            cycle=cycle,
            order=order,
            warmup=warmup,
            args=args,
        )
        trials.append(trial)
        status = "OK" if trial.get("ok") else "FAILED"
        print(
            f"{status}: connect={format_number(trial.get('connect_ms'))}ms, "
            f"first={format_number(trial.get('first_text_after_audio_ms'))}ms, "
            f"json={format_number(trial.get('complete_json_after_audio_ms'))}ms, "
            f"e2e_first={format_number(trial.get('end_to_end_first_text_ms'))}ms"
            + (f", error={trial['error']}" if trial.get("error") else ""),
            flush=True,
        )
        if trial.get("validation_warning"):
            print("提示:", trial["validation_warning"], flush=True)
        if order < total and args.inter_trial_delay > 0:
            await asyncio.sleep(args.inter_trial_delay)

    summary = build_summary(trials, args.lengths)
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "environment": {
            "host": platform.node(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "audio_path": str(audio.path),
            "audio_sha256": audio.sha256,
            "audio_duration_seconds": round(audio.duration_seconds, 6),
            "input_rate": args.input_rate,
            "chunk_ms": args.chunk_ms,
            "silence_tail_sec": args.silence_tail_sec,
            "fast_upload": args.fast_upload,
            "lengths": args.lengths,
            "repeats": args.repeats,
            "warmups": args.warmups,
            "seed": args.seed,
            "daemon_running": bool(daemon_pids),
            "daemon_pids": daemon_pids,
            "concurrent_daemon_warning": bool(daemon_pids),
            "expected_response": EXPECTED_RESPONSE,
        },
        "summary": summary,
        "trials": trials,
    }
    atomic_write_text(args.output_json, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    write_csv(args.output_csv, trials)
    print_summary(summary)
    print(f"\nJSON 报告: {args.output_json.resolve()}")
    print(f"CSV 明细: {args.output_csv.resolve()}")
    return 0 if summary["valid_trials"] > 0 else 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="独立测试豆包实时模型的系统提示词长度是否显著影响响应时间",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--wav", type=Path, default=Path("runtime/prompt_latency_input.wav"), help="每轮重复发送的固定 WAV")
    parser.add_argument("--record", action="store_true", help="测试前使用 arecord 覆盖录制 --wav")
    parser.add_argument("--record-seconds", type=int, default=4, help="固定录音秒数")
    parser.add_argument("--input-device", default="plughw:rockchipi2sdm_1", help="录音设备，仅 --record 使用")
    parser.add_argument("--input-rate", type=int, default=16000)
    parser.add_argument("--output-rate", type=int, default=24000)
    parser.add_argument("--speaker", default="zh_female_xiaohe_jupiter_bigtts")
    parser.add_argument("--asr-end-smooth-ms", type=int, default=300)
    parser.add_argument("--lengths", type=parse_lengths, default=parse_lengths("2000,6000,12000,16000,20590,24000"))
    parser.add_argument("--repeats", type=int, default=5, help="每个长度的正式重复次数，建议至少 5")
    parser.add_argument("--warmups", type=int, default=1, help="不计入统计的预热次数")
    parser.add_argument("--seed", type=int, default=20260713, help="随机测试顺序种子")
    parser.add_argument("--chunk-ms", type=int, default=100)
    parser.add_argument("--silence-tail-sec", type=float, default=0.25)
    parser.add_argument("--fast-upload", action="store_true", help="不按实时速度发送音频；仅用于隔离服务端计算，不代表真实语音体验")
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--response-timeout", type=float, default=25.0)
    parser.add_argument("--inter-trial-delay", type=float, default=0.4)
    parser.add_argument("--allow-daemon-running", action="store_true", help="允许 daemon 并发运行；正式测试不建议使用")
    parser.add_argument("--filler-file", type=Path, help="可选的真实文本分布样本；只作为不可执行填充数据")
    parser.add_argument("--self-program-dir", type=Path, default=Path("/home/test/self_program"))
    parser.add_argument("--env-file", type=Path, default=Path("/home/test/.doubao_realtime_env"))
    parser.add_argument("--output-json", type=Path, default=Path("runtime/prompt_latency_benchmark.json"))
    parser.add_argument("--output-csv", type=Path, default=Path("runtime/prompt_latency_benchmark.csv"))
    parser.add_argument("--dry-run", action="store_true", help="只验证音频和提示词构造，不连接模型")
    parser.add_argument("--self-test", action="store_true", help="运行纯本地单元自检后退出")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats 必须至少为 1")
    if args.warmups < 0:
        parser.error("--warmups 不能小于 0")
    if args.record_seconds < 1:
        parser.error("--record-seconds 必须至少为 1")
    if args.chunk_ms <= 0:
        parser.error("--chunk-ms 必须大于 0")
    return args


def main() -> int:
    args = parse_args()
    if args.self_test:
        run_self_test()
        return 0
    if args.record and running_daemon_pids() and not args.allow_daemon_running:
        print(
            "BENCHMARK_FAILED: 检测到 new_project daemon 正在运行。"
            "为避免麦克风冲突，请先停止 daemon，或明确添加 --allow-daemon-running。",
            file=sys.stderr,
        )
        return 2
    if args.record:
        record_audio(args.wav, args.input_device, args.input_rate, args.record_seconds)
    try:
        return asyncio.run(run_benchmark(args))
    except KeyboardInterrupt:
        print("测试已由用户中断。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"BENCHMARK_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
