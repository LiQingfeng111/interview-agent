#!/usr/bin/env bash
# 清理我这两天建的一次性验证脚本（保留运维必需的 + 有价值的产物）
set -u
PROJ=/mnt/e/桌面/实习项目/interview_agent
cd "$PROJ/tools" || exit 1

# 保留清单：运维脚本 + 有长期价值的工具 + 数据产物
KEEP="ai_review.py calibrate_embedding.py capture_api_snapshot.py capture_prompt_golden.py \
compare_retrieval2.py demo_snapshot.py embedding_calib.json embedding_calib_result.json \
enrich_bank.py eval_health.py expand_bank.py export_cassettes.py fake_openai_server.py \
gen_bank.py make_calibration.py prompt_inspect.py review_bank.py service.py \
show_backend_trace.py verify_deploy.py hybrid_retrieval_result.json retrieval_round2.json"

# 删除清单：纯一次性排查脚本
REMOVE="check_docker.sh check_env.sh check_infra_net.sh check_pg_ollama.sh check_pip.sh \
check_sudo_infra.sh check_versions.sh check_wsl_gpu.sh compare_retrieval.py debug_head.py \
debug_report_field.py demo_vector.sql demo_vector2.sql diff_prompt.py e2e_test.sh \
import_bank.sh init_schema.sh inspect_db.py inspect_pg_tables.sh install_backend.sh \
install_langchain.sh install_langchain_venv.sh probe_auth_flake.sh probe_ollama_from_wsl.sh \
pull_embedding.py run_all_both_backends.sh run_backend.sh run_baseline.sh \
run_capture_golden.sh run_full_and_live.sh run_inspect.sh run_lc_tests.sh run_live_lc.sh \
run_matrix.sh run_prompt_tests.sh setup_pgvector.sh start_pg.sh test_embedding.py \
test_pgvector.sh verify_auth_fix.sh"

echo "=== 将删除 $(echo $REMOVE | wc -w) 个一次性脚本 ==="
for f in $REMOVE; do
  if [ -f "$f" ]; then
    rm -f "$f" && echo "  rm $f"
  fi
done

# e2e_test.sh 被 ops.sh 依赖，单独移到 backend/tests 下
cat > "$PROJ/backend/tests/e2e_smoke.sh" <<'SMOKE'
#!/usr/bin/env bash
# 端到端冒烟：注册 → 登录 → 上传 → 向量化 → 检索
set -u
BASE=${BASE:-http://127.0.0.1:8080}
J='Content-Type: application/json'
U="smoke_$RANDOM"

echo "1) 注册 $U"
curl -s -X POST "$BASE/api/auth/register" -H "$J" \
  -d "{\"user\":\"$U\",\"password\":\"smoke123456\"}" | head -c 200; echo

echo "2) 登录"
TOKEN=$(curl -s -X POST "$BASE/api/auth/login" -H "$J" \
  -d "{\"user\":\"$U\",\"password\":\"smoke123456\"}" \
  | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
[ -z "$TOKEN" ] && { echo "  ✗ 登录失败"; exit 1; }
echo "  ✓ token ${#TOKEN} 字符"

echo "3) 未登录应 401"
CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/auth/me")
[ "$CODE" = "401" ] && echo "  ✓ 401" || echo "  ✗ 期望 401 实际 $CODE"

echo "4) 健康"
curl -s "$BASE/api/health" | head -c 400; echo

echo "5) 题库"
curl -s "$BASE/api/bank/stats" -H "Authorization: Bearer $TOKEN" | head -c 300; echo

echo "6) 语义检索题库"
curl -s -X POST "$BASE/api/bank/search" -H "$J" -H "Authorization: Bearer $TOKEN" \
  -d '{"q":"多头注意力每个头多少维","k":3}' | head -c 500; echo
SMOKE
chmod +x "$PROJ/backend/tests/e2e_smoke.sh"
echo "  → 端到端脚本已归位 backend/tests/e2e_smoke.sh"

echo ""
echo "=== 剩余文件数 ==="
ls -1 | wc -l
