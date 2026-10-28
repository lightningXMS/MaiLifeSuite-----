"""Mai_life 指令菜单的结构化目录与纯文本降级内容。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True,slots=True)
class CommandItem:
    command:str
    description:str


@dataclass(frozen=True,slots=True)
class CommandSection:
    title:str
    items:tuple[CommandItem,...]


COMMAND_SECTIONS:tuple[CommandSection,...]=(
    CommandSection("日常状态",(
        CommandItem("/麦麦状态","生活状态、模型与后台任务健康"),
        CommandItem("/麦麦配置","当前插件配置摘要"),
    )),
    CommandSection("生活记录",(
        CommandItem("/麦麦添加日期 日期 名称","添加自己的重要日期（需用户档案）"),
        CommandItem("/麦麦删除日期 编号","删除自己的重要日期（需用户档案）"),
        CommandItem("/麦麦确认日期 编号 [日期]","确认日期候选（需用户档案）"),
    )),
    CommandSection("见闻与书柜",(
        CommandItem("/麦麦转述 群QQ号 内容","向白名单群发起简单转述"),
    )),
    CommandSection("管理与诊断",(
        CommandItem("/麦麦立即创作","立即执行创作判断（管理员）"),
        CommandItem("/麦麦重生日程","重新生成今日日程（管理员）"),
        CommandItem("/麦麦休息测试","休息闸门诊断（管理员）"),
    )),
)


def build_command_usage_text(notice:str="")->str:
    """构造不依赖 Markdown 的命令菜单文本，命令菜单的唯一发送形态。"""
    lines=["麦生活 · 指令中心","输入 /麦麦 或 /麦麦帮助 可再次查看菜单。"]
    clean_notice=" ".join(str(notice or "").replace("\x00","").split())[:120]
    if clean_notice:lines.extend(("",clean_notice))
    for section in COMMAND_SECTIONS:
        lines.extend(("",section.title))
        lines.extend(f"{item.command}  {item.description}" for item in section.items)
    lines.extend(("","私聊管理员无需重复创建用户档案；重要日期操作仍需启用用户档案。"))
    return "\n".join(lines)


__all__=["COMMAND_SECTIONS","CommandItem","CommandSection","build_command_usage_text"]
