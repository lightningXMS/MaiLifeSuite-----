"""Deterministic life-state simulation with narrative dreams."""
from __future__ import annotations

import math
import random
import time
from datetime import date, datetime
from typing import Any

# 心情基线回归参数：把心情温和拉回中性偏正的基线（约 40%/天），
# 防止纯积分器在健康日程下钉死 +1、在重负荷/少餐日程下阴跌钉死 -1。
MOOD_BASELINE = 0.15
MOOD_REGRESSION_PER_HOUR = 0.0175

# 睡眠均衡恢复：夜间睡眠向 REST_TARGET 指数收敛——缺觉时恢复多、精力充沛时恢复少，
# 与小时级清醒消耗相抵后每日净变化趋近 0，避免长期运行精力数周内缓慢耗尽贴地。
REST_TARGET = 90.0
REST_RATE = 0.35
# 累计睡眠达到 45 分钟后进入深睡；10 分钟 tick 也能逐级推进到深睡相位。
DEEP_SLEEP_AFTER_HOURS = 0.75

# 未配置模型时的梦境兜底池：同一调性的晨光/小路/水声/暖光意象，每套摘要配至少 3 条碎片，
# 随机取一套，避免每天的梦境逐字相同；不预言、不出现用户，mood 统一 calm 保持原有余韵强度。
DREAM_FALLBACKS=[
    {"summary":"只记得梦里走过一条被晨光照亮的小路，醒来时细节已经慢慢散掉了。",
     "fragments":["路边有很轻的风","远处的窗户亮着暖光","醒来前像是听见了水声","天快亮时梦就淡了"],"mood":"calm"},
    {"summary":"梦里一直在下很轻的雨，屋檐的水滴得很慢，醒来只记得青草被洗干净的味道。",
     "fragments":["青草味很干净","雨声盖住了别的声音","天亮前雨好像停了","地面还是湿的"],"mood":"calm"},
    {"summary":"梦见自己沿着河边走了很久，水面把灯光揉得很碎，后来就记不太清了。",
     "fragments":["河面的光很碎","有人在不远处轻声说话","走着走着天就亮了","鞋边沾了露水"],"mood":"calm"},
    {"summary":"梦里回到一间熟悉的旧房间，阳光斜斜地落在桌面上，醒来时心里很安静。",
     "fragments":["灰尘在光里浮着","窗帘被风吹动了一下","旧钟走得很慢","窗外有鸟叫"],"mood":"calm"},
    {"summary":"只记得梦里在等一班很慢的车，站台空空的，醒来时那种安静还留了一会儿。",
     "fragments":["站台的长椅是凉的","远处有广播的杂音","车始终没有来","天色介于早晚之间"],"mood":"calm"},
]


class LifeStateEngine:
    def __init__(self, store: Any, config: Any, llm: Any, logger: Any, bot_name: str = "麦麦") -> None:
        self.store=store; self.config=config; self.llm=llm; self.logger=logger
        self.bot_name=bot_name

    def update_config(self, config: Any) -> None:
        self.config=config

    def set_bot_name(self,name:str)->None:
        clean=str(name or "").strip()
        if clean:self.bot_name=clean

    @staticmethod
    def _clamp(value: float, low: float, high: float) -> float:
        return max(low,min(high,value))

    def _cycle_offset(self, now: datetime) -> int | None:
        """返回距周期起始日的天数（取模周期长度）；未启用或未配置时返回 None。"""
        cfg=self.config.state
        if not cfg.body_cycle_enabled: return None
        try: start=date.fromisoformat(cfg.body_cycle_start_date)
        except ValueError: return None
        return (now.date()-start).days % cfg.body_cycle_length_days

    def _body_cycle(self, now: datetime) -> str:
        cfg=self.config.state
        if not cfg.body_cycle_enabled: return "未启用"
        try:
            date.fromisoformat(cfg.body_cycle_start_date)
        except ValueError:
            return "已启用但未配置起始日期"
        offset=self._cycle_offset(now)
        if offset<cfg.body_cycle_period_days: return f"周期第{offset+1}天"
        return f"周期第{offset+1}天，非经期"

    def _in_period(self, now: datetime) -> bool:
        """是否处于经期；经期内只做轻度状态修正（精力消耗略增、心情轻抑）。"""
        offset=self._cycle_offset(now)
        return offset is not None and offset<self.config.state.body_cycle_period_days

    # 状态数值由确定性规则推进，LLM 不得直接改写核心数值。
    async def advance(self, now: datetime, segment: dict[str, Any] | None, scene: dict[str, Any] | None) -> dict[str, Any]:
        """按上次更新时间推进一次状态，并处理日程驱动的入睡或自然醒转换。"""
        state=await self.store.get_state(); runtime=await self.store.get_sleep_runtime()
        # 单次最多补算 72 小时；更长的离线区间由 timeline 按日程边界分段推进。
        now_ts=now.timestamp(); elapsed=max(0.0,min(72.0,(now_ts-float(state.get("last_updated_at",now_ts)))/3600))
        kind=str((segment or {}).get("kind") or "leisure")
        scheduled_sleep=kind in {"sleep","nap"}
        grace=float(runtime.get("awake_grace_until",0))>now_ts
        # 睡前协商期（sleep_defer_until 未到）不入睡：夜窗前对话未完时她还在回复。
        defer=float(runtime.get("sleep_defer_until",0))>now_ts
        old_phase=str(runtime.get("phase") or "awake")
        old_sleep_started=float(runtime.get("started_at",now_ts))
        # 睡眠段类型从 last_event 还原（睡眠段内逐次同步），午休不做梦。
        old_sleep_kind=str(runtime.get("last_event") or "").removeprefix("进入")
        in_period=self._in_period(now)
        effective_sleep=scheduled_sleep and not grace and not defer
        woke=False; sleep_duration=0.0
        # 睡眠恢复与清醒消耗分支互斥，避免同一时间段重复计算。
        if effective_sleep:
            # 相位按累计睡眠时长判定：入睡 45 分钟后进入深睡；离线补算时 started_at 可能
            # 远早于 now，同样一次到位。elapsed=0 的收尾 advance 也只做 minimal 更新，不降级。
            if old_phase not in {"falling_asleep","light_sleep","deep_sleep"}:
                runtime.update({"phase":"falling_asleep","started_at":min(now_ts,float(state.get("last_updated_at",now_ts)))})
                new_phase="falling_asleep"
            elif old_phase=="deep_sleep":
                new_phase="deep_sleep"
            else:
                slept_hours=max(0.0,(now_ts-float(runtime.get("started_at",now_ts)))/3600)
                new_phase="deep_sleep" if kind=="sleep" and slept_hours>=DEEP_SLEEP_AFTER_HOURS else "light_sleep"
            runtime["phase"]=new_phase
            # 睡眠段延续时也同步类型：nap 紧接 sleep 时升级为 sleep，避免整晚睡眠被误判为午休。
            runtime["last_event"]=f"进入{kind}"
            # 均衡恢复：夜间睡眠向 REST_TARGET 收敛，午休保持固定小幅恢复。
            energy=float(state.get("energy",70))
            recover=(REST_TARGET-energy)*(1.0-math.exp(-REST_RATE*elapsed)) if kind=="sleep" else 1.4*elapsed
            state["energy"]=self._clamp(energy+recover,0,100)
            state["hunger"]=self._clamp(float(state.get("hunger",20))+1.0*elapsed,0,100)
            state["mood_arousal"]=self._clamp(float(state.get("mood_arousal",0.6))-0.2*elapsed,0,1)
        else:
            if old_phase in {"falling_asleep","light_sleep","deep_sleep"}:
                sleep_duration=max(0,(now_ts-float(runtime.get("started_at",now_ts)))/3600)
                woke=True; runtime.update({"phase":"awake","started_at":now_ts,"last_event":"自然醒来"})
            elif grace:
                runtime["phase"]="woken"
            else:
                runtime["phase"]="awake"
            load={"work":2.1,"study":1.8,"travel":1.4,"leisure":1.0,"meal":0.5,"rest":0.3}.get(kind,1.1)
            if in_period: load*=1.1
            state["energy"]=self._clamp(float(state.get("energy",70))-load*elapsed,0,100)
            state["hunger"]=self._clamp(float(state.get("hunger",20))+5.0*elapsed,0,100)
            state["mood_arousal"]=self._clamp(float(state.get("mood_arousal",0.6))+0.05*elapsed,0,1)
        # 心情和健康只接受轻微、可解释的本地修正，不随机制造严重疾病。
        energy=float(state["energy"]); hunger=float(state["hunger"])
        mood=float(state.get("mood_valence",0))
        mood += (-0.03*elapsed if energy<30 else 0.01*elapsed if energy>70 else 0)
        mood += -0.04*elapsed if hunger>75 else 0
        if in_period: mood += -0.004*elapsed
        # 指数式基线回归：离线长时段补算时既不欠账也不会过冲穿越基线。
        # 饥饿/精力持续负面驱动时回归翻倍；心情已被钉死在 -1 附近时再 ×3，
        # 保证平衡点离开 -1 钳位边界（持续判断用当前心情值，无需额外持久化计时）。
        rate=MOOD_REGRESSION_PER_HOUR*2 if (hunger>75 or energy<30) else MOOD_REGRESSION_PER_HOUR
        if mood<=-0.8: rate*=3
        mood += (MOOD_BASELINE - mood) * (1.0 - math.exp(-rate * elapsed))
        state["mood_valence"]=self._clamp(mood,-1,1)
        if energy<20:
            state["health_status"]="tired"; state["health_note"]="精力很低，需要休息"
        elif hunger>88:
            state["health_status"]="mild_discomfort"; state["health_note"]="有些饿，胃里空空的"
        else:
            state["health_status"]="normal"; state["health_note"]="状态正常"
        state["sleep_phase"]=runtime["phase"]
        state["current_location"]=str((segment or {}).get("location") or state.get("current_location") or "家里")
        state["current_activity"]=str((scene or {}).get("scene") or (segment or {}).get("summary") or "自由活动")
        state["body_cycle"]=self._body_cycle(now)
        state["last_updated_at"]=now_ts
        runtime["last_event"]=runtime.get("last_event","")
        await self.store.save_state(state); await self.store.save_sleep_runtime(runtime)
        # 做梦双门：先查可选的近 7 天上限（默认 0 不限，被拦当晚不掷骰），再掷概率。
        # 概率门放在 advance 调用点而非 generate_dream 内部，保住其「一次有效夜睡至多一个梦」契约。
        if (self.config.memory.enabled and woke and sleep_duration>=3 and old_sleep_kind!="nap"
                and not await self._dream_week_full(now_ts) and self._dream_roll()):
            await self.generate_dream(state, old_sleep_started, sleep_duration,now)
        return {"state":state,"woke":woke,"sleep_duration":sleep_duration}

    async def advance_timeline(self,now:datetime,timeline:list[dict[str,Any]],
                               final_segment:dict[str,Any]|None,final_scene:dict[str,Any]|None)->dict[str,Any]:
        """按顺序推进离线日程，在节点边界完成睡眠转换和一次性状态增量。"""
        if timeline:
            first_start=timeline[0]["start"]
            state=await self.store.get_state()
            if float(state.get("last_updated_at") or 0)<first_start.timestamp():
                # 超过离线补算上限的旧时间直接截断，防止一次启动循环数百天。
                state["last_updated_at"]=first_start.timestamp(); await self.store.save_state(state)
                runtime=await self.store.get_sleep_runtime()
                if float(runtime.get("started_at") or 0)<first_start.timestamp():
                    runtime["started_at"]=first_start.timestamp(); await self.store.save_sleep_runtime(runtime)
        for span in timeline:
            segment=span.get("segment") if isinstance(span.get("segment"),dict) else None
            await self.advance(span["start"],segment,None)
            await self.advance(span["end"],segment,None)
            completion=span.get("completion") if isinstance(span.get("completion"),dict) else {}
            deltas=completion.get("deltas") if isinstance(completion.get("deltas"),dict) else {}
            if deltas:
                await self.apply_deltas(deltas,updated_at=span["end"].timestamp())
                framework_id=str(completion.get("framework_id") or "")
                if framework_id:await self.store.mark_scene_applied(framework_id)
            # 离线补算逐小时快照：整点桶 INSERT OR IGNORE 幂等，长离线中间小时也有轨迹。
            await self.store.save_state_snapshot(span["end"].timestamp(),await self.store.get_state())
        return await self.advance(now,final_segment,final_scene)

    # 场景结束时一次性应用增量，并统一限制在合法范围内。
    async def apply_deltas(self, deltas: dict[str, Any], updated_at: float=0) -> dict[str, Any]:
        state=await self.store.get_state()
        for key,low,high in (("energy",0,100),("hunger",0,100),("mood_valence",-1,1),("mood_arousal",0,1)):
            try: delta=float(deltas.get(key,0))
            except (TypeError,ValueError): delta=0
            state[key]=self._clamp(float(state.get(key,0))+delta,low,high)
        state["last_updated_at"]=updated_at or time.time(); await self.store.save_state(state); return state

    # 做梦概率门：每个合格夜晚醒来独立掷一次；两端点短路，不消耗全局随机流
    # （保住 seeded 测试与 generate_dream 内 random.choice 的流位置）。
    # 用 `<` 而非 `<=`：避免 p=0 时 random.random()==0.0 的 2^-53 假阳性。
    def _dream_roll(self) -> bool:
        p=float(self.config.memory.dream_probability)
        if p<=0:return False
        if p>=1:return True
        return random.random()<p

    # 近 7 天做梦上限门：created_at 口径 [now_ts-7*86400, now_ts) 内已落库梦境数达到
    # dream_weekly_max 则当晚不做；默认 0=不限制，短路不查库（dreams 表无 created_at 索引）。
    async def _dream_week_full(self, now_ts: float) -> bool:
        limit=int(self.config.memory.dream_weekly_max)
        if limit<=0:return False
        dreams=await self.store.dreams_between(now_ts-7*86400,now_ts)
        return len(dreams)>=limit

    # 梦境只负责叙事和轻微余韵，不制造预言或重大健康事件。
    async def generate_dream(self, state: dict[str, Any], sleep_started_at: float, hours: float,
                             woke_at: datetime|None=None) -> None:
        """为一次有效夜间睡眠生成至多一个梦境，并创建有限期分享契机。"""
        count=int(self.config.memory.dream_fragment_count) if self.config.memory.dream_fragments_enabled else 0
        variant=random.choice(DREAM_FALLBACKS)
        fallback={"summary":variant["summary"],
                  "fragments":variant["fragments"][:count],"mood":variant["mood"]}
        result=fallback
        if self.llm.task_available("dream"):
            # 摘取入睡前白天里的群聊公开话题，作为梦境的模糊参考素材。
            group_context=""
            try:
                observations=await self.store.recent_group_observations(sleep_started_at,8)
                topics=[str(item.get("topic") or "").strip() for item in observations if str(item.get("topic") or "").strip()]
                if topics:group_context="白天在群里看到过这些话题："+ "、".join(topics[:8]) + "。这些可以作为梦境的模糊参考，但不要逐字复述或暴露群友身份。"
            except Exception:pass
            prompt=(f"{self.bot_name}刚结束约{hours:.1f}小时睡眠。当前心情值{state.get('mood_valence',0):.2f}，"
                    f"最近生活场景是{state.get('current_activity','普通日常')}。"
                    f"{group_context}"
                    "生成克制自然的醒后梦境，"
                    f"返回JSON：summary为40到120字摘要，fragments为最多{count}个短片段，mood为calm/warm/uneasy之一。"
                    "不要解释，不要写成预言，不要强行出现用户，也不要制造重大健康事件。")
            raw=await self.llm.generate_json(
                prompt,"你只输出合法JSON格式的梦境记录。",fallback,max_tokens=0,
                task_kind="dream",request_type="dream",
            )
            if isinstance(raw,dict):result=raw
        text=str(result.get("summary") or fallback["summary"])[:500]
        raw_fragments=result.get("fragments") if isinstance(result.get("fragments"),list) else []
        fragments=[str(item).strip()[:300] for item in raw_fragments if str(item).strip()][:count]
        mood=str(result.get("mood") or "calm"); mood_delta=0.03 if mood=="warm" else -0.02 if mood=="uneasy" else 0.0
        now=woke_at.timestamp() if woke_at else time.time()
        dream_id=await self.store.add_dream(text,mood_delta,0.5,sleep_started_at,fragments,created_at=now)
        # 契机器锚定到入睡时刻 +12h：正常睡眠（<12h）下即 sleep_started_at+12h；
        # 超长睡眠或传入过去时间戳时不生成出生即已过期的契机，顺延到创建时刻 +12h。
        expires=sleep_started_at+12*3600
        if expires<=now: expires=now+12*3600
        await self.store.add_opportunity({
            "id":f"dream-{dream_id}","framework_id":f"dream:{dream_id}","topic":"昨晚醒来后还记得一点梦",
            "motive":"梦境留下了短暂余韵，可能想向熟悉的网友自然提起",
            "weight":0.46,"privacy":"normal","expires_at":expires,
        })
        await self.apply_deltas({"mood_valence":mood_delta,"energy":0.5},updated_at=now)

    # 被用户叫醒后设置清醒宽限，避免每条消息反复判醒。
    async def mark_woken(self, now: datetime, reason: str) -> None:
        runtime=await self.store.get_sleep_runtime(); state=await self.store.get_state()
        runtime["phase"]="woken"; runtime["awake_grace_until"]=now.timestamp()+self.config.rest_gate.awake_grace_minutes*60
        runtime["woken_count"]=int(runtime.get("woken_count",0))+1; runtime["last_event"]=reason
        state["sleep_phase"]="woken"; state["energy"]=self._clamp(float(state.get("energy",70))-3,0,100); state["last_updated_at"]=now.timestamp()
        await self.store.save_sleep_runtime(runtime); await self.store.save_state(state)

    # 叫醒宽限到期后由 BedtimeManager 调用：从 woken 重新入睡，
    # 入睡相位从头计时（45 分钟后才进深睡），与用户配置的 awake_grace_minutes 对齐。
    async def mark_resleep(self, now: datetime) -> None:
        runtime=await self.store.get_sleep_runtime(); state=await self.store.get_state()
        if str(runtime.get("phase") or "")!="woken":return
        now_ts=now.timestamp()
        runtime.update({"phase":"falling_asleep","started_at":now_ts,
                        "awake_grace_until":0,"sleep_defer_until":0,"last_event":"叫醒后重新入睡"})
        state["sleep_phase"]="falling_asleep"; state["last_updated_at"]=now_ts
        await self.store.save_sleep_runtime(runtime); await self.store.save_state(state)



