<template>
  <section class="page candidate-page" data-testid="candidate-review">
    <header class="page-head">
      <div>
        <h2>实体复核候选集</h2>
        <p class="page-desc">
          按实体类别置信度阈值，从命名实体数据判定复核候选；记录适用阈值与真实分块出处，
          同一证据同一规则重跑不重复派单。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" data-testid="refresh-candidates" @click="reloadCandidates">
          刷新候选
        </button>
        <button class="btn primary" type="button" data-testid="build-candidates" @click="build">
          按当前阈值构建候选集
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">候选样本</span>
        <strong class="stat-value">{{ candidates.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">跨阈值样本</span>
        <strong class="stat-value">{{ crossCount }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">不可单独结案</span>
        <strong class="stat-value">{{ notClosableCount }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">当前查看 run</span>
        <strong class="stat-value run-value">{{ activeRunId || 'latest' }}</strong>
      </article>
    </div>

    <form class="filter-bar threshold-panel" @submit.prevent="build">
      <div class="threshold-grid">
        <label v-for="row in thresholdRows" :key="row.type" class="filter-item threshold-item">
          <span>{{ row.type }} 阈值</span>
          <input
            v-model.number="thresholdDraft[row.type]"
            type="number"
            step="0.01"
            min="0"
            max="1"
            :data-testid="`threshold-${row.type}`"
          />
        </label>
      </div>
      <label class="filter-item">
        <span>分块集（可选）</span>
        <input v-model="chunkSetId" placeholder="留空读取全部实体产物" data-testid="chunk-set-id" />
      </label>
      <label class="filter-item checkbox-item">
        <input v-model="onlyCross" type="checkbox" data-testid="only-cross" />
        <span>仅看跨阈值样本</span>
      </label>
      <button class="btn" type="button" @click="reloadCandidates">查询</button>
    </form>

    <div class="run-bar">
      <span class="run-label">历史构建（改阈值后可回看旧 run 与旧结论）：</span>
      <button
        v-for="run in runs"
        :key="run.run_id"
        type="button"
        class="link run-chip"
        :class="{ active: run.run_id === activeRunId }"
        :data-testid="`run-${run.run_id}`"
        @click="selectRun(run.run_id)"
      >
        {{ run.created_at }} · {{ run.candidate_count }}候选 · {{ run.cross_threshold_count }}跨阈值
      </button>
      <button v-if="activeRunId" type="button" class="link" @click="selectRun('')">回到最新</button>
    </div>

    <p v-if="message" class="error-text" data-testid="candidate-message">{{ message }}</p>

    <table class="data-table candidate-table">
      <thead>
        <tr>
          <th>候选编号</th>
          <th>实体</th>
          <th>类别</th>
          <th>适用阈值/规则</th>
          <th>置信度区间</th>
          <th>出处（章节/分块）</th>
          <th>跨阈值</th>
          <th>结案约束</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="candidate in visibleCandidates" :key="candidate.candidate_id"
            :data-testid="`candidate-${candidate.candidate_id}`"
            :data-cross="String(candidate.cross_threshold)">
          <td class="mono">{{ candidate.candidate_id }}</td>
          <td>{{ candidate.entity_text }}</td>
          <td>{{ candidate.entity_type }}</td>
          <td>
            {{ candidate.applied_threshold.toFixed(2) }}
            <small>（{{ ruleLabel(candidate.threshold_rule.rule_source) }}）</small>
          </td>
          <td>{{ candidate.min_confidence.toFixed(2) }} ~ {{ candidate.max_confidence.toFixed(2) }}</td>
          <td>
            <ul class="evidence-list">
              <li v-for="evidence in candidate.evidence" :key="evidence.evidence_id"
                  :class="evidence.threshold_side"
                  :data-testid="`evidence-${evidence.evidence_id}`">
                <strong>{{ evidence.section }}</strong>
                <span class="mono">{{ evidence.chunk_id }}</span>
                <span :class="evidence.confidence < evidence.applied_threshold ? 'conf-low' : 'conf-high'">
                  {{ evidence.confidence.toFixed(2) }}
                </span>
                <em>{{ evidence.source_excerpt }}</em>
              </li>
            </ul>
          </td>
          <td>
            <span :class="candidate.cross_threshold ? 'tag warn' : 'tag ok'">
              {{ candidate.cross_threshold ? '是' : '否' }}
            </span>
          </td>
          <td>
            <span :class="candidate.independently_closable ? 'tag ok' : 'tag warn'">
              {{ candidate.independently_closable ? '可单独结案' : '需覆盖全部证据' }}
            </span>
          </td>
          <td class="row-actions">
            <button type="button" class="link" :data-testid="`dispatch-${candidate.candidate_id}`"
                    @click="dispatchOne(candidate)">
              派复核待办
            </button>
            <button type="button" class="link" :data-testid="`close-${candidate.candidate_id}`"
                    @click="openConclusion(candidate)">
              登记结论
            </button>
            <button type="button" class="link" @click="queryClosed(candidate)">
              查旧结论
            </button>
          </td>
        </tr>
        <tr v-if="!visibleCandidates.length">
          <td colspan="9" class="empty-state">当前阈值下暂无复核候选，可调整阈值后重新构建。</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showConclusion" class="modal-backdrop" data-testid="conclusion-modal">
      <form class="modal" @submit.prevent="submitConclusion">
        <h3>登记复核结论 · {{ conclusionForm.entityText }}</h3>
        <p v-if="conclusionForm.cross" class="error-text">
          该候选标题与正文证据分列阈值两侧，结论须覆盖全部证据后才能结案。
        </p>
        <label class="create-field">
          <span>复核人</span>
          <input v-model="conclusionForm.reviewer" required data-testid="conclusion-reviewer" />
        </label>
        <label class="create-field">
          <span>复核结论</span>
          <textarea v-model="conclusionForm.conclusion" rows="3" required
                    data-testid="conclusion-text"></textarea>
        </label>
        <fieldset class="evidence-check">
          <legend>结论覆盖的证据（{{ conclusionForm.covered.length }}/{{ conclusionForm.allEvidence.length }}）</legend>
          <label v-for="evidence in conclusionForm.allEvidence" :key="evidence.evidence_id" class="evidence-option">
            <input v-model="conclusionForm.covered" type="checkbox" :value="evidence.evidence_id"
                   :data-testid="`cover-${evidence.evidence_id}`" />
            <span>{{ evidence.section }} · {{ evidence.chunk_id }} · {{ evidence.confidence.toFixed(2) }}</span>
          </label>
        </fieldset>
        <div class="page-actions">
          <button type="button" class="btn" @click="showConclusion = false">取消</button>
          <button type="submit" class="btn primary" data-testid="submit-conclusion">提交结案</button>
        </div>
      </form>
    </div>

    <div v-if="closedResults.length" class="closed-panel" data-testid="closed-panel">
      <h3>已结案结果（按原依据查询，规则与来源随结论保留）</h3>
      <ul>
        <li v-for="task in closedResults" :key="task.复核编号">
          <strong>{{ task.复核编号 }}</strong>
          <span>{{ task.复核对象 }}</span>
          <span>规则 {{ task.threshold_rule?.entity_type }}@{{ task.threshold_rule?.threshold }}</span>
          <span>run {{ task.run_id }}</span>
          <span>结论：{{ task.复核结论 }}（{{ task.复核人 }} · {{ task.复核时间 }}）</span>
        </li>
      </ul>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import {
  buildReviewCandidates,
  dispatchCandidateTodos,
  listCandidateRuns,
  listReviewCandidates,
  queryClosedReviews,
  submitCandidateConclusion,
} from '@/api/backend-service'
import type { CandidateRun, ReviewCandidate } from '@/data/types'

const DEFAULT_THRESHOLDS: Record<string, number> = {
  PERSON: 0.9,
  ORG: 0.85,
  LOC: 0.85,
  TIME: 0.9,
  MONEY: 0.9,
  TECH: 0.88,
}

const candidates = ref<ReviewCandidate[]>([])
const runs = ref<CandidateRun[]>([])
const activeRunId = ref('')
const onlyCross = ref(false)
const chunkSetId = ref('')
const message = ref('')
const thresholdDraft = reactive<Record<string, number>>({ ...DEFAULT_THRESHOLDS })
const thresholdRows = Object.entries(DEFAULT_THRESHOLDS).map(([type, value]) => ({ type, value }))

const showConclusion = ref(false)
const closedResults = ref<Awaited<ReturnType<typeof queryClosedReviews>>['items']>([])
const conclusionForm = reactive({
  reviewId: '',
  candidateId: '',
  entityText: '',
  cross: false,
  reviewer: '',
  conclusion: '',
  covered: [] as string[],
  allEvidence: [] as ReviewCandidate['evidence'],
})

const visibleCandidates = computed(() =>
  onlyCross.value ? candidates.value.filter((item) => item.cross_threshold) : candidates.value,
)
const crossCount = computed(() => candidates.value.filter((item) => item.cross_threshold).length)
const notClosableCount = computed(() => candidates.value.filter((item) => !item.independently_closable).length)

function ruleLabel(source: string): string {
  if (source === 'type_default') return '类别默认阈值'
  if (source === 'override') return '本次覆盖阈值'
  return '兜底阈值'
}

async function reloadCandidates(): Promise<void> {
  message.value = ''
  try {
    const payload = await listReviewCandidates({
      runId: activeRunId.value || undefined,
      onlyCross: onlyCross.value,
    })
    candidates.value = payload.items
    const history = await listCandidateRuns()
    runs.value = history.items
  } catch (error) {
    message.value = error instanceof Error ? error.message : '候选集读取失败'
  }
}

async function build(): Promise<void> {
  message.value = ''
  try {
    const thresholds = Object.fromEntries(
      Object.entries(thresholdDraft).filter(([, value]) => Number.isFinite(value)),
    )
    const result = await buildReviewCandidates({
      chunk_set_id: chunkSetId.value.trim() || null,
      thresholds,
    })
    activeRunId.value = result.run_id
    await reloadCandidates()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '候选集构建失败'
  }
}

function selectRun(runId: string): void {
  activeRunId.value = runId
  void reloadCandidates()
}

async function dispatchOne(candidate: ReviewCandidate): Promise<void> {
  message.value = ''
  try {
    const batchKey = `UI-${candidate.source_scope}`
    const result = await dispatchCandidateTodos({
      batch_key: batchKey,
      candidate_ids: [candidate.candidate_id],
      run_id: activeRunId.value || undefined,
    })
    if (result.error) {
      message.value = result.error
      return
    }
    message.value = result.tasks_created
      ? `已创建 ${result.tasks_created} 条待办，规则与来源已随单保留`
      : '该证据在此规则下已派过单，未重复派单'
  } catch (error) {
    message.value = error instanceof Error ? error.message : '派单失败'
  }
}

function openConclusion(candidate: ReviewCandidate): void {
  conclusionForm.reviewId = ''
  conclusionForm.candidateId = candidate.candidate_id
  conclusionForm.entityText = candidate.entity_text
  conclusionForm.cross = candidate.cross_threshold
  conclusionForm.reviewer = ''
  conclusionForm.conclusion = ''
  conclusionForm.allEvidence = candidate.evidence
  // 非跨阈值默认覆盖全部证据；跨阈值需复核员显式勾选确认两条口径
  conclusionForm.covered = candidate.cross_threshold ? [] : candidate.evidence.map((e) => e.evidence_id)
  conclusionForm.reviewId = guessReviewId(candidate)
  showConclusion.value = true
}

function guessReviewId(candidate: ReviewCandidate): string {
  // 待办编号由后端按候选+规则+批次稳定生成，前端不掌握时留空，由后端兜底提示
  void candidate
  return ''
}

async function submitConclusion(): Promise<void> {
  message.value = ''
  if (!conclusionForm.reviewId) {
    message.value = '请先在人工复核模块或通过派单获取复核编号后再登记结论'
    return
  }
  try {
    const result = await submitCandidateConclusion(conclusionForm.reviewId, {
      conclusion: conclusionForm.conclusion,
      reviewer: conclusionForm.reviewer,
      covered_evidence_ids: conclusionForm.covered,
    })
    if (result.error) {
      message.value = result.missing_evidence?.length
        ? `${result.error} 缺失证据：${result.missing_evidence.join('、')}`
        : result.error
      return
    }
    showConclusion.value = false
    message.value = `候选 ${result.candidate_id} 已结案`
  } catch (error) {
    message.value = error instanceof Error ? error.message : '结论登记失败'
  }
}

async function queryClosed(candidate: ReviewCandidate): Promise<void> {
  message.value = ''
  try {
    const payload = await queryClosedReviews({ candidateId: candidate.candidate_id })
    closedResults.value = payload.items
    if (!payload.total) message.value = '该候选暂无已结案结论'
  } catch (error) {
    message.value = error instanceof Error ? error.message : '旧结论查询失败'
  }
}

onMounted(reloadCandidates)
</script>
