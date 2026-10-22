"""直播棋譜：抓取正在進行的職業比賽（不是歷史棋譜）。

主要來源是弈客（home.yikeweiqi.com/#/live），列表與棋譜都需要帶簽章 header，
簽法來自弈客網頁前端的 JS；弈客失敗時退回星陣（19x19.com），星陣轉播的其實是
弈客的部分棋局，不需要簽章。兩者都是非官方 API，對方改版時可能需要重新調整。

棋戰預定取自弈棋（yigo.org）首頁嵌入的 Google 日曆，接在直播列表後面。

為了不對來源網站造成負擔，列表與每局棋譜都快取 CACHE_TTL_SECONDS 秒、棋戰預定快取
SCHEDULE_CACHE_TTL_SECONDS 秒，快取存在 GCS（Cloud Run 多個 instance 共用），記憶體再多一層。
"""

import asyncio
import datetime
import hashlib
import json
import random
import re
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

# 棋戰預定：弈棋首頁「棋戰預定」欄位嵌入的 Google 公開日曆（依顯示順序）
SCHEDULE_CALENDARS = [
    ("台灣", "282p5pbvkd24rs22pva8p6datk@group.calendar.google.com"),
    ("中國", "4ukicnhe56g4sp1alg4fjo0hnc@group.calendar.google.com"),
    ("日本", "nvuf5nq1qmcsbop92895u142u4@group.calendar.google.com"),
    ("韓國", "a73c9vd6p2ofb03n118a3a49o0@group.calendar.google.com"),
    ("國際", "nc7e6730no5n0ma1sem39p6o68@group.calendar.google.com"),
]
SCHEDULE_ICS_URL = "https://calendar.google.com/calendar/ical/{calendar_id}/public/basic.ics"
SCHEDULE_DAYS = 3  # 含今天
# 日曆檔每個都有數百 KB 到 2MB，內容也不常變，快取久一點
SCHEDULE_CACHE_TTL_SECONDS = 6 * 60 * 60
SCHEDULE_HTTP_TIMEOUT_SECONDS = 30.0
TAIPEI_TZ = datetime.timezone(datetime.timedelta(hours=8))  # 台灣沒有日光節約時間
WEEKDAYS = "一二三四五六日"

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


def _is_fresh(entry: Optional[Dict[str, Any]], ttl: float) -> bool:
    return bool(entry) and time.time() - entry.get("fetched_at", 0) < ttl


async def _load_cache(key: str, ttl: float = CACHE_TTL_SECONDS) -> Optional[Dict[str, Any]]:
    entry = _memory_cache.get(key)
    if _is_fresh(entry, ttl):
        return entry

    from services.storage import download_file_as_text, file_exists

    remote_path = f"{GCS_PREFIX}/cache/{key}.json"
    try:
        if await file_exists(remote_path):
            entry = json.loads(await download_file_as_text(remote_path))
            if _is_fresh(entry, ttl):
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


def _yike_ai(game: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """列表裡的即時 AI 分析：BlackWinRate 為黑勝率（%）、Delta 為黑領先目數、RateHandsCount 為分析到第幾手"""
    if not game.get("RealtimeAnalysisFlag"):
        return None
    try:
        return {
            "black_win_rate": float(game["BlackWinRate"]),
            "black_lead": float(game["Delta"]) if game.get("Delta") not in (None, "") else None,
            "move": int(game["RateHandsCount"]),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _yike_same_game_key(game: Dict[str, Any]):
    """同一盤棋的判斷依據：雙方棋手（不分黑白）＋對局日期"""
    players = frozenset(
        str(game.get(id_key) or game.get(name_key, ""))
        for id_key, name_key in (("BlackPlayer", "BlackName"), ("WhitePlayer", "WhiteName"))
    )
    return players, game.get("GameDate", "")


def _merge_yike_duplicates(live_games: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """熱門對局弈客會另開一個置頂直播間（TopFlag=1，資料來自新浪），跟一般直播間是同一盤棋，
    但人氣、AI 分析各自計算，置頂間開得晚，AI 勝率有時還沒出來。這裡合併成一筆：
    - 位置：同一組中最前面的那筆（置頂的排最前面）
    - 棋譜：優先用一般直播間（有官方直播員、開得早）
    - 名稱：取最短的（一般直播間的名稱會再加上「黑 VS 白」）
    - AI：取有勝率且分析到最新手數的那份
    """
    groups: Dict[Any, List[Dict[str, Any]]] = {}
    for game in live_games:
        groups.setdefault(_yike_same_game_key(game), []).append(game)

    merged = []
    for group in groups.values():
        record_game = min(group, key=lambda g: (bool(g.get("TopFlag")), g["Id"]))
        ais = [ai for ai in map(_yike_ai, group) if ai]
        merged.append(
            {
                "source": SOURCE_YIKE,
                "id": str(record_game["Id"]),
                "name": min((g.get("GameName", "") for g in group), key=len),
                "black": record_game.get("BlackName", ""),
                "white": record_game.get("WhiteName", ""),
                "move_count": max(g.get("HandsCount", 0) for g in group),
                "ai": max(ais, key=lambda ai: ai["move"]) if ais else None,
            }
        )
    return merged


async def _fetch_yike_list(client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    live_games = []
    for page in range(1, YIKE_MAX_PAGES + 1):
        result = await _yike_get(
            client,
            "/v2/golive/list",
            {"p": page, "since": 0, "official": 1, "version": 2, "usertoken": -1},
        )
        page_games = result.get("list") or []
        page_live = [g for g in page_games if g.get("Status") == YIKE_STATUS_LIVE]
        live_games.extend(page_live)
        # 進行中的排在最前面，這頁出現已結束的棋局就不用再翻下一頁
        if len(page_live) < len(page_games) or not page_games:
            break
    return _merge_yike_duplicates(live_games)


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


def _xingzhen_ai(live_match: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """星陣的 winrate 是黑勝率（0~1），對應目前的手數；沒有目差"""
    if live_match.get("winrate") is None:
        return None
    try:
        return {
            "black_win_rate": float(live_match["winrate"]) * 100,
            "black_lead": None,
            "move": int(live_match.get("moveNum") or 0),
        }
    except (TypeError, ValueError):
        return None


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
            "ai": _xingzhen_ai(m),
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
# 棋戰預定（Google 日曆）
# ============================================================================


def _unescape_ics(text: str) -> str:
    return re.sub(r"\\([\\,;nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), text)


def _parse_ics_date(name: str, value: str) -> Optional[datetime.date]:
    """全天活動是 DTSTART;VALUE=DATE:20261023，少數有時間的活動是 UTC 時間，換成台灣日期"""
    try:
        if "VALUE=DATE" in name or len(value) == 8:
            return datetime.datetime.strptime(value[:8], "%Y%m%d").date()
        moment = datetime.datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
        if value.endswith("Z"):
            moment = moment.replace(tzinfo=datetime.timezone.utc).astimezone(TAIPEI_TZ)
        return moment.date()
    except ValueError:
        return None


def _parse_ics_events(ics_text: str):
    """回傳 (摘要, 開始日期, 結束日期(含)) 。地點（LOCATION）不需要，直接略過；
    日曆裡的重複性活動（RRULE）都在多年前結束，只取第一次"""
    text = re.sub(r"\r?\n[ \t]", "", ics_text.replace("\r\n", "\n"))
    for block in re.findall(r"BEGIN:VEVENT\n(.*?)END:VEVENT", text, re.S):
        fields = {}
        for line in block.split("\n"):
            name, sep, value = line.partition(":")
            if sep:
                fields.setdefault(name.split(";")[0], (name, value))
        if "SUMMARY" not in fields or "DTSTART" not in fields:
            continue
        start = _parse_ics_date(*fields["DTSTART"])
        if not start:
            continue
        end = _parse_ics_date(*fields["DTEND"]) if "DTEND" in fields else None
        if end and "VALUE=DATE" in fields["DTEND"][0]:
            end -= datetime.timedelta(days=1)  # 全天活動的 DTEND 是隔天（不含）
        yield _unescape_ics(fields["SUMMARY"][1]).strip(), start, max(end or start, start)


async def _fetch_schedule(today: datetime.date) -> List[Dict[str, Any]]:
    """接下來 SCHEDULE_DAYS 天（含今天）每天的棋戰；跨日的棋戰每天都會列出"""
    days = [today + datetime.timedelta(days=i) for i in range(SCHEDULE_DAYS)]
    events_by_day: Dict[datetime.date, List[str]] = {day: [] for day in days}

    async with httpx.AsyncClient(timeout=SCHEDULE_HTTP_TIMEOUT_SECONDS) as client:
        responses = await asyncio.gather(
            *(
                client.get(SCHEDULE_ICS_URL.format(calendar_id=calendar_id), headers=HTTP_HEADERS)
                for _, calendar_id in SCHEDULE_CALENDARS
            ),
            return_exceptions=True,
        )

    for (region, _), response in zip(SCHEDULE_CALENDARS, responses):
        if isinstance(response, Exception) or response.status_code != 200:
            logger.warning(f"Failed to fetch {region} schedule calendar: {response}")
            continue
        for summary, start, end in _parse_ics_events(response.text):
            for day in days:
                if start <= day <= end and f"[{region}] {summary}" not in events_by_day[day]:
                    events_by_day[day].append(f"[{region}] {summary}")

    return [{"date": day.isoformat(), "events": events_by_day[day]} for day in days]


# ============================================================================
# 對外介面
# ============================================================================


def format_ai_line(ai: Optional[Dict[str, Any]], current_move: int) -> Optional[str]:
    """例如「📊 AI（2 手之前）：黑勝率 65.8%，黑領先 2.4 目」

    列表與棋譜分開快取，勝率可能比盤面晚幾手；棋盤上沒有手數，所以寫「n 手之前」。
    """
    if not ai:
        return None
    moves_behind = current_move - ai["move"]
    when = "當前手數" if moves_behind <= 0 else f"{moves_behind} 手之前"
    text = f"📊 AI（{when}）：黑勝率 {ai['black_win_rate']:.1f}%"
    lead = ai.get("black_lead")
    if lead is not None:
        if lead > 0:
            text += f"，黑領先 {lead:g} 目"
        elif lead < 0:
            text += f"，白領先 {-lead:g} 目"
        else:
            text += "，雙方目數相當"
    return text


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


async def get_schedule_text() -> Optional[str]:
    """棋戰預定的文字，接在直播列表後面；抓不到時回傳 None"""
    today = datetime.datetime.now(TAIPEI_TZ).date()
    key = f"schedule_{today:%Y%m%d}"
    cached = await _load_cache(key, SCHEDULE_CACHE_TTL_SECONDS)
    if cached:
        schedule = cached["data"]
    else:
        schedule = await _fetch_schedule(today)
        if not any(day["events"] for day in schedule):
            return None
        await _save_cache(key, schedule)

    lines = ["📅 棋戰預定："]
    for day in schedule:
        date = datetime.date.fromisoformat(day["date"])
        lines.append(f"\n{date.month}/{date.day}（{WEEKDAYS[date.weekday()]}）")
        lines.extend(f"• {event}" for event in day["events"] or ["無"])
    return "\n".join(lines)


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
