"""佐為聊天：用 TypeSafe Jev 從固定的圖片台詞中挑出最適合的一張回覆。

台詞圖片放在 assets/sai/，檔名（不含副檔名）就是台詞。同一句台詞有多張圖時，
用「台詞 (2).png」「台詞 (3).png」命名，回覆時會隨機挑一張。
assets/sai/sai_hints.json 為每句台詞補充「適合回答什麼問題」，讓 Jev 判斷得更準；
沒寫也可以，只是會退回只看台詞本身。新增圖片後執行 scripts/sync_sai_hints.py 即可。

所有調整判斷行為的常數都集中在檔案最上方。
"""

import asyncio
import hashlib
import io
import json
import os
import random
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv

from logger import logger

load_dotenv()

# ============================================================================
# 可調整的常數
# ============================================================================

# 觸發名稱：輸入「@佐為 聊天內容」時啟動
SAI_TRIGGER_NAME = os.getenv("SAI_TRIGGER_NAME", "佐為")

JEV_API_URL = os.getenv("TYPESAFE_API_URL", "https://api.typesafe.ai/v1/systemone")
JEV_MODEL = "jev-latest"
JEV_TIMEOUT_SECONDS = 10.0
JEV_MAX_RETRIES = 2  # 429 / 529 時的重試次數（指數退避）

# Jev 的 confidence 低於這個值就視為「沒有合適的台詞」，改用 FALLBACK_LINES
MIN_CONFIDENCE = 0.35

# 「沒有合適台詞」選項的 key（不會和任何台詞撞名）
NO_MATCH_KEY = "__none__"
NO_MATCH_HINT = "上面所有台詞都無法自然地回應這句話（閒聊、無關話題、看不懂的內容）"

# 沒有合適台詞時，從這些台詞中隨機挑一句當作含糊帶過的回應
FALLBACK_LINES = ["有意思", "沒什麼", "說的也是"]

# 使用者訊息長度上限（避免把超長訊息送給 Jev）
MAX_USER_TEXT_LENGTH = 200

JEV_INSTRUCTIONS = (
    "使用者正在跟《棋靈王》的藤原佐為聊天，使用者的訊息就是 state。"
    "請從選項中挑出佐為最自然、最貼切的一句台詞來回應。"
    "選項的 key 是佐為的台詞，說明文字則是這句台詞適合回應的問題或情境。"
    "如果使用者在問一個具體的問題（例如第一手下在哪裡、你是誰、怎麼死的），"
    "請選字面上直接回答該問題的那句台詞，而不是語氣相近的句子。"
    f"如果沒有任何台詞合適，選 {NO_MATCH_KEY}。"
)

SAI_ASSET_DIR = Path(__file__).resolve().parent.parent / "assets" / "sai"
SAI_HINTS_PATH = SAI_ASSET_DIR / "sai_hints.json"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}

# LINE 限制：原圖 10MB 以內、縮圖 1MB 以內
PREVIEW_MAX_SIDE = 480
PREVIEW_JPEG_QUALITY = 80
GCS_PREFIX = "sai"

# ============================================================================
# 台詞目錄
# ============================================================================

_VARIANT_SUFFIX = re.compile(r"\s*\(\d+\)$")


def _normalize(name: str) -> str:
    """統一成 NFC，避免 macOS（NFD）和 Linux 的檔名比對不到。"""
    return unicodedata.normalize("NFC", name)


@dataclass
class SaiLine:
    text: str  # 台詞（也是給 Jev 的選項 key）
    hint: Optional[str]
    files: List[Path] = field(default_factory=list)  # 同一句台詞的多張圖


_catalog_cache: Optional[Tuple[Tuple, Dict[str, SaiLine]]] = None


def _scan_signature() -> Tuple:
    """圖片目錄與 hints 檔的簽章，內容有變動時才重新載入。"""
    paths = [p for p in SAI_ASSET_DIR.iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS]
    paths.append(SAI_HINTS_PATH)
    return tuple(
        sorted((p.name, p.stat().st_mtime_ns) for p in paths if p.exists())
    )


def load_hints() -> Dict[str, str]:
    if not SAI_HINTS_PATH.exists():
        return {}
    try:
        raw = json.loads(SAI_HINTS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        logger.error(f"sai_hints.json 格式錯誤，略過說明文字: {error}")
        return {}
    return {_normalize(k): v for k, v in raw.items() if isinstance(v, str) and v}


def load_catalog() -> Dict[str, SaiLine]:
    """讀取 assets/sai/，回傳 {台詞: SaiLine}。新增圖片不需要重啟。"""
    global _catalog_cache
    signature = _scan_signature()
    if _catalog_cache and _catalog_cache[0] == signature:
        return _catalog_cache[1]

    hints = load_hints()
    catalog: Dict[str, SaiLine] = {}
    for path in sorted(SAI_ASSET_DIR.iterdir()):
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        text = _normalize(_VARIANT_SUFFIX.sub("", path.stem)).strip()
        if not text or text == NO_MATCH_KEY:
            continue
        line = catalog.setdefault(text, SaiLine(text=text, hint=hints.get(text)))
        line.files.append(path)

    _catalog_cache = (signature, catalog)
    logger.info(f"載入佐為台詞 {len(catalog)} 句、圖片 {sum(len(l.files) for l in catalog.values())} 張")
    return catalog


# ============================================================================
# Jev
# ============================================================================


def build_jev_request(user_text: str, catalog: Dict[str, SaiLine]) -> dict:
    criteria: Dict[str, Optional[str]] = {
        line.text: line.hint for line in catalog.values()
    }
    criteria[NO_MATCH_KEY] = NO_MATCH_HINT
    return {
        "state": user_text[:MAX_USER_TEXT_LENGTH],
        "model": JEV_MODEL,
        "questions": {
            "reply": {
                "type": "choice",
                "instructions": JEV_INSTRUCTIONS,
                "criteria": criteria,
            }
        },
    }


async def ask_jev(user_text: str, catalog: Dict[str, SaiLine]) -> Optional[dict]:
    """呼叫 Jev，回傳 choice 答案（含 choice / probabilities / confidence）。失敗回傳 None。"""
    api_key = os.getenv("TYPESAFE_API_KEY")
    if not api_key:
        logger.error("TYPESAFE_API_KEY 未設定，無法使用佐為聊天")
        return None

    payload = build_jev_request(user_text, catalog)
    headers = {"Authorization": f"Bearer {api_key}"}

    async with httpx.AsyncClient(timeout=JEV_TIMEOUT_SECONDS) as client:
        for attempt in range(JEV_MAX_RETRIES + 1):
            try:
                response = await client.post(JEV_API_URL, json=payload, headers=headers)
            except httpx.HTTPError as error:
                logger.error(f"Jev 連線失敗: {error}")
                return None

            if response.status_code in (429, 529) and attempt < JEV_MAX_RETRIES:
                await asyncio.sleep(0.5 * 2**attempt)
                continue
            if response.status_code != 200:
                logger.error(f"Jev 回應 {response.status_code}: {response.text[:300]}")
                return None
            return response.json().get("answers", {}).get("reply")
    return None


def pick_line(answer: Optional[dict], catalog: Dict[str, SaiLine]) -> Optional[str]:
    """依 Jev 的答案決定要回哪句台詞；不確定時回傳 None。"""
    if not answer:
        return None
    choice = answer.get("choice")
    confidence = answer.get("confidence", 0.0)
    if choice == NO_MATCH_KEY or choice not in catalog:
        return None
    if confidence < MIN_CONFIDENCE:
        logger.info(f"Jev 信心不足 ({confidence:.2f} < {MIN_CONFIDENCE}): {choice}")
        return None
    return choice


def pick_fallback(catalog: Dict[str, SaiLine]) -> Optional[str]:
    candidates = [t for t in FALLBACK_LINES if t in catalog]
    return random.choice(candidates) if candidates else None


async def choose_sai_line(user_text: str) -> Optional[SaiLine]:
    """輸入使用者的聊天內容，回傳要回覆的台詞（含圖片檔）。"""
    catalog = load_catalog()
    if not catalog:
        logger.error(f"{SAI_ASSET_DIR} 內沒有任何圖片")
        return None

    answer = await ask_jev(user_text, catalog)
    line_text = pick_line(answer, catalog)
    if line_text is None and answer is not None:
        line_text = pick_fallback(catalog)
    if line_text is None:
        return None

    logger.info(
        f"佐為回應: {user_text!r} -> {line_text!r} "
        f"(confidence={(answer or {}).get('confidence')})"
    )
    return catalog[line_text]


# ============================================================================
# 圖片上傳（GCS 上一律用 ASCII 檔名，避開中文網址的編碼問題）
# ============================================================================

_uploaded_hashes: set = set()


def _make_preview(image_bytes: bytes) -> bytes:
    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as image:
        image = image.convert("RGB")
        image.thumbnail((PREVIEW_MAX_SIDE, PREVIEW_MAX_SIDE))
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=PREVIEW_JPEG_QUALITY)
        return output.getvalue()


async def get_image_urls(line: SaiLine) -> Optional[Tuple[str, str]]:
    """把這句台詞的一張圖上傳到 GCS（已上傳過就略過），回傳 (原圖 URL, 縮圖 URL)。"""
    from services.storage import file_exists, get_public_url, upload_buffer

    path = random.choice(line.files)
    image_bytes = await asyncio.to_thread(path.read_bytes)
    digest = hashlib.sha1(image_bytes).hexdigest()[:16]
    suffix = ".jpg" if path.suffix.lower() in (".jpg", ".jpeg") else ".png"
    content_type = "image/jpeg" if suffix == ".jpg" else "image/png"
    original_path = f"{GCS_PREFIX}/{digest}{suffix}"
    preview_path = f"{GCS_PREFIX}/{digest}_preview.jpg"

    if digest not in _uploaded_hashes:
        if not (await file_exists(original_path) and await file_exists(preview_path)):
            preview_bytes = await asyncio.to_thread(_make_preview, image_bytes)
            cache = "public, max-age=86400"
            await upload_buffer(
                image_bytes, original_path, content_type=content_type, cache_control=cache
            )
            await upload_buffer(
                preview_bytes, preview_path, content_type="image/jpeg", cache_control=cache
            )
        _uploaded_hashes.add(digest)

    return get_public_url(original_path), get_public_url(preview_path)


async def get_sai_reply_images(user_text: str) -> Optional[Tuple[str, str]]:
    """給 LINE handler 用：回傳要發送的 (原圖 URL, 縮圖 URL)，沒有適合的回應時回傳 None。"""
    line = await choose_sai_line(user_text)
    if line is None:
        return None
    return await get_image_urls(line)
