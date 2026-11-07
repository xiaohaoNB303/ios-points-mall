#!/usr/bin/env bash
# 把当前访问地址写入运行摘要与固定 Issue
# 用法: bash report_url.sh <URL>
set -euo pipefail

URL="${1:?缺少 URL 参数}"

cat > url.md <<'EOF'
### 积分商城 · 当前访问地址

EOF
printf '%s\n' "$URL" >> url.md
cat >> url.md <<'EOF'

> **建议收藏固定入口（永不变）**：https://xiaohaonb303.github.io/ios-points-mall/
> 打开它会自动跳转到当前可用的商城地址，不用手动换链接。
>
> 接入地址会随运行环境变化，固定入口会自动读取最新地址。
EOF
printf '> 更新时间（UTC）：%s\n' "$(date -u '+%Y-%m-%d %H:%M')" >> url.md

# 写到本次运行摘要
cat url.md >> "$GITHUB_STEP_SUMMARY"

# 同步到固定 Issue（标题统一为「积分商城 · 访问地址」）
NUM=$(gh issue list --state open --search "积分商城 in:title" --json number -q '.[0].number' || true)
if [ -z "$NUM" ]; then
  gh issue create --title "积分商城 · 访问地址" --body-file url.md
else
  gh issue edit "$NUM" --title "积分商城 · 访问地址" --body-file url.md
fi

echo "已播报地址: $URL"
