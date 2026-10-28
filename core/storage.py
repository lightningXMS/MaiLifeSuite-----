"""Transactional SQLite storage for Mai_life."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

SCHEMA_VERSION = 14
# 长期运行记录（互动、主动事件、契机、转述候选/事件、跳过统计）的保留天数。
# 这些表没有过期字段驱动的外部清理，超期行必须在 cleanup_runtime_records 中删除，
# 否则只会无限增长；pending 与在途行永远保留，避免打断正在结算的引用。
RECORD_RETENTION_DAYS = 90
# 心情事件规则：kind -> (每次幅度, 每日上限次数)。让心情对真实互动有响应，
# 上限保证重度聊天日也不会把心情顶满。
MOOD_EVENT_RULES = {
    "passive_reply": (0.02, 3),
    "proactive_reply": (0.03, 2),
    "creation_archived": (0.05, 1),
}
# 心情事件与状态快照的保留天数：这是动力学分析的数据源，比运行记录保留更久。
STATE_HISTORY_RETENTION_DAYS = 180
# 活动播报 pending 占位视为崩溃残留的时长：正常发送在秒级完成，超时后允许重新占位。
_ANNOUNCE_CLAIM_STALE_SECONDS = 600


class LifeStore:
    def __init__(self, data_dir: str, *, journal_mode: str = "memory",
                 logger: Any = None) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "mai_life.db"
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = asyncio.Lock()
        self._logger = logger
        # 测试用临时库默认 memory（回滚日志放内存，不落盘，避免 -journal 文件残留）；
        # 生产 data 库由 plugin.py 显式传入 delete，保留崩溃回滚保护。
        self._journal_mode = str(journal_mode or "memory").strip().casefold() or "memory"

    def _log_error(self, message: str) -> None:
        """库级告警（如整库替换）必须留痕，避免用户数据被静默降级成备份文件。"""
        if self._logger is None:
            return
        try:
            self._logger.error(f"[MaiLife] {message}")
        except Exception:
            pass

    # 初始化可重复调用；同一 Runner 内不会重复打开 SQLite 连接。
    async def initialize(self) -> None:
        async with self._lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            if self._conn is None:
                self._open_checked()
            try:
                self._create_schema()
            except sqlite3.DatabaseError as exc:
                # 结构无法兼容时保留原库，再用当前 Schema 建立新库；必须留痕。
                self._log_error(f"数据库结构不兼容，将保留原库备份并重建（原因: {exc}）")
                self._replace_incompatible_database()
                self._create_schema()

    def _replace_incompatible_database(self) -> None:
        if self._conn is not None:
            self._conn.close(); self._conn=None
        if self.path.exists():
            stamp=int(time.time()); backup=self.path.with_suffix(f".incompatible.{stamp}.db")
            counter=1
            while backup.exists():
                backup=self.path.with_suffix(f".incompatible.{stamp}.{counter}.db"); counter+=1
            shutil.move(str(self.path),str(backup))
            self._log_error(f"原数据库已备份为 {backup.name}，请检查后决定是否迁回数据")
        self._conn=self._connect()

    def _connect(self) -> sqlite3.Connection:
        """打开数据库连接并设置行工厂与忙等待超时，写入冲突时最多等待 5 秒。"""
        conn=sqlite3.connect(self.path,check_same_thread=False)
        try:
            conn.row_factory=sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(f"PRAGMA journal_mode={self._journal_mode}")
            return conn
        except Exception:
            # journal_mode 等 PRAGMA 可能在损坏文件上抛错；必须关闭连接，否则文件句柄泄漏。
            conn.close()
            raise

    # quick_check 失败时先保留损坏文件，再创建全新数据库。
    def _open_checked(self) -> None:
        """打开 SQLite 并执行完整性检查；损坏文件保留副本后再建立空库。"""
        try:
            conn=self._connect()
            try:
                result=conn.execute("PRAGMA quick_check").fetchone()
                if result and str(result[0]).lower() != "ok":
                    raise sqlite3.DatabaseError(str(result[0]))
            except Exception:
                conn.close()
                raise
            self._conn = conn
        except sqlite3.DatabaseError:
            # 损坏时先释放当前连接，保留损坏文件副本后再建立空库。
            if self._conn is not None:
                try:self._conn.close()
                except Exception:pass
            self._conn = None
            if self.path.exists():
                stamp=int(time.time()); backup = self.path.with_suffix(f".corrupt.{stamp}.db")
                counter=1
                while backup.exists():
                    backup=self.path.with_suffix(f".corrupt.{stamp}.{counter}.db"); counter+=1
                shutil.move(str(self.path), str(backup))
            self._conn = self._connect()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("LifeStore is not initialized")
        return self._conn

    # 所有复合写入使用 IMMEDIATE 事务，防止状态只更新一半。
    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # Schema 使用显式版本号，为后续迁移保留稳定入口。
    def _create_schema(self) -> None:
        """幂等创建当前 Schema，并在建表完成后执行旧版本的增量迁移。"""
        version = 0
        existing = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        if existing:
            row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            try:
                version = int(row[0]) if row else 0
            except (TypeError, ValueError) as exc:
                raise sqlite3.DatabaseError("schema_version 非法") from exc
            if version > SCHEMA_VERSION:
                self.conn.close()
                stamp=int(time.time()); backup = self.path.with_suffix(f".future-v{version}.{stamp}.db")
                counter=1
                while backup.exists():
                    backup=self.path.with_suffix(f".future-v{version}.{stamp}.{counter}.db"); counter+=1
                shutil.move(str(self.path), str(backup))
                self._conn = self._connect()
        # 表按生活时间线、用户消息、长期记忆、社交与联网运行态分组；建表本身可重复执行。
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            -- 全局生活时间线、睡眠、梦境、日程和主动契机。
            CREATE TABLE IF NOT EXISTS global_state(
              id INTEGER PRIMARY KEY CHECK(id=1), energy REAL NOT NULL, hunger REAL NOT NULL,
              mood_valence REAL NOT NULL, mood_arousal REAL NOT NULL,
              health_status TEXT NOT NULL, health_note TEXT NOT NULL,
              sleep_phase TEXT NOT NULL, current_location TEXT NOT NULL,
              current_activity TEXT NOT NULL, body_cycle TEXT NOT NULL,
              last_updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sleep_runtime(
              id INTEGER PRIMARY KEY CHECK(id=1), phase TEXT NOT NULL,
              started_at REAL NOT NULL, awake_grace_until REAL NOT NULL DEFAULT 0,
              woken_count INTEGER NOT NULL DEFAULT 0, last_event TEXT NOT NULL DEFAULT '',
              sleep_defer_until REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS dreams(
              id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,
              content TEXT NOT NULL, mood_delta REAL NOT NULL DEFAULT 0,
              energy_delta REAL NOT NULL DEFAULT 0, sleep_started_at REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS dream_fragments(
              id INTEGER PRIMARY KEY AUTOINCREMENT, dream_id INTEGER NOT NULL,
              position INTEGER NOT NULL, content TEXT NOT NULL,
              UNIQUE(dream_id,position)
            );
            CREATE TABLE IF NOT EXISTS daily_framework(
              id TEXT PRIMARY KEY, day TEXT NOT NULL, start_minute INTEGER NOT NULL,
              end_minute INTEGER NOT NULL, kind TEXT NOT NULL, summary TEXT NOT NULL,
              location TEXT NOT NULL, energy_load REAL NOT NULL, shareability REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_framework_day ON daily_framework(day,start_minute);
            CREATE TABLE IF NOT EXISTS detailed_scenes(
              framework_id TEXT PRIMARY KEY, scene TEXT NOT NULL,
              state_deltas TEXT NOT NULL, created_at REAL NOT NULL,
              applied INTEGER NOT NULL DEFAULT 0,
              FOREIGN KEY(framework_id) REFERENCES daily_framework(id)
            );
            CREATE TABLE IF NOT EXISTS proactive_opportunities(
              id TEXT PRIMARY KEY, framework_id TEXT NOT NULL, topic TEXT NOT NULL,
              motive TEXT NOT NULL, weight REAL NOT NULL, privacy TEXT NOT NULL,
              target_user_id TEXT NOT NULL DEFAULT '',
              expires_at REAL NOT NULL, consumed_by TEXT NOT NULL DEFAULT '',
              consumed_at REAL NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_opportunity_active ON proactive_opportunities(expires_at,consumed_at);
            -- 活动播报：按 目标类型+目标ID+活动键 去重与限流
            -- （status: pending/sent/archived/failed；archived 不参与去重但计入当日限额）。
            CREATE TABLE IF NOT EXISTS announce_log(
              target_type TEXT NOT NULL, target_id TEXT NOT NULL, activity_key TEXT NOT NULL,
              status TEXT NOT NULL, day TEXT NOT NULL DEFAULT '',
              sent_at REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
              next_retry_at REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL,
              PRIMARY KEY(target_type, target_id, activity_key)
            );
            CREATE INDEX IF NOT EXISTS idx_announce_day ON announce_log(day,target_type,target_id,status);
            -- 活动播报每日成功计数：同一活动键重复播报（deduplicate=false）也逐次累加，
            -- 与 announce_log 行数解耦，保证限额按"发送事件"而非"活动数"统计。
            CREATE TABLE IF NOT EXISTS announce_daily(
              day TEXT NOT NULL, target_type TEXT NOT NULL, target_id TEXT NOT NULL,
              count INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(day, target_type, target_id)
            );
            -- 每日日程生成状态：重试计数/下次重试时间跨重启持久化
            -- （status: retrying/success/exhausted；has_temp=1 表示当天框架是临时兜底，不得阻断重试）。
            CREATE TABLE IF NOT EXISTS schedule_generation(
              day TEXT PRIMARY KEY, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
              next_retry_at REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
              has_temp INTEGER NOT NULL DEFAULT 0, fallback_source TEXT NOT NULL DEFAULT '',
              updated_at REAL NOT NULL
            );
            -- 成功生成的日程归档：独立于当天框架（replace_framework 不触碰本表），
            -- 按 archived_at 过期清理；随机兜底只使用未过期归档。
            CREATE TABLE IF NOT EXISTS schedule_archives(
              archive_id TEXT PRIMARY KEY, source_day TEXT NOT NULL, archived_at REAL NOT NULL,
              day_type TEXT NOT NULL, nodes_json TEXT NOT NULL,
              generator TEXT NOT NULL DEFAULT 'llm', schema_version INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_archives_archived_at ON schedule_archives(archived_at);
            -- 用户关系、互动、主动发送、休息积压和环境缓存。
            CREATE TABLE IF NOT EXISTS users(
              user_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL, proactive_enabled INTEGER NOT NULL,
              display_name TEXT NOT NULL, temperature REAL NOT NULL,
              role TEXT NOT NULL DEFAULT 'friend', daily_proactive_max INTEGER NOT NULL DEFAULT 1,
              quiet_start TEXT NOT NULL, quiet_end TEXT NOT NULL,
              stream_id TEXT NOT NULL DEFAULT '', last_user_message_at REAL NOT NULL DEFAULT 0,
              last_proactive_at REAL NOT NULL DEFAULT 0,
              proactive_day TEXT NOT NULL DEFAULT '', proactive_count INTEGER NOT NULL DEFAULT 0,
              last_relation_day TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS interaction_events(
              id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
              created_at REAL NOT NULL, kind TEXT NOT NULL, hour INTEGER NOT NULL,
              content_summary TEXT NOT NULL DEFAULT '', source_message_id TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_interaction_user_time ON interaction_events(user_id,created_at);
            CREATE TABLE IF NOT EXISTS proactive_events(
              id TEXT PRIMARY KEY, user_id TEXT NOT NULL, opportunity_id TEXT NOT NULL,
              stream_id TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
              expires_at REAL NOT NULL, sent_at REAL NOT NULL DEFAULT 0,
              host_task_id TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_proactive_pending ON proactive_events(stream_id,status,expires_at);
            -- 主动候选跳过原因统计：按用户、原因、自然日聚合，供 /麦麦管理 主动 诊断。
            CREATE TABLE IF NOT EXISTS proactive_skip_stats(
              user_id TEXT NOT NULL, reason TEXT NOT NULL, day TEXT NOT NULL,
              count INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(user_id,reason,day)
            );
            -- 心情事件流与逐小时状态快照：互动加分的账本和长期动力学分析的数据源。
            CREATE TABLE IF NOT EXISTS mood_events(
              id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,
              day TEXT NOT NULL, kind TEXT NOT NULL, delta REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_mood_events_day ON mood_events(day,kind);
            CREATE TABLE IF NOT EXISTS state_snapshots(
              ts REAL PRIMARY KEY, energy REAL NOT NULL, hunger REAL NOT NULL,
              mood_valence REAL NOT NULL, mood_arousal REAL NOT NULL,
              sleep_phase TEXT NOT NULL, current_activity TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS rest_backlogs(
              id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
              created_at REAL NOT NULL, summary TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0,
              source_message_id TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS weather_cache(
              id INTEGER PRIMARY KEY CHECK(id=1), fetched_at REAL NOT NULL,
              location_name TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL,
              temperature REAL, weather_code INTEGER, description TEXT NOT NULL,
              raw_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wake_candidates(
              session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, message_id TEXT NOT NULL,
              reason TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversation_continuity(
              user_id TEXT PRIMARY KEY, intent TEXT NOT NULL DEFAULT '',
              unresolved_topics TEXT NOT NULL DEFAULT '[]', updated_at REAL NOT NULL DEFAULT 0
            );
            -- 模型统计、回复防重和撤回关联使用短期运行记录。
            CREATE TABLE IF NOT EXISTS llm_usage_events(
              id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,
              source TEXT NOT NULL, task_name TEXT NOT NULL, model_name TEXT NOT NULL,
              request_type TEXT NOT NULL, prompt_tokens INTEGER NOT NULL DEFAULT 0,
              completion_tokens INTEGER NOT NULL DEFAULT 0, total_tokens INTEGER NOT NULL DEFAULT 0,
              latency_ms REAL NOT NULL DEFAULT 0, success INTEGER NOT NULL, error_summary TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_llm_usage_time ON llm_usage_events(created_at,source,task_name);
            CREATE TABLE IF NOT EXISTS reply_turns(
              session_id TEXT NOT NULL, anchor_message_id TEXT NOT NULL, status TEXT NOT NULL,
              created_at REAL NOT NULL, expires_at REAL NOT NULL,
              PRIMARY KEY(session_id,anchor_message_id)
            );
            CREATE TABLE IF NOT EXISTS message_turn_sources(
              session_id TEXT NOT NULL, turn_anchor TEXT NOT NULL, source_message_id TEXT NOT NULL,
              user_id TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, expires_at REAL NOT NULL,
              PRIMARY KEY(session_id,turn_anchor,source_message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_message_turn_source
              ON message_turn_sources(session_id,source_message_id,expires_at);
            CREATE TABLE IF NOT EXISTS recall_events(
              session_id TEXT NOT NULL, recalled_message_id TEXT NOT NULL,
              user_id TEXT NOT NULL DEFAULT '', operator_id TEXT NOT NULL DEFAULT '',
              group_id TEXT NOT NULL DEFAULT '', notice_type TEXT NOT NULL,
              source_adapter TEXT NOT NULL DEFAULT 'unknown', summary TEXT NOT NULL DEFAULT '',
              summary_expires_at REAL NOT NULL DEFAULT 0,
              media_types TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL, expires_at REAL NOT NULL,
              PRIMARY KEY(session_id,recalled_message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_recall_event_expiry
              ON recall_events(session_id,expires_at);
            -- 日记、重要日期和联网见闻只保存结构化摘要，不复制完整聊天历史。
            CREATE TABLE IF NOT EXISTS diary_entries(
              day TEXT PRIMARY KEY, created_at REAL NOT NULL, title TEXT NOT NULL,
              content TEXT NOT NULL, mood_summary TEXT NOT NULL,
              privacy TEXT NOT NULL DEFAULT 'private', source_digest TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS important_dates(
              id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
              event_name TEXT NOT NULL, event_date TEXT NOT NULL,
              recurrence TEXT NOT NULL DEFAULT 'none', source TEXT NOT NULL DEFAULT 'conversation',
              created_at REAL NOT NULL, updated_at REAL NOT NULL,
              source_message_id TEXT NOT NULL DEFAULT ''
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_important_date_unique
              ON important_dates(user_id,event_name,event_date,recurrence);
            CREATE INDEX IF NOT EXISTS idx_important_date_user ON important_dates(user_id,event_date);
            CREATE TABLE IF NOT EXISTS date_candidates(
              id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
              event_name TEXT NOT NULL, date_text TEXT NOT NULL,
              suggested_date TEXT NOT NULL DEFAULT '', confidence REAL NOT NULL DEFAULT 0,
              source_summary TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending', source_message_id TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_date_candidate_user ON date_candidates(user_id,status,created_at);
            CREATE TABLE IF NOT EXISTS date_trigger_events(
              date_id INTEGER NOT NULL, occurrence_date TEXT NOT NULL,
              lead_days INTEGER NOT NULL, created_at REAL NOT NULL,
              PRIMARY KEY(date_id,occurrence_date,lead_days)
            );
            CREATE TABLE IF NOT EXISTS memory_runtime(
              id INTEGER PRIMARY KEY CHECK(id=1), last_diary_day TEXT NOT NULL DEFAULT '',
              last_cleanup_at REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS news_items(
              id TEXT PRIMARY KEY, source_id TEXT NOT NULL, title TEXT NOT NULL,
              url TEXT NOT NULL, summary TEXT NOT NULL, content TEXT NOT NULL DEFAULT '',
              published_at REAL NOT NULL DEFAULT 0, fetched_at REAL NOT NULL,
              content_hash TEXT NOT NULL, relevance_score REAL NOT NULL DEFAULT 0,
              relevance_reason TEXT NOT NULL DEFAULT '', associated_at REAL NOT NULL DEFAULT 0,
              opportunity_id TEXT NOT NULL DEFAULT '', expires_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_news_pending ON news_items(associated_at,fetched_at);
            CREATE TABLE IF NOT EXISTS exploration_notes(
              id TEXT PRIMARY KEY, topic TEXT NOT NULL, query TEXT NOT NULL,
              summary TEXT NOT NULL, source_urls TEXT NOT NULL DEFAULT '[]',
              created_at REAL NOT NULL, relevance_score REAL NOT NULL DEFAULT 0,
              relevance_reason TEXT NOT NULL DEFAULT '', opportunity_id TEXT NOT NULL DEFAULT '',
              expires_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_exploration_time ON exploration_notes(created_at);
            CREATE TABLE IF NOT EXISTS information_source_runtime(
              source_key TEXT PRIMARY KEY, last_attempt_at REAL NOT NULL DEFAULT 0,
              last_success_at REAL NOT NULL DEFAULT 0, failure_count INTEGER NOT NULL DEFAULT 0,
              next_retry_at REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
              etag TEXT NOT NULL DEFAULT '', last_modified TEXT NOT NULL DEFAULT ''
            );
            -- 群聊观察和跨会话转述使用 QQ 号定位，名称字段仅用于展示。
            CREATE TABLE IF NOT EXISTS group_observations(
              id TEXT PRIMARY KEY, group_id TEXT NOT NULL, group_alias TEXT NOT NULL,
              topic TEXT NOT NULL, summary TEXT NOT NULL, interest_score REAL NOT NULL DEFAULT 0,
              source_adapter TEXT NOT NULL DEFAULT 'unknown', created_at REAL NOT NULL,
              expires_at REAL NOT NULL, source_message_ids TEXT NOT NULL DEFAULT '[]'
            );
            CREATE INDEX IF NOT EXISTS idx_group_observation_time
              ON group_observations(group_id,created_at);
            CREATE TABLE IF NOT EXISTS group_directory(
              group_id TEXT PRIMARY KEY, group_name TEXT NOT NULL DEFAULT '',
              stream_id TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS group_user_activity(
              group_id TEXT NOT NULL, user_id TEXT NOT NULL,
              display_name TEXT NOT NULL DEFAULT '', last_active_at REAL NOT NULL,
              source_message_id TEXT NOT NULL DEFAULT '',
              PRIMARY KEY(group_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS relay_candidates(
              id TEXT PRIMARY KEY, kind TEXT NOT NULL,
              opportunity_id TEXT NOT NULL DEFAULT '',
              source_observation_id TEXT NOT NULL DEFAULT '', source_group_id TEXT NOT NULL DEFAULT '',
              target_group_id TEXT NOT NULL DEFAULT '', target_user_id TEXT NOT NULL DEFAULT '',
              target_stream_id TEXT NOT NULL, summary TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
              mention_user_id TEXT NOT NULL DEFAULT '', mention_name TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
              sent_at REAL NOT NULL DEFAULT 0, host_task_id TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_relay_pending
              ON relay_candidates(target_stream_id,status,expires_at);
            CREATE INDEX IF NOT EXISTS idx_relay_user_time
              ON relay_candidates(target_user_id,kind,created_at);
            CREATE TABLE IF NOT EXISTS relay_events(
              id INTEGER PRIMARY KEY AUTOINCREMENT, relay_id TEXT NOT NULL,
              status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
            );
            -- 书柜采用文档、不可变修订和运行记录三层结构，便于失败恢复与权限过滤。
            CREATE TABLE IF NOT EXISTS bookshelf_documents(
              id TEXT PRIMARY KEY, doc_type TEXT NOT NULL, work_type TEXT NOT NULL DEFAULT '',
              title TEXT NOT NULL, privacy TEXT NOT NULL, status TEXT NOT NULL,
              current_revision INTEGER NOT NULL DEFAULT 0,
              source_kind TEXT NOT NULL DEFAULT '', source_ref TEXT NOT NULL DEFAULT '',
              summary TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
              updated_at REAL NOT NULL, archived_at REAL NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_bookshelf_visibility
              ON bookshelf_documents(doc_type,privacy,status,updated_at);
            CREATE TABLE IF NOT EXISTS bookshelf_revisions(
              id INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL,
              revision_number INTEGER NOT NULL, stage TEXT NOT NULL,
              content TEXT NOT NULL, model_task TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
              UNIQUE(document_id,revision_number)
            );
            CREATE TABLE IF NOT EXISTS creation_inspirations(
              id TEXT PRIMARY KEY, source_kind TEXT NOT NULL, source_ref TEXT NOT NULL,
              prompt_digest TEXT NOT NULL, privacy_ceiling TEXT NOT NULL,
              score REAL NOT NULL DEFAULT 0, status TEXT NOT NULL,
              created_at REAL NOT NULL, expires_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_creation_inspiration_pending
              ON creation_inspirations(status,score,created_at);
            CREATE TABLE IF NOT EXISTS creation_runs(
              id TEXT PRIMARY KEY, inspiration_id TEXT NOT NULL, document_id TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL, started_at REAL NOT NULL, completed_at REAL NOT NULL DEFAULT 0,
              error_summary TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS reading_notes(
              id TEXT PRIMARY KEY, source_api TEXT NOT NULL, external_id TEXT NOT NULL,
              title TEXT NOT NULL, summary TEXT NOT NULL, annotation TEXT NOT NULL,
              source_digest TEXT NOT NULL, created_at REAL NOT NULL,
              UNIQUE(source_api,external_id)
            );
            -- 搜索 Key 只保存不可逆指纹和健康状态，调用事件与模型 Token 分开统计。
            CREATE TABLE IF NOT EXISTS search_key_runtime(
              provider_id TEXT NOT NULL, key_fingerprint TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'healthy', cooldown_until REAL NOT NULL DEFAULT 0,
              failure_count INTEGER NOT NULL DEFAULT 0, last_error_class TEXT NOT NULL DEFAULT '',
              last_used_at REAL NOT NULL DEFAULT 0, last_success_at REAL NOT NULL DEFAULT 0,
              PRIMARY KEY(provider_id,key_fingerprint)
            );
            CREATE TABLE IF NOT EXISTS search_api_events(
              id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,
              operation TEXT NOT NULL, provider_id TEXT NOT NULL, provider_type TEXT NOT NULL,
              key_fingerprint TEXT NOT NULL DEFAULT '', success INTEGER NOT NULL,
              status_code INTEGER NOT NULL DEFAULT 0, latency_ms REAL NOT NULL DEFAULT 0,
              result_count INTEGER NOT NULL DEFAULT 0, error_class TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_search_api_event_time
              ON search_api_events(created_at,provider_type,success);
            -- 逻辑搜索历史：一次降级链共享一条记录，保存查询词与结果摘要，供本地审计。
            CREATE TABLE IF NOT EXISTS search_history(
              id TEXT PRIMARY KEY, created_at REAL NOT NULL,
              operation TEXT NOT NULL, query TEXT NOT NULL,
              provider_type TEXT NOT NULL DEFAULT '', success INTEGER NOT NULL,
              result_count INTEGER NOT NULL DEFAULT 0, error_class TEXT NOT NULL DEFAULT '',
              duration_ms REAL NOT NULL DEFAULT 0, results TEXT NOT NULL DEFAULT '[]',
              expires_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_search_history_time
              ON search_history(created_at,operation);
            """
        )
        # v1 数据库可能已经存在 users 表；只补列，不重建用户数据。
        self._ensure_column("users", "role", "TEXT NOT NULL DEFAULT 'friend'")
        self._ensure_column("users", "daily_proactive_max", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column("proactive_opportunities", "target_user_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("relay_candidates", "opportunity_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("proactive_events", "host_task_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("relay_candidates", "host_task_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("interaction_events", "source_message_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("rest_backlogs", "source_message_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("important_dates", "source_message_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("date_candidates", "source_message_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("group_observations", "source_message_ids", "TEXT NOT NULL DEFAULT '[]'")
        self._ensure_column("recall_events", "summary_expires_at", "REAL NOT NULL DEFAULT 0")
        self._ensure_column("group_user_activity", "source_message_id", "TEXT NOT NULL DEFAULT ''")
        # 状态表同样可能缺列（旧库没有 mood_arousal/body_cycle 等）；缺列时末尾的
        # 种子 INSERT 会失败并被误判为库不兼容而整库重置，必须先补列。
        self._ensure_column("global_state", "mood_arousal", "REAL NOT NULL DEFAULT 0.65")
        self._ensure_column("global_state", "body_cycle", "TEXT NOT NULL DEFAULT '未启用'")
        self._ensure_column("sleep_runtime", "awake_grace_until", "REAL NOT NULL DEFAULT 0")
        self._ensure_column("sleep_runtime", "woken_count", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("sleep_runtime", "last_event", "TEXT NOT NULL DEFAULT ''")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_proactive_host_task ON proactive_events(host_task_id,status)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_relay_host_task ON relay_candidates(host_task_id,status)"
        )
        # v1.5 首次升级时把已有私人日记纳入书柜，不复制聊天记录，也不改变原日记表。
        self.conn.execute(
            """INSERT OR IGNORE INTO bookshelf_documents
            (id,doc_type,work_type,title,privacy,status,current_revision,source_kind,source_ref,summary,
             created_at,updated_at,archived_at)
            SELECT 'diary:'||day,'diary','',title,'private','archived',1,'diary',day,mood_summary,
                   created_at,created_at,created_at FROM diary_entries"""
        )
        self.conn.execute(
            """INSERT OR IGNORE INTO bookshelf_revisions
            (document_id,revision_number,stage,content,model_task,created_at)
            SELECT 'diary:'||day,1,'diary',content,'diary',created_at FROM diary_entries"""
        )
        # 上面的 bookshelf 迁移是 DML，会在 legacy 模式下隐式开启事务；
        # 后续迁移统一使用 BEGIN IMMEDIATE，必须先提交，否则嵌套 BEGIN 会抛
        # "cannot start a transaction within a transaction"，触发 initialize() 误判
        # 结构不兼容并把整个用户数据库替换为空库（v9/v10 -> v11 升级路径）。
        self.conn.commit()
        if version < 9:
            self._migrate_to_v9()
        if version < 10:
            self._migrate_to_v10()
        if version < 11:
            self._migrate_to_v11()
        if version < 12:
            self._migrate_to_v12()
        if version < 13:
            self._migrate_to_v13()
        if version < 14:
            self._migrate_to_v14()
        self.conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)",(str(SCHEMA_VERSION),))
        now = time.time()
        self.conn.execute(
            """INSERT OR IGNORE INTO global_state
            (id,energy,hunger,mood_valence,mood_arousal,health_status,health_note,
             sleep_phase,current_location,current_activity,body_cycle,last_updated_at)
            VALUES(1,70,20,0,0.65,'normal','状态正常','awake','家里','自由活动','未启用',?)""",
            (now,),
        )
        self.conn.execute(
            """INSERT OR IGNORE INTO sleep_runtime
            (id,phase,started_at,awake_grace_until,woken_count,last_event)
            VALUES(1,'awake',?,0,0,'初始化')""",
            (now,),
        )
        self.conn.execute("INSERT OR IGNORE INTO memory_runtime(id) VALUES(1)")
        self.conn.commit()

    def _migrate_to_v9(self) -> None:
        """删除已取消的技能/别名数据，同时保留其余生活状态。"""
        with self._tx() as conn:
            conn.execute("DROP TABLE IF EXISTS skill_events")
            conn.execute("DROP TABLE IF EXISTS skills")
            conn.execute("DROP TABLE IF EXISTS relationship_entries")
            # v1.6 的 -1 表示“继承角色默认值”；v1.7 起数据库中只保存明确额度。
            conn.execute("""UPDATE users SET daily_proactive_max=
                CASE WHEN role='owner' THEN 2 ELSE 1 END WHERE daily_proactive_max<0""")
            # 旧版允许手填昵称、群别名和 @ 目标；升级后清空这些展示快照，等待 Host 重新读取。
            conn.execute("UPDATE users SET display_name='' ")
            conn.execute("UPDATE group_observations SET group_alias='' ")
            conn.execute("UPDATE relay_candidates SET mention_user_id='',mention_name='' ")
            columns={str(row[1]) for row in conn.execute("PRAGMA table_info(memory_runtime)").fetchall()}
            if "last_skill_day" in columns:
                conn.execute("""CREATE TABLE memory_runtime_v9(
                    id INTEGER PRIMARY KEY CHECK(id=1), last_diary_day TEXT NOT NULL DEFAULT '',
                    last_cleanup_at REAL NOT NULL DEFAULT 0)""")
                conn.execute("""INSERT OR REPLACE INTO memory_runtime_v9(id,last_diary_day,last_cleanup_at)
                    SELECT id,last_diary_day,last_cleanup_at FROM memory_runtime""")
                conn.execute("DROP TABLE memory_runtime")
                conn.execute("ALTER TABLE memory_runtime_v9 RENAME TO memory_runtime")

    def _migrate_to_v10(self) -> None:
        """v10 新增逻辑搜索历史表；新表由 CREATE TABLE IF NOT EXISTS 幂等创建，无需搬运旧数据。"""
        with self._tx() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS search_history(
                  id TEXT PRIMARY KEY, created_at REAL NOT NULL,
                  operation TEXT NOT NULL, query TEXT NOT NULL,
                  provider_type TEXT NOT NULL DEFAULT '', success INTEGER NOT NULL,
                  result_count INTEGER NOT NULL DEFAULT 0, error_class TEXT NOT NULL DEFAULT '',
                  duration_ms REAL NOT NULL DEFAULT 0, results TEXT NOT NULL DEFAULT '[]',
                  expires_at REAL NOT NULL
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_search_history_time ON search_history(created_at,operation)"
            )

    def _migrate_to_v11(self) -> None:
        """移除已废弃的疑难图片视觉摘要表。"""
        with self._tx() as conn:
            conn.execute("DROP TABLE IF EXISTS image_summaries")

    def _migrate_to_v12(self) -> None:
        """v12 新增主动候选跳过原因统计表；新表由 CREATE TABLE IF NOT EXISTS 幂等创建。"""
        with self._tx() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS proactive_skip_stats(
                  user_id TEXT NOT NULL, reason TEXT NOT NULL, day TEXT NOT NULL,
                  count INTEGER NOT NULL DEFAULT 0,
                  PRIMARY KEY(user_id,reason,day)
                )"""
            )

    def _migrate_to_v13(self) -> None:
        """v13 新增心情事件流与逐小时状态快照表；均为幂等新建，不改动任何旧数据（含书柜）。"""
        with self._tx() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS mood_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,
                day TEXT NOT NULL, kind TEXT NOT NULL, delta REAL NOT NULL)"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mood_events_day ON mood_events(day,kind)")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS state_snapshots(
                ts REAL PRIMARY KEY, energy REAL NOT NULL, hunger REAL NOT NULL,
                mood_valence REAL NOT NULL, mood_arousal REAL NOT NULL,
                sleep_phase TEXT NOT NULL, current_activity TEXT NOT NULL DEFAULT '')"""
            )

    def _migrate_to_v14(self) -> None:
        """v14 为睡眠相位增加睡前推迟点；幂等补列，不改动任何旧数据（含书柜）。"""
        self._ensure_column("sleep_runtime","sleep_defer_until","REAL NOT NULL DEFAULT 0")

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        """幂等补充 SQLite 列，避免测试阶段升级时覆盖已有生活数据。"""
        columns = {str(row[1]) for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    async def close(self) -> None:
        async with self._lock:
            if self._conn:
                self._conn.commit()
                self._conn.close()
                self._conn = None

    async def get_state(self) -> dict[str, Any]:
        async with self._lock:
            row = self.conn.execute("SELECT * FROM global_state WHERE id=1").fetchone()
            return dict(row) if row else {}

    async def save_state(self, state: dict[str, Any]) -> None:
        async with self._lock:
            with self._tx() as conn:
                conn.execute(
                    """UPDATE global_state SET energy=?,hunger=?,mood_valence=?,mood_arousal=?,
                    health_status=?,health_note=?,sleep_phase=?,current_location=?,current_activity=?,
                    body_cycle=?,last_updated_at=? WHERE id=1""",
                    tuple(state[k] for k in (
                        "energy","hunger","mood_valence","mood_arousal","health_status",
                        "health_note","sleep_phase","current_location","current_activity",
                        "body_cycle","last_updated_at"
                    )),
                )

    # 心情事件：单条 SQL 原子加分，避免与状态推进的读改写竞争；当日次数超限直接跳过。
    async def record_mood_event(self, kind: str, now: Any) -> float:
        """记录一次心情事件并原子加分，返回实际加分值；超过当日上限或规则缺失返回 0.0。

        now 使用环境时区的 datetime，day 归属与跳过统计保持一致。
        只更新 mood_valence，不触碰 last_updated_at，避免干扰状态推进的时间游标。
        """
        rule = MOOD_EVENT_RULES.get(str(kind or ""))
        if not rule:
            return 0.0
        delta, cap = float(rule[0]), max(0, int(rule[1]))
        day = now.strftime("%Y-%m-%d")
        async with self._lock:
            with self._tx() as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM mood_events WHERE kind=? AND day=?", (kind, day)
                ).fetchone()[0]
                if count >= cap:
                    return 0.0
                conn.execute(
                    "INSERT INTO mood_events(created_at,day,kind,delta) VALUES(?,?,?,?)",
                    (float(now.timestamp()), day, kind[:32], delta),
                )
                conn.execute(
                    "UPDATE global_state SET mood_valence=MAX(-1.0,MIN(1.0,mood_valence+?)) WHERE id=1",
                    (delta,),
                )
                return delta

    # 逐小时状态快照：主键按整点取整天然幂等，运行期间每小时留下一条轨迹。
    async def save_state_snapshot(self, ts: float, state: dict[str, Any]) -> None:
        bucket = float(int(float(ts) // 3600) * 3600)
        async with self._lock:
            self.conn.execute(
                """INSERT OR IGNORE INTO state_snapshots
                (ts,energy,hunger,mood_valence,mood_arousal,sleep_phase,current_activity)
                VALUES(?,?,?,?,?,?,?)""",
                (bucket, float(state.get("energy") or 0), float(state.get("hunger") or 0),
                 float(state.get("mood_valence") or 0), float(state.get("mood_arousal") or 0),
                 str(state.get("sleep_phase") or "")[:32], str(state.get("current_activity") or "")[:120]),
            )
            self.conn.commit()

    # 只读轨迹查询：WebUI 历史曲线按小时桶升序读取最近窗口，不做任何写入。
    async def recent_state_snapshots(self, now: float, hours: int = 72) -> list[dict[str, Any]]:
        """返回 [now-hours*3600, now] 内的状态快照，按 ts 升序；无数据返回空列表。

        now 为环境时区对应的 epoch 秒；窗口上限收敛到 30 天，防止调用方误传放大查询。
        """
        span = max(1, min(24 * 30, int(hours))) * 3600
        end = float(now)
        async with self._lock:
            rows = self.conn.execute(
                """SELECT ts,energy,hunger,mood_valence,mood_arousal,sleep_phase
                FROM state_snapshots WHERE ts>=? AND ts<=? ORDER BY ts ASC""",
                (end - span, end),
            ).fetchall()
            return [dict(row) for row in rows]

    async def get_sleep_runtime(self) -> dict[str, Any]:
        async with self._lock:
            row = self.conn.execute("SELECT * FROM sleep_runtime WHERE id=1").fetchone()
            return dict(row) if row else {}

    async def save_sleep_runtime(self, runtime: dict[str, Any]) -> None:
        async with self._lock:
            with self._tx() as conn:
                conn.execute(
                    """UPDATE sleep_runtime SET phase=?,started_at=?,awake_grace_until=?,
                    woken_count=?,last_event=?,sleep_defer_until=? WHERE id=1""",
                    (runtime["phase"],runtime["started_at"],runtime.get("awake_grace_until",0),
                     runtime.get("woken_count",0),runtime.get("last_event",""),
                     runtime.get("sleep_defer_until",0)),
                )

    async def add_dream(self, content: str, mood_delta: float, energy_delta: float,
                        sleep_started_at: float, fragments: list[str] | None = None,
                        created_at: float = 0) -> int:
        async with self._lock:
            with self._tx() as conn:
                cursor=conn.execute(
                    "INSERT INTO dreams(created_at,content,mood_delta,energy_delta,sleep_started_at) VALUES(?,?,?,?,?)",
                    (created_at or time.time(), content[:1000], mood_delta, energy_delta, sleep_started_at),
                )
                dream_id=int(cursor.lastrowid)
                clean=[str(item).strip()[:300] for item in (fragments or []) if str(item).strip()][:5]
                conn.executemany(
                    "INSERT INTO dream_fragments(dream_id,position,content) VALUES(?,?,?)",
                    [(dream_id,index,item) for index,item in enumerate(clean)],
                )
                return dream_id

    async def latest_dream(self) -> dict[str, Any]:
        async with self._lock:
            # 同秒入库的梦境按 id 决胜，保证 latest_dream 稳定返回最新一条。
            row = self.conn.execute("SELECT * FROM dreams ORDER BY created_at DESC, id DESC LIMIT 1").fetchone()
            if not row:return {}
            data=dict(row)
            fragments=self.conn.execute(
                "SELECT content FROM dream_fragments WHERE dream_id=? ORDER BY position",(data["id"],)
            ).fetchall()
            data["fragments"]=[str(item[0]) for item in fragments]
            return data

    async def dreams_between(self, start: float, end: float) -> list[dict[str, Any]]:
        async with self._lock:
            rows=self.conn.execute(
                "SELECT * FROM dreams WHERE created_at>=? AND created_at<? ORDER BY created_at",(start,end)
            ).fetchall()
            result=[]
            for row in rows:
                data=dict(row)
                fragments=self.conn.execute(
                    "SELECT content FROM dream_fragments WHERE dream_id=? ORDER BY position",(data["id"],)
                ).fetchall()
                data["fragments"]=[str(item[0]) for item in fragments]
                result.append(data)
            return result

    # 替换日程时同步清理旧场景和机会，避免旧节点继续触发。
    async def replace_framework(self, day: str, nodes: list[dict[str, Any]]) -> None:
        async with self._lock:
            with self._tx() as conn:
                old_ids = [r[0] for r in conn.execute("SELECT id FROM daily_framework WHERE day=?", (day,))]
                if old_ids:
                    marks = ",".join("?" for _ in old_ids)
                    conn.execute(f"DELETE FROM proactive_opportunities WHERE framework_id IN ({marks})", old_ids)
                    conn.execute(f"DELETE FROM detailed_scenes WHERE framework_id IN ({marks})", old_ids)
                conn.execute("DELETE FROM daily_framework WHERE day=?", (day,))
                conn.executemany(
                    """INSERT INTO daily_framework(id,day,start_minute,end_minute,kind,summary,location,energy_load,shareability)
                    VALUES(:id,:day,:start_minute,:end_minute,:kind,:summary,:location,:energy_load,:shareability)""",
                    nodes,
                )

    async def get_framework(self, day: str) -> list[dict[str, Any]]:
        async with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM daily_framework WHERE day=? ORDER BY start_minute", (day,)
            ).fetchall()
            return [dict(r) for r in rows]

    async def save_scene(self, framework_id: str, scene: str, deltas: dict[str, Any], opportunities: list[dict[str, Any]]) -> None:
        async with self._lock:
            with self._tx() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO detailed_scenes(framework_id,scene,state_deltas,created_at,applied) VALUES(?,?,?,?,0)",
                    (framework_id, scene, json.dumps(deltas, ensure_ascii=False), time.time()),
                )
                for item in opportunities:
                    conn.execute(
                        """INSERT OR IGNORE INTO proactive_opportunities
                        (id,framework_id,topic,motive,weight,privacy,target_user_id,expires_at) VALUES(?,?,?,?,?,?,?,?)""",
                        (item["id"], framework_id, item["topic"], item["motive"], item["weight"],
                         item.get("privacy","normal"),item.get("target_user_id",""),item["expires_at"]),
                    )

    async def get_scene(self, framework_id: str) -> dict[str, Any]:
        async with self._lock:
            row = self.conn.execute("SELECT * FROM detailed_scenes WHERE framework_id=?", (framework_id,)).fetchone()
            if not row:
                return {}
            data = dict(row)
            data["state_deltas"] = json.loads(data.get("state_deltas") or "{}")
            return data

    async def get_scenes_by_framework_ids(self, framework_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """按框架 ID 批量读取细化场景，消除多日时间线逐节点查询的 N+1。"""
        ids=[str(framework_id) for framework_id in framework_ids if str(framework_id)]
        result={framework_id:{} for framework_id in dict.fromkeys(ids)}
        if not ids:return result
        marks=",".join("?" for _ in ids)
        async with self._lock:
            rows=self.conn.execute(
                f"SELECT * FROM detailed_scenes WHERE framework_id IN ({marks})", ids,
            ).fetchall()
            for row in rows:
                data=dict(row); data["state_deltas"]=json.loads(data.get("state_deltas") or "{}")
                result[str(data["framework_id"])]=data
        return result

    async def mark_scene_applied(self, framework_id: str) -> None:
        async with self._lock:
            self.conn.execute("UPDATE detailed_scenes SET applied=1 WHERE framework_id=?", (framework_id,))
            self.conn.commit()

    async def add_opportunity(self, item: dict[str, Any]) -> bool:
        async with self._lock:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO proactive_opportunities
                (id,framework_id,topic,motive,weight,privacy,target_user_id,expires_at) VALUES(?,?,?,?,?,?,?,?)""",
                (item["id"],item["framework_id"],item["topic"],item["motive"],item["weight"],item.get("privacy","normal"),
                 item.get("target_user_id",""),item["expires_at"]),
            )
            self.conn.commit()
            return cur.rowcount == 1

    async def active_opportunities(self, now: float) -> list[dict[str, Any]]:
        async with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM proactive_opportunities WHERE consumed_at=0 AND expires_at>? ORDER BY weight DESC",
                (now,),
            ).fetchall()
            return [dict(r) for r in rows]

    # 条件更新保证同一生活事件最多被一个用户消费。
    async def consume_opportunity(self, opportunity_id: str, user_id: str, now: float) -> bool:
        async with self._lock:
            cur = self.conn.execute(
                "UPDATE proactive_opportunities SET consumed_by=?,consumed_at=? WHERE id=? AND consumed_at=0",
                (user_id, now, opportunity_id),
            )
            self.conn.commit()
            return cur.rowcount == 1

    async def release_opportunity(self, opportunity_id: str) -> None:
        async with self._lock:
            self.conn.execute("UPDATE proactive_opportunities SET consumed_by='',consumed_at=0 WHERE id=?", (opportunity_id,))
            self.conn.commit()

    async def sync_users(self, profiles: Iterable[Any]) -> None:
        """将 WebUI 用户列表同步到运行库；QQ 号与角色配置是身份判断的唯一来源。"""
        async with self._lock:
            with self._tx() as conn:
                seen=[]
                for p in profiles:
                    uid=str(p.user_id).strip()
                    if not uid: continue
                    seen.append(uid)
                    role=str(getattr(p,"role","friend") or "friend")
                    daily_limit=max(0,min(20,int(getattr(p,"daily_proactive_max",1))))
                    conn.execute(
                        """INSERT INTO users(user_id,enabled,proactive_enabled,display_name,temperature,role,daily_proactive_max,quiet_start,quiet_end)
                        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                        enabled=excluded.enabled,proactive_enabled=excluded.proactive_enabled,
                        role=excluded.role,daily_proactive_max=excluded.daily_proactive_max,
                        quiet_start=excluded.quiet_start,quiet_end=excluded.quiet_end""",
                        (uid,int(p.enabled),int(p.proactive_enabled),"",float(p.initial_temperature),role,daily_limit,p.quiet_start,p.quiet_end),
                    )
                if seen:
                    marks=",".join("?" for _ in seen)
                    conn.execute(f"UPDATE users SET enabled=0,proactive_enabled=0 WHERE user_id NOT IN ({marks})", seen)
                else:
                    conn.execute("UPDATE users SET enabled=0,proactive_enabled=0")

    async def get_user(self, user_id: str) -> dict[str, Any]:
        async with self._lock:
            row=self.conn.execute("SELECT * FROM users WHERE user_id=?",(user_id,)).fetchone()
            return dict(row) if row else {}

    async def list_users(self, proactive_only: bool=False, *, include_disabled:bool=False) -> list[dict[str, Any]]:
        async with self._lock:
            sql="SELECT * FROM users" if include_disabled else "SELECT * FROM users WHERE enabled=1"
            if proactive_only:sql += " WHERE enabled=1 AND proactive_enabled=1" if include_disabled else " AND proactive_enabled=1"
            return [dict(r) for r in self.conn.execute(sql).fetchall()]

    async def set_user_stream(self, user_id: str, stream_id: str) -> None:
        async with self._lock:
            self.conn.execute("UPDATE users SET stream_id=? WHERE user_id=?",(stream_id,user_id)); self.conn.commit()

    async def update_user_display_name(self,user_id:str,display_name:str)->None:
        """昵称只作为自动展示快照，身份与授权始终使用 user_id。"""
        clean=" ".join(str(display_name or "").replace("\x00","").split())[:120]
        if not user_id or not clean:return
        async with self._lock:
            self.conn.execute("UPDATE users SET display_name=? WHERE user_id=?",(clean,user_id)); self.conn.commit()

    # 同时记录活跃时段，并识别用户是否回应了近期主动消息。
    async def record_interaction(self, user_id: str, text: str, now: float, hour: int,
                                 source_message_id: str="") -> None:
        """记录截断后的互动摘要，并原子更新用户活跃时间和主动回复标记。"""
        async with self._lock:
            with self._tx() as conn:
                conn.execute("UPDATE users SET last_user_message_at=? WHERE user_id=?",(now,user_id))
                conn.execute(
                    """INSERT INTO interaction_events
                    (user_id,created_at,kind,hour,content_summary,source_message_id) VALUES(?,?,?,?,?,?)""",
                    (user_id,now,"message",hour,text[:240],source_message_id[:240]),
                )
                pending=conn.execute(
                    "SELECT id,sent_at FROM proactive_events WHERE user_id=? AND status='sent' AND sent_at>? ORDER BY sent_at DESC LIMIT 1",
                    (user_id,now-7200),
                ).fetchone()
                if pending:
                    exists=conn.execute(
                        "SELECT 1 FROM interaction_events WHERE user_id=? AND kind='proactive_response' AND created_at>?",
                        (user_id,float(pending[1])),
                    ).fetchone()
                    if not exists:
                        conn.execute(
                            """INSERT INTO interaction_events
                            (user_id,created_at,kind,hour,content_summary,source_message_id) VALUES(?,?,?,?,?,?)""",
                            (user_id,now,"proactive_response",hour,"回应了主动消息",source_message_id[:240]),
                        )

    async def active_hours(self, user_id: str, since: float) -> dict[int,int]:
        async with self._lock:
            rows=self.conn.execute(
                "SELECT hour,COUNT(*) c FROM interaction_events WHERE user_id=? AND created_at>? AND kind='message' GROUP BY hour",
                (user_id,since),
            ).fetchall()
            return {int(r[0]):int(r[1]) for r in rows}

    # 主动巡检批量预加载：一次查询返回全部用户的活跃时段，避免每用户×每契机重复异步查询。
    async def active_hours_batch(self, user_ids: Iterable[str], since: float) -> dict[str, dict[int,int]]:
        """按 user_id 批量返回活跃时段计数，未产生互动的用户返回空字典。"""
        ids=list(dict.fromkeys(str(user_id) for user_id in user_ids if str(user_id)))
        result={user_id:{} for user_id in ids}
        if not ids:return result
        marks=",".join("?" for _ in ids)
        async with self._lock:
            rows=self.conn.execute(
                f"SELECT user_id,hour,COUNT(*) c FROM interaction_events "
                f"WHERE user_id IN ({marks}) AND created_at>? AND kind='message' GROUP BY user_id,hour",
                (*ids,since),
            ).fetchall()
            for row in rows:
                result[str(row[0])][int(row[1])]=int(row[2])
        return result

    async def recent_interactions(self, user_id: str, limit: int=8, *, within_days: float=14) -> list[str]:
        """最近互动摘要；within_days 限制时效，避免把很久以前的话题当“最近消息”。"""
        async with self._lock:
            rows=self.conn.execute(
                """SELECT content_summary FROM interaction_events
                WHERE user_id=? AND kind='message' AND created_at>? ORDER BY created_at DESC LIMIT ?""",
                (user_id,time.time()-max(0.0,float(within_days))*86400,limit),
            ).fetchall()
            return [str(row[0]) for row in reversed(rows) if str(row[0]).strip()]

    @staticmethod
    def _backlog_label(summary: str, created_at: float, now: float) -> str:
        """积压摘要加相对时间，避免模型把几小时前的消息当成刚发生的。"""
        hours=(now-float(created_at or 0))/3600
        prefix=f"约{max(1,round(hours))}小时前：" if hours>=1 else ""
        return prefix+str(summary or "")

    async def add_rest_backlog(self, user_id: str, summary: str, now: float,
                               source_message_id: str="") -> None:
        async with self._lock:
            self.conn.execute(
                """INSERT INTO rest_backlogs
                (user_id,created_at,summary,source_message_id) VALUES(?,?,?,?)""",
                (user_id,now,summary[:240],source_message_id[:240]),
            ); self.conn.commit()

    async def consume_rest_backlogs(self, user_id: str) -> list[str]:
        async with self._lock:
            with self._tx() as conn:
                rows=conn.execute("SELECT id,summary,created_at FROM rest_backlogs WHERE user_id=? AND consumed=0 ORDER BY created_at LIMIT 5",(user_id,)).fetchall()
                if rows:
                    conn.executemany("UPDATE rest_backlogs SET consumed=1 WHERE id=?",[(r[0],) for r in rows])
                now=time.time()
                return [self._backlog_label(str(r[1]),float(r[2]),now) for r in rows]

    async def peek_rest_backlogs(self, user_id: str) -> list[str]:
        async with self._lock:
            rows=self.conn.execute("SELECT summary,created_at FROM rest_backlogs WHERE user_id=? AND consumed=0 ORDER BY created_at LIMIT 5",(user_id,)).fetchall()
            now=time.time()
            return [self._backlog_label(str(r[0]),float(r[1]),now) for r in rows]

    # 批量版本使用窗口函数保留“每用户最多 3 条、按创建时间排序”的原有语义。
    async def peek_rest_backlogs_batch(self, user_ids: Iterable[str]) -> dict[str, list[str]]:
        """按 user_id 批量返回未消费休息积压，未消费积压为空时该用户返回空列表。"""
        ids=list(dict.fromkeys(str(user_id) for user_id in user_ids if str(user_id)))
        result={user_id:[] for user_id in ids}
        if not ids:return result
        marks=",".join("?" for _ in ids)
        async with self._lock:
            rows=self.conn.execute(
                f"""SELECT user_id,summary,created_at FROM (
                    SELECT user_id,summary,created_at,ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY created_at) rn
                    FROM rest_backlogs WHERE user_id IN ({marks}) AND consumed=0
                ) WHERE rn<=5 ORDER BY user_id""",
                ids,
            ).fetchall()
            now=time.time()
            for row in rows:
                result[str(row[0])].append(self._backlog_label(str(row[1]),float(row[2]),now))
        return result

    async def add_proactive_pending(self, event_id: str, user_id: str, opportunity_id: str, stream_id: str, now: float, expires_at: float) -> None:
        async with self._lock:
            self.conn.execute(
                "INSERT INTO proactive_events(id,user_id,opportunity_id,stream_id,status,created_at,expires_at) VALUES(?,?,?,?,?,?,?)",
                (event_id,user_id,opportunity_id,stream_id,"pending",now,expires_at),
            ); self.conn.commit()

    async def set_proactive_task_id(self, event_id: str, host_task_id: str) -> bool:
        """关联 Host 主动任务，后续只确认该任务实际产生的发送。"""
        if not event_id or not host_task_id:return False
        async with self._lock:
            cursor=self.conn.execute(
                "UPDATE proactive_events SET host_task_id=? WHERE id=? AND status='pending'",
                (host_task_id[:240],event_id),
            )
            self.conn.commit(); return cursor.rowcount==1

    async def set_proactive_event_status(self, event_id: str, status: str) -> None:
        async with self._lock:
            self.conn.execute(
                "UPDATE proactive_events SET status=? WHERE id=? AND status='pending'",
                (status[:24],event_id),
            )
            self.conn.commit()

    async def pending_proactive_for_task(self, stream_id: str, host_task_id: str, now: float) -> dict[str, Any]:
        if not stream_id or not host_task_id:return {}
        async with self._lock:
            row=self.conn.execute(
                """SELECT id,user_id,opportunity_id,stream_id,status,created_at,expires_at,sent_at,host_task_id
                FROM proactive_events WHERE stream_id=? AND host_task_id=? AND status='pending' AND expires_at>?""",
                (stream_id,host_task_id,now),
            ).fetchone()
            return dict(row) if row else {}

    async def proactive_for_task(self, stream_id: str, host_task_id: str) -> dict[str, Any]:
        if not stream_id or not host_task_id:return {}
        async with self._lock:
            row=self.conn.execute(
                """SELECT id,user_id,opportunity_id,stream_id,status,created_at,expires_at,sent_at,host_task_id
                FROM proactive_events WHERE stream_id=? AND host_task_id=?""",
                (stream_id,host_task_id),
            ).fetchone()
            return dict(row) if row else {}

    async def proactive_event(self, event_id: str) -> dict[str, Any]:
        """按插件事件号读取记录，用于消除 Host 唤醒与 task_id 回写之间的竞态。"""
        if not event_id:return {}
        async with self._lock:
            row=self.conn.execute(
                """SELECT id,user_id,opportunity_id,stream_id,status,created_at,expires_at,sent_at,host_task_id
                FROM proactive_events WHERE id=?""",
                (event_id,),
            ).fetchone()
            return dict(row) if row else {}

    # 只有 send_service.after_send 确认平台发送成功后，才增加实际主动额度。
    async def mark_pending_sent(self, stream_id: str, now: float, day: str = "", *,
                                event_id: str = "", host_task_id: str = "") -> bool:
        """提交已成功发送的主动候选；event_id 优先，其次 host_task_id，最后会话兜底。

        不要求 status='pending'：发送确认可能跨越 120 秒过期边界，只要事件尚未结算
        （sent_at=0）就按平台实际发送结果结算，避免"发送成功但事件 expired"。
        sent_at=0 同时保证幂等——同一事件不会重复累计主动额度。
        """
        async with self._lock:
            # 普通被动回复没有 pending 记录，只读查询不会创建 SQLite journal。
            if event_id:
                row=self.conn.execute(
                    """SELECT id,user_id,opportunity_id FROM proactive_events
                    WHERE id=? AND stream_id=? AND sent_at=0""",
                    (event_id,stream_id),
                ).fetchone()
            elif host_task_id:
                row=self.conn.execute(
                    """SELECT id,user_id,opportunity_id FROM proactive_events
                    WHERE stream_id=? AND host_task_id=? AND sent_at=0
                    ORDER BY created_at DESC LIMIT 1""",
                    (stream_id,host_task_id),
                ).fetchone()
            else:
                # 保留 v1 API 的会话兜底；插件主链使用 event_id/host_task_id 做精确确认。
                row=self.conn.execute(
                    "SELECT id,user_id,opportunity_id FROM proactive_events WHERE stream_id=? AND status='pending' AND expires_at>? ORDER BY created_at DESC LIMIT 1",
                    (stream_id,now),
                ).fetchone()
            if not row:
                return False
            with self._tx() as conn:
                conn.execute("UPDATE proactive_events SET status='sent',sent_at=? WHERE id=?",(now,row[0]))
                day=day or time.strftime("%Y-%m-%d",time.localtime(now))
                user=conn.execute("SELECT proactive_day FROM users WHERE user_id=?",(row[1],)).fetchone()
                if user and user[0]==day:
                    conn.execute("UPDATE users SET proactive_count=proactive_count+1,last_proactive_at=? WHERE user_id=?",(now,row[1]))
                else:
                    conn.execute("UPDATE users SET proactive_day=?,proactive_count=1,last_proactive_at=? WHERE user_id=?",(day,now,row[1]))
                relay=conn.execute(
                    "SELECT id FROM relay_candidates WHERE opportunity_id=? AND kind='group_to_private' AND status='queued'",
                    (row[2],),
                ).fetchone()
                if relay:
                    conn.execute("UPDATE relay_candidates SET status='sent',sent_at=? WHERE id=?",(now,relay[0]))
                    conn.execute("INSERT INTO relay_events(relay_id,status,detail,created_at) VALUES(?,?,?,?)",
                                 (relay[0],"sent","proactive_after_send",now))
                return True

    def _expire_pending_locked(self, conn: sqlite3.Connection, now: float,
                               max_retries: int, day: str) -> None:
        """过期 pending 事件的共用逻辑：释放机会、标记 expired、记 planner_no_reply。

        必须在已持有 ``self._lock`` 的上下文里以事务连接调用（``expire_pending``
        的锁内或 ``cleanup_runtime_records`` 的 ``_tx`` 内），自身绝不再取锁，
        否则维护清理与主动巡检会因重复取锁死锁。
        """
        rows=conn.execute(
            """SELECT id,opportunity_id,user_id FROM proactive_events
            WHERE status='pending' AND expires_at<=? AND sent_at=0""",
            (now,),
        ).fetchall()
        day=day or time.strftime("%Y-%m-%d",time.localtime(now))
        for row in rows:
            event_id=str(row[0]); opportunity_id=str(row[1]); user_id=str(row[2] or "")
            fail_count=conn.execute(
                """SELECT COUNT(*) FROM proactive_events
                WHERE opportunity_id=? AND status IN ('expired','cancelled','failed')""",
                (opportunity_id,),
            ).fetchone()[0]
            if int(fail_count) < max(0,int(max_retries)):
                # 释放机会，让它在后续巡检中重新进入候选池。
                conn.execute(
                    "UPDATE proactive_opportunities SET consumed_by='',consumed_at=0 WHERE id=?",
                    (opportunity_id,),
                )
            conn.execute(
                "UPDATE proactive_events SET status='expired' WHERE id=?",
                (event_id,),
            )
            # Planner 沉默或超时导致事件过期：记录一次跳过，便于诊断主动发言偏低。
            conn.execute(
                """INSERT INTO proactive_skip_stats(user_id,reason,day,count) VALUES(?,?,?,1)
                ON CONFLICT(user_id,reason,day) DO UPDATE SET count=count+1""",
                (user_id,"planner_no_reply",day),
            )

    async def expire_pending(self, now: float, *, max_retries: int = 2, day: str = "") -> None:
        """把过期的 pending 事件标记为 expired，并释放尚未发送成功的对应机会以便重试。

        同一机会最多重试 max_retries 次（按历史 expired 事件计数），超过后机会自然过期，
        避免 Planner 持续沉默时无限重复触发。sent_at>0 的已发送事件不会被误判为失败。
        day 是跳过统计归属的自然日（YYYY-MM-DD），由调用方按配置时区传入；
        留空时退回服务器本地日期，仅兼容旧调用方。
        """
        async with self._lock:
            with self._tx() as conn:
                self._expire_pending_locked(conn, now, max_retries, day)

    async def record_proactive_skip(self, user_id: str, reason: str, now: float, day: str = "") -> None:
        """按用户、原因、自然日聚合累计一次主动候选跳过，供诊断与 /麦麦管理 主动 查看。

        day 由调用方按配置时区传入，避免服务器时区与配置时区不一致时落错日期桶；
        留空时退回服务器本地日期，仅兼容旧调用方。
        """
        if not reason:
            return
        day = day or time.strftime("%Y-%m-%d", time.localtime(now))
        async with self._lock:
            self.conn.execute(
                """INSERT INTO proactive_skip_stats(user_id,reason,day,count) VALUES(?,?,?,1)
                ON CONFLICT(user_id,reason,day) DO UPDATE SET count=count+1""",
                (str(user_id or ""), str(reason)[:32], day),
            )
            self.conn.commit()

    async def proactive_skip_summary(self, since_day: str, limit: int = 50) -> list[dict[str, Any]]:
        """按原因聚合主动候选跳过次数（默认统计最近一个自然日），返回原因与次数。"""
        async with self._lock:
            rows = self.conn.execute(
                """SELECT reason,SUM(count) AS total,COUNT(DISTINCT user_id) AS users
                FROM proactive_skip_stats WHERE day>=? GROUP BY reason ORDER BY total DESC LIMIT ?""",
                (since_day, max(1, min(200, int(limit)))),
            ).fetchall()
            return [dict(row) for row in rows]

    # ── 活动播报：按 目标类型+目标ID+活动键 的持久化去重、每日限额与失败重试 ──

    async def announce_try_claim(
        self, target_type: str, target_id: str, activity_key: str,
        *, day: str, now: float, deduplicate: bool, retry_interval_minutes: int,
    ) -> tuple[bool, str]:
        """原子尝试占位一个播报名额。

        返回 (可发送, 原因)。已有 sent 记录视为已播报（deduplicate=false 时同一
        activity_key 只在当日去重，跨日允许重播——由调用方把 day 放进 activity_key，
        成功后由调用方把记录归档）；archived 不阻塞去重但计入当日限额。
        pending 未过期视为并发占用；failed 且未到重试时间视为冷却中。
        pending 占位超过 _ANNOUNCE_CLAIM_STALE_SECONDS 视为崩溃残留，允许重新占位。
        """
        async with self._lock:
            row = self.conn.execute(
                "SELECT status,next_retry_at,created_at FROM announce_log WHERE target_type=? AND target_id=? AND activity_key=?",
                (target_type, target_id, activity_key),
            ).fetchone()
            if row is not None:
                status, next_retry_at, created_at = str(row[0]), float(row[1] or 0), float(row[2] or 0)
                if status == "sent":
                    return False, "already_announced"
                if status == "pending" and now - created_at < _ANNOUNCE_CLAIM_STALE_SECONDS:
                    return False, "in_progress"
                if status == "failed" and now < next_retry_at:
                    return False, "retry_cooldown"
            with self._tx() as conn:
                conn.execute(
                    """INSERT INTO announce_log(target_type,target_id,activity_key,status,day,sent_at,error,next_retry_at,created_at)
                    VALUES(?,?,?,?,?,'0','','0',?)
                    ON CONFLICT(target_type,target_id,activity_key) DO UPDATE SET
                    status='pending',day=excluded.day,created_at=excluded.created_at""",
                    (target_type, target_id, activity_key, "pending", day, now),
                )
            return True, "claimed"

    async def announce_finish(
        self, target_type: str, target_id: str, activity_key: str,
        *, day: str, now: float, ok: bool, error: str = "", retry_interval_minutes: int = 10,
    ) -> None:
        """结算一次播报尝试：成功记 sent 并递增当日成功计数（计入每日限额），
        失败记 failed 并设置重试时间。两个写操作在同一事务内完成。"""
        async with self._lock:
            with self._tx() as conn:
                if ok:
                    conn.execute(
                        "UPDATE announce_log SET status='sent',day=?,sent_at=?,error='',next_retry_at=0 "
                        "WHERE target_type=? AND target_id=? AND activity_key=?",
                        (day, now, target_type, target_id, activity_key),
                    )
                    conn.execute(
                        """INSERT INTO announce_daily(day,target_type,target_id,count) VALUES(?,?,?,1)
                        ON CONFLICT(day,target_type,target_id) DO UPDATE SET count=count+1""",
                        (day, target_type, target_id),
                    )
                else:
                    conn.execute(
                        "UPDATE announce_log SET status='failed',error=?,next_retry_at=? "
                        "WHERE target_type=? AND target_id=? AND activity_key=?",
                        (str(error or "")[:240], now + max(1, retry_interval_minutes) * 60,
                         target_type, target_id, activity_key),
                    )

    async def announce_forget(self, target_type: str, target_id: str, activity_key: str) -> None:
        """deduplicate=false 时归档同键记录：不再参与去重（可重播）。
        当日成功次数由 announce_daily 计数表独立维护，不受归档影响。"""
        async with self._lock:
            with self._tx() as conn:
                conn.execute(
                    "UPDATE announce_log SET status='archived' WHERE target_type=? AND target_id=? AND activity_key=?",
                    (target_type, target_id, activity_key),
                )

    async def announce_daily_count(self, day: str, target_type: str, target_id: str) -> int:
        """统计某目标当日成功播报事件数（announce_finish(ok=True) 逐次累加，失败不计）。"""
        async with self._lock:
            row = self.conn.execute(
                "SELECT count FROM announce_daily WHERE day=? AND target_type=? AND target_id=?",
                (day, target_type, target_id),
            ).fetchone()
            return int(row[0] or 0) if row else 0

    async def announce_cleanup(self, keep_days: int, now: float) -> int:
        """清理过期播报记录：活动键内含自然日，跨日后旧键不再参与去重，可安全删除。"""
        cutoff_day = time.strftime("%Y-%m-%d", time.localtime(now - max(1, keep_days) * 86400))
        async with self._lock:
            with self._tx() as conn:
                cursor = conn.execute(
                    "DELETE FROM announce_log WHERE day<? AND status IN ('sent','archived')", (cutoff_day,))
                conn.execute("DELETE FROM announce_daily WHERE day<?", (cutoff_day,))
                return cursor.rowcount

    # ── 每日日程生成状态与成功归档：跨重启的重试计数 + 独立于当天框架的模板库 ──

    async def get_generation_state(self, day: str) -> dict[str, Any]:
        async with self._lock:
            row = self.conn.execute("SELECT * FROM schedule_generation WHERE day=?", (day,)).fetchone()
            return dict(row) if row else {}

    async def set_generation_state(
        self, day: str, *, status: str, attempts: int, next_retry_at: float = 0.0,
        last_error: str = "", has_temp: int = 0, fallback_source: str = "",
    ) -> None:
        """UPSERT 某天的生成状态；已完结（success/exhausted）的行由 cleanup 按保留期移除。"""
        async with self._lock:
            with self._tx() as conn:
                conn.execute(
                    """INSERT INTO schedule_generation(day,status,attempts,next_retry_at,last_error,has_temp,fallback_source,updated_at)
                    VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(day) DO UPDATE SET status=excluded.status,attempts=excluded.attempts,
                    next_retry_at=excluded.next_retry_at,last_error=excluded.last_error,
                    has_temp=excluded.has_temp,fallback_source=excluded.fallback_source,updated_at=excluded.updated_at""",
                    (day, status, int(attempts), float(next_retry_at), str(last_error or "")[:240],
                     int(has_temp), str(fallback_source or ""), time.time()),
                )

    async def archive_schedule(self, nodes: list[dict[str, Any]], *, source_day: str,
                               day_type: str, generator: str = "llm") -> str:
        """把一份通过校验的日程快照独立归档，返回唯一归档 ID；写入失败抛异常由调用方记录。"""
        if not nodes:
            raise ValueError("空日程不允许归档")
        archive_id = hashlib.sha1(f"{source_day}:{time.time_ns()}".encode()).hexdigest()[:20]
        async with self._lock:
            with self._tx() as conn:
                conn.execute(
                    """INSERT INTO schedule_archives(archive_id,source_day,archived_at,day_type,nodes_json,generator,schema_version)
                    VALUES(?,?,?,?,?,?,1)""",
                    (archive_id, source_day, time.time(), day_type,
                     json.dumps(nodes, ensure_ascii=False), generator),
                )
        return archive_id

    async def list_archives(self, now: float, retention_days: int,
                            day_type: str | None = None) -> list[dict[str, Any]]:
        """列出未过期的归档模板；day_type 用于优先匹配工作日/周末。"""
        cutoff = now - max(1, retention_days) * 86400
        async with self._lock:
            if day_type:
                rows = self.conn.execute(
                    "SELECT * FROM schedule_archives WHERE archived_at>=? AND day_type=? ORDER BY archived_at",
                    (cutoff, day_type),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM schedule_archives WHERE archived_at>=? ORDER BY archived_at", (cutoff,),
                ).fetchall()
            return [dict(r) for r in rows]

    async def cleanup_schedule_records(self, retention_days: int, now: float) -> int:
        """清理超期归档（按归档时间）与已完结的生成状态；retrying 状态与未过期归档永不删除。"""
        cutoff_ts = now - max(1, retention_days) * 86400
        cutoff_day = time.strftime("%Y-%m-%d", time.localtime(cutoff_ts))
        async with self._lock:
            with self._tx() as conn:
                removed = conn.execute(
                    "DELETE FROM schedule_archives WHERE archived_at<?", (cutoff_ts,)).rowcount
                conn.execute(
                    "DELETE FROM schedule_generation WHERE day<? AND status IN ('success','exhausted')",
                    (cutoff_day,))
                return removed

    async def pending_proactive_users(self, now: float) -> set[str]:
        """返回仍在等待 Planner/平台确认的用户，防止额度在发送前被并发穿透。"""
        async with self._lock:
            rows=self.conn.execute(
                "SELECT DISTINCT user_id FROM proactive_events WHERE status='pending' AND expires_at>?",
                (now,),
            ).fetchall()
            return {str(row[0]) for row in rows if str(row[0] or "")}

    async def set_wake_candidate(self, session_id: str, user_id: str, message_id: str, reason: str, now: float, expires_at: float) -> None:
        async with self._lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO wake_candidates
                (session_id,user_id,message_id,reason,created_at,expires_at) VALUES(?,?,?,?,?,?)""",
                (session_id,user_id,message_id,reason[:240],now,expires_at),
            )
            self.conn.commit()

    async def pop_wake_candidate(self, session_id: str, now: float, message_id: str = "") -> dict[str, Any]:
        """只有真实发送时消费待醒候选；普通发送只执行一次只读查询。"""
        async with self._lock:
            if message_id:
                row=self.conn.execute(
                    "SELECT * FROM wake_candidates WHERE session_id=? AND message_id=? AND expires_at>?",
                    (session_id,message_id,now),
                ).fetchone()
            else:
                row=self.conn.execute(
                    "SELECT * FROM wake_candidates WHERE session_id=? AND expires_at>?",
                    (session_id,now),
                ).fetchone()
            if not row:
                self.conn.execute("DELETE FROM wake_candidates WHERE session_id=? AND expires_at<=?",(session_id,now))
                self.conn.commit()
                return {}
            with self._tx() as conn:
                conn.execute("DELETE FROM wake_candidates WHERE session_id=? AND message_id=?",(session_id,row["message_id"]))
            return dict(row)

    async def clear_wake_candidate(self, session_id: str, message_id: str = "") -> None:
        async with self._lock:
            if message_id:
                self.conn.execute("DELETE FROM wake_candidates WHERE session_id=? AND message_id=?",(session_id,message_id))
            else:
                self.conn.execute("DELETE FROM wake_candidates WHERE session_id=?",(session_id,))
            self.conn.commit()

    async def get_continuity(self, user_id: str) -> dict[str, Any]:
        async with self._lock:
            row=self.conn.execute("SELECT * FROM conversation_continuity WHERE user_id=?",(user_id,)).fetchone()
            if not row:return {"intent":"","unresolved_topics":[],"updated_at":0}
            data=dict(row)
            try:data["unresolved_topics"]=json.loads(data.get("unresolved_topics") or "[]")
            except (TypeError,json.JSONDecodeError):data["unresolved_topics"]=[]
            return data

    async def save_continuity(self, user_id: str, intent: str, topics: list[str], now: float) -> None:
        clean_topics=[str(item).strip()[:180] for item in topics if str(item).strip()][:5]
        async with self._lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO conversation_continuity
                (user_id,intent,unresolved_topics,updated_at) VALUES(?,?,?,?)""",
                (user_id,intent[:80],json.dumps(clean_topics,ensure_ascii=False),now),
            )
            self.conn.commit()

    async def register_message_turn(self, session_id: str, turn_anchor: str, source_message_ids: list[str],
                                    user_id: str, now: float, expires_at: float) -> None:
        """持久化合并消息与最终回复锚点，热重载后仍能识别撤回轮次。"""
        sources=[]
        for value in source_message_ids:
            normalized=str(value or "").strip()[:240]
            if normalized and normalized not in sources:sources.append(normalized)
        if not session_id or not turn_anchor or not sources:return
        async with self._lock:
            with self._tx() as conn:
                conn.execute("DELETE FROM message_turn_sources WHERE expires_at<=?",(now,))
                conn.executemany(
                    """INSERT OR REPLACE INTO message_turn_sources
                    (session_id,turn_anchor,source_message_id,user_id,created_at,expires_at)
                    VALUES(?,?,?,?,?,?)""",
                    [(session_id[:240],turn_anchor[:240],source,user_id[:80],now,expires_at) for source in sources],
                )

    async def record_recall_event(self, *, session_id: str, recalled_message_id: str, user_id: str,
                                  operator_id: str, group_id: str, notice_type: str,
                                  source_adapter: str, summary: str, media: list[str],
                                  now: float, expires_at: float, summary_expires_at: float=0) -> None:
        """写入撤回墓碑和可选短摘要；同一消息重复通知时以最新安全字段覆盖。"""
        if not session_id or not recalled_message_id:return
        async with self._lock:
            self.conn.execute(
                """INSERT INTO recall_events
                (session_id,recalled_message_id,user_id,operator_id,group_id,notice_type,source_adapter,
                 summary,summary_expires_at,media_types,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(session_id,recalled_message_id) DO UPDATE SET
                user_id=excluded.user_id,operator_id=excluded.operator_id,group_id=excluded.group_id,
                notice_type=excluded.notice_type,source_adapter=excluded.source_adapter,
                summary=CASE WHEN excluded.summary<>'' OR excluded.media_types<>'[]' THEN excluded.summary ELSE recall_events.summary END,
                summary_expires_at=CASE WHEN excluded.summary<>'' OR excluded.media_types<>'[]' THEN excluded.summary_expires_at ELSE recall_events.summary_expires_at END,
                media_types=CASE WHEN excluded.media_types<>'[]' THEN excluded.media_types ELSE recall_events.media_types END,
                created_at=excluded.created_at,expires_at=MAX(recall_events.expires_at,excluded.expires_at)""",
                (session_id[:240],recalled_message_id[:240],user_id[:80],operator_id[:80],group_id[:80],
                 notice_type[:40],source_adapter[:32],summary[:1000],summary_expires_at,
                 json.dumps(media,ensure_ascii=False),now,expires_at),
            )
            self.conn.commit()

    async def is_recalled_turn(self, session_id: str, turn_anchor: str, now: float) -> bool:
        """直接锚点或其任一合并来源被撤回时，整轮都视为已取消。"""
        if not session_id or not turn_anchor:return False
        async with self._lock:
            row=self.conn.execute(
                """SELECT 1 FROM recall_events r
                WHERE r.session_id=? AND r.expires_at>? AND (
                  r.recalled_message_id=? OR EXISTS(
                    SELECT 1 FROM message_turn_sources t
                    WHERE t.session_id=r.session_id AND t.turn_anchor=?
                    AND t.source_message_id=r.recalled_message_id AND t.expires_at>?
                  )
                ) LIMIT 1""",
                (session_id,now,turn_anchor,turn_anchor,now),
            ).fetchone()
            return row is not None

    async def turn_anchors_for_source(self, session_id: str, source_message_id: str, now: float) -> list[str]:
        if not session_id or not source_message_id:return []
        async with self._lock:
            rows=self.conn.execute(
                """SELECT DISTINCT turn_anchor FROM message_turn_sources
                WHERE session_id=? AND source_message_id=? AND expires_at>?""",
                (session_id,source_message_id,now),
            ).fetchall()
            anchors=[str(row[0]) for row in rows if str(row[0]).strip()]
            if source_message_id not in anchors:anchors.append(source_message_id)
            return anchors

    async def recent_recall_context(self, session_id: str, now: float, limit: int=5) -> list[dict[str,Any]]:
        if not session_id:return []
        async with self._lock:
            rows=self.conn.execute(
                """SELECT recalled_message_id,notice_type,created_at FROM recall_events
                WHERE session_id=? AND expires_at>? ORDER BY created_at DESC LIMIT ?""",
                (session_id,now,max(1,min(20,int(limit)))),
            ).fetchall()
            return [dict(row) for row in rows]

    async def latest_recall_summary(self, session_id: str, user_id: str, now: float) -> dict[str,Any]:
        if not session_id or not user_id:return {}
        async with self._lock:
            row=self.conn.execute(
                """SELECT recalled_message_id,summary,media_types,created_at FROM recall_events
                WHERE session_id=? AND user_id=? AND group_id='' AND (summary<>'' OR media_types<>'[]')
                AND summary_expires_at>?
                ORDER BY created_at DESC LIMIT 1""",
                (session_id,user_id,now),
            ).fetchone()
            if not row:return {}
            result=dict(row)
            try:result["media_types"]=json.loads(result.get("media_types") or "[]")
            except (TypeError,json.JSONDecodeError):result["media_types"]=[]
            return result

    # WebUI 跨会话读取最近私聊撤回摘要：网页查询不带会话身份，latest_recall_summary 无法复用。
    async def recent_recall_summaries(self, now: float, limit: int = 5) -> list[dict[str, Any]]:
        """返回最近的私聊撤回摘要（含媒介类型），按 created_at 倒序；无数据返回空列表。

        与 latest_recall_summary 同范式：group_id='' 只取私聊、summary_expires_at 过滤有效期、
        media_types 解析成 list；limit 收敛到 1-50，防止调用方误传放大查询。
        """
        bounded=max(1,min(50,int(limit)))
        async with self._lock:
            rows=self.conn.execute(
                """SELECT session_id,user_id,summary,media_types,created_at FROM recall_events
                WHERE group_id='' AND summary_expires_at>? AND (summary<>'' OR media_types<>'[]')
                ORDER BY created_at DESC LIMIT ?""",
                (float(now),bounded),
            ).fetchall()
            result=[]
            for row in rows:
                item=dict(row)
                try:item["media_types"]=json.loads(item.get("media_types") or "[]")
                except (TypeError,json.JSONDecodeError):item["media_types"]=[]
                result.append(item)
            return result

    async def clear_recall_summaries(self) -> None:
        """关闭摘要缓存后立即清空正文与媒介元数据，但保留发送取消墓碑。"""
        async with self._lock:
            self.conn.execute(
                "UPDATE recall_events SET summary='',media_types='[]',summary_expires_at=0 "
                "WHERE summary<>'' OR media_types<>'[]' OR summary_expires_at<>0"
            )
            self.conn.commit()

    async def redact_recalled_private_artifacts(self, user_id: str, message_id: str) -> None:
        """撤回后移除由该私聊消息衍生的短期状态，避免后续 Prompt 再次引用。"""
        if not user_id or not message_id:return
        async with self._lock:
            with self._tx() as conn:
                conn.execute("DELETE FROM interaction_events WHERE user_id=? AND source_message_id=?",(user_id,message_id))
                latest=conn.execute(
                    "SELECT COALESCE(MAX(created_at),0) FROM interaction_events WHERE user_id=? AND kind='message'",
                    (user_id,),
                ).fetchone()
                conn.execute("UPDATE users SET last_user_message_at=? WHERE user_id=?",(float(latest[0] or 0),user_id))
                conn.execute("DELETE FROM rest_backlogs WHERE user_id=? AND source_message_id=?",(user_id,message_id))
                conn.execute("DELETE FROM date_candidates WHERE user_id=? AND source_message_id=? AND status='pending'",(user_id,message_id))
                date_rows=conn.execute(
                    "SELECT id FROM important_dates WHERE user_id=? AND source_message_id=?",(user_id,message_id)
                ).fetchall()
                for row in date_rows:
                    date_id=int(row[0])
                    conn.execute("DELETE FROM date_trigger_events WHERE date_id=?",(date_id,))
                    opportunity_rows=conn.execute(
                        "SELECT id FROM proactive_opportunities WHERE framework_id=?",
                        (f"important-date:{date_id}",),
                    ).fetchall()
                    for opportunity in opportunity_rows:
                        conn.execute(
                            "UPDATE proactive_events SET status='cancelled' WHERE opportunity_id=? AND status='pending'",
                            (opportunity[0],),
                        )
                    conn.execute("DELETE FROM proactive_opportunities WHERE framework_id=?",
                                 (f"important-date:{date_id}",))
                conn.execute("DELETE FROM important_dates WHERE user_id=? AND source_message_id=?",(user_id,message_id))

    async def retract_group_observation_source(self, group_id: str, message_id: str, now: float) -> int:
        """群消息撤回后删除包含该来源的匿名摘要，并取消尚未发送的群转私候选。"""
        if not group_id or not message_id:return 0
        async with self._lock:
            rows=self.conn.execute(
                "SELECT id,source_message_ids FROM group_observations WHERE group_id=? AND expires_at>?",
                (group_id,now),
            ).fetchall()
            observation_ids=[]
            for row in rows:
                try:sources=json.loads(row[1] or "[]")
                except (TypeError,json.JSONDecodeError):sources=[]
                if message_id in {str(item) for item in sources}:observation_ids.append(str(row[0]))
            if not observation_ids:return 0
            with self._tx() as conn:
                for observation_id in observation_ids:
                    relays=conn.execute(
                        """SELECT id,opportunity_id FROM relay_candidates
                        WHERE source_observation_id=? AND kind='group_to_private'""",(observation_id,)
                    ).fetchall()
                    for relay in relays:
                        opportunity_id=str(relay[1] or "")
                        conn.execute(
                            """UPDATE relay_candidates SET status='cancelled'
                            WHERE id=? AND status IN ('queued','pending','sending')""",(relay[0],)
                        )
                        if opportunity_id:
                            conn.execute(
                                "UPDATE proactive_events SET status='cancelled' WHERE opportunity_id=? AND status='pending'",
                                (opportunity_id,),
                            )
                            conn.execute("DELETE FROM proactive_opportunities WHERE id=?",(opportunity_id,))
                    conn.execute("DELETE FROM group_observations WHERE id=?",(observation_id,))
            return len(observation_ids)

    async def record_group_activity(self, group_id: str, user_id: str, display_name: str, now: float,
                                    source_message_id: str="") -> None:
        if not group_id or not user_id:return
        async with self._lock:
            self.conn.execute(
                """INSERT INTO group_user_activity(group_id,user_id,display_name,last_active_at,source_message_id)
                VALUES(?,?,?,?,?) ON CONFLICT(group_id,user_id) DO UPDATE SET
                display_name=CASE WHEN excluded.last_active_at>=group_user_activity.last_active_at
                                  THEN excluded.display_name ELSE group_user_activity.display_name END,
                source_message_id=CASE WHEN excluded.last_active_at>=group_user_activity.last_active_at
                                       THEN excluded.source_message_id ELSE group_user_activity.source_message_id END,
                last_active_at=MAX(group_user_activity.last_active_at,excluded.last_active_at)""",
                (group_id,user_id,display_name[:120],now,source_message_id[:240]),
            )
            self.conn.commit()

    async def upsert_group_directory(self,group_id:str,group_name:str,stream_id:str,now:float)->None:
        if not group_id:return
        name=" ".join(str(group_name or "").replace("\x00","").split())[:160]
        async with self._lock:
            self.conn.execute(
                """INSERT INTO group_directory(group_id,group_name,stream_id,updated_at) VALUES(?,?,?,?)
                ON CONFLICT(group_id) DO UPDATE SET
                group_name=CASE WHEN excluded.group_name!='' THEN excluded.group_name ELSE group_directory.group_name END,
                stream_id=CASE WHEN excluded.stream_id!='' THEN excluded.stream_id ELSE group_directory.stream_id END,
                updated_at=MAX(group_directory.updated_at,excluded.updated_at)""",
                (group_id,name,str(stream_id or "")[:240],float(now)),
            ); self.conn.commit()

    async def get_group_directory(self,group_id:str)->dict[str,Any]:
        async with self._lock:
            row=self.conn.execute("SELECT * FROM group_directory WHERE group_id=?",(group_id,)).fetchone()
            return dict(row) if row else {}

    async def list_group_directory(self,limit:int=100)->list[dict[str,Any]]:
        async with self._lock:
            rows=self.conn.execute(
                "SELECT * FROM group_directory ORDER BY updated_at DESC LIMIT ?",(max(1,min(500,int(limit))),)
            ).fetchall()
            return [dict(row) for row in rows]

    async def list_group_member_names(self,limit:int=200)->list[str]:
        """群成员昵称去重列表（按最近活跃排序）——供搜索查询词隐私清洗。"""
        async with self._lock:
            rows=self.conn.execute(
                "SELECT DISTINCT display_name FROM group_user_activity "
                "WHERE display_name!='' ORDER BY last_active_at DESC LIMIT ?",
                (max(1,min(500,int(limit))),),
            ).fetchall()
            return [" ".join(str(row[0] or "").split())[:120] for row in rows if str(row[0] or "").strip()]

    async def unique_group_for_stream(self,stream_id:str)->str:
        async with self._lock:
            rows=self.conn.execute(
                "SELECT group_id FROM group_directory WHERE stream_id=? ORDER BY updated_at DESC",(stream_id,)
            ).fetchall()
            values={str(row[0]) for row in rows if str(row[0])}
            return next(iter(values)) if len(values)==1 else ""

    async def clear_recalled_group_activity(self, group_id: str, message_id: str) -> int:
        """仅清除仍由该撤回消息占据的最新活跃指针；未知状态不会触发群转私。"""
        if not group_id or not message_id:return 0
        async with self._lock:
            cursor=self.conn.execute(
                """UPDATE group_user_activity SET display_name='',last_active_at=0,source_message_id=''
                WHERE group_id=? AND source_message_id=?""",(group_id,message_id),
            )
            self.conn.commit(); return int(cursor.rowcount)

    async def get_group_activity(self, group_id: str, user_id: str) -> dict[str, Any]:
        async with self._lock:
            row=self.conn.execute(
                "SELECT * FROM group_user_activity WHERE group_id=? AND user_id=?",(group_id,user_id)
            ).fetchone()
            return dict(row) if row else {}

    async def save_group_observation(self, item: dict[str, Any]) -> bool:
        """群聊原文不会传入存储层，只接受长度受限的公开话题摘要。"""
        async with self._lock:
            cursor=self.conn.execute(
                """INSERT OR IGNORE INTO group_observations
                (id,group_id,group_alias,topic,summary,interest_score,source_adapter,created_at,expires_at,source_message_ids)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (item["id"],item["group_id"],item["group_alias"],item["topic"][:240],item["summary"][:1200],
                 max(0,min(1,float(item.get("interest_score") or 0))),item.get("source_adapter","unknown")[:32],
                 float(item["created_at"]),float(item["expires_at"]),
                 json.dumps([str(value)[:240] for value in item.get("source_message_ids") or [] if str(value).strip()],ensure_ascii=False)),
            )
            self.conn.commit(); return cursor.rowcount==1

    async def recent_group_observations(self, now: float, limit: int=10) -> list[dict[str, Any]]:
        async with self._lock:
            rows=self.conn.execute(
                """SELECT * FROM group_observations WHERE expires_at>?
                ORDER BY created_at DESC LIMIT ?""",(now,max(1,min(100,int(limit))))
            ).fetchall()
            return [dict(row) for row in rows]

    async def create_relay_candidate(self, item: dict[str, Any]) -> bool:
        """创建转述候选，并在同一事务内把相同目标的旧候选标记为已取代。"""
        async with self._lock:
            with self._tx() as conn:
                if item["kind"]=="explicit":
                    sending=conn.execute(
                        """SELECT 1 FROM relay_candidates WHERE target_stream_id=? AND kind='explicit'
                        AND status='sending' AND expires_at>? LIMIT 1""",
                        (item.get("target_stream_id",""),float(item["created_at"])),
                    ).fetchone()
                    if sending:return False
                    stale=conn.execute(
                        """SELECT id FROM relay_candidates WHERE target_stream_id=? AND kind='explicit'
                        AND status='pending' AND expires_at>?""",
                        (item.get("target_stream_id",""),float(item["created_at"])),
                    ).fetchall()
                    conn.execute(
                        """UPDATE relay_candidates SET status='superseded' WHERE target_stream_id=?
                        AND kind='explicit' AND status='pending'""",(item.get("target_stream_id",""),)
                    )
                    conn.executemany(
                        "INSERT INTO relay_events(relay_id,status,detail,created_at) VALUES(?,'superseded','newer_relay',?)",
                        [(row[0],float(item["created_at"])) for row in stale],
                    )
                cursor=conn.execute(
                    """INSERT OR IGNORE INTO relay_candidates
                    (id,kind,opportunity_id,source_observation_id,source_group_id,target_group_id,target_user_id,target_stream_id,
                     summary,reason,mention_user_id,mention_name,status,created_at,expires_at,host_task_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (item["id"],item["kind"],item.get("opportunity_id","")[:80],item.get("source_observation_id","")[:80],item.get("source_group_id","")[:80],
                     item.get("target_group_id","")[:80],item.get("target_user_id","")[:80],item.get("target_stream_id","")[:160],
                     item.get("summary","")[:1200],item.get("reason","")[:1200],item.get("mention_user_id","")[:80],
                     item.get("mention_name","")[:120],item.get("status","pending")[:24],float(item["created_at"]),
                     float(item["expires_at"]),item.get("host_task_id","")[:240]),
                )
                if cursor.rowcount:
                    conn.execute("INSERT INTO relay_events(relay_id,status,detail,created_at) VALUES(?,?,?,?)",
                                 (item["id"],item.get("status","pending"),"created",float(item["created_at"])))
                return cursor.rowcount==1

    async def pending_relay_context(self, stream_id: str, now: float) -> dict[str, Any]:
        async with self._lock:
            row=self.conn.execute(
                """SELECT * FROM relay_candidates WHERE target_stream_id=? AND kind='explicit'
                AND status IN ('pending','sending') AND expires_at>?
                ORDER BY created_at DESC LIMIT 1""",(stream_id,now)
            ).fetchone()
            return dict(row) if row else {}

    async def set_relay_task_id(self, relay_id: str, host_task_id: str) -> bool:
        if not relay_id or not host_task_id:return False
        async with self._lock:
            cursor=self.conn.execute(
                "UPDATE relay_candidates SET host_task_id=? WHERE id=? AND status='pending'",
                (host_task_id[:240],relay_id),
            )
            self.conn.commit(); return cursor.rowcount==1

    async def relay_for_task(self, stream_id: str, host_task_id: str) -> dict[str, Any]:
        if not stream_id or not host_task_id:return {}
        async with self._lock:
            row=self.conn.execute(
                "SELECT * FROM relay_candidates WHERE target_stream_id=? AND host_task_id=? AND kind='explicit'",
                (stream_id,host_task_id),
            ).fetchone()
            return dict(row) if row else {}

    async def relay_candidate(self, relay_id: str) -> dict[str, Any]:
        """按候选号读取显式转述，支持 Planner 先于 Host task_id 回写到达。"""
        if not relay_id:return {}
        async with self._lock:
            row=self.conn.execute(
                "SELECT * FROM relay_candidates WHERE id=? AND kind='explicit'",
                (relay_id,),
            ).fetchone()
            return dict(row) if row else {}

    async def reserve_relay_for_send(self, stream_id: str, now: float, host_task_id: str = "",
                                    *, allow_expired: bool = False) -> dict[str, Any]:
        """发送前原子占用一次 @；Host 正常分段时只有第一段会命中。

        allow_expired 用于迟到发送放行：active 归因仍存活时，已过 expires_at 但未结算的
        候选仍应被占用并结算，避免“回复已发出但候选停留在 pending”。
        """
        async with self._lock:
            with self._tx() as conn:
                if host_task_id:
                    row=conn.execute(
                        """SELECT * FROM relay_candidates WHERE target_stream_id=? AND host_task_id=?
                        AND kind='explicit' AND status='pending' AND (? OR expires_at>?)""",
                        (stream_id,host_task_id,1 if allow_expired else 0,now),
                    ).fetchone()
                else:
                    # 兼容不返回 task_id 的旧 Host；不抢占已经精确关联的新任务。
                    row=conn.execute(
                        """SELECT * FROM relay_candidates WHERE target_stream_id=? AND kind='explicit'
                        AND host_task_id='' AND status='pending' AND expires_at>?
                        ORDER BY created_at DESC LIMIT 1""",(stream_id,now)
                    ).fetchone()
                if not row:return {}
                changed=conn.execute("UPDATE relay_candidates SET status='sending' WHERE id=? AND status='pending'",
                                     (row["id"],)).rowcount
                if not changed:return {}
                conn.execute("INSERT INTO relay_events(relay_id,status,detail,created_at) VALUES(?,?,?,?)",
                             (row["id"],"sending","before_send",now))
                return dict(row)

    async def finish_relay_send(self, relay_id: str, sent: bool, now: float) -> None:
        async with self._lock:
            with self._tx() as conn:
                row=conn.execute("SELECT status,expires_at FROM relay_candidates WHERE id=?",(relay_id,)).fetchone()
                if not row:return
                current=str(row[0] or "")
                if current=="sent":return
                # 已被禁用、过期或新任务取代的在途发送若失败，保留原终态；若平台实际成功则记录事实。
                if not sent and current!="sending":return
                status="sent" if sent else ("pending" if float(row[1])>now else "expired")
                conn.execute("UPDATE relay_candidates SET status=?,sent_at=? WHERE id=?",
                             (status,now if sent else 0,relay_id))
                conn.execute("INSERT INTO relay_events(relay_id,status,detail,created_at) VALUES(?,?,?,?)",
                             (relay_id,status,"after_send",now))

    async def set_relay_status(self, relay_id: str, status: str, now: float, detail: str="") -> None:
        """只从非终态迁移；已 sent/superseded/failed/expired/cancelled 的候选不会被覆盖。"""
        async with self._lock:
            with self._tx() as conn:
                cursor=conn.execute(
                    "UPDATE relay_candidates SET status=? WHERE id=? AND status IN ('pending','sending','queued')",
                    (status[:24],relay_id),
                )
                if cursor.rowcount:
                    conn.execute("INSERT INTO relay_events(relay_id,status,detail,created_at) VALUES(?,?,?,?)",
                                 (relay_id,status[:24],detail[:300],now))

    async def social_share_stats(self, user_id: str, since: float) -> dict[str, Any]:
        async with self._lock:
            row=self.conn.execute(
                """SELECT COUNT(*) count,MAX(created_at) last_at FROM relay_candidates
                WHERE target_user_id=? AND kind='group_to_private' AND created_at>=?
                AND status IN ('queued','sent')""",(user_id,since)
            ).fetchone()
            return {"count":int(row[0] or 0),"last_at":float(row[1] or 0)} if row else {"count":0,"last_at":0}

    async def add_creation_inspiration(self, item: dict[str, Any]) -> bool:
        async with self._lock:
            cursor=self.conn.execute(
                """INSERT OR IGNORE INTO creation_inspirations
                (id,source_kind,source_ref,prompt_digest,privacy_ceiling,score,status,created_at,expires_at)
                VALUES(?,?,?,?,?,?, 'pending',?,?)""",
                (item["id"],item["source_kind"][:40],item["source_ref"][:160],item["prompt_digest"][:2000],
                 "public" if item.get("privacy_ceiling")=="public" else "private",
                 max(0,min(1,float(item.get("score") or 0))),float(item["created_at"]),float(item["expires_at"])),
            )
            self.conn.commit(); return cursor.rowcount==1

    async def pending_creation_inspirations(self, now: float, limit: int=10) -> list[dict[str, Any]]:
        async with self._lock:
            rows=self.conn.execute(
                """SELECT * FROM creation_inspirations WHERE status='pending' AND expires_at>?
                ORDER BY score DESC,created_at DESC LIMIT ?""",(now,max(1,min(100,int(limit))))
            ).fetchall()
            return [dict(row) for row in rows]

    async def claim_creation_inspiration(self, inspiration_id: str) -> bool:
        """条件更新防止后台巡检和管理员命令同时消费同一份灵感。"""
        async with self._lock:
            cursor=self.conn.execute(
                "UPDATE creation_inspirations SET status='creating' WHERE id=? AND status='pending'",
                (inspiration_id,),
            )
            self.conn.commit(); return cursor.rowcount==1

    async def recover_creation_claims(self, now: float) -> None:
        """Runner 异常退出后恢复未完成灵感，并封存残留的 running 记录。"""
        async with self._lock:
            with self._tx() as conn:
                running=conn.execute("SELECT document_id FROM creation_runs WHERE status='running'").fetchall()
                document_ids=[str(row[0]) for row in running if str(row[0] or "")]
                if document_ids:
                    marks=",".join("?" for _ in document_ids)
                    # 已有 final 修订的文档书体完好（续写中被硬打断的旧书，或终稿落库后才被打断的新书），
                    # 恢复 archived 让它继续留在书柜，且不刷新 updated_at/archived_at 白吃当日额度；
                    # 只有什么都没留下的新书才标记 failed。
                    conn.execute(
                        f"""UPDATE bookshelf_documents SET status='archived'
                        WHERE id IN ({marks})
                        AND id IN (SELECT document_id FROM bookshelf_revisions WHERE stage='final')""",
                        document_ids,
                    )
                    conn.execute(
                        f"""UPDATE bookshelf_documents SET status='failed',updated_at=?
                        WHERE id IN ({marks})
                        AND id NOT IN (SELECT document_id FROM bookshelf_revisions WHERE stage='final')""",
                        (now,*document_ids),
                    )
                conn.execute(
                    "UPDATE creation_runs SET status='interrupted',completed_at=?,error_summary='Runner interrupted' WHERE status='running'",
                    (now,),
                )
                conn.execute("UPDATE creation_inspirations SET status='pending' WHERE status='creating' AND expires_at>?",(now,))
                conn.execute("UPDATE creation_inspirations SET status='expired' WHERE status='creating' AND expires_at<=?",(now,))

    async def mark_creation_inspiration(self, inspiration_id: str, status: str) -> None:
        async with self._lock:
            self.conn.execute("UPDATE creation_inspirations SET status=? WHERE id=?",(status[:24],inspiration_id))
            self.conn.commit()

    async def archived_work_count(self, start: float, end: float) -> int:
        """当日归档章节数：每条 final 终稿修订算一章，同一本书当天续写多章也逐章计入每日额度。

        不能按文档 archived_at 计数：续写只是把同一文档的 archived_at 刷新到当天，
        同日的第二章起计数不再增长，continuation_min_gap_days=0 时每日上限会形同虚设。
        每次成功归档（新书或续写）恰好落库一条 final 修订，按修订计数即"每章花一个额度"。
        """
        async with self._lock:
            row=self.conn.execute(
                """SELECT COUNT(*) FROM bookshelf_revisions r
                JOIN bookshelf_documents d ON d.id=r.document_id
                WHERE d.doc_type='work' AND r.stage='final'
                AND r.created_at>=? AND r.created_at<?""",(start,end)
            ).fetchone()
            return int(row[0]) if row else 0

    async def continuable_works(self, work_types: list[str], *, stale_before: float, max_chapters: int,
                                min_gap_before: float, limit: int = 50) -> list[dict[str, Any]]:
        """书柜续写候选：只统计 final 修订的章节数与最近章节时间，等最久的书优先。

        章节数上限、最小间隔与停滞判定全部下推 SQL，避免候选集被服务侧过滤饿死；
        隐私守卫依赖当日灵感，由调用方过滤。
        """
        async with self._lock:
            types=[str(item)[:40] for item in work_types if str(item)]
            if not types:return []
            marks=",".join("?" for _ in types)
            rows=self.conn.execute(
                f"""SELECT d.id,d.work_type,d.title,d.privacy,d.summary,d.created_at,
                (SELECT COUNT(*) FROM bookshelf_revisions r WHERE r.document_id=d.id AND r.stage='final') AS chapters,
                (SELECT COALESCE(MAX(r.created_at),0) FROM bookshelf_revisions r
                 WHERE r.document_id=d.id AND r.stage='final') AS last_chapter_at
                FROM bookshelf_documents d
                WHERE d.doc_type='work' AND d.status='archived' AND d.work_type IN ({marks})
                AND last_chapter_at>=? AND last_chapter_at<=? AND chapters<?
                ORDER BY last_chapter_at ASC LIMIT ?""",
                (*types,float(stale_before),float(min_gap_before),max(1,int(max_chapters)),
                 max(1,min(200,int(limit)))),
            ).fetchall()
            return [dict(row) for row in rows]

    async def create_bookshelf_document(self, item: dict[str, Any]) -> bool:
        async with self._lock:
            cursor=self.conn.execute(
                """INSERT OR IGNORE INTO bookshelf_documents
                (id,doc_type,work_type,title,privacy,status,current_revision,source_kind,source_ref,summary,
                 created_at,updated_at,archived_at)
                VALUES(?,?,?,?,?,?,0,?,?,?,?,?,0)""",
                (item["id"],item.get("doc_type","work")[:24],item.get("work_type","")[:40],item.get("title","未命名")[:160],
                 "public" if item.get("privacy")=="public" else "private",item.get("status","inspiration")[:24],
                 item.get("source_kind","")[:40],item.get("source_ref","")[:160],item.get("summary","")[:1000],
                 float(item["created_at"]),float(item["created_at"])),
            )
            self.conn.commit(); return cursor.rowcount==1

    async def add_bookshelf_revision(self, document_id: str, stage: str, content: str, model_task: str,
                                     now: float, *, set_current: bool=True, status: str="") -> int:
        """追加不可变书柜修订，并可原子推进文档当前版本和流水线状态。"""
        async with self._lock:
            with self._tx() as conn:
                row=conn.execute(
                    "SELECT COALESCE(MAX(revision_number),0)+1 FROM bookshelf_revisions WHERE document_id=?",
                    (document_id,),
                ).fetchone(); revision=int(row[0] if row else 1)
                conn.execute(
                    """INSERT INTO bookshelf_revisions
                    (document_id,revision_number,stage,content,model_task,created_at) VALUES(?,?,?,?,?,?)""",
                    (document_id,revision,stage[:24],content[:20000],model_task[:80],now),
                )
                if set_current:
                    archived=now if status=="archived" else 0
                    conn.execute(
                        """UPDATE bookshelf_documents SET current_revision=?,status=COALESCE(NULLIF(?,''),status),
                        updated_at=?,archived_at=CASE WHEN ?>0 THEN ? ELSE archived_at END WHERE id=?""",
                        (revision,status[:24],now,archived,archived,document_id),
                    )
                return revision

    async def update_bookshelf_document(self, document_id: str, *, title: str="", summary: str="",
                                        privacy: str="", status: str="", now: float=0) -> None:
        async with self._lock:
            row=self.conn.execute("SELECT * FROM bookshelf_documents WHERE id=?",(document_id,)).fetchone()
            if not row:return
            self.conn.execute(
                """UPDATE bookshelf_documents SET title=?,summary=?,privacy=?,status=?,updated_at=?,
                archived_at=CASE WHEN ?='archived' THEN ? ELSE archived_at END WHERE id=?""",
                (title[:160] or row["title"],summary[:1000] or row["summary"],
                 ("public" if privacy=="public" else "private") if privacy else row["privacy"],
                 status[:24] or row["status"],now or time.time(),status,now or time.time(),document_id),
            )
            self.conn.commit()

    async def start_creation_run(self, run_id: str, inspiration_id: str, document_id: str, now: float) -> None:
        async with self._lock:
            self.conn.execute(
                "INSERT INTO creation_runs(id,inspiration_id,document_id,status,started_at) VALUES(?,?,?,'running',?)",
                (run_id,inspiration_id,document_id,now),
            ); self.conn.commit()

    async def finish_creation_run(self, run_id: str, status: str, now: float, error: str="") -> None:
        async with self._lock:
            self.conn.execute(
                "UPDATE creation_runs SET status=?,completed_at=?,error_summary=? WHERE id=?",
                (status[:24],now,error[:500],run_id),
            ); self.conn.commit()

    async def list_bookshelf_documents(self, *, allow_private: bool, limit: int=20,
                                       doc_type: str="") -> list[dict[str, Any]]:
        async with self._lock:
            clauses=["d.status='archived'"]; params:list[Any]=[]
            if not allow_private:clauses.append("d.privacy='public'")
            if doc_type:clauses.append("d.doc_type=?"); params.append(doc_type)
            params.append(max(1,min(100,int(limit))))
            rows=self.conn.execute(
                f"""SELECT d.*,r.stage,r.content FROM bookshelf_documents d
                LEFT JOIN bookshelf_revisions r ON r.document_id=d.id AND r.revision_number=d.current_revision
                WHERE {' AND '.join(clauses)} ORDER BY d.updated_at DESC LIMIT ?""",params
            ).fetchall()
            return [dict(row) for row in rows]

    async def get_bookshelf_document(self, document_id: str, *, allow_private: bool) -> dict[str, Any]:
        async with self._lock:
            row=self.conn.execute("SELECT * FROM bookshelf_documents WHERE id=?",(document_id,)).fetchone()
            if not row or (row["privacy"]=="private" and not allow_private):return {}
            data=dict(row)
            revisions=self.conn.execute(
                """SELECT revision_number,stage,content,model_task,created_at FROM bookshelf_revisions
                WHERE document_id=? ORDER BY revision_number""",(document_id,)
            ).fetchall()
            data["revisions"]=[dict(item) for item in revisions]
            current=next((item for item in data["revisions"] if int(item["revision_number"])==int(data["current_revision"])),{})
            data["content"]=str(current.get("content") or "")
            return data

    async def save_reading_note(self, item: dict[str, Any]) -> bool:
        """外部阅读联动只接收有限文字摘要和生成批注，不接受二进制字段。"""
        async with self._lock:
            with self._tx() as conn:
                cursor=conn.execute(
                    """INSERT OR IGNORE INTO reading_notes
                    (id,source_api,external_id,title,summary,annotation,source_digest,created_at)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (item["id"],item["source_api"][:160],item["external_id"][:200],item["title"][:160],
                     item["summary"][:3000],item["annotation"][:4000],item["source_digest"][:128],float(item["created_at"])),
                )
                if not cursor.rowcount:return False
                document_id=f"reading:{item['id']}"; content=item["annotation"] or item["summary"]
                conn.execute(
                    """INSERT INTO bookshelf_documents
                    (id,doc_type,work_type,title,privacy,status,current_revision,source_kind,source_ref,summary,
                     created_at,updated_at,archived_at)
                    VALUES(?,'reading_note','',?,'private','archived',1,'external_reading',?,?,?,?,?)""",
                    (document_id,item["title"][:160],item["external_id"][:200],item["summary"][:1000],
                     float(item["created_at"]),float(item["created_at"]),float(item["created_at"])),
                )
                conn.execute(
                    """INSERT INTO bookshelf_revisions
                    (document_id,revision_number,stage,content,model_task,created_at)
                    VALUES(?,1,'annotation',?,'reading_annotation',?)""",
                    (document_id,content[:10000],float(item["created_at"])),
                )
                return True

    async def cleanup_creation(self, now: float) -> None:
        async with self._lock:
            self.conn.execute(
                "UPDATE creation_inspirations SET status='expired' WHERE status='pending' AND expires_at<=?",(now,)
            ); self.conn.commit()

    async def management_date_candidates(self, limit: int=100) -> list[dict[str, Any]]:
        async with self._lock:
            rows=self.conn.execute(
                """SELECT id,user_id,event_name,date_text,suggested_date,confidence,created_at,status
                FROM date_candidates WHERE status='pending' ORDER BY created_at DESC LIMIT ?""",
                (max(1,min(500,int(limit))),),
            ).fetchall()
            return [dict(row) for row in rows]

    async def management_bookshelf(self, limit: int=100) -> list[dict[str, Any]]:
        """管理摘要不返回正文和修订内容，降低误暴露私密文本的风险。"""
        async with self._lock:
            rows=self.conn.execute(
                """SELECT id,doc_type,work_type,title,privacy,status,current_revision,source_kind,
                summary,created_at,updated_at,archived_at FROM bookshelf_documents
                ORDER BY updated_at DESC LIMIT ?""",(max(1,min(500,int(limit))),)
            ).fetchall()
            return [dict(row) for row in rows]

    async def management_proactive_candidates(self, limit: int=100) -> list[dict[str, Any]]:
        async with self._lock:
            rows=self.conn.execute(
                """SELECT e.id,e.user_id,e.opportunity_id,e.status,e.created_at,e.expires_at,e.sent_at,
                o.topic,o.privacy,u.role FROM proactive_events e
                LEFT JOIN proactive_opportunities o ON o.id=e.opportunity_id
                LEFT JOIN users u ON u.user_id=e.user_id
                ORDER BY e.created_at DESC LIMIT ?""",(max(1,min(500,int(limit))),)
            ).fetchall()
            return [dict(row) for row in rows]

    async def management_creation_runs(self, limit: int=50) -> list[dict[str, Any]]:
        async with self._lock:
            rows=self.conn.execute(
                """SELECT r.id,r.inspiration_id,r.document_id,r.status,r.started_at,r.completed_at,
                d.title,d.privacy,d.work_type FROM creation_runs r
                LEFT JOIN bookshelf_documents d ON d.id=r.document_id
                ORDER BY r.started_at DESC LIMIT ?""",(max(1,min(200,int(limit))),)
            ).fetchall()
            return [dict(row) for row in rows]

    async def management_overview_counts(self) -> dict[str, int]:
        """使用 COUNT 聚合，避免概览数字受明细分页上限影响。"""
        async with self._lock:
            row=self.conn.execute(
                """SELECT
                (SELECT COUNT(*) FROM users WHERE enabled=1) AS users,
                (SELECT COUNT(*) FROM users WHERE enabled=1 AND role='owner') AS owners,
                (SELECT COUNT(*) FROM date_candidates WHERE status='pending') AS pending_dates,
                (SELECT COUNT(*) FROM bookshelf_documents) AS bookshelf_documents,
                (SELECT COUNT(*) FROM bookshelf_documents WHERE privacy='private') AS private_documents,
                (SELECT COUNT(*) FROM proactive_events WHERE status='pending') AS pending_proactive"""
            ).fetchone()
            return {key:int(row[key] or 0) for key in row.keys()} if row else {}

    async def cleanup_runtime_records(self, now: float, usage_before: float, *,
                                      records_before: float = 0) -> None:
        """清理运行态记录；records_before 提供长期记录的保留截止时间，0 表示本轮跳过长期清理。"""
        async with self._lock:
            with self._tx() as conn:
                conn.execute("DELETE FROM wake_candidates WHERE expires_at<=?",(now,))
                # 休息积压按 7 天保留期清理，避免关闭增强或长期静默后无界堆积。
                conn.execute("DELETE FROM rest_backlogs WHERE created_at<=?",(now-7*86400,))
                conn.execute("DELETE FROM reply_turns WHERE expires_at<=?",(now,))
                conn.execute("DELETE FROM message_turn_sources WHERE expires_at<=?",(now,))
                conn.execute(
                    "UPDATE recall_events SET summary='',media_types='[]',summary_expires_at=0 "
                    "WHERE summary_expires_at>0 AND summary_expires_at<=?",(now,)
                )
                conn.execute("DELETE FROM recall_events WHERE expires_at<=?",(now,))
                # 维护清理与 expire_pending 共用同一套过期逻辑（释放机会 + 记 planner_no_reply），
                # 避免维护抢先时重试静默失效；_expire_pending_locked 是同步方法，不会重复取锁。
                self._expire_pending_locked(conn, now, max(0, 2), "")
                conn.execute("DELETE FROM llm_usage_events WHERE created_at<?",(usage_before,))
                conn.execute("DELETE FROM group_observations WHERE expires_at<=?",(now,))
                conn.execute("UPDATE relay_candidates SET status='expired' WHERE expires_at<=? AND status IN ('pending','sending','queued')",(now,))
                conn.execute("UPDATE creation_inspirations SET status='expired' WHERE status IN ('pending','creating') AND expires_at<=?",(now,))
                if records_before>0:
                    # 只删终态行；已发送按 sent_at 计龄，未发送的终态按 expires_at 计龄。
                    conn.execute(
                        """DELETE FROM proactive_events WHERE status!='pending'
                        AND (CASE WHEN sent_at>0 THEN sent_at ELSE expires_at END)<=?""",
                        (records_before,),
                    )
                    # 契机都有有限 expires_at；pending 事件引用的旧契机不可能存在
                    # （pending 事件 120 秒内必然被 expire_pending 转为终态）。
                    conn.execute("DELETE FROM proactive_opportunities WHERE expires_at<=?",(records_before,))
                    conn.execute("DELETE FROM interaction_events WHERE created_at<?",(records_before,))
                    conn.execute(
                        """DELETE FROM relay_candidates WHERE status NOT IN ('pending','sending','queued')
                        AND (CASE WHEN sent_at>0 THEN sent_at ELSE expires_at END)<=?""",
                        (records_before,),
                    )
                    conn.execute("DELETE FROM relay_events WHERE created_at<?",(records_before,))
                    # day 是 YYYY-MM-DD 文本，按本地日期换算截止日即可（偏移至多一天）。
                    day_cut=time.strftime("%Y-%m-%d",time.localtime(records_before))
                    conn.execute("DELETE FROM proactive_skip_stats WHERE day<?",(day_cut,))
                    # 心情事件与状态快照是动力学分析数据源，保留期比运行记录更长。
                    history_before=now-STATE_HISTORY_RETENTION_DAYS*86400
                    conn.execute("DELETE FROM state_snapshots WHERE ts<=?",(history_before,))
                    conn.execute("DELETE FROM mood_events WHERE created_at<=?",(history_before,))

    async def record_llm_usage(self, *, created_at: float, source: str, task_name: str, model_name: str,
                               request_type: str, prompt_tokens: int, completion_tokens: int,
                               total_tokens: int, latency_ms: float, success: bool, error_summary: str="") -> None:
        async with self._lock:
            self.conn.execute(
                """INSERT INTO llm_usage_events
                (created_at,source,task_name,model_name,request_type,prompt_tokens,completion_tokens,total_tokens,latency_ms,success,error_summary)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (created_at,source[:32],task_name[:80],model_name[:160],request_type[:80],max(0,int(prompt_tokens)),
                 max(0,int(completion_tokens)),max(0,int(total_tokens)),max(0,float(latency_ms)),int(success),error_summary[:300]),
            )
            self.conn.commit()

    async def usage_summary(self, since: float) -> list[dict[str, Any]]:
        async with self._lock:
            rows=self.conn.execute(
                """SELECT source,task_name,COUNT(*) calls,SUM(success) successes,
                SUM(prompt_tokens) prompt_tokens,SUM(completion_tokens) completion_tokens,
                SUM(total_tokens) total_tokens,AVG(latency_ms) avg_latency_ms
                FROM llm_usage_events WHERE created_at>=? GROUP BY source,task_name ORDER BY total_tokens DESC""",
                (since,),
            ).fetchall()
            return [dict(row) for row in rows]

    async def reserve_reply_turn(self, session_id: str, anchor_message_id: str, now: float, expires_at: float) -> bool:
        if not session_id or not anchor_message_id:return True
        async with self._lock:
            with self._tx() as conn:
                conn.execute("DELETE FROM reply_turns WHERE expires_at<=?",(now,))
                row=conn.execute(
                    "SELECT status FROM reply_turns WHERE session_id=? AND anchor_message_id=?",
                    (session_id,anchor_message_id),
                ).fetchone()
                if row:return False
                conn.execute(
                    "INSERT INTO reply_turns(session_id,anchor_message_id,status,created_at,expires_at) VALUES(?,?,?,?,?)",
                    (session_id,anchor_message_id,"generated",now,expires_at),
                )
                return True

    async def release_reply_turn(self, session_id: str, anchor_message_id: str) -> None:
        if not session_id or not anchor_message_id:return
        async with self._lock:
            self.conn.execute(
                "DELETE FROM reply_turns WHERE session_id=? AND anchor_message_id=?",
                (session_id,anchor_message_id),
            )
            self.conn.commit()

    async def save_diary(self, day: str, title: str, content: str, mood_summary: str,
                         source_digest: str, created_at: float) -> None:
        """保存私人日记，并同步维护书柜中的同一日记文档及其修订。"""
        async with self._lock:
            with self._tx() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO diary_entries
                    (day,created_at,title,content,mood_summary,privacy,source_digest)
                    VALUES(?,?,?,?,?,'private',?)""",
                    (day,created_at,title[:120],content[:4000],mood_summary[:300],source_digest[:128]),
                )
                document_id=f"diary:{day}"
                conn.execute(
                    """INSERT OR IGNORE INTO bookshelf_documents
                    (id,doc_type,work_type,title,privacy,status,current_revision,source_kind,source_ref,summary,
                     created_at,updated_at,archived_at)
                    VALUES(?,'diary','',?,'private','archived',1,'diary',?,?,?,?,?)""",
                    (document_id,title[:120],day,mood_summary[:500],created_at,created_at,created_at),
                )
                conn.execute(
                    """INSERT OR IGNORE INTO bookshelf_revisions
                    (document_id,revision_number,stage,content,model_task,created_at)
                    VALUES(?,1,'diary',?,'diary',?)""",(document_id,content[:4000],created_at),
                )
                conn.execute("UPDATE memory_runtime SET last_diary_day=? WHERE id=1",(day,))

    async def get_diary(self, day: str) -> dict[str, Any]:
        async with self._lock:
            row=self.conn.execute("SELECT * FROM diary_entries WHERE day=?",(day,)).fetchone()
            return dict(row) if row else {}

    async def list_diaries(self, limit: int=7) -> list[dict[str, Any]]:
        async with self._lock:
            rows=self.conn.execute(
                "SELECT * FROM diary_entries ORDER BY day DESC LIMIT ?",(max(1,min(100,int(limit))),)
            ).fetchall()
            return [dict(row) for row in rows]

    async def scenes_for_day(self, day: str) -> list[dict[str, Any]]:
        async with self._lock:
            rows=self.conn.execute(
                """SELECT f.kind,f.summary,f.location,s.scene,s.applied
                FROM daily_framework f LEFT JOIN detailed_scenes s ON s.framework_id=f.id
                WHERE f.day=? ORDER BY f.start_minute""",(day,)
            ).fetchall()
            return [dict(row) for row in rows]

    async def interaction_counts(self, start: float, end: float) -> list[dict[str, Any]]:
        """日记只读取交互数量，不读取用户消息摘要或原文。"""
        async with self._lock:
            rows=self.conn.execute(
                """SELECT user_id,kind,COUNT(*) count FROM interaction_events
                WHERE created_at>=? AND created_at<? GROUP BY user_id,kind""",(start,end)
            ).fetchall()
            return [dict(row) for row in rows]

    async def add_important_date(self, user_id: str, event_name: str, event_date: str,
                                 recurrence: str, source: str, now: float,
                                 source_message_id: str="") -> int:
        """按用户、名称、日期和周期幂等保存明确的重要日期。"""
        clean_recurrence=recurrence if recurrence in {"none","annual"} else "none"
        async with self._lock:
            with self._tx() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO important_dates
                    (user_id,event_name,event_date,recurrence,source,created_at,updated_at,source_message_id)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (user_id,event_name[:120],event_date,clean_recurrence,source[:40],now,now,source_message_id[:240]),
                )
                row=conn.execute(
                    """SELECT id FROM important_dates
                    WHERE user_id=? AND event_name=? AND event_date=? AND recurrence=?""",
                    (user_id,event_name[:120],event_date,clean_recurrence),
                ).fetchone()
                return int(row[0]) if row else 0

    async def list_important_dates(self, user_id: str="") -> list[dict[str, Any]]:
        async with self._lock:
            if user_id:
                rows=self.conn.execute(
                    "SELECT * FROM important_dates WHERE user_id=? ORDER BY event_date,event_name",(user_id,)
                ).fetchall()
            else:
                rows=self.conn.execute("SELECT * FROM important_dates ORDER BY event_date,event_name").fetchall()
            return [dict(row) for row in rows]

    async def remove_important_date(self, date_id: int, user_id: str="") -> bool:
        async with self._lock:
            with self._tx() as conn:
                if user_id:
                    cursor=conn.execute("DELETE FROM important_dates WHERE id=? AND user_id=?",(date_id,user_id))
                else:
                    cursor=conn.execute("DELETE FROM important_dates WHERE id=?",(date_id,))
                if cursor.rowcount==1:
                    conn.execute("DELETE FROM date_trigger_events WHERE date_id=?",(date_id,))
                    conn.execute(
                        "DELETE FROM proactive_opportunities WHERE framework_id=? AND consumed_at=0",
                        (f"important-date:{date_id}",),
                    )
                return cursor.rowcount==1

    async def add_date_candidate(self, user_id: str, event_name: str, date_text: str,
                                 suggested_date: str, confidence: float, source_summary: str,
                                 now: float, source_message_id: str="") -> int:
        """保存仍需当前 QQ 用户确认的模糊日期候选及其有限来源摘要。"""
        async with self._lock:
            existing=self.conn.execute(
                """SELECT id FROM date_candidates WHERE user_id=? AND event_name=? AND date_text=?
                AND status='pending' ORDER BY created_at DESC LIMIT 1""",
                (user_id,event_name[:120],date_text[:120]),
            ).fetchone()
            if existing:return int(existing[0])
            cursor=self.conn.execute(
                """INSERT INTO date_candidates
                (user_id,event_name,date_text,suggested_date,confidence,source_summary,created_at,status,source_message_id)
                VALUES(?,?,?,?,?,?,?,'pending',?)""",
                (user_id,event_name[:120],date_text[:120],suggested_date[:10],max(0,min(1,float(confidence))),
                 source_summary[:300],now,source_message_id[:240]),
            )
            self.conn.commit(); return int(cursor.lastrowid)

    async def list_date_candidates(self, user_id: str, status: str="pending") -> list[dict[str, Any]]:
        async with self._lock:
            rows=self.conn.execute(
                """SELECT * FROM date_candidates WHERE user_id=? AND status=?
                ORDER BY created_at DESC""",(user_id,status)
            ).fetchall()
            return [dict(row) for row in rows]

    async def confirm_date_candidate(self, candidate_id: int, user_id: str,
                                     event_date: str, now: float) -> int:
        """候选确认与正式日期写入同一事务，避免出现半确认状态。"""
        async with self._lock:
            with self._tx() as conn:
                row=conn.execute(
                    "SELECT * FROM date_candidates WHERE id=? AND user_id=? AND status='pending'",
                    (candidate_id,user_id),
                ).fetchone()
                if not row:return 0
                recurrence="annual" if any(word in str(row["event_name"]) for word in ("生日","纪念日")) else "none"
                conn.execute(
                    """INSERT OR IGNORE INTO important_dates
                    (user_id,event_name,event_date,recurrence,source,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?)""",
                    (user_id,row["event_name"],event_date,recurrence,"confirmed_candidate",now,now),
                )
                saved=conn.execute(
                    """SELECT id FROM important_dates
                    WHERE user_id=? AND event_name=? AND event_date=? AND recurrence=?""",
                    (user_id,row["event_name"],event_date,recurrence),
                ).fetchone()
                conn.execute("UPDATE date_candidates SET status='confirmed' WHERE id=?",(candidate_id,))
                return int(saved[0]) if saved else 0

    async def memory_runtime(self) -> dict[str, Any]:
        async with self._lock:
            row=self.conn.execute("SELECT * FROM memory_runtime WHERE id=1").fetchone()
            return dict(row) if row else {}

    async def has_date_trigger(self, date_id: int, occurrence_date: str) -> bool:
        """该日期的某次发生是否已生成过任何提醒（用于错过当天的补算判断）。"""
        async with self._lock:
            row=self.conn.execute(
                "SELECT 1 FROM date_trigger_events WHERE date_id=? AND occurrence_date=? LIMIT 1",
                (int(date_id),str(occurrence_date)),
            ).fetchone()
            return row is not None

    async def reserve_date_trigger(self, date_id: int, occurrence_date: str,
                                   lead_days: int, now: float) -> bool:
        async with self._lock:
            cursor=self.conn.execute(
                """INSERT OR IGNORE INTO date_trigger_events
                (date_id,occurrence_date,lead_days,created_at) VALUES(?,?,?,?)""",
                (date_id,occurrence_date,lead_days,now),
            )
            self.conn.commit(); return cursor.rowcount==1

    async def add_date_opportunity_once(self, date_id: int, occurrence_date: str, lead_days: int,
                                        item: dict[str, Any], now: float) -> bool:
        """日期触发标记和专属主动契机必须一起成功或一起回滚。"""
        async with self._lock:
            with self._tx() as conn:
                cursor=conn.execute(
                    """INSERT OR IGNORE INTO date_trigger_events
                    (date_id,occurrence_date,lead_days,created_at) VALUES(?,?,?,?)""",
                    (date_id,occurrence_date,lead_days,now),
                )
                if cursor.rowcount!=1:return False
                conn.execute(
                    """INSERT OR IGNORE INTO proactive_opportunities
                    (id,framework_id,topic,motive,weight,privacy,target_user_id,expires_at)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (item["id"],item["framework_id"],item["topic"],item["motive"],item["weight"],
                     item.get("privacy","target_only"),item.get("target_user_id",""),item["expires_at"]),
                )
                return True

    async def cleanup_date_candidates(self, before: float) -> None:
        async with self._lock:
            self.conn.execute(
                "DELETE FROM date_candidates WHERE status='pending' AND created_at<?",(before,)
            )
            self.conn.execute("UPDATE memory_runtime SET last_cleanup_at=? WHERE id=1",(time.time(),))
            self.conn.commit()

    async def upsert_news_item(self, item: dict[str, Any]) -> bool:
        """写入新闻；正文哈希变化时清除旧关联分数，强制重新进行自我关联。"""
        async with self._lock:
            existing=self.conn.execute("SELECT content_hash FROM news_items WHERE id=?",(item["id"],)).fetchone()
            self.conn.execute(
                """INSERT INTO news_items
                (id,source_id,title,url,summary,content,published_at,fetched_at,content_hash,expires_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                source_id=excluded.source_id,title=excluded.title,url=excluded.url,
                summary=excluded.summary,content=excluded.content,published_at=excluded.published_at,
                fetched_at=excluded.fetched_at,
                relevance_score=CASE WHEN news_items.content_hash<>excluded.content_hash THEN 0 ELSE news_items.relevance_score END,
                relevance_reason=CASE WHEN news_items.content_hash<>excluded.content_hash THEN '' ELSE news_items.relevance_reason END,
                associated_at=CASE WHEN news_items.content_hash<>excluded.content_hash THEN 0 ELSE news_items.associated_at END,
                opportunity_id=CASE WHEN news_items.content_hash<>excluded.content_hash THEN '' ELSE news_items.opportunity_id END,
                content_hash=excluded.content_hash,expires_at=excluded.expires_at""",
                (item["id"],item["source_id"][:120],item["title"][:500],item["url"][:2000],
                 item.get("summary","")[:3000],item.get("content","")[:16000],float(item.get("published_at") or 0),
                 float(item["fetched_at"]),item["content_hash"][:80],float(item["expires_at"])),
            )
            self.conn.commit(); return existing is None or str(existing[0])!=str(item["content_hash"])

    async def pending_news_items(self, now: float, limit: int=20) -> list[dict[str, Any]]:
        async with self._lock:
            rows=self.conn.execute(
                """SELECT * FROM news_items WHERE associated_at=0 AND expires_at>?
                ORDER BY published_at DESC,fetched_at DESC LIMIT ?""",(now,max(1,min(100,int(limit))))
            ).fetchall()
            return [dict(row) for row in rows]

    async def mark_news_associated(self, item_id: str, score: float, reason: str,
                                   opportunity_id: str, now: float) -> None:
        async with self._lock:
            self.conn.execute(
                """UPDATE news_items SET relevance_score=?,relevance_reason=?,associated_at=?,opportunity_id=?
                WHERE id=?""",(max(0,min(1,float(score))),reason[:1000],now,opportunity_id[:80],item_id)
            )
            self.conn.commit()

    async def update_news_summary(self, item_id: str, summary: str) -> None:
        async with self._lock:
            self.conn.execute("UPDATE news_items SET summary=? WHERE id=?",(summary[:3000],item_id))
            self.conn.commit()

    async def recent_news_items(self, now: float, limit: int=10, *, associated_only: bool=False) -> list[dict[str, Any]]:
        async with self._lock:
            clause=" AND associated_at>0" if associated_only else ""
            rows=self.conn.execute(
                f"""SELECT * FROM news_items WHERE expires_at>?{clause}
                ORDER BY published_at DESC,fetched_at DESC LIMIT ?""",(now,max(1,min(100,int(limit))))
            ).fetchall()
            return [dict(row) for row in rows]

    async def save_exploration_note(self, item: dict[str, Any]) -> None:
        async with self._lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO exploration_notes
                (id,topic,query,summary,source_urls,created_at,relevance_score,relevance_reason,opportunity_id,expires_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (item["id"],item["topic"][:240],item["query"][:240],item["summary"][:5000],
                 json.dumps(item.get("source_urls") or [],ensure_ascii=False),float(item["created_at"]),
                 max(0,min(1,float(item.get("relevance_score") or 0))),item.get("relevance_reason","")[:1000],
                 item.get("opportunity_id","")[:80],float(item["expires_at"])),
            )
            self.conn.commit()

    async def exploration_count(self, start: float, end: float) -> int:
        async with self._lock:
            row=self.conn.execute(
                "SELECT COUNT(*) FROM exploration_notes WHERE created_at>=? AND created_at<?",(start,end)
            ).fetchone()
            return int(row[0]) if row else 0

    async def recent_exploration_notes(self, now: float, limit: int=10) -> list[dict[str, Any]]:
        async with self._lock:
            rows=self.conn.execute(
                """SELECT * FROM exploration_notes WHERE expires_at>?
                ORDER BY created_at DESC LIMIT ?""",(now,max(1,min(100,int(limit))))
            ).fetchall()
            result=[]
            for row in rows:
                item=dict(row)
                try:item["source_urls"]=json.loads(item.get("source_urls") or "[]")
                except (TypeError,json.JSONDecodeError):item["source_urls"]=[]
                result.append(item)
            return result

    async def get_information_source_runtime(self, source_key: str) -> dict[str, Any]:
        async with self._lock:
            row=self.conn.execute(
                "SELECT * FROM information_source_runtime WHERE source_key=?",(source_key,)
            ).fetchone()
            return dict(row) if row else {"source_key":source_key,"last_attempt_at":0,"last_success_at":0,
                                          "failure_count":0,"next_retry_at":0,"last_error":"","etag":"","last_modified":""}

    async def save_information_source_runtime(self, source_key: str, *, now: float, success: bool,
                                              next_retry_at: float, error: str="", etag: str="",
                                              last_modified: str="") -> None:
        """保存来源退避状态，同时保留最近成功时间和可复用的 HTTP 缓存头。"""
        async with self._lock:
            previous=self.conn.execute(
                "SELECT * FROM information_source_runtime WHERE source_key=?",(source_key,)
            ).fetchone()
            failures=0 if success else int(previous["failure_count"] if previous else 0)+1
            last_success=now if success else float(previous["last_success_at"] if previous else 0)
            old_etag=str(previous["etag"] if previous else ""); old_modified=str(previous["last_modified"] if previous else "")
            self.conn.execute(
                """INSERT OR REPLACE INTO information_source_runtime
                (source_key,last_attempt_at,last_success_at,failure_count,next_retry_at,last_error,etag,last_modified)
                VALUES(?,?,?,?,?,?,?,?)""",
                (source_key[:200],now,last_success,failures,next_retry_at,"" if success else error[:500],
                 etag or old_etag,last_modified or old_modified),
            )
            self.conn.commit()

    async def reconcile_search_keys(self,entries:list[tuple[str,str]],*,reset_existing:bool=False)->None:
        """配置热更新后只保留仍存在的 Key 指纹，原始 Key 从不入库。"""
        allowed={(str(provider),str(key)) for provider,key in entries if provider and key}
        async with self._lock:
            with self._tx() as conn:
                if reset_existing:
                    conn.execute("DELETE FROM search_key_runtime")
                    return
                rows=conn.execute("SELECT provider_id,key_fingerprint FROM search_key_runtime").fetchall()
                for row in rows:
                    if (str(row[0]),str(row[1])) not in allowed:
                        conn.execute("DELETE FROM search_key_runtime WHERE provider_id=? AND key_fingerprint=?",(row[0],row[1]))

    async def get_search_key_runtime(self,provider_id:str,key_fingerprint:str)->dict[str,Any]:
        async with self._lock:
            row=self.conn.execute(
                "SELECT * FROM search_key_runtime WHERE provider_id=? AND key_fingerprint=?",
                (provider_id,key_fingerprint),
            ).fetchone()
            return dict(row) if row else {"provider_id":provider_id,"key_fingerprint":key_fingerprint,
                "status":"healthy","cooldown_until":0,"failure_count":0,"last_error_class":"",
                "last_used_at":0,"last_success_at":0}

    async def save_search_key_runtime(self,provider_id:str,key_fingerprint:str,*,status:str,
                                      cooldown_until:float,failure_count:int,error_class:str,
                                      used_at:float,success_at:float=0)->None:
        async with self._lock:
            previous=self.conn.execute(
                "SELECT last_success_at FROM search_key_runtime WHERE provider_id=? AND key_fingerprint=?",
                (provider_id,key_fingerprint),
            ).fetchone()
            last_success=float(success_at or (previous[0] if previous else 0))
            self.conn.execute(
                """INSERT OR REPLACE INTO search_key_runtime
                (provider_id,key_fingerprint,status,cooldown_until,failure_count,last_error_class,last_used_at,last_success_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (provider_id[:80],key_fingerprint[:32],status[:24],float(cooldown_until),max(0,int(failure_count)),
                 error_class[:80],float(used_at),last_success),
            ); self.conn.commit()

    async def record_search_api_event(self,*,created_at:float,operation:str,provider_id:str,
                                      provider_type:str,key_fingerprint:str,success:bool,status_code:int,
                                      latency_ms:float,result_count:int,error_class:str)->None:
        async with self._lock:
            self.conn.execute(
                """INSERT INTO search_api_events
                (created_at,operation,provider_id,provider_type,key_fingerprint,success,status_code,
                 latency_ms,result_count,error_class) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (float(created_at),operation[:40],provider_id[:80],provider_type[:40],key_fingerprint[:32],
                 int(success),int(status_code),float(latency_ms),max(0,int(result_count)),error_class[:80]),
            ); self.conn.commit()

    async def record_search_history(self,*,created_at:float,operation:str,query:str,
                                    provider_type:str,success:bool,result_count:int,
                                    error_class:str,duration_ms:float,
                                    results:list[dict[str,Any]],expires_at:float)->None:
        """保存一条逻辑搜索历史：一次降级链共享 event_at，含清洗后的查询词与有限结果摘要。"""
        history_id=hashlib.sha1(
            f"{float(created_at)}:{operation}:{query}".encode("utf-8","ignore")
        ).hexdigest()[:20]
        async with self._lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO search_history
                (id,created_at,operation,query,provider_type,success,result_count,error_class,
                 duration_ms,results,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (history_id,float(created_at),operation[:40],str(query)[:300],provider_type[:40],
                 int(success),max(0,int(result_count)),error_class[:80],float(duration_ms),
                 json.dumps(results[:20],ensure_ascii=False),float(expires_at)),
            ); self.conn.commit()

    async def recent_search_history(self,now:float,limit:int=20,*,operation:str="")->list[dict[str,Any]]:
        """按时间倒序读取本地搜索历史；operation 留空时返回全部来源。"""
        bounded=max(1,min(100,int(limit)))
        async with self._lock:
            if operation:
                rows=self.conn.execute(
                    """SELECT * FROM search_history WHERE expires_at>? AND operation=?
                    ORDER BY created_at DESC LIMIT ?""",(float(now),operation[:40],bounded)
                ).fetchall()
            else:
                rows=self.conn.execute(
                    """SELECT * FROM search_history WHERE expires_at>?
                    ORDER BY created_at DESC LIMIT ?""",(float(now),bounded)
                ).fetchall()
            result=[]
            for row in rows:
                item=dict(row)
                try:item["results"]=json.loads(item.get("results") or "[]")
                except (TypeError,json.JSONDecodeError):item["results"]=[]
                result.append(item)
            return result

    async def search_history_count(self,operation:str,start:float,end:float)->int:
        async with self._lock:
            row=self.conn.execute(
                """SELECT COUNT(*) FROM search_history WHERE operation=? AND created_at>=? AND created_at<?""",
                (operation[:40],float(start),float(end)),
            ).fetchone()
            return int(row[0]) if row else 0

    async def search_api_summary(self,since:float)->list[dict[str,Any]]:
        async with self._lock:
            rows=self.conn.execute(
                """SELECT provider_type,COUNT(*) calls,SUM(success) successes,
                SUM(result_count) results,AVG(latency_ms) average_latency_ms,
                MAX(created_at) last_at FROM search_api_events WHERE created_at>=?
                GROUP BY provider_type ORDER BY calls DESC""",(float(since),)
            ).fetchall()
            return [dict(row) for row in rows]

    async def search_success_count(self,operation:str,start:float,end:float)->int:
        """成功事件一一对应成功的逻辑搜索，失败的 Key 尝试不会占用每日额度。"""
        async with self._lock:
            row=self.conn.execute(
                """SELECT COUNT(*) FROM search_api_events
                WHERE operation=? AND success=1 AND created_at>=? AND created_at<?""",
                (operation[:40],float(start),float(end)),
            ).fetchone()
            return int(row[0]) if row else 0

    async def search_attempt_count(self,operation:str,start:float,end:float)->int:
        """同一次降级链共享 event_at，按逻辑搜索计数而不是按 Key 请求计数。"""
        async with self._lock:
            row=self.conn.execute(
                """SELECT COUNT(DISTINCT created_at) FROM search_api_events
                WHERE operation=? AND created_at>=? AND created_at<?""",
                (operation[:40],float(start),float(end)),
            ).fetchone()
            return int(row[0]) if row else 0

    async def search_provider_health(self)->list[dict[str,Any]]:
        async with self._lock:
            rows=self.conn.execute(
                """SELECT provider_id,key_fingerprint,status,cooldown_until,failure_count,
                last_error_class,last_used_at,last_success_at FROM search_key_runtime
                ORDER BY provider_id,key_fingerprint"""
            ).fetchall()
            return [dict(row) for row in rows]

    async def cleanup_information(self, now: float) -> None:
        async with self._lock:
            with self._tx() as conn:
                conn.execute("DELETE FROM news_items WHERE expires_at<=?",(now,))
                conn.execute("DELETE FROM exploration_notes WHERE expires_at<=?",(now,))
                conn.execute("DELETE FROM search_api_events WHERE created_at<=?",(now-90*86400,))
                conn.execute("DELETE FROM search_history WHERE expires_at<=?",(now,))

    # 每个自然日只结算一次关系温度，离线补算时按结算日结束时刻判断冷却。
    async def update_relationships(self, day: str, day_start: float, day_end: float, now: float) -> None:
        """按日结算互动增量和长期不活跃衰减，并保证重复补算不会再次修改温度。"""
        del now
        async with self._lock:
            with self._tx() as conn:
                users=conn.execute("SELECT * FROM users WHERE enabled=1").fetchall()
                for user in users:
                    if str(user["last_relation_day"] or "")>=day: continue
                    msg_count=conn.execute(
                        "SELECT COUNT(*) FROM interaction_events WHERE user_id=? AND kind='message' AND created_at>=? AND created_at<?",
                        (user["user_id"],day_start,day_end),
                    ).fetchone()[0]
                    responded=conn.execute(
                        "SELECT 1 FROM interaction_events WHERE user_id=? AND kind='proactive_response' AND created_at>=? AND created_at<? LIMIT 1",
                        (user["user_id"],day_start,day_end),
                    ).fetchone()
                    delta=(0.5 if msg_count>0 else 0)+(0.5 if msg_count>=5 else 0)+(0.5 if responded else 0)
                    latest_before=conn.execute(
                        """SELECT MAX(created_at) FROM interaction_events
                        WHERE user_id=? AND kind='message' AND created_at<?""",
                        (user["user_id"],day_end),
                    ).fetchone()[0]
                    if msg_count==0 and latest_before and day_end-float(latest_before)>7*86400:
                        delta=-0.4
                    current_temperature=float(user["temperature"])
                    floor=min(10.0,current_temperature) if delta<0 else 0.0
                    temperature=max(floor,min(100.0,current_temperature+delta))
                    conn.execute("UPDATE users SET temperature=?,last_relation_day=? WHERE user_id=?",(temperature,day,user["user_id"]))

    async def first_interaction_day(self, user_id: str) -> str:
        """该用户最早一条互动所在的自然日（YYYY-MM-DD）；无互动返回空串。

        供关系补算回看到建档/首次互动日，避免新用户前几天的增益永久丢失。
        """
        if not user_id:
            return ""
        async with self._lock:
            row=self.conn.execute(
                "SELECT MIN(created_at) FROM interaction_events WHERE user_id=? AND kind='message'",
                (str(user_id),),
            ).fetchone()
            if not row or not row[0]:
                return ""
            return time.strftime("%Y-%m-%d", time.localtime(float(row[0])))

    async def get_weather(self) -> dict[str, Any]:
        async with self._lock:
            row=self.conn.execute("SELECT * FROM weather_cache WHERE id=1").fetchone()
            if not row: return {}
            data=dict(row)
            try:data["raw_json"]=json.loads(data.get("raw_json") or "{}")
            except (TypeError,json.JSONDecodeError):data["raw_json"]={}
            return data

    async def save_weather(self, data: dict[str, Any]) -> None:
        async with self._lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO weather_cache
                (id,fetched_at,location_name,latitude,longitude,temperature,weather_code,description,raw_json)
                VALUES(1,?,?,?,?,?,?,?,?)""",
                (data["fetched_at"],data["location_name"],data["latitude"],data["longitude"],
                 data.get("temperature"),data.get("weather_code"),data["description"],json.dumps(data.get("raw_json",{}),ensure_ascii=False)),
            ); self.conn.commit()

    async def clear_weather(self) -> None:
        async with self._lock:
            self.conn.execute("DELETE FROM weather_cache WHERE id=1")
            self.conn.commit()
