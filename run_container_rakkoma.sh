#!/bin/bash
# ラッコM&A アナライザー 専用デーモンコンテナ（軽量・単体運用）
# - 専用イメージ rakkoma:latest（Dockerfile から自動ビルド）で起動
# - python3 scrape_rakkoma.py を --init(tini) 管理で常駐
# - --restart unless-stopped: PC再起動 / Docker起動と同時に自動復帰
# 役割: 新着をSQLiteに一元化・LLM評価し、買い/様子見の厳選のみSlack通知。新着でダッシュボード再生成
set -euo pipefail

HOST_DIR="/home/masaya/rakkoma"     # ホスト側のリポジトリ（コンテナ /root/rakkoma にマウント）
IMAGE="rakkoma:latest"
CONTAINER_NAME="rakkoma_observer"   # 旧 watcher(=このClaudeセッションが居るコンテナ)を壊さないよう別名で起動

# 専用イメージが無ければビルド（slim・requests+anthropicのみ）
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "[INFO] 専用イメージをビルド: ${IMAGE}"
    docker build -t "$IMAGE" "$HOST_DIR"
fi

# 既存コンテナを停止・削除（冪等起動）
if docker ps -aq --filter "name=^${CONTAINER_NAME}$" | grep -q .; then
    echo "[INFO] 既存コンテナを置き換え: ${CONTAINER_NAME}"
    docker rm -f "$CONTAINER_NAME" >/dev/null
fi

# ホスト側 ~/.bashrc から API キー・Webhook を読み込む（未設定でも :- で落ちない）
source ~/.bashrc 2>/dev/null || true
[ -z "${ANTHROPIC_API_KEY:-}" ]         && echo "[WARN] ANTHROPIC_API_KEY 未設定 → 評価・通知は行わずDB保存のみ"
[ -z "${SLACK_WEBHOOK_URL_RAKKOMA:-}" ] && echo "[WARN] SLACK_WEBHOOK_URL_RAKKOMA 未設定 → 通知は出ない"
[ -z "${R2_ACCESS_KEY_ID:-}" ]          && echo "[WARN] R2_* 未設定 → クラウド死活監視/R2バックアップなし（ローカル日次バックアップのみ）"

# 認証(account/key/secret)はMLBと共通の汎用 R2_*。バケットだけ rakkoma 専用に分離（R2_RAKKOMA_BUCKET）
docker run -d \
    --name "$CONTAINER_NAME" \
    --restart unless-stopped \
    --init \
    -e "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}" \
    -e "SLACK_WEBHOOK_URL_RAKKOMA=${SLACK_WEBHOOK_URL_RAKKOMA:-}" \
    -e "R2_ACCOUNT_ID=${R2_ACCOUNT_ID:-}" \
    -e "R2_ACCESS_KEY_ID=${R2_ACCESS_KEY_ID:-}" \
    -e "R2_SECRET_ACCESS_KEY=${R2_SECRET_ACCESS_KEY:-}" \
    -e "R2_BUCKET=${R2_RAKKOMA_BUCKET:-}" \
    -v "${HOST_DIR}:/root/rakkoma" \
    "$IMAGE"

echo ""
echo "[OK] コンテナ起動: ${CONTAINER_NAME}（イメージ ${IMAGE}・単体運用）"
echo "     ログ確認 : docker logs -f ${CONTAINER_NAME}"
echo "     状態確認 : docker ps --filter name=${CONTAINER_NAME}"
echo "     停止     : docker stop ${CONTAINER_NAME}"
echo "     再ビルド : docker rmi ${IMAGE} && bash ${HOST_DIR}/run_container_rakkoma.sh"
