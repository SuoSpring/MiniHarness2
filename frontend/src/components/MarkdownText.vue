<script lang="ts">
import MarkdownIt from "markdown-it";
import { katex } from "@mdit/plugin-katex";
import "katex/dist/katex.min.css";

// 模型文本只通过 Markdown 语法生成元素，原始 HTML 按文字显示。
const renderer = new MarkdownIt({ html: false, breaks: true }).use(katex, {
  delimiters: "all",
  mathFence: true,
  trust: false,
  throwOnError: false,
});
</script>

<script setup lang="ts">
import { computed } from "vue";
const props = defineProps<{ text: string }>();
const html = computed(() => renderer.render(props.text));
</script>

<template><div class="markdown-body" v-html="html"></div></template>
