<script lang="ts">
	import { getContext } from 'svelte';
	import { marked } from 'marked';
	import DOMPurify from 'dompurify';
	import Modal from './common/Modal.svelte';

	const i18n: any = getContext('i18n');

	// A banner with type === 'popup' (admin Settings > General > Banners).
	// The parent owns the queue/dismissal logic; this component only renders
	// the current announcement and closes via bind:show (button, backdrop, Esc).
	export let show = false;
	export let banner: { id: string; title?: string | null; content: string } | null = null;
</script>

{#if banner}
	<Modal bind:show size="md">
		<div class="px-6 pt-5 pb-6">
			<div class="flex items-start justify-between gap-3 mb-3">
				<div class="text-lg font-semibold text-gray-900 dark:text-gray-100">
					📢 {banner.title || $i18n.t('Announcement')}
				</div>
				<button
					class="p-1 rounded-lg text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800 transition"
					on:click={() => (show = false)}
					aria-label={$i18n.t('Close')}
				>
					<svg
						xmlns="http://www.w3.org/2000/svg"
						viewBox="0 0 20 20"
						fill="currentColor"
						class="size-4"
					>
						<path
							d="M6.28 5.22a.75.75 0 0 0-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 1 0 1.06 1.06L10 11.06l3.72 3.72a.75.75 0 1 0 1.06-1.06L11.06 10l3.72-3.72a.75.75 0 0 0-1.06-1.06L10 8.94 6.28 5.22Z"
						/>
					</svg>
				</button>
			</div>
			<div class="text-gray-700 dark:text-gray-200 markdown-prose-sm max-h-[60vh] overflow-y-auto">
				<!-- No newline->br preprocessing: it would collapse block markdown
				     (headings, lists, code fences) into one paragraph. breaks:true
				     handles single-newline line breaks the GFM way instead. -->
				{@html DOMPurify.sanitize(marked.parse(banner.content ?? '', { breaks: true }))}
			</div>
			<div class="mt-5 flex justify-end">
				<button
					class="px-4 py-2 text-sm font-medium rounded-lg bg-black text-white hover:bg-gray-800 dark:bg-white dark:text-black dark:hover:bg-gray-100 transition"
					on:click={() => (show = false)}
				>
					{$i18n.t('Confirm')}
				</button>
			</div>
		</div>
	</Modal>
{/if}
