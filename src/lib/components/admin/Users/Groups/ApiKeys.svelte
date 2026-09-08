<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';

	import dayjs from 'dayjs';
	import relativeTime from 'dayjs/plugin/relativeTime';
	import localizedFormat from 'dayjs/plugin/localizedFormat';
	dayjs.extend(relativeTime);
	dayjs.extend(localizedFormat);

	import { createGroupApiKey, deleteGroupApiKey, getGroupApiKeys } from '$lib/apis/groups';
	import { copyToClipboard } from '$lib/utils';

	import ConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';
	import Plus from '$lib/components/icons/Plus.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';

	const i18n = getContext('i18n');

	export let groupId: string;

	let keys: any[] | null = null;
	let loading = false;

	// Shown once, right after creation — the server never returns it again.
	let createdKey: any = null;
	let createdKeyCopied = false;

	let showCreateForm = false;
	let name = '';
	let expiresInDays = '0'; // '0' = never expires

	let showRevokeConfirmDialog = false;
	let keyToRevoke: any = null;

	const EXPIRY_OPTIONS = ['0', '30', '90', '365'];

	const init = async () => {
		if (!groupId) {
			return;
		}

		// On failure fall back to an empty list rather than leaving the spinner up.
		keys = await getGroupApiKeys(localStorage.token, groupId).catch((error) => {
			toast.error(`${error}`);
			return [];
		});
	};

	const createHandler = async () => {
		loading = true;

		const days = parseInt(expiresInDays, 10);
		const res = await createGroupApiKey(localStorage.token, groupId, {
			name: name.trim(),
			expiresAt: days > 0 ? dayjs().add(days, 'day').unix() : null
		}).catch((error) => {
			toast.error(`${error}`);
			return null;
		});

		loading = false;

		if (res) {
			createdKey = res;
			createdKeyCopied = false;
			name = '';
			expiresInDays = '0';
			showCreateForm = false;

			toast.success($i18n.t('API Key created.'));
			await init();
		}
	};

	const revokeHandler = async () => {
		if (!keyToRevoke) {
			return;
		}

		const res = await deleteGroupApiKey(localStorage.token, groupId, keyToRevoke.id).catch(
			(error) => {
				toast.error(`${error}`);
				return null;
			}
		);

		if (res) {
			if (createdKey?.id === keyToRevoke.id) {
				createdKey = null;
			}

			toast.success($i18n.t('API Key revoked.'));
			await init();
		}

		keyToRevoke = null;
	};

	const isExpired = (key: any) => key?.expires_at && key.expires_at * 1000 <= Date.now();

	onMount(() => {
		init();
	});
</script>

<ConfirmDialog
	bind:show={showRevokeConfirmDialog}
	title={$i18n.t('Revoke API Key')}
	message={$i18n.t('Any client still using this key will stop working immediately.')}
	on:confirm={() => {
		revokeHandler();
	}}
/>

<div class="flex flex-col h-full text-sm">
	<div class="mb-2.5">
		<div class="text-xs text-gray-500 dark:text-gray-400">
			{$i18n.t(
				'Shared API keys for this group. They authenticate as the group itself, not as any member, so they keep working when members change.'
			)}
		</div>
	</div>

	{#if createdKey}
		<div
			class="mb-2.5 px-3 py-2.5 rounded-lg bg-gray-50 dark:bg-gray-850 border border-gray-100 dark:border-gray-800"
		>
			<div class="flex justify-between items-center mb-1">
				<div class="text-xs font-medium">
					{$i18n.t('Copy this key now — it will not be shown again.')}
				</div>
				<button
					class="p-0.5"
					aria-label={$i18n.t('Close')}
					on:click={() => {
						createdKey = null;
					}}
					type="button"
				>
					<XMark className="size-4" />
				</button>
			</div>

			<div class="flex gap-1.5 items-center">
				<input
					class="w-full font-mono text-xs bg-transparent outline-hidden"
					value={createdKey.key}
					readonly
				/>
				<button
					class="shrink-0 text-xs font-medium px-2 py-1 rounded-lg bg-gray-100 hover:bg-gray-200 dark:bg-gray-800 dark:hover:bg-gray-700 transition"
					aria-label={$i18n.t('Copy API Key')}
					on:click={async () => {
						await copyToClipboard(createdKey.key);
						createdKeyCopied = true;
						setTimeout(() => {
							createdKeyCopied = false;
						}, 2000);
					}}
					type="button"
				>
					{createdKeyCopied ? $i18n.t('Copied') : $i18n.t('Copy')}
				</button>
			</div>
		</div>
	{/if}

	<div class="flex-1 overflow-y-auto scrollbar-hidden">
		{#if keys === null}
			<div class="flex justify-center py-10">
				<Spinner />
			</div>
		{:else if keys.length === 0}
			<div class="text-xs text-gray-500 dark:text-gray-400 py-6 text-center">
				{$i18n.t('No API keys yet.')}
			</div>
		{:else}
			<div class="flex flex-col gap-1.5">
				{#each keys as key (key.id)}
					<div
						class="flex justify-between items-center gap-2 px-3 py-2 rounded-lg bg-gray-50 dark:bg-gray-850"
					>
						<div class="min-w-0">
							<div class="flex items-center gap-1.5">
								<div class="text-xs font-medium truncate">
									{key.name || $i18n.t('Untitled')}
								</div>
								{#if isExpired(key)}
									<div class="text-[0.65rem] px-1.5 rounded-full bg-red-500/10 text-red-500">
										{$i18n.t('Expired')}
									</div>
								{/if}
							</div>
							<div class="text-xs text-gray-500 dark:text-gray-400 font-mono truncate">
								{key.key_hint}
							</div>
							<div class="text-[0.65rem] text-gray-500 dark:text-gray-400 mt-0.5">
								{$i18n.t('Created At')}
								{dayjs(key.created_at * 1000).format('LL')}
								·
								{$i18n.t('Last used')}
								{key.last_used_at ? dayjs(key.last_used_at * 1000).fromNow() : $i18n.t('Never')}
								·
								{$i18n.t('Expires')}
								{key.expires_at ? dayjs(key.expires_at * 1000).format('LL') : $i18n.t('Never')}
							</div>
						</div>

						<Tooltip content={$i18n.t('Revoke')}>
							<button
								class="shrink-0 text-xs font-medium px-2 py-1 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 text-red-500 transition"
								aria-label={$i18n.t('Revoke')}
								on:click={() => {
									keyToRevoke = key;
									showRevokeConfirmDialog = true;
								}}
								type="button"
							>
								{$i18n.t('Revoke')}
							</button>
						</Tooltip>
					</div>
				{/each}
			</div>
		{/if}
	</div>

	<div class="pt-2.5">
		{#if showCreateForm}
			<div class="flex flex-col gap-1.5 px-3 py-2.5 rounded-lg bg-gray-50 dark:bg-gray-850">
				<input
					class="w-full text-xs bg-transparent outline-hidden"
					placeholder={$i18n.t('Name')}
					bind:value={name}
					on:keydown={(e) => {
						// The tab lives inside the group form — don't let Enter submit it.
						if (e.key === 'Enter') {
							e.preventDefault();
							createHandler();
						}
					}}
				/>

				<div class="flex justify-between items-center gap-2">
					<select
						class="text-xs bg-transparent outline-hidden dark:text-gray-300"
						bind:value={expiresInDays}
					>
						{#each EXPIRY_OPTIONS as option}
							<option class="dark:bg-gray-900" value={option}>
								{option === '0'
									? $i18n.t('No expiration')
									: $i18n.t('Expires in {{days}} days', { days: option })}
							</option>
						{/each}
					</select>

					<div class="flex gap-1.5">
						<button
							class="text-xs font-medium px-3 py-1.5 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 transition"
							on:click={() => {
								showCreateForm = false;
							}}
							type="button"
						>
							{$i18n.t('Cancel')}
						</button>
						<button
							class="text-xs font-medium px-3 py-1.5 rounded-lg bg-black hover:bg-gray-900 text-white dark:bg-white dark:text-black dark:hover:bg-gray-100 transition flex items-center gap-1.5 {loading
								? 'cursor-not-allowed'
								: ''}"
							disabled={loading}
							on:click={() => {
								createHandler();
							}}
							type="button"
						>
							{$i18n.t('Create')}
							{#if loading}
								<Spinner className="size-3" />
							{/if}
						</button>
					</div>
				</div>
			</div>
		{:else}
			<button
				class="flex gap-1.5 items-center text-xs font-medium px-3.5 py-1.5 rounded-lg bg-gray-100/70 hover:bg-gray-100 dark:bg-gray-850 dark:hover:bg-gray-800 transition"
				on:click={() => {
					showCreateForm = true;
				}}
				type="button"
			>
				<Plus strokeWidth="2" className="size-3.5" />
				{$i18n.t('Create new secret key')}
			</button>
		{/if}
	</div>
</div>
