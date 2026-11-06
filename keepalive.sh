#!/usr/bin/env bash
# 保活脚本（云端计时核心）
# 职责：
#   1) 守护 server.py 与 cloudflared —— 仅当进程崩溃时才重启，绝不主动关闭 Cloudflare；
#   2) 把 data.json 持久化到仓库 data 分支，并在启动时从 data 分支恢复 ——
#      保证「换链接 / 接力 / 超时重启」后账号、商品、兑换码、积分全部不丢；
#   3) 检测到代码更新时「热重载」server.py（不重启 Cloudflared = 不换公网地址）；
#   4) 临近 6 小时任务上限时「自接力」下一棒（带 relay_from 标记，实现零断线交接）。
set -u
PORT="${PORT:-8000}"
# 关键：以「脚本所在目录」为准。本地是 /workspace/...，GitHub runner 是
# /home/runner/work/...，写死路径会导致云端找不到 data.json → 持久化/热更新全空转。
# 仍允许用环境变量 CODE_DIR 覆盖。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODE_DIR="${CODE_DIR:-$SCRIPT_DIR}"
REPO_URL="${REPO_URL:?缺少 REPO_URL（应带令牌的仓库地址）}"
DATA_BRANCH="data"
SYNC_DIR="/tmp/mall-datastore"
RUN_MAX_MIN="${RUN_MAX_MIN:-350}"
HANDOFF_MIN="${HANDOFF_MIN:-3}"
START=$(date +%s)

# ---------- 从 data 分支恢复历史数据（换链接不丢数据的核心） ----------
restore_data() {
  mkdir -p "$SYNC_DIR"
  if [ ! -d "$SYNC_DIR/.git" ]; then
    git clone --quiet --depth 1 --single-branch --branch "$DATA_BRANCH" "$REPO_URL" "$SYNC_DIR" 2>/dev/null \
      || { git init -q "$SYNC_DIR" && git -C "$SYNC_DIR" remote add origin "$REPO_URL"; }
  fi
  if [ -d "$SYNC_DIR/.git" ] && git -C "$SYNC_DIR" cat-file -e "origin/$DATA_BRANCH:data.json" 2>/dev/null; then
    git -C "$SYNC_DIR" show "origin/$DATA_BRANCH:data.json" > "$CODE_DIR/data.json"
    echo "$(date -u) 已从 $DATA_BRANCH 分支恢复历史数据（$(wc -c < "$CODE_DIR/data.json") 字节）"
    return 0
  fi
  echo "$(date -u) 未找到历史 data.json（首次运行或分支为空），沿用本地/默认数据"
}

# ---------- 持久化：把本地 data.json 推回 data 分支 ----------
sync_data() {
  [ -f "$CODE_DIR/data.json" ] || return 0
  mkdir -p "$SYNC_DIR"
  if [ ! -d "$SYNC_DIR/.git" ]; then
    git clone --quiet --depth 1 --single-branch --branch "$DATA_BRANCH" "$REPO_URL" "$SYNC_DIR" 2>/dev/null \
      || { git init -q "$SYNC_DIR" && git -C "$SYNC_DIR" remote add origin "$REPO_URL"; }
  fi
  [ -d "$SYNC_DIR/.git" ] || return 0
  cp "$CODE_DIR/data.json" "$SYNC_DIR/data.json"
  ( cd "$SYNC_DIR"
    # 先拉取远端最新，降低并发推送导致非快进的概率
    git fetch -q origin "$DATA_BRANCH" 2>/dev/null || true
    git add -f data.json
    if git diff --cached --quiet; then return 0; fi
    git -c user.email="bot@mall.local" -c user.name="mall-bot" \
      commit -q -m "sync $(date -u '+%Y-%m-%dT%H:%M:%SZ')" 2>/dev/null || return 0
    # 推送失败（多为并发非快进）则拉取后重试，最多 3 次
    for i in 1 2 3; do
      if git push -q origin "$DATA_BRANCH" 2>/dev/null; then return 0; fi
      git fetch -q origin "$DATA_BRANCH" 2>/dev/null || true
      git pull -q --ff-only origin "$DATA_BRANCH" 2>/dev/null || true
      cp "$CODE_DIR/data.json" data.json
      git add -f data.json
      git -c user.email="bot@mall.local" -c user.name="mall-bot" \
        commit -q -m "sync $(date -u '+%Y-%m-%dT%H:%M:%SZ')" 2>/dev/null || true
    done
  )
}

# ---------- 热更新：代码变了只重启 server.py，不动 Cloudflared ----------
update_code() {
  ( cd "$CODE_DIR"
    git fetch -q origin master >/dev/null 2>&1 || git fetch -q origin >/dev/null 2>&1 || return 0
    LOCAL=$(git hash-object server.py 2>/dev/null || echo none)
    REMOTE=$(git rev-parse origin/master:server.py 2>/dev/null || echo none)
    if [ "$LOCAL" != "$REMOTE" ] && [ "$REMOTE" != "none" ]; then
      echo "$(date -u) 检测到代码更新，热重载 server.py（保持 Cloudflare 地址不变）…"
      git checkout -q origin/master -- server.py public 2>/dev/null || true
      pkill -f "python3 server.py" 2>/dev/null || true
      sleep 1
      ( cd "$CODE_DIR" && nohup python3 server.py > server.log 2>&1 & )
    fi
  )
}

# 退出前务必落盘一次（避免任务被回收时丢最后一段数据）
trap sync_data EXIT

echo "保活脚本启动：PORT=$PORT CODE_DIR=$CODE_DIR"

# 启动即恢复历史数据，确保「换链接」后账号/积分/商品/兑换码不丢
restore_data

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

  # 4) 临近超时：自接力下一棒（带 relay_from 标记，新棒启动期间本棒仍在线，间隙极小）
  ELAPSED=$(( ($(date +%s) - START) / 60 ))
  if [ "$ELAPSED" -gt $((RUN_MAX_MIN - HANDOFF_MIN)) ]; then
    echo "$(date -u) 临近超时，自接力下一棒（relay_from=$GITHUB_RUN_ID）…"
    gh workflow run tunnel.yml -f relay_from="$GITHUB_RUN_ID" 2>/dev/null || true
    sleep 150
    exit 0
  fi

  sleep 30
done
