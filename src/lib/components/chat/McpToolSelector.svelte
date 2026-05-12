<script context="module" lang="ts">
	type McpTool = {
		id: string;
		name: string;
		server: string;
		requires_confluence_auth?: boolean;
		default?: boolean;
	};
	let _mcpToolsCache: Array<McpTool> = [];
	let _mcpLastSelection: string[] | null = null;
	let _confluenceAuthenticated = false;
</script>

<script lang="ts">
	import { onMount, getContext, tick } from 'svelte';
	import { WEBUI_BASE_URL } from '$lib/constants';
	import Dropdown from '$lib/components/common/Dropdown.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import ConfluenceLoginModal from './ConfluenceLoginModal.svelte';

	const i18n = getContext('i18n');

	export let selectedMcpTools: string[] = [];
	export let confluenceSessionCookie: string = '';
	export let selectedModelNames: string[] = [];

	let mcpTools: Array<McpTool> = _mcpToolsCache;
	let loaded = _mcpToolsCache.length > 0;
	let checkingAuth = false;
	let showLoginModal = false;
	let loginUrl = '';

	// Tools flagged `default: true` in mcp-config.json are always-on and hidden from the UI.
	$: defaultToolIds = mcpTools.filter((t) => t.default).map((t) => t.id);
	$: optionalTools = mcpTools.filter((t) => !t.default);

	// "instant" models are plain LLMs — the Search Tools UI is hidden entirely for them.
	$: hideForInstant = selectedModelNames.some((n) => (n ?? '').toLowerCase().includes('instant'));

	function uniqueUnion(a: string[], b: string[]): string[] {
		return Array.from(new Set([...a, ...b]));
	}

	// Keep default tool IDs in selectedMcpTools whenever the default list changes.
	$: if (loaded && defaultToolIds.length > 0) {
		const merged = uniqueUnion(defaultToolIds, selectedMcpTools);
		if (merged.length !== selectedMcpTools.length) {
			selectedMcpTools = merged;
		}
	}

	// Load/save selection from localStorage (per-user persistence)
	function saveSelection() {
		try {
			localStorage.setItem('mcpToolSelection', JSON.stringify(selectedMcpTools));
		} catch {}
	}

	function loadSelection(): string[] | null {
		try {
			const saved = localStorage.getItem('mcpToolSelection');
			if (saved !== null) return JSON.parse(saved);
		} catch {}
		return null;
	}

	onMount(async () => {
		// Load tools (cache or fetch) before settling on a selection so the first-time
		// fallback has the actual tool list to work with.
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
				}
			} catch (e) {
				console.error('Failed to load MCP tools:', e);
			}
			loaded = true;
		}

		// Chat.svelte resets the bound `selectedMcpTools` to [] on every remount (e.g.
		// navigating between chats or to the home route). Always re-derive selection
		// here so it survives those remounts:
		//   1. localStorage holds the authoritative state (incl. user-explicit empty).
		//   2. Module-scope cache as a fallback within a single page session.
		//   3. First-time fallback: turn all optional tools on.
		const saved = loadSelection();
		if (saved !== null) {
			selectedMcpTools = saved;
			_mcpLastSelection = saved;
		} else if (_mcpLastSelection !== null) {
			selectedMcpTools = [..._mcpLastSelection];
		} else if (mcpTools.length > 0) {
			selectedMcpTools = mcpTools.filter((t) => !t.default).map((t) => t.id);
			_mcpLastSelection = [...selectedMcpTools];
			saveSelection();
		}

		// Check confluence auth on mount
		if (!_confluenceAuthenticated) {
			const authed = await checkConfluenceAuth();
			if (!authed && hasAnyConfluenceToolSelected()) {
				await promptConfluenceLogin();
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
				if (data.authenticated) {
					_confluenceAuthenticated = true;
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

	/** Show login modal asking user to confirm before opening popup */
	async function promptConfluenceLogin() {
		loginUrl = await getLoginUrl();
		showLoginModal = true;
	}

	/** Actually open the popup after user confirms in the modal */
	function startConfluenceLoginFlow() {
		showLoginModal = false;
		if (!loginUrl) return;

		checkingAuth = true;
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

	/** User cancelled the login modal — turn off confluence tools */
	function cancelConfluenceLogin() {
		showLoginModal = false;
		selectedMcpTools = selectedMcpTools.filter((id) => !needsConfluenceAuth(id));
		_mcpLastSelection = [...selectedMcpTools];
		saveSelection();
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
		// Default tools cannot be toggled off.
		if (defaultToolIds.includes(id)) return;

		if (selectedMcpTools.includes(id)) {
			selectedMcpTools = selectedMcpTools.filter((t) => t !== id);
		} else {
			if (needsConfluenceAuth(id) && !_confluenceAuthenticated) {
				selectedMcpTools = [...selectedMcpTools, id];
				await promptConfluenceLogin();
			} else {
				selectedMcpTools = [...selectedMcpTools, id];
			}
		}
		_mcpLastSelection = [...selectedMcpTools];
		saveSelection();
	}

	async function toggleAll() {
		const optionalIds = optionalTools.map((t) => t.id);
		const allOptionalSelected =
			optionalIds.length > 0 && optionalIds.every((id) => selectedMcpTools.includes(id));
		if (allOptionalSelected) {
			selectedMcpTools = selectedMcpTools.filter((id) => !optionalIds.includes(id));
		} else {
			selectedMcpTools = uniqueUnion(selectedMcpTools, optionalIds);
			if (hasAnyConfluenceToolSelected() && !_confluenceAuthenticated) {
				await promptConfluenceLogin();
			}
		}
		_mcpLastSelection = [...selectedMcpTools];
		saveSelection();
	}

	$: optionalSelectedCount = optionalTools.filter((t) => selectedMcpTools.includes(t.id)).length;
	$: allSelected = optionalTools.length > 0 && optionalSelectedCount === optionalTools.length;
	$: someSelected = optionalSelectedCount > 0 && optionalSelectedCount < optionalTools.length;
	$: noneSelected = optionalSelectedCount === 0;
</script>

{#if loaded && optionalTools.length > 0 && !hideForInstant}
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
			{#if allSelected}
				<span class="opacity-60">(all)</span>
			{:else if someSelected}
				<span class="opacity-60">({optionalSelectedCount})</span>
			{:else if noneSelected}
				<span class="opacity-60">(off)</span>
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
					<div class="line-clamp-1 text-xs text-left">{$i18n.t('All')}</div>
					<div class="shrink-0">
						<Switch state={allSelected} on:change={async () => { await tick(); }} />
					</div>
				</button>

				<hr class="border-gray-50 dark:border-gray-800 mx-2 my-0.5" />

				<!-- Individual tools -->
				{#each optionalTools as tool}
					<button
						type="button"
						class="flex w-full justify-between gap-2 items-center px-3 py-1.5 text-sm cursor-pointer rounded-xl hover:bg-gray-50 dark:hover:bg-gray-800/50"
						on:click={() => toggleTool(tool.id)}
					>
						<div class="flex items-center gap-1.5 line-clamp-1 text-xs text-left flex-1">
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

{#if showLoginModal}
	<ConfluenceLoginModal
		{loginUrl}
		onLogin={startConfluenceLoginFlow}
		onCancel={cancelConfluenceLogin}
	/>
{/if}
