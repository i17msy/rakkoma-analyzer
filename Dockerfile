# ラッコM&A アナライザー 専用イメージ（軽量・単体運用）
# 依存は requests（スクレイプ/Slack）と anthropic（LLM評価）のみ。
# コードはランタイムで /root/rakkoma にマウントするので COPY しない。
FROM python:3.12-slim

ENV TZ=Asia/Tokyo \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir requests anthropic

WORKDIR /root/rakkoma

# 環境変数は docker run -e で注入:
#   ANTHROPIC_API_KEY         … 新着のLLM評価
#   SLACK_WEBHOOK_URL_RAKKOMA … 厳選通知（買い/様子見×適合≥4×総合≥2.5）
CMD ["python3", "scrape_rakkoma.py"]
