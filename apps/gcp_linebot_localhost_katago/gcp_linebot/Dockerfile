# 依賴皆有預編譯 wheel（其餘為純 Python），不需要 gcc；
# MP4 由 imageio-ffmpeg 內建的 ffmpeg 產生，不需要 apt 安裝系統 ffmpeg。
FROM python:3.11-slim
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 依賴層：只有 requirements.txt 改變才會重建
COPY requirements.txt .
RUN pip install -r requirements.txt

# 體積最大的圖片獨立一層，程式碼改動時不必重推 150MB
COPY assets/ ./assets/

# 程式碼：新增頂層檔案或目錄時，記得補在這裡
COPY main.py config.py logger.py __init__.py ./
COPY LLM/ ./LLM/
COPY handlers/ ./handlers/
COPY services/ ./services/
COPY scripts/ ./scripts/

EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
