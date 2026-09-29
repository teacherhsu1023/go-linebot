"""同步 assets/sai/ 的圖片與 sai_hints.json。

新增圖片後執行一次：
    python scripts/sync_sai_hints.py

- 圖片有、hints 沒有：把台詞加進 sai_hints.json（說明留空，請自己補上「適合回答什麼」）
- hints 有、圖片沒有：只列出提醒，不會刪除

只使用標準庫，不需要 LINE / GCP 相關的環境變數。
"""

import json
import re
import sys
import unicodedata
from pathlib import Path

ASSET_DIR = Path(__file__).resolve().parent.parent / "assets" / "sai"
HINTS_PATH = ASSET_DIR / "sai_hints.json"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
VARIANT_SUFFIX = re.compile(r"\s*\(\d+\)$")


def normalize(name: str) -> str:
    return unicodedata.normalize("NFC", name)


def main() -> int:
    hints = {}
    if HINTS_PATH.exists():
        hints = {
            normalize(k): v
            for k, v in json.loads(HINTS_PATH.read_text(encoding="utf-8")).items()
        }

    lines = set()
    for path in ASSET_DIR.iterdir():
        if path.suffix.lower() in IMAGE_EXTENSIONS:
            lines.add(normalize(VARIANT_SUFFIX.sub("", path.stem)).strip())

    added = sorted(lines - hints.keys())
    orphaned = sorted(hints.keys() - lines)
    empty = sorted(k for k in lines if k in hints and not hints[k])

    for line in added:
        hints[line] = ""

    HINTS_PATH.write_text(
        json.dumps(hints, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"台詞共 {len(lines)} 句，圖片目錄 {ASSET_DIR}")
    if added:
        print(f"\n新增 {len(added)} 句（請到 sai_hints.json 補上說明）：")
        for line in added:
            print(f"  + {line}")
    if empty:
        print(f"\n以下 {len(empty)} 句說明仍是空的：")
        for line in empty:
            print(f"  ? {line}")
    if orphaned:
        print(f"\nhints 裡有、但找不到圖片的 {len(orphaned)} 句：")
        for line in orphaned:
            print(f"  - {line}")
    if not (added or empty or orphaned):
        print("圖片與 hints 已同步。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
