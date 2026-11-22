<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref } from "vue";
import type { Run, Session } from "../types";
import RunTurn from "./RunTurn.vue";

const props = defineProps<{ runs: Run[]; run: Run | null; session: Session }>();
const turns = computed(() => props.runs.slice().reverse());
const viewport = ref<HTMLElement | null>(null);
const content = ref<HTMLElement | null>(null);
let followBottom = true;
let lastScrollTop = 0;
let observer: ResizeObserver | undefined;

function scrollToLatest(): void {
  followBottom = true;
  const box = viewport.value;
  if (box) {
    box.scrollTop = box.scrollHeight;
    lastScrollTop = box.scrollTop;
  }
}
function trackPosition(): void {
  const box = viewport.value;
  if (!box) return;
  // 异步排版可能在上一次滚动事件送达前增高正文。只有向上滚动才取消跟随。
  if (box.scrollTop < lastScrollTop - 1) followBottom = false;
  if (box.scrollHeight - box.clientHeight - box.scrollTop < 24)
    followBottom = true;
  lastScrollTop = box.scrollTop;
}
function jumpTo(id: string): void {
  const box = viewport.value;
  const turn = content.value?.querySelector<HTMLElement>(
    '[data-run-id="' + CSS.escape(id) + '"]',
  );
  if (!box || !turn) return;
  followBottom = false;
  box.scrollTop +=
    turn.getBoundingClientRect().top - box.getBoundingClientRect().top;
  turn.focus({ preventScroll: true });
}
function openAtReadingPosition(event: MouseEvent): void {
  if ((event.target as Element).closest("button, summary, input, select"))
    followBottom = false;
}
onMounted(() => {
  scrollToLatest();
  observer = new ResizeObserver(() => {
    if (followBottom && viewport.value?.clientHeight) scrollToLatest();
  });
  observer.observe(content.value!);
  observer.observe(viewport.value!);
});
onBeforeUnmount(() => observer?.disconnect());
defineExpose({ jumpTo, scrollToLatest });
</script>

<template>
  <div
    ref="viewport"
    class="conversation-viewport"
    @scroll.passive="trackPosition"
    @click.capture="openAtReadingPosition"
  >
    <div ref="content" class="conversation-content">
      <div v-if="!turns.length" class="empty empty-run">
        <span class="empty-glyph" aria-hidden="true">[ / ]</span>
        <h3>今天想做点什么？</h3>
        <p>从一个问题、一个想法，或一段需要改进的代码开始。</p>
      </div>
      <RunTurn
        v-for="(entry, index) in turns"
        :key="entry.id"
        :entry="entry"
        :number="index + 1"
        :selected="run?.id === entry.id ? run : undefined"
        :session="session"
        tabindex="-1"
      >
        <template #activity><slot name="activity" /></template>
        <template #default="{ item }"><slot :item="item" /></template>
      </RunTurn>
    </div>
  </div>
</template>
