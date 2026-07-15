<script lang="ts">
	import { getContext } from 'svelte';

	import { config } from '$lib/stores';
	import Tooltip from '$lib/components/common/Tooltip.svelte';

	const i18n = getContext('i18n');

	export let history: any = null;

	// Emitted by the oh-my-gateway pipe at the end of each turn:
	// <details type="context_usage" done="true"><summary>…</summary>{"used":N,"max":M}</details>
	const MARKER_RE =
		/<details\s+type="context_usage"[^>]*>\s*<summary>[^<]*<\/summary>\s*([\s\S]*?)\s*<\/details>/g;

	// Newest usage marker on the current branch: walk from currentId up the
	// parent chain and take the last marker inside the first message that has
	// one (later turns sit closer to currentId, so the first hit is newest).
	const findUsage = (h: any): { used: number; max: number } | null => {
		if (!h?.currentId || !h?.messages) return null;
		let id: string | null = h.currentId;
		let hops = 0;
		while (id && hops < 100) {
			const m = h.messages[id];
			if (!m) break;
			if (m.role === 'assistant' && typeof m.content === 'string') {
				MARKER_RE.lastIndex = 0;
				let match: RegExpExecArray | null;
				let last: string | null = null;
				while ((match = MARKER_RE.exec(m.content)) !== null) last = match[1];
				if (last) {
					try {
						const parsed = JSON.parse(last);
						if (Number(parsed?.used) > 0 && Number(parsed?.max) > 0) {
							return { used: Number(parsed.used), max: Number(parsed.max) };
						}
					} catch {
						// malformed marker — keep walking
					}
				}
			}
			id = m.parentId ?? null;
			hops++;
		}
		return null;
	};

	$: usage = findUsage(history);
	$: ratio = usage ? usage.used / usage.max : 0;
	$: level = ratio >= 0.85 ? 'red' : ratio >= 0.6 ? 'yellow' : 'green';

	const fmtK = (n: number) => `${Math.max(1, Math.round(n / 1000))}k`;
</script>

<!-- Admin-togglable: Admin Settings > General > Show Context Usage -->
{#if usage && ($config?.features?.enable_context_usage ?? true)}
	<Tooltip content={$i18n.t('Context usage')} placement="top">
		<div
			class="flex items-center gap-1.5 translate-y-[1px] text-sm rounded-lg px-2 py-1 cursor-default"
		>
			<span
				class="size-1.5 rounded-full shrink-0 {level === 'red'
					? 'bg-red-500 dark:bg-red-400'
					: level === 'yellow'
						? 'bg-amber-500 dark:bg-amber-400'
						: 'bg-emerald-500 dark:bg-emerald-400'}"
			></span>
			<span
				class="text-[12px] tabular-nums {level === 'red'
					? 'text-red-600 dark:text-red-400'
					: level === 'yellow'
						? 'text-amber-600 dark:text-amber-400'
						: 'text-gray-400 dark:text-gray-500'}"
			>
				{fmtK(usage.used)} / {fmtK(usage.max)}
			</span>
		</div>
	</Tooltip>
{/if}
