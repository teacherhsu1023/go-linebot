"""不經過 LINE 和 GCS，直接測試佐為會怎麼回應。

    python scripts/sai_check.py "你第一手棋都下在哪裡"
    python scripts/sai_check.py            # 進入互動模式，一次測很多句

需要在 .env 或環境變數設定 TYPESAFE_API_KEY。
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from handlers import sai_handler  # noqa: E402


async def check(text: str) -> None:
    catalog = sai_handler.load_catalog()
    answer = await sai_handler.ask_jev(text, catalog)
    if answer is None:
        print("  Jev 沒有回應（檢查 TYPESAFE_API_KEY 與網路）")
        return

    probabilities = sorted(
        answer.get("probabilities", {}).items(), key=lambda kv: kv[1], reverse=True
    )
    print(f"  confidence = {answer.get('confidence'):.2f}（門檻 {sai_handler.MIN_CONFIDENCE}）")
    for key, probability in probabilities[:3]:
        print(f"  {probability:6.1%}  {key}")

    line = sai_handler.pick_line(answer, catalog)
    if line:
        print(f"  => 回覆：{line}")
    else:
        fallback = sai_handler.pick_fallback(catalog)
        print(f"  => 沒有把握，改回含糊帶過的台詞（例如：{fallback}）")


async def main() -> None:
    if len(sys.argv) > 1:
        text = " ".join(sys.argv[1:])
        print(f"使用者：{text}")
        await check(text)
        return
    while True:
        try:
            text = input("使用者> ").strip()
        except EOFError:
            return
        if text:
            await check(text)


if __name__ == "__main__":
    asyncio.run(main())
