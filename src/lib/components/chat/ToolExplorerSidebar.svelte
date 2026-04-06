<script lang="ts">
	import { getContext } from 'svelte';
	import { showImageGallery, imageGalleryData } from '$lib/stores';
	import XMark from '$lib/components/icons/XMark.svelte';

	const i18n = getContext('i18n');

	export let toolData: Record<string, Array<{ query: string; results: Array<{
		title: string;
		content: string;
		url: string;
		thumbnail: string;
		doc_type: string;
	}> }>> = {};

	export let onClose: () => void = () => {};

	// Tab state
	let activeTab = '';
	$: tabs = Object.keys(toolData || {});
	$: if (tabs.length > 0 && !tabs.includes(activeTab)) {
		activeTab = tabs[0];
	}

	// Total results count per tab
	function tabCount(tab: string): number {
		return (toolData?.[tab] || []).reduce((sum, call) => sum + (call?.results?.length || 0), 0);
	}

	// Expand/collapse state for each query
	let expandedQueries: Record<string, boolean> = {};

	function toggleQuery(key: string) {
		expandedQueries[key] = !expandedQueries[key];
	}

	// Search
	let searchQuery = '';

	$: currentCalls = toolData[activeTab] || [];
	$: filteredCalls = searchQuery.trim()
		? currentCalls.map((call) => ({
				...call,
				results: call.results.filter(
					(r) =>
						r.title.toLowerCase().includes(searchQuery.toLowerCase()) ||
						r.content.toLowerCase().includes(searchQuery.toLowerCase()) ||
						r.doc_type.toLowerCase().includes(searchQuery.toLowerCase())
				)
			})).filter((call) => call.results.length > 0)
		: currentCalls;

	$: totalFiltered = filteredCalls.reduce((sum, call) => sum + call.results.length, 0);

	function openThumbnail(thumbnail: string, results: Array<{ thumbnail: string }>) {
		const thumbs = results.map((r) => r.thumbnail).filter(Boolean);
		if (thumbs.length > 0) {
			imageGalleryData.set({
				images: thumbs,
				current: thumbnail.split('/').pop() || ''
			});
			showImageGallery.set(true);
		}
	}

	// Friendly tab labels
	function tabLabel(tab: string): string {
		const labels: Record<string, string> = {
			confluence: 'Confluence',
			jira: 'Jira',
			mlm_cql: 'MLM',
			basic_knowledge: 'Knowledge',
			cql: 'Confluence',
		};
		return labels[tab] || tab.charAt(0).toUpperCase() + tab.slice(1);
	}
</script>

<div class="flex flex-col h-full bg-white dark:bg-gray-850 border-l border-gray-100 dark:border-gray-800">
	<!-- Header -->
	<div class="flex items-center justify-between px-3 py-2 border-b border-gray-100 dark:border-gray-800 shrink-0">
		<div class="flex items-center gap-2 min-w-0">
			<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" class="size-4 shrink-0 text-gray-500">
				<path stroke-linecap="round" stroke-linejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m5.231 13.481L15 17.25m-4.5-15H5.625c-.621 0-1.125.504-1.125 1.125v16.5c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Zm3.75 11.625a2.625 2.625 0 1 1-5.25 0 2.625 2.625 0 0 1 5.25 0Z" />
			</svg>
			<span class="text-sm font-medium text-gray-700 dark:text-gray-300 truncate">
				{$i18n.t('Tool Results')}
			</span>
		</div>
		<button
			class="p-1 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 transition text-gray-500"
			on:click={onClose}
		>
			<XMark className="size-4" />
		</button>
	</div>

	<!-- Tabs -->
	{#if tabs.length > 1}
		<div class="flex gap-1 px-2 pt-2 pb-1 overflow-x-auto scrollbar-hidden shrink-0">
			{#each tabs as tab}
				<button
					class="px-2.5 py-1 text-xs rounded-lg transition whitespace-nowrap {activeTab === tab
						? 'bg-gray-100 dark:bg-gray-800 font-medium text-gray-900 dark:text-white'
						: 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'}"
					on:click={() => (activeTab = tab)}
				>
					{tabLabel(tab)}
					<span class="ml-1 text-[10px] opacity-60">({tabCount(tab)})</span>
				</button>
			{/each}
		</div>
	{/if}

	<!-- Search -->
	<div class="px-2 py-1.5 shrink-0">
		<div class="relative">
			<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" class="size-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-400">
				<path fill-rule="evenodd" d="M9 3.5a5.5 5.5 0 1 0 0 11 5.5 5.5 0 0 0 0-11ZM2 9a7 7 0 1 1 12.452 4.391l3.328 3.329a.75.75 0 1 1-1.06 1.06l-3.329-3.328A7 7 0 0 1 2 9Z" clip-rule="evenodd" />
			</svg>
			<input
				type="text"
				bind:value={searchQuery}
				placeholder="{$i18n.t('Search results')}... ({totalFiltered})"
				class="w-full pl-8 pr-3 py-1.5 text-xs rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 text-gray-700 dark:text-gray-300 placeholder-gray-400 focus:outline-none focus:ring-1 focus:ring-blue-500"
			/>
		</div>
	</div>

	<!-- Results -->
	<div class="flex-1 overflow-y-auto scrollbar-hidden">
		{#if filteredCalls.length === 0}
			<div class="flex items-center justify-center h-24 text-xs text-gray-400">
				{searchQuery ? $i18n.t('No matching results') : $i18n.t('No results')}
			</div>
		{:else}
			{#each filteredCalls as call, callIdx}
				{@const queryKey = `${activeTab}-${callIdx}`}
				{@const isExpanded = expandedQueries[queryKey] !== false}
				<!-- Query row -->
				<button
					class="w-full flex items-center gap-2 px-3 py-2 text-left border-b border-gray-50 dark:border-gray-800/50 hover:bg-gray-50 dark:hover:bg-gray-800/50 transition"
					on:click={() => toggleQuery(queryKey)}
				>
					<svg
						xmlns="http://www.w3.org/2000/svg"
						viewBox="0 0 20 20"
						fill="currentColor"
						class="size-3 shrink-0 text-gray-400 transition-transform {isExpanded ? 'rotate-90' : ''}"
					>
						<path fill-rule="evenodd" d="M8.22 5.22a.75.75 0 0 1 1.06 0l4.25 4.25a.75.75 0 0 1 0 1.06l-4.25 4.25a.75.75 0 0 1-1.06-1.06L11.94 10 8.22 6.28a.75.75 0 0 1 0-1.06Z" clip-rule="evenodd" />
					</svg>
					<span class="flex-1 text-xs text-gray-700 dark:text-gray-300 truncate font-medium">
						{call.query || 'Search'}
					</span>
					<span class="text-[10px] text-gray-400 shrink-0">({call.results.length})</span>
				</button>

				<!-- Result items -->
				{#if isExpanded}
					{#each call.results as result, resultIdx}
						<div class="flex gap-2.5 px-3 py-2 ml-5 border-b border-gray-50/50 dark:border-gray-800/30 hover:bg-gray-50/50 dark:hover:bg-gray-800/30">
							<!-- Thumbnail -->
							{#if result.thumbnail}
								<button
									class="shrink-0 w-10 h-10 rounded overflow-hidden bg-gray-100 dark:bg-gray-800 cursor-pointer"
									on:click={() => openThumbnail(result.thumbnail, call.results)}
								>
									<img
										src={result.thumbnail}
										alt=""
										class="w-full h-full object-cover"
										loading="lazy"
									/>
								</button>
							{/if}

							<!-- Info -->
							<div class="flex-1 min-w-0">
								<div class="flex items-center gap-1.5">
									{#if result.doc_type}
										<span class="text-[9px] px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 shrink-0">
											{result.doc_type}
										</span>
									{/if}
									{#if result.url}
										<a
											href={result.url}
											target="_blank"
											rel="noopener noreferrer"
											class="text-xs font-medium text-blue-600 dark:text-blue-400 hover:underline truncate"
											title={result.title}
										>
											{result.title || 'Untitled'}
										</a>
									{:else}
										<span class="text-xs font-medium text-gray-700 dark:text-gray-300 truncate">
											{result.title || 'Untitled'}
										</span>
									{/if}
								</div>
								{#if result.content}
									<p class="text-[10px] text-gray-400 line-clamp-2 mt-0.5 leading-relaxed">
										{result.content}
									</p>
								{/if}
							</div>
						</div>
					{/each}
				{/if}
			{/each}
		{/if}
	</div>
</div>
