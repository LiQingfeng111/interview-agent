import json

d = json.load(open("/tmp/openapi.json"))
paths = d.get("paths", {})
print(f"端点数: {len(paths)}")
print()
for p in sorted(paths):
    methods = ",".join(m.upper() for m in paths[p] if m in ("get", "post", "put", "delete", "patch"))
    print(f"  {methods:10} {p}")
