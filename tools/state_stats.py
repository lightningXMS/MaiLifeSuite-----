"""Mai_life 状态动力学统计工具。

以只读模式打开插件 SQLite（不影响运行中的库），输出心情/精力/饥饿的每日轨迹、
睡眠阶段分布与心情事件汇总，供长期调参分析。仅用 Python 标准库，服务器可直接运行：

    python tools/state_stats.py /path/to/mai_life.db
    python tools/state_stats.py /path/to/mai_life.db --days 14 --csv snapshots.csv

说明：state_snapshots 由插件运行期间每小时记录一条，mood_events 记录互动加分事件；
两者都从部署新版本后开始积累，旧版本数据库中没有历史数据属正常。
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime

_PHASE_ZH = {"awake": "清醒", "woken": "被叫醒", "falling_asleep": "入睡中",
             "light_sleep": "浅睡", "deep_sleep": "深睡", "sleeping_again": "回笼"}
_EVENT_ZH = {"passive_reply": "被动回复成功", "proactive_reply": "主动发送成功",
             "creation_archived": "创作归档"}


def _local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts)


def _fmt_day(ts: float) -> str:
    return _local(ts).strftime("%Y-%m-%d")


def _avg(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _safe_rows(conn: sqlite3.Connection, sql: str, params: tuple = ()):
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        print(f"[跳过] 查询失败（可能是旧 Schema 或表尚未创建）：{exc}")
        return []


def main() -> int:
    parser = argparse.ArgumentParser(description="Mai_life 状态动力学统计")
    parser.add_argument("db", nargs="?", default="data/mai_life.db",
                        help="mai_life.db 路径（默认 data/mai_life.db，需在插件目录运行）")
    parser.add_argument("--days", type=int, default=30,
                        help="只统计最近 N 天（默认 30；0 表示全部）")
    parser.add_argument("--csv", default="", help="把逐小时快照导出为 CSV 文件")
    args = parser.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    try:
        conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        print(f"无法打开数据库 {args.db}: {exc}")
        return 1
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=3000")

    snapshots = [dict(row) for row in _safe_rows(
        conn, "SELECT * FROM state_snapshots ORDER BY ts")]
    if args.days > 0:
        latest = snapshots[-1]["ts"] if snapshots else 0
        cutoff = _local(latest).timestamp() - args.days * 86400 if latest else 0
        snapshots = [row for row in snapshots if row["ts"] >= cutoff]
    events = [dict(row) for row in _safe_rows(
        conn, "SELECT * FROM mood_events ORDER BY created_at")]
    if args.days > 0 and snapshots:
        cutoff_day = _fmt_day(snapshots[0]["ts"])
        events = [row for row in events if row["day"] >= cutoff_day]

    if not snapshots:
        print("没有状态快照：新版本插件尚未运行过（部署后每小时自动记录一条），暂无可统计数据。")
        return 0

    span = f"{_fmt_day(snapshots[0]['ts'])} ~ {_fmt_day(snapshots[-1]['ts'])}"
    print(f"== 状态快照 ==\n范围 {span}，共 {len(snapshots)} 条（每小时一条）\n")

    by_day: dict[str, list[dict]] = defaultdict(list)
    for row in snapshots:
        by_day[_fmt_day(row["ts"])].append(row)

    print("日期        快照 精力min/avg/max      饥饿min/avg/max      心情min/avg/max   心情起→止      睡眠阶段分布")
    for day, rows in sorted(by_day.items()):
        energies = [float(r["energy"]) for r in rows]
        hungers = [float(r["hunger"]) for r in rows]
        moods = [float(r["mood_valence"]) for r in rows]
        phases = defaultdict(int)
        for r in rows:
            phases[_PHASE_ZH.get(r["sleep_phase"], r["sleep_phase"])] += 1
        phase_text = " ".join(f"{name}{count}h" for name, count in sorted(phases.items(), key=lambda i: -i[1]))
        print(f"{day} {len(rows):4d}  {min(energies):5.1f}/{_avg(energies):5.1f}/{max(energies):5.1f}    "
              f"{min(hungers):5.1f}/{_avg(hungers):5.1f}/{max(hungers):5.1f}    "
              f"{min(moods):+.2f}/{_avg(moods):+.2f}/{max(moods):+.2f}  "
              f"{moods[0]:+.2f}→{moods[-1]:+.2f}  {phase_text}")

    all_moods = [float(r["mood_valence"]) for r in snapshots]
    print(f"\n整体：心情均值 {_avg(all_moods):+.3f}（区间 {min(all_moods):+.2f} ~ {max(all_moods):+.2f}），"
          f"精力均值 {_avg([float(r['energy']) for r in snapshots]):.1f}，"
          f"饥饿均值 {_avg([float(r['hunger']) for r in snapshots]):.1f}")
    below = sum(1 for value in all_moods if value < 0)
    print(f"心情为负的快照占比：{below}/{len(all_moods)}（{below / len(snapshots) * 100:.0f}%）\n")

    if events:
        print("== 心情事件 ==")
        by_kind: dict[str, list[dict]] = defaultdict(list)
        for row in events:
            by_kind[row["kind"]].append(row)
        for kind, rows in sorted(by_kind.items()):
            label = _EVENT_ZH.get(kind, kind)
            print(f"{label}：{len(rows)} 次，累计 {sum(float(r['delta']) for r in rows):+.2f}")
        by_event_day: dict[str, float] = defaultdict(float)
        for row in events:
            by_event_day[row["day"]] += float(row["delta"])
        recent = sorted(by_event_day.items())[-7:]
        print("最近每日加分：" + "；".join(f"{day} {total:+.2f}" for day, total in recent))
        print()
    else:
        print("== 心情事件 ==\n暂无记录（部署新版本后，回复/主动/创作成功会自动记账）\n")

    if args.csv:
        try:
            with open(args.csv, "w", encoding="utf-8-sig", newline="") as handle:
                handle.write("time,energy,hunger,mood_valence,mood_arousal,sleep_phase,current_activity\n")
                for row in snapshots:
                    handle.write(f"{_local(row['ts']).isoformat()},{row['energy']},{row['hunger']},"
                                 f"{row['mood_valence']},{row['mood_arousal']},"
                                 f"{row['sleep_phase']},\"{row['current_activity']}\"\n")
            print(f"快照已导出：{args.csv}")
        except OSError as exc:
            print(f"CSV 导出失败：{exc}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
