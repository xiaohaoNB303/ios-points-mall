#!/usr/bin/env bash
# 把最新公网地址写到 GitHub Step Summary 与固定的 Issue
# 用法: bash report_url.sh <URL>
set -euo pipefail

URL="${1:?缺少 URL 参数}"

cat > url.md <<'EOF'
### 积分商城 · 当前公网地址

EOF
printf '%s\n' "$URL" >> url.md
cat >> url.md <<'EOF'

> **建议收藏固定入口（永不变）**：https://xiaohaonb303.github.io/ios-points-mall/
> 打开它会自动跳转到当前可用的商城地址，不用手动换链接。
>
> 上面这条是 Cloudflare **免登录临时隧道**地址，每隔几小时重建时会变化。
> 本仓库通过 GitHub Actions 自接力（云端计时）保持商城长期在线。
EOF
printf '> 更新时间（UTC）：%s\n' "$(date -u '+%Y-%m-%d %H:%M')" >> url.md

# 写到本次运行摘要
cat url.md >> "$GITHUB_STEP_SUMMARY"

# 同步到固定的 Issue（标题统一为“积分商城”，不含 emoji）
NUM=$(gh issue list --state open --search "积分商城公网地址" --json number -q '.[0].number' || true)
if [ -z "$NUM" ]; then
  gh issue create --title "积分商城 · 公网地址（自动更新）" --body-file url.md
else
  gh issue edit "$NUM" --title "积分商城 · 公网地址（自动更新）" --body-file url.md
fi

echo "已播报地址: $URL"
