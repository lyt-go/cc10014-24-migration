import { expect, test } from '@playwright/test'
import { readFile } from 'node:fs/promises'

type ModuleCase = {
  key: string
  fields: string[]
  actions: string[]
  actionTargets: string[]
  shape: string
}

const modules: ModuleCase[] = [
  {
    "key": "raw_ingest",
    "fields": [
      "批次编号",
      "来源系统",
      "文件格式",
      "原始行数",
      "入库时间",
      "字符编码",
      "校验摘要",
      "落地路径"
    ],
    "actions": [
      "发起校验",
      "确认入库",
      "归档批次"
    ],
    "actionTargets": [
      "校验中",
      "已入库",
      "已归档"
    ],
    "shape": "job-timeline"
  },
  {
    "key": "column_mapping",
    "fields": [
      "规则编号",
      "源字段名",
      "目标字段名",
      "数据类型",
      "默认值",
      "转换函数",
      "规则版本",
      "生效日期"
    ],
    "actions": [
      "提交校验",
      "标记生效",
      "标记废弃"
    ],
    "actionTargets": [
      "待校验",
      "已生效",
      "已废弃"
    ],
    "shape": "crud-table"
  },
  {
    "key": "cleanse_rule",
    "fields": [
      "策略编号",
      "目标字段",
      "空值处理",
      "异常阈值",
      "正则表达式",
      "替换值",
      "策略版本",
      "执行顺序"
    ],
    "actions": [
      "提交校验",
      "标记生效",
      "停用策略"
    ],
    "actionTargets": [
      "待校验",
      "已生效",
      "已停用"
    ],
    "shape": "crud-table"
  },
  {
    "key": "dedup_config",
    "fields": [
      "配置编号",
      "业务键列",
      "排序字段",
      "保留策略",
      "时间窗口",
      "相似度阈值",
      "配置版本",
      "目标数据集"
    ],
    "actions": [
      "提交校验",
      "标记生效",
      "停用配置"
    ],
    "actionTargets": [
      "待校验",
      "已生效",
      "已停用"
    ],
    "shape": "crud-table"
  },
  {
    "key": "ods_layer",
    "fields": [
      "表编号",
      "来源系统",
      "表名称",
      "分区字段",
      "存储格式",
      "保留天数",
      "压缩算法",
      "创建批次"
    ],
    "actions": [
      "提交校验",
      "确认上线",
      "标记下线"
    ],
    "actionTargets": [
      "待校验",
      "已上线",
      "已下线"
    ],
    "shape": "crud-table"
  },
  {
    "key": "dwd_layer",
    "fields": [
      "表编号",
      "父层来源",
      "表名称",
      "粒度说明",
      "分区字段",
      "关联键列",
      "刷新水位",
      "创建批次"
    ],
    "actions": [
      "提交校验",
      "确认上线",
      "标记下线"
    ],
    "actionTargets": [
      "待校验",
      "已上线",
      "已下线"
    ],
    "shape": "crud-table"
  },
  {
    "key": "dws_layer",
    "fields": [
      "表编号",
      "统计粒度",
      "汇总维度",
      "度量字段",
      "聚合函数",
      "时间范围",
      "刷新周期",
      "创建批次"
    ],
    "actions": [
      "提交校验",
      "确认上线",
      "标记下线"
    ],
    "actionTargets": [
      "待校验",
      "已上线",
      "已下线"
    ],
    "shape": "crud-table"
  },
  {
    "key": "dim_model",
    "fields": [
      "模型编号",
      "维度名称",
      "自然键列",
      "代理键列",
      "缓慢变化策略",
      "生效日期",
      "失效日期",
      "刷新周期"
    ],
    "actions": [
      "提交校验",
      "标记生效",
      "停用模型"
    ],
    "actionTargets": [
      "待校验",
      "已生效",
      "已停用"
    ],
    "shape": "crud-table"
  },
  {
    "key": "snapshot_mgr",
    "fields": [
      "快照编号",
      "目标数据集",
      "快照时间",
      "数据行数",
      "存储路径",
      "压缩算法",
      "保留天数",
      "快照版本"
    ],
    "actions": [
      "创建快照",
      "确认完成",
      "标记过期"
    ],
    "actionTargets": [
      "创建中",
      "已完成",
      "已过期"
    ],
    "shape": "job-timeline"
  },
  {
    "key": "partition_mgr",
    "fields": [
      "策略编号",
      "目标表名",
      "分区字段",
      "分区粒度",
      "保留天数",
      "冷热阈值",
      "预建数量",
      "目录规则"
    ],
    "actions": [
      "提交校验",
      "标记生效",
      "停用策略"
    ],
    "actionTargets": [
      "待校验",
      "已生效",
      "已停用"
    ],
    "shape": "crud-table"
  },
  {
    "key": "quality_check",
    "fields": [
      "规则编号",
      "校验表名",
      "校验字段",
      "校验表达式",
      "阈值下界",
      "阈值上界",
      "阻断级别",
      "采样比例"
    ],
    "actions": [
      "提交校验",
      "标记生效",
      "停用规则"
    ],
    "actionTargets": [
      "待校验",
      "已生效",
      "已停用"
    ],
    "shape": "crud-table"
  },
  {
    "key": "lineage_track",
    "fields": [
      "链路编号",
      "上游表名",
      "下游表名",
      "来源字段",
      "目标字段",
      "转换逻辑",
      "采集批次",
      "链路版本"
    ],
    "actions": [
      "提交校验",
      "标记生效",
      "停用链路"
    ],
    "actionTargets": [
      "待校验",
      "已生效",
      "已停用"
    ],
    "shape": "dag-graph"
  },
  {
    "key": "backfill_job",
    "fields": [
      "任务编号",
      "目标表名",
      "起始日期",
      "截止日期",
      "并行度",
      "覆盖策略",
      "完成水位",
      "失败区间"
    ],
    "actions": [
      "启动任务",
      "确认完成",
      "取消任务"
    ],
    "actionTargets": [
      "运行中",
      "已完成",
      "已取消"
    ],
    "shape": "job-timeline"
  },
  {
    "key": "table_compare",
    "fields": [
      "任务编号",
      "左表名称",
      "右表名称",
      "比对字段",
      "比对方式",
      "差异阈值",
      "采样比例",
      "比对批次"
    ],
    "actions": [
      "启动比对",
      "确认完成",
      "取消比对"
    ],
    "actionTargets": [
      "比对中",
      "已完成",
      "已取消"
    ],
    "shape": "job-timeline"
  },
  {
    "key": "schema_registry",
    "fields": [
      "定义编号",
      "表名称",
      "字段名称",
      "字段类型",
      "可空标识",
      "默认值",
      "定义版本",
      "生效批次"
    ],
    "actions": [
      "提交校验",
      "标记生效",
      "标记废弃"
    ],
    "actionTargets": [
      "待校验",
      "已生效",
      "已废弃"
    ],
    "shape": "crud-table"
  },
  {
    "key": "data_publish",
    "fields": [
      "版本编号",
      "数据集名称",
      "发布范围",
      "查询参数",
      "响应字段",
      "缓存秒数",
      "分页上限",
      "消费应用"
    ],
    "actions": [
      "提交校验",
      "确认发布",
      "撤回版本"
    ],
    "actionTargets": [
      "待校验",
      "已发布",
      "已撤回"
    ],
    "shape": "crud-table"
  },
  {
    "key": "transform_dag",
    "fields": [
      "节点编号",
      "输入表名",
      "输出表名",
      "转换脚本",
      "执行顺序",
      "依赖节点",
      "失败策略",
      "调度周期"
    ],
    "actions": [
      "提交校验",
      "标记生效",
      "停用节点"
    ],
    "actionTargets": [
      "待校验",
      "已生效",
      "已停用"
    ],
    "shape": "dag-graph"
  },
  {
    "key": "cold_archive",
    "fields": [
      "策略编号",
      "目标表名",
      "冷数据阈值",
      "归档路径",
      "压缩算法",
      "归档格式",
      "保留天数",
      "恢复超时"
    ],
    "actions": [
      "提交校验",
      "标记生效",
      "停用策略"
    ],
    "actionTargets": [
      "待校验",
      "已生效",
      "已停用"
    ],
    "shape": "crud-table"
  }
]
const repoKind = 'fullstack'
const crudModules = modules.filter(m => m.shape === 'crud-table')
const dagModules = modules.filter(m => m.shape === 'dag-graph')
const timelineModules = modules.filter(m => m.shape === 'job-timeline')
const analysisModules = modules.filter(m => m.shape === 'analysis-dashboard')

function parseCsv(text: string): string[][] {
  const records: string[][] = []
  let row: string[] = []
  let cell = ''
  let quoted = false
  const input = text.replace(/^\uFEFF/, '')
  for (let index = 0; index < input.length; index++) {
    const char = input[index]
    if (quoted) {
      if (char === '"' && input[index + 1] === '"') { cell += '"'; index++ }
      else if (char === '"') { quoted = false }
      else { cell += char }
    } else if (char === '"' && cell === '') { quoted = true }
    else if (char === ',') { row.push(cell); cell = '' }
    else if (char === '\n') {
      row.push(cell.replace(/\r$/, ''))
      if (row.some(v => v !== '')) records.push(row)
      row = []; cell = ''
    } else { cell += char }
  }
  row.push(cell.replace(/\r$/, ''))
  if (row.some(v => v !== '')) records.push(row)
  return records
}

test('CRUD模块：筛选、导出、登记、动作、指标全链路', async ({ page }) => {
  test.setTimeout(300_000)
  const browserErrors: string[] = []
  page.on('pageerror', e => browserErrors.push(e.message))
  page.on('response', r => { if (r.url().includes('/api/') && r.status()>=400) browserErrors.push(`${r.status()} ${r.url()}`) })
  if (crudModules.length === 0) { test.skip(); return }

  for (const mod of crudModules) {
    await page.goto(`/${mod.key}`)
    await page.waitForTimeout(3000)
    const bodyRows = page.locator('tbody tr[data-testid^="row-"]')
    await expect(bodyRows).toHaveCount(3)
    const firstValue = await bodyRows.nth(0).locator('td').nth(0).innerText()

    await page.getByTestId(`filter-${mod.fields[0]}`).fill(firstValue)
    await page.getByTestId('query').click()
    await expect(bodyRows).toHaveCount(1)
    await page.getByTestId('reset').click()
    await expect(bodyRows).toHaveCount(3)

    const downloadPromise = page.waitForEvent('download')
    await page.getByTestId('export').click()
    const download = await downloadPromise
    expect(download.suggestedFilename()).toBe(`${mod.key}.csv`)

    const createdValues = mod.fields.map((f, i) => `验收-${mod.key}-${i+1}`)
    await page.getByTestId('open-create').click()
    await expect(page.getByTestId('create-modal')).toBeVisible()
    for (const [i, field] of mod.fields.entries()) {
      await page.getByTestId(`create-${field}`).fill(createdValues[i])
    }
    await page.getByTestId('submit-create').click()
    await expect(page.getByTestId('create-modal')).toHaveCount(0)
    await expect(bodyRows).toHaveCount(4)

    const createdRow = page.locator('tbody tr').filter({ hasText: createdValues[0] })
    for (const [i, action] of mod.actions.entries()) {
      await createdRow.getByRole('button', { name: action, exact: true }).click()
      await expect(createdRow).toHaveAttribute('data-status', mod.actionTargets[i])
    }
  }

  expect(browserErrors).toEqual([])
})

test('DAG拓扑模块：节点渲染、详情、动作、导出', async ({ page }) => {
  test.setTimeout(300_000)
  const browserErrors: string[] = []
  page.on('pageerror', e => browserErrors.push(e.message))
  page.on('response', r => { if (r.url().includes('/api/') && r.status()>=400) browserErrors.push(`${r.status()} ${r.url()}`) })
  if (dagModules.length === 0) { test.skip(); return }

  for (const mod of dagModules) {
    await page.goto(`/${mod.key}`)
    await page.waitForTimeout(3000)
    await expect(page.locator('svg.dag-svg')).toBeVisible()
    const nodes = page.locator('g.dag-node')
    const count = await nodes.count()
    expect(count).toBeGreaterThanOrEqual(3)

    await nodes.first().click()
    await expect(page.getByTestId('dag-detail')).toBeVisible()

    const downloadPromise = page.waitForEvent('download')
    await page.getByTestId('export').click()
    const download = await downloadPromise
    expect(download.suggestedFilename()).toBe(`${mod.key}.csv`)

    await page.getByTestId('open-create').click()
    await expect(page.getByTestId('create-modal')).toBeVisible()
    for (const [i, field] of mod.fields.slice(0,3).entries()) {
      await page.locator('.create-field input').nth(i).fill(`E2E-${mod.key}-${i}`)
    }
    await page.getByTestId('submit-create').click()
    await expect(page.getByTestId('create-modal')).toHaveCount(0)
  }

  expect(browserErrors).toEqual([])
})

test('时间线模块：甘特条渲染、日期筛选、日志查看、登记', async ({ page }) => {
  test.setTimeout(300_000)
  const browserErrors: string[] = []
  page.on('pageerror', e => browserErrors.push(e.message))
  page.on('response', r => { if (r.url().includes('/api/') && r.status()>=400) browserErrors.push(`${r.status()} ${r.url()}`) })
  if (timelineModules.length === 0) { test.skip(); return }

  for (const mod of timelineModules) {
    await page.goto(`/${mod.key}`)
    await expect(page.locator('.timeline-bar').first()).toBeVisible({ timeout: 10000 })
    const bars = page.locator('.timeline-bar')
    const count = await bars.count()
    expect(count).toBeGreaterThanOrEqual(3)

    await bars.first().click()
    await expect(page.getByTestId('timeline-log')).toBeVisible()

    const downloadPromise = page.waitForEvent('download')
    await page.getByTestId('export').click()
    const download = await downloadPromise
    expect(download.suggestedFilename()).toBe(`${mod.key}.csv`)

    await page.getByTestId('open-create').click()
    await expect(page.getByTestId('create-modal')).toBeVisible()
    for (const [i, field] of mod.fields.slice(0,3).entries()) {
      await page.locator('.create-field input').nth(i).fill(`E2E-${mod.key}-${i}`)
    }
    await page.getByTestId('submit-create').click()
    await expect(page.getByTestId('create-modal')).toHaveCount(0)
  }

  expect(browserErrors).toEqual([])
})

test('分析看板模块：KPI卡片、运行分析、图表渲染', async ({ page }) => {
  test.setTimeout(300_000)
  const browserErrors: string[] = []
  page.on('pageerror', e => browserErrors.push(e.message))
  page.on('response', r => { if (r.url().includes('/api/') && r.status()>=400) browserErrors.push(`${r.status()} ${r.url()}`) })
  if (analysisModules.length === 0) { test.skip(); return }

  for (const mod of analysisModules) {
    await page.goto(`/${mod.key}`)
    await expect(page.getByTestId('kpi-card').first()).toBeVisible({ timeout: 10000 })
    const kpiCards = page.getByTestId('kpi-card')
    const kpiCount = await kpiCards.count()
    expect(kpiCount).toBeGreaterThanOrEqual(2)
    await expect(kpiCards.first().locator('strong')).toBeVisible()

    await page.getByTestId('run-analysis').click()
    await expect(page.locator('.analysis-bar').first()).toBeVisible({ timeout: 5000 })
    await expect(page.locator('.data-table tbody tr').first()).toBeVisible({ timeout: 5000 })
  }

  expect(browserErrors).toEqual([])
})

test('概览汇总', async ({ page }) => {
  test.setTimeout(300_000)
  await page.goto('/')
  await expect(page.locator('.stat-value').nth(1)).toBeVisible()
  // 验证概览页加载正常即可，不硬编码精确计数（取决于各模块 E2E 登记成功数）
})

test('血缘链路版本表：迁移、批次、版本切换、回滚与血缘核对', async ({ page }) => {
  test.setTimeout(300_000)
  const browserErrors: string[] = []
  page.on('pageerror', e => browserErrors.push(e.message))
  page.on('response', r => { if (r.url().includes('/api/') && r.status()>=400) browserErrors.push(`${r.status()} ${r.url()}`) })

  await page.goto('/lineage_version')
  await expect(page.getByTestId('run-migration')).toBeVisible()
  // 迁移前所有存量链路都待迁移（运行时双写的边已直接落版本表，不计入缺失边）
  const legacyBefore = Number(await page.getByTestId('stat-legacy').innerText())
  const pendingBefore = Number(await page.getByTestId('stat-pending').innerText())
  const migratedBefore = Number(await page.getByTestId('stat-migrated').innerText())
  expect(pendingBefore + migratedBefore).toBe(legacyBefore)

  // 执行迁移（默认 resume 严格边界）
  await page.getByTestId('run-migration').click()
  await expect(page.getByTestId('migrate-modal')).toBeVisible()
  await page.getByTestId('submit-migrate').click()
  await expect(page.getByTestId('migrate-modal')).toHaveCount(0)
  await expect(page.getByTestId('stat-pending')).toHaveText('0')
  // 批次表与版本表都出现数据，每条链路一个当前版本（前序 DAG 用例可能已登记新链路）
  await expect(page.locator('[data-testid^="batch-LB-"]').first()).toBeVisible()
  const linkRows = page.locator('[data-testid^="link-"]')
  expect(await linkRows.count()).toBeGreaterThanOrEqual(3)
  await expect(page.locator('[data-testid="current-version"]').first()).not.toHaveText(/已回滚/)

  // 迁移后返回血缘追踪列表核对：DAG 边数与存量一致，旧查询字段仍可读
  await page.getByTestId('goto-lineage').click()
  await page.waitForTimeout(2000)
  await expect(page.locator('svg.dag-svg')).toBeVisible()
  await expect(page.locator('g.dag-node').first()).toBeVisible()
  const edgeCountBefore = await page.locator('svg.dag-svg line.dag-edge').count()
  expect(edgeCountBefore).toBeGreaterThanOrEqual(3)

  // 回到版本表做版本切换并沿调用链追踪
  await page.goto('/lineage_version')
  await page.locator('[data-testid^="trace-"]').first().click()
  await expect(page.getByTestId('trace-panel')).toBeVisible()
  const traceText = await page.getByTestId('trace-panel').innerText()
  expect(traceText).toContain('触达表')

  // 回滚迁移批次：当前版本停用，但血缘追踪列表边不丢失（旧记录兜底）
  const batchId = await page.locator('[data-testid^="batch-LB-"]').first().getAttribute('data-testid')
  const id = batchId!.replace('batch-', '')
  await page.getByTestId(`rollback-${id}`).click()
  await page.waitForTimeout(1000)
  await page.goto('/lineage_track')
  await page.waitForTimeout(2000)
  const edgeCountAfter = await page.locator('svg.dag-svg line.dag-edge').count()
  expect(edgeCountAfter).toBe(edgeCountBefore)

  expect(browserErrors).toEqual([])
})
