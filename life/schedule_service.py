"""Daily framework generation and near-term scene expansion."""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_ALLOWED={"meal","work","study","travel","leisure","sleep","nap","rest"}

# generate_json 失败时原样返回传入的 fallback 对象（无成功标志），用哨兵做身份判断
# 才能区分"模型真实生成"与"内部回落"——回落绝不能被记为生成成功。
_GEN_FAILED = object()

# 身份信号只从人格串里读已经写明的内容，不替用户假定身份；不同人的麦麦身份不同。
_IDENTITY_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("学生", "大学", "本科", "研究生", "硕士", "博士", "在读", "留学生",
      "大一", "大二", "大三", "大四", "高二", "高三", "初中生", "中学生"), "student"),
    (("上班", "职场", "打工人", "程序员", "工程师", "设计师", "产品经理", "老师",
      "教师", "医生", "护士", "研究员", "编辑", "运营", "实习", "会计", "律师"), "worker"),
    (("自由职业", "创作者", "写作者", "画师", "博主", "up主", "主播", "自媒体", "独立开发"), "creator"),
    (("居家", "家庭主妇", "全职妈妈", "全职爸爸", "退休"), "homebody"),
)
_IDENTITY_NOTES: dict[str, str] = {
    "student": ("人格表明麦麦是学生：工作日的在校段必须写成具体方向的上课、自习、实验、社团或赶作业"
                "（例如'上午去教学楼听两门课''下午在图书馆整理笔记''晚上在宿舍赶课程论文'），"
                "地点用学校、教室、图书馆、食堂、宿舍；不要编造固定课表，每天课程主题可以不同，周末可以离校活动。"),
    "worker": ("人格表明麦麦已经工作：白天段必须写成通勤、工位上的具体事务、午休或加班"
               "（例如'上午通勤后在工位赶项目进度''午休时下楼买咖啡''晚上加班整理方案'），地点用公司、工位、通勤路上。"),
    "creator": ("人格表明麦麦是自由创作者：白天段必须写成具体的创作、约稿、整理素材或学习技法"
                "（例如'上午在家画商稿的线稿''下午整理作品集''晚上学新的绘画技法'），地点以家里和工作室为主。"),
    "homebody": ("人格表明麦麦以居家生活为主：白天段写成具体的家务、采买、照顾家人或兴趣手艺"
                 "（例如'上午买菜做饭并打扫家里''下午打理阳台的花草''晚上做手工'），地点以家里和社区为主。"),
}
_GENERIC_IDENTITY_NOTE = ("如果人格里写明了身份（如学生、上班族、自由职业者），日程必须体现该身份的日常"
                          "（学生写上课/自习方向，职场写通勤/工位事务方向），概括到方向即可；"
                          "人格没有写明身份时按普通人的日常安排，不要假定特定身份。")
# 回落模板按身份定向时只改 work/study 段的方向与地点，时间结构不动。
_IDENTITY_FALLBACK: dict[str, dict[str, tuple[str, str]]] = {
    "student": {"work": ("上午去学校上课", "学校"), "study": ("下午上课或去图书馆自习", "学校")},
    "worker": {"work": ("到公司处理工作事务", "公司"), "study": ("学习专业技能或收尾未完成的方案", "公司")},
    "creator": {"work": ("在家处理创作委托", "家里"), "study": ("学新技法并整理素材", "家里")},
    "homebody": {"work": ("打理家务和日常采买", "家里"), "study": ("做兴趣手艺或收拾家里", "家里")},
}


def to_minute(text: str, *, allow_day_end: bool=False) -> int | None:
    try:
        if allow_day_end and text=="24:00":return 1440
        h,m=map(int,text.split(":"));
        if 0<=h<=23 and 0<=m<=59: return h*60+m
    except (ValueError,AttributeError):
        pass
    return None


def hhmm(value: int) -> str:
    value=max(0,min(1440,value)); return "24:00" if value==1440 else f"{value//60:02d}:{value%60:02d}"


class ScheduleService:
    def __init__(self, store: Any, config: Any, llm: Any, plugin_dir: str, logger: Any, bot_name: str = "麦麦") -> None:
        self.store=store; self.config=config; self.llm=llm; self.plugin_dir=Path(plugin_dir); self.logger=logger
        self.bot_name=bot_name; self._personality=""
        # 串行化同一天的实际生成：启动维护、每日定时、维护循环和管理员命令共用一把锁。
        self._generation_lock=asyncio.Lock()

    def update_config(self, config: Any) -> None:
        self.config=config

    def set_bot_name(self,name:str)->None:
        clean=str(name or "").strip()
        if clean:self.bot_name=clean

    def set_personality(self,personality:str)->None:
        """人格串含身份信息（如"大二学生"），回落模板按它定向；不同用户的麦麦身份不同。"""
        self._personality=str(personality or "")

    def _template(self) -> dict[str, Any]:
        root=self.plugin_dir.resolve()
        path=(root/str(self.config.schedule.template_file or "mai_template.json")).resolve()
        try:path.relative_to(root)
        except ValueError:
            self.logger.warning("[MaiLife] 日程模板必须位于插件目录内，已使用内置 fallback")
            return {}
        try:
            data=json.loads(path.read_text(encoding="utf-8-sig"))
            return data if isinstance(data,dict) else {}
        except Exception as exc:
            self.logger.warning(f"[MaiLife] 日程模板读取失败: {exc}"); return {}

    def _fallback(self, day: str, weekend: bool) -> list[dict[str, Any]]:
        builtin=[
            {"start":"00:00","end":"08:00","kind":"sleep","summary":"安稳睡觉","location":"卧室","energy_load":8,"shareability":0.05},
            {"start":"08:00","end":"09:00","kind":"meal","summary":"起床洗漱并吃早餐","location":"家里","energy_load":-1,"shareability":0.25},
            {"start":"09:00","end":"12:00","kind":"work","summary":"处理积压的私信和待办清单","location":"书桌前","energy_load":-4,"shareability":0.25},
            {"start":"12:00","end":"13:00","kind":"meal","summary":"准备午饭","location":"厨房","energy_load":-1,"shareability":0.4},
            {"start":"13:00","end":"13:40","kind":"nap","summary":"短暂午休","location":"卧室","energy_load":3,"shareability":0.05},
            {"start":"13:40","end":"18:00","kind":"study","summary":"整理这周的读书笔记和课程作业","location":"书桌前","energy_load":-5,"shareability":0.25},
            {"start":"18:00","end":"19:00","kind":"meal","summary":"做晚饭","location":"厨房","energy_load":-1,"shareability":0.55},
            {"start":"19:00","end":"23:30","kind":"leisure","summary":"拼完上次没拼完的拼图","location":"家里","energy_load":-3,"shareability":0.55},
            {"start":"23:30","end":"24:00","kind":"sleep","summary":"准备睡觉","location":"卧室","energy_load":1,"shareability":0.05},
        ]
        template=self._template(); key="weekend" if weekend else "workday"; raw=template.get(key)
        # 模板即使是数组也可能缺睡眠或进餐；校验失败必须回到稳定内置骨架。
        validated=self._validate(day,raw) if isinstance(raw,list) else []
        return self._apply_identity(validated or self._validate(day,builtin),self._personality)

    # 身份信号只从人格串里读已经写明的内容，不替用户假定身份；不同人的麦麦身份不同。
    @staticmethod
    def _identity_kind(personality: str) -> str:
        text=str(personality or "")
        for words,kind in _IDENTITY_RULES:
            if any(word in text for word in words): return kind
        return ""

    def _identity_note(self, personality: str) -> str:
        return _IDENTITY_NOTES.get(self._identity_kind(personality), _GENERIC_IDENTITY_NOTE)

    def _apply_identity(self, nodes: list[dict[str, Any]], personality: str) -> list[dict[str, Any]]:
        """回落模板按人格身份定向：只改 work/study 段的方向与地点，时间结构不动。"""
        plan=_IDENTITY_FALLBACK.get(self._identity_kind(personality))
        if not plan: return nodes
        result=[]
        for item in nodes:
            node=dict(item)
            if node.get("kind") in plan:
                summary,location=plan[str(node["kind"])]
                node["summary"]=summary; node["location"]=location
            result.append(node)
        return result

    def _state_summary(self, state: dict[str, Any] | None) -> str:
        """把当前精力/饥饿/心情浓缩成一句中文提示，供日程生成参考。"""
        if not isinstance(state,dict): return ""
        try:
            energy=float(state.get("energy",70)); hunger=float(state.get("hunger",20)); mood=float(state.get("mood_valence",0))
        except (TypeError,ValueError): return ""
        hunger_note="（很高，最近经常饿）" if hunger>75 else ""
        energy_note="（很低，需要多休息）" if energy<30 else ""
        mood_note="（偏低）" if mood<-0.3 else ""
        return (f"{self.bot_name}当前状态：精力 {energy:.0f}/100{energy_note}，饥饿 {hunger:.0f}/100{hunger_note}，"
                f"心情 {mood:+.2f}{mood_note}。生成日程时请保证正常三餐、不要安排过高强度。")

    @staticmethod
    def _apply_memory_hints(day:str,nodes:list[dict[str,Any]],memory_context:dict[str,Any]|None)->list[dict[str,Any]]:
        context=memory_context or {}; dates=context.get("private_date_hints") or []
        if not dates:return nodes
        result=[dict(item) for item in nodes]
        target=next((item for item in result if item.get("kind")=="leisure"),None)
        if target is None:return result
        target["summary"]=(str(target.get("summary") or "自由活动")+"，也为临近的重要安排做些准备")[:160]
        seed=f"{day}:{target['start_minute']}:{target['end_minute']}:{target['kind']}:{target['summary']}"
        target["id"]=hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
        return result

    # 所有 LLM 日程必须经过时间、类型、重叠和必要节点校验。
    def _validate(self, day: str, raw: Any, state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """规范化节点、修复可安全修复的重叠，并拒绝缺少夜间睡眠或进餐的框架。"""
        if not isinstance(raw,list): return []
        cleaned=[]
        for index,item in enumerate(raw):
            if not isinstance(item,dict): continue
            start=to_minute(str(item.get("start") or item.get("time") or ""))
            end=to_minute(str(item.get("end") or ""),allow_day_end=True)
            if start is None: continue
            if end is None: end=min(1440,start+60)
            if end<=start: continue
            kind=str(item.get("kind") or "leisure").lower(); kind=kind if kind in _ALLOWED else "leisure"
            summary=str(item.get("summary") or item.get("activity") or "自由活动")[:160]
            location=str(item.get("location") or "家里")[:80]
            try: energy=max(-12.0,min(12.0,float(item.get("energy_load",0))))
            except (TypeError,ValueError): energy=0
            try: share=max(0,min(1,float(item.get("shareability",0.3))))
            except (TypeError,ValueError): share=0.3
            cleaned.append({"start_minute":start,"end_minute":end,"kind":kind,"summary":summary,"location":location,"energy_load":energy,"shareability":share})
        # 排序后只向后推迟重叠节点，绝不把后段改回已经过去的时间。
        cleaned.sort(key=lambda x:x["start_minute"])
        result=[]; last_end=-1
        for item in cleaned:
            if item["start_minute"]<last_end: item["start_minute"]=last_end
            if item["end_minute"]<=item["start_minute"]: continue
            seed=f"{day}:{item['start_minute']}:{item['end_minute']}:{item['kind']}:{item['summary']}"
            item["id"]=hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]; item["day"]=day
            result.append(item); last_end=item["end_minute"]
        night_sleep=sum(
            max(0,min(item["end_minute"],10*60)-item["start_minute"])
            +max(0,item["end_minute"]-max(item["start_minute"],21*60))
            for item in result if item["kind"]=="sleep"
        )
        meals=sum(x["kind"]=="meal" for x in result)
        # 饥饿偏高时要求三餐齐全，避免少餐框架把饥饿锁在告警区。
        hungry=float((state or {}).get("hunger",0) or 0)>75
        if hungry and meals<3: return []
        return result if night_sleep>=5*60 and meals>=2 else []

    @staticmethod
    def _state_deltas(node:dict[str,Any])->dict[str,float]:
        """节点完成数值由本地规则给出，不接受模型任意改写核心状态。"""
        kind=str(node.get("kind") or "leisure")
        try:energy=max(-12.0,min(12.0,float(node.get("energy_load") or 0)))
        except (TypeError,ValueError):energy=0.0
        return {"energy":energy,"hunger":-35.0 if kind=="meal" else 0.0,
                "mood_valence":0.03 if kind=="leisure" else 0.0,"mood_arousal":0.0}

    # LLM 失败或结果不合格时使用模板骨架，保证日程服务始终可用。
    # 生成前收集三个变化源：最近几天日程、历法背景、兴趣素材；避免每天照抄模板。
    async def _variety_context(self, now: datetime) -> str:
        blocks: list[str] = []
        recent: list[str] = []
        for offset in range(1, 6):
            past = (now - timedelta(days=offset)).date().isoformat()
            nodes = await self.store.get_framework(past)
            if not nodes: continue
            text = "；".join(str(node.get("summary") or "")[:40] for node in nodes
                            if node.get("kind") not in {"sleep"})[:400]
            if text: recent.append(f"{past}：{text}")
        if recent:
            blocks.append("最近几天的安排（时间结构可以相似，具体内容必须明显错开）：\n"+"\n".join(recent))
        return "\n".join(blocks)

    async def ensure_day(self, now: datetime, personality: str, weather_text: str, force: bool=False,
                         memory_context: dict[str,Any]|None=None,
                         environment: dict[str,Any]|None=None) -> list[dict[str, Any]]:
        """确保当天生活框架存在；生成失败进入持久化重试，达上限后用历史归档随机兜底。

        成功 = 模型返回可解析 JSON 且通过 _validate 校验；内部回落（哨兵）绝不记为成功。
        force=True（每日定时重规划/管理员命令）开启新一轮尝试；失败时保留已有日程不覆盖。
        """
        day=now.strftime("%Y-%m-%d")
        existing=await self.store.get_framework(day)
        state_row=await self.store.get_generation_state(day)
        if not force and self._should_wait(existing,state_row,now): return existing
        async with self._generation_lock:
            # 双重检查：等锁期间其他入口（定时/维护/手动）可能已完成生成或推进重试状态。
            existing=await self.store.get_framework(day)
            state_row=await self.store.get_generation_state(day)
            if not force and self._should_wait(existing,state_row,now): return existing
            return await self._ensure_day_locked(day,now,existing,state_row,personality,weather_text,
                                                 force,memory_context,environment)

    @staticmethod
    def _should_wait(existing: list[dict[str,Any]]|None, state_row: dict[str,Any]|None,
                     now: datetime) -> bool:
        """非强制路径是否应跳过本轮生成（复用现有框架，或等待下次重试时间）。"""
        if not state_row:
            return bool(existing)
        status=str(state_row.get("status") or "")
        if status=="retrying":
            # 重试中：未到下次重试时间就复用现状（哪怕现状是临时骨架）。
            return now.timestamp()<float(state_row.get("next_retry_at") or 0)
        if status in {"success","exhausted"}:
            # 已成功/已达上限：有日程即复用；无日程属异常残留，放行走补写或自愈。
            return bool(existing)
        return bool(existing)

    async def _ensure_day_locked(self, day: str, now: datetime, existing: list[dict[str,Any]],
                                 state_row: dict[str,Any]|None, personality: str, weather_text: str,
                                 force: bool, memory_context: dict[str,Any]|None,
                                 environment: dict[str,Any]|None) -> list[dict[str, Any]]:
        cfg=self.config.schedule
        max_attempts=1+max(0,int(cfg.generation_max_retries))
        if not force and state_row and str(state_row.get("status") or "")=="exhausted" and not existing:
            # 达上限但当天仍无框架（最终兜底写入曾失败）：直接重做最终兜底，不重启 LLM 重试轮。
            state=await self.store.get_state(); weekend=now.weekday()>=5
            nodes,source=await self._final_fallback(day,weekend,state,now,memory_context)
            await self.store.replace_framework(day,nodes)
            await self.store.set_generation_state(day,status="exhausted",
                                                  attempts=int(state_row.get("attempts") or 0),
                                                  last_error=str(state_row.get("last_error") or ""),
                                                  has_temp=0,fallback_source=source)
            return nodes
        if force:
            attempts=0  # 强制重新生成开启新一轮尝试，重置计数。
        elif state_row and str(state_row.get("status") or "")=="retrying":
            attempts=int(state_row.get("attempts") or 0)
        else:
            attempts=0
        state=await self.store.get_state()
        ok,nodes,error=await self._generate_once(day,now,state,personality,weather_text,
                                                 memory_context,environment)
        attempts+=1
        weekend=now.weekday()>=5
        if ok and nodes is not None:
            await self.store.replace_framework(day,nodes)
            try:
                await self.store.archive_schedule(nodes,source_day=day,
                                                  day_type="weekend" if weekend else "workday",generator="llm")
            except Exception as exc:
                # 归档失败必须留痕，不得静默声称归档成功；当天日程本身仍正常生效。
                self.logger.error(f"[MaiLife] 日程归档写入失败 day={day}: {exc}")
            await self.store.set_generation_state(day,status="success",attempts=attempts,
                                                  last_error="",has_temp=0,fallback_source="")
            return nodes
        # ── 生成失败：不得立即用固定模板锁定当天，也不得覆盖已有日程 ──
        interval=max(1,int(cfg.generation_retry_interval_minutes))*60
        next_retry=now.timestamp()+interval
        prev_temp=int(state_row.get("has_temp") or 0) if state_row else 0
        prev_source=str(state_row.get("fallback_source") or "") if state_row else ""
        if attempts>=max_attempts:
            if existing and not prev_temp:
                # 达上限且当天已有真实日程 → 优先保留，不得无故替换。
                await self.store.set_generation_state(day,status="exhausted",attempts=attempts,
                                                      last_error=error,has_temp=0,
                                                      fallback_source=prev_source)
                self.logger.error(f"[MaiLife] 日程生成连续失败 {attempts} 次，保留当天现有日程 day={day}: {error}")
                return existing
            # 无日程，或只有临时骨架（has_temp=1）：用随机归档/内置模板作最终兜底并替换。
            nodes,source=await self._final_fallback(day,weekend,state,now,memory_context)
            await self.store.replace_framework(day,nodes)
            await self.store.set_generation_state(day,status="exhausted",attempts=attempts,
                                                  last_error=error,has_temp=0,fallback_source=source)
            self.logger.error(f"[MaiLife] 日程生成连续失败 {attempts} 次，已用 {source} 兜底 day={day}: {error}")
            return nodes
        if existing:
            # 已有日程（真实或临时骨架）保持不动，仅推进重试状态，等待到期重试。
            await self.store.set_generation_state(day,status="retrying",attempts=attempts,
                                                  next_retry_at=next_retry,last_error=error,
                                                  has_temp=prev_temp,fallback_source=prev_source)
            self.logger.warning(f"[MaiLife] 日程生成失败（第 {attempts}/{max_attempts} 次），保留现有日程，"
                                f"{cfg.generation_retry_interval_minutes} 分钟后重试 day={day}: {error}")
            return existing
        temp=self._apply_memory_hints(day,self._fallback(day,weekend),memory_context)
        await self.store.replace_framework(day,temp)
        await self.store.set_generation_state(day,status="retrying",attempts=attempts,
                                              next_retry_at=next_retry,last_error=error,
                                              has_temp=1,fallback_source="")
        self.logger.warning(f"[MaiLife] 日程生成失败（第 {attempts}/{max_attempts} 次），临时使用内置骨架，"
                            f"{cfg.generation_retry_interval_minutes} 分钟后重试 day={day}: {error}")
        return temp

    async def _generate_once(self, day: str, now: datetime, state: dict[str,Any],
                             personality: str, weather_text: str,
                             memory_context: dict[str,Any]|None,
                             environment: dict[str,Any]|None) -> tuple[bool,list[dict[str,Any]]|None,str]:
        """一次真正的生成尝试。成功的唯一定义：模型返回可解析 JSON 且通过日程校验。"""
        if not self.llm.task_available("schedule"):
            return False,None,"schedule 模型任务不可用"
        # 人格串是身份的唯一来源：刷新到实例，回落模板与提示词都按它定向。
        self._personality=str(personality or "")
        weekend=now.weekday()>=5
        variety=await self._variety_context(now)
        calendar_bits=[str(environment.get(key)) for key in ("day_type","holiday","lunar","solar_term","upcoming_festival")
                       if environment and environment.get(key) and environment.get(key)!="无"]
        if calendar_bits:
            variety=(variety+"\n今日历法："+ "，".join(calendar_bits[:4])).strip()
        ideas: list[str] = []
        try:
            for note in await self.store.recent_exploration_notes(now.timestamp(),5):
                # 关联分数过低的探索笔记只是噪音，不进灵感池。
                try:
                    if float(note.get("relevance_score") or 0)<0.35: continue
                except (TypeError,ValueError): continue
                topic=str(note.get("topic") or "").strip()
                if topic: ideas.append(topic[:40])
        except Exception: pass
        try:
            for doc in await self.store.list_bookshelf_documents(allow_private=True,limit=5):
                title=str(doc.get("title") or "").strip()
                if title and title!="未命名": ideas.append(f"自己的作品《{title[:30]}》")
        except Exception: pass
        picked=random.sample(ideas,min(3,len(ideas))) if ideas else []
        if picked:
            variety=(variety+"\n今天可以考虑的活动灵感（自由选用，不要硬塞）："+ "、".join(picked)).strip()
        prompt=(f"为虚拟网友{self.bot_name}生成{day}的生活框架。{'周末' if weekend else '工作日'}，天气背景：{weather_text}。\n"
                f"人格：{personality or '自然、独立、有自己的生活'}\n"
                f"身份落实：{self._identity_note(personality)}\n"
                f"{self._state_summary(state)}\n"
                f"参考节奏（只参考时间结构和比例，禁止照抄里面的描述）："
                f"{json.dumps(self._template().get('weekend' if weekend else 'workday',[]),ensure_ascii=False)}\n"
                f"匿名生活记忆：{json.dumps(memory_context or {},ensure_ascii=False)}。日期提示不含用户身份，不得猜测是谁。"
                f"{variety}\n"
                "要求：\n"
                "1) 每个节点的 summary 必须具体到'正在做什么'（例如'上午去教学楼听两门课''给连载小说写第三章''玩两把新出的游戏'），"
                "禁止'处理自己的事情''放松、看东西和随便逛逛'这类空泛描述；\n"
                "2) 工作/学习段必须与上面的人格身份一致（学生写上课/自习/社团方向，职场写通勤/工位事务方向，"
                "自由创作者写约稿/创作方向），location 与身份匹配（学生多用学校、图书馆、食堂、宿舍，"
                "职场多用公司、工位）；休闲段每天至少有一件事与最近几天不同；\n"
                "3) 节假日、节气、农历和天气要自然反映在活动里；\n"
                "4) 日程应符合普通人的时间、精力和生活常识，不安排突兀的高强度事项，时间不重叠，包含夜间睡眠和至少两顿饭；\n"
                "5) energy_load 是该节点对全天精力的影响合计值：工作/学习 -2~-6，睡眠 +4~+8，餐饮 -1，休闲 -1~-3，"
                "出行 -2~-4；全天 energy_load 合计应与当天精力消耗相抵（接近 0），睡眠增益不要重复计算白天消耗。\n"
                "返回JSON数组。字段必须是start,end,kind,summary,location,energy_load,shareability。"
                "kind只能是meal/work/study/travel/leisure/sleep/nap/rest。")
        try:
            raw=await self.llm.generate_json(prompt,"你是生活日程规划器，只输出合法JSON数组。",
                                             _GEN_FAILED,max_tokens=0,task_kind="schedule",
                                             request_type="daily_schedule")
        except Exception as exc:
            return False,None,f"模型调用异常: {exc}"
        if raw is _GEN_FAILED or not isinstance(raw,list):
            return False,None,"模型未返回有效 JSON"
        validated=self._validate(day,raw,state)
        if not validated:
            return False,None,"生成结果未通过日程校验"
        return True,validated,""

    async def _final_fallback(self, day: str, weekend: bool, state: dict[str,Any], now: datetime,
                              memory_context: dict[str,Any]|None) -> tuple[list[dict[str,Any]],str]:
        """重试上限后的最终兜底：优先随机复用未过期的历史归档，无可用归档才用内置固定模板。"""
        retention=max(1,int(self.config.schedule.generation_template_retention_days))
        now_ts=now.timestamp()
        try:
            await self.store.cleanup_schedule_records(retention,now_ts)
        except Exception as exc:
            self.logger.warning(f"[MaiLife] 日程归档清理失败: {exc}")
        wanted="weekend" if weekend else "workday"
        archives: list[dict[str,Any]] = []
        try:
            archives=await self.store.list_archives(now_ts,retention,day_type=wanted)
            if not archives:
                archives=await self.store.list_archives(now_ts,retention)
        except Exception as exc:
            self.logger.error(f"[MaiLife] 读取日程归档失败: {exc}")
        random.shuffle(archives)  # 随机选取（可测试：测试可用 random.seed 固定验证）。
        for archive in archives:
            try:
                raw_nodes=json.loads(archive.get("nodes_json") or "[]")
            except Exception:
                continue  # 损坏归档直接跳过。
            converted=self._convert_archive(raw_nodes,day,state)
            if converted:
                return converted,"archive_random"
        builtin=self._apply_memory_hints(day,self._fallback(day,weekend),memory_context)
        return builtin,"builtin_template"

    def _convert_archive(self, raw_nodes: Any, day: str,
                         state: dict[str,Any]) -> list[dict[str,Any]]|None:
        """把历史归档转换为当天日程：日期与节点 ID 按新日期重建，再次通过现有校验才可用。"""
        if not isinstance(raw_nodes,list): return None
        cleaned=[]
        for item in raw_nodes:
            if not isinstance(item,dict): continue
            try:
                start=int(item.get("start_minute") or 0); end=int(item.get("end_minute") or 0)
            except (TypeError,ValueError): continue
            share=item.get("shareability")
            cleaned.append({"start":hhmm(start),"end":hhmm(end),
                            "kind":str(item.get("kind") or "leisure"),
                            "summary":str(item.get("summary") or ""),
                            "location":str(item.get("location") or "家里"),
                            "energy_load":item.get("energy_load") or 0,
                            "shareability":share if isinstance(share,(int,float)) else 0.3})
        return self._validate(day,cleaned,state) or None

    @staticmethod
    def current_and_next(nodes: list[dict[str, Any]], minute: int) -> tuple[dict[str, Any]|None,dict[str, Any]|None]:
        current=None; nxt=None
        for node in nodes:
            if node["start_minute"]<=minute<node["end_minute"]: current=node
            elif node["start_minute"]>minute:
                nxt=node; break
        return current,nxt

    # 只细化临近节点，减少模型调用并保持场景与最新环境一致。
    async def expand_due(self, now: datetime, nodes: list[dict[str, Any]], state: dict[str, Any], weather_text: str) -> None:
        """细化当前与下一节点；状态增量始终由本地规则生成，模型只写叙事和机会。"""
        minute=now.hour*60+now.minute; lead=self.config.schedule.detail_lead_minutes
        due=[n for n in nodes if n["end_minute"]>minute and n["start_minute"]<=minute+lead][:2]
        for node in due:
            if await self.store.get_scene(node["id"]): continue
            expires=datetime.combine(now.date(),datetime.min.time(),tzinfo=now.tzinfo)+timedelta(minutes=node["end_minute"])
            fallback={"scene":node["summary"],"state_deltas":self._state_deltas(node),
                      "opportunities":([{"topic":node["summary"],"motive":"想自然分享一点正在经历的生活","weight":node["shareability"],"privacy":"normal"}] if node["shareability"]>=0.35 else [])}
            prompt=(f"把生活框架细化为具体但克制的场景。框架：{json.dumps(node,ensure_ascii=False)}\n"
                    f"人格：{self._personality or '自然、独立、有自己的生活'}\n"
                    f"当前精力{state.get('energy')}、饥饿{state.get('hunger')}、天气{weather_text}。\n"
                    "返回JSON对象：scene字符串和opportunities数组。机会字段topic,motive,weight。"
                    "场景必须与人格身份一致（学生写在校场景，职场写工位场景），不要凭空制造重大事件，不要强行想用户。")
            raw=fallback
            if self.llm.task_available("scene_detail"):
                raw=await self.llm.generate_json(prompt,"你是日常场景细化器，只返回JSON对象。",fallback,max_tokens=0,task_kind="scene_detail",request_type="scene_detail")
            if not isinstance(raw,dict): raw=fallback
            scene=str(raw.get("scene") or fallback["scene"])[:500]
            deltas=self._state_deltas(node)
            opportunities=[]
            for idx,item in enumerate(raw.get("opportunities") or []):
                if not isinstance(item,dict): continue
                try: weight=max(0,min(1,float(item.get("weight",0.3))))
                except (TypeError,ValueError): weight=0.3
                op_id=hashlib.sha1(f"{node['id']}:{idx}:{item.get('topic','')}".encode()).hexdigest()[:20]
                opportunities.append({"id":op_id,"topic":str(item.get("topic") or node["summary"])[:160],
                    "motive":str(item.get("motive") or "想分享生活")[:240],"weight":weight,
                    "privacy":"normal","expires_at":expires.timestamp()})
            await self.store.save_scene(node["id"],scene,deltas,opportunities)

    async def state_timeline(self,start:datetime,end:datetime)->list[dict[str,Any]]:
        """按日程边界切分离线时间；缺失的过去日程只使用本地模板，场景按 ID 批量读取。"""
        if end<=start:return []
        windows=[]; day=start.date()
        while day<=end.date():
            day_start=datetime.combine(day,datetime.min.time(),tzinfo=end.tzinfo)
            next_day=datetime.combine(day+timedelta(days=1),datetime.min.time(),tzinfo=end.tzinfo)
            window_start=max(start,day_start); window_end=min(end,next_day)
            if window_end<=window_start:day+=timedelta(days=1); continue
            stored=await self.store.get_framework(day.isoformat())
            nodes=stored or self._fallback(day.isoformat(),day.weekday()>=5)
            windows.append((window_start,window_end,day_start,stored,nodes))
            day+=timedelta(days=1)
        # 先收集窗口内所有已存储框架的节点 ID，再一次性查询场景，消除每节点一次 get_scene。
        scene_map=await self.store.get_scenes_by_framework_ids(
            node["id"] for _window_start,_window_end,_day_start,stored,nodes in windows if stored
            for node in nodes
        )
        spans=[]
        for window_start,window_end,day_start,stored,nodes in windows:
            cursor=window_start
            for node in nodes:
                node_start=day_start+timedelta(minutes=int(node["start_minute"]))
                node_end=day_start+timedelta(minutes=int(node["end_minute"]))
                if node_end<=window_start or node_start>=window_end:continue
                span_start=max(window_start,node_start); span_end=min(window_end,node_end)
                if cursor<span_start:
                    spans.append({"start":cursor,"end":span_start,"segment":{"kind":"leisure","summary":"自由活动","location":"家里"}})
                scene=scene_map.get(str(node["id"]),{}) if stored else {}
                completion={}
                if node_end<=end and node_end>start:
                    if scene and not int(scene.get("applied") or 0):completion={"framework_id":str(node["id"]),"deltas":self._state_deltas(node)}
                    elif not scene:completion={"framework_id":"","deltas":self._state_deltas(node)}
                spans.append({"start":span_start,"end":span_end,"segment":node,"completion":completion})
                cursor=max(cursor,span_end)
            if cursor<window_end:
                spans.append({"start":cursor,"end":window_end,"segment":{"kind":"leisure","summary":"自由活动","location":"家里"}})
        return spans

    async def context(self, now: datetime) -> dict[str, Any]:
        """返回今日框架、当前/下一节点和当前节点唯一的细化场景。"""
        nodes=await self.store.get_framework(now.strftime("%Y-%m-%d")); minute=now.hour*60+now.minute
        current,nxt=self.current_and_next(nodes,minute); scene=await self.store.get_scene(current["id"]) if current else {}
        return {"nodes":nodes,"current":current,"next":nxt,"scene":scene}
