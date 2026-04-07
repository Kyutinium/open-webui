<script lang="ts">
	import { onMount, onDestroy, getContext } from 'svelte';
	import panzoom, { type PanZoom } from 'panzoom';
	import { showImageGallery, imageGalleryData } from '$lib/stores';
	import XMark from '$lib/components/icons/XMark.svelte';

	const i18n = getContext('i18n');

	let images: string[] = [];
	let currentIndex = 0;
	let loading = true;

	// Page URL pattern state
	let pageBase = '';
	let pageExt = '';
	let minPage = 1;
	let maxPageFound = 1;
	let maxPageSearching = false;
	let patternMode = false;

	let sceneElement: HTMLElement;
	let instance: PanZoom | null = null;
	let thumbStrip: HTMLElement;

	// Auto-scroll thumbnail strip to keep current in view
	$: if (thumbStrip && images.length > 1) {
		const thumb = thumbStrip.children[currentIndex] as HTMLElement;
		if (thumb) {
			thumb.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });
		}
	}

	// Drag-to-scroll for thumbnail strip
	let isDragging = false;
	let dragStartX = 0;
	let dragScrollLeft = 0;

	function onThumbMouseDown(e: MouseEvent) {
		isDragging = true;
		dragStartX = e.pageX - thumbStrip.offsetLeft;
		dragScrollLeft = thumbStrip.scrollLeft;
		thumbStrip.style.cursor = 'grabbing';
	}

	function onThumbMouseMove(e: MouseEvent) {
		if (!isDragging) return;
		e.preventDefault();
		const x = e.pageX - thumbStrip.offsetLeft;
		const walk = (x - dragStartX) * 2;
		thumbStrip.scrollLeft = dragScrollLeft - walk;
	}

	function onThumbMouseUp() {
		isDragging = false;
		if (thumbStrip) thumbStrip.style.cursor = 'grab';
	}

	$: folder = $imageGalleryData?.folder ?? '';
	$: currentFile = $imageGalleryData?.current ?? '';

	$: if ($imageGalleryData && $showImageGallery) {
		loadImages();
	}

	function parsePageUrl(url: string): { base: string; ext: string; pageNum: number } | null {
		const match = url.match(/^(.+\/)(\d+)(\.\w+)$/);
		if (!match) return null;
		return { base: match[1], pageNum: parseInt(match[2], 10), ext: match[3] };
	}

	function buildPageUrl(n: number): string {
		return `${pageBase}${n}${pageExt}`;
	}

	function checkImageExists(url: string): Promise<boolean> {
		return new Promise((resolve) => {
			const img = new Image();
			img.onload = () => resolve(true);
			img.onerror = () => resolve(false);
			img.src = url;
		});
	}

	async function discoverMaxPage(startFrom: number) {
		if (maxPageSearching) return;
		maxPageSearching = true;
		let n = startFrom;
		while (true) {
			const exists = await checkImageExists(buildPageUrl(n + 1));
			if (exists) {
				n++;
				maxPageFound = n;
				rebuildImageList();
			} else {
				break;
			}
		}
		maxPageSearching = false;
	}

	function rebuildImageList() {
		const newImages: string[] = [];
		for (let i = minPage; i <= maxPageFound; i++) {
			newImages.push(buildPageUrl(i));
		}
		images = newImages;
	}

	async function loadImages() {
		loading = true;
		currentIndex = 0;
		patternMode = false;
		pageBase = '';
		pageExt = '';
		minPage = 1;
		maxPageFound = 1;

		// Mode 1: Direct image URLs provided
		if ($imageGalleryData?.images && $imageGalleryData.images.length > 0) {
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

		// Mode 2: URL pattern-based lazy loading
		const fullUrl = folder && currentFile ? `${folder}/${currentFile}` : '';
		if (!fullUrl) {
			images = [];
			loading = false;
			return;
		}

		const parsed = parsePageUrl(fullUrl);
		if (parsed) {
			patternMode = true;
			pageBase = parsed.base;
			pageExt = parsed.ext;
			maxPageFound = parsed.pageNum;

			// Start with just the clicked page
			images = [fullUrl];
			currentIndex = 0;
			loading = false;

			// Discover nearby pages in background
			// Check forward
			discoverMaxPage(parsed.pageNum);
			// Check backward (find minPage)
			discoverMinPage(parsed.pageNum);
		} else {
			// Not a page-numbered URL, show as single image
			images = [fullUrl];
			currentIndex = 0;
			loading = false;
		}
	}

	async function discoverMinPage(startFrom: number) {
		let n = startFrom;
		while (n > 1) {
			const exists = await checkImageExists(buildPageUrl(n - 1));
			if (exists) {
				n--;
				minPage = n;
				rebuildImageList();
				// Update currentIndex to keep same image selected
				currentIndex = images.findIndex((img) => img === buildPageUrl(startFrom));
				if (currentIndex < 0) currentIndex = 0;
			} else {
				break;
			}
		}
	}

	async function ensurePageAhead(currentPage: number, ahead: number = 3) {
		if (!patternMode) return;
		const target = currentPage + ahead;
		if (target <= maxPageFound) return;
		if (maxPageSearching) return;
		await discoverMaxPage(maxPageFound);
	}

	function getImageUrl(imagePath: string): string {
		return imagePath;
	}

	function goTo(index: number) {
		if (index < 0 || index >= images.length) return;
		currentIndex = index;
		resetZoom();
		// Pre-fetch ahead when navigating forward
		if (patternMode) {
			const currentPage = minPage + index;
			ensurePageAhead(currentPage);
		}
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
	$: pageDisplay = patternMode
		? `${minPage + currentIndex}`
		: `${currentIndex + 1}`;
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
						<span class="font-medium">p.{pageDisplay}</span> / {images.length}{maxPageSearching ? '+' : ''}
						<div class="truncate text-[10px] opacity-70 mt-0.5">{currentImageName}</div>
					</div>

					<button
						class="p-1.5 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 transition disabled:opacity-30 disabled:cursor-not-allowed"
						on:click={next}
						disabled={currentIndex === images.length - 1 && !maxPageSearching}
					>
						<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" class="size-4">
							<path fill-rule="evenodd" d="M8.22 5.22a.75.75 0 0 1 1.06 0l4.25 4.25a.75.75 0 0 1 0 1.06l-4.25 4.25a.75.75 0 0 1-1.06-1.06L11.94 10 8.22 6.28a.75.75 0 0 1 0-1.06Z" clip-rule="evenodd" />
						</svg>
					</button>
				</div>
			</div>

			<!-- Thumbnail strip (only show discovered pages, max 20 visible) -->
			{#if images.length > 1}
				<div class="border-t border-gray-100 dark:border-gray-800 shrink-0">
					<!-- svelte-ignore a11y-no-static-element-interactions -->
					<div
						bind:this={thumbStrip}
						class="flex gap-1 p-2 overflow-x-auto scrollbar-hidden cursor-grab select-none"
						on:mousedown={onThumbMouseDown}
						on:mousemove={onThumbMouseMove}
						on:mouseup={onThumbMouseUp}
						on:mouseleave={onThumbMouseUp}
					>
						{#each images as img, idx}
							<button
								class="shrink-0 w-10 h-10 rounded overflow-hidden border-2 transition {idx === currentIndex
									? 'border-blue-500 ring-1 ring-blue-500/30'
									: 'border-transparent hover:border-gray-300 dark:hover:border-gray-600'}"
								on:click={() => { if (!isDragging) goTo(idx); }}
							>
								<img
									src={getImageUrl(img)}
									alt={img.split('/').pop()}
									class="w-full h-full object-cover pointer-events-none"
									loading="lazy"
								/>
							</button>
						{/each}
					</div>
				</div>
			{/if}
		{/if}
	</div>
{/if}
