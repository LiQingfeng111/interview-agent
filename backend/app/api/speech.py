"""语音：ASR（语音转文字）+ TTS（文字转语音）。

## 技术选型

| 能力 | 方案 | 理由 |
|---|---|---|
| **ASR** | 浏览器原生 `webkitSpeechRecognition` | 零成本、零依赖、中文支持好；后端不做，省一次上传 |
| **TTS** | 浏览器原生 `speechSynthesis` | 同上；比调云端 TTS 快且免费 |
| **降级** | 后端 `/api/speech/transcribe`（可选接 Whisper） | 浏览器不支持时用 |

## 为什么用浏览器原生而不是后端

三个理由：
1. **延迟**：语音识别在本地，说完整句立刻出文字，不用上传音频等识别
2. **成本**：不花钱（Whisper API 按分钟收费）
3. **隐私**：面试内容不外传

代价：`webkitSpeechRecognition` 只有 Chrome/Edge 支持完整（Firefox 不支持）。
所以后端留了 `/api/speech/transcribe` 作为降级入口，但默认不启用（需要装 whisper）。

## 后端这个文件提供什么

1. `/api/speech/capabilities` —— 告诉前端后端支持什么（便于前端决定用哪条路）
2. `/api/speech/transcribe` —— 音频转文字（需要 whisper，没装则返回 501 + 明确说明）
3. `/api/speech/synthesize` —— 文字转语音（需要 edge-tts，没装则返回 501）
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import Response

from app.core import errors
from app.deps import current_user
from app.schemas import ApiResponse

log = logging.getLogger("interview.speech")

router = APIRouter()

ALLOWED_AUDIO = [".webm", ".wav", ".mp3", ".m4a", ".ogg"]
MAX_AUDIO_MB = 25


def _whisper_available() -> tuple[bool, str]:
    """检查 whisper 是否**真正可用**（不只是装了包）。

    ## 为什么要检查到"模型能加载"这一层

    实测踩到的坑：`faster-whisper` 装上后 `import` 成功，
    `_whisper_available()` 返回 True，接口报告"ASR 可用"——
    但实际调用时崩在模型下载：

        huggingface_hub.errors.LocalEntryNotFoundError:
        Got: ConnectError: [Errno 101] Network is unreachable

    因为 `WhisperModel("base")` 首次要联网下载 140MB 模型。
    在离线/受限网络环境下，"装了包"不等于"能用"。

    现在的判断顺序：
    1. `whisper` CLI 存在 → 可用（它要么带了模型，要么用户已下过）
    2. `faster_whisper` 可导入 → **再查本地有没有缓存模型**
    3. 都不可用 → 返回 False，接口据此如实报告

    这样前端拿到的 capabilities 是**可信的**，不会出现
    "显示能用、点了报错"。
    """
    # ① whisper CLI
    if shutil.which("whisper"):
        return True, "cli"

    # ② faster-whisper + 本地模型缓存检查
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False, ""

    if _has_local_whisper_model():
        return True, "faster-whisper"
    # 装了但没模型：标注出来，让上层给"需要下载模型"的提示
    return False, "faster-whisper(缺模型)"


def _has_local_whisper_model() -> bool:
    """检查 HuggingFace 缓存里有没有 whisper 模型。

    标准缓存路径：`~/.cache/huggingface/hub/models--Systran--faster-whisper-*`
    """
    try:
        home = Path.home()
        cache = home / ".cache" / "huggingface" / "hub"
        if not cache.exists():
            return False
        for p in cache.iterdir():
            name = p.name.lower()
            if "whisper" in name and p.is_dir():
                # 还要确认不是空目录（下载中断会留空壳）
                snapshots = p / "snapshots"
                if snapshots.exists() and any(snapshots.iterdir()):
                    return True
        return False
    except Exception:  # noqa: BLE001
        return False


def _tts_available() -> tuple[bool, str]:
    """检查 edge-tts 是否可用（免费、中文自然）。"""
    try:
        import edge_tts  # noqa: F401
        return True, "edge-tts"
    except ImportError:
        pass
    # 退化到系统 TTS（Windows SAPI / Linux espeak）
    if shutil.which("espeak-ng") or shutil.which("espeak"):
        return True, "system"
    return False, ""


@router.get("/capabilities", response_model=ApiResponse)
def capabilities() -> ApiResponse:
    """告诉前端后端能做什么。

    前端逻辑：
    - 浏览器支持 webkitSpeechRecognition → 用浏览器 ASR（最快、免费）
    - 否则后端 asr=true → 用后端
    - 都不支持 → 禁用语音输入，只留文字

    ## 返回值要"如实"

    这里的 `available` 是**真能跑**（含模型已就绪），不是"包装了"。
    实测教训：包装上但模型没下载，接口报"可用"→ 用户点了报错。
    宁可报不可用 + 给出补救路径，也不谎报。
    """
    asr_ok, asr_kind = _whisper_available()
    tts_ok, tts_kind = _tts_available()

    asr = {"available": asr_ok, "backend": asr_kind or None}
    if not asr_ok:
        if "缺模型" in asr_kind:
            asr["hint"] = (
                "faster-whisper 已安装但缺少模型文件。"
                "首次使用需联网下载（约 140MB）："
                "python -c \"from faster_whisper import WhisperModel; WhisperModel('base')\""
            )
        else:
            asr["hint"] = (
                "浏览器原生语音识别（Chrome/Edge）不需要这个；"
                "如需服务端识别：pip install faster-whisper"
            )

    return ApiResponse(data={
        "asr": asr,
        "tts": {"available": tts_ok, "backend": tts_kind or None},
        "note": "浏览器原生 ASR/TTS 优先（零延迟、免费、内容不外传）；后端仅在浏览器不支持时使用",
    })


@router.post("/transcribe", response_model=ApiResponse)
async def transcribe(
    file: UploadFile = File(...),
    language: str = Query(default="zh"),
    user: dict[str, str] = Depends(current_user),
) -> ApiResponse:
    """音频转文字。

    没装 whisper 时返回 501 + 明确的安装说明，而不是"功能不可用"这种废话。
    """
    ok, kind = _whisper_available()
    if not ok:
        raise errors.AppError(
            501, 43008,
            "服务端语音识别未启用。浏览器直接支持的（Chrome/Edge）不需要这个接口；"
            "如需服务端识别，请安装：pip install faster-whisper",
        )

    suffix = Path(file.filename or "audio.webm").suffix.lower()
    if suffix not in ALLOWED_AUDIO:
        raise errors.file_unsupported(suffix, ALLOWED_AUDIO)

    raw = await file.read()
    size_mb = len(raw) / 1024 / 1024
    if size_mb > MAX_AUDIO_MB:
        raise errors.file_too_large(size_mb, MAX_AUDIO_MB)

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(raw)
        tmp_path = tmp.name

    try:
        text = await _run_asr(tmp_path, language, kind)
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    return ApiResponse(data={"text": text, "language": language,
                             "bytes": len(raw), "backend": kind})


@router.post("/synthesize")
async def synthesize(
    payload: dict[str, Any],
    user: dict[str, str] = Depends(current_user),
) -> Response:
    """文字转语音，返回 mp3 字节流。

    没装 edge-tts 时返回 501 JSON（不是音频），前端据 status 判断。
    """
    text = str(payload.get("text") or "").strip()
    if not text:
        raise errors.AppError(422, 41001, "要合成的文字不能为空")
    if len(text) > 2000:
        text = text[:2000]

    ok, kind = _tts_available()
    if not ok:
        raise errors.AppError(
            501, 43009,
            "服务端语音合成未启用。浏览器原生的 speechSynthesis 可直接用；"
            "如需服务端合成，请安装：pip install edge-tts",
        )

    voice = str(payload.get("voice") or "zh-CN-XiaoxiaoNeural")

    try:
        audio = await _run_tts(text, voice, kind)
    except Exception as e:  # noqa: BLE001
        log.exception("TTS 失败")
        raise errors.AppError(502, 43009, f"语音合成失败：{type(e).__name__}") from e

    return Response(content=audio, media_type="audio/mpeg",
                    headers={"Content-Disposition": "inline; filename=tts.mp3"})


# ============================================================================
# 内部实现
# ============================================================================

async def _run_asr(path: str, language: str, kind: str) -> str:
    """跑语音识别。"""
    if kind == "faster-whisper":
        import asyncio

        def _sync() -> str:
            from faster_whisper import WhisperModel
            # base 模型约 140MB，CPU 上 1 秒音频约 0.3 秒；够用
            model = WhisperModel("base", device="cpu", compute_type="int8")
            segments, _ = model.transcribe(path, language=language, beam_size=1)
            return "".join(s.text for s in segments).strip()

        return await asyncio.to_thread(_sync)

    # CLI 方式
    import asyncio

    def _cli() -> str:
        out_dir = tempfile.mkdtemp()
        subprocess.run(
            ["whisper", path, "--language", language, "--model", "base",
             "--output_format", "txt", "--output_dir", out_dir],
            check=True, capture_output=True, timeout=300,
        )
        txt = Path(out_dir) / (Path(path).stem + ".txt")
        return txt.read_text(encoding="utf-8").strip() if txt.exists() else ""

    return await asyncio.to_thread(_cli)


async def _run_tts(text: str, voice: str, kind: str) -> bytes:
    """跑语音合成，返回 mp3 字节。"""
    if kind == "edge-tts":
        import asyncio
        import io

        def _sync() -> bytes:
            import edge_tts
            buf = io.BytesIO()

            async def _gen():
                comm = edge_tts.Communicate(text, voice)
                async for chunk in comm.stream():
                    if chunk["type"] == "audio":
                        buf.write(chunk["data"])

            asyncio.run(_gen())
            return buf.getvalue()

        return await asyncio.to_thread(_sync)

    # 系统 TTS 只到 wav，这里返回 wav 也行（前端 audio 标签能放）
    import asyncio
    import tempfile

    def _sys() -> bytes:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            out = tmp.name
        try:
            exe = shutil.which("espeak-ng") or shutil.which("espeak")
            subprocess.run([exe, "-v", "zh", "-w", out, text],
                           check=True, capture_output=True, timeout=60)
            return Path(out).read_bytes()
        finally:
            Path(out).unlink(missing_ok=True)

    return await asyncio.to_thread(_sys)
