<script lang="ts">
	import { getContext } from 'svelte';

	const i18n = getContext('i18n');

	export let loginUrl: string = '';
	export let onLogin: () => void = () => {};
	export let onCancel: () => void = () => {};

	function handleLogin() {
		if (loginUrl) onLogin();
	}
</script>

<div class="fixed inset-0 z-[9999] flex items-center justify-center bg-black/50">
	<div class="w-[520px] max-w-[92vw] rounded-2xl bg-white dark:bg-gray-900 shadow-xl p-6">
		<h2 class="text-xl font-semibold text-gray-900 dark:text-gray-100">
			{$i18n.t('Confluence 로그인이 필요합니다')}
		</h2>

		<p class="mt-3 text-sm text-gray-600 dark:text-gray-300 leading-relaxed">
			{$i18n.t('유관 문서를 조회하려면 Confluence 로그인이 필요합니다.')}<br />
			{$i18n.t('아래 "Confluence 로그인" 버튼을 눌러 로그인을 진행해주세요.')}
		</p>

		{#if !loginUrl}
			<p class="mt-3 text-sm text-red-600">
				{$i18n.t('로그인 URL을 가져오지 못했습니다. 새로고침 후 다시 시도해 주세요.')}
			</p>
		{/if}

		<div class="mt-6 flex items-center justify-end gap-2">
			<button
				type="button"
				class="px-4 py-2 rounded-xl border border-gray-300 dark:border-gray-700 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-800 transition"
				on:click={onCancel}
			>
				{$i18n.t('취소')}
			</button>
			<button
				type="button"
				class="px-4 py-2 rounded-xl bg-blue-600 hover:bg-blue-700 text-white transition disabled:opacity-50 disabled:cursor-not-allowed"
				disabled={!loginUrl}
				on:click={handleLogin}
			>
				{$i18n.t('Confluence 로그인')}
			</button>
		</div>
	</div>
</div>
