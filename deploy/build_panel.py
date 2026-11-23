#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Собирает index.html из src/panel.template.html, src/panel.css и src/panel.js.

Панель отдаётся одним файлом (так её читает index.py и так её ждут тесты),
поэтому исходники разделены, а index.html — собранный артефакт.

    python3 deploy/build_panel.py          — пересобрать index.html
    python3 deploy/build_panel.py --check  — проверить, что index.html не разошёлся
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "src" / "panel.template.html"
CSS = ROOT / "src" / "panel.css"
JS = ROOT / "src" / "panel.js"
OUT = ROOT / "index.html"

CSS_MARK = "<!-- @panel-css -->"
JS_MARK = "<!-- @panel-js -->"


def build() -> str:
    template = TEMPLATE.read_text(encoding="utf-8")
    text = template.replace(CSS_MARK, CSS.read_text(encoding="utf-8"))
    return text.replace(JS_MARK, JS.read_text(encoding="utf-8"))


def main(argv) -> int:
    text = build()
    if "--check" in argv:
        current = OUT.read_text(encoding="utf-8")
        if current != text:
            print("index.html расходится со сборкой: запустите deploy/build_panel.py")
            return 1
        print("index.html совпадает со сборкой")
        return 0
    OUT.write_text(text, encoding="utf-8", newline="")
    print("index.html собран: %d символов" % len(text))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
