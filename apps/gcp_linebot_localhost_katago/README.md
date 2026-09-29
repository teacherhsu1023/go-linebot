# GCP + 本地 KataGo 分離架構

## 架構說明

此架構將服務分為兩個部分：

1. **GCP Cloud Run 服務** (`gcp_linebot/`)

   - 處理 LINE Bot Webhook 請求
   - 提供圍棋對弈功能
   - 上傳 SGF 檔案至 GCS
   - 呼叫本地 KataGo 服務進行覆盤
   - 生成 GIF 動畫和 LLM 評論
   - 回傳結果至 LINE

2. **本地 KataGo 服務** (`localhost_katago/`)
   - 接收來自 Cloud Run 的覆盤請求
   - 從 GCS 下載 SGF 檔案
   - 執行 KataGo 全盤覆盤分析
   - 上傳分析結果至 GCS
   - 通知 Cloud Run 分析完成

### 架構流程

**對弈功能：**

```
1. 用戶在 LINE 中輸入座標（如 D4, Q16）
2. Cloud Run 服務接收請求並驗證落子合法性
3. 更新棋盤狀態並保存為 SGF 格式至 GCS
4. 生成當前棋盤圖片並回傳至 LINE
```

**形勢判斷功能：**
```
1. 用戶在進行中的對局裡輸入「形勢」或「形式」或「evaluation」
2. Cloud Run 取得當前對局的 SGF（GCS）並呼叫本地 KataGo /evaluation 端點
3. 本地服務執行 KataGo evaluation 分析（ownership / scoreLead）
4. Cloud Run 繪製領地分布圖並上傳圖片至 GCS，回傳目數差距與圖片至 LINE
```

**AI 對弈功能：**

```
1. 用戶輸入「對弈 ai」開啟 AI 對弈模式
2. 用戶在 LINE 中輸入座標（如 D4, Q16）
3. Cloud Run 服務接收請求並驗證落子合法性
4. 更新棋盤狀態並保存為 SGF 格式至 GCS
5. 生成用戶的棋盤圖片（暫不回傳）
6. Cloud Run 發起 HTTP 請求至本地 KataGo 服務
7. 本地服務從 GCS 下載 SGF 檔案
8. 本地服務執行 KataGo GTP 獲取 AI 下一步棋
9. 本地服務回調 Cloud Run
10. Cloud Run 更新 SGF 檔案並生成 AI 的棋盤圖片
11. 合併回傳：用戶的棋盤圖片 + AI 的棋盤圖片 + 文字訊息
```

**覆盤分析功能：**

```
1. 用戶透過 LINE 上傳 SGF 檔案或發送"覆盤"指令
2. Cloud Run 服務接收請求並上傳檔案至 GCS
3. Cloud Run 發起 HTTP 請求至本地 KataGo 服務
4. 本地服務從 GCS 下載 SGF 檔案
5. 本地服務執行 KataGo 分析
6. 分析結果上傳至 GCS
7. 本地服務回調 Cloud Run
8. Cloud Run 進行 LLM 分析並生成 GIF
9. 覆盤結果回傳至 LINE
```

## 前置準備

### 1. GCP 設定

#### 1.1 安裝 Google Cloud SDK

**以 macOS 為例:**

```bash
# 使用 Homebrew 安裝
brew install google-cloud-sdk
```

#### 1.2 初始化 GCP

```bash
# 登入 GCP
gcloud auth login

# 設定專案
gcloud config set project YOUR_PROJECT_ID

# 完成後運行以下指令，會顯示出你的 PROJECT_ID
gcloud config get-value project
```

#### 1.3 建立 GCS Bucket

前往 [Google Cloud Storage 設定頁面](https://console.cloud.google.com/storage/browser) 建立 bucket 以儲存 sgf 棋譜、每一手的棋盤圖片以及覆盤的 gif 圖檔

**重要：GCS Bucket 需要設定為公開訪問**

為了讓 LINE Bot 能夠顯示棋盤圖片，GCS Bucket 必須設定為公開訪問：

1. 在 GCS Bucket 設定頁面，點選「權限」標籤
2. 點選「新增主要項目」
3. 在「新增主要項目」欄位中輸入 `allUsers`
4. 選擇角色為「Storage 物件檢視者」（Storage Object Viewer）
5. 儲存設定

這樣 LINE Bot 才能正確顯示上傳到 GCS 的棋盤圖片。

#### 1.4 建立 Artifact Registry

前往 [Google Cloud Artifacts 設定頁面](https://console.cloud.google.com/artifacts)，建立 Artifact Registry，部署腳本會使用 Artifact Registry 來儲存 Docker 映像檔

#### 1.5 確認 GCP 相關變數設定

在部署前，需要設定以下環境變數和 Secrets：

**必要環境變數：**

1. **GCP_PROJECT_ID** - GCP 專案 ID

2. **GCS_BUCKET_NAME** - GCS Bucket 名稱

3. **LOCALHOST_KATAGO_URL** - 本地 KataGo 服務的公開 URL（用於 AI 對弈和覆盤功能）

   ```bash
   # 如果使用 cloudflare tunnel 或 ngrok
   export LOCALHOST_KATAGO_URL=https://your-tunnel-url.com
   ```

4. **CLOUD_RUN_CALLBACK_REVIEW_URL** - Cloud Run 服務的覆盤回調 URL
   ```bash
   # 部署後會自動取得，格式為：
   # https://SERVICE_NAME-PROJECT_NUMBER.REGION.run.app/callback/review
   export CLOUD_RUN_CALLBACK_REVIEW_URL=https://your-service-url.run.app/callback/review
   ```

5. **CLOUD_RUN_CALLBACK_GET_AI_NEXT_MOVE_URL** - Cloud Run 服務的 AI 對弈回調 URL
   ```bash
   # 部署後會自動取得，格式為：
   # https://SERVICE_NAME-PROJECT_NUMBER.REGION.run.app/callback/get_ai_next_move
   export CLOUD_RUN_CALLBACK_GET_AI_NEXT_MOVE_URL=https://your-service-url.run.app/callback/get_ai_next_move
   ```

**設定 Secrets Manager（用於敏感資訊）：**

於 [Google Cloud Secret Manager](https://console.cloud.google.com/security/secret-manager) 頁面設定需保密的資料

1. **LINE_CHANNEL_ACCESS_TOKEN** - LINE Bot Channel Access Token

2. **OPENAI_API_KEY** - OpenAI API Key

3. **TYPESAFE_API_KEY** - TypeSafe（Jev）API Key，佐為聊天功能使用（本機測試時填入 `.env` 即可）

**可選環境變數：**

請根據 GCP 服務中申請的名稱修改 `gcp_linebot/scripts/deploy.sh` 檔案中的各項環境變數預設值

- `CLOUD_RUN_SERVICE_NAME` - Cloud Run 服務名稱（預設：`go-linebot-webhook`）
- `GCP_REGION` - GCP 區域（預設：`asia-east1`）
- `ARTIFACT_REGISTRY_REPO` - Artifact Registry repository 名稱（預設：`go-linebot-repo`）
- `IMAGE_TAG` - Docker 映像檔標籤（預設：`latest`）

### 2. Python 環境設定

請參考 [localhost_all README - Python 環境設定](../localhost_all/README.md#1-python-環境設定) 的說明。

### 3. KataGo 設定

請參考 [localhost_all README - KataGo 設定](../localhost_all/README.md#2-katago-設定) 的說明。

**注意：** 本地 KataGo 服務的模型檔案路徑為：

- 模型目錄：`apps/gcp_linebot_localhost_katago/localhost_katago/katago/models`

### 4. 環境變數設定

#### 4.1 本地 KataGo 服務環境變數

建立 `.env` 檔案：

```bash
cd apps/gcp_linebot_localhost_katago/localhost_katago
cp env.example .env
```

編輯 `.env` 檔案：

```bash
# GCP Configuration
GCP_PROJECT_ID=your_gcp_project_id
GCS_BUCKET_NAME=your_gcp_bucket_name

# Server Configuration
PORT=3000
```

## 功能說明

### 佐為聊天功能

輸入「@佐為 聊天內容」，機器人會請 [TypeSafe Jev](https://docs.typesafe.ai/api) 從 `gcp_linebot/assets/sai/` 的台詞圖片中挑出最適合的一句，並回覆該張圖片。

- 圖片檔名（不含副檔名）就是台詞；同一句有多張圖時，命名為 `台詞 (2).png`，回覆時隨機挑一張
- `assets/sai/sai_hints.json` 為每句台詞補充「適合回答什麼問題」，這是 Jev 判斷準不準的關鍵
- 新增圖片後，執行 `python scripts/sync_sai_hints.py`，再到 `sai_hints.json` 補上新台詞的說明，不需要改程式
- 不經過 LINE 測試判斷結果：`python scripts/sai_check.py "你第一手棋都下在哪裡"`
- 信心門檻、含糊帶過的台詞等設定集中在 `handlers/sai_handler.py` 最上方
- 需要環境變數 `TYPESAFE_API_KEY`

### 形勢判斷功能

形勢判斷功能對當前盤面進行 KataGo 評估，顯示領地分布與目數差距。

**使用方式：**

1. 在進行中的對局裡輸入「形勢」或「形式」或「evaluation」
2. Bot 會回傳領地分布圖與目數文字（例如：目前形勢：黑 +3.5 目。）

**技術規格：**

- 分析引擎：KataGo evaluation（單一盤面）
- score_lead 為黑棋領先的目數

### AI 對弈功能

AI 對弈功能允許用戶與 KataGo AI 進行對戰。啟用後，用戶下完一手棋，AI 會自動思考並下下一手。

**使用方式：**

1. 輸入「對弈 ai」或「vs ai」開啟 AI 對弈模式
2. 開始下棋（例如：D4）
3. AI 會自動回應並下下一步棋
4. 輸入「對弈 free」或「vs free」關閉 AI 對弈模式，恢復一般對弈模式
5. 輸入「對弈」或「vs」查看當前模式狀態

**技術規格：**

- AI 引擎：KataGo GTP（visits=400）
- 思考時間：約 10 秒內
- 配置檔案：`localhost_katago/katago/configs/default_gtp.cfg`

**注意事項：**

- AI 對弈模式啟用後，用戶下完棋後不會立即收到回覆，需等待 AI 思考完成
- 系統會合併回傳用戶的棋盤圖片和 AI 的棋盤圖片，以及 AI 的落子位置

### 覆盤分析功能

覆盤分析功能使用 KataGo 對整局棋進行深度分析，找出關鍵手數並生成評論。

**使用方式：**

1. 上傳 SGF 棋譜檔案
2. 輸入「覆盤」開始分析
3. 等待約 10 分鐘獲得分析結果

**分析結果包含：**

- 🗺️ 全盤手順圖 - 顯示整局棋的所有手順
- 📈 勝率變化圖 - 顯示黑方勝率隨手數的變化曲線
- 🎬 關鍵手數 GIF 動畫 - 勝率差距最大的前 20 手動態演示
- 💬 ChatGPT 評論 - 針對關鍵手數的評論

**技術規格：**

- 分析引擎：KataGo AI（visits=1000）
- 分析時間：KataGo 全盤分析約 6 分鐘
- 評論生成：ChatGPT 評論生成約 3 分鐘
- 動畫繪製：GIF 動畫繪製約 10 秒

## 執行方式

### 本地測試

#### 1. 啟動 Cloud Run 服務

```bash
cd apps/gcp_linebot_localhost_katago/gcp_linebot
source venv/bin/activate
python main.py
```

#### 2. 啟動本地 KataGo 服務

```bash
cd apps/gcp_linebot_localhost_katago/localhost_katago
source venv/bin/activate
python main.py
```

### 建置並部署至 Cloud Run

```bash
cd ./gcp_linebot

# 使用部署腳本，並依照提示修改相關環境變數
./scripts/deploy.sh
```

## 專案結構

```
gcp_linebot_localhost_katago/
├── gcp_linebot/                    # Cloud Run 服務
│   ├── main.py                     # FastAPI 主程式
│   ├── config.py                   # 設定檔
│   ├── handlers/                   # 處理器
│   │   ├── line_handler.py         # LINE Bot 處理
│   │   ├── sgf_handler.py          # SGF 解析
│   │   ├── draw_handler.py         # GIF 生成
│   │   └── board_visualizer.py     # 棋盤視覺化
│   ├── services/
│   │   └── storage.py              # GCS 儲存服務
│   ├── LLM/
│   │   └── providers/              # LLM 提供者
│   ├── Dockerfile
│   └── requirements.txt
└── localhost_katago/               # 本地 KataGo 服務
    ├── main.py                     # KataGo 分析服務
    ├── config.py                   # 設定檔
    ├── handlers/
    │   └── katago_handler.py       # KataGo 處理
    ├── services/
    │   └── storage.py              # GCS 儲存服務
    ├── katago/                     # KataGo 相關檔案
    │   ├── models/                 # KataGo 模型
│   ├── configs/                # 設定檔
│   ├── review.py               # 覆盤分析
│   └── evaluation.py           # 形勢判斷
    ├── env.example
    └── requirements.txt
```
