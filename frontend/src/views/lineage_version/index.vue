<template>
  <section class="page" data-module="lineage_version">
    <header class="page-head">
      <div>
        <h2>血缘链路版本表</h2>
        <p class="page-desc">迁移存量血缘链路并按批次管理版本：每条链路仅一个当前版本，迁移批次与失败明细可回查，支持续做、回滚与冲突仲裁。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" data-testid="run-migration" @click="openMigrate">执行迁移</button>
        <button class="btn" type="button" data-testid="refresh" @click="reload">刷新</button>
        <RouterLink class="btn ghost" to="/lineage_track" data-testid="goto-lineage">返回血缘追踪列表核对</RouterLink>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value" :data-testid="`stat-${item.key}`">{{ item.value }}</strong>
      </article>
    </div>

    <p v-if="message" class="error-text" data-testid="message">{{ message }}</p>

    <h3 class="section-title">迁移批次</h3>
    <table class="data-table" data-testid="batch-table">
      <thead>
        <tr>
          <th v-for="col in batchColumns" :key="col">{{ col }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="b in batches" :key="b.batch_id" :data-status="b.status" :data-testid="`batch-${b.batch_id}`">
          <td>{{ b.batch_id }}</td>
          <td>{{ b.kind }}</td>
          <td>{{ b.conflict_policy }}</td>
          <td>{{ b.status }}</td>
          <td>{{ b.migrated }}/{{ b.total }}</td>
          <td>{{ b.skipped }}</td>
          <td>{{ b.conflicts }}</td>
          <td>
            <span v-if="b.failures && b.failures.length">
              {{ b.failures.length }} 条失败：{{ b.failures[0].reason }}
            </span>
            <span v-else>—</span>
          </td>
          <td>
            <button class="link" type="button" :data-testid="`rollback-${b.batch_id}`"
                    :disabled="b.status === '进行中'" @click="rollback(b.batch_id)">回滚批次</button>
          </td>
        </tr>
        <tr v-if="!batches.length">
          <td :colspan="batchColumns.length" class="empty-state">尚无迁移批次，点击「执行迁移」开始</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">链路版本集（每条链路仅一个当前版本）</h3>
    <table class="data-table" data-testid="version-table">
      <thead>
        <tr>
          <th>链路</th>
          <th>上游表名</th>
          <th>下游表名</th>
          <th>当前版本</th>
          <th>迁移批次</th>
          <th>版本数</th>
          <th>冲突状态</th>
          <th>版本明细与切换</th>
          <th>血缘调用链</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="link in links" :key="link.lineage_key" :data-testid="`link-${link.lineage_key}`">
          <td>{{ link.lineage_key }}</td>
          <td>{{ link['上游表名'] }}</td>
          <td>{{ link['下游表名'] }}</td>
          <td data-testid="current-version">{{ link.current_version ?? '—（已回滚）' }}</td>
          <td>{{ link.current_batch ?? '—' }}</td>
          <td>{{ link.version_count }}</td>
          <td>
            <span v-if="link.conflict_state === '待仲裁'" class="error-text">待仲裁</span>
            <span v-else>正常</span>
          </td>
          <td>
            <div v-for="v in link.versions" :key="v.version_id" class="version-line"
                 :data-testid="`version-${v.version_id}`">
              <span :class="{ 'current-tag': v.is_current }">
                {{ v['链路版本'] }}（{{ v['迁移批次'] }}）{{ v.is_current ? '·当前' : '' }}
              </span>
              <button v-if="!v.is_current && v.conflict_state !== '待仲裁'" class="link" type="button"
                      @click="switchVersion(link.lineage_key, v.version_id)">切换为此版本</button>
              <template v-if="v.conflict_state === '待仲裁'">
                <button class="link" type="button" @click="resolve(v.version_id, 'current')">仲裁保留当前</button>
                <button class="link" type="button" @click="resolve(v.version_id, 'legacy')">仲裁采用旧记录</button>
              </template>
            </div>
          </td>
          <td>
            <button class="link" type="button" :data-testid="`trace-${link.lineage_key}`"
                    @click="trace(link['下游表名'])">向下游追踪</button>
          </td>
        </tr>
        <tr v-if="!links.length">
          <td colspan="9" class="empty-state">版本表暂无数据，请先执行迁移</td>
        </tr>
      </tbody>
    </table>

    <div v-if="traceResult" class="dag-detail" data-testid="trace-panel">
      <h4>血缘追踪调用链（{{ traceResult.direction }}）</h4>
      <p>起点表：{{ traceResult.table }}</p>
      <p>触达表：{{ traceResult.tables.join(' → ') }}</p>
      <ul>
        <li v-for="e in traceResult.edges" :key="e.lineage_key">
          {{ e['上游表名'] }} → {{ e['下游表名'] }}（{{ e['链路版本'] }} / {{ e['迁移批次'] }}）
        </li>
      </ul>
      <button class="btn" type="button" @click="traceResult = null">关闭</button>
    </div>

    <div v-if="showMigrate" class="modal-backdrop" data-testid="migrate-modal">
      <form class="modal" @submit.prevent="submitMigrate">
        <h3>执行血缘链路迁移</h3>
        <label class="create-field"><span>冲突策略</span>
          <select v-model="policy" data-testid="policy-select">
            <option value="resume">resume 续做（严格边界：只补缺失边，显示续做批次与完整链路集）</option>
            <option value="rollback">rollback 回滚（放宽边界：停用旧版本，显示回滚批次与一次性结果）</option>
            <option value="arbitrate">arbitrate 仲裁（冲突挂起，人工决定优先谁）</option>
          </select>
        </label>
        <label class="create-field"><span>备注</span>
          <input v-model="remark" data-testid="migrate-remark" placeholder="如：上线流水线迁移" />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showMigrate = false">取消</button>
          <button class="btn primary" type="submit" data-testid="submit-migrate">确认执行</button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ links.length }} 条链路 · {{ batches.length }} 个批次</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { request } from '@/api/client'

const ENDPOINT = '/api/lineage_versions'
interface Batch {
  batch_id: string
  kind: string
  conflict_policy: string
  status: string
  total: number
  migrated: number
  skipped: number
  conflicts: number
  failures: Array<{ reason: string }>
}
interface VersionRow {
  version_id: number
  is_current: boolean
  conflict_state: string
  ['链路版本']: string
  ['迁移批次']: string
}
interface LinkRow {
  lineage_key: string
  ['上游表名']: string
  ['下游表名']: string
  current_version: string | null
  current_batch: string | null
  version_count: number
  conflict_state: string
  versions: VersionRow[]
}
interface Status {
  legacy_total: number
  migrated_total: number
  pending_total: number
  conflict_total: number
  complete: boolean
}
interface TraceResult {
  table: string
  direction: string
  tables: string[]
  edges: Array<{ lineage_key: string; ['上游表名']: string; ['下游表名']: string; ['链路版本']: string; ['迁移批次']: string }>
}

const batches = ref<Batch[]>([])
const links = ref<LinkRow[]>([])
const status = ref<Status | null>(null)
const message = ref('')
const showMigrate = ref(false)
const policy = ref('resume')
const remark = ref('')
const traceResult = ref<TraceResult | null>(null)
const batchColumns = ['批次编号', '批次类型', '冲突策略', '状态', '已迁移/存量', '跳过', '冲突', '失败明细', '操作']

const stats = computed(() => [
  { key: 'legacy', label: '存量链路', value: status.value?.legacy_total ?? 0 },
  { key: 'migrated', label: '已迁移当前版本', value: status.value?.migrated_total ?? 0 },
  { key: 'pending', label: '待迁移缺失边', value: status.value?.pending_total ?? 0 },
  { key: 'conflict', label: '待仲裁冲突', value: status.value?.conflict_total ?? 0 },
])

async function reload() {
  try {
    const [vRes, sRes, bRes] = await Promise.all([
      request(ENDPOINT), request(`${ENDPOINT}/status`), request(`${ENDPOINT}/batches`),
    ])
    const versionData = await vRes.json()
    const statusData = await sRes.json()
    const batchData = await bRes.json()
    links.value = versionData.items ?? []
    batches.value = batchData.items ?? []
    status.value = statusData
  } catch (e) {
    message.value = '版本表加载失败'
  }
}

function openMigrate() { showMigrate.value = true; remark.value = '' }
async function submitMigrate() {
  const res = await request(`${ENDPOINT}/migrate`, {
    method: 'POST', body: JSON.stringify({ conflict_policy: policy.value, remark: remark.value }),
  })
  const data = await res.json()
  showMigrate.value = false
  if (data.ok) {
    message.value = `迁移完成：${data.kind} ${data.batch_id}，写入 ${data.migrated} 条，跳过 ${data.skipped} 条`
  } else {
    message.value = `迁移存在失败边（批次 ${data.batch_id} 已保留失败明细）：${(data.failures ?? []).map((f: { reason: string }) => f.reason).join('；')}`
  }
  await reload()
}

async function rollback(batchId: string) {
  const res = await request(`${ENDPOINT}/rollback`, {
    method: 'POST', body: JSON.stringify({ batch_id: batchId }),
  })
  const data = await res.json()
  message.value = data.ok ? `回滚批次已生成：${data.batch_id}，停用 ${data.affected} 个当前版本（历史保留可恢复）` : `回滚失败：${data.message}`
  await reload()
}

async function switchVersion(lineageKey: string, versionId: number) {
  const res = await request(`${ENDPOINT}/switch`, {
    method: 'POST', body: JSON.stringify({ lineage_key: lineageKey, version_id: versionId }),
  })
  const data = await res.json()
  message.value = data.ok ? `版本切换完成（批次 ${data.batch_id}），已沿血缘调用链定位 ${data.trace.tables.length} 张表` : `切换被拒绝：${data.message}`
  await reload()
}

async function resolve(versionId: number, winner: string) {
  const res = await request(`${ENDPOINT}/resolve-conflict`, {
    method: 'POST', body: JSON.stringify({ version_id: versionId, winner }),
  })
  const data = await res.json()
  message.value = data.ok ? data.message : `仲裁失败：${data.message}`
  await reload()
}

async function trace(tableName: string) {
  const res = await request(`${ENDPOINT}/trace`, {
    method: 'POST', body: JSON.stringify({ table_name: tableName, direction: 'downstream' }),
  })
  traceResult.value = await res.json()
}

onMounted(reload)
</script>

<style scoped>
.section-title { margin: 18px 0 8px; font-size: 15px; }
.version-line { display: flex; gap: 8px; align-items: center; padding: 2px 0; }
.current-tag { font-weight: 600; color: #0f766e; }
</style>
