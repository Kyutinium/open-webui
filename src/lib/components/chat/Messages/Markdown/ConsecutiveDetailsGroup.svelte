<script lang="ts">
	import { decode } from 'html-entities';
	import { getContext } from 'svelte';
	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';

	import ChevronUp from '$lib/components/icons/ChevronUp.svelte';
	import ChevronDown from '$lib/components/icons/ChevronDown.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import WrenchSolid from '$lib/components/icons/WrenchSolid.svelte';
	import Sparkles from '$lib/components/icons/Sparkles.svelte';
	import CheckCircle from '$lib/components/icons/CheckCircle.svelte';
	import FullHeightIframe from '$lib/components/common/FullHeightIframe.svelte';

	import { settings } from '$lib/stores';

	const i18n = getContext('i18n');

	export let id = '';
	export let tokens: Array<{
		summary?: string;
		attributes?: {
			type?: string;
			name?: string;
			done?: string;
			duration?: string;
			embeds?: string;
			arguments?: string;
		};
	}> = [];

	export let messageDone = true;

	let open = $settings?.expandDetails ?? false;

	// Claude Code style: a group that contains reasoning auto-expands while it's
	// still working so the user can watch the thinking + tool calls live, then
	// collapses back to its resting state (expandDetails) once the work is done.
	// Tool-only groups keep their compact default. A manual toggle wins for good.
	let userToggled = false;
	let restingOpen = open;

	function parseJSONString(str: string) {
		try {
			return parseJSONString(JSON.parse(str));
		} catch (e) {
			return str;
		}
	}

	$: toolCallCount = tokens.filter((t) => t?.attributes?.type === 'tool_calls').length;
	$: reasoningCount = tokens.filter((t) => t?.attributes?.type === 'reasoning').length;
	$: hasPending =
		!messageDone &&
		tokens.some((t) => t?.attributes?.done !== undefined && t?.attributes?.done !== 'true');

	$: codeInterpreterCount = tokens.filter((t) => t?.attributes?.type === 'code_interpreter').length;

	// Auto-expand a reasoning-bearing group while it is working; collapse to the
	// resting state once done. Gated to groups with reasoning so tool-only
	// groups keep their compact streaming view. Manual toggle disables this.
	$: if (reasoningCount > 0 && !userToggled) {
		open = hasPending ? true : restingOpen;
	}

	// Collect all embeds from tool_calls tokens
	$: allEmbeds = (() => {
		const result: Array<{ name: string; embed: string; args: string }> = [];
		for (const t of tokens) {
			if (t?.attributes?.type !== 'tool_calls') continue;
			const raw = decode(t.attributes?.embeds ?? '');
			try {
				const parsed = parseJSONString(raw);
				if (Array.isArray(parsed) && parsed.length > 0) {
					for (const embed of parsed) {
						result.push({
							name: t.attributes?.name ?? '',
							embed,
							args: decode(t.attributes?.arguments ?? '')
						});
					}
				}
			} catch {}
		}
		return result;
	})();

	$: summaryText = (() => {
		const parts = [];

		// Thoughts first, Claude Code style ("thought 2 times, 3 Grep").
		if (reasoningCount > 0) {
			parts.push(
				reasoningCount === 1
					? $i18n.t('thought once')
					: $i18n.t('thought {{COUNT}} times', { COUNT: reasoningCount })
			);
		}

		if (toolCallCount > 0) {
			// Aggregate tool count, matching SubagentGroup phrasing ("5 tools").
			parts.push(
				toolCallCount === 1
					? $i18n.t('{{COUNT}} tool', { COUNT: toolCallCount })
					: $i18n.t('{{COUNT}} tools', { COUNT: toolCallCount })
			);
		}

		if (codeInterpreterCount > 0) {
			if (codeInterpreterCount === 1) {
				parts.push($i18n.t('Ran {{COUNT}} analysis', { COUNT: codeInterpreterCount }));
			} else {
				parts.push($i18n.t('Ran {{COUNT}} analyses', { COUNT: codeInterpreterCount }));
			}
		}

		const prefix = hasPending ? $i18n.t('Exploring') : $i18n.t('Explored');
		const detail = parts.join(', ');
		return detail;
	})();

	$: prefixText = hasPending ? $i18n.t('Exploring') : $i18n.t('Explored');
</script>

<div {id} class="w-full">
	<!-- svelte-ignore a11y-no-static-element-interactions -->
	<button
		class="w-fit text-left text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 transition cursor-pointer"
		aria-label={$i18n.t('Toggle details')}
		aria-expanded={open}
		on:click={() => {
			open = !open;
			userToggled = true;
		}}
	>
		<div class="flex items-center gap-1.5">
			<!-- Status icon -->
			{#if hasPending}
				<div>
					<Spinner className="size-4" />
				</div>
			{:else if toolCallCount > 0}
				<div class="text-emerald-500 dark:text-emerald-400">
					<CheckCircle className="size-4" strokeWidth="2" />
				</div>
			{:else}
				<div class="text-gray-400 dark:text-gray-500">
					<Sparkles className="size-3.5" />
				</div>
			{/if}

			<!-- Summary text -->
			<div class="flex-1 line-clamp-1">
				<span class="text-gray-600 dark:text-gray-300 {hasPending ? 'shimmer' : ''}"
					>{prefixText}</span
				>
				{#if summaryText}
					<span class="text-gray-400 dark:text-gray-500">{summaryText}</span>
				{/if}
			</div>

			<!-- Chevron -->
			<div class="flex shrink-0 self-center text-gray-400 dark:text-gray-500">
				{#if open}
					<ChevronUp strokeWidth="3.5" className="size-3" />
				{:else}
					<ChevronDown strokeWidth="3.5" className="size-3" />
				{/if}
			</div>
		</div>
	</button>

	{#if open}
		<div transition:slide={{ duration: 300, easing: quintOut, axis: 'y' }}>
			<div
				class="mt-1 mb-0.5 ml-1.5 pl-2 border-l border-gray-100 dark:border-gray-850 space-y-0.5"
			>
				<slot name="content" />
			</div>
		</div>
	{/if}

	{#if allEmbeds.length > 0}
		{#each allEmbeds as embedItem, idx}
			<div id={`${id}-embed-${idx}`}>
				<FullHeightIframe
					src={embedItem.embed}
					args={embedItem.args}
					allowScripts={true}
					allowForms={$settings?.iframeSandboxAllowForms ?? false}
					allowSameOrigin={$settings?.iframeSandboxAllowSameOrigin ?? false}
					allowPopups={true}
				/>
			</div>
		{/each}
	{/if}
</div>
