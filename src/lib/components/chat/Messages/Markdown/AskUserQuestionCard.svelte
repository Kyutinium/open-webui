<script lang="ts">
	import { tick, getContext, onMount } from 'svelte';

	const i18n: any = getContext('i18n');

	export let data: {
		callId?: string;
		name?: string;
		previousResponseId?: string;
		questions?: Array<{
			question: string;
			options?: Array<{ label: string; description?: string }>;
			multiSelect?: boolean;
		}>;
		raw?: unknown;
	};
	export let disabled: boolean = false;

	// Meta options inserted by the model that look like "Other / 직접 입력"
	// shouldn't be sent as the answer — clicking them should focus the
	// custom-text input instead. claude-code's AskUserQuestion already adds
	// an Other choice automatically; the model occasionally invents one.
	const META_OPTION_PATTERN =
		/^\s*(other|custom(\s*answer)?|free.?form|none\s*of\s*the\s*above|direct\s*input|input\s*manually|기타|직접\s*입력(할게요|하기|함)?|아무것도\s*아님)\s*$/i;

	const isMetaOption = (label: string) => META_OPTION_PATTERN.test(label.trim());

	type AnswerMap = Map<number, string>;
	type SelectionMap = Map<number, Set<string>>;
	type TextMap = Map<number, string>;

	const questions = (data?.questions ?? []).filter(
		(q) => q && (q.question || (q.options && q.options.length))
	);
	const isMulti = questions.length > 1;
	const REVIEW_TAB = questions.length;

	let activeTab = 0;
	let answers: AnswerMap = new Map();
	let multiSelections: SelectionMap = new Map();
	let customTexts: TextMap = new Map();
	let submitted = false;
	let focusedOption = 0; // 0..optionCount-1 for options, optionCount for input
	let containerRef: HTMLDivElement | null = null;
	let inputRef: HTMLInputElement | null = null;
	const optionRefs: (HTMLButtonElement | null)[] = [];

	$: current = questions[activeTab];
	$: isReviewTab = isMulti && activeTab === REVIEW_TAB;
	$: isDisabled = disabled || submitted;
	$: allAnswered = questions.every((_, i) => answers.has(i));
	$: answeredCount = answers.size;
	$: optionCount = current?.options?.length ?? 0;
	$: INPUT_INDEX = optionCount;

	const isRecommended = (label: string): boolean =>
		/\(Recommended\)\s*$/i.test(label) || /\(권장\)\s*$/.test(label);

	const stripRecommended = (label: string): string =>
		label.replace(/\s*\((Recommended|권장)\)\s*$/i, '');

	const setAnswer = (idx: number, value: string) => {
		const next = new Map(answers);
		next.set(idx, value);
		answers = next;
	};

	const advanceToNext = (fromTab: number) => {
		if (!isMulti) return;
		const nextUnanswered = questions.findIndex((_, i) => i > fromTab && !answers.has(i));
		if (nextUnanswered !== -1) activeTab = nextUnanswered;
		else activeTab = REVIEW_TAB;
	};

	const focusInput = async () => {
		await tick();
		inputRef?.focus();
		focusedOption = INPUT_INDEX;
	};

	const handleSingleSelect = (label: string) => {
		if (isDisabled) return;
		// Meta options ("Other", "직접 입력") shouldn't be submitted as the
		// answer — they're a UX hint to type a custom value.
		if (isMetaOption(label)) {
			focusInput();
			return;
		}
		setAnswer(activeTab, label);
		advanceToNext(activeTab);
	};

	const handleMultiToggle = (label: string) => {
		if (isDisabled) return;
		if (isMetaOption(label)) {
			focusInput();
			return;
		}
		const next = new Map(multiSelections);
		const cur = new Set(next.get(activeTab) || []);
		if (cur.has(label)) cur.delete(label);
		else cur.add(label);
		next.set(activeTab, cur);
		multiSelections = next;
	};

	const handleMultiConfirm = () => {
		if (isDisabled) return;
		const sel = multiSelections.get(activeTab);
		if (!sel || sel.size === 0) return;
		setAnswer(activeTab, Array.from(sel).join(', '));
		advanceToNext(activeTab);
	};

	const handleCustomChange = (val: string) => {
		const next = new Map(customTexts);
		next.set(activeTab, val);
		customTexts = next;
	};

	const handleCustomSubmit = () => {
		const text = (customTexts.get(activeTab) || '').trim();
		if (isDisabled || !text) return;
		setAnswer(activeTab, text);
		advanceToNext(activeTab);
	};

	const handleEditQuestion = (idx: number) => {
		activeTab = idx;
	};

	const submitAll = async () => {
		if (!allAnswered || isDisabled) return;
		submitted = true;

		let answerText: string;
		if (questions.length === 1) {
			answerText = answers.get(0) || '';
		} else {
			const result = questions.map((q, i) => ({
				question: q.question,
				answer: answers.get(i) || ''
			}));
			answerText = JSON.stringify(result);
		}

		// Route the answer through the dedicated AskUserQuestion channel.
		// Chat.svelte listens for ``auq:answer:submit`` and POSTs to
		// ``/api/v1/auq/answer``, which relays straight to the gateway as
		// ``function_call_output``. This bypasses the chat-completion path
		// entirely so title-task races, context injection, and
		// per-process pending-state can't corrupt the reply.
		await tick();
		try {
			window.postMessage(
				{
					type: 'auq:answer:submit',
					callId: data?.callId ?? '',
					answer: answerText,
					previousResponseId: data?.previousResponseId ?? ''
				},
				window.origin
			);
		} catch (e) {
			console.error('[AskUserQuestionCard] failed to post answer:', e);
		}
	};

	// Auto-submit single-question once answered
	$: if (questions.length === 1 && answers.has(0) && !submitted && !disabled) {
		submitAll();
	}

	const handleEnterCustom = (event: KeyboardEvent) => {
		if (event.key === 'Enter' && !event.shiftKey) {
			event.preventDefault();
			handleCustomSubmit();
		}
	};

	// Card-scoped keyboard navigation: ArrowUp/Down to move between options,
	// Enter to select the focused option, ArrowLeft/Right for tab nav when
	// multi-question. Mirrors a2a-agent's AskUserQuestionCard.tsx.
	const handleCardKeydown = (event: KeyboardEvent) => {
		if (isDisabled) return;
		if (event.key === 'ArrowLeft') {
			event.preventDefault();
			if (isMulti) activeTab = Math.max(0, activeTab - 1);
			focusedOption = 0;
			return;
		}
		if (event.key === 'ArrowRight' && isMulti) {
			event.preventDefault();
			activeTab = Math.min(REVIEW_TAB, activeTab + 1);
			focusedOption = 0;
			return;
		}
		if (isReviewTab) return;
		const maxIdx = INPUT_INDEX;
		if (event.key === 'ArrowUp') {
			event.preventDefault();
			focusedOption = Math.max(0, focusedOption - 1);
		} else if (event.key === 'ArrowDown') {
			event.preventDefault();
			focusedOption = Math.min(maxIdx, focusedOption + 1);
		} else if (event.key === 'Enter' && focusedOption < optionCount) {
			event.preventDefault();
			const opt = current?.options?.[focusedOption];
			if (opt) {
				if (current?.multiSelect) handleMultiToggle(opt.label);
				else handleSingleSelect(opt.label);
			}
		}
	};

	// Move focus to the right element whenever focusedOption / tab change.
	$: if (!isDisabled && !isReviewTab && typeof window !== 'undefined') {
		tick().then(() => {
			if (focusedOption < optionCount) optionRefs[focusedOption]?.focus();
			else if (focusedOption === INPUT_INDEX) inputRef?.focus();
		});
	}

	onMount(() => {
		// Focus the first option on mount so keyboard nav is immediately usable.
		if (!isDisabled) {
			tick().then(() => {
				if (optionCount > 0) optionRefs[0]?.focus();
				else inputRef?.focus();
			});
		}
	});

	// Fallback rendering when there are no parsed questions (e.g. permission
	// prompts with unknown shape). Show the raw payload as JSON.
	const hasRaw = data?.raw !== undefined && data?.raw !== null;
</script>

{#if questions.length === 0}
	<!-- Fallback: no parsed questions -->
	<div
		class="my-2 rounded-xl border border-amber-300/40 bg-amber-50/40 dark:border-amber-700/40 dark:bg-amber-900/10 px-4 py-3 text-sm"
	>
		<div class="font-medium text-amber-700 dark:text-amber-300 mb-2">
			❓ {data?.name ? data.name : 'AskUserQuestion'}
		</div>
		{#if hasRaw}
			<pre class="text-xs whitespace-pre-wrap break-all">{JSON.stringify(data.raw, null, 2)}</pre>
		{/if}
		<p class="text-xs text-gray-500 dark:text-gray-400 mt-2">
			답변을 입력하면 자동으로 이어서 진행됩니다.
		</p>
	</div>
{:else}
	<div
		bind:this={containerRef}
		role="group"
		tabindex="0"
		on:keydown={handleCardKeydown}
		class="my-2 rounded-xl border transition-colors focus:outline-none focus:ring-1 focus:ring-blue-500/30 {isDisabled
			? 'border-gray-200/60 dark:border-gray-700/60 bg-gray-50/50 dark:bg-gray-800/30'
			: 'border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 shadow-sm'}"
		data-testid="ask-user-question-card"
	>
		<!-- Tab bar (multi-question only) -->
		{#if isMulti}
			<div
				class="flex gap-1 overflow-x-auto px-3 py-2 border-b border-gray-200/60 dark:border-gray-700/60"
			>
				{#each questions as q, i}
					{@const answered = answers.has(i)}
					{@const active = i === activeTab}
					<button
						type="button"
						on:click={() => (activeTab = i)}
						class="px-3 py-1.5 text-xs rounded-md whitespace-nowrap transition-all flex items-center gap-1.5 flex-shrink-0
							{active
							? 'bg-gray-100 dark:bg-gray-800 text-gray-900 dark:text-gray-100 font-medium'
							: 'text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-100 hover:bg-gray-50 dark:hover:bg-gray-800/60'}"
					>
						{#if answered}
							<svg class="size-3 text-green-500 flex-shrink-0" viewBox="0 0 20 20" fill="currentColor">
								<path
									fill-rule="evenodd"
									d="M16.704 4.153a.75.75 0 0 1 .143 1.052l-8 10.5a.75.75 0 0 1-1.127.075l-4.5-4.5a.75.75 0 0 1 1.06-1.06l3.894 3.893 7.48-9.817a.75.75 0 0 1 1.05-.143Z"
									clip-rule="evenodd"
								/>
							</svg>
						{/if}
						<span class={active ? '' : 'truncate max-w-[100px]'}>
							{active
								? q.question
								: q.question.length > 16
									? q.question.slice(0, 16) + '…'
									: q.question}
						</span>
					</button>
				{/each}
				<button
					type="button"
					on:click={() => (activeTab = REVIEW_TAB)}
					class="px-3 py-1.5 text-xs rounded-md whitespace-nowrap transition-all flex items-center gap-1.5 flex-shrink-0
						{isReviewTab
						? 'bg-gray-100 dark:bg-gray-800 text-gray-900 dark:text-gray-100 font-medium'
						: 'text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-100 hover:bg-gray-50 dark:hover:bg-gray-800/60'}"
				>
					<svg class="size-3" viewBox="0 0 20 20" fill="currentColor">
						<path
							fill-rule="evenodd"
							d="M2 4.75A.75.75 0 0 1 2.75 4h14.5a.75.75 0 0 1 0 1.5H2.75A.75.75 0 0 1 2 4.75ZM2 10a.75.75 0 0 1 .75-.75h14.5a.75.75 0 0 1 0 1.5H2.75A.75.75 0 0 1 2 10Zm.75 4.5a.75.75 0 0 0 0 1.5h14.5a.75.75 0 0 0 0-1.5H2.75Z"
							clip-rule="evenodd"
						/>
					</svg>
					<span>{$i18n?.t?.('Review') ?? 'Review'}</span>
				</button>
			</div>
		{/if}

		<div class="px-4 py-3">
			{#if isMulti && !isReviewTab}
				<div class="flex items-center justify-between mb-2">
					<span class="text-xs text-gray-500 dark:text-gray-400">
						{activeTab + 1} / {questions.length}
					</span>
					<span class="text-xs text-gray-500 dark:text-gray-400">
						{answeredCount}/{questions.length}
					</span>
				</div>
			{/if}

			{#if isReviewTab}
				<!-- Review panel -->
				<div class="space-y-2">
					<div class="flex items-center gap-2 mb-3 text-sm font-medium">
						<svg class="size-4 text-gray-500" viewBox="0 0 20 20" fill="currentColor">
							<path
								fill-rule="evenodd"
								d="M3 4.25A2.25 2.25 0 0 1 5.25 2h5.5A2.25 2.25 0 0 1 13 4.25v2h-1.5v-2a.75.75 0 0 0-.75-.75h-5.5a.75.75 0 0 0-.75.75v11.5c0 .414.336.75.75.75h5.5a.75.75 0 0 0 .75-.75v-2H13v2A2.25 2.25 0 0 1 10.75 18h-5.5A2.25 2.25 0 0 1 3 15.75V4.25Z"
								clip-rule="evenodd"
							/>
							<path
								fill-rule="evenodd"
								d="M16.78 7.97a.75.75 0 0 1 0 1.06l-2.47 2.47h6.44a.75.75 0 0 1 0 1.5h-6.44l2.47 2.47a.75.75 0 1 1-1.06 1.06l-3.75-3.75a.75.75 0 0 1 0-1.06l3.75-3.75a.75.75 0 0 1 1.06 0Z"
								clip-rule="evenodd"
							/>
						</svg>
						<span>Review</span>
					</div>
					{#each questions as q, i}
						{@const answer = answers.get(i)}
						<div
							class="flex items-start justify-between gap-3 rounded-lg border px-3 py-2
								{answer
								? 'border-gray-200 dark:border-gray-700 bg-gray-50/50 dark:bg-gray-800/30'
								: 'border-amber-300/50 dark:border-amber-700/50 bg-amber-50/40 dark:bg-amber-900/10'}"
						>
							<div class="flex-1 min-w-0">
								<p class="text-xs text-gray-500 dark:text-gray-400 truncate">{q.question}</p>
								{#if answer}
									<p class="text-sm text-gray-900 dark:text-gray-100 mt-0.5">{answer}</p>
								{:else}
									<p class="text-sm text-amber-600 dark:text-amber-400 mt-0.5 italic">—</p>
								{/if}
							</div>
							{#if !isDisabled}
								<button
									type="button"
									on:click={() => handleEditQuestion(i)}
									class="flex-shrink-0 p-1 rounded text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800"
									aria-label={`Edit question ${i + 1}`}
								>
									<svg class="size-3.5" viewBox="0 0 20 20" fill="currentColor">
										<path
											d="M2.695 14.763l-1.262 3.154a.5.5 0 0 0 .65.65l3.155-1.262a4 4 0 0 0 1.343-.886L17.5 5.5a2.121 2.121 0 0 0-3-3L3.58 13.42a4 4 0 0 0-.885 1.343Z"
										/>
									</svg>
								</button>
							{/if}
						</div>
					{/each}
					{#if !isDisabled}
						<div class="pt-2">
							<button
								type="button"
								disabled={!allAnswered}
								on:click={submitAll}
								class="w-full inline-flex items-center justify-center gap-1.5 px-4 py-2 text-sm rounded-lg transition-colors
									{allAnswered
									? 'bg-blue-600 text-white hover:bg-blue-700 dark:bg-blue-500 dark:hover:bg-blue-600'
									: 'bg-gray-200 dark:bg-gray-700 text-gray-500 dark:text-gray-400 cursor-not-allowed'}"
							>
								<svg class="size-3.5" viewBox="0 0 20 20" fill="currentColor">
									<path
										d="M3.105 2.288a.75.75 0 0 0-.826.95l1.414 4.926A.75.75 0 0 0 4.42 8.75H10a.75.75 0 0 1 0 1.5H4.42a.75.75 0 0 0-.727.585l-1.414 4.926a.75.75 0 0 0 .826.95 28.897 28.897 0 0 0 15.293-7.155.75.75 0 0 0 0-1.115A28.897 28.897 0 0 0 3.105 2.288Z"
									/>
								</svg>
								Submit
							</button>
						</div>
					{/if}
				</div>
			{:else if current}
				<!-- Single question view -->
				<p
					class="text-sm mb-3 {isDisabled
						? 'text-gray-500 dark:text-gray-400'
						: 'text-gray-900 dark:text-gray-100 font-medium'}"
				>
					{current.question}
				</p>

				{#if current.options && current.options.length}
					<div class="flex flex-col gap-1.5 mb-3">
						{#each current.options as opt, optIdx}
							{@const recommended = isRecommended(opt.label)}
							{@const displayLabel = stripRecommended(opt.label)}
							{@const isSelected = current.multiSelect
								? (multiSelections.get(activeTab)?.has(opt.label) ?? false)
								: answers.get(activeTab) === opt.label}
							{@const isFocused = focusedOption === optIdx}
							<button
								type="button"
								bind:this={optionRefs[optIdx]}
								disabled={isDisabled}
								on:click={() =>
									current.multiSelect
										? handleMultiToggle(opt.label)
										: handleSingleSelect(opt.label)}
								title={opt.description ?? ''}
								class="inline-flex items-center gap-2 px-3 py-2 text-sm rounded-lg border transition-colors w-full text-left focus:outline-none
									{isDisabled
									? isSelected
										? 'border-blue-500/40 bg-blue-50 dark:bg-blue-900/20 text-blue-700 dark:text-blue-300'
										: 'border-gray-200/40 dark:border-gray-700/40 text-gray-400 dark:text-gray-500 cursor-not-allowed'
									: isFocused
										? 'border-blue-500/60 bg-blue-50/50 dark:bg-blue-900/15 ring-1 ring-blue-500/30'
										: recommended
											? 'border-blue-500/50 bg-blue-50 dark:bg-blue-900/20 text-blue-700 dark:text-blue-300 hover:bg-blue-100 dark:hover:bg-blue-900/30'
											: isSelected
												? 'border-blue-500/50 bg-blue-50 dark:bg-blue-900/20 text-blue-700 dark:text-blue-300'
												: 'border-gray-200 dark:border-gray-700 text-gray-900 dark:text-gray-100 hover:border-gray-400 dark:hover:border-gray-500 hover:bg-gray-50 dark:hover:bg-gray-800/40'}"
							>
								{#if current.multiSelect}
									<span
										class="flex items-center justify-center w-4 h-4 rounded border transition-colors flex-shrink-0
											{isSelected
											? 'border-blue-500 bg-blue-500 text-white'
											: 'border-gray-300 dark:border-gray-600'}"
									>
										{#if isSelected}
											<svg class="size-3" viewBox="0 0 20 20" fill="currentColor">
												<path
													fill-rule="evenodd"
													d="M16.704 4.153a.75.75 0 0 1 .143 1.052l-8 10.5a.75.75 0 0 1-1.127.075l-4.5-4.5a.75.75 0 0 1 1.06-1.06l3.894 3.893 7.48-9.817a.75.75 0 0 1 1.05-.143Z"
													clip-rule="evenodd"
												/>
											</svg>
										{/if}
									</span>
								{/if}
								{#if recommended}
									<svg class="size-3.5 text-blue-500 flex-shrink-0" viewBox="0 0 20 20" fill="currentColor">
										<path
											fill-rule="evenodd"
											d="M10.868 2.884c-.321-.772-1.415-.772-1.736 0l-1.83 4.401-4.753.381c-.833.067-1.171 1.107-.536 1.651l3.62 3.102-1.106 4.637c-.194.813.691 1.456 1.405 1.02L10 15.591l4.069 2.485c.713.436 1.598-.207 1.404-1.02l-1.106-4.637 3.62-3.102c.635-.544.297-1.584-.536-1.65l-4.752-.382-1.831-4.401Z"
											clip-rule="evenodd"
										/>
									</svg>
								{/if}
								<span class="flex-1">{displayLabel}</span>
								{#if !current.multiSelect && isSelected}
									<svg class="size-3.5 text-blue-500 flex-shrink-0" viewBox="0 0 20 20" fill="currentColor">
										<path
											fill-rule="evenodd"
											d="M16.704 4.153a.75.75 0 0 1 .143 1.052l-8 10.5a.75.75 0 0 1-1.127.075l-4.5-4.5a.75.75 0 0 1 1.06-1.06l3.894 3.893 7.48-9.817a.75.75 0 0 1 1.05-.143Z"
											clip-rule="evenodd"
										/>
									</svg>
								{/if}
							</button>
						{/each}
					</div>
				{/if}

				{#if !isDisabled}
					<div class="flex items-center gap-2">
						<input
							bind:this={inputRef}
							type="text"
							value={customTexts.get(activeTab) || ''}
							on:input={(e) => handleCustomChange(e.currentTarget.value)}
							on:keydown={handleEnterCustom}
							on:focus={() => (focusedOption = INPUT_INDEX)}
							placeholder="선택지에 없으면 여기 직접 입력 후 Enter"
							class="flex-1 px-3 py-1.5 text-sm rounded-lg border border-gray-200 dark:border-gray-700 bg-transparent text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none focus:border-gray-400 dark:focus:border-gray-500"
							data-testid="ask-user-question-custom-input"
						/>
						<button
							type="button"
							disabled={!(customTexts.get(activeTab) || '').trim()}
							on:click={handleCustomSubmit}
							class="p-1.5 rounded-lg transition-colors
								{(customTexts.get(activeTab) || '').trim()
								? 'text-blue-600 dark:text-blue-400 hover:bg-blue-50 dark:hover:bg-blue-900/20'
								: 'text-gray-300 dark:text-gray-600 cursor-not-allowed'}"
							data-testid="ask-user-question-custom-send"
							aria-label="Send custom answer"
						>
							<svg class="size-4" viewBox="0 0 20 20" fill="currentColor">
								<path
									d="M3.105 2.288a.75.75 0 0 0-.826.95l1.414 4.926A.75.75 0 0 0 4.42 8.75H10a.75.75 0 0 1 0 1.5H4.42a.75.75 0 0 0-.727.585l-1.414 4.926a.75.75 0 0 0 .826.95 28.897 28.897 0 0 0 15.293-7.155.75.75 0 0 0 0-1.115A28.897 28.897 0 0 0 3.105 2.288Z"
								/>
							</svg>
						</button>
					</div>
				{/if}

				{#if current.multiSelect && current.options?.length && !isDisabled}
					<div class="mt-3">
						<button
							type="button"
							disabled={(multiSelections.get(activeTab)?.size ?? 0) === 0}
							on:click={handleMultiConfirm}
							class="inline-flex items-center justify-center gap-1.5 w-full px-3 py-2 text-sm rounded-lg transition-colors
								{(multiSelections.get(activeTab)?.size ?? 0) > 0
								? 'bg-blue-600 text-white hover:bg-blue-700 dark:bg-blue-500 dark:hover:bg-blue-600'
								: 'bg-gray-200 dark:bg-gray-700 text-gray-500 dark:text-gray-400 cursor-not-allowed'}"
							data-testid="ask-user-question-multi-confirm"
						>
							<svg class="size-3.5" viewBox="0 0 20 20" fill="currentColor">
								<path
									fill-rule="evenodd"
									d="M16.704 4.153a.75.75 0 0 1 .143 1.052l-8 10.5a.75.75 0 0 1-1.127.075l-4.5-4.5a.75.75 0 0 1 1.06-1.06l3.894 3.893 7.48-9.817a.75.75 0 0 1 1.05-.143Z"
									clip-rule="evenodd"
								/>
							</svg>
							{(multiSelections.get(activeTab)?.size ?? 0) > 0
								? `Submit (${multiSelections.get(activeTab)?.size ?? 0})`
								: 'Submit'}
						</button>
					</div>
				{/if}
			{/if}
		</div>

		{#if submitted}
			<div
				class="border-t border-gray-200/60 dark:border-gray-700/60 px-4 py-2 flex items-center gap-1.5 text-xs text-gray-400 dark:text-gray-500"
			>
				<svg class="size-3" viewBox="0 0 20 20" fill="currentColor">
					<path
						fill-rule="evenodd"
						d="M16.704 4.153a.75.75 0 0 1 .143 1.052l-8 10.5a.75.75 0 0 1-1.127.075l-4.5-4.5a.75.75 0 0 1 1.06-1.06l3.894 3.893 7.48-9.817a.75.75 0 0 1 1.05-.143Z"
						clip-rule="evenodd"
					/>
				</svg>
				<span>Answered</span>
			</div>
		{/if}
	</div>
{/if}
