import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type {
  ActionResult,
  CandidateBuildResult,
  CandidateConclusionResult,
  CandidateDispatchResult,
  CandidateReviewTask,
  CandidateRun,
  EntryRow,
  OverviewResult,
  PageResult,
  ReviewCandidate,
} from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// -- 实体复核候选集 --

async function postJson<T>(path: string, body: Record<string, unknown>): Promise<T> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  const payload = (await response.json()) as T & { detail?: string }
  if (!response.ok) {
    throw new Error(payload.detail ?? `接口返回 ${response.status}，操作未生效`)
  }
  return payload
}

/** 按类别置信度阈值构建复核候选集，返回不可变 run 快照与候选明细。 */
export function buildReviewCandidates(payload: {
  chunk_set_id?: string | null
  thresholds?: Record<string, number>
}): Promise<CandidateBuildResult> {
  return postJson('/api/entity_recognize/review-candidates/build', payload)
}

/** 读取候选集；runId 缺省读最新构建，传入历史 runId 读该次快照用于对比。 */
export async function listReviewCandidates(params: {
  runId?: string
  entityType?: string
  onlyCross?: boolean
} = {}): Promise<{ items: ReviewCandidate[]; total: number; run_id: string }> {
  const query = new URLSearchParams()
  if (params.runId) query.set('run_id', params.runId)
  if (params.entityType) query.set('entity_type', params.entityType)
  if (params.onlyCross) query.set('only_cross', 'true')
  const response = await request(`/api/entity_recognize/review-candidates?${query.toString()}`)
  if (!response.ok) throw new Error('复核候选集读取失败')
  return await response.json()
}

/** 历次构建记录：改阈值后对比历史数据、适用阈值与旧结论。 */
export async function listCandidateRuns(): Promise<{ items: CandidateRun[]; total: number }> {
  const response = await request('/api/entity_recognize/review-candidates/runs/history')
  if (!response.ok) throw new Error('候选集构建历史读取失败')
  return await response.json()
}

/** 用复核候选集创建人工复核待办，返回每个样本采用的规则和被选中的来源。 */
export function dispatchCandidateTodos(payload: {
  batch_key: string
  candidate_ids?: string[]
  run_id?: string
  reviewer?: string
}): Promise<CandidateDispatchResult> {
  return postJson('/api/annot_review/candidate-dispatch', payload)
}

/** 登记候选复核结论；跨阈值候选未覆盖全部证据时后端返回 400。 */
export function submitCandidateConclusion(
  reviewId: string,
  payload: { conclusion: string; reviewer: string; covered_evidence_ids?: string[] },
): Promise<CandidateConclusionResult> {
  return postJson(`/api/annot_review/reviews/${reviewId}/candidate-conclusion`, payload)
}

/** 按原依据（候选/run/分块出处）查询已结案结果。 */
export async function queryClosedReviews(params: {
  candidateId?: string
  runId?: string
  chunkId?: string
}): Promise<{ items: CandidateReviewTask[]; total: number }> {
  const query = new URLSearchParams()
  if (params.candidateId) query.set('candidate_id', params.candidateId)
  if (params.runId) query.set('run_id', params.runId)
  if (params.chunkId) query.set('chunk_id', params.chunkId)
  const response = await request(`/api/annot_review/closed-reviews?${query.toString()}`)
  if (!response.ok) throw new Error('已结案复核结果读取失败')
  return await response.json()
}
