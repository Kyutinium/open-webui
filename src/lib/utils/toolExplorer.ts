// Tool Explorer ingestion that works on the RAW message content instead of
// DOM mounting. The <details type="tool_explorer|search_results_button">
// blocks are frequently nested inside collapsed groups (the "Explored"
// ConsecutiveDetailsGroup, subagent groups, thought-wrapped reasoning), where
// their tokens are only rendered — and any `use:` action only mounted — once
// the user expands the collapsible. Scanning the content string makes live
// population and the one-shot Tool Results focus independent of rendering.

import { get } from 'svelte/store';
import { chatId, toolExplorerData, showToolExplorer } from '$lib/stores';

// Same shape loadChat uses; matches CLOSED blocks only, so partially
// streamed blocks are picked up on a later scan once they close.
const EXPLORER_DETAILS_RE =
	/<details\s+type="(?:tool_explorer|search_results_button)"[^>]*>\s*<summary>[^<]*<\/summary>\s*([\s\S]*?)\s*<\/details>/g;

let _chatId = '';
// Assistant turns that already auto-focused the Tool Results tab — one yank
// per turn; later results in the same turn update the tab silently.
let _autoFocusedTurnIds = new Set<string>();

export const ingestToolExplorerBlocks = (
	content: string,
	{ turnId, done }: { turnId?: string; done?: boolean } = {}
) => {
	if (!content) return;

	const currentChatId = get(chatId) as string;
	if (_chatId && _chatId !== currentChatId) {
		_autoFocusedTurnIds = new Set();
	}
	_chatId = currentChatId;

	let text = content;
	if (text.includes('&lt;details')) {
		text = text
			.replaceAll('&lt;', '<')
			.replaceAll('&gt;', '>')
			.replaceAll('&quot;', '"')
			.replaceAll('&amp;', '&');
	}
	text = text.replace(/^> /gm, '');

	let addedNew = false;
	const existing = get(toolExplorerData) as Record<string, any[]> | null;
	const next: Record<string, any[]> = existing ? { ...existing } : {};
	for (const m of text.matchAll(EXPLORER_DETAILS_RE)) {
		let data: Record<string, any[]>;
		try {
			data = JSON.parse(m[1].replace(/^> /gm, '').trim());
		} catch {
			continue;
		}
		for (const [key, val] of Object.entries(data)) {
			if (!Array.isArray(val)) continue;
			if (!next[key]) next[key] = [];
			for (const raw of val as any[]) {
				const call = turnId && !raw.turnId ? { ...raw, turnId } : raw;
				const isDup = next[key].some(
					(c: any) =>
						c.turnId === call.turnId &&
						c.query === call.query &&
						c.results?.length === call.results?.length
				);
				if (!isDup) {
					next[key] = [...next[key], call];
					addedNew = true;
				}
			}
		}
	}
	if (!addedNew) return;
	toolExplorerData.set(next);

	// Auto-focus only for genuinely new results on a still-streaming message
	// (old chats mount with done=true and populate silently), at most once per
	// assistant turn so a manual switch away is respected.
	const willFocus = !done && !!turnId && !_autoFocusedTurnIds.has(turnId);
	console.warn(
		'[search-results] new calls ingested',
		{ turnId, done, willFocus, keys: Object.keys(next) }
	);
	if (willFocus) {
		_autoFocusedTurnIds.add(turnId as string);
		showToolExplorer.set(true);
	}
};
