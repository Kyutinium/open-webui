<script lang="ts">
	import { decode } from 'html-entities';
	import { onMount, getContext } from 'svelte';
	import { get } from 'svelte/store';
	const i18n = getContext('i18n');

	import fileSaver from 'file-saver';
	const { saveAs } = fileSaver;

	import { marked, type Token } from 'marked';
	import { copyToClipboard, unescapeHtml } from '$lib/utils';

	import { WEBUI_BASE_URL } from '$lib/constants';
	import { settings } from '$lib/stores';

	import CodeBlock from '$lib/components/chat/Messages/CodeBlock.svelte';
	import MarkdownInlineTokens from '$lib/components/chat/Messages/Markdown/MarkdownInlineTokens.svelte';
	import KatexRenderer from './KatexRenderer.svelte';
	import AlertRenderer, { alertComponent } from './AlertRenderer.svelte';
	import Collapsible from '$lib/components/common/Collapsible.svelte';
	import ToolCallDisplay from '$lib/components/common/ToolCallDisplay.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Download from '$lib/components/icons/Download.svelte';
	import ConsecutiveDetailsGroup from './ConsecutiveDetailsGroup.svelte';
	import SubagentGroup from './SubagentGroup.svelte';
	import AskUserQuestionCard from './AskUserQuestionCard.svelte';

	import HtmlToken from './HTMLToken.svelte';
	import Clipboard from '$lib/components/icons/Clipboard.svelte';
	import ColonFenceBlock from './ColonFenceBlock.svelte';

	import { showImageGallery, imageGalleryData, showToolExplorer, toolExplorerData, chatId } from '$lib/stores';

	// Track which chatId the tool explorer was last populated for
	let _toolExplorerChatId = '';

	function autoOpenToolExplorer(node: HTMLElement, params: { data: Record<string, any[]>; messageDone: boolean; turnId?: string }) {
		const { data, messageDone, turnId } = params;
		if (!data) return;
		const currentChatId = get(chatId);
		// If chatId changed since last population, reset first
		if (_toolExplorerChatId && _toolExplorerChatId !== currentChatId) {
			toolExplorerData.set(null);
		}
		_toolExplorerChatId = currentChatId;
		const existing = get(toolExplorerData);
		const tagged = (calls: any[]) => calls.map((c) => (turnId && !c.turnId ? { ...c, turnId } : c));
		if (existing) {
			const merged = { ...existing };
			for (const [key, calls] of Object.entries(data)) {
				if (!merged[key]) merged[key] = [];
				for (const call of tagged(calls as any[])) {
					const isDup = merged[key].some(
						(c: any) =>
							c.turnId === call.turnId &&
							c.query === call.query &&
							c.results?.length === call.results?.length
					);
					if (!isDup) merged[key] = [...merged[key], call];
				}
			}
			toolExplorerData.set(merged);
		} else {
			const seeded: Record<string, any[]> = {};
			for (const [key, calls] of Object.entries(data)) {
				seeded[key] = tagged(calls as any[]);
			}
			toolExplorerData.set(seeded);
		}
		// Populate only — do NOT auto-open the Tool Results tab on render, which
		// stole focus from the tab the user was viewing. The explicit
		// "View searched documents" button still opens it on click.
	}

	export let id: string;
	export let messageId: string = '';
	export let tokens: Token[];
	export let top = true;
	export let attributes = {};
	export let sourceIds = [];

	export let done = true;

	export let save = false;
	export let preview = false;

	export let paragraphTag = 'p';

	export let editCodeBlock = true;
	export let topPadding = false;

	export let onSave: Function = () => {};
	export let onUpdate: Function = () => {};
	export let onPreview: Function = () => {};

	export let onTaskClick: Function = () => {};
	export let onSourceClick: Function = () => {};

	const headerComponent = (depth: number) => {
		return 'h' + depth;
	};

	// Claude Code style grouping: a turn's reasoning + tool_calls +
	// code_interpreter collapse into one ConsecutiveDetailsGroup that summarizes
	// the work ("Explored · thought 2 times, 3 Grep") and, while it's still
	// working, auto-expands so the user can watch the thinking and tool calls
	// live (see ConsecutiveDetailsGroup). Subagent tool calls are pulled out
	// separately into their own labeled group (isSubagentToolToken below), so a
	// subagent's run never folds into the main-agent summary.
	const GROUPABLE_DETAIL_TYPES = new Set(['tool_calls', 'reasoning', 'code_interpreter']);

	const isGroupableDetailToken = (token: Token & { attributes?: { type?: string } }) => {
		return token?.type === 'details' && GROUPABLE_DETAIL_TYPES.has(token?.attributes?.type ?? '');
	};

	// A subagent's tool call: a tool_calls detail tagged with the parent Task id
	// by the gateway pipeline. These are pulled out of the normal consecutive
	// grouping and collected under a dedicated, labeled subagent group.
	const isSubagentToolToken = (
		token: Token & { attributes?: { type?: string; parent?: string } }
	) => {
		return (
			token?.type === 'details' &&
			token?.attributes?.type === 'tool_calls' &&
			!!token?.attributes?.parent
		);
	};

	// Out-of-band detail blocks that render nothing inline (the hidden
	// tool_explorer sidebar trigger, the end-of-turn results button). The pipe
	// emits tool_explorer live, mid-turn, so it interleaves with the real tool
	// calls — it must not break a run of groupable work the way a visible block
	// would. Treated as transparent: emitted, but never a group boundary.
	const TRANSPARENT_DETAIL_TYPES = new Set(['tool_explorer', 'search_results_button']);
	const isTransparentDetailToken = (token: any) => {
		return token?.type === 'details' && TRANSPARENT_DETAIL_TYPES.has(token?.attributes?.type ?? '');
	};

	// Blank filler between blocks — a marked ``space`` token (blank line) or a
	// whitespace-only text/paragraph/html token. These must not break a run of
	// groupable details: ``tool_A``, blank, ``tool_B`` should still group.
	const isBlankToken = (token: any) => {
		if (!token) return false;
		if (token.type === 'space') return true;
		if (token.type === 'text' || token.type === 'paragraph' || token.type === 'html') {
			const raw = token.raw ?? token.text ?? '';
			return typeof raw === 'string' && raw.trim() === '';
		}
		return false;
	};

	const getDisplayTokens = (tokenList: Token[] = []) => {
		// Bucket subagent tool calls by their parent (Task) id up front so a
		// subagent's steps group together even when several subagents run in
		// parallel and their tool calls interleave in the stream.
		const subagentItems = new Map<string, any[]>();
		for (const token of tokenList) {
			if (isSubagentToolToken(token)) {
				const pid = (token as any)?.attributes?.parent ?? '';
				const bucket = subagentItems.get(pid) ?? [];
				bucket.push(token);
				subagentItems.set(pid, bucket);
			}
		}

		// Loosely typed (matching the rest of this file): the array mixes marked
		// Token objects with the synthetic detail_group / subagent_group nodes.
		const displayTokens: any[] = [];
		let detailGroup: any[] = [];
		// Blank tokens seen since the last meaningful token, held until we know
		// whether they sit inside a group (drop) or end it (keep).
		let pendingBlanks: any[] = [];
		const emittedSubagents = new Set<string>();

		const flushDetailGroup = () => {
			if (detailGroup.length > 1) {
				displayTokens.push({
					type: 'detail_group',
					items: [...detailGroup]
				});
			} else if (detailGroup.length === 1) {
				displayTokens.push(detailGroup[0]);
			}

			detailGroup = [];
		};

		const flushPendingBlanks = () => {
			for (const blank of pendingBlanks) {
				displayTokens.push(blank);
			}
			pendingBlanks = [];
		};

		for (const token of tokenList) {
			if (isSubagentToolToken(token)) {
				// A subagent group breaks any run of generic groupable details.
				flushDetailGroup();
				flushPendingBlanks();
				const attrs = (token as any)?.attributes ?? {};
				const pid = attrs.parent ?? '';
				// Emit the group once, at the position of its first child, with
				// every child of that subagent (collected above).
				if (!emittedSubagents.has(pid)) {
					emittedSubagents.add(pid);
					displayTokens.push({
						type: 'subagent_group',
						parent: pid,
						label: attrs.subagent ?? '',
						items: subagentItems.get(pid) ?? [token]
					});
				}
			} else if (isGroupableDetailToken(token)) {
				// Blank tokens between grouped details are just inter-block
				// spacing — drop them so the run isn't split. Before a group
				// starts they are ordinary content, so emit them.
				if (detailGroup.length > 0) {
					pendingBlanks = [];
				} else {
					flushPendingBlanks();
				}
				detailGroup.push(token);
			} else if (isTransparentDetailToken(token)) {
				// Invisible / out-of-band UI detail — emit it but keep any open
				// run of grouped work intact (it renders nothing inline, so its
				// position relative to the group does not matter).
				displayTokens.push(token);
			} else if (isBlankToken(token)) {
				pendingBlanks.push(token);
			} else {
				flushDetailGroup();
				flushPendingBlanks();
				displayTokens.push(token);
			}
		}

		flushDetailGroup();
		flushPendingBlanks();

		return displayTokens;
	};

	const getDetailTextContent = (token) => {
		return decode(token?.text || '')
			.replace(/<summary>.*?<\/summary>/gi, '')
			.trim();
	};

	$: displayTokens = getDisplayTokens(tokens);

	const exportTableToCSVHandler = (token, tokenIdx = 0) => {
		console.log('Exporting table to CSV');

		// Extract header row text, decode HTML entities, and escape for CSV.
		const header = token.header.map(
			(headerCell) => `"${decode(headerCell.text).replace(/"/g, '""')}"`
		);

		// Create an array for rows that will hold the mapped cell text.
		const rows = token.rows.map((row) =>
			row.map((cell) => {
				// Map tokens into a single text
				const cellContent = cell.tokens.map((token) => token.text).join('');
				// Decode HTML entities and escape double quotes, wrap in double quotes
				return `"${decode(cellContent).replace(/"/g, '""')}"`;
			})
		);

		// Combine header and rows
		const csvData = [header, ...rows];

		// Join the rows using commas (,) as the separator and rows using newline (\n).
		const csvContent = csvData.map((row) => row.join(',')).join('\n');

		// Log rows and CSV content to ensure everything is correct.
		console.log(csvData);
		console.log(csvContent);

		// To handle Unicode characters, you need to prefix the data with a BOM:
		const bom = '\uFEFF'; // BOM for UTF-8

		// Create a new Blob prefixed with the BOM to ensure proper Unicode encoding.
		const blob = new Blob([bom + csvContent], { type: 'text/csv;charset=UTF-8' });

		// Use FileSaver.js's saveAs function to save the generated CSV file.
		saveAs(blob, `table-${id}-${tokenIdx}.csv`);
	};
</script>

<!-- {JSON.stringify(tokens)} -->
{#each displayTokens as token, tokenIdx (tokenIdx)}
	{#if token.type === 'hr'}
		<hr class=" border-gray-100/30 dark:border-gray-850/30" />
	{:else if token.type === 'heading'}
		<svelte:element this={headerComponent(token.depth)} dir="auto">
			<MarkdownInlineTokens
				id={`${id}-${tokenIdx}-h`}
				tokens={token.tokens}
				{done}
				{sourceIds}
				{onSourceClick}
			/>
		</svelte:element>
	{:else if token.type === 'code'}
		{#if token.raw.includes('```')}
			<CodeBlock
				id={`${id}-${tokenIdx}`}
				collapsed={$settings?.collapseCodeBlocks ?? false}
				{token}
				lang={token?.lang ?? ''}
				code={token?.text ?? ''}
				{attributes}
				{save}
				{preview}
				edit={editCodeBlock}
				stickyButtonsClassName={topPadding ? 'top-10' : 'top-0'}
				onSave={(value) => {
					onSave({
						raw: token.raw,
						oldContent: token.text,
						newContent: value
					});
				}}
				{onUpdate}
				{onPreview}
			/>
		{:else}
			{token.text}
		{/if}
	{:else if token.type === 'table'}
		<div class="relative w-full group mb-2">
			<div class="scrollbar-hidden relative overflow-x-auto max-w-full">
				<table
					class=" w-full text-sm text-start text-gray-500 dark:text-gray-400 max-w-full rounded-xl"
					dir="auto"
				>
					<thead
						class="text-xs text-gray-700 uppercase bg-white dark:bg-gray-900 dark:text-gray-400 border-none"
					>
						<tr class="">
							{#each token.header as header, headerIdx}
								<th
									scope="col"
									class="px-2.5! py-2! cursor-pointer border-b border-gray-100! dark:border-gray-800!"
									style={token.align[headerIdx] ? `text-align: ${token.align[headerIdx]}` : ''}
								>
									<div class="gap-1.5 text-start">
										<div class="shrink-0 break-normal">
											<MarkdownInlineTokens
												id={`${id}-${tokenIdx}-header-${headerIdx}`}
												tokens={header.tokens}
												{done}
												{sourceIds}
												{onSourceClick}
											/>
										</div>
									</div>
								</th>
							{/each}
						</tr>
					</thead>
					<tbody>
						{#each token.rows as row, rowIdx}
							<tr class="bg-white dark:bg-gray-900 text-xs">
								{#each row ?? [] as cell, cellIdx}
									<td
										class="px-3! py-2! text-gray-900 dark:text-white w-max {token.rows.length -
											1 ===
										rowIdx
											? ''
											: 'border-b border-gray-50! dark:border-gray-850!'}"
										style={token.align[cellIdx] ? `text-align: ${token.align[cellIdx]}` : ''}
									>
										<div class="break-normal">
											<MarkdownInlineTokens
												id={`${id}-${tokenIdx}-row-${rowIdx}-${cellIdx}`}
												tokens={cell.tokens}
												{done}
												{sourceIds}
												{onSourceClick}
											/>
										</div>
									</td>
								{/each}
							</tr>
						{/each}
					</tbody>
				</table>
			</div>

			<div class=" absolute top-1 right-1.5 z-20 invisible group-hover:visible flex gap-0.5">
				<Tooltip content={$i18n.t('Copy')}>
					<button
						class="p-1 rounded-lg bg-transparent transition"
						on:click={(e) => {
							e.stopPropagation();
							copyToClipboard(token.raw.trim(), null, $settings?.copyFormatted ?? false);
						}}
					>
						<Clipboard className=" size-3.5" strokeWidth="1.5" />
					</button>
				</Tooltip>

				<Tooltip content={$i18n.t('Export to CSV')}>
					<button
						class="p-1 rounded-lg bg-transparent transition"
						on:click={(e) => {
							e.stopPropagation();
							exportTableToCSVHandler(token, tokenIdx);
						}}
					>
						<Download className=" size-3.5" strokeWidth="1.5" />
					</button>
				</Tooltip>
			</div>
		</div>
	{:else if token.type === 'blockquote'}
		{@const alert = alertComponent(token)}
		{#if alert}
			<AlertRenderer {token} {alert} />
		{:else}
			<blockquote dir="auto">
				<svelte:self
					id={`${id}-${tokenIdx}`}
					{messageId}
					tokens={token.tokens}
					{done}
					{editCodeBlock}
					{onTaskClick}
					{sourceIds}
					{onSourceClick}
				/>
			</blockquote>
		{/if}
	{:else if token.type === 'list'}
		{#if token.ordered}
			<ol start={token.start || 1} dir="auto">
				{#each token.items as item, itemIdx}
					<li class="text-start">
						{#if item?.task}
							<input
								class=" translate-y-[1px] -translate-x-1 flex-shrink-0"
								type="checkbox"
								checked={item.checked}
								on:change={(e) => {
									onTaskClick({
										id: id,
										token: token,
										tokenIdx: tokenIdx,
										item: item,
										itemIdx: itemIdx,
										checked: e.target.checked
									});
								}}
							/>
						{/if}

						<svelte:self
							id={`${id}-${tokenIdx}-${itemIdx}`}
							{messageId}
							tokens={item.tokens}
							top={token.loose}
							{done}
							{editCodeBlock}
							{onTaskClick}
							{sourceIds}
							{onSourceClick}
						/>
					</li>
				{/each}
			</ol>
		{:else}
			<ul dir="auto" class="">
				{#each token.items as item, itemIdx}
					<li class="text-start {item?.task ? 'flex -translate-x-6.5 gap-3 ' : ''}">
						{#if item?.task}
							<input
								class="flex-shrink-0"
								type="checkbox"
								checked={item.checked}
								on:change={(e) => {
									onTaskClick({
										id: id,
										token: token,
										tokenIdx: tokenIdx,
										item: item,
										itemIdx: itemIdx,
										checked: e.target.checked
									});
								}}
							/>

							<div>
								<svelte:self
									id={`${id}-${tokenIdx}-${itemIdx}`}
									{messageId}
									tokens={item.tokens}
									top={token.loose}
									{done}
									{editCodeBlock}
									{onTaskClick}
									{sourceIds}
									{onSourceClick}
								/>
							</div>
						{:else}
							<svelte:self
								id={`${id}-${tokenIdx}-${itemIdx}`}
								{messageId}
								tokens={item.tokens}
								top={token.loose}
								{done}
								{editCodeBlock}
								{onTaskClick}
								{sourceIds}
								{onSourceClick}
							/>
						{/if}
					</li>
				{/each}
			</ul>
		{/if}
	{:else if token.type === 'subagent_group'}
		<SubagentGroup
			id={`${id}-${tokenIdx}-subagent-group`}
			label={token.label}
			tokens={token.items}
			messageDone={done}
		>
			<div slot="content" class="space-y-1">
				{#each token.items as detailToken, detailIdx}
					<ToolCallDisplay
						id={`${id}-${tokenIdx}-${detailIdx}-sa-tc`}
						attributes={detailToken.attributes}
						resultContent={getDetailTextContent(detailToken)}
						grouped={true}
						open={$settings?.expandDetails ?? false}
						className="w-full space-y-1"
					/>
				{/each}
			</div>
		</SubagentGroup>
	{:else if token.type === 'detail_group'}
		<ConsecutiveDetailsGroup
			id={`${id}-${tokenIdx}-detail-group`}
			tokens={token.items}
			messageDone={done}
		>
			<div slot="content" class="space-y-1">
				{#each token.items as detailToken, detailIdx}
					{@const textContent = getDetailTextContent(detailToken)}

					{#if detailToken?.attributes?.type === 'tool_calls'}
						<ToolCallDisplay
							id={`${id}-${tokenIdx}-${detailIdx}-tc`}
							attributes={detailToken.attributes}
							resultContent={getDetailTextContent(detailToken)}
							grouped={true}
							open={$settings?.expandDetails ?? false}
							className="w-full space-y-1"
						/>
					{:else if textContent.length > 0}
						<Collapsible
							title={detailToken.summary}
							open={$settings?.expandDetails ?? false}
							attributes={detailToken?.attributes}
							messageDone={done}
							className="w-full space-y-1"
							dir="auto"
						>
							<div class="mb-1.5" slot="content">
								<svelte:self
									id={`${id}-${tokenIdx}-${detailIdx}-d`}
									{messageId}
									tokens={marked.lexer(decode(detailToken.text))}
									attributes={detailToken?.attributes}
									{done}
									{editCodeBlock}
									{onTaskClick}
									{sourceIds}
									{onSourceClick}
								/>
							</div>
						</Collapsible>
					{:else}
						<Collapsible
							title={detailToken.summary}
							open={false}
							disabled={true}
							attributes={detailToken?.attributes}
							messageDone={done}
							className="w-full space-y-1"
							dir="auto"
						/>
					{/if}
				{/each}
			</div>
		</ConsecutiveDetailsGroup>
	{:else if token.type === 'details' && token?.attributes?.type === 'tool_explorer'}
		<!-- Tool Explorer: auto-open sidebar during streaming, no visible UI -->
		{@const explorerData = (() => {
			try {
				const text = decode(token?.text || '').replace(/<summary>.*?<\/summary>/gi, '').trim();
				return JSON.parse(text);
			} catch { return null; }
		})()}
		{#if explorerData}
			<span use:autoOpenToolExplorer={{ data: explorerData, messageDone: done, turnId: messageId }} class="hidden" />
		{/if}
	{:else if token.type === 'details' && token?.attributes?.type === 'search_results_button'}
		<!-- Final "검색된 문서 보기" button (only shown after done) -->
		{#if done}
			{@const explorerData = (() => {
				try {
					const text = decode(token?.text || '').replace(/<summary>.*?<\/summary>/gi, '').trim();
					return JSON.parse(text);
				} catch { return null; }
			})()}
			{#if explorerData}
				<button
					class="flex items-center gap-1.5 px-2.5 py-1 my-0.5 rounded border border-blue-500/70 dark:border-blue-400/70 hover:bg-blue-50 dark:hover:bg-blue-500/10 transition text-[11px] font-medium text-blue-600 dark:text-blue-400"
					on:click={() => {
						// Ensure this message's calls are in the store (some messages
						// only emit search_results_button without a streaming
						// tool_explorer block, so the auto-push path may not have
						// populated them). Merge with existing data so prior turns
						// are preserved; dedup means repeated clicks are no-ops.
						const existing = get(toolExplorerData) || {};
						const next: Record<string, any[]> = { ...existing };
						for (const [key, calls] of Object.entries(explorerData as Record<string, any[]>)) {
							if (!next[key]) next[key] = [];
							for (const raw of calls as any[]) {
								const call = messageId && !raw.turnId ? { ...raw, turnId: messageId } : raw;
								const isDup = next[key].some(
									(c: any) =>
										c.turnId === call.turnId &&
										c.query === call.query &&
										c.results?.length === call.results?.length
								);
								if (!isDup) next[key] = [...next[key], call];
							}
						}
						toolExplorerData.set(next);
						showToolExplorer.set(true);
					}}
				>
					<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" class="size-3">
						<path fill-rule="evenodd" d="M9 3.5a5.5 5.5 0 1 0 0 11 5.5 5.5 0 0 0 0-11ZM2 9a7 7 0 1 1 12.452 4.391l3.328 3.329a.75.75 0 1 1-1.06 1.06l-3.329-3.328A7 7 0 0 1 2 9Z" clip-rule="evenodd" />
					</svg>
					{$i18n.t('View searched documents')}
				</button>
			{/if}
		{/if}
	{:else if token.type === 'details' && token?.attributes?.type === 'image_gallery'}
		<!-- Image Gallery: no visible button, handled via ToolExplorer thumbnail click -->
	{:else if token.type === 'details' && token?.attributes?.type === 'ask_user_question'}
		<!-- AskUserQuestion: interactive card with clickable options -->
		{@const questionData = (() => {
			try {
				const text = decode(token?.text || '')
					.replace(/<summary>.*?<\/summary>/gi, '')
					.trim();
				return JSON.parse(text);
			} catch {
				return null;
			}
		})()}
		{#if questionData}
			<AskUserQuestionCard data={questionData} disabled={!done} />
		{/if}
	{:else if token.type === 'details'}
		{@const textContent = getDetailTextContent(token)}

		{#if token?.attributes?.type === 'tool_calls'}
			<!-- Tool calls have dedicated handling with ToolCallDisplay component -->
			<ToolCallDisplay
				id={`${id}-${tokenIdx}-tc`}
				attributes={token.attributes}
				resultContent={getDetailTextContent(token)}
				open={$settings?.expandDetails ?? false}
				className="w-full space-y-1"
			/>
		{:else if textContent.length > 0}
			<Collapsible
				title={token.summary}
				open={$settings?.expandDetails ?? false}
				attributes={token?.attributes}
				messageDone={done}
				className="w-full space-y-1"
				dir="auto"
			>
				<div class=" mb-1.5" slot="content">
					<svelte:self
						id={`${id}-${tokenIdx}-d`}
						{messageId}
						tokens={marked.lexer(decode(token.text))}
						attributes={token?.attributes}
						{done}
						{editCodeBlock}
						{onTaskClick}
						{sourceIds}
						{onSourceClick}
					/>
				</div>
			</Collapsible>
		{:else}
			<Collapsible
				title={token.summary}
				open={false}
				disabled={true}
				attributes={token?.attributes}
				messageDone={done}
				className="w-full space-y-1"
				dir="auto"
			/>
		{/if}
	{:else if token.type === 'html'}
		<HtmlToken {id} {token} {onSourceClick} />
	{:else if token.type === 'iframe'}
		<iframe
			src="{WEBUI_BASE_URL}/api/v1/files/{token.fileId}/content"
			title={token.fileId}
			width="100%"
			frameborder="0"
			on:load={(e) => {
				try {
					e.currentTarget.style.height =
						e.currentTarget.contentWindow.document.body.scrollHeight + 20 + 'px';
				} catch {}
			}}
		></iframe>
	{:else if token.type === 'paragraph'}
		{#if paragraphTag == 'span'}
			<span dir="auto">
				<MarkdownInlineTokens
					id={`${id}-${tokenIdx}-p`}
					tokens={token.tokens ?? []}
					{done}
					{sourceIds}
					{onSourceClick}
				/>
			</span>
		{:else}
			<p dir="auto">
				<MarkdownInlineTokens
					id={`${id}-${tokenIdx}-p`}
					tokens={token.tokens ?? []}
					{done}
					{sourceIds}
					{onSourceClick}
				/>
			</p>
		{/if}
	{:else if token.type === 'text'}
		{#if top}
			<p>
				{#if token.tokens}
					<MarkdownInlineTokens
						id={`${id}-${tokenIdx}-t`}
						tokens={token.tokens}
						{done}
						{sourceIds}
						{onSourceClick}
					/>
				{:else}
					{unescapeHtml(token.text)}
				{/if}
			</p>
		{:else if token.tokens}
			<MarkdownInlineTokens
				id={`${id}-${tokenIdx}-p`}
				tokens={token.tokens ?? []}
				{done}
				{sourceIds}
				{onSourceClick}
			/>
		{:else}
			{unescapeHtml(token.text)}
		{/if}
	{:else if token.type === 'inlineKatex'}
		{#if token.text}
			<KatexRenderer content={token.text} displayMode={token?.displayMode ?? false} />
		{/if}
	{:else if token.type === 'blockKatex'}
		{#if token.text}
			<KatexRenderer content={token.text} displayMode={token?.displayMode ?? false} />
		{/if}
	{:else if token.type === 'colonFence'}
		<ColonFenceBlock
			id={`${id}-${tokenIdx}`}
			{token}
			{tokenIdx}
			{done}
			{editCodeBlock}
			{sourceIds}
			{onTaskClick}
			{onSourceClick}
		/>
	{:else if token.type === 'space'}
		<div class="my-2" />
	{:else}
		{console.log('Unknown token', token)}
	{/if}
{/each}
