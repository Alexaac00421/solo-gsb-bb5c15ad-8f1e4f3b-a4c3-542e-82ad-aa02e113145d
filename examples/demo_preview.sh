#!/usr/bin/env bash
# 定时公开前内审预览（一次性凭证）端到端示例：
#   服务须以 PREVIEW_ADMIN_KEY 配置启动（本脚本默认 dev-admin-key）。
#   上传 + 标注 -> 定时发布（约 30 分钟后公开，到点前公开面 404）
#   -> 管理员颁发凭证：无/错密钥、未配置、立即公开版本、已撤回版本均被拒
#   -> 持凭证领取预览包：只含成品 PNG + 冻结遮盖 + 清单，绝无原图
#   -> 二次领取 -> 403 invalid_preview_token（原子一次性）
#   -> 撤回后再颁发/领取 -> 410 publication_withdrawn
#   -> 伪造/过期凭证 -> 403，错误体不含文档与版本信息
#   -> 服务数据归档不含任何凭证内容
# 依赖：curl、python3（仅用于解析 JSON、生成示例图与 RFC 3339 时间戳、解 ZIP）
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
ADMIN_KEY="${ADMIN_KEY:-dev-admin-key}"

iso_in() {  # $1 = 距当前的秒数 -> RFC 3339（UTC，尾部 Z）
  python3 - "$1" <<'EOF'
import sys
from datetime import datetime, timedelta, timezone
print((datetime.now(timezone.utc) + timedelta(seconds=int(sys.argv[1])))
      .replace(microsecond=0).isoformat().replace("+00:00", "Z"))
EOF
}

code() {  # 仅输出 HTTP 状态码
  curl -sS -o /dev/null -w "%{http_code}" "$@"
}

status_of_issue() {  # $1=doc_id [$2=ttl] [$3=key]；仅输出颁发请求的状态码末行
  issue "$@" | tail -n1
}

python3 - <<'EOF'
from PIL import Image
Image.new("RGB", (64, 48), (30, 90, 160)).save("/tmp/preview_a.png")
Image.new("RGB", (64, 48), (160, 90, 30)).save("/tmp/preview_b.png")
EOF

upload() {
  curl -sS -F "file=@$1" "$BASE/documents" \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['doc_id'])"
}

issue() {  # $1=doc_id [$2=ttl] [$3=key] ；空字符串 key 表示不带头。输出含状态码末行
  local doc="$1" ttl="${2:-15}" key="${3-$ADMIN_KEY}"
  local args=(-sS -w $'\n%{http_code}' -X POST "$BASE/preview-tokens"
    -H 'Content-Type: application/json')
  if [ -n "$key" ]; then args+=(-H "X-Admin-Key: $key"); fi
  args+=(-d "{\"doc_id\":\"$doc\",\"version\":1,\"ttl_minutes\":$ttl}")
  curl "${args[@]}"
}

echo "== 1. 上传两份原图并标注 =="
A=$(upload /tmp/preview_a.png)
B=$(upload /tmp/preview_b.png)
for d in "$A" "$B"; do
  curl -sS -X PUT "$BASE/documents/$d/annotations" -H 'Content-Type: application/json' \
    -d '{"revision":0,"rects":[{"x":4,"y":4,"w":12,"h":10,"reason":"身份证号"}]}' >/dev/null
done
echo "A=$A B=$B"

echo "== 2. A v1 定时发布（1800 秒后公开）；B v1 立即公开 =="
T_A=$(iso_in 1800)
curl -sS -X POST "$BASE/documents/$A/publish" -H 'Content-Type: application/json' \
  -d "{\"publish_at\":\"$T_A\"}" | python3 -m json.tool
curl -sS -X POST "$BASE/documents/$B/publish" >/dev/null
echo "  到点前公开面：A v1 详情 HTTP $(code "$BASE/publications/$A/1")（仍 404，预览不改变此语义）"

echo "== 3. 颁发凭证：密钥校验 =="
echo "  缺少 X-Admin-Key -> HTTP $(status_of_issue "$A" 15 "")（401 invalid_admin_key）"
echo "  错误密钥        -> HTTP $(status_of_issue "$A" 15 wrong-key)（401 invalid_admin_key）"
echo "  立即公开的 B v1 -> HTTP $(status_of_issue "$B" 15)（409 preview_requires_future_publication）"
issue "$A" 15 | head -n -1 | python3 -m json.tool
RESP=$(issue "$A" 15)
TOKEN=$(echo "$RESP" | head -n -1 \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['preview_token'])")
echo "  成功颁发（201），TOKEN=${TOKEN:0:16}…"

echo "== 4. 领取预览包：只含成品 PNG + 冻结遮盖 + 清单，绝无原图 =="
rm -f /tmp/preview-A.zip
curl -sS -o /tmp/preview-A.zip -w "  HTTP %{http_code} Content-Type: %{content_type}\n" \
  "$BASE/preview-packages/$TOKEN"
python3 - /tmp/preview-A.zip "$A" <<'EOF'
import io, json, sys, zipfile
zf = zipfile.ZipFile(sys.argv[1])
doc = sys.argv[2]
names = sorted(zf.namelist())
print("  成员:", names)
assert names == sorted(["manifest.json", "redactions.json", f"{doc}_v1.png"])
assert not any("original" in n for n in names)
man = json.loads(zf.read("manifest.json"))
red = json.loads(zf.read("redactions.json"))
print("  manifest:", {k: man[k] for k in ("kind", "version", "publish_at")})
print("  冻结遮盖:", red["redactions"])
blob = b"".join(zf.read(n) for n in names)
assert b"original_sha256" not in blob
print("  包内无 original_sha256 字段，PNG 头:", zf.read(f"{doc}_v1.png")[:8] == b"\x89PNG\r\n\x1a\n")
EOF

echo "== 5. 原子一次性：二次领取同一凭证 -> 403 invalid_preview_token =="
echo "  HTTP $(code "$BASE/preview-packages/$TOKEN")"

echo "== 6. 伪造/无效凭证统一 403，错误体不泄露版本信息 =="
curl -sS -w "\n  HTTP %{http_code}\n" "$BASE/preview-packages/pvp_not-a-real-token"

echo "== 7. 撤回优先：撤回 A v1 后无法再颁发 =="
curl -sS -o /dev/null -X POST "$BASE/documents/$A/publications/1/withdraw" \
  -H 'Content-Type: application/json' -d '{"reason":"定时版本发现遮盖遗漏"}'
echo "  再颁发 -> HTTP $(status_of_issue "$A" 15)（410 publication_withdrawn）"
# 撤回前已颁发但未使用的凭证，在撤回提交后领取同样为 410（不核销）：用 C 演示
C=$(upload /tmp/preview_b.png)
curl -sS -X PUT "$BASE/documents/$C/annotations" -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[]}' >/dev/null
T_C=$(iso_in 1800)
curl -sS -o /dev/null -X POST "$BASE/documents/$C/publish" \
  -H 'Content-Type: application/json' -d "{\"publish_at\":\"$T_C\"}"
CTOKEN=$(issue "$C" 15 | head -n -1 \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['preview_token'])")
curl -sS -o /dev/null -X POST "$BASE/documents/$C/publications/1/withdraw" \
  -H 'Content-Type: application/json' -d '{"reason":"撤回先于领取"}'
echo "  撤回先提交，凭证领取 -> HTTP $(code "$BASE/preview-packages/$CTOKEN")（410，不核销）"

echo "== 8. 到期凭证 403（TTL 1 分钟；演示用错误格式凭证同样 403） =="
echo "  真正的到期等待请见测试 tests/test_preview.py（时钟注入，无需真实等待）"

echo "== 9. 服务数据归档不含任何凭证内容 =="
curl -sS -X POST -o /tmp/preview-archive.zip "$BASE/service-data-archives"
python3 - <<EOF
import hashlib, zipfile
zf = zipfile.ZipFile("/tmp/preview-archive.zip")
joined = b"".join(zf.read(n) for n in zf.namelist())
for tok in ("$TOKEN", "$CTOKEN"):
    assert tok.encode() not in joined, "明文凭证进入归档！"
    assert hashlib.sha256(tok.encode()).hexdigest().encode() not in joined, "凭证摘要进入归档！"
assert not any("preview" in n for n in zf.namelist())
print("  归档成员:", zf.namelist())
print("  OK：归档无凭证明文、无凭证摘要、无 preview 成员")
EOF

echo
echo "全部演示完成。"
