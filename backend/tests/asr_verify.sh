#!/usr/bin/env bash
set -u
PROJ=/mnt/e/桌面/实习项目/interview_agent
PY="$PROJ/.venv/bin/python"
cd "$PROJ" || exit 1

echo "=== 安装 faster-whisper（含 ctranslate2，约 200MB）==="
timeout 900 "$PY" -m pip install --quiet faster-whisper 2>&1 | tail -5

echo ""
echo "=== 验证导入 ==="
"$PY" - <<'EOF'
try:
    import faster_whisper
    print("  faster-whisper ok", getattr(faster_whisper, "__version__", ""))
except ImportError as e:
    print("  安装失败:", e)
EOF

echo ""
echo "=== 实测 ASR：把刚才 TTS 生成的音频转回文字 ==="
"$PY" - <<'EOF'
import sys
try:
    from faster_whisper import WhisperModel
except ImportError:
    print("  跳过（未安装）")
    raise SystemExit(0)

import os
if not os.path.exists("/tmp/tts_test.mp3"):
    print("  跳过（没有测试音频）")
    raise SystemExit(0)

print("  加载 base 模型（首次会下载约 140MB）…")
model = WhisperModel("base", device="cpu", compute_type="int8")
segments, info = model.transcribe("/tmp/tts_test.mp3", language="zh", beam_size=1)
text = "".join(s.text for s in segments).strip()
print(f"  识别结果: {text!r}")
print(f"  语言: {info.language}  时长: {info.duration:.1f}s")
if text:
    print("  ✓ ASR 真实可用（TTS→ASR 闭环成功）")
else:
    print("  ✗ 识别为空")
EOF
