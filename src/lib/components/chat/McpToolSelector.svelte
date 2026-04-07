<script context="module" lang="ts">
	let _mcpInitialized = false;
</script>

<script lang="ts">
	import { onMount, getContext } from 'svelte';
	import { WEBUI_BASE_URL } from '$lib/constants';
	import Tooltip from '$lib/components/common/Tooltip.svelte';

	const i18n = getContext('i18n');

	export let selectedMcpTools: string[] = [];

	let mcpTools: Array<{ id: string; name: string; server: string }> = [];
	let showDropdown = false;
	let loaded = false;

	onMount(async () => {
		try {
			const resp = await fetch(`${WEBUI_BASE_URL}/api/v1/mcp_tools`, {
				credentials: 'include'
			});
			if (resp.ok) {
				mcpTools = await resp.json();
				// Only set defaults on first ever mount, not re-mounts
				if (!_mcpInitialized && selectedMcpTools.length === 0 && mcpTools.length > 0) {
					selectedMcpTools = mcpTools.map((t) => t.id);
					_mcpInitialized = true;
				}
			}
		} catch (e) {
			console.error('Failed to load MCP tools:', e);
		}
		loaded = true;
	});

	function toggleTool(id: string) {
		if (selectedMcpTools.includes(id)) {
			selectedMcpTools = selectedMcpTools.filter((t) => t !== id);
		} else {
			selectedMcpTools = [...selectedMcpTools, id];
		}
	}

	function toggleAll() {
		if (selectedMcpTools.length === mcpTools.length) {
			selectedMcpTools = [];
		} else {
			selectedMcpTools = mcpTools.map((t) => t.id);
		}
	}

	$: allSelected = mcpTools.length > 0 && selectedMcpTools.length === mcpTools.length;
	$: someSelected = selectedMcpTools.length > 0 && selectedMcpTools.length < mcpTools.length;
</script>

{#if loaded && mcpTools.length > 0}
	<div class="relative">
		<Tooltip content={$i18n.t('Search Tools')} placement="top">
			<button
				class="translate-y-[0.5px] px-1 flex gap-1 items-center rounded-lg self-center transition {selectedMcpTools.length > 0
					? 'text-blue-600 dark:text-blue-400 hover:text-blue-700'
					: 'text-gray-400 hover:text-gray-600 dark:hover:text-gray-300'}"
				type="button"
				on:click={() => (showDropdown = !showDropdown)}
			>
				<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" class="size-4">
					<path stroke-linecap="round" stroke-linejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m5.231 13.481L15 17.25m-4.5-15H5.625c-.621 0-1.125.504-1.125 1.125v16.5c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Zm3.75 11.625a2.625 2.625 0 1 1-5.25 0 2.625 2.625 0 0 1 5.25 0Z" />
				</svg>
				{#if someSelected}
					<span class="text-xs">{selectedMcpTools.length}</span>
				{/if}
			</button>
		</Tooltip>

		{#if showDropdown}
			<!-- svelte-ignore a11y-click-events-have-key-events -->
			<!-- svelte-ignore a11y-no-static-element-interactions -->
			<div
				class="fixed inset-0 z-40"
				on:click={() => (showDropdown = false)}
			/>
			<div class="absolute bottom-full left-0 mb-2 w-52 bg-white dark:bg-gray-850 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg z-50 py-1">
				<div class="px-3 py-1.5 text-[10px] text-gray-400 uppercase tracking-wider">
					{$i18n.t('Search Tools')}
				</div>

				<!-- Select All -->
				<button
					class="w-full flex items-center justify-between px-3 py-1.5 text-xs hover:bg-gray-50 dark:hover:bg-gray-800 transition"
					on:click={toggleAll}
				>
					<span class="text-gray-700 dark:text-gray-300 font-medium">{$i18n.t('All')}</span>
					<div class="w-8 h-4.5 rounded-full transition-colors duration-200 {allSelected ? 'bg-blue-500' : someSelected ? 'bg-blue-300' : 'bg-gray-300 dark:bg-gray-600'} relative">
						<div class="absolute top-0.5 left-0.5 w-3.5 h-3.5 rounded-full bg-white shadow transition-transform duration-200 {allSelected || someSelected ? 'translate-x-3.5' : ''}" />
					</div>
				</button>

				<div class="border-t border-gray-100 dark:border-gray-800 my-0.5" />

				<!-- Individual tools -->
				{#each mcpTools as tool}
					{@const isOn = selectedMcpTools.includes(tool.id)}
					<button
						class="w-full flex items-center justify-between px-3 py-1.5 text-xs hover:bg-gray-50 dark:hover:bg-gray-800 transition"
						on:click={() => toggleTool(tool.id)}
					>
						<span class="text-gray-700 dark:text-gray-300">{tool.name}</span>
						<div class="w-8 h-4.5 rounded-full transition-colors duration-200 {isOn ? 'bg-blue-500' : 'bg-gray-300 dark:bg-gray-600'} relative">
							<div class="absolute top-0.5 left-0.5 w-3.5 h-3.5 rounded-full bg-white shadow transition-transform duration-200 {isOn ? 'translate-x-3.5' : ''}" />
						</div>
					</button>
				{/each}
			</div>
		{/if}
	</div>
{/if}
