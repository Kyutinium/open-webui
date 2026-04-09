<script context="module" lang="ts">
	let _mcpToolsCache: Array<{ id: string; name: string; server: string; requires_confluence_auth?: boolean }> = [];
	let _mcpDefaultSelection: string[] = [];
	let _mcpLastSelection: string[] | null = null;
	let _confluenceAuthenticated = false;
	let _localStorageLoaded = false;
</script>

<script lang="ts">
	import { onMount, getContext, tick } from 'svelte';
	import { WEBUI_BASE_URL } from '$lib/constants';
	import Dropdown from '$lib/components/common/Dropdown.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';

	const i18n = getContext('i18n');

	export let selectedMcpTools: string[] = [];
	export let confluenceSessionCookie: string = '';

	let mcpTools: Array<{ id: string; name: string; server: string; requires_confluence_auth?: boolean }> = _mcpToolsCache;
	let loaded = _mcpToolsCache.length > 0;
	let checkingAuth = false;

	// Load/save selection from localStorage (per-user persistence)
	function saveSelection() {
		try {
			localStorage.setItem('mcpToolSelection', JSON.stringify(selectedMcpTools));
		} catch {}
	}

	function loadSelection(): string[] | null {
		try {
			const saved = localStorage.getItem('mcpToolSelection');
			if (saved) return JSON.parse(saved);
		} catch {}
		return null;
	}

	onMount(async () => {
		// Restore from localStorage first, then module cache
		if (!_localStorageLoaded) {
			const saved = loadSelection();
			if (saved !== null && saved.length > 0) {
				selectedMcpTools = saved;
				_mcpLastSelection = saved;
			} else if (_mcpLastSelection !== null && selectedMcpTools.length === 0) {
				selectedMcpTools = [..._mcpLastSelection];
			}
			_localStorageLoaded = true;
		} else if (_mcpLastSelection !== null && selectedMcpTools.length === 0) {
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
							saveSelection();
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
			const authed = await checkConfluenceAuth();
			if (!authed && hasAnyConfluenceToolSelected()) {
				await openConfluenceLogin();
			}
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
					confluenceSessionCookie = data.token;
					return true;
				}
			}
		} catch (e) {
			console.error('Confluence auth check failed:', e);
		}
		_confluenceAuthenticated = false;
		confluenceSessionCookie = '';
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
		const loginUrl = `${await getLoginUrl()}`;
		const popup = window.open(loginUrl, 'confluence_login', 'width=600,height=700');

		const pollInterval = setInterval(async () => {
			if (popup && popup.closed) {
				clearInterval(pollInterval);
				const success = await checkConfluenceAuth();
				checkingAuth = false;
				if (!success) {
					selectedMcpTools = selectedMcpTools.filter((id) => !needsConfluenceAuth(id));
					_mcpLastSelection = [...selectedMcpTools];
					saveSelection();
				}
				return;
			}
			const success = await checkConfluenceAuth();
			if (success) {
				clearInterval(pollInterval);
				checkingAuth = false;
				if (popup && !popup.closed) popup.close();
			}
		}, 2000);

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
		return 'https://confluence.gwanghands.net/login.action';
	}

	async function toggleTool(id: string) {
		if (selectedMcpTools.includes(id)) {
			selectedMcpTools = selectedMcpTools.filter((t) => t !== id);
		} else {
			if (needsConfluenceAuth(id) && !_confluenceAuthenticated) {
				selectedMcpTools = [...selectedMcpTools, id];
				await openConfluenceLogin();
			} else {
				selectedMcpTools = [...selectedMcpTools, id];
			}
		}
		_mcpLastSelection = [...selectedMcpTools];
		saveSelection();
	}

	async function toggleAll() {
		if (selectedMcpTools.length === mcpTools.length) {
			selectedMcpTools = [];
		} else {
			selectedMcpTools = mcpTools.map((t) => t.id);
			if (hasAnyConfluenceToolSelected() && !_confluenceAuthenticated) {
				await openConfluenceLogin();
			}
		}
		_mcpLastSelection = [...selectedMcpTools];
		saveSelection();
	}

	$: allSelected = mcpTools.length > 0 && selectedMcpTools.length === mcpTools.length;
	$: someSelected = selectedMcpTools.length > 0 && selectedMcpTools.length < mcpTools.length;
	$: noneSelected = selectedMcpTools.length === 0;
</script>

{#if loaded && mcpTools.length > 0}
	<Dropdown side="top" align="start">
		<button
			class="flex items-center gap-1 px-2 py-0.5 rounded-lg text-xs transition
				{noneSelected
					? 'text-gray-400 hover:text-gray-600 dark:hover:text-gray-300'
					: 'text-blue-600 dark:text-blue-400 hover:text-blue-700 dark:hover:text-blue-300'}"
			type="button"
		>
			<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" class="size-3.5">
				<path fill-rule="evenodd" d="M9 3.5a5.5 5.5 0 1 0 0 11 5.5 5.5 0 0 0 0-11ZM2 9a7 7 0 1 1 12.452 4.391l3.328 3.329a.75.75 0 1 1-1.06 1.06l-3.329-3.328A7 7 0 0 1 2 9Z" clip-rule="evenodd" />
			</svg>
			{$i18n.t('Search Tools')}
			{#if someSelected}
				<span class="opacity-60">({selectedMcpTools.length})</span>
			{/if}
		</button>

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
