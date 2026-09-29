#!/bin/bash

# 設定錯誤時立即退出
set -e

# 取得腳本所在目錄（應用程式根目錄）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# 設定變數（可從環境變數覆蓋，否則使用預設值）
# 檢查 gcloud 是否可用
if ! command -v gcloud &> /dev/null; then
    echo "❌ 錯誤：找不到 gcloud 命令，請先安裝 Google Cloud SDK"
    exit 1
fi

PROJECT_ID=${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || echo "")}
SERVICE_NAME=${CLOUD_RUN_SERVICE_NAME:-"go-linebot-webhook"}
REGION=${GCP_REGION:-"asia-east1"}
REPOSITORY=${ARTIFACT_REGISTRY_REPO:-"go-linebot-repo"}
IMAGE_TAG=${IMAGE_TAG:-"latest"}
IMAGE_NAME="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/go-linebot-image:${IMAGE_TAG}"

# 驗證必要變數
if [ -z "$PROJECT_ID" ]; then
    echo "❌ 錯誤：無法取得 PROJECT_ID，請設定 GCP_PROJECT_ID 環境變數或執行 'gcloud config set project YOUR_PROJECT_ID'"
    exit 1
fi

# 檢查是否在正確的目錄
if [ ! -f "$APP_DIR/Dockerfile" ]; then
    echo "❌ 錯誤：找不到 Dockerfile，請確認在正確的目錄執行腳本"
    exit 1
fi

echo "📦 專案資訊："
echo "   PROJECT_ID: $PROJECT_ID"
echo "   SERVICE_NAME: $SERVICE_NAME"
echo "   REGION: $REGION"
echo "   IMAGE: $IMAGE_NAME"
echo ""

# 建構映像檔（在應用程式目錄下執行，使用 .dockerignore）
echo "🚀 開始建構映像檔..."
cd "$APP_DIR"
if ! gcloud builds submit --tag "$IMAGE_NAME" --project "$PROJECT_ID"; then
    echo "❌ 映像檔建構失敗"
    exit 1
fi

# 設定環境變數預設值（可透過環境變數覆蓋）
# LOCALHOST_KATAGO_URL 用於 AI 對弈和覆盤功能（統一使用此 URL）
LOCALHOST_KATAGO_URL=${LOCALHOST_KATAGO_URL:-"https://assumption-coated-extensions-toys.trycloudflare.com"}
CLOUD_RUN_CALLBACK_REVIEW_URL=${CLOUD_RUN_CALLBACK_REVIEW_URL:-"https://go-linebot-webhook-731821281792.asia-east1.run.app/callback/review"}
CLOUD_RUN_CALLBACK_GET_AI_NEXT_MOVE_URL=${CLOUD_RUN_CALLBACK_GET_AI_NEXT_MOVE_URL:-"https://go-linebot-webhook-731821281792.asia-east1.run.app/callback/get_ai_next_move"}
GCS_BUCKET_NAME=${GCS_BUCKET_NAME:-"go-line-bot-files"}

# 驗證必要環境變數
if [ -z "$GCS_BUCKET_NAME" ]; then
    echo "❌ 錯誤：缺少 GCS_BUCKET_NAME，請設定環境變數"
    exit 1
fi

# 準備 Secrets Manager
SECRETS_ARG="--update-secrets=LINE_CHANNEL_ACCESS_TOKEN=LINE_CHANNEL_ACCESS_TOKEN:latest,OPENAI_API_KEY=OPENAI_API_KEY:latest,TYPESAFE_API_KEY=TYPESAFE_API_KEY:latest"

# 準備 Cloud Run 環境變數
# 包含應用啟動所需的所有環境變數
ENV_VARS_ARG="--set-env-vars=LOCALHOST_KATAGO_URL=${LOCALHOST_KATAGO_URL},CLOUD_RUN_CALLBACK_REVIEW_URL=${CLOUD_RUN_CALLBACK_REVIEW_URL},CLOUD_RUN_CALLBACK_GET_AI_NEXT_MOVE_URL=${CLOUD_RUN_CALLBACK_GET_AI_NEXT_MOVE_URL},GCP_PROJECT_ID=${PROJECT_ID},GCS_BUCKET_NAME=${GCS_BUCKET_NAME}"

# 部署到 Cloud Run
echo "🌐 正在部署到 Cloud Run..."
DEPLOY_CMD="gcloud run deploy $SERVICE_NAME \
  --image $IMAGE_NAME \
  --region $REGION \
  --cpu-boost \
  --platform managed \
  --allow-unauthenticated \
  --memory 512Mi \
  --cpu 1 \
  --port 8080 \
  --timeout=600 \
  --project $PROJECT_ID"

# 加入 env vars / Secrets 到部署命令
DEPLOY_CMD="$DEPLOY_CMD $ENV_VARS_ARG $SECRETS_ARG"

if ! eval "$DEPLOY_CMD"; then
    echo "❌ Cloud Run 部署失敗"
    exit 1
fi

# 顯示服務 URL
SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" --region "$REGION" --project "$PROJECT_ID" --format 'value(status.url)')
echo ""
echo "✅ 部署完成！"
echo "🌐 服務 URL: $SERVICE_URL"
echo "📋 Webhook URL: $SERVICE_URL${WEBHOOK_PATH:-/webhook}"