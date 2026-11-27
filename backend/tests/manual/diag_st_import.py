import traceback

print("=== 尝试导入 sentence_transformers ===")
try:
    import sentence_transformers
    print("  ✓ 成功")
except Exception:
    print("  ✗ 失败，完整 traceback：")
    print()
    tb = traceback.format_exc()
    # 只打印最后 12 行（关键错误信息）
    lines = tb.split("\n")
    for line in lines[-14:]:
        if line.strip():
            print("  " + line)

print()
print("=== 相关包版本 ===")
for mod in ("torch", "transformers", "tokenizers", "huggingface_hub",
            "numpy", "PIL"):
    try:
        m = __import__(mod)
        print(f"  {mod:<20} {getattr(m, '__version__', '?')}")
    except ImportError:
        print(f"  {mod:<20} (未安装)")
