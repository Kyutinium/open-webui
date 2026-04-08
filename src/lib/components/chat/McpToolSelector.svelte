<script context="module" lang="ts">
	let _mcpToolsCache: Array<{ id: string; name: string; server: string }> = [];
	let _mcpDefaultSelection: string[] = [];
</script>

<script lang="ts">
	import { onMount, getContext, tick } from 'svelte';
	import { WEBUI_BASE_URL } from '$lib/constants';
	import Dropdown from '$lib/components/common/Dropdown.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';

	const i18n = getContext('i18n');

	export let selectedMcpTools: string[] = [];

	let mcpTools: Array<{ id: string; name: string; server: string }> = _mcpToolsCache;
	let loaded = _mcpToolsCache.length > 0;

	onMount(async () => {
		if (_mcpToolsCache.length > 0) {
			mcpTools = _mcpToolsCache;
			loaded = true;
			return;
		}
		try {
			const resp = await fetch(`${WEBUI_BASE_URL}/api/v1/mcp_tools`, {
				credentials: 'include'
			});
			if (resp.ok) {
				mcpTools = await resp.json();
				_mcpToolsCache = mcpTools;
				if (mcpTools.length > 0) {
					_mcpDefaultSelection = mcpTools.map((t) => t.id);
					if (selectedMcpTools.length === 0) {
						selectedMcpTools = [..._mcpDefaultSelection];
					}
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
	<Dropdown side="top" align="start">
		<Tooltip content={$i18n.t('Search Tools')} placement="top">
			<button
				class="translate-y-[0.5px] px-1 flex gap-1 items-center rounded-lg self-center transition {selectedMcpTools.length > 0
					? 'text-blue-600 dark:text-blue-400 hover:text-blue-700'
					: 'text-gray-400 hover:text-gray-600 dark:hover:text-gray-300'}"
				type="button"
			>
				<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" class="size-4">
					<path stroke-linecap="round" stroke-linejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m5.231 13.481L15 17.25m-4.5-15H5.625c-.621 0-1.125.504-1.125 1.125v16.5c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Zm3.75 11.625a2.625 2.625 0 1 1-5.25 0 2.625 2.625 0 0 1 5.25 0Z" />
				</svg>
				{#if someSelected}
					<span class="text-xs">{selectedMcpTools.length}</span>
				{/if}
			</button>
		</Tooltip>

		<div slot="content">
			<div class="min-w-52 max-w-60 rounded-2xl px-1 py-1 border border-gray-100 dark:border-gray-800 z-50 bg-white dark:bg-gray-850 dark:text-white shadow-lg max-h-72 overflow-y-auto scrollbar-thin">
				<div class="px-3 py-1.5 text-[10px] text-gray-400 uppercase tracking-wider">
					{$i18n.t('Search Tools')}
				</div>

				<!-- Select All -->
				<button
					type="button"
					class="flex w-full justify-between gap-2 items-center px-3 py-1.5 text-sm cursor-pointer rounded-xl hover:bg-gray-50 dark:hover:bg-gray-800/50"
					on:click={toggleAll}
				>
					<div class="line-clamp-1 text-xs">{$i18n.t('All')}</div>
					<div class="shrink-0">
						<Switch state={allSelected} on:change={async () => { await tick(); }} />
					</div>
				</button>

				<hr class="border-gray-50 dark:border-gray-800 mx-2 my-0.5" />

				<!-- Individual tools -->
				{#each mcpTools as tool}
					<button
						type="button"
						class="flex w-full justify-between gap-2 items-center px-3 py-1.5 text-sm cursor-pointer rounded-xl hover:bg-gray-50 dark:hover:bg-gray-800/50"
						on:click={() => toggleTool(tool.id)}
					>
						<div class="line-clamp-1 text-xs">{tool.name}</div>
						<div class="shrink-0">
							<Switch
								state={selectedMcpTools.includes(tool.id)}
								on:change={async (e) => {
									const state = e.detail;
									await tick();
								}}
							/>
						</div>
					</button>
				{/each}
			</div>
		</div>
	</Dropdown>
{/if}
