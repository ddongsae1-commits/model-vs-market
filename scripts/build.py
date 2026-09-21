# -*- coding: utf-8 -*-
"""template.html + data/*.json → docs/index.html

GitHub Pages 는 docs/ 폴더를 그대로 웹에 올려 줍니다. 그래서 '빌드'라고 해봐야
자리표시자에 데이터를 끼워 넣는 게 전부입니다. 일부러 이렇게 단순하게 뒀습니다 --
빌드가 복잡해지면 고장났을 때 원인을 못 찾습니다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import store

ROOT = Path(__file__).resolve().parent.parent
PAGE_KEYS = ["gamesData", "trackRecord", "soccerData", "clvLog", "deepStats"]


def main():
    html = (ROOT / "template.html").read_text(encoding="utf-8")
    for k in PAGE_KEYS:
        ph = "{{" + k + "}}"
        if ph not in html:
            raise SystemExit(f"템플릿에 자리표시자가 없습니다: {ph}")
        html = html.replace(ph, store.read_text(k, "null"), 1)
    chips = store.read_chips()
    html = html.replace("{{dateChip}}", chips.get("dateChip", ""), 1)
    html = html.replace("{{runChip}}", chips.get("runChip", ""), 1)
    if "{{" in html and "}}" in html:
        import re
        left = re.findall(r"\{\{[a-zA-Z]+\}\}", html)
        if left:
            raise SystemExit(f"채우지 못한 자리표시자: {left}")
    out = ROOT / "docs" / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"빌드 완료 · docs/index.html {len(html):,d}자")


if __name__ == "__main__":
    main()
