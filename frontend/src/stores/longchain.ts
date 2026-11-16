import { defineStore } from 'pinia'

/**
 * 长链查询游标：依赖解析与基线管理共享同一个快照窗口。
 * 依赖解析页建立窗口后把游标记下来，基线预警清单沿用同一游标，
 * 两处看到的等待总数与下游触发范围因此来自同一水位。
 */
export type LongChainWatermark = {
  snapshot_id: string
  arrival_high: number
  policy_version: number
  batches: Record<string, number>
  conflicts: { 作业名称?: string; 批次编号?: string; 水位序号?: number; 原因?: string }[]
}

export const useLongChainStore = defineStore('longchain', {
  state: () => ({
    cursor: '',
    watermark: null as LongChainWatermark | null,
    chainId: '',
  }),
  getters: {
    hasCursor: (state) => Boolean(state.cursor && state.watermark),
  },
  actions: {
    remember(cursor: string, watermark: LongChainWatermark, chainId = '') {
      this.cursor = cursor
      this.watermark = watermark
      this.chainId = chainId
    },
    clear() {
      this.cursor = ''
      this.watermark = null
      this.chainId = ''
    },
  },
})
