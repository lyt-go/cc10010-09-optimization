"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

from typing import Any

from app.chain_seed import CHAIN_BASELINES, CHAIN_RELATIONS, LONGCHAIN_TABLES
from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 长链关系并入依赖关系表：依赖解析的清单、筛选、导出以及状态流转都还是
        # 走同一张表，已有业务口径不变；链路编号、链路序号、历史版本等字段只在
        # 长链查询里参与归并。
        dependency_rows = self._tables.setdefault("dependency", [])
        next_id = max((int(row.get("id", 0)) for row in dependency_rows), default=0) + 1
        for relation in CHAIN_RELATIONS:
            row = dict(relation)
            row["id"] = next_id
            next_id += 1
            dependency_rows.append(row)

        baseline_rows = self._tables.setdefault("baseline", [])
        next_id = max((int(row.get("id", 0)) for row in baseline_rows), default=0) + 1
        for baseline in CHAIN_BASELINES:
            row = dict(baseline)
            row["id"] = next_id
            next_id += 1
            baseline_rows.append(row)

        # 运行水位、解除水位与批次进度是长链查询的独立取数来源。
        for name, rows in LONGCHAIN_TABLES.items():
            self._tables.setdefault(name, [dict(row) for row in rows])

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
