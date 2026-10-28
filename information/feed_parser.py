"""搜索结果正文使用的轻量 HTML 文本提取器。"""
from __future__ import annotations

import html
from html.parser import HTMLParser


def _clean_space(value:str)->str:return " ".join(html.unescape(str(value or "")).split())


class _TextExtractor(HTMLParser):
    def __init__(self)->None:
        super().__init__(convert_charrefs=True); self.parts=[]; self._skip=0
    def handle_starttag(self,tag:str,attrs:list[tuple[str,str|None]])->None:
        del attrs
        if tag in {"script","style","noscript","svg","nav","footer","header"}:self._skip+=1
        elif not self._skip and tag in {"p","br","li","h1","h2","h3","article","section"}:self.parts.append("\n")
    def handle_endtag(self,tag:str)->None:
        if tag in {"script","style","noscript","svg","nav","footer","header"} and self._skip:self._skip-=1
        elif not self._skip and tag in {"p","li","article","section"}:self.parts.append("\n")
    def handle_data(self,data:str)->None:
        if not self._skip and data.strip():self.parts.append(data)


def readable_text(raw_html:str,max_chars:int)->str:
    parser=_TextExtractor()
    try:parser.feed(raw_html)
    except Exception:return ""
    lines=[]
    for line in "".join(parser.parts).splitlines():
        clean=_clean_space(line)
        if len(clean)>=12 and clean not in lines:lines.append(clean)
    return "\n".join(lines)[:max(0,int(max_chars))]
