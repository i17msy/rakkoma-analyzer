#!/bin/bash
# scrape_rakkoma.py 専用デーモンコンテナ起動スクリプト
# - python3 scrape_rakkoma.py を PID 1 相当で起動（--init で tini が管理）
# - --restart unless-stopped: PC再起動後も Docker Desktop 起動と同時に自動復帰
set -euo pipefail

IMAGE="youtube:2.0"
CONTAINER_NAME="rakkoma_watcher"

# 既存コンテナを停止・削除（冪等起動）
if docker ps -q --filter "name=^${CONTAINER_NAME}$" | grep -q .; then
    echo "[INFO] 既存コンテナを停止: ${CONTAINER_NAME}"
    docker stop "$CONTAINER_NAME"
fi
if docker ps -aq --filter "name=^${CONTAINER_NAME}$" | grep -q .; then
    echo "[INFO] 既存コンテナを削除: ${CONTAINER_NAME}"
    docker rm "$CONTAINER_NAME"
fi

# ホスト側 ~/.bashrc から API キー・Webhook を読み込む
#   SLACK_WEBHOOK_URL_RAKKOMA : 新着Slack通知用（scrape_rakkoma.py）
#   ANTHROPIC_API_KEY         : LLM評価用（analyze.py）
source ~/.bashrc 2>/dev/null || true

# 未設定でも set -u でクラッシュしないよう :- で空文字フォールバックし、警告のみ
[ -z "${SLACK_WEBHOOK_URL_RAKKOMA:-}" ] && echo "[WARN] SLACK_WEBHOOK_URL_RAKKOMA 未設定 → Slack通知は無効"
[ -z "${ANTHROPIC_API_KEY:-}" ]         && echo "[WARN] ANTHROPIC_API_KEY 未設定 → analyze.py のLLM評価は不可"

docker run -d \
    --name "$CONTAINER_NAME" \
    --restart unless-stopped \
    --init \
    -e "SLACK_WEBHOOK_URL_RAKKOMA=${SLACK_WEBHOOK_URL_RAKKOMA:-}" \
    -e "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}" \
    -v /home/masaya/y_work:/root/work \
    -v /home/masaya/rakkoma:/root/rakkoma \
    "$IMAGE" \
    python3 /root/rakkoma/scrape_rakkoma.py

echo ""
echo "[OK] コンテナ起動: ${CONTAINER_NAME}"
echo "     ログ確認 : docker logs -f ${CONTAINER_NAME}"
echo "     状態確認 : docker ps --filter name=${CONTAINER_NAME}"
echo "     停止     : docker stop ${CONTAINER_NAME}"
