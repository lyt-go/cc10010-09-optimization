"""依赖解析接口：维护依赖关系，覆盖提交关系评审、生效关系、解除关系等动作。

长链依赖查询（/longchain）把依赖关系与运行水位按同一游标窗口归并后再分页，
依赖解析与基线管理共用这份结果。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, LongChainLocate, LongChainPage, PageResult
from app.services.dependency import DependencyService
from app.services.longchain import longchain_service

router = APIRouter(prefix="/api/dependency", tags=["依赖解析"])

service = DependencyService()

LIST_FIELDS = ["关系编号", "上游作业", "下游作业", "依赖类型", "偏移量分钟", "触发条件", "等待超时", "跳过标志"]
STATUSES = ["草稿", "关系评审中", "已生效", "已解除"]
CHAIN_STATES = ["等待中", "已满足", "已解除"]


def _page_size(size: int) -> int:
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    if size < 1:
        raise HTTPException(status_code=400, detail="每页至少 1 条")
    return size


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按关系编号检索"),
    status: str | None = Query(default=None, description="草稿、关系评审中、已生效、已解除"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按关系编号与状态过滤依赖解析列表；没有数据时返回空页，不报错。"""
    _page_size(size)
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出依赖解析清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "dependency", "total": total, "items": items}


@router.get("/longchain", response_model=LongChainPage)
def query_longchain(
    chain_id: str | None = Query(default=None, description="按链路编号定位"),
    upstream: str | None = Query(default=None, description="按上游作业筛选"),
    downstream: str | None = Query(default=None, description="按下游作业筛选"),
    status: str | None = Query(default=None, description="等待中、已满足、已解除"),
    keyword: str | None = Query(default=None, description="按关系编号/作业/链路检索"),
    page: int = Query(default=1, ge=1),
    size: int = 20,
    cursor: str | None = Query(default=None, description="上一页返回的 next_cursor，翻页必须带回"),
) -> LongChainPage:
    """分页查询长链依赖结果：总数、页内状态与下游触发范围同一水位，不重复展开。"""
    if status and status not in CHAIN_STATES:
        raise HTTPException(status_code=400, detail=f"状态取值应为：{'、'.join(CHAIN_STATES)}")
    _page_size(size)
    result = longchain_service.query(
        chain_id=chain_id,
        upstream=upstream,
        downstream=downstream,
        status=status,
        keyword=keyword,
        page=page,
        size=size,
        cursor=cursor,
    )
    return LongChainPage(**result)


@router.get("/longchain/locate", response_model=LongChainLocate)
def locate_longchain(
    job: str = Query(description="要定位的作业名称"),
    cursor: str | None = Query(default=None, description="共享同一游标窗口"),
) -> LongChainLocate:
    """按作业定位其在各长链中的上下游位置与水位。"""
    if not job.strip():
        raise HTTPException(status_code=400, detail="作业名称不能为空")
    return LongChainLocate(**longchain_service.locate(job))


@router.get("/longchain/{chain_id}", response_model=LongChainPage)
def chain_detail(chain_id: str) -> LongChainPage:
    """读取单条长链的完整窗口结果；链路不存在时给出可读说明。"""
    result = longchain_service.chain_detail(chain_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"长链 {chain_id} 不存在或没有依赖关系")
    return LongChainPage(**result)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条依赖关系明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"依赖关系 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条依赖关系，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="依赖关系已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条依赖关系执行提交关系评审、生效关系、解除关系；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
