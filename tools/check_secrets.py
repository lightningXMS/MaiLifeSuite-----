# -*- coding: utf-8 -*-
"""提交前敏感信息检查：扫描 git 跟踪文件中禁止出现的真实标识。

用法：python tools/check_secrets.py   （无输出/退出码 0 = 通过）
可在提交前手动执行，或接入 CI。FORBIDDEN 清单维护所有曾泄漏/禁止入库的标识。

覆盖范围与局限：
- 内容扫描覆盖全部 git 跟踪文件（含已暂存未提交的新文件、源码、文档、测试与配置）。
- 文件名扫描对疑似配置备份 / 环境文件直接判失败（文本扫描无法识别二进制备份中的密钥）。
- 局限：内容按 UTF-8 解码后匹配，被编码藏进二进制文件或非 UTF-8 变体的密钥无法识别；
  扫描只作用于当前工作树，不覆盖远端仍可达的历史提交对象。
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# 曾泄漏或禁止入库的真实标识（发现即失败；新增敏感号时同步追加）。
# Tavily 真实 Key 前缀为 tvly-（开发 Key 为 tvly-dev-）；生产前缀规则同时
# 命中开发前缀（子串关系），保留两条以便报告区分。
FORBIDDEN = [("Tavily API Key 开发前缀", "tvly-dev-"),
             ("Tavily API Key 生产前缀", "tvly-"),
             ("机器人账号", "3622957636"),
             ("私聊用户", "3359730152"), ("私聊用户", "1226370292"),
             ("私聊用户", "1289936609"), ("私聊用户", "3241497867"),
             ("私聊用户", "3113181988"), ("私聊用户", "1851208953"),
             ("群号", "893066853"), ("群号", "975411296"),
             ("群号", "1059613583"), ("群号", "1079275988"),
             ("群号", "571780722"), ("群号", "644408460"), ("群号", "319927359")]

# 规则定义处自带模式串，扫描内容时排除自身文件避免自命中。
SELF_FILES = {"tools/check_secrets.py", "tests/test_no_secrets.py"}


def find_forbidden(text: str, rules: list[tuple[str, str]] | None = None) -> list[str]:
    """返回 text 命中的规则描述列表（空列表 = 干净）；rules 缺省用 FORBIDDEN。"""
    active = FORBIDDEN if rules is None else rules
    return [f"含{name} {pat}" for name, pat in active if pat in text]


def find_forbidden_backup_paths(tracked: list[str]) -> list[str]:
    """跟踪文件名疑似配置备份 / 环境文件时返回命中描述（内容无关，直接判失败）。"""
    hits: list[str] = []
    for f in tracked:
        normalized = f.replace("\\", "/").casefold()
        name = normalized.rsplit("/", 1)[-1]
        if ("config_back/" in normalized or ".toml.backup" in normalized
                or name.endswith(".bak") or name.startswith(".env")):
            hits.append(f"{f}: 疑似配置备份/环境文件入库")
    return hits


def scan_tree() -> list[str]:
    """扫描当前 git 跟踪文件，返回全部命中描述（空列表 = 通过）。"""
    tracked = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True,
                             text=True, encoding="utf-8", errors="replace").stdout.splitlines()
    scannable = [f for f in tracked if f not in SELF_FILES]
    failures: list[str] = []
    for f in scannable:
        p = REPO / f
        if not p.is_file():
            continue
        try:
            text = p.read_bytes().decode("utf-8", errors="replace")
        except Exception:
            text = ""  # 无法解码的文件只走文件名检测。
        failures += [f"{f}: {hit}" for hit in find_forbidden(text)]
    failures += find_forbidden_backup_paths(scannable)
    return failures


def main() -> int:
    failures = scan_tree()
    if failures:
        print("发现敏感信息，禁止提交：")
        for line in failures:
            print("  -", line)
        return 1
    print(f"敏感信息检查通过（{len(FORBIDDEN)} 条内容规则 + 配置备份文件名检测）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
