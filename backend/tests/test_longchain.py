"""长链依赖查询与基线预警的水位口径回归测试。

用标准库 unittest，不依赖 pytest/httpx：
    cd backend && python -m unittest discover -s tests -v
"""
from __future__ import annotations

import copy
import unittest

from app.services.baseline import BaselineService
from app.services.longchain import WAITING, longchain_service, _encode_cursor
from app.store import Store

# 每个用例前重置 store 与服务缓存，避免用例之间互相污染。
_BASE_TABLES = copy.deepcopy(Store()._tables)


def reset_store() -> Store:
    fresh = Store()
    fresh._tables = copy.deepcopy(_BASE_TABLES)
    import app.store as store_module

    store_module.store._tables = fresh._tables
    longchain_service._snapshots.clear()
    return fresh


def append_row(module: str, row: dict) -> None:
    from app.store import store

    store.rows(module).append(row)


def page_through(size: int) -> tuple[list[dict], list[str], list[int]]:
    """按 next_cursor 翻完全部页，返回 (明细, 各页 total, 快照集合)。"""
    rows: list[dict] = []
    totals: list[int] = []
    snapshots: list[str] = []
    first = longchain_service.query(page=1, size=size)
    cursor = first["next_cursor"]
    totals.append(first["total"])
    snapshots.append(first["watermark"]["snapshot_id"])
    rows.extend(first["items"])
    while first["has_more"]:
        result = longchain_service.query(cursor=cursor)
        cursor = result["next_cursor"]
        totals.append(result["total"])
        snapshots.append(result["watermark"]["snapshot_id"])
        rows.extend(result["items"])
        first = result
    return rows, totals, snapshots


class LongChainQueryTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()

    def test_paging_uses_one_watermark_and_never_repeats(self) -> None:
        rows, totals, snapshots = page_through(size=4)
        self.assertEqual(len({row["关系编号"] for row in rows}), len(rows))
        self.assertEqual(len(set(totals)), 1)
        self.assertEqual(len(set(snapshots)), 1)
        self.assertEqual(len(rows), totals[0])

    def test_repeating_same_cursor_returns_same_snapshot(self) -> None:
        first = longchain_service.query(page=1, size=6)
        again = longchain_service.query(page=1, size=6, cursor=first["cursor"])
        self.assertEqual(first["watermark"]["snapshot_id"], again["watermark"]["snapshot_id"])
        self.assertEqual([row["关系编号"] for row in first["items"]], [row["关系编号"] for row in again["items"]])
        self.assertEqual(first["summary"], again["summary"])

    def test_later_completion_does_not_rewrite_old_cursor(self) -> None:
        first = longchain_service.query(page=1, size=5)
        old_cursor = first["next_cursor"]
        old_total = first["total"]
        old_waiting = first["total_waiting"]

        append_row(
            "runtime_watermark",
            {
                "批次编号": "B-20260928-01",
                "作业名称": "DWS_ORDER_DAY",
                "水位序号": 12,
                "运行状态": "成功",
                "水位口径版本": 1,
                "到达序号": 900,
                "运行记录编号": "RUNT-late",
            },
        )
        resumed = longchain_service.query(cursor=old_cursor)
        self.assertEqual(resumed["watermark"]["snapshot_id"], first["watermark"]["snapshot_id"])
        self.assertEqual(resumed["total"], old_total)
        self.assertEqual(resumed["total_waiting"], old_waiting)

        fresh = longchain_service.query(page=1, size=100)
        self.assertGreater(fresh["watermark"]["arrival_high"], first["watermark"]["arrival_high"])
        self.assertLess(fresh["total_waiting"], old_waiting)

    def test_released_relations_never_return_to_waiting(self) -> None:
        append_row(
            "dependency_release",
            {
                "关系编号": "CHAIN-0006",
                "批次编号": "B-20260929-01",
                "水位序号": 5,
                "水位口径版本": 1,
                "到达序号": 901,
                "解除原因": "口径变更后解除",
                "解除时间": "2026-09-29T04:00:00",
            },
        )
        result = longchain_service.query(size=100)
        released = {row["关系编号"] for row in result["items"] if row["状态"] == "已解除"}
        self.assertIn("CHAIN-0006", released)
        self.assertEqual(
            result["total_waiting"],
            sum(item["waiting_count"] for item in result["summary"]),
        )
        warning_numbers = {row["关系编号"] for row in longchain_service.warnings()["warnings"]}
        self.assertFalse(warning_numbers & released)

    def test_release_before_snapshot_watermark_is_not_applied(self) -> None:
        early = _encode_cursor({"arrival": 150, "policy": 1})
        result = longchain_service.query(cursor=early, size=100)
        states = {row["关系编号"]: row["状态"] for row in result["items"]}
        self.assertEqual(states["CHAIN-0021"], WAITING)
        self.assertEqual(states["CHAIN-0005"], WAITING)

    def test_policy_upgrade_recomputes_old_cursor(self) -> None:
        first = longchain_service.query(page=1, size=5)
        append_row(
            "watermark_policy",
            {
                "id": 2,
                "水位口径版本": 2,
                "口径说明": "按上游完成事件计入等待解除",
                "到达序号": 910,
                "生效时间": "2026-09-29T05:00:00",
            },
        )
        recomputed = longchain_service.query(cursor=first["next_cursor"], size=5)
        self.assertEqual(recomputed["watermark"]["policy_version"], 2)

    def test_historical_versions_counted_once(self) -> None:
        result = longchain_service.query(chain_id="LC-002", size=100)
        keys = [(row["链路编号"], row["链路序号"], row["上游作业"], row["下游作业"]) for row in result["items"]]
        self.assertEqual(len(keys), len(set(keys)))
        superseded = [row for row in result["items"] if row["关系编号"] == "CHAIN-0013"][0]
        self.assertEqual([item["关系编号"] for item in superseded["历史关系"]], ["CHAIN-0012"])

    def test_watermark_conflict_keeps_higher_version(self) -> None:
        result = longchain_service.query(chain_id="LC-002", size=100)
        dwd_user = next(row for row in result["items"] if row["上游作业"] == "DWD_USER")
        self.assertEqual(dwd_user["上游水位批次"], "B-20260928-02")
        self.assertEqual(dwd_user["上游运行状态"], "运行中")
        conflict_jobs = {item["作业名称"] for item in result["watermark"]["conflicts"]}
        self.assertIn("DWD_USER", conflict_jobs)

    def test_state_partitions_total(self) -> None:
        total = longchain_service.query(size=1)["total"]
        counts = {
            state: longchain_service.query(status=state, size=100)["total"]
            for state in ("等待中", "已满足", "已解除")
        }
        self.assertEqual(sum(counts.values()), total)

    def test_total_waiting_matches_downstream_scope(self) -> None:
        result = longchain_service.query(size=100)
        waiting = longchain_service.query(status="等待中", size=100)["items"]
        pending = {job for item in result["summary"] for job in item["downstream_pending"]}
        self.assertEqual(result["total_waiting"], len(waiting))
        self.assertEqual(pending, {row["下游作业"] for row in waiting})

    def test_locate_and_filters(self) -> None:
        located = longchain_service.locate("DWD_ORDER")
        self.assertEqual(located["chains"], ["LC-001"])
        self.assertEqual({item["定位"] for item in located["positions"]}, {"上游作业", "下游作业"})
        filtered = longchain_service.query(upstream="ODS_", size=100)
        self.assertTrue(all(row["上游作业"].startswith("ODS_") for row in filtered["items"]))

    def test_baseline_warnings_share_cursor(self) -> None:
        dependency = longchain_service.query(page=1, size=5)
        baseline_view = BaselineService().warning_view(cursor=dependency["cursor"])
        self.assertEqual(
            baseline_view["watermark"]["snapshot_id"],
            dependency["watermark"]["snapshot_id"],
        )
        self.assertEqual(baseline_view["total_waiting"], dependency["total_waiting"])
        warned = [item for item in baseline_view["warnings"] if item["是否预警"]]
        self.assertTrue(warned)
        self.assertTrue(all(item["关联链路"] for item in warned))


if __name__ == "__main__":
    unittest.main()
