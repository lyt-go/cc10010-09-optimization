"""长链依赖查询：把依赖关系与运行水位按统一游标窗口归并成一份稳定结果。

取数分三路，互相不覆盖：
- 依赖关系（dependency 表），带链路编号、链路序号、历史版本；
- 运行水位（runtime_watermark 表），同一作业可有多条不同批次的到达事件；
- 解除水位（dependency_release 表），关系一旦解除就以墓碑形式保留。

归并规则：
1. 游标固定快照到达高水位（``arrival_high``），只读取 ``到达序号 <= 高水位``
   的水位与解除事件；快照生成后到达的完成事件不会改写旧游标，旧页对象与
   新页总数因此始终来自同一水位。
2. 同一作业多批次水位冲突时按 ``水位序号、到达序号、批次编号`` 取最高一版，
   被压下的版本写进 ``watermark.conflicts``；同一段依赖的多个历史版本只保留
   版本最高的一条参与计数，其余版本放进 ``历史关系`` 承接，行不丢、数不重。
3. 等待口径按 ``水位口径版本`` 升级；解除是墓碑，不受重复到达和版本回溯影响，
   已解除关系永远不会再被计入等待结果。
4. 同一查询游标（快照到达高水位 + 口径 + 过滤条件）只生成一份结果并缓存，
   分页总数、页内状态、下游触发范围都从这份快照派生。

依赖解析与基线管理都从这里取同一份长链依赖查询结果，跨模块共享同一游标窗口。
"""
from __future__ import annotations

import base64
import hashlib
import json
import threading
from dataclasses import dataclass, field
from typing import Any

from app.store import store

RELATION_MODULE = "dependency"
WATERMARK_MODULE = "runtime_watermark"
RELEASE_MODULE = "dependency_release"
BATCH_MODULE = "longchain_batch"
POLICY_MODULE = "watermark_policy"

SUCCESS_STATUSES = {"成功", "已完成", "skipped"}
RELEASED = "已解除"
WAITING = "等待中"
SATISFIED = "已满足"
STATES = [WAITING, SATISFIED, RELEASED]
CHAIN_LABELS = {
    "LC-001": "订单日终链路",
    "LC-002": "用户日终链路",
    "LC-003": "支付日终链路",
    "LC-004": "商品日终链路",
}
# 保持与依赖解析清单一致的展示字段，额外字段只增不改。
RELATION_FIELDS = (
    "关系编号",
    "上游作业",
    "下游作业",
    "依赖类型",
    "偏移量分钟",
    "触发条件",
    "等待超时",
    "跳过标志",
)
SNAPSHOT_CACHE_LIMIT = 64


@dataclass(frozen=True)
class Snapshot:
    """一次查询固定的水位与归并结果。"""

    snapshot_id: str
    cursor: str
    arrival_high: int
    policy_version: int
    batch_watermarks: dict[str, int]
    watermarks: dict[str, dict[str, Any]]
    conflicts: list[dict[str, Any]]
    releases: dict[str, dict[str, Any]]
    relations: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    rows: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def watermark_payload(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "arrival_high": self.arrival_high,
            "policy_version": self.policy_version,
            "batches": dict(self.batch_watermarks),
            "conflicts": [dict(item) for item in self.conflicts],
        }


class LongChainQueryService:
    """长链依赖查询的单一数据来源。

    依赖解析的 /api/dependency/longchain 与基线管理的 /api/baseline/warnings
    都调用这里，共享同一个游标窗口；查询只读，不修改依赖表。
    """

    def __init__(self) -> None:
        self._snapshots: dict[str, Snapshot] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 水位
    def current_arrival(self) -> int:
        values = [
            int(row.get("到达序号", 0))
            for row in store.rows(WATERMARK_MODULE) + store.rows(RELEASE_MODULE)
        ]
        return max(values, default=0)

    def current_policy_version(self) -> int:
        return max(
            (int(row.get("水位口径版本", 1)) for row in store.rows(POLICY_MODULE)),
            default=1,
        )

    def _batch_watermarks(self) -> dict[str, int]:
        """每个批次的当前水位序号。"""
        marks: dict[str, int] = {}
        for row in store.rows(BATCH_MODULE):
            batch = str(row.get("批次编号", ""))
            marks[batch] = max(marks.get(batch, 0), int(row.get("水位序号", 0)))
        return marks

    def _resolve_cursor(
        self, cursor: str | None
    ) -> tuple[int, int, dict[str, int], int | None, int | None]:
        """把游标解析成 (到达高水位, 口径版本, 批次水位, 续读偏移, 页大小)。

        游标缺省时按当前到达水位与当前口径建新窗口。游标里连着快照当时的
        批次水位一起固定，因此批次水位推进后旧游标仍按旧窗口重放；携带
        arrival 的旧游标不超过当前到达水位；口径升级后强制按新口径重算
        （只升不降）。
        """
        now_arrival = self.current_arrival()
        now_policy = self.current_policy_version()
        now_batches = self._batch_watermarks()
        if not cursor:
            return now_arrival, now_policy, now_batches, None, None
        payload = _decode_cursor(cursor)
        if not payload:
            return now_arrival, now_policy, now_batches, None, None
        arrival = min(int(payload.get("arrival", now_arrival)), now_arrival)
        policy = max(int(payload.get("policy", now_policy)), now_policy)
        raw_batches = payload.get("batches")
        batches = (
            {str(key): int(value) for key, value in raw_batches.items()}
            if isinstance(raw_batches, dict)
            else now_batches
        )
        offset = payload.get("offset")
        size = payload.get("size")
        return (
            arrival,
            policy,
            batches,
            int(offset) if offset is not None else None,
            int(size) if size is not None else None,
        )

    # ------------------------------------------------------------------ 快照
    def _data_fingerprint(self) -> str:
        """关系表与解除表的结构指纹。

        快照按水位固定，但依赖关系被登记/改状态后旧快照不应继续复用；
        指纹参与 snapshot_id，同一数据状态下重复请求同一游标仍命中同一份结果。
        """
        relation_rows = [
            (
                row.get("关系编号"),
                row.get("status"),
                row.get("历史版本"),
                row.get("链路编号"),
                row.get("链路序号"),
            )
            for row in store.rows(RELATION_MODULE)
        ]
        release_rows = [
            (row.get("关系编号"), row.get("到达序号")) for row in store.rows(RELEASE_MODULE)
        ]
        return _digest({"relations": relation_rows, "releases": release_rows})

    def _snapshot(self, cursor: str | None) -> Snapshot:
        arrival_high, policy, batch_marks, _, _ = self._resolve_cursor(cursor)
        snapshot_id = _digest(
            {
                "arrival": arrival_high,
                "policy": policy,
                "batches": batch_marks,
                "data": self._data_fingerprint(),
            }
        )
        with self._lock:
            cached = self._snapshots.get(snapshot_id)
        if cached is not None:
            # 重复请求同一查询游标命中同一份快照，不重复展开关系。
            return cached

        watermarks, conflicts = self._watermarks(arrival_high, batch_marks, policy)
        releases = self._releases(arrival_high)
        relations = tuple(
            dict(row)
            for row in sorted(
                (row for row in store.rows(RELATION_MODULE) if str(row.get("链路编号") or "")),
                key=_relation_sort_key,
            )
        )
        snapshot = Snapshot(
            snapshot_id=snapshot_id,
            cursor=_encode_cursor(
                {"arrival": arrival_high, "policy": policy, "batches": batch_marks}
            ),
            arrival_high=arrival_high,
            policy_version=policy,
            batch_watermarks=batch_marks,
            watermarks=watermarks,
            conflicts=conflicts,
            releases=releases,
            relations=relations,
            rows=self._build_rows(relations, watermarks, releases, arrival_high, policy),
        )
        with self._lock:
            self._snapshots[snapshot_id] = snapshot
            while len(self._snapshots) > SNAPSHOT_CACHE_LIMIT:
                self._snapshots.pop(next(iter(self._snapshots)))
        return snapshot

    def _releases(self, arrival_high: int) -> dict[str, dict[str, Any]]:
        """按关系编号收解除墓碑：只认到达高水位之前的解除，重复到达保留最早一次。"""
        releases: dict[str, dict[str, Any]] = {}
        for row in sorted(
            store.rows(RELEASE_MODULE),
            key=lambda item: int(item.get("到达序号", 0)),
        ):
            if int(row.get("到达序号", 0)) > arrival_high:
                continue
            relation_no = str(row.get("关系编号", ""))
            if relation_no and relation_no not in releases:
                releases[relation_no] = dict(row)
        return releases

    def _watermarks(
        self,
        arrival_high: int,
        batch_marks: dict[str, int],
        policy: int,
    ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
        """归并运行水位，挑选每个作业唯一的一版。

        同一作业的多条到达事件里：
        - 口径高于当前快照口径的版本先挂起（按新口径重算时才会生效）；
        - 水位序号高于该批次当前水位的行视为过期上报，仅记录冲突；
        - 其余按 (水位序号, 到达序号, 批次编号) 取最高一版，被压下的版本
          记录成冲突明细，便于对账而不是静默丢弃。
        """
        candidates: dict[str, list[dict[str, Any]]] = {}
        conflicts: list[dict[str, Any]] = []
        for row in sorted(
            store.rows(WATERMARK_MODULE),
            key=lambda item: (int(item.get("到达序号", 0)), str(item.get("批次编号", ""))),
        ):
            arrival = int(row.get("到达序号", 0))
            if arrival > arrival_high:
                continue
            if int(row.get("水位口径版本", 1)) > policy:
                conflicts.append(_conflict(row, "口径版本高于当前快照，按新口径重算时纳入"))
                continue
            batch = str(row.get("批次编号", ""))
            seq = int(row.get("水位序号", 0))
            if seq > batch_marks.get(batch, seq):
                conflicts.append(_conflict(row, "水位序号高于批次当前水位，判为过期上报"))
                continue
            candidates.setdefault(str(row.get("作业名称", "")), []).append(dict(row))

        chosen: dict[str, dict[str, Any]] = {}
        for job, rows in candidates.items():
            rows.sort(key=_watermark_rank, reverse=True)
            winner = rows[0]
            chosen[job] = winner
            for loser in rows[1:]:
                conflicts.append(
                    _conflict(
                        loser,
                        f"与批次 {winner.get('批次编号')} 的水位冲突，保留更高水位版本",
                    )
                )
        return chosen, conflicts

    def _build_rows(
        self,
        relations: tuple[dict[str, Any], ...],
        watermarks: dict[str, dict[str, Any]],
        releases: dict[str, dict[str, Any]],
        arrival_high: int,
        policy: int,
    ) -> tuple[dict[str, Any], ...]:
        """把关系、水位、解除墓碑归并成唯一逻辑边；历史版本放进 历史关系。"""
        groups: dict[tuple[str, int, str, str], list[dict[str, Any]]] = {}
        for relation in relations:
            key = (
                str(relation.get("链路编号", "")),
                int(relation.get("链路序号", 0)),
                str(relation.get("上游作业", "")),
                str(relation.get("下游作业", "")),
            )
            groups.setdefault(key, []).append(relation)

        rows: list[dict[str, Any]] = []
        for key, versions in groups.items():
            versions.sort(key=_relation_version_key, reverse=True)
            representative = versions[0]
            history = [
                {
                    "关系编号": item.get("关系编号"),
                    "历史版本": int(item.get("历史版本", 1)),
                    "状态": self._edge_state(item, watermarks, releases),
                    "承接": item.get("status"),
                }
                for item in versions[1:]
            ]

            upstream = str(representative.get("上游作业", ""))
            downstream = str(representative.get("下游作业", ""))
            watermark = watermarks.get(upstream)
            row: dict[str, Any] = {
                field: representative.get(field) for field in RELATION_FIELDS
            }
            row.update(
                {
                    "id": representative.get("id"),
                    "链路编号": key[0],
                    "链路序号": key[1],
                    "历史版本": int(representative.get("历史版本", 1)),
                    "状态": self._edge_state(representative, watermarks, releases),
                    "上游水位序号": watermark.get("水位序号") if watermark else None,
                    "上游水位批次": watermark.get("批次编号") if watermark else None,
                    "上游运行状态": watermark.get("运行状态") if watermark else None,
                    "上游待到水位": watermark.get("水位序号") if watermark else None,
                    "历史关系": history,
                    "口径状态": self._relation_status(representative),
                    "游标高水位": arrival_high,
                    "水位口径版本": policy,
                }
            )
            rows.append(row)

        rows.sort(
            key=lambda item: (
                str(item["链路编号"]),
                int(item["链路序号"]),
                str(item["关系编号"]),
            )
        )
        return tuple(rows)

    @staticmethod
    def _relation_status(relation: dict[str, Any]) -> str:
        return str(relation.get("status", ""))

    def _edge_state(
        self,
        relation: dict[str, Any],
        watermarks: dict[str, dict[str, Any]],
        releases: dict[str, dict[str, Any]],
    ) -> str:
        """单条边的状态：解除墓碑优先，其次看上游运行水位是否成功。"""
        relation_no = str(relation.get("关系编号", ""))
        if relation_no in releases:
            return RELEASED
        if str(relation.get("status", "")) == RELEASED:
            return RELEASED
        upstream = str(relation.get("上游作业", ""))
        watermark = watermarks.get(upstream)
        if watermark is not None and str(watermark.get("运行状态", "")) in SUCCESS_STATUSES:
            return SATISFIED
        return WAITING

    # ------------------------------------------------------------------ 汇总
    def summarise(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按链路汇总等待与下游触发范围，口径与页内明细完全一致。"""
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row["链路编号"]), []).append(row)
        summaries: list[dict[str, Any]] = []
        for chain_id in sorted(grouped):
            chain_rows = grouped[chain_id]
            waiting = [row for row in chain_rows if row["状态"] == WAITING]
            satisfied = [row for row in chain_rows if row["状态"] == SATISFIED]
            released = [row for row in chain_rows if row["状态"] == RELEASED]
            summaries.append(
                {
                    "chain_id": chain_id,
                    "chain_name": CHAIN_LABELS.get(chain_id, chain_id),
                    "relation_count": len(chain_rows),
                    "waiting_count": len(waiting),
                    "satisfied_count": len(satisfied),
                    "released_count": len(released),
                    "downstream_triggered": sorted({str(row["下游作业"]) for row in satisfied}),
                    "downstream_pending": sorted({str(row["下游作业"]) for row in waiting}),
                    "head_job": chain_rows[0]["上游作业"] if chain_rows else None,
                    "tail_job": chain_rows[-1]["下游作业"] if chain_rows else None,
                    "watermark_seq": max(
                        (
                            int(row["上游水位序号"] or 0)
                            for row in chain_rows
                        ),
                        default=0,
                    ),
                }
            )
        return summaries

    # ------------------------------------------------------------------ 查询
    def query(
        self,
        *,
        chain_id: str | None = None,
        upstream: str | None = None,
        downstream: str | None = None,
        status: str | None = None,
        keyword: str | None = None,
        page: int | None = None,
        size: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """分页查询长链依赖结果。

        ``cursor`` 为空时按当前到达水位建新窗口；翻页时原样带回上一页的
        ``next_cursor``（或 ``cursor``），总数、页内状态与下游触发范围都来自
        同一份快照。``cursor`` 里带续读偏移时从最后稳定位置继续。
        """
        size = max(int(size or 0), 1)
        _, _, _, cursor_offset, cursor_size = self._resolve_cursor(cursor)
        if page is None and cursor_offset is not None:
            start = max(cursor_offset, 0)
            if cursor_size:
                size = cursor_size
        else:
            start = max(int(page or 1) - 1, 0) * size

        snapshot = self._snapshot(cursor)
        scope = {
            "chain_id": chain_id or "",
            "upstream": upstream or "",
            "downstream": downstream or "",
            "status": status or "",
            "keyword": keyword or "",
        }
        rows = _filter_rows(list(snapshot.rows), scope)
        total = len(rows)
        page_rows = rows[start : start + size]
        summaries = self.summarise(rows)
        current_page = start // size + 1

        next_cursor = _encode_cursor(
            {
                "arrival": snapshot.arrival_high,
                "policy": snapshot.policy_version,
                "batches": snapshot.batch_watermarks,
                "offset": start + len(page_rows),
                "size": size,
            }
        )
        has_more = start + len(page_rows) < total
        return {
            "items": [dict(row) for row in page_rows],
            "total": total,
            "page": current_page,
            "size": size,
            "cursor": snapshot.cursor,
            "next_cursor": next_cursor,
            "watermark": snapshot.watermark_payload(),
            "summary": summaries,
            "total_waiting": sum(item["waiting_count"] for item in summaries),
            "warning_count": sum(1 for row in rows if row["状态"] == WAITING),
            "has_more": has_more,
        }

    def chain_detail(self, chain_id: str) -> dict[str, Any] | None:
        """读取单条长链的完整窗口结果；用于按链路定位。"""
        result = self.query(chain_id=chain_id, size=200)
        if not result["items"]:
            return None
        result["has_more"] = False
        result["next_cursor"] = result["cursor"]
        return result

    def locate(self, job: str) -> dict[str, Any]:
        """按作业定位它在各条长链里的上下游位置与水位。"""
        needle = job.strip()
        result = self.query(size=200)
        positions: list[dict[str, Any]] = []
        for row in result["items"]:
            upstream = str(row.get("上游作业", ""))
            downstream = str(row.get("下游作业", ""))
            if needle not in (upstream, downstream):
                continue
            role = "上游作业" if upstream == needle else "下游作业"
            positions.append(
                {
                    "链路编号": row["链路编号"],
                    "链路序号": row["链路序号"],
                    "关系编号": row["关系编号"],
                    "定位": role,
                    "上游作业": upstream,
                    "下游作业": downstream,
                    "状态": row["状态"],
                    "上游水位序号": row["上游水位序号"],
                    "上游水位批次": row["上游水位批次"],
                }
            )
        return {
            "job": needle,
            "cursor": result["cursor"],
            "watermark": result["watermark"],
            "positions": positions,
            "chains": sorted({str(item["链路编号"]) for item in positions}),
        }

    def all_rows(self, cursor: str | None = None) -> dict[str, Any]:
        """读取同一快照下的全量结果，供前端加载或跨模块对账。"""
        snapshot = self._snapshot(cursor)
        return {
            "items": [dict(row) for row in snapshot.rows],
            "total": len(snapshot.rows),
            "cursor": snapshot.cursor,
            "watermark": snapshot.watermark_payload(),
            "summary": self.summarise(list(snapshot.rows)),
        }

    def warnings(self, *, cursor: str | None = None) -> dict[str, Any]:
        """基线预警清单所需的等待视图，与分页查询共享同一游标窗口。"""
        snapshot = self._snapshot(cursor)
        waiting = [dict(row) for row in snapshot.rows if row["状态"] == WAITING]
        return {
            "cursor": snapshot.cursor,
            "watermark": snapshot.watermark_payload(),
            "total_waiting": len(waiting),
            "warnings": waiting,
            "summary": self.summarise(list(snapshot.rows)),
        }


def _relation_sort_key(row: dict[str, Any]) -> tuple[str, int, str]:
    return (
        str(row.get("链路编号", "")),
        int(row.get("链路序号", 0)),
        str(row.get("关系编号", "")),
    )


def _relation_version_key(row: dict[str, Any]) -> tuple[int, int]:
    return (int(row.get("历史版本", 1)), int(row.get("id", 0) or 0))


def _watermark_rank(row: dict[str, Any]) -> tuple[int, int, int]:
    batch = str(row.get("批次编号", ""))
    suffix = batch.rsplit("-", 1)[-1]
    return (
        int(row.get("水位序号", 0)),
        int(row.get("到达序号", 0)),
        int(suffix) if suffix.isdigit() else 0,
    )


def _conflict(row: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "作业名称": row.get("作业名称"),
        "批次编号": row.get("批次编号"),
        "水位序号": row.get("水位序号"),
        "到达序号": row.get("到达序号"),
        "水位口径版本": row.get("水位口径版本"),
        "原因": reason,
    }


def _filter_rows(rows: list[dict[str, Any]], scope: dict[str, str]) -> list[dict[str, Any]]:
    chain_id = scope.get("chain_id", "")
    upstream = scope.get("upstream", "")
    downstream = scope.get("downstream", "")
    status = scope.get("status", "")
    keyword = scope.get("keyword", "")
    filtered = rows
    if chain_id:
        filtered = [row for row in filtered if row["链路编号"] == chain_id]
    if upstream:
        filtered = [row for row in filtered if upstream in str(row.get("上游作业", ""))]
    if downstream:
        filtered = [row for row in filtered if downstream in str(row.get("下游作业", ""))]
    if status:
        filtered = [row for row in filtered if row["状态"] == status]
    if keyword:
        filtered = [
            row
            for row in filtered
            if keyword in str(row.get("关系编号", ""))
            or keyword in str(row.get("上游作业", ""))
            or keyword in str(row.get("下游作业", ""))
            or keyword in str(row.get("链路编号", ""))
        ]
    return filtered


def _digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding).decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# 依赖解析与基线管理共用这一份服务实例，保证两处取数对得上同一份查询结果。
longchain_service = LongChainQueryService()
