<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef } from 'vue'
import { LayoutGrid, List } from 'lucide-vue-next'
import type { SessionSummary } from '../lib/types'
import { fetchSessions } from '../lib/api'
import { fmtCost, fmtTokens, ts } from '../lib/format'
import SessionCard from './SessionCard.vue'
import SessionRow from './SessionRow.vue'

type View = 'list' | 'cards'
const VIEW_KEY = 'sssf-sessions-view'

const sessions = shallowRef<SessionSummary[]>([])
const apiError = ref<string | null>(null)
const loaded = ref(false)
const nowMs = ref(Date.now())

function initialView(): View {
  try {
    return localStorage.getItem(VIEW_KEY) === 'cards' ? 'cards' : 'list'
  } catch {
    return 'list'
  }
}
const view = ref<View>(initialView())

function setView(next: View) {
  view.value = next
  try {
    localStorage.setItem(VIEW_KEY, next)
  } catch {
    /* private mode — the session keeps the choice */
  }
}

let timer: ReturnType<typeof setInterval> | undefined
let inflight = false

async function tick() {
  if (inflight) return
  inflight = true
  try {
    sessions.value = await fetchSessions()
    nowMs.value = Date.now()
    apiError.value = null
    loaded.value = true
  } catch (err) {
    apiError.value = err instanceof Error ? err.message : String(err)
  } finally {
    inflight = false
  }
}

onMounted(() => {
  void tick()
  timer = setInterval(() => void tick(), 500)
})

onUnmounted(() => clearInterval(timer))

/** Optimistic removal; an empty id means the write failed, so re-sync instead. */
function onArchived(adwId: string) {
  if (!adwId) {
    void tick()
    return
  }
  sessions.value = sessions.value.filter((s) => s.adw_id !== adwId)
}

/** Newest first — rank 1 is the latest run, in both views. */
const ordered = computed(() =>
  sessions.value.toSorted((a, b) => (ts(b.started_at) || 0) - (ts(a.started_at) || 0)),
)

const succeeded = computed(() => ordered.value.filter((s) => s.status === 'success').length)
const successPct = computed(() =>
  ordered.value.length ? Math.round((succeeded.value / ordered.value.length) * 100) : 0,
)
const totalTokens = computed(() =>
  ordered.value.reduce((sum, s) => sum + (s.total_tokens ?? 0), 0),
)
const totalCost = computed(() =>
  ordered.value.reduce((sum, s) => sum + (s.total_cost ?? 0), 0),
)
/** Pure-CSS donut: success arc over the track, no chart dependency. */
const donutStyle = computed(() => ({
  background: `conic-gradient(var(--green) 0% ${successPct.value}%, var(--border) ${successPct.value}% 100%)`,
}))
</script>

<template>
  <div class="sessions">
    <div v-if="apiError" class="error-bar">api unreachable — retrying {{ apiError }}</div>

    <div v-if="ordered.length" class="dash">
      <div class="dash-tile">
        <span class="dash-num">{{ ordered.length }}</span>
        <span class="dash-label dim">runs</span>
      </div>
      <div class="dash-tile">
        <span class="donut" :style="donutStyle"><span class="donut-hole">{{ successPct }}%</span></span>
        <span class="dash-label dim">{{ succeeded }}/{{ ordered.length }} green</span>
      </div>
      <div class="dash-tile">
        <span class="dash-num mono">{{ fmtTokens(totalTokens) }}</span>
        <span class="dash-label dim">tokens billed</span>
      </div>
      <div class="dash-tile">
        <span class="dash-num mono">{{ fmtCost(totalCost) }}</span>
        <span class="dash-label dim">total cost</span>
      </div>
      <div class="view-toggle" role="tablist" aria-label="Sessions view">
        <button
          type="button"
          role="tab"
          :aria-selected="view === 'list'"
          :class="{ active: view === 'list' }"
          @click="setView('list')"
        >
          <List :size="18" :stroke-width="2" /> List
        </button>
        <button
          type="button"
          role="tab"
          :aria-selected="view === 'cards'"
          :class="{ active: view === 'cards' }"
          @click="setView('cards')"
        >
          <LayoutGrid :size="18" :stroke-width="2" /> Cards
        </button>
      </div>
    </div>

    <div v-if="ordered.length && view === 'list'" class="rows">
      <SessionRow
        v-for="(s, i) in ordered"
        :key="s.adw_id"
        :session="s"
        :now-ms="nowMs"
        :rank="i"
        @archived="onArchived"
      />
    </div>
    <div v-if="ordered.length && view === 'cards'" class="cards">
      <SessionCard
        v-for="s in ordered"
        :key="s.adw_id"
        :session="s"
        :now-ms="nowMs"
        @archived="onArchived"
      />
    </div>
    <div v-else-if="loaded && !ordered.length" class="empty-state">
      no sessions yet — run an ADW to see it here
    </div>
    <div v-else-if="!apiError && !ordered.length" class="empty-state">loading sessions…</div>
  </div>
</template>

<style scoped>
.sessions {
  display: flex;
  flex-direction: column;
}

.dash {
  display: flex;
  align-items: stretch;
  gap: 14px;
  padding: 18px 24px 4px;
  flex-wrap: wrap;
}

.dash-tile {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 18px;
  border: 1px solid var(--border-soft);
  border-radius: 14px;
  background: var(--surface);
}

.dash-num {
  font-size: 22px;
  font-weight: 700;
}

.dash-num.mono {
  font-family: var(--mono);
  font-variant-numeric: tabular-nums;
}

.dash-label {
  font-size: 15px;
}

.donut {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  border-radius: 50%;
  flex: none;
}

.donut-hole {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: var(--panel);
  font-size: 11px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

.view-toggle {
  margin-left: auto;
  display: inline-flex;
  align-self: center;
  padding: 3px;
  gap: 2px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--panel);
}

.view-toggle button {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 7px 16px;
  border: 0;
  border-radius: 9px;
  background: transparent;
  color: var(--dim);
  font-family: inherit;
  font-size: 16px;
  cursor: pointer;
  transition:
    color 0.15s ease,
    background 0.15s ease;
}

.view-toggle button:hover {
  color: var(--text);
}

.view-toggle button.active {
  background: var(--panel-2);
  color: var(--text);
  box-shadow: inset 0 0 0 1px var(--border-soft);
}

.rows {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px 24px 28px;
  max-width: 1180px;
}

.cards {
  /* Uniform grid: every card the same width and (fixed in SessionCard) height,
     independent of content. */
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(460px, 1fr));
  gap: 18px;
  padding: 16px 24px 28px;
}
</style>
