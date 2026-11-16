<template>
  <section class="chain-panel" data-panel="longchain">
    <header class="chain-head">
      <div>
        <h3>长链依赖查询</h3>
        <p class="page-desc">
          依赖关系与运行水位按同一游标窗口归并，分页总数、页内状态与下游触发范围取自同一水位。
        </p>
      </div>
      <span v-if="store.hasCursor" class="chain-badge">
        水位 #{{ store.watermark?.arrival_high }} · 口径 v{{ store.watermark?.policy_version }}
      </span>
    </header>

    <form class="filter-bar" @submit.prevent="search">
      <label class="filter-item">
        <span>链路编号</span>
        <input v-model="filters.chain_id" placeholder="如 LC-001" />
      </label>
      <label class="filter-item">
        <span>上游作业</span>
        <input v-model="filters.upstream" placeholder="按上游作业筛选" />
      </label>
      <label class="filter-item">
        <span>下游作业</span>
        <input v-model="filters.downstream" placeholder="按下游作业筛选" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="filters.status">
          <option value="">全部</option>
          <option v-for="state in states" :key="state" :value="state">{{ state }}</option>
        </select>
      </label>
      <label class="filter-item">
        <span>检索</span>
        <input v-model="filters.keyword" placeholder="关系编号/作业/链路" />
      </label>
      <button class="btn primary" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="reset">重置</button>
    </form>

    <form class="filter-bar" @submit.prevent="locateJob">
      <label class="filter-item">
        <span>按作业定位</span>
        <input v-model="locateKeyword" placeholder="输入上游或下游作业" />
      </label>
      <button class="btn" type="submit">定位上下游</button>
      <button class="btn ghost" type="button" @click="locateKeyword = ''; positions = []">清除定位</button>
    </form>

    <div v-if="positions.length" class="locate-bar">
      <span class="locate-tag" v-for="item in positions" :key="item.关系编号 + item.定位">
        {{ item.链路编号 }} / {{ item.关系编号 }} · {{ item.定位 }} · {{ item.状态 }}
      </span>
    </div>

    <div class="chain-summary">
      <article v-for="item in summary" :key="item.chain_id" class="chain-card">
        <strong>{{ item.chain_name }}</strong>
        <span class="chain-id">{{ item.chain_id }}</span>
        <dl>
          <div><dt>关系</dt><dd>{{ item.relation_count }}</dd></div>
          <div><dt>等待</dt><dd class="warn-text">{{ item.waiting_count }}</dd></div>
          <div><dt>已满足</dt><dd>{{ item.satisfied_count }}</dd></div>
          <div><dt>已解除</dt><dd>{{ item.released_count }}</dd></div>
        </dl>
        <p class="chain-trigger">下游已触发：{{ item.downstream_triggered.join('、') || '—' }}</p>
        <p class="chain-trigger">下游待触发：{{ item.downstream_pending.join('、') || '—' }}</p>
      </article>
    </div>

    <table class="data-table">
      <thead>
        <tr>
          <th>链路</th>
          <th>段</th>
          <th>关系编号</th>
          <th>上游作业</th>
          <th>下游作业</th>
          <th>状态</th>
          <th>上游水位</th>
          <th>历史关系</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td>{{ row.链路编号 }}</td>
          <td>{{ row.链路序号 }}</td>
          <td>{{ row.关系编号 }}</td>
          <td>{{ row.上游作业 }}</td>
          <td>{{ row.下游作业 }}</td>
          <td :class="stateClass(row.状态)">{{ row.状态 }}</td>
          <td>{{ formatWatermark(row) }}</td>
          <td>{{ formatHistory(row.历史关系) }}</td>
        </tr>
        <tr v-if="!rows.length">
          <td colspan="8" class="empty-state">当前筛选条件下没有长链依赖数据</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>
        共 {{ total }} 条关系 · 等待 {{ totalWaiting }} 条 · 第 {{ page }} 页
        <template v-if="store.watermark">
          · 快照 {{ store.watermark.snapshot_id }}（水位 #{{ store.watermark.arrival_high }}，口径 v{{ store.watermark.policy_version }}）
        </template>
      </span>
      <span class="pager">
        <button class="btn" type="button" :disabled="page <= 1 || loading" @click="prevPage">上一页</button>
        <button class="btn" type="button" :disabled="!hasMore || loading" @click="nextPage">下一页</button>
      </span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <details v-if="conflicts.length" class="conflict-box">
      <summary>水位冲突 {{ conflicts.length }} 条（保留较高水位版本，承接原有历史关系）</summary>
      <ul>
        <li v-for="(item, index) in conflicts" :key="index">
          {{ item.作业名称 }} / {{ item.批次编号 }} / #{{ item.水位序号 }} — {{ item.原因 }}
        </li>
      </ul>
    </details>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchJson } from '@/api/client'
import { useLongChainStore } from '@/stores/longchain'

type ChainRow = {
  id: number
  关系编号: string
  链路编号: string
  链路序号: number
  上游作业: string
  下游作业: string
  状态: string
  上游水位序号: number | null
  上游水位批次: string | null
  上游运行状态: string | null
  历史关系: { 关系编号: string; 历史版本: number; 状态: string }[]
}

type ChainSummary = {
  chain_id: string
  chain_name: string
  relation_count: number
  waiting_count: number
  satisfied_count: number
  released_count: number
  downstream_triggered: string[]
  downstream_pending: string[]
}

type ChainPage = {
  items: ChainRow[]
  total: number
  page: number
  size: number
  cursor: string
  next_cursor: string
  watermark: {
    snapshot_id: string
    arrival_high: number
    policy_version: number
    batches: Record<string, number>
    conflicts: { 作业名称?: string; 批次编号?: string; 水位序号?: number; 原因?: string }[]
  }
  summary: ChainSummary[]
  total_waiting: number
  has_more: boolean
}

const ENDPOINT = '/api/dependency/longchain'
const states = ['等待中', '已满足', '已解除']
const store = useLongChainStore()

const filters = ref({ chain_id: '', upstream: '', downstream: '', status: '', keyword: '' })
const rows = ref<ChainRow[]>([])
const summary = ref<ChainSummary[]>([])
const total = ref(0)
const totalWaiting = ref(0)
const page = ref(1)
const size = ref(10)
const cursor = ref('')
const nextCursor = ref('')
const hasMore = ref(false)
const loading = ref(false)
const errorMessage = ref('')
const locateKeyword = ref('')
const positions = ref<{ 链路编号: string; 关系编号: string; 定位: string; 状态: string }[]>([])

const conflicts = computed(() => store.watermark?.conflicts ?? [])

function stateClass(state: string) {
  if (state === '等待中') return 'warn-text'
  if (state === '已解除') return 'muted-text'
  return 'ok-text'
}

function formatWatermark(row: ChainRow) {
  if (row.上游水位序号 === null) return '—'
  return `#${row.上游水位序号} ${row.上游运行状态 ?? ''}`.trim()
}

function formatHistory(history: ChainRow['历史关系']) {
  if (!history.length) return '—'
  return history.map((item) => `${item.关系编号}(${item.状态})`).join('、')
}

async function load(target: { page?: number; resume?: string; reset?: boolean }) {
  loading.value = true
  errorMessage.value = ''
  const query = new URLSearchParams()
  Object.entries(filters.value).forEach(([key, value]) => {
    if (value) query.set(key, value)
  })
  if (target.reset) {
    // 新查询从头建窗口；同一游标只生成一份结果供翻页复用。
    cursor.value = ''
  }
  const activeCursor = target.resume ?? cursor.value
  if (activeCursor) query.set('cursor', activeCursor)
  query.set('size', String(size.value))
  if (target.page && target.page > 0) query.set('page', String(target.page))
  try {
    const payload = await fetchJson<ChainPage>(`${ENDPOINT}?${query.toString()}`)
    rows.value = payload.items ?? []
    summary.value = payload.summary ?? []
    total.value = payload.total ?? 0
    totalWaiting.value = payload.total_waiting ?? 0
    page.value = payload.page ?? 1
    hasMore.value = Boolean(payload.has_more)
    cursor.value = payload.cursor ?? ''
    nextCursor.value = payload.next_cursor ?? payload.cursor ?? ''
    store.remember(payload.cursor, payload.watermark, filters.value.chain_id)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '长链依赖查询失败'
  } finally {
    loading.value = false
  }
}

function search() {
  positions.value = []
  void load({ page: 1, reset: true })
}

function reset() {
  filters.value = { chain_id: '', upstream: '', downstream: '', status: '', keyword: '' }
  search()
}

function goPage(target: number) {
  // 向后翻页沿用 next_cursor 的续读偏移，从最后稳定位置继续；
  // 向前翻页回到同一快照窗口按页号重取，旧页对象与新页总数仍来自同一水位。
  const next = Math.max(target, 1)
  if (next > page.value) {
    void load({ resume: nextCursor.value })
  } else {
    void load({ page: next, resume: cursor.value })
  }
}

function nextPage() {
  goPage(page.value + 1)
}

function prevPage() {
  goPage(page.value - 1)
}

async function locateJob() {
  if (!locateKeyword.value.trim()) return
  errorMessage.value = ''
  try {
    const query = new URLSearchParams({ job: locateKeyword.value.trim() })
    if (cursor.value) query.set('cursor', cursor.value)
    const payload = await fetchJson<{ positions: typeof positions.value; cursor: string }>(
      `${ENDPOINT}/locate?${query.toString()}`,
    )
    positions.value = payload.positions ?? []
    if (!positions.value.length) {
      errorMessage.value = `作业 ${locateKeyword.value.trim()} 不在当前长链窗口内`
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '作业定位失败'
  }
}

onMounted(() => {
  void load({ page: 1, reset: true })
})

defineExpose({ reload: () => load({ page: 1, reset: true }), cursor })
</script>
