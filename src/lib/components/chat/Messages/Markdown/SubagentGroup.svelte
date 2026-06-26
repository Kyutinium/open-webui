<script lang="ts">
	import { decode } from 'html-entities';
	import { getContext } from 'svelte';
	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';

	import ChevronUp from '$lib/components/icons/ChevronUp.svelte';
	import ChevronDown from '$lib/components/icons/ChevronDown.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import Sparkles from '$lib/components/icons/Sparkles.svelte';
	import CheckCircle from '$lib/components/icons/CheckCircle.svelte';

	import { settings } from '$lib/stores';

	const i18n = getContext('i18n');

	export let id = '';
	// The subagent label ("type: description") carried on the grouped tool
	// tokens by the gateway pipeline.
	export let label = '';
	export let tokens: Array<{
		summary?: string;
		attributes?: {
			type?: string;
			name?: string;
			done?: string;
			parent?: string;
			subagent?: string;
		};
	}> = [];

	export let messageDone = true;

	// Collapsed by default (respecting the global expandDetails preference) so a
	// subagent's steps stay tucked under the header until the user drills in.
	let open = $settings?.expandDetails ?? false;

	$: toolCallCount = tokens.filter((t) => t?.attributes?.type === 'tool_calls').length;
	// A subagent has returned its final answer once its own Task/Agent tool
	// result joins the group — the gateway pipe tags that block with
	// name="Task"/"Agent". Every emitted tool block already carries
	// done="true" (results stream in complete), so the done flag can't tell a
	// still-running subagent from a finished one; the Task result's arrival can.
	$: subagentReturned = tokens.some(
		(t) => t?.attributes?.name === 'Task' || t?.attributes?.name === 'Agent'
	);
	// Spin while the message is still streaming and the subagent hasn't
	// returned yet — a still-working subagent showing a done check is
	// confusing. Falls back to done once the whole message completes even if no
	// Task result was surfaced (e.g. MCP_TOOL_ONLY hides it).
	$: hasPending =
		!messageDone &&
		(!subagentReturned ||
			tokens.some((t) => t?.attributes?.done !== undefined && t?.attributes?.done !== 'true'));

	$: decodedLabel = decode(label ?? '').trim();
	$: countText =
		toolCallCount === 1
			? $i18n.t('{{COUNT}} tool', { COUNT: toolCallCount })
			: $i18n.t('{{COUNT}} tools', { COUNT: toolCallCount });
</script>

<div {id} class="w-full">
	<!-- svelte-ignore a11y-no-static-element-interactions -->
	<button
		class="w-fit text-left text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 transition cursor-pointer"
		aria-label={$i18n.t('Toggle subagent details')}
		aria-expanded={open}
		on:click={() => {
			open = !open;
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

			<!-- Subagent label + tool count -->
			<div class="flex-1 line-clamp-1">
				<span class="text-gray-600 dark:text-gray-300 {hasPending ? 'shimmer' : ''}"
					>{$i18n.t('Subagent')}</span
				>
				{#if decodedLabel}
					<span class="text-gray-500 dark:text-gray-400">{decodedLabel}</span>
				{/if}
				{#if toolCallCount > 0}
					<span class="text-gray-400 dark:text-gray-500">· {countText}</span>
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
				class="mt-1 mb-0.5 ml-2 pl-3 border-l border-gray-200 dark:border-gray-700 space-y-0.5"
			>
				<slot name="content" />
			</div>
		</div>
	{/if}
</div>
