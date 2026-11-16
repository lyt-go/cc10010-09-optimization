"""长链依赖与运行水位示例数据。

依赖关系本身仍然落在 dependency 表里：这里给出的行在启动时会并入该表，
因此依赖解析模块的清单、状态流转与导出口径都不变。运行水位、批次进度和
解除水位的记录单独成表，供长链查询服务按到达序号归并。

约定：
- ``到达序号`` 是全局单调的到达顺序，快照只纳入 <= 快照到达高水位的数据；
  较晚到达的完成/解除事件因此不会改写已经固定下来的旧游标。
- ``水位口径版本`` 表示等待汇总口径。口径升级后按新版本重算，解除事件仍然
  作为墓碑保留，已解除关系不会被重新计入等待。
"""
from __future__ import annotations

from typing import Any


def _relation(
    relation_no: str,
    upstream: str,
    downstream: str,
    chain_id: str,
    stage: int,
    *,
    status: str = "已生效",
    relation_type: str = "主依赖",
    offset: str = "5",
    timeout: str = "60",
    version: int = 1,
    effective_seq: int = 1,
) -> dict[str, Any]:
    return {
        "status": status,
        "pending": status != "已解除",
        "abnormal": status == "已解除",
        "关系编号": relation_no,
        "上游作业": upstream,
        "下游作业": downstream,
        "依赖类型": relation_type,
        "偏移量分钟": offset,
        "触发条件": "上游成功",
        "等待超时": timeout,
        "跳过标志": "否",
        "链路编号": chain_id,
        "链路序号": stage,
        "历史版本": version,
        "生效水位": effective_seq,
    }


CHAIN_RELATIONS: list[dict[str, Any]] = [
    # LC-001：一条跨 3 个运行批次的 8 段长链，串起 9 个作业。
    _relation("CHAIN-0001", "ODS_ORDER", "STG_ORDER", "LC-001", 1, status="已解除", version=1),
    _relation("CHAIN-0002", "ODS_ORDER", "STG_ORDER", "LC-001", 1, version=2),
    _relation("CHAIN-0003", "STG_ORDER", "DWD_ORDER", "LC-001", 2),
    _relation("CHAIN-0004", "DWD_ORDER", "DWD_ORDER_DETAIL", "LC-001", 3),
    _relation("CHAIN-0005", "DWD_ORDER_DETAIL", "DWS_ORDER_DAY", "LC-001", 4),
    _relation("CHAIN-0006", "DWS_ORDER_DAY", "ADS_ORDER_DAY", "LC-001", 5),
    _relation("CHAIN-0007", "ADS_ORDER_DAY", "ADS_ORDER_SUM", "LC-001", 6),
    _relation("CHAIN-0008", "ADS_ORDER_SUM", "RPT_ORDER_BOARD", "LC-001", 7),
    _relation("CHAIN-0009", "RPT_ORDER_BOARD", "RPT_ORDER_FEED", "LC-001", 8),
    # LC-002：含同一对作业的两个历史版本，用来验证口径升级后的去重。
    _relation("CHAIN-0010", "ODS_USER", "STG_USER", "LC-002", 1),
    _relation("CHAIN-0011", "STG_USER", "DWD_USER", "LC-002", 2),
    _relation("CHAIN-0012", "DWD_USER", "DWS_USER_DAY", "LC-002", 3, version=1),
    _relation("CHAIN-0013", "DWD_USER", "DWS_USER_DAY", "LC-002", 3, version=2),
    _relation("CHAIN-0014", "DWS_USER_DAY", "ADS_USER_DAY", "LC-002", 4),
    _relation("CHAIN-0015", "ADS_USER_DAY", "RPT_USER_BOARD", "LC-002", 5),
    _relation("CHAIN-0016", "RPT_USER_BOARD", "RPT_USER_FEED", "LC-002", 6),
    # LC-003：支付链路，部分作业仍在等待。
    _relation("CHAIN-0017", "ODS_PAY", "STG_PAY", "LC-003", 1),
    _relation("CHAIN-0018", "STG_PAY", "DWD_PAY", "LC-003", 2),
    _relation("CHAIN-0019", "DWD_PAY", "DWS_PAY_DAY", "LC-003", 3),
    _relation("CHAIN-0020", "DWS_PAY_DAY", "ADS_PAY_DAY", "LC-003", 4),
    _relation("CHAIN-0021", "ADS_PAY_DAY", "RPT_PAY_BOARD", "LC-003", 5),
    # LC-004：商品链路。
    _relation("CHAIN-0022", "ODS_ITEM", "STG_ITEM", "LC-004", 1),
    _relation("CHAIN-0023", "STG_ITEM", "DWD_ITEM", "LC-004", 2),
    _relation("CHAIN-0024", "DWD_ITEM", "DWS_ITEM_DAY", "LC-004", 3),
    _relation("CHAIN-0025", "DWS_ITEM_DAY", "ADS_ITEM_DAY", "LC-004", 4),
]


CHAIN_BATCHES: list[dict[str, Any]] = [
    {
        "id": 1,
        "批次编号": "B-20260928-01",
        "水位序号": 12,
        "水位口径版本": 1,
        "状态": "已完成",
        "更新时间": "2026-09-28T02:10:00",
    },
    {
        "id": 2,
        "批次编号": "B-20260928-02",
        "水位序号": 9,
        "水位口径版本": 1,
        "状态": "已完成",
        "更新时间": "2026-09-28T06:20:00",
    },
    {
        "id": 3,
        "批次编号": "B-20260929-01",
        "水位序号": 5,
        "水位口径版本": 1,
        "状态": "运行中",
        "更新时间": "2026-09-29T01:05:00",
    },
]


def _run(
    batch: str,
    job: str,
    seq: int,
    status: str,
    arrived: int,
    occurred_at: str,
    epoch: int = 1,
) -> dict[str, Any]:
    return {
        "批次编号": batch,
        "作业名称": job,
        "水位序号": seq,
        "运行状态": status,
        "水位口径版本": epoch,
        "到达序号": arrived,
        "到达时间": occurred_at,
        "运行记录编号": f"RUNT-{batch}-{job}",
    }


# 运行水位数据：同一作业可能有多条来自不同批次的报告，快照按
# (水位序号, 到达序号, 批次编号) 的固定优先级挑一版，并在 watermark.conflicts
# 里说明被压下的版本。
RUN_WATERMARKS: list[dict[str, Any]] = [
    _run("B-20260928-01", "ODS_ORDER", 12, "成功", 101, "2026-09-28T00:10:00"),
    _run("B-20260928-01", "STG_ORDER", 12, "成功", 102, "2026-09-28T00:20:00"),
    _run("B-20260928-01", "DWD_ORDER", 12, "成功", 103, "2026-09-28T00:40:00"),
    _run("B-20260928-02", "DWD_ORDER", 6, "运行中", 104, "2026-09-28T05:10:00"),
    _run("B-20260928-01", "DWD_ORDER_DETAIL", 7, "运行中", 105, "2026-09-28T01:00:00"),
    _run("B-20260928-01", "DWS_ORDER_DAY", 4, "运行中", 106, "2026-09-28T01:20:00"),
    _run("B-20260928-01", "ADS_ORDER_DAY", 3, "运行中", 107, "2026-09-28T01:40:00"),
    _run("B-20260928-02", "ODS_USER", 9, "成功", 108, "2026-09-28T05:00:00"),
    _run("B-20260928-02", "STG_USER", 9, "成功", 109, "2026-09-28T05:10:00"),
    _run("B-20260928-02", "DWD_USER", 9, "运行中", 110, "2026-09-28T05:30:00"),
    _run("B-20260929-01", "DWD_USER", 2, "失败", 111, "2026-09-29T00:30:00"),
    _run("B-20260928-02", "DWS_USER_DAY", 5, "运行中", 112, "2026-09-28T06:00:00"),
    _run("B-20260928-01", "ODS_PAY", 12, "成功", 113, "2026-09-28T00:15:00"),
    _run("B-20260928-01", "STG_PAY", 12, "成功", 114, "2026-09-28T00:35:00"),
    _run("B-20260928-01", "DWD_PAY", 12, "成功", 115, "2026-09-28T01:05:00"),
    _run("B-20260928-01", "DWS_PAY_DAY", 8, "运行中", 116, "2026-09-28T01:25:00"),
    _run("B-20260928-01", "ODS_ITEM", 11, "成功", 117, "2026-09-28T00:18:00"),
    _run("B-20260928-02", "STG_ITEM", 8, "成功", 118, "2026-09-28T05:05:00"),
    _run("B-20260928-01", "DWD_ITEM", 11, "成功", 119, "2026-09-28T01:10:00"),
    _run("B-20260928-01", "DWS_ITEM_DAY", 6, "运行中", 120, "2026-09-28T01:30:00"),
    # 同一批次较早的重复上报：水位序号低于该批次当前水位，按冲突记录保留高水位。
    _run("B-20260928-02", "DWD_USER", 7, "运行中", 121, "2026-09-28T05:25:00"),
]


CHAIN_RELEASES: list[dict[str, Any]] = [
    {
        "关系编号": "CHAIN-0001",
        "批次编号": "B-20260928-01",
        "水位序号": 12,
        "水位口径版本": 1,
        "到达序号": 201,
        "解除原因": "历史版本由 CHAIN-0002 承接",
        "解除时间": "2026-09-28T02:15:00",
    },
    {
        "关系编号": "CHAIN-0005",
        "批次编号": "B-20260928-02",
        "水位序号": 9,
        "水位口径版本": 1,
        "到达序号": 202,
        "解除原因": "下游改由实时链路触发",
        "解除时间": "2026-09-28T06:30:00",
    },
    {
        "关系编号": "CHAIN-0021",
        "批次编号": "B-20260929-01",
        "水位序号": 5,
        "水位口径版本": 1,
        "到达序号": 203,
        "解除原因": "报表下线",
        "解除时间": "2026-09-29T01:10:00",
    },
]


CHAIN_BASELINES: list[dict[str, Any]] = [
    {
        "status": "已启用",
        "pending": False,
        "abnormal": False,
        "基线编号": "BASE-0004",
        "基线名称": "订单日终基线",
        "承诺完成时间": "2026-09-29T06:00:00",
        "预警余量分钟": "30",
        "破线级别": "P1",
        "关联作业列表": "ADS_ORDER_DAY,DWS_ORDER_DAY,RPT_ORDER_BOARD",
        "工作日标记": "是",
        "节假日偏移": "0",
    },
    {
        "status": "已启用",
        "pending": False,
        "abnormal": False,
        "基线编号": "BASE-0005",
        "基线名称": "用户日终基线",
        "承诺完成时间": "2026-09-29T07:00:00",
        "预警余量分钟": "45",
        "破线级别": "P2",
        "关联作业列表": "DWD_USER,DWS_USER_DAY,ADS_USER_DAY",
        "工作日标记": "是",
        "节假日偏移": "0",
    },
]


LONGCHAIN_TABLES: dict[str, list[dict[str, Any]]] = {
    "longchain_batch": CHAIN_BATCHES,
    "runtime_watermark": RUN_WATERMARKS,
    "dependency_release": CHAIN_RELEASES,
    "watermark_policy": [
        {
            "id": 1,
            "水位口径版本": 1,
            "口径说明": "按上游成功事件计入等待解除",
            "到达序号": 1,
            "生效时间": "2026-09-01T00:00:00",
        }
    ],
}
