"""基线管理业务规则：状态流转、字段校验与筛选口径都收在这里。

预警清单不在这里另算等待口径——等待水位统一从长链依赖查询服务取，
保证基线与依赖解析看到的是同一份结果。
"""
from __future__ import annotations

from typing import Any

from app.services.longchain import longchain_service
from app.store import store

MODULE = "baseline"
REQUIRED_FIELDS = ["基线编号", "基线名称", "承诺完成时间"]
STATUS_ORDER = ["草稿", "基线评审中", "已启用", "已停用"]
ACTION_RULES = {"提交基线评审": "基线评审中", "启用基线": "已启用", "停用基线": "已停用"}
NEGATIVE_ACTIONS = ["停用基线"]


class BaselineService:
    def related_jobs(self, entry: dict[str, Any]) -> list[str]:
        """基线关联的作业列表，兼容逗号/顿号两种已有写法。"""
        raw = str(entry.get("关联作业列表") or "")
        for separator in ("，", "、", ";"):
            raw = raw.replace(separator, ",")
        return [part.strip() for part in raw.split(",") if part.strip()]

    def warning_view(self, *, cursor: str | None = None) -> dict[str, Any]:
        """把长链等待水位映射到基线预警；与依赖解析共享同一游标。"""
        view = longchain_service.warnings(cursor=cursor)
        waiting = view["warnings"]
        summary_by_chain = {item["chain_id"]: item for item in view["summary"]}
        entries: list[dict[str, Any]] = []
        for baseline in self.list_entries(page=1, size=1000)[0]:
            jobs = self.related_jobs(baseline)
            chain_ids = sorted(
                {
                    str(row["链路编号"])
                    for row in waiting
                    if str(row.get("上游作业")) in jobs or str(row.get("下游作业")) in jobs
                }
            )
            waiting_count = sum(
                int(summary_by_chain.get(chain_id, {}).get("waiting_count", 0))
                for chain_id in chain_ids
            )
            entries.append(
                {
                    "基线编号": baseline.get("基线编号"),
                    "基线名称": baseline.get("基线名称"),
                    "承诺完成时间": baseline.get("承诺完成时间"),
                    "预警余量分钟": baseline.get("预警余量分钟"),
                    "破线级别": baseline.get("破线级别"),
                    "关联作业列表": jobs,
                    "关联链路": chain_ids,
                    "等待关系数": waiting_count,
                    "是否预警": waiting_count > 0,
                    "水位口径版本": view["watermark"]["policy_version"],
                    "游标高水位": view["watermark"]["arrival_high"],
                }
            )
        entries.sort(key=lambda item: (not item["是否预警"], str(item["基线编号"])))
        enriched = {**view, "warnings": entries, "waiting": waiting}
        return enriched

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("基线编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"基线定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于基线管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"基线定义已{action}"
