<script lang="ts">
	import { onMount } from 'svelte';
	import panzoom, { type PanZoom, type PanZoomOptions } from 'panzoom';

	export let className = '';
	export let options: Partial<PanZoomOptions> = {};

	let containerElement: HTMLElement;
	let instance: PanZoom | undefined;

	export const reset = () => {
		instance?.moveTo(0, 0);
		instance?.zoomAbs(0, 0, 1);
	};

	// Lock panning until the user has zoomed in. Standard image-viewer behavior
	// (PhotoSwipe, Lightbox, macOS Preview): at fit-to-screen scale the image
	// stays centered and only becomes draggable once it overflows the viewport.
	const isNotZoomed = () => (instance?.getTransform().scale ?? 1) <= 1;
	const lockMousePan = () => {
		const locked = isNotZoomed();
		console.debug('[PanzoomContainer] beforeMouseDown locked=', locked, 'scale=', instance?.getTransform().scale);
		return locked;
	};
	// Only block single-finger drags so that two-finger pinch zoom still works.
	const lockTouchPan = (e: TouchEvent) => e.touches.length === 1 && isNotZoomed();

	onMount(() => {
		const defaultOpts: PanZoomOptions = {
			bounds: true,
			boundsPadding: 0.1,
			zoomSpeed: 0.065,
			beforeMouseDown: lockMousePan,
			onTouch: lockTouchPan
		};
		const localInstance = panzoom(containerElement, { ...defaultOpts, ...options });
		instance = localInstance;
		return () => {
			localInstance.dispose();
		};
	});
</script>

<div bind:this={containerElement} class={className}>
	<slot />
</div>
