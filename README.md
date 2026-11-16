# 数据仓库ETL任务编排平台

面向数据仓库ETL任务开发、作业编排调度、任务依赖解析、源端数据校验、异常任务重跑、血缘分板与运行全景监控的ETL工程管理平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| ETL作业 | `etljob` | 作业定义 | 作业编号、作业名称、源端数据表 |
| 任务编排 | `dag` | 编排图 | 图编号、图名称、拓扑结构 |
| 依赖解析 | `dependency` | 依赖关系 | 关系编号、上游作业、下游作业 |
| 源端校验 | `sourcecheck` | 校验配置 | 配置编号、源端数据表、行数下限 |
| 运行实例 | `runtime` | 运行记录 | 记录编号、作业名称、开始时间 |
| 异常重跑 | `rerun` | 重跑任务 | 任务编号、原始运行编号、重跑区间开始 |
| 基线管理 | `baseline` | 基线定义 | 基线编号、基线名称、承诺完成时间 |
| 分区检测 | `partitioncheck` | 检测规则 | 规则编号、数据表名、期望分区数 |
| 数据比对 | `comparator` | 比对任务 | 任务编号、源表名、目标表名 |
| 元数据存储 | `metastore` | 元数据表 | 表编号、表名、所属数仓层 |
| 资源配置 | `resource` | 资源池 | 池编号、池名称、最大并发数 |
| SLA监控 | `slavalidate` | SLA规则 | 规则编号、适用作业、完成截止时间 |
| SQL模板 | `sqltpl` | 模板定义 | 模板编号、模板名称、模板类型 |
| 数据备份 | `backuprestore` | 备份策略 | 策略编号、数据表名、备份周期 |
| 日志归档 | `logarchive` | 归档策略 | 策略编号、日志类型、保留天数 |
| 通知渠道 | `notifychan` | 渠道配置 | 渠道编号、渠道类型、接收地址 |
| 发布计划 | `publishplan` | 发布单 | 发布编号、发布内容、变更说明 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 长链依赖查询

依赖关系与运行水位分开取数，由 `backend/app/services/longchain.py` 按统一游标
窗口归并成一份长链依赖查询结果：

- `GET /api/dependency/longchain`：按链路、上下游作业、状态、关键字筛选分页。
  响应额外带 `cursor` / `next_cursor`、`watermark`、`summary`（等待数与下游触发
  范围）。翻页必须带回 `next_cursor`，分页总数与页内状态因此始终来自同一水位。
- `GET /api/dependency/longchain/locate?job=...`：按上游/下游作业定位。
- `GET /api/dependency/longchain/{chain_id}`：按链路编号读取完整窗口。
- `GET /api/baseline/warnings?cursor=...`：基线预警清单，等待水位来自同一份长链
  查询结果；带上依赖解析返回的游标即跨模块共享同一窗口。

口径说明：

- 游标固定 `arrival_high`（到达序号高水位）与各批次水位；快照之后到达的完成
  事件不会改写旧游标，旧页对象与新页总数不会跨水位拼接。
- 同一作业多批次水位冲突时按 `水位序号、到达序号、批次编号` 取最高一版，被压下
  的版本记在 `watermark.conflicts`；同一段依赖的历史版本只计一次，旧版本在
  `历史关系` 里承接。
- 等待口径按 `水位口径版本` 升级：口径变更后按新口径重算，解除记录以墓碑保留，
  已解除关系不会重新计入等待。
- 同一游标只生成一份快照并缓存，连续请求同一游标不会重复展开关系。

回归测试：

```bash
cd backend && .venv/bin/python -m unittest discover -s tests -v
```

领域：数据工程
子方向：数仓与数据湖
