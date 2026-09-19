<script setup lang="ts">
import { computed } from 'vue'
import type { SessionSummary } from '../lib/types'
import { archiveSession } from '../lib/api'
import { fmtAgo, fmtCost, fmtDate, fmtDuration, fmtTokens, ts } from '../lib/format'
import { hrefFor } from '../lib/router'
import PhaseDots from './PhaseDots.vue'
import StatusChip from './StatusChip.vue'

const props = defineProps<{
  session: SessionSummary
  nowMs: number
  /** Zero-based position in the newest-first list — #1 wears the latest badge. */
  rank: number
}>()
const emit = defineEmits<{ archived: [adwId: string] }>()

async function archive(event: MouseEvent) {
  event.preventDefault()
  event.stopPropagation()
  emit('archived', props.session.adw_id)
  try {
    await archiveSession(props.session.adw_id)
  } catch {
    emit('archived', '')
  }
}

const running = computed(() => props.session.status === 'running')

const durationMs = computed(() => {
  const s = props.session
  const start = ts(s.started_at)
  if (!Number.isFinite(start)) return NaN
  const end = running.value ? props.nowMs : ts(s.ended_at)
  return (Number.isFinite(end) ? end : props.nowMs) - start
})

const passed = computed(() => (props.session.phases ?? []).filter((p) => p.status === 'success').length)
const total = computed(() => (props.session.phases ?? []).length)
</script>

<template>
  <a class="row" :class="session.status" :href="hrefFor(session.adw_id)">
    <span class="rank" :class="{ latest: rank === 0 }" :title="rank === 0 ? 'newest run' : `run #${rank + 1}`">
      {{ rank === 0 ? '●' : rank + 1 }}
    </span>
    <span class="main">
      <span class="id-line">
        <span class="id">{{ session.adw_id }}</span>
        <span v-if="rank === 0" class="latest-badge">latest</span>
        <StatusChip :status="session.status ?? 'fail'" />
        <span
          v-if="session.branch"
          class="branch"
          :title="`worktree ${session.worktree_path ?? ''} · source ${session.source_branch ?? ''}`"
          >⎇ {{ session.branch }}</span
        >
      </span>
      <span class="req" :title="session.request ?? ''">{{ session.request || '—' }}</span>
      <span class="sub dim">
        <PhaseDots :phases="session.phases ?? []" />
        <span v-if="total" class="dim">{{ passed }}/{{ total }} phases</span>
        <span class="dim">{{ fmtAgo(session.started_at, nowMs) }}</span>
        <span class="dim" :title="fmtDate(session.started_at)">{{ fmtDate(session.started_at) }}</span>
      </span>
    </span>
    <span class="stats">
      <span class="stat" :title="`cost ${fmtCost(session.total_cost)}`">{{ fmtCost(session.total_cost) }}</span>
      <span class="stat" :title="`${session.total_tokens ?? '—'} tokens`">{{ fmtTokens(session.total_tokens) }}</span>
      <span class="stat" :title="`runtime ${fmtDuration(durationMs)}`">{{ fmtDuration(durationMs) }}</span>
    </span>
    <button
      class="row-archive"
      type="button"
      title="Archive — remove this run from review"
      aria-label="Archive run"
      @click="archive"
    >
      ×
    </button>
  </a>
</template>

<style scoped>
.row {
  position: relative;
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 14px 18px;
  border: 1px solid var(--border-soft);
  border-left-width: 3px;
  border-radius: 0;
  background: var(--surface);
  color: var(--text);
  cursor: pointer;
  overflow: hidden;
  transition:
    border-color 0.15s ease,
    box-shadow 0.15s ease,
    transform 0.15s ease;
}

.row:hover {
  border-color: rgba(148, 163, 255, 0.45);
  box-shadow: var(--shadow);
  transform: translateY(-1px);
}

.row.running {
  border-left-color: var(--blue);
}

.row.fail {
  border-left-color: var(--red);
}

.row.success {
  border-left-color: var(--green);
}

.rank {
  flex: none;
  width: 30px;
  text-align: center;
  font-family: var(--mono);
  font-size: 16px;
  color: var(--faint);
}

.rank.latest {
  color: var(--green);
  font-size: 20px;
}

.main {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.id-line {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.id {
  font-family: var(--mono);
  font-size: 17px;
  font-weight: 700;
  color: var(--purple);
}

.latest-badge {
  flex: none;
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  padding: 1px 9px;
  border-radius: 0;
  color: var(--green);
  border: 1px solid currentColor;
}

.branch {
  font-family: var(--mono);
  font-size: 15px;
  color: var(--dim);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.req {
  font-size: 16px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sub {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 15px;
}

.stats {
  flex: none;
  display: flex;
  gap: 14px;
  font-family: var(--mono);
  font-size: 15px;
  color: var(--dim);
  font-variant-numeric: tabular-nums;
}

.stat {
  white-space: nowrap;
}

.row-archive {
  flex: none;
  width: 28px;
  height: 28px;
  padding: 0;
  border: 0;
  border-radius: 0;
  background: transparent;
  color: var(--dim);
  font-family: inherit;
  font-size: 20px;
  line-height: 1;
  cursor: pointer;
  opacity: 0;
  transition:
    opacity 0.15s ease,
    background 0.15s ease,
    color 0.15s ease;
}

.row:hover .row-archive,
.row-archive:focus-visible {
  opacity: 1;
}

.row-archive:hover {
  background: rgba(255, 111, 103, 0.16);
  color: var(--red);
}

@media (max-width: 760px) {
  .stats,
  .rank {
    display: none;
  }
}
</style>
