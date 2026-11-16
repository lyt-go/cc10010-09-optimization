<template>
  <section class="chain-panel" data-panel="baseline-warnings">
    <header class="chain-head">
      <div>
        <h3>基线预警清单</h3>
        <p class="page-desc">
          等待水位来自长链依赖查询结果；与依赖解析共享同一游标窗口，已解除关系不计入等待。
        </p>
      </div>
      <span class="chain-badge">
        {{ store.hasCursor ? `共享游标 · 水位 #${store.watermark?.arrival_high} · 口径 v${store.watermark?.policy_version}` : '尚未建立共享游标' }}
      </span>
    </header>

    <div class="filter-bar">
      <button class="btn primary" type="button" :disabled="loading" @click="load">读取预警</button>
      <button class="btn ghost" type="button" :disabled="!store.hasCursor || loading" @click="clearShared">使用共享游标重读</button>
      <button class="btn ghost" type="button" :disabled="!store.hasCursor" @click="store.clear()">解除共享</button>
    </div>

    <table class="data-table">
      <thead>
        <tr>
          <th>基线编号</th>
          <th>基线名称</th>
          <th>承诺完成时间</th>
          <th>关联链路</th>
          <th>等待关系数</th>
          <th>是否预警</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in warnings" :key="String(row.基线编号)">
          <td>{{ row.基线编号 }}</td>
          <td>{{ row.基线名称 }}</td>
          <td>{{ row.承诺完成时间 }}</td>
          <td>{{ row.关联链路.join('、') || '—' }}</td>
          <td :class="row.是否预警 ? 'warn-text' : ''">{{ row.等待关系数 }}</td>
          <td :class="row.是否预警 ? 'warn-text' : 'ok-text'">{{ row.是否预警 ? '预警' : '正常' }}</td>
        </tr>
        <tr v-if="!warnings.length">
          <td colspan="6" class="empty-state">暂无基线预警数据</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>
        等待关系 {{ totalWaiting }} 条
        <template v-if="watermark"> · 快照 {{ watermark.snapshot_id }}（水位 #{{ watermark.arrival_high }}，口径 v{{ watermark.policy_version }}）</template>
      </span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { fetchJson } from '@/api/client'
import { useLongChainStore, type LongChainWatermark } from '@/stores/longchain'

type WarningRow = {
  基线编号: string | null
  基线名称: string | null
  承诺完成时间: string | null
  关联链路: string[]
  等待关系数: number
  是否预警: boolean
}

type WarningPayload = {
  cursor: string
  watermark: LongChainWatermark
  total_waiting: number
  warnings: WarningRow[]
}

const store = useLongChainStore()
const warnings = ref<WarningRow[]>([])
const watermark = ref<LongChainWatermark | null>(null)
const totalWaiting = ref(0)
const loading = ref(false)
const errorMessage = ref('')
const shared = ref(false)

async function load() {
  loading.value = true
  errorMessage.value = ''
  try {
    const query = shared.value && store.cursor ? `?cursor=${encodeURIComponent(store.cursor)}` : ''
    const payload = await fetchJson<WarningPayload>(`/api/baseline/warnings${query}`)
    warnings.value = payload.warnings ?? []
    watermark.value = payload.watermark
    totalWaiting.value = payload.total_waiting ?? 0
    if (shared.value && store.cursor) {
      // 跨模块查询共享同一游标窗口：沿用依赖解析建好的窗口。
      store.remember(store.cursor, payload.watermark, store.chainId)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '基线预警读取失败'
  } finally {
    loading.value = false
  }
}

function clearShared() {
  shared.value = true
  void load()
}

onMounted(() => {
  shared.value = store.hasCursor
  void load()
})
</script>
