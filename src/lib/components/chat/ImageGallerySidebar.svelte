<script lang="ts">
	import { onMount, onDestroy, getContext } from 'svelte';
	import panzoom, { type PanZoom } from 'panzoom';
	import { WEBUI_BASE_URL } from '$lib/constants';
	import { showImageGallery, imageGalleryData } from '$lib/stores';
	import XMark from '$lib/components/icons/XMark.svelte';

	const i18n = getContext('i18n');

	let images: string[] = [];
	let currentIndex = 0;
	let loading = true;
	let error = '';
	let directMode = false;

	let sceneElement: HTMLElement;
	let instance: PanZoom | null = null;

	$: folder = $imageGalleryData?.folder ?? '';
	$: currentFile = $imageGalleryData?.current ?? '';

	$: if ($imageGalleryData && $showImageGallery) {
		loadImages();
	}

	async function loadImages() {
		loading = true;
		error = '';
		currentIndex = 0;

		// Mode 1: Direct image URLs from MCP tool results
		if ($imageGalleryData?.images && $imageGalleryData.images.length > 0) {
			directMode = true;
			images = $imageGalleryData.images;
			if (currentFile) {
				const idx = images.findIndex(
					(img) => img === currentFile || img.endsWith('/' + currentFile) || img.includes(currentFile)
				);
				currentIndex = idx >= 0 ? idx : 0;
			}
			loading = false;
			return;
		}

		// Mode 2: Folder-based listing via image proxy API
		directMode = false;
		if (!folder) {
			loading = false;
			return;
		}
		try {
			const params = new URLSearchParams({ filename: `${folder}/${currentFile}` });
			const resp = await fetch(
				`${WEBUI_BASE_URL}/api/v1/image_proxy/get_image_list?${params}`,
				{ credentials: 'include' }
			);
			if (!resp.ok) throw new Error(`Failed to load image list: ${resp.status}`);
			const data = await resp.json();

			if (Array.isArray(data)) {
				images = data;
			} else if (data?.images) {
				images = data.images;
			} else if (data?.data) {
				images = data.data;
			} else if (data?.items) {
				images = data.items;
			} else {
				images = [];
			}

			if (currentFile && images.length > 0) {
				const idx = images.findIndex(
					(img) => img === currentFile || img.endsWith('/' + currentFile) || img.includes(currentFile)
				);
				currentIndex = idx >= 0 ? idx : 0;
			}
		} catch (e: any) {
			error = e.message || 'Failed to load images';
			images = [];
		} finally {
			loading = false;
		}
	}

	function getImageUrl(imagePath: string): string {
		// Direct URLs (http/https/data) are used as-is
		if (imagePath.startsWith('http://') || imagePath.startsWith('https://') || imagePath.startsWith('data:')) {
			return imagePath;
		}
		// Folder-based: proxy through open-webui backend
		const filename = imagePath.split('/').pop() || imagePath;
		const imageFolder = imagePath.substring(0, imagePath.lastIndexOf('/')) || folder;
		const params = new URLSearchParams({ filename, folder: imageFolder });
		return `${WEBUI_BASE_URL}/api/v1/image_proxy/get_image?${params}`;
	}

	function goTo(index: number) {
		if (index < 0 || index >= images.length) return;
		currentIndex = index;
		resetZoom();
	}

	function prev() {
		goTo(currentIndex - 1);
	}

	function next() {
		goTo(currentIndex + 1);
	}

	function close() {
		showImageGallery.set(false);
		imageGalleryData.set(null);
	}

	function resetZoom() {
		if (instance) {
			instance.dispose();
			instance = null;
		}
		if (sceneElement) {
			instance = panzoom(sceneElement, {
				bounds: true,
				boundsPadding: 0.1,
				zoomSpeed: 0.065,
				maxZoom: 10,
				minZoom: 0.1
			});
		}
	}

	function handleKeyDown(event: KeyboardEvent) {
		if (!$showImageGallery) return;
		if (event.key === 'Escape') close();
		else if (event.key === 'ArrowLeft') prev();
		else if (event.key === 'ArrowRight') next();
	}

	onMount(() => {
		window.addEventListener('keydown', handleKeyDown);
	});

	onDestroy(() => {
		window.removeEventListener('keydown', handleKeyDown);
		if (instance) {
			instance.dispose();
			instance = null;
		}
	});

	$: currentImageName = images[currentIndex]
		? images[currentIndex].split('/').pop()
		: '';
</script>

{#if $showImageGallery}
	<div class="flex flex-col h-full bg-white dark:bg-gray-850 border-l border-gray-100 dark:border-gray-800">
		<!-- Header -->
		<div class="flex items-center justify-between px-3 py-2 border-b border-gray-100 dark:border-gray-800 shrink-0">
			<div class="flex items-center gap-2 min-w-0">
				<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" class="size-4 shrink-0 text-gray-500">
					<path stroke-linecap="round" stroke-linejoin="round" d="m2.25 15.75 5.159-5.159a2.25 2.25 0 0 1 3.182 0l5.159 5.159m-1.5-1.5 1.409-1.409a2.25 2.25 0 0 1 3.182 0l2.909 2.909M3.75 21h16.5A2.25 2.25 0 0 0 22.5 18.75V5.25A2.25 2.25 0 0 0 20.25 3H3.75A2.25 2.25 0 0 0 1.5 5.25v13.5A2.25 2.25 0 0 0 3.75 21Z" />
				</svg>
				<span class="text-sm font-medium text-gray-700 dark:text-gray-300 truncate">
					{$i18n.t('Image Gallery')}
				</span>
			</div>
			<button
				class="p-1 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 transition text-gray-500"
				on:click={close}
			>
				<XMark className="size-4" />
			</button>
		</div>

		{#if loading}
			<div class="flex-1 flex items-center justify-center">
				<div class="text-sm text-gray-400">{$i18n.t('Loading')}...</div>
			</div>
		{:else if error}
			<div class="flex-1 flex items-center justify-center p-4">
				<div class="text-sm text-red-500">{error}</div>
			</div>
		{:else if images.length === 0}
			<div class="flex-1 flex items-center justify-center">
				<div class="text-sm text-gray-400">{$i18n.t('No images found')}</div>
			</div>
		{:else}
			<!-- Main image viewer -->
			<div class="flex-1 flex flex-col min-h-0">
				<!-- Current image display -->
				<div class="flex-1 relative overflow-hidden bg-gray-50 dark:bg-gray-900 flex items-center justify-center min-h-0">
					<img
						bind:this={sceneElement}
						src={getImageUrl(images[currentIndex])}
						alt={currentImageName}
						class="max-w-full max-h-full object-contain select-none"
						draggable="false"
						on:load={resetZoom}
					/>
				</div>

				<!-- Navigation controls -->
				<div class="flex items-center justify-between px-3 py-2 border-t border-gray-100 dark:border-gray-800 shrink-0">
					<button
						class="p-1.5 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 transition disabled:opacity-30 disabled:cursor-not-allowed"
						on:click={prev}
						disabled={currentIndex === 0}
					>
						<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" class="size-4">
							<path fill-rule="evenodd" d="M11.78 5.22a.75.75 0 0 1 0 1.06L8.06 10l3.72 3.72a.75.75 0 1 1-1.06 1.06l-4.25-4.25a.75.75 0 0 1 0-1.06l4.25-4.25a.75.75 0 0 1 1.06 0Z" clip-rule="evenodd" />
						</svg>
					</button>

					<div class="text-xs text-gray-500 dark:text-gray-400 text-center truncate px-2">
						<span class="font-medium">{currentIndex + 1}</span> / {images.length}
						<div class="truncate text-[10px] opacity-70 mt-0.5">{currentImageName}</div>
					</div>

					<button
						class="p-1.5 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 transition disabled:opacity-30 disabled:cursor-not-allowed"
						on:click={next}
						disabled={currentIndex === images.length - 1}
					>
						<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" class="size-4">
							<path fill-rule="evenodd" d="M8.22 5.22a.75.75 0 0 1 1.06 0l4.25 4.25a.75.75 0 0 1 0 1.06l-4.25 4.25a.75.75 0 0 1-1.06-1.06L11.94 10 8.22 6.28a.75.75 0 0 1 0-1.06Z" clip-rule="evenodd" />
						</svg>
					</button>
				</div>
			</div>

			<!-- Thumbnail strip -->
			<div class="border-t border-gray-100 dark:border-gray-800 shrink-0">
				<div class="flex gap-1 p-2 overflow-x-auto scrollbar-hidden">
					{#each images as img, idx}
						<button
							class="shrink-0 w-12 h-12 rounded-md overflow-hidden border-2 transition {idx === currentIndex
								? 'border-blue-500 ring-1 ring-blue-500/30'
								: 'border-transparent hover:border-gray-300 dark:hover:border-gray-600'}"
							on:click={() => goTo(idx)}
						>
							<img
								src={getImageUrl(img)}
								alt={img.split('/').pop()}
								class="w-full h-full object-cover"
								loading="lazy"
							/>
						</button>
					{/each}
				</div>
			</div>
		{/if}
	</div>
{/if}
