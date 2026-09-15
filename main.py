"""AstrBot plugin: 查询北京科技大学（iBeiKe）无课教室并渲染为图片。

数据来源为 iBeiKe 教务公开接口 https://jwgl-api.ibeike.work/rest_rooms ，无需任何鉴权。
插件每天在固定时间预取「当天 + 次日」共 6 个大节的数据并持久化，
/wk 与 /mrwk 指令优先复用缓存，缓存缺失时现查。
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import Any

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

API_BASE = "https://jwgl-api.ibeike.work"
API_PATH = "/rest_rooms"

SLOT_COUNT = 6                       # 每天 6 个大节
SLOT_LABELS = ["一大节", "二大节", "三大节", "四大节", "五大节", "六大节"]
WEEKDAY_CN = ("一", "二", "三", "四", "五", "六", "日")

CACHE_KEY = "free_rooms_cache"       # 插件 KV 存储键
DEFAULT_REFRESH_TIME = "07:00"
TICK_SECONDS = 30                    # 调度器轮询间隔（秒）
REQUEST_TIMEOUT = 20                 # 单个请求超时（秒）
REQUEST_RETRY = 2                    # 单个请求重试次数

HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
    ),
}

T2I_TMPL = """
<div class="card">
  <div class="title">{{ title }}</div>
  <div class="sub">{{ buildings|length }} 栋楼有教室空闲 · 数据更新于 {{ fetched_at }}</div>
  {% for b in buildings %}
  <div class="bname">{{ b.name }}</div>
  <table>
    <thead>
      <tr><th class="c0">楼层</th>{% for h in headers %}<th>{{ h }}</th>{% endfor %}</tr>
    </thead>
    <tbody>
      {% for row in b.rows %}
      <tr>
        <td class="c0">{{ row.floor }}层</td>
        {% for c in row.cells %}<td>{{ c }}</td>{% endfor %}
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% endfor %}
  <div class="foot">数据来源：iBeiKe 教务公开接口</div>
</div>
"""

T2I_STYLE = """
* { box-sizing: border-box; }
body { margin: 0; padding: 18px; background: #f5f6f8; color: #20242b;
       font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif; }
.card { background: #fff; border-radius: 12px; padding: 18px 20px 14px;
        width: fit-content; min-width: 680px; max-width: 1180px; }
.title { font-size: 22px; font-weight: 700; }
.sub { font-size: 12px; color: #8a9099; margin: 4px 0 10px; }
.bname { font-size: 15px; font-weight: 700; margin: 14px 0 6px; padding-left: 8px;
         border-left: 4px solid #3b82f6; line-height: 1.2; }
table { border-collapse: collapse; width: 100%; table-layout: fixed; }
th, td { border: 1px solid #e3e6ea; padding: 5px 7px; font-size: 13px;
         text-align: center; vertical-align: top; word-break: break-word; line-height: 1.45; }
th { background: #eef2f7; font-weight: 600; color: #48505c; }
td.c0 { width: 56px; background: #fafbfc; color: #6b7280; font-weight: 600; }
.foot { margin-top: 12px; font-size: 11px; color: #a0a6ae; text-align: right; }
"""


@register(
    "astrbot_plugin_chai",
    "maimote02-coder",
    "查询北科大无课教室，按楼栋/楼层/大节渲染为图片。",
    "1.0.0",
)
class ChaiPlugin(Star):
    """无课教室查询插件。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._cache: dict[str, dict[str, Any]] = {}
        self._http: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._last_run_date: str | None = None
        self._time_raw: str | None = None
        self._time_hhmm: tuple[int, int] = (7, 0)

    # --------------------------------------------------------- 生命周期
    async def initialize(self) -> None:
        """加载缓存并启动每日刷新调度器。"""
        stored = await self.get_kv_data(CACHE_KEY, {})
        self._cache = stored if isinstance(stored, dict) else {}
        self._http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT), headers=HEADERS
        )
        self._task = asyncio.create_task(self._scheduler_loop(), name="chai-free-room-scheduler")
        logger.info(
            "[chai] 无课教室插件已启动，缓存日期: %s",
            ", ".join(sorted(self._cache)) or "（空）",
        )

    async def terminate(self) -> None:
        """停止调度器、关闭连接并落盘缓存。"""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - 卸载阶段不应抛出
                logger.error("[chai] 调度器退出异常: %s", exc)
            self._task = None
        if self._http is not None and not self._http.closed:
            await self._http.close()
            self._http = None
        await self._save_cache()
        logger.info("[chai] 无课教室插件已停止")

    # --------------------------------------------------------- 指令
    @filter.command("wk", alias={"无课"})
    async def free_rooms_today(self, event: AstrMessageEvent):
        """查询今天的无课教室"""
        yield await self._build_reply(event, date.today())

    @filter.command("mrwk", alias={"明日无课"})
    async def free_rooms_tomorrow(self, event: AstrMessageEvent):
        """查询明天的无课教室"""
        yield await self._build_reply(event, date.today() + timedelta(days=1))

    # --------------------------------------------------------- 调度器
    async def _scheduler_loop(self) -> None:
        """轮询调度器：到点或缓存缺失时刷新，单次异常不中断循环。"""
        while True:
            try:
                await self._scheduler_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 定时任务必须长活
                logger.error("[chai] 定时刷新异常: %s", exc, exc_info=True)
            await asyncio.sleep(TICK_SECONDS)

    async def _scheduler_tick(self) -> None:
        """判断本轮是否需要刷新，需要则刷新今天与明天。"""
        now = datetime.now()
        today, tomorrow = now.date(), now.date() + timedelta(days=1)
        if self._last_run_date == today.isoformat():
            return

        hh, mm = self._refresh_time()
        reached = (now.hour, now.minute) >= (hh, mm)
        missing = not self._has(today) or not self._has(tomorrow)
        if not reached and not missing:
            return

        self._last_run_date = today.isoformat()
        logger.info("[chai] 开始刷新无课教室数据: %s / %s", today, tomorrow)
        await self._refresh(today, tomorrow)

    def _refresh_time(self) -> tuple[int, int]:
        """读取并缓存配置里的刷新时间，配置变更后自动生效。"""
        raw = str(self.config.get("refresh_time", DEFAULT_REFRESH_TIME) or DEFAULT_REFRESH_TIME)
        if raw != self._time_raw:
            self._time_raw = raw
            try:
                hh_s, mm_s = raw.strip().split(":")
                hh, mm = int(hh_s), int(mm_s)
                if not (0 <= hh <= 23 and 0 <= mm <= 59):
                    raise ValueError("out of range")
                self._time_hhmm = (hh, mm)
            except Exception:  # noqa: BLE001 - 配置错误不应影响插件运行
                logger.warning("[chai] refresh_time 配置无效: %r，回退为 %s", raw, DEFAULT_REFRESH_TIME)
                self._time_hhmm = (7, 0)
        return self._time_hhmm

    # --------------------------------------------------------- 数据获取
    async def _refresh(self, *days: date) -> None:
        """刷新指定日期并覆盖写入缓存，缓存只保留这些日期。"""
        keep = {d.isoformat() for d in days}
        merged = {k: v for k, v in self._cache.items() if k in keep}
        for d in days:
            try:
                slots = await self._fetch_day(d)
            except Exception as exc:  # noqa: BLE001 - 单日失败不影响另一天
                logger.error("[chai] 刷新 %s 无课教室失败: %s", d, exc)
                continue
            merged[d.isoformat()] = {
                "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "slots": slots,
            }
        self._cache = merged
        await self._save_cache()

    async def _fetch_day(self, d: date) -> dict[str, dict[str, dict[str, list[str]]]]:
        """并发查询某天 6 个大节的空闲教室。

        Args:
            d: 目标日期。

        Returns:
            形如 {"1": {楼栋: {楼层: [教室号]}}, ...}，按大节序号分组的字典。

        Raises:
            RuntimeError: 6 个大节全部查询失败时抛出。
        """
        results = await asyncio.gather(
            *(self._fetch_slot(d, slot) for slot in range(1, SLOT_COUNT + 1)),
            return_exceptions=True,
        )
        slots: dict[str, dict[str, dict[str, list[str]]]] = {}
        errors: list[str] = []
        for slot, result in enumerate(results, start=1):
            if isinstance(result, BaseException):
                errors.append("第%d大节(%s)" % (slot, result))
                slots[str(slot)] = {}
            else:
                slots[str(slot)] = result
        if len(errors) == SLOT_COUNT:
            raise RuntimeError("全部大节查询失败: " + "; ".join(errors))
        if errors:
            logger.warning("[chai] %s 部分大节查询失败: %s", d, "; ".join(errors))
        return slots

    async def _fetch_slot(self, d: date, slot: int) -> dict[str, dict[str, list[str]]]:
        """查询某天某个大节的空闲教室，失败时重试。

        Args:
            d: 目标日期。
            slot: 大节序号，1-6。

        Returns:
            {楼栋名: {楼层: [教室号, ...]}}。

        Raises:
            RuntimeError: 重试耗尽仍失败时抛出。
        """
        if self._http is None:
            raise RuntimeError("HTTP 会话尚未初始化")
        params = {"a": slot, "b": slot, "date": d.isoformat()}
        last_error = "unknown"
        for attempt in range(REQUEST_RETRY):
            try:
                async with self._http.get(API_BASE + API_PATH, params=params) as resp:
                    if resp.status != 200:
                        last_error = "HTTP %d" % resp.status
                    else:
                        return self._parse_payload(await resp.json(content_type=None))
            except Exception as exc:  # noqa: BLE001 - 统一转成重试
                last_error = repr(exc)
            await asyncio.sleep(0.5 * (attempt + 1))
        raise RuntimeError(last_error)

    @staticmethod
    def _parse_payload(payload: Any) -> dict[str, dict[str, list[str]]]:
        """把接口原始响应整理成 {楼栋: {楼层: [教室]}}，丢弃空楼层。"""
        buildings: dict[str, dict[str, list[str]]] = {}
        for block in (payload or {}).get("data") or []:
            name = block.get("name")
            if not name:
                continue
            floors: dict[str, list[str]] = {}
            for item in block.get("data") or []:
                rooms = [str(r) for r in (item.get("classroom") or []) if r]
                if rooms:
                    floors[str(item.get("floor", ""))] = rooms
            if floors:
                buildings[str(name)] = floors
        return buildings

    # --------------------------------------------------------- 缓存
    def _has(self, d: date) -> bool:
        """判断某天是否已有可用缓存。"""
        entry = self._cache.get(d.isoformat())
        return isinstance(entry, dict) and bool(entry.get("slots"))

    async def _ensure_day(self, d: date) -> dict[str, Any]:
        """取某天的数据：命中缓存直接返回，否则现查并写回缓存。"""
        if self._has(d):
            return self._cache[d.isoformat()]["slots"]
        async with self._lock:  # 避免同一时刻并发重复请求
            if self._has(d):
                return self._cache[d.isoformat()]["slots"]
            slots = await self._fetch_day(d)
            self._cache[d.isoformat()] = {
                "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "slots": slots,
            }
            await self._save_cache()
            return slots

    async def _save_cache(self) -> None:
        """把缓存写入插件 KV 存储，失败只记日志。"""
        try:
            await self.put_kv_data(CACHE_KEY, self._cache)
        except Exception as exc:  # noqa: BLE001 - 持久化失败不应打断主流程
            logger.error("[chai] 保存无课教室缓存失败: %s", exc)

    # --------------------------------------------------------- 渲染
    async def _build_reply(self, event: AstrMessageEvent, d: date):
        """查询并按图片返回某天的无课教室，渲染不可用时降级为纯文本。"""
        label = self._date_label(d)
        try:
            slots = await self._ensure_day(d)
        except Exception as exc:  # noqa: BLE001 - 网络/接口异常统一提示
            logger.error("[chai] 查询 %s 无课教室失败: %s", d, exc, exc_info=True)
            return event.plain_result("查询无课教室失败，请稍后重试。")

        buildings = self._transpose(slots)
        if not buildings:
            return event.plain_result("%s 暂时没有空闲教室。" % label)

        entry = self._cache.get(d.isoformat()) or {}
        try:
            url = await self.html_render(
                T2I_TMPL,
                {
                    "title": "%s 无课教室" % label,
                    "headers": SLOT_LABELS,
                    "buildings": buildings,
                    "fetched_at": entry.get("fetched_at", "未知"),
                    "style": T2I_STYLE,
                },
                options={"type": "jpeg", "quality": 90, "full_page": True},
            )
            return event.image_result(url)
        except Exception as exc:  # noqa: BLE001 - 文转图不可用时降级
            logger.error("[chai] 渲染无课教室图片失败: %s", exc, exc_info=True)
            return event.plain_result(self._text_fallback(label, buildings))

    @staticmethod
    def _transpose(slots: dict[str, Any]) -> list[dict[str, Any]]:
        """把「大节 -> 楼栋 -> 楼层」转成「楼栋 -> 楼层行 x 6 大节列」。"""
        grid: dict[str, dict[str, list[str]]] = {}
        for slot in range(1, SLOT_COUNT + 1):
            for name, floors in (slots.get(str(slot)) or {}).items():
                by_floor = grid.setdefault(name, {})
                for floor, rooms in floors.items():
                    by_floor.setdefault(floor, [""] * SLOT_COUNT)[slot - 1] = "、".join(rooms)

        result: list[dict[str, Any]] = []
        for name, floors in grid.items():
            rows = [
                {"floor": floor, "cells": cells}
                for floor, cells in sorted(floors.items(), key=lambda kv: ChaiPlugin._floor_key(kv[0]))
                if any(cells)  # 没有空教室的楼层不渲染
            ]
            if rows:  # 没有任何空闲楼层的楼栋也不渲染
                result.append({"name": name, "rows": rows})
        return result

    @staticmethod
    def _floor_key(floor: str) -> tuple[int, int, str]:
        """楼层排序键：数字楼层在前，带前缀的（如 B3、B10）在后并按其中数字排序。"""
        text = str(floor)
        if text.isdigit():
            return (0, int(text), "")
        digits = "".join(ch for ch in text if ch.isdigit())
        return (1, int(digits), text) if digits else (2, 0, text)

    @staticmethod
    def _date_label(d: date) -> str:
        """把日期格式化成 9月15日（周二）。"""
        return "%d月%d日（周%s）" % (d.month, d.day, WEEKDAY_CN[d.weekday()])

    @staticmethod
    def _text_fallback(label: str, buildings: list[dict[str, Any]]) -> str:
        """文转图不可用时的纯文本兜底输出。"""
        lines = ["%s 无课教室" % label]
        for building in buildings:
            lines.append("【%s】" % building["name"])
            for row in building["rows"]:
                cells = [
                    "%s %s" % (SLOT_LABELS[i], c) for i, c in enumerate(row["cells"]) if c
                ]
                lines.append("  %s层：%s" % (row["floor"], "；".join(cells)))
        return "\n".join(lines)
