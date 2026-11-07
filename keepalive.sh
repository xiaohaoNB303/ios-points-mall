#!/usr/bin/env bash
# 保活脚本（云端计时核心）
# 职责：
#   1) 守护 server.py 与 cloudflared —— 仅当进程崩溃时才重启，绝不主动关闭 Cloudflare；
#   2) 把 data.json 持久化到仓库 data 分支，并在启动时从 data 分支恢复；
#   3) 【数据不丢的关键】用 data.json 里的单调版本号 _rev 比较「谁更新」：
#        - 本地比远端新  -> 才推送；
#        - 本地比远端旧  -> 把远端数据拉回本地（绝不拿旧快照覆盖新数据）；
#        - server.py 每次写盘都会落 .dirty 标记，本脚本检测到就立即同步；
#   4) 检测到代码更新时「热重载」server.py（不重启 Cloudflared = 不换公网地址）；
#   5) 临近 6 小时任务上限时「自接力」下一棒（零断线交接）。
set -u
PORT="${PORT:-8000}"
# 以「脚本所在目录」为准：本地 /workspace/...，GitHub runner /home/runner/work/...
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODE_DIR="${CODE_DIR:-$SCRIPT_DIR}"
REPO_URL="${REPO_URL:?缺少 REPO_URL（应带令牌的仓库地址）}"
DATA_BRANCH="data"
SYNC_DIR="/tmp/mall-datastore"
RUN_MAX_MIN="${RUN_MAX_MIN:-350}"
HANDOFF_MIN="${HANDOFF_MIN:-3}"
START=$(date +%s)

DATA_FILE="$CODE_DIR/data.json"
DIRTY_FILE="$CODE_DIR/data.json.dirty"
REMOTE_SNAP="$SYNC_DIR/.remote_data.json"

# 读取 data.json 里的 _rev（缺失/损坏视为 0）
rev_of() {
  python3 - "$1" <<'PY' 2>/dev/null || echo 0
import json, sys
try:
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        print(int(json.load(f).get("_rev", 0) or 0))
except Exception:
    print(0)
PY
}

# 确保 SYNC_DIR 是一个可用的 git 工作副本（指向 data 分支）
ensure_sync_repo() {
  mkdir -p "$SYNC_DIR"
  if [ ! -d "$SYNC_DIR/.git" ]; then
    rm -rf "$SYNC_DIR"; mkdir -p "$SYNC_DIR"
    git clone --quiet --depth 1 --single-branch --branch "$DATA_BRANCH" "$REPO_URL" "$SYNC_DIR" 2>/dev/null \
      || { git init -q "$SYNC_DIR" && git -C "$SYNC_DIR" remote add origin "$REPO_URL"; }
  fi
}

# 取远端 data.json 快照与其 rev；返回 1 表示远端没有可用的 data.json
fetch_remote_snapshot() {
  git -C "$SYNC_DIR" fetch -q origin "$DATA_BRANCH" 2>/dev/null || true
  if git -C "$SYNC_DIR" cat-file -e "origin/$DATA_BRANCH:data.json" 2>/dev/null; then
    git -C "$SYNC_DIR" show "origin/$DATA_BRANCH:data.json" > "$REMOTE_SNAP" 2>/dev/null || return 1
    return 0
  fi
  return 1
}

# ---------- 启动恢复：只在远端确实更新时才覆盖本地，绝不拿旧快照盖新数据 ----------
restore_data() {
  [ -f "$DATA_FILE" ] || { :; }
  ensure_sync_repo
  if fetch_remote_snapshot; then
    RREV=$(rev_of "$REMOTE_SNAP")
    LREV=$(rev_of "$DATA_FILE")
    if [ "$RREV" -gt "$LREV" ]; then
      cp "$REMOTE_SNAP" "$DATA_FILE"
      rm -f "$DIRTY_FILE"
      echo "$(date -u) 已从 $DATA_BRANCH 分支恢复较新数据（rev $RREV > $LREV，$(wc -c < "$DATA_FILE") 字节）"
    else
      echo "$(date -u) 本地数据不比远端旧（rev $LREV >= $RREV），保留本地，避免覆盖新写入"
    fi
  else
    echo "$(date -u) 远端暂无 data.json，沿用本地/默认数据"
  fi
}

# ---------- 持久化：按 _rev 决定推 / 拉，绝不拿旧快照覆盖新数据 ----------
sync_data() {
  [ -f "$DATA_FILE" ] || return 0
  ensure_sync_repo
  local LREV RREV=0
  LREV=$(rev_of "$DATA_FILE")
  if fetch_remote_snapshot; then RREV=$(rev_of "$REMOTE_SNAP"); fi

  if [ "$LREV" -lt "$RREV" ]; then
    # 远端更新（例如另一实例刚写过，或换链接后本实例还拿着旧文件）：拉回来
    cp "$REMOTE_SNAP" "$DATA_FILE"
    rm -f "$DIRTY_FILE"
    echo "$(date -u) 远端数据较新（rev $RREV > $LREV），已同步到本地"
    return 0
  fi
  if [ "$LREV" -eq "$RREV" ]; then
    # 没有新写入，也清掉脏标记（可能已被别的实例同步过）
    rm -f "$DIRTY_FILE"
    return 0
  fi

  # 本地更新：基于远端最新提交，写入并推送（失败重试时会重新比较 rev）
  ( cd "$SYNC_DIR"
    git fetch -q origin "$DATA_BRANCH" 2>/dev/null || true
    git reset -q --hard "origin/$DATA_BRANCH" 2>/dev/null || true
    cp "$DATA_FILE" data.json
    git add -f data.json
    if git diff --cached --quiet; then rm -f "$DIRTY_FILE"; return 0; fi
    git -c user.email="bot@mall.local" -c user.name="mall-bot" \
      commit -q -m "sync $(date -u '+%Y-%m-%dT%H:%M:%SZ') rev=$LREV" 2>/dev/null || return 0
    for i in 1 2 3; do
      if git push -q origin "HEAD:$DATA_BRANCH" 2>/dev/null; then
        rm -f "$DIRTY_FILE"
        echo "$(date -u) 已同步 rev=$LREV 到 $DATA_BRANCH 分支"
        return 0
      fi
      # 推送失败（多为并发非快进）：重新取远端，若远端已不比本地旧则放弃本次，避免覆盖
      git fetch -q origin "$DATA_BRANCH" 2>/dev/null || true
      RR=0
      if git cat-file -e "origin/$DATA_BRANCH:data.json" 2>/dev/null; then
        git show "origin/$DATA_BRANCH:data.json" > "$REMOTE_SNAP" 2>/dev/null || true
        RR=$(rev_of "$REMOTE_SNAP")
      fi
      if [ "$RR" -ge "$LREV" ]; then
        echo "$(date -u) 远端已有更新（rev $RR >= $LREV），放弃本次推送，避免覆盖"
        return 0
      fi
      git reset -q --hard "origin/$DATA_BRANCH" 2>/dev/null || true
      cp "$DATA_FILE" data.json
      git add -f data.json
      git -c user.email="bot@mall.local" -c user.name="mall-bot" \
        commit -q -m "sync $(date -u '+%Y-%m-%dT%H:%M:%SZ') rev=$LREV retry$i" 2>/dev/null || true
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
      git checkout -q origin/master -- server.py public keepalive.sh 2>/dev/null || true
      pkill -f "python3 server.py" 2>/dev/null || true
      sleep 1
      ( cd "$CODE_DIR" && nohup python3 server.py > server.log 2>&1 & )
    fi
  )
}

# 退出前务必落盘一次（避免任务被回收时丢最后一段数据）
trap sync_data EXIT

echo "保活脚本启动：PORT=$PORT CODE_DIR=$CODE_DIR"

# 启动即恢复历史数据（换链接后账号/积分/公告/活动/红包全部不丢）
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

  # 4) 临近超时：自接力下一棒
  ELAPSED=$(( ($(date +%s) - START) / 60 ))
  if [ "$ELAPSED" -gt $((RUN_MAX_MIN - HANDOFF_MIN)) ]; then
    echo "$(date -u) 临近超时，自接力下一棒（relay_from=$GITHUB_RUN_ID）…"
    gh workflow run tunnel.yml -f relay_from="$GITHUB_RUN_ID" 2>/dev/null || true
    sleep 150
    exit 0
  fi

  # 有新写入（.dirty）则 3 秒内立即同步，否则 10 秒轮询一次
  if [ -f "$DIRTY_FILE" ]; then sleep 3; else sleep 10; fi
done
