#!/usr/bin/env bash
# 保活脚本（云端计时核心）
# 职责：
#   1) 守护 server.py 与 cloudflared —— 仅当进程崩溃时才重启，绝不主动关闭 Cloudflare；
#   2) 定期把 data.json 同步回仓库 data 分支，实现账号/商品/兑换码跨重启持久化；
#   3) 检测到代码更新时「热重载」server.py（不重启 Cloudflared = 不换公网地址）；
#   4) 临近 6 小时任务上限时自接力下一棒，最大限度缩短断线间隙。
set -u
PORT="${PORT:-8000}"
CODE_DIR="${CODE_DIR:-/workspace/ios-points-mall}"
REPO_URL="${REPO_URL:?缺少 REPO_URL（应带令牌的仓库地址）}"
DATA_BRANCH="data"
SYNC_DIR="/tmp/mall-datastore"
RUN_MAX_MIN="${RUN_MAX_MIN:-350}"
HANDOFF_MIN="${HANDOFF_MIN:-3}"
START=$(date +%s)

# ---------- 准备 data 分支镜像（用于持久化） ----------
mkdir -p "$SYNC_DIR"
if [ ! -d "$SYNC_DIR/.git" ]; then
  if git ls-remote --exit-code "$REPO_URL" "refs/heads/$DATA_BRANCH" >/dev/null 2>&1; then
    git clone --quiet --depth 1 --single-branch --branch "$DATA_BRANCH" "$REPO_URL" "$SYNC_DIR" 2>/dev/null \
      || rm -rf "$SYNC_DIR"
  fi
  if [ ! -d "$SYNC_DIR/.git" ]; then
    git init -q "$SYNC_DIR"
    git -C "$SYNC_DIR" remote add origin "$REPO_URL"
    git -C "$SYNC_DIR" checkout -q -b "$DATA_BRANCH" 2>/dev/null || true
  fi
fi

# ---------- 持久化：把本地 data.json 推回 data 分支 ----------
sync_data() {
  [ -f "$CODE_DIR/data.json" ] || return 0
  cp "$CODE_DIR/data.json" "$SYNC_DIR/data.json"
  ( cd "$SYNC_DIR"
    git add -A
    if git diff --cached --quiet; then return 0; fi
    git -c user.email="bot@mall.local" -c user.name="mall-bot" \
      commit -q -m "sync $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    git push -q origin "$DATA_BRANCH" 2>&1 | tail -2 || true
  )
}

# ---------- 热更新：代码变了只重启 server.py，不动 Cloudflared ----------
update_code() {
  ( cd "$CODE_DIR"
    git fetch -q origin master 2>/dev/null || return 0
    LOCAL=$(git hash-object server.py 2>/dev/null || echo none)
    REMOTE=$(git rev-parse origin/master:server.py 2>/dev/null || echo none)
    if [ "$LOCAL" != "$REMOTE" ]; then
      echo "$(date -u) 检测到代码更新，热重载 server.py（保持 Cloudflare 地址不变）…"
      git checkout -q origin/master -- server.py public 2>/dev/null || true
      pkill -f "python3 server.py" 2>/dev/null || true
      sleep 1
      ( cd "$CODE_DIR" && nohup python3 server.py > server.log 2>&1 & )
    fi
  )
}

# 退出前务必落盘一次
trap sync_data EXIT

echo "保活脚本启动：PORT=$PORT CODE_DIR=$CODE_DIR"

while true; do
  # 1) 守护 server.py
  if ! pgrep -f "python3 server.py" >/dev/null; then
    echo "$(date -u) server.py 丢失，重启中…"
    ( cd "$CODE_DIR" && nohup python3 server.py > server.log 2>&1 & )
  fi

  # 2) 守护 cloudflared（仅崩溃时重启，绝不主动关闭）
  if ! pgrep -f "cloudflared tunnel" >/dev/null; then
    echo "$(date -u) cloudflared 丢失，重启中（保持同一隧道）…"
    ( cd "$CODE_DIR" && nohup ./cloudflared tunnel --url "http://localhost:$PORT" \
        --logfile cf.log --loglevel info >/dev/null 2>&1 & )
    sleep 6
    NEW=$(grep -oE 'https://[a-z0-9.-]+\.trycloudflare\.com' "$CODE_DIR/cf.log" | tail -1)
    [ -n "$NEW" ] && bash "$CODE_DIR/report_url.sh" "$NEW"
  fi

  # 3) 持久化 + 热更新
  sync_data
  update_code

  # 4) 临近超时：自接力下一棒（新 run 启动期间本棒仍在线，间隙极小）
  ELAPSED=$(( ($(date +%s) - START) / 60 ))
  if [ "$ELAPSED" -gt $((RUN_MAX_MIN - HANDOFF_MIN)) ]; then
    echo "$(date -u) 临近超时，接力下一棒…"
    gh workflow run tunnel.yml || true
    sleep 150
    exit 0
  fi

  sleep 30
done
