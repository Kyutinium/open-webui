<script lang="ts">
	import { getContext } from 'svelte';

	import { terminalServers, selectedTerminalId } from '$lib/stores';

	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Cloud from '$lib/components/icons/Cloud.svelte';

	const i18n = getContext('i18n');

	// Kept for API compatibility with MessageInput's bind:show.
	export let show = false;

	$: systemTerminals = ($terminalServers ?? []).filter((t) => t.id);
	$: selectedSystemTerminal = systemTerminals.find((t) => t.id === $selectedTerminalId);
</script>

<div class="flex items-center translate-x-0.5">
	<!-- The terminal is admin-designated and connected automatically (see
	     (app)/+layout.svelte); show a read-only "File Manager" badge — users
	     interact with it as the workspace file browser, so the server
	     name/id is deliberately not surfaced. -->
	{#if $selectedTerminalId && selectedSystemTerminal}
		<Tooltip content={$i18n.t('File Manager')} placement="top">
			<div
				class="flex items-center gap-1.5 translate-y-[1px] text-sm rounded-lg px-2.5 py-1 cursor-default"
			>
				<Cloud className="size-3.5" strokeWidth="2" />
				<span class="truncate text-[13px] max-w-[100px] sm:max-w-[150px]"
					>{$i18n.t('File Manager')}</span
				>
				<!-- Connected indicator light -->
				<span class="relative flex size-1.5 shrink-0">
					<span
						class="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-60"
					></span>
					<span
						class="relative inline-flex size-1.5 rounded-full bg-emerald-500 dark:bg-emerald-400"
					></span>
				</span>
			</div>
		</Tooltip>
	{/if}
</div>
