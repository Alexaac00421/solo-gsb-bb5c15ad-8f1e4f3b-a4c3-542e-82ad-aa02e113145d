#!/usr/bin/env bash
# 端到端调用示例：正常 PNG/JPEG 上传 + 超限（413）/空文件（400）/非图像（400）/
# 非 PNG/JPEG（415）拒绝，并展示超限文件不会占满进程内存、不留临时文件或原图。
#
# 依赖：curl、python3（仅用于生成示例图与计算 SHA-256）
#
# 用法：
#   MAX_UPLOAD_MB=1 bash examples/demo_upload_limits.sh
#   BASE=http://localhost:8000 bash examples/demo_upload_limits.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
LIMIT_MB="${MAX_UPLOAD_MB:-1}"   # 与服务端 MAX_UPLOAD_MB 保持一致，默认示例取 1MB

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "服务端假设 MAX_UPLOAD_MB=${LIMIT_MB}（$((LIMIT_MB * 1024 * 1024)) 字节）；示例文件生成于 $WORK"

python3 - "$WORK" "$LIMIT_MB" <<'EOF'
import io
import os
import sys
from PIL import Image

work, limit_mb = sys.argv[1], int(sys.argv[2])

# 正常 PNG（80x60）
Image.new("RGB", (80, 60), (30, 90, 160)).save(os.path.join(work, "scan.png"), "PNG")

# 正常 JPEG：原始 10x20、EXIF Orientation=6（顺时针 90°），建档尺寸旋正为 20x10
img = Image.new("RGB", (10, 20), (0, 120, 0))
exif = img.getexif()
exif[274] = 6
img.save(os.path.join(work, "scan.jpg"), "JPEG", exif=exif)

# GIF：能解码但非 PNG/JPEG -> 415 unsupported_media_type
Image.new("RGB", (5, 5)).save(os.path.join(work, "pic.gif"), "GIF")

# 截断的 PNG -> 400 image_decode_failed
buf = io.BytesIO()
Image.new("RGB", (10, 10)).save(buf, "PNG")
with open(os.path.join(work, "truncated.png"), "wb") as f:
    f.write(buf.getvalue()[:20])

# 空文件 -> 400 empty_upload
open(os.path.join(work, "empty.png"), "wb").close()

# 超过上限 $((limit_mb + 1)) MiB -> 413 payload_too_large
with open(os.path.join(work, "too_big.bin"), "wb") as f:
    f.truncate((limit_mb + 1) * 1024 * 1024)

# 恰好等于上限的文件：不触发 413（继续进入解码；此处故意放非图像，
# 用来证明边界"等于可处理"——返回的是 400 image_decode_failed 而非 413）
with open(os.path.join(work, "exact_limit.bin"), "wb") as f:
    f.truncate(limit_mb * 1024 * 1024)
EOF

echo
echo "== 1. 正常 PNG 上传 -> 201（返回 doc_id、旋正后宽高、原始字节 SHA-256） =="
curl -sS -w "\n-> HTTP %{http_code}\n" \
  -F "file=@$WORK/scan.png;filename=scan.png" "$BASE/documents"
echo "本地 SHA-256 对照：$(sha256sum "$WORK/scan.png" | cut -d' ' -f1)"

echo
echo "== 2. EXIF 旋正 JPEG 上传 -> 201（10x20 + Orientation=6 建档为 width=20,height=10） =="
curl -sS -w "\n-> HTTP %{http_code}\n" \
  -F "file=@$WORK/scan.jpg;filename=scan.jpg" "$BASE/documents"

echo
echo "== 3. 超过 MAX_UPLOAD_MB 的文件 -> 413 payload_too_large =="
echo "（服务端分块流式读取，累计字节首次越限即停：不会把整份文件读入内存，"
echo "  临时文件随请求清理，不写文档、不留原图）"
curl -sS -w "\n-> HTTP %{http_code}\n" \
  -F "file=@$WORK/too_big.bin;filename=too_big.bin" "$BASE/documents"

echo
echo "== 4. 恰好等于上限 -> 不判超限（此样例不是图像，故随后 400 image_decode_failed） =="
curl -sS -w "\n-> HTTP %{http_code}\n" \
  -F "file=@$WORK/exact_limit.bin;filename=exact_limit.bin" "$BASE/documents"

echo
echo "== 5. 空文件 -> 400 empty_upload =="
curl -sS -w "\n-> HTTP %{http_code}\n" \
  -F "file=@$WORK/empty.png;filename=empty.png;type=image/png" "$BASE/documents"

echo
echo "== 6. 截断/损坏图像 -> 400 image_decode_failed =="
curl -sS -w "\n-> HTTP %{http_code}\n" \
  -F "file=@$WORK/truncated.png;filename=truncated.png" "$BASE/documents"

echo
echo "== 7. GIF -> 415 unsupported_media_type =="
curl -sS -w "\n-> HTTP %{http_code}\n" \
  -F "file=@$WORK/pic.gif;filename=pic.gif" "$BASE/documents"

echo
echo "可随后查询成功建档的文档元数据：curl -sS $BASE/documents/<doc_id>"
