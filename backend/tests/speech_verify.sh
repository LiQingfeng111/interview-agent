#!/usr/bin/env bash
set -u
PROJ=/mnt/e/桌面/实习项目/interview_agent
PY="$PROJ/.venv/bin/python"
cd "$PROJ" || exit 1

echo "=== 检查语音包是否已装 ==="
"$PY" - <<'EOF'
for mod, label in (("edge_tts", "edge-tts"), ("faster_whisper", "faster-whisper")):
    try:
        __import__(mod)
        print(f"  {label}: 已安装")
    except ImportError:
        print(f"  {label}: 未安装")
EOF

echo ""
echo "=== 安装 edge-tts ==="
"$PY" -m pip install --quiet edge-tts 2>&1 | tail -3
"$PY" - <<'EOF'
try:
    import edge_tts
    print("  edge-tts 就绪", getattr(edge_tts, "__version__", ""))
except ImportError as e:
    print("  edge-tts 安装失败:", e)
EOF

echo ""
echo "=== 实测 TTS：合成一句中文 ==="
"$PY" - <<'EOF'
import asyncio
import io

try:
    import edge_tts
except ImportError:
    print("  跳过（未安装）")
    raise SystemExit(0)


async def main():
    buf = io.BytesIO()
    comm = edge_tts.Communicate("你好，这是一次语音合成测试。", "zh-CN-XiaoxiaoNeural")
    chunks = 0
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            buf.write(chunk["data"])
            chunks += 1
    data = buf.getvalue()
    print(f"  生成 {len(data)} 字节 mp3（{chunks} 个音频块）")
    if len(data) > 1000:
        print("  ✓ TTS 真实可用")
        # 存一份供人工试听
        with open("/tmp/tts_test.mp3", "wb") as f:
            f.write(data)
        print("  已保存 /tmp/tts_test.mp3")
    else:
        print("  ✗ 生成的音频过小，可能失败")


asyncio.run(main())
EOF
