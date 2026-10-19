"""直播棋譜：抓取正在進行的職業比賽（不是歷史棋譜）。

主要來源是弈客（home.yikeweiqi.com/#/live），列表與棋譜都需要帶簽章 header，
簽法來自弈客網頁前端的 JS；弈客失敗時退回星陣（19x19.com），星陣轉播的其實是
弈客的部分棋局，不需要簽章。兩者都是非官方 API，對方改版時可能需要重新調整。

為了不對來源網站造成負擔，列表與每局棋譜都快取 CACHE_TTL_SECONDS 秒，
快取存在 GCS（Cloud Run 多個 instance 共用），記憶體再多一層。
"""

import asyncio
import hashlib
import json
import random
import time
from typing import Any, Dict, List, Optional

import httpx
from opencc import OpenCC

from logger import logger

# ============================================================================
# 可調整的常數
# ============================================================================

# 列表與棋譜的快取秒數（圍棋盤面變化不快，5 分鐘查一次即可）
CACHE_TTL_SECONDS = 5 * 60

HTTP_TIMEOUT_SECONDS = 10.0
GCS_PREFIX = "live"

# 弈客列表一頁 20 局，進行中的排在最前面；最多翻幾頁找進行中的棋局
YIKE_MAX_PAGES = 3

YIKE_API_URL = "https://api.yikeweiqi.com"
YIKE_NEW_API_URL = "https://api-new.yikeweiqi.com"
YIKE_APP_KEY = "3396jtzhK57XhJom"
YIKE_APP_SECRET = "hfdSXRKm0DQyLmNXmNCNkZpjy2o5q1Hk"
YIKE_ACCESS_TOKEN_SALT = "@1%e$5*f@3"
YIKE_VERSION = "96813"
YIKE_STATUS_LIVE = 2

XINGZHEN_API_URL = "https://api.19x19.com/api/engine/golives"

SOURCE_YIKE = "yike"
SOURCE_XINGZHEN = "19x19"

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0 Safari/537.36"
    ),
}

# 簡轉繁；「里」在人名、賽名（藤泽里菜、千里）幾乎都不該轉成「裡」，保留原字
_opencc = OpenCC("s2tw")
_KEEP_CHARS = {"里"}


def to_traditional(text: str) -> str:
    converted = _opencc.convert(text)
    if len(converted) != len(text):
        return converted
    return "".join(
        src if src in _KEEP_CHARS else dst for src, dst in zip(text, converted)
    )


# ============================================================================
# 快取（記憶體 + GCS）
# ============================================================================

_memory_cache: Dict[str, Dict[str, Any]] = {}


def _is_fresh(entry: Optional[Dict[str, Any]]) -> bool:
    return bool(entry) and time.time() - entry.get("fetched_at", 0) < CACHE_TTL_SECONDS


async def _load_cache(key: str) -> Optional[Dict[str, Any]]:
    entry = _memory_cache.get(key)
    if _is_fresh(entry):
        return entry

    from services.storage import download_file_as_text, file_exists

    remote_path = f"{GCS_PREFIX}/cache/{key}.json"
    try:
        if await file_exists(remote_path):
            entry = json.loads(await download_file_as_text(remote_path))
            if _is_fresh(entry):
                _memory_cache[key] = entry
                return entry
    except Exception as error:
        logger.warning(f"Failed to load live cache {key}: {error}")
    return None


async def _save_cache(key: str, data: Any) -> None:
    entry = {"fetched_at": time.time(), "data": data}
    _memory_cache[key] = entry

    from services.storage import upload_buffer

    try:
        await upload_buffer(
            json.dumps(entry, ensure_ascii=False).encode("utf-8"),
            f"{GCS_PREFIX}/cache/{key}.json",
            content_type="application/json",
            cache_control="no-cache, max-age=0",
        )
    except Exception as error:
        logger.warning(f"Failed to save live cache {key}: {error}")


# ============================================================================
# 弈客
# ============================================================================


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _yike_headers() -> Dict[str, str]:
    nonce = str(random.randint(0, 10**8))
    now_ms = str(int(time.time() * 1000))
    return {
        **HTTP_HEADERS,
        "AppKey": YIKE_APP_KEY,
        "Nonce": nonce,
        "CurTime": now_ms,
        "CheckSum": hashlib.sha1(
            (YIKE_APP_SECRET + nonce + now_ms).encode("utf-8")
        ).hexdigest(),
        "timestamp": now_ms,
        "accesstoken": _md5(YIKE_ACCESS_TOKEN_SALT + _md5(now_ms) + "web"),
        "usertoken": "-1",
        "version": YIKE_VERSION,
        "Platform": "web",
        "uuid": "web",
        "Referer": "https://home.yikeweiqi.com/",
    }


async def _yike_get(client: httpx.AsyncClient, path: str, params: Dict[str, Any]):
    response = await client.get(
        f"{YIKE_API_URL}{path}", params=params, headers=_yike_headers()
    )
    response.raise_for_status()
    body = response.json()
    if body.get("Status") != 1200:
        raise RuntimeError(f"yike {path} failed: {body.get('Status')} {body.get('Message')}")
    return body["Result"]


async def _yike_new_get(client: httpx.AsyncClient, path: str):
    """api-new 的回應格式跟舊 API 不同：小寫欄位、成功時 status 為 0"""
    response = await client.get(f"{YIKE_NEW_API_URL}{path}", headers=_yike_headers())
    response.raise_for_status()
    body = response.json()
    if body.get("status") != 0:
        raise RuntimeError(f"yike {path} failed: {body.get('status')} {body.get('message')}")
    return body["result"]


async def _fetch_yike_list(client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    games = []
    for page in range(1, YIKE_MAX_PAGES + 1):
        result = await _yike_get(
            client,
            "/v2/golive/list",
            {"p": page, "since": 0, "official": 1, "version": 2, "usertoken": -1},
        )
        page_games = result.get("list") or []
        live_games = [g for g in page_games if g.get("Status") == YIKE_STATUS_LIVE]
        games.extend(
            {
                "source": SOURCE_YIKE,
                "id": str(g["Id"]),
                "name": g.get("GameName", ""),
                "black": g.get("BlackName", ""),
                "white": g.get("WhiteName", ""),
                "move_count": g.get("HandsCount", 0),
            }
            for g in live_games
        )
        # 進行中的排在最前面，這頁出現已結束的棋局就不用再翻下一頁
        if len(live_games) < len(page_games) or not page_games:
            break
    return games


async def _fetch_yike_record(client: httpx.AsyncClient, game_id: str) -> Dict[str, str]:
    """單局棋譜與結果；新版 API 會附上結果（如「白中盘胜」），失敗時改用舊版"""
    try:
        result = await _yike_new_get(client, f"/v1/golives/{game_id}")
        sgf_text, game_result = result.get("sgf"), result.get("game_result")
    except Exception as error:
        logger.warning(f"Failed to fetch yike game {game_id} from api-new: {error}")
        result = await _yike_get(client, "/v2/golive/dtl/", {"id": game_id})
        live = result.get("live") or {}
        sgf_text, game_result = live.get("Content"), live.get("GameResult")
    if not sgf_text:
        raise RuntimeError(f"yike game {game_id} has no sgf")
    return {"sgf": sgf_text, "result": game_result or ""}


# ============================================================================
# 星陣
# ============================================================================


async def _xingzhen_get(client: httpx.AsyncClient, path: str, params=None):
    response = await client.get(
        f"{XINGZHEN_API_URL}{path}", params=params, headers=HTTP_HEADERS
    )
    response.raise_for_status()
    body = response.json()
    if str(body.get("code")) != "0" or "data" not in body:
        raise RuntimeError(f"19x19 {path} failed: {body.get('code')} {body.get('msg')}")
    return body["data"]


async def _fetch_xingzhen_list(client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    data = await _xingzhen_get(client, "/all")
    return [
        {
            "source": SOURCE_XINGZHEN,
            "id": str(m["liveId"]),
            "name": m.get("name", ""),
            "black": m.get("pb", ""),
            "white": m.get("pw", ""),
            "move_count": m.get("moveNum", 0),
        }
        for m in (item.get("liveMatch") or {} for item in data)
        if m.get("liveId") and not m.get("gameResult")
    ]


def _sgf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("]", "\\]")


def _xingzhen_to_sgf(live_match: Dict[str, Any], moves: str) -> str:
    """星陣的手順是逗號分隔的 index：SGF 欄 = index % 19、列 = index // 19"""
    size = live_match.get("boardSize") or 19
    nodes = []
    for i, value in enumerate(v for v in moves.split(",") if v.strip()):
        color = "B" if i % 2 == 0 else "W"
        index = int(value)
        if 0 <= index < size * size:
            point = chr(ord("a") + index % size) + chr(ord("a") + index // size)
        else:
            point = ""  # 虛手
        nodes.append(f";{color}[{point}]")

    header = (
        f"(;GM[1]FF[4]CA[UTF-8]SZ[{size}]KM[{live_match.get('komi', '')}]"
        f"PB[{_sgf_escape(live_match.get('pb', ''))}]"
        f"PW[{_sgf_escape(live_match.get('pw', ''))}]"
        f"GN[{_sgf_escape(live_match.get('name', ''))}]"
    )
    return header + "".join(nodes) + ")"


def _format_sgf_result(result: str) -> str:
    """把 SGF 格式的結果（B+R、W+3.5、B+T）轉成中文"""
    if "+" not in result:
        return result
    color, _, detail = result.partition("+")
    winner = {"B": "黑", "W": "白"}.get(color.upper())
    if not winner:
        return result
    if detail.upper() in ("R", "RESIGN"):
        return f"{winner}中盤勝"
    if detail.upper() in ("T", "TIME"):
        return f"{winner}超時勝"
    if detail:
        return f"{winner}勝 {detail} 目"
    return f"{winner}勝"


async def _fetch_xingzhen_record(client: httpx.AsyncClient, game_id: str) -> Dict[str, str]:
    data = await _xingzhen_get(client, f"/situation/{game_id}", {"no_cache": 1})
    live_match = data.get("liveMatch") or {}
    return {
        "sgf": _xingzhen_to_sgf(live_match, data.get("moves") or ""),
        "result": _format_sgf_result(live_match.get("gameResult") or ""),
    }


# ============================================================================
# 對外介面
# ============================================================================


async def get_live_games() -> List[Dict[str, Any]]:
    """正在進行的直播列表（弈客優先，失敗時退回星陣），名稱保持來源的簡體字"""
    cached = await _load_cache("list")
    if cached:
        return cached["data"]

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        try:
            games = await _fetch_yike_list(client)
        except Exception as error:
            logger.warning(f"Failed to fetch yike live list, falling back to 19x19: {error}")
            games = await _fetch_xingzhen_list(client)

    await _save_cache("list", games)
    return games


async def get_live_record(game: Dict[str, Any]) -> Dict[str, str]:
    """取得某一局直播目前的棋譜：{"sgf": SGF 字串, "result": 結果，進行中為空字串}

    列表快取 5 分鐘，這段期間內棋局可能剛好下完，所以這裡會帶上結果。
    """
    key = f"record_{game['source']}_{game['id']}"
    cached = await _load_cache(key)
    if cached:
        return cached["data"]

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        if game["source"] == SOURCE_YIKE:
            try:
                record = await _fetch_yike_record(client, game["id"])
            except Exception as error:
                # 星陣的 liveId 與弈客的棋局 Id 相同，可以直接拿來查
                logger.warning(
                    f"Failed to fetch yike game {game['id']}, falling back to 19x19: {error}"
                )
                record = await _fetch_xingzhen_record(client, game["id"])
        else:
            record = await _fetch_xingzhen_record(client, game["id"])

    await _save_cache(key, record)
    return record


if __name__ == "__main__":
    # 不經過 GCS 快取，直接測試兩個來源：python -m handlers.live_handler
    async def _main():
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            for fetch_list, fetch_record in (
                (_fetch_yike_list, _fetch_yike_record),
                (_fetch_xingzhen_list, _fetch_xingzhen_record),
            ):
                games = await fetch_list(client)
                print(f"== {fetch_list.__name__}: {len(games)} live games")
                for i, g in enumerate(games, 1):
                    print(i, to_traditional(f"{g['name']} 黑-{g['black']} 白-{g['white']}"))
                if games:
                    record = await fetch_record(client, games[0]["id"])
                    print(record["result"] or "進行中", record["sgf"][:200])

    asyncio.run(_main())
