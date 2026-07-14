<script>
	import { getContext } from 'svelte';

	export let title = '';
	export let content = '';
	const i18n = getContext('i18n');
</script>

<div class="px-3">
	<div class="text-center dark:text-white text-2xl font-medium z-50" role="heading" aria-level="2">
		{#if title}
			{title}
		{:else}
			{$i18n.t('Add Files')}
		{/if}
	</div>

	<slot
		><div class="px-2 mt-2 text-center text-gray-700 dark:text-gray-200 w-full">
			{#if content}
				{content}
			{:else}
				{$i18n.t('Drop any files here to upload')}
			{/if}
		</div>
	</slot>

	<div class="px-2 mt-1.5 text-center text-sm text-amber-600 dark:text-amber-400 w-full">
		{$i18n.t('NASCA-encrypted files cannot be read.')}
	</div>
</div>
