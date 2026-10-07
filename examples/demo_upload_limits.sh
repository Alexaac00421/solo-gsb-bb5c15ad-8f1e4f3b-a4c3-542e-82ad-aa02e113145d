#!/usr/bin/env bash
# 上传大小上限（分块有界读取）端到端示例：
#   正常上传 -> 201；恰好达到上限 -> 201；超过上限 -> 413 payload_too_large 且不留痕
# 服务需以较小上限启动，例如：
#   MAX_UPLOAD_MB=1 uvicorn app.main:app --host 0.0.0.0 --port 8000
# 依赖：curl、python3（仅用于生成示例文件）
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
LIMIT_BYTES=$((1 * 1024 * 1024))  # 与 MAX_UPLOAD_MB=1 对应

python3 - <<'EOF'
from PIL import Image
Image.new("RGB", (64, 48), (30, 90, 160)).save("/tmp/upload_ok.png")
EOF

echo "== 1. 正常上传（远小于上限）-> 201，返回 doc_id 与旋正后宽高 =="
curl -sS -F "file=@/tmp/upload_ok.png" "$BASE/documents"
echo

echo "== 2. 恰好达到上限（1 MiB）-> 仍 201 =="
python3 - <<'EOF'
data = open("/tmp/upload_ok.png", "rb").read()
limit = 1024 * 1024
assert len(data) < limit
# PNG 解码器忽略 IEND 之后的填充字节；填充后恰好等于上限
open("/tmp/upload_exact.png", "wb").write(data + b"\0" * (limit - len(data)))
EOF
curl -sS -o /dev/null -w "exact-limit upload: HTTP %{http_code}\n" \
  -F "file=@/tmp/upload_exact.png" "$BASE/documents"

echo "== 3. 超过上限 1 字节 -> 413 payload_too_large =="
python3 - <<'EOF'
open("/tmp/upload_over.bin", "wb").write(b"0" * (1024 * 1024 + 1))
EOF
curl -sS -w "\nHTTP %{http_code}\n" -F "file=@/tmp/upload_over.bin" "$BASE/documents"

echo "== 4. 远超上限（8 MiB）-> 413；服务端分块读取、首次超限即拒绝，不占满内存 =="
python3 - <<'EOF'
open("/tmp/upload_huge.bin", "wb").write(b"0" * (8 * 1024 * 1024))
EOF
curl -sS -w "\nHTTP %{http_code}\n" -F "file=@/tmp/upload_huge.bin" "$BASE/documents"

echo "== 5. 空文件 -> 400 empty_upload（既有行为不变）=="
: > /tmp/upload_empty.bin
curl -sS -w "\nHTTP %{http_code}\n" -F "file=@/tmp/upload_empty.bin" "$BASE/documents"
