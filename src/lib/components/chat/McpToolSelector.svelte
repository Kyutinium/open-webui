<script context="module" lang="ts">
	let _mcpToolsCache: Array<{ id: string; name: string; server: string; requires_confluence_auth?: boolean }> = [];
	let _mcpDefaultSelection: string[] = [];
	let _mcpLastSelection: string[] | null = null;
	let _confluenceAuthenticated = false;
</script>

<script lang="ts">
	import { onMount, getContext, tick } from 'svelte';
	import { WEBUI_BASE_URL } from '$lib/constants';
	import Dropdown from '$lib/components/common/Dropdown.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';

	const i18n = getContext('i18n');

	export let selectedMcpTools: string[] = [];
	export let confluenceToken: string = '';

	let mcpTools: Array<{ id: string; name: string; server: string; requires_confluence_auth?: boolean }> = _mcpToolsCache;
	let loaded = _mcpToolsCache.length > 0;
	let checkingAuth = false;

	onMount(async () => {
		if (_mcpLastSelection !== null && selectedMcpTools.length === 0) {
			selectedMcpTools = [..._mcpLastSelection];
		}
		if (_mcpToolsCache.length > 0) {
			mcpTools = _mcpToolsCache;
			loaded = true;
		} else {
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
		}
		// Check confluence auth on mount
		if (!_confluenceAuthenticated) {
			await checkConfluenceAuth();
		} else {
			confluenceToken = confluenceToken || '';
		}
	});

	async function checkConfluenceAuth(): Promise<boolean> {
		try {
			const resp = await fetch(`${WEBUI_BASE_URL}/api/v1/confluence/check`, {
				credentials: 'include'
			});
			if (resp.ok) {
				const data = await resp.json();
				if (data.authenticated && data.token) {
					_confluenceAuthenticated = true;
					confluenceToken = data.token;
					return true;
				}
			}
		} catch (e) {
			console.error('Confluence auth check failed:', e);
		}
		_confluenceAuthenticated = false;
		confluenceToken = '';
		return false;
	}

	function needsConfluenceAuth(toolId: string): boolean {
		const tool = mcpTools.find((t) => t.id === toolId);
		return tool?.requires_confluence_auth === true;
	}

	function hasAnyConfluenceToolSelected(): boolean {
		return selectedMcpTools.some((id) => needsConfluenceAuth(id));
	}

	async function openConfluenceLogin() {
		checkingAuth = true;
		// Open confluence login in popup
		const loginUrl = `${await getLoginUrl()}`;
		const popup = window.open(loginUrl, 'confluence_login', 'width=600,height=700');

		// Poll for login completion
		const pollInterval = setInterval(async () => {
			// Check if popup was closed
			if (popup && popup.closed) {
				clearInterval(pollInterval);
				const success = await checkConfluenceAuth();
				checkingAuth = false;
				if (!success) {
					// Remove confluence tools from selection if auth failed
					selectedMcpTools = selectedMcpTools.filter((id) => !needsConfluenceAuth(id));
					_mcpLastSelection = [...selectedMcpTools];
				}
				return;
			}
			// Also check periodically in case cookie was set
			const success = await checkConfluenceAuth();
			if (success) {
				clearInterval(pollInterval);
				checkingAuth = false;
				if (popup && !popup.closed) popup.close();
			}
		}, 2000);

		// Timeout after 5 minutes
		setTimeout(() => {
			clearInterval(pollInterval);
			checkingAuth = false;
		}, 300000);
	}

	async function getLoginUrl(): Promise<string> {
		try {
			const resp = await fetch(`${WEBUI_BASE_URL}/api/v1/confluence/check`, {
				credentials: 'include'
			});
			if (resp.ok) {
				const data = await resp.json();
				return data.login_url || 'https://confluence.gwanghands.net/login.action';
			}
		} catch {}
		return 'https://confluence.gwandhands.net/login.action';
	}

	async function toggleTool(id: string) {
		if (selectedMcpTools.includes(id)) {
			selectedMcpTools = selectedMcpTools.filter((t) => t !== id);
		} else {
			// If enabling a confluence tool, check auth first
			if (needsConfluenceAuth(id) && !_confluenceAuthenticated) {
				selectedMcpTools = [...selectedMcpTools, id];
				await openConfluenceLogin();
			} else {
				selectedMcpTools = [...selectedMcpTools, id];
			}
		}
		_mcpLastSelection = [...selectedMcpTools];
	}

	async function toggleAll() {
		if (selectedMcpTools.length === mcpTools.length) {
			selectedMcpTools = [];
		} else {
			selectedMcpTools = mcpTools.map((t) => t.id);
			// Check if any confluence tools need auth
			if (hasAnyConfluenceToolSelected() && !_confluenceAuthenticated) {
				await openConfluenceLogin();
			}
		}
		_mcpLastSelection = [...selectedMcpTools];
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
						<div class="flex items-center gap-1.5 line-clamp-1 text-xs">
							{tool.name}
							{#if tool.requires_confluence_auth && !_confluenceAuthenticated}
								<span class="text-[9px] text-amber-500" title="Login required">*</span>
							{/if}
						</div>
						<div class="shrink-0">
							{#if checkingAuth && needsConfluenceAuth(tool.id)}
								<div class="w-8 flex justify-center">
									<div class="size-3 border-2 border-blue-500 border-t-transparent rounded-full animate-spin" />
								</div>
							{:else}
								<Switch
									state={selectedMcpTools.includes(tool.id)}
									on:change={async (e) => {
										const state = e.detail;
										await tick();
									}}
								/>
							{/if}
						</div>
					</button>
				{/each}

				{#if checkingAuth}
					<div class="px-3 py-1.5 text-[10px] text-amber-500">
						Confluence login in progress...
					</div>
				{/if}
			</div>
		</div>
	</Dropdown>
{/if}
