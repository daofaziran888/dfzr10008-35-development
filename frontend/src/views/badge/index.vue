<template>
  <section class="page" data-module="badge">
    <header class="page-head">
      <div>
        <h2>数据产品认证管理</h2>
        <p class="page-desc">
          认证按产品归属控制：只有产品负责人能提交认证申请，非归属单位进入只读态；申请时冻结认证标准、有效期限与标准符合性结果，
          审核结论回写产品发布准入，驳回或过期立即移出可用认证集合。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" :disabled="readonlyMode" @click="openCreate">登记认证申请</button>
        <button class="btn" type="button" @click="exportRows">导出数据产品认证清单</button>
      </div>
    </header>

    <div class="filter-bar operator-bar">
      <label class="filter-item">
        <span>当前操作人</span>
        <select v-model="operator">
          <option v-for="name in operatorOptions" :key="name" :value="name">{{ name }}</option>
        </select>
      </label>
      <span class="scope-hint" :class="readonlyMode ? 'readonly' : ''">
        {{ readonlyMode ? '非归属单位：认证记录只读，不能提交或审核' : '归属单位：可按负责人 / 审核人身份操作' }}
      </span>
    </div>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <label class="filter-item checkbox-item">
        <span>只看可用认证</span>
        <input v-model="onlyAvailable" type="checkbox" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :class="row['只读'] ? 'row-readonly' : ''">
          <td v-for="column in columns" :key="column">{{ renderCell(row, column) }}</td>
          <td class="row-actions">
            <template v-if="row['只读']">
              <span class="readonly-tag">只读（非归属单位）</span>
            </template>
            <template v-else>
              <button
                v-for="action in actions"
                :key="action"
                class="link"
                type="button"
                :disabled="!canRunAction(action, row)"
                @click="runAction(action, row)"
              >
                {{ action }}
              </button>
              <button class="link" type="button" @click="showMaterials(row)">查看材料</button>
            </template>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无数据产品认证数据，可先登记认证申请</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条数据产品认证记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <div v-if="dialog" class="dialog-mask" @click.self="closeDialog">
      <section class="dialog">
        <header class="dialog-head">
          <h3>{{ dialog.title }}</h3>
          <button class="link" type="button" @click="closeDialog">关闭</button>
        </header>

        <form v-if="dialog.mode === 'create'" class="dialog-form" @submit.prevent="submitCreate">
          <label class="filter-item">
            <span>产品编号</span>
            <input v-model="form['产品编号']" placeholder="PROD-0007" required />
          </label>
          <label class="filter-item">
            <span>认证标准</span>
            <input v-model="form['认证标准']" placeholder="数据口径可追溯 v1" required />
          </label>
          <label class="filter-item">
            <span>认证类型</span>
            <input v-model="form['认证类型']" placeholder="数据产品认证" />
          </label>
          <label class="filter-item">
            <span>有效期起</span>
            <input v-model="form['有效期起']" type="month" />
          </label>
          <label class="filter-item">
            <span>有效期限（至）</span>
            <input v-model="form['有效期限']" type="date" required />
          </label>
          <label class="filter-item">
            <span>标准核对表</span>
            <input v-model="form['标准核对表']" placeholder="STD-07" />
          </label>
          <label class="filter-item">
            <span>负责人签字</span>
            <input v-model="form['负责人签字']" :placeholder="operator" />
          </label>
          <p class="dialog-hint">
            提交后认证标准、有效期限与标准符合性结果立即冻结，认证材料成为不可改动的共享材料；
            同一产品的同一认证标准只保留一个认证审核结果。
          </p>
          <footer class="dialog-foot">
            <button class="btn primary" type="submit">提交认证申请</button>
          </footer>
        </form>

        <div v-else-if="dialog.mode === 'materials'">
          <table class="data-table">
            <thead>
              <tr><th>材料项</th><th>内容</th></tr>
            </thead>
            <tbody>
              <tr v-for="(value, key) in dialog.materials['认证材料']" :key="String(key)">
                <td>{{ key }}</td>
                <td>{{ value }}</td>
              </tr>
            </tbody>
          </table>
          <p class="dialog-hint">认证材料在申请时冻结，属于共享材料，审核阶段不可修改。</p>
        </div>
      </section>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/badge'
const columns = [
  '认证编号',
  '产品编号',
  '产品名称',
  '认证标准',
  '认证类型',
  '认证机构',
  '有效期限',
  '申请人',
  '认证结果',
  '标准符合性结果',
  '认证状态',
  '发布准入',
  '可用',
]
const actions = ['通过认证', '驳回认证', '延期认证']
const operatorOptions = ['周宁', '李敏', '王芳', '陈然', '值班管理员']

const operator = ref('周宁')
const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const onlyAvailable = ref(false)
const filters = ref<Record<string, string>>({})
const filterFields = ['认证编号', '产品编号', '认证标准']

const readonlyMode = computed(() => {
  if (rows.value.length) {
    return rows.value.every((row) => Boolean(row['只读']))
  }
  return operator.value === '陈然'
})

const stats = computed(() => {
  const passed = rows.value.filter((row) => row['认证状态'] === '已通过').length
  const expired = rows.value.filter((row) => row['认证状态'] === '已过期').length
  const available = rows.value.filter((row) => Boolean(row['可用'])).length
  return [
    { label: '认证记录总数', value: total.value },
    { label: '已通过认证', value: passed },
    { label: '已过期认证', value: expired },
    { label: '可用认证集合', value: available },
  ]
})

type Dialog =
  | { mode: 'create'; title: string }
  | { mode: 'materials'; title: string; materials: Row }
  | null

const dialog = ref<Dialog>(null)
const form = ref<Record<string, string>>({})

function renderCell(row: Row, column: string): string {
  const value = row[column]
  if (column === '可用') {
    return value ? '可用' : '不可用'
  }
  return value === null || value === undefined || value === '' ? '—' : String(value)
}

function canRunAction(action: string, row: Row): boolean {
  const permissions = row.permissions ?? {}
  if (action === '通过认证' || action === '驳回认证') {
    return Boolean(permissions.can_review)
  }
  if (action === '延期认证') {
    return Boolean(permissions.can_review)
  }
  return false
}

function openCreate() {
  errorMessage.value = ''
  if (readonlyMode.value) {
    errorMessage.value = '当前操作人不属于产品归属单位，认证记录为只读，不能提交认证申请'
    return
  }
  form.value = {
    产品编号: '',
    认证标准: '',
    认证类型: '数据产品认证',
    有效期起: '2026-06',
    有效期限: '2027-06-30',
    标准核对表: 'STD-07',
    负责人签字: operator.value,
  }
  dialog.value = { mode: 'create', title: '登记认证申请' }
}

function closeDialog() {
  dialog.value = null
}

async function showMaterials(row: Row) {
  try {
    const response = await request(`${ENDPOINT}/${row.id}/materials`)
    if (!response.ok) {
      throw new Error('认证材料读取失败')
    }
    dialog.value = {
      mode: 'materials',
      title: `认证材料 · ${row['认证编号']}`,
      materials: await response.json(),
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '认证材料读取失败'
  }
}

async function submitCreate() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}?operator=${encodeURIComponent(operator.value)}`, {
      method: 'POST',
      body: JSON.stringify({ values: { ...form.value } }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '认证申请被拒绝')
    }
    closeDialog()
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '认证申请提交失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const values: Record<string, string> = { action }
    if (action === '延期认证') {
      const extended = window.prompt('新的有效期至（YYYY-MM-DD）', String(row['有效期至'] ?? ''))
      if (!extended) {
        return
      }
      values['有效期至'] = extended
    }
    const response = await request(`${ENDPOINT}/${row.id}/actions?operator=${encodeURIComponent(operator.value)}`, {
      method: 'POST',
      body: JSON.stringify({ values }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '数据产品认证动作未生效')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据产品认证操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  Object.entries(filters.value).forEach(([key, value]) => {
    if (value) {
      query.set(key, value)
    }
  })
  query.set('operator', operator.value)
  if (onlyAvailable.value) {
    query.set('only_available', 'true')
  }
  try {
    const response = await request(`${ENDPOINT}?${query.toString()}`)
    if (!response.ok) {
      throw new Error('认证记录列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据产品认证列表读取失败'
  }
}

function resetFilters() {
  filters.value = {}
  onlyAvailable.value = false
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export?operator=${encodeURIComponent(operator.value)}`, '_blank')
}

watch(operator, () => {
  void reload()
})

onMounted(reload)
</script>
