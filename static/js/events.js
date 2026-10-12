// ============================================================
//  FULLSCREEN MEDIA VIEWER — shared across events & event_detail
//  Pinch-to-zoom + pan — iOS, Android, desktop
//  ============================================================

(function() {
  'use strict';

  const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
              (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

  // All platforms use custom CSS-transform pinch-to-zoom in the viewer.
  // The viewport is locked (maximum-scale=1.0, user-scalable=no) while
  // the viewer is active so native page zoom cannot interfere.
  const isMobile = document.documentElement.classList.contains('is-mobile') ||
                 ((('ontouchstart' in window) || navigator.maxTouchPoints > 0) &&
                  (window.matchMedia && window.matchMedia('(pointer: coarse)').matches));

  // ---- DOM refs ----
  let viewer = null;
  let titleOverlay = null;
  let counter = null;
  let thumbStrip = null;
  let closeBtn = null;
  let prevArrow = null;
  let nextArrow = null;
  let allMedia = [];
  let mediaGroups = [];
  let currentGroupIndex = 0;
  let stripObserver = null;
  let currentMediaIndex = -1;
  let preloadImages = [];
  let header = null;
  let thumbStripBuilt = false;
  let savedScrollY = 0;
  let currentViewerMedia = null;
  let zoomContainer = null;
  let pendingPlayTimer = null;

  // ---- Auto-hiding chrome ----
  let chromeTimer = null;
  let singleTapTimer = null;
  let lastTouchEndTime = 0;
  const CHROME_HIDE_DELAY = 3000;

  // ---- Stop a video element completely (pause + release pipeline) ----
  function stopVideo(v) {
    if (!v || v.tagName !== 'VIDEO') return;
    try { v.pause(); } catch (err) {}
    v.removeAttribute('src');
    try { v.load(); } catch (err) {}
  }

  // ---- Auto-hiding chrome (title, X, arrows, counter, thumb strip) ----
  function showChrome() {
    viewer.classList.remove('chrome-hidden');
    clearTimeout(chromeTimer);
    chromeTimer = setTimeout(hideChrome, CHROME_HIDE_DELAY);
  }

  function hideChrome() {
    clearTimeout(chromeTimer);
    viewer.classList.add('chrome-hidden');
  }

  // ---- Zoom state ----
  let zoomScale = 1;
  let zoomPanX = 0;
  let zoomPanY = 0;
  let isZoomed = false;
  const MAX_ZOOM = 5;
  const MIN_ZOOM = 1;

  // ---- Desktop drag-pan state ----
  let isMouseDragging = false;
  let justDragged = false;
  let dragStartX = 0;
  let dragStartY = 0;
  let dragStartPanX = 0;
  let dragStartPanY = 0;

  // ---- Touch tracking ----
  let touchStartX = 0;
  let touchStartY = 0;
  let touchStartTime = 0;
  let pinchStartDist = 0;
  let pinchStartScale = 1;
  let isPinching = false;
  let isDragging = false;
  let lastPinchEndTime = 0;
  // Accumulated pan offset for dragging (persists across touch sequences)
  let panOffsetX = 0;
  let panOffsetY = 0;

  // ---- Init ----
  document.addEventListener('DOMContentLoaded', function() {
    viewer = document.getElementById('fullscreen-viewer');
    if (!viewer) return;
    if (isIOS) viewer.classList.add('is-ios');

    titleOverlay = document.getElementById('event-title-overlay');
    counter = document.getElementById('media-counter');
    thumbStrip = document.getElementById('thumb-strip');
    closeBtn = document.getElementById('viewer-close');
    prevArrow = document.getElementById('prev-arrow');
    nextArrow = document.getElementById('next-arrow');
    header = document.querySelector('.header-nav');

    collectAllMedia();

    mediaGroups.forEach(function(group, groupIndex) {
      group.forEach(function(item, index) {
        item.el.addEventListener('click', function(e) {
          e.stopPropagation();
          currentGroupIndex = groupIndex;
          allMedia = group;
          currentMediaIndex = index;
          openViewer();
        });
      });
    });

    if (prevArrow) prevArrow.addEventListener('click', function(e) { e.stopPropagation(); navMedia(-1); });
    if (nextArrow) nextArrow.addEventListener('click', function(e) { e.stopPropagation(); navMedia(1); });
    if (closeBtn) closeBtn.addEventListener('click', function(e) { e.stopPropagation(); closeViewer(); });

    // Backdrop click to close
    viewer.addEventListener('click', function(e) {
      if (justDragged) { justDragged = false; return; }
      if (e.target.classList.contains('nav-arrow')) return;
      if (e.target.classList.contains('viewer-close')) return;
      if (e.target.classList.contains('thumb-strip-item')) return;
      if (e.target.id === 'thumb-strip') return;
      if (e.target.classList.contains('viewer-zoom-container')) return;
      if (e.target.classList.contains('viewer-media')) return;
      if (e.target.tagName === 'VIDEO') {
        let rect = e.target.getBoundingClientRect();
        if (e.clientY - rect.top > rect.height - 40) return;
      }
      closeViewer();
    });

    // Touch handlers on the zoom container (the scrollable/zoomable area)
    // We attach to viewer and filter by target
    viewer.addEventListener('touchstart', handleTouchStart, { passive: false });
    viewer.addEventListener('touchmove', handleTouchMove, { passive: false });
    viewer.addEventListener('touchend', handleTouchEnd, { passive: false });

    // Tap handling: single tap toggles chrome, double tap toggles zoom
    let lastTapTime = 0;
    viewer.addEventListener('touchend', function(e) {
      let now = Date.now();
      lastTouchEndTime = now;
      // Only single-finger releases count as taps
      if (e.changedTouches.length !== 1) { lastTapTime = 0; return; }
      // Ignore touchends within 400ms of a pinch ending — simultaneous
      // two-finger release fires two rapid touchends that look like a double-tap
      if (now - lastPinchEndTime < 400) { lastTapTime = 0; return; }
      if (now - lastTapTime < 300 && !isPinching && !isDragging) {
        // Double tap: cancel any pending chrome toggle, zoom instead
        clearTimeout(singleTapTimer);
        e.preventDefault();
        toggleZoom();
        showChrome();
        lastTapTime = now;
        return;
      }
      // Potential single tap: must be a genuine tap (no drag/swipe movement)
      let t = e.changedTouches[0];
      let moved = Math.abs(t.clientX - touchStartX) > 10 ||
                  Math.abs(t.clientY - touchStartY) > 10;
      if (!moved && !isPinching && !isDragging && !isControlElement(e.target)) {
        let target = e.target;
        // Decide based on chrome state NOW, not when the timer fires —
        // iOS fires a synthetic mousemove right after a tap that would
        // otherwise show the chrome and make the deferred toggle re-hide it
        let chromeWasHidden = viewer.classList.contains('chrome-hidden');
        clearTimeout(singleTapTimer);
        singleTapTimer = setTimeout(function() {
          // Videos: a tap interacts with native controls (play/pause),
          // so just wake the chrome rather than toggling it away
          if (target.tagName === 'VIDEO') { showChrome(); return; }
          if (chromeWasHidden) showChrome();
          else hideChrome();
        }, 320);
      }
      lastTapTime = now;
    }, { passive: false });

    // Desktop: moving the mouse wakes the chrome. Touch devices fire a
    // synthetic mousemove after each tap — ignore those or they'd fight
    // the tap-toggle.
    viewer.addEventListener('mousemove', function() {
      if (Date.now() - lastTouchEndTime < 1000) return;
      if (viewer.classList.contains('active')) showChrome();
    });

    // ---- Desktop zoom: cursor-anchored wheel zoom + drag-to-pan ----
    if (!isMobile) {
      // Wheel zooms toward the cursor
      viewer.addEventListener('wheel', function(e) {
        if (!currentViewerMedia || isControlElement(e.target)) return;
        e.preventDefault();
        showChrome();
        let factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
        zoomAt(zoomScale * factor, e.clientX, e.clientY);
      }, { passive: false });

      // Drag to pan while zoomed
      viewer.addEventListener('mousedown', function(e) {
        if (!isZoomed || isControlElement(e.target)) return;
        if (e.button !== 0) return;
        e.preventDefault();
        isMouseDragging = true;
        dragStartX = e.clientX;
        dragStartY = e.clientY;
        dragStartPanX = zoomPanX;
        dragStartPanY = zoomPanY;
        currentViewerMedia.style.transition = 'none';
        updateCursor();
      });

      window.addEventListener('mousemove', function(e) {
        if (!isMouseDragging) return;
        justDragged = true;
        zoomPanX = dragStartPanX + (e.clientX - dragStartX);
        zoomPanY = dragStartPanY + (e.clientY - dragStartY);
        clampAndApply();
      });

      window.addEventListener('mouseup', function() {
        if (isMouseDragging) {
          isMouseDragging = false;
          updateCursor();
        }
      });
    }

    // Double-click toggles zoom, anchored at the click point (desktop only —
    // touch double-tap synthesizes a dblclick, which must NOT run custom zoom
    // on mobile or it produces the GPU grid)
    viewer.addEventListener('dblclick', function(e) {
      if (isMobile) return;
      if (isControlElement(e.target)) return;
      e.preventDefault();
      if (isZoomed) {
        resetZoom();
        updateCursor();
      } else {
        zoomAt(2.5, e.clientX, e.clientY);
      }
    });

    document.addEventListener('keydown', handleKeyboard);

    if (thumbStrip) {
      thumbStrip.addEventListener('click', function(e) {
        let thumb = e.target.closest('.thumb-strip-item');
        if (thumb) {
          e.stopPropagation();
          let idx = parseInt(thumb.dataset.index);
          if (!isNaN(idx)) {
            currentMediaIndex = idx;
            displayCurrentMedia();
            updateThumbStrip();
          }
        }
      });
    }
  });

  // ---- Touch Handlers ----
  function isControlElement(target) {
    return target.closest('.thumb-strip-item') ||
           target.closest('.viewer-close') ||
           target.closest('.nav-arrow');
  }

  function handleTouchStart(e) {
    if (isControlElement(e.target)) return;

    if (e.touches.length === 2) {
      e.preventDefault();
      isPinching = true;
      isDragging = false;
      pinchStartDist = getDist(e.touches[0], e.touches[1]);
      pinchStartScale = zoomScale;
      touchStartTime = Date.now();
    } else if (e.touches.length === 1) {
      touchStartX = e.touches[0].clientX;
      touchStartY = e.touches[0].clientY;
      touchStartTime = Date.now();
      isPinching = false;
      isDragging = false;
    }
  }

  function handleTouchMove(e) {
    if (isControlElement(e.target)) return;

    if (isPinching && e.touches.length === 2) {
      e.preventDefault();
      let dist = getDist(e.touches[0], e.touches[1]);
      let ratio = dist / pinchStartDist;
      zoomScale = Math.max(1, Math.min(6, pinchStartScale * ratio));
      isZoomed = zoomScale > 1;
      applyTransform();
    } else if (e.touches.length === 1 && isZoomed) {
      e.preventDefault();
      isDragging = true;
      // Accumulate delta from last known start, not absolute position
      let dx = e.touches[0].clientX - touchStartX;
      let dy = e.touches[0].clientY - touchStartY;
      zoomPanX = panOffsetX + dx;
      zoomPanY = panOffsetY + dy;
      applyTransform();
    }
  }

  function handleTouchEnd(e) {
    if (isPinching) {
      isPinching = false;
      lastPinchEndTime = Date.now();
      // If dropping to 1 finger, save current pan as offset and update touch start
      if (e.touches.length === 1) {
        panOffsetX = zoomPanX;
        panOffsetY = zoomPanY;
        touchStartX = e.touches[0].clientX;
        touchStartY = e.touches[0].clientY;
      }
      clampAndApply();
      return;
    }

    if (e.changedTouches.length !== 1) return;

    let dx = e.changedTouches[0].clientX - touchStartX;
    let dy = e.changedTouches[0].clientY - touchStartY;
    let elapsed = Date.now() - touchStartTime;

    if (isDragging) {
      isDragging = false;
      // Save accumulated offset for next drag
      panOffsetX = zoomPanX;
      panOffsetY = zoomPanY;
      clampAndApply();
      return;
    }

    // Swipe to navigate (only when not zoomed, and not the tail end of a pinch).
    if (!isZoomed && Date.now() - lastPinchEndTime > 400 &&
        Math.abs(dx) > Math.abs(dy) && Math.abs(dx) > 60 && elapsed < 500) {
      if (dx < 0) navMedia(1);
      else navMedia(-1);
    }
  }

  function getDist(t1, t2) {
    let dx = t1.clientX - t2.clientX;
    let dy = t1.clientY - t2.clientY;
    return Math.sqrt(dx * dx + dy * dy);
  }

  // ---- Transform ----
  function applyTransform() {
    if (!currentViewerMedia) return;
    currentViewerMedia.style.transform = 'scale(' + zoomScale + ') translate(' +
      (zoomPanX / zoomScale) + 'px, ' + (zoomPanY / zoomScale) + 'px)';
  }

  function clampAndApply() {
    if (!currentViewerMedia || !isZoomed) {
      zoomPanX = 0;
      zoomPanY = 0;
      applyTransform();
      return;
    }
    let vw = viewer.clientWidth;
    let vh = viewer.clientHeight;
    // Rendered (on-screen, pre-transform) size — not source resolution.
    // The element is laid out with object-fit/max-width, so its layout box is
    // what's actually visible; transform scales that box.
    let baseW = currentViewerMedia.clientWidth;
    let baseH = currentViewerMedia.clientHeight;

    let scaledW = baseW * zoomScale;
    let scaledH = baseH * zoomScale;

    if (scaledW > vw && scaledH > vh) {
      // Both dimensions overflow
      let overflowX = (scaledW - vw) / 2;
      let overflowY = (scaledH - vh) / 2;
      zoomPanX = Math.max(-overflowX, Math.min(overflowX, zoomPanX));
      zoomPanY = Math.max(-overflowY, Math.min(overflowY, zoomPanY));
    } else if (scaledW > vw) {
      let overflowX = (scaledW - vw) / 2;
      zoomPanX = Math.max(-overflowX, Math.min(overflowX, zoomPanX));
      zoomPanY = 0;
    } else if (scaledH > vh) {
      let overflowY = (scaledH - vh) / 2;
      zoomPanX = 0;
      zoomPanY = Math.max(-overflowY, Math.min(overflowY, zoomPanY));
    } else {
      zoomPanX = 0;
      zoomPanY = 0;
    }
    applyTransform();
  }

  // Zoom toward a screen point (clientX/Y) so that point stays put.
  // The image is centered in the viewer; pan is in screen px. Keeping the
  // cursor anchored means the image-space point under the cursor is the
  // same before and after the scale change.
  function zoomAt(newScale, clientX, clientY) {
    if (!currentViewerMedia) return;
    newScale = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, newScale));

    let rect = viewer.getBoundingClientRect();
    let centerX = rect.left + rect.width / 2;
    let centerY = rect.top + rect.height / 2;
    // Cursor offset from the viewer center, in screen px
    let offX = clientX - centerX;
    let offY = clientY - centerY;

    // Image-space point currently under the cursor (undo current pan+scale)
    let imgX = (offX - zoomPanX) / zoomScale;
    let imgY = (offY - zoomPanY) / zoomScale;

    zoomScale = newScale;
    isZoomed = zoomScale > 1.001;

    if (!isZoomed) {
      resetZoom();
      updateCursor();
      return;
    }
    // New pan so that same image point lands back under the cursor
    zoomPanX = offX - imgX * zoomScale;
    zoomPanY = offY - imgY * zoomScale;
    currentViewerMedia.style.transition = 'transform 0.12s ease-out';
    clampAndApply();
    updateCursor();
  }

  function updateCursor() {
    if (!currentViewerMedia) return;
    if (isMobile) { currentViewerMedia.style.cursor = ''; return; }
    // Don't show zoom cursor on videos
    if (currentViewerMedia.tagName === 'VIDEO') { currentViewerMedia.style.cursor = ''; return; }
    if (isMouseDragging) currentViewerMedia.style.cursor = 'grabbing';
    else if (isZoomed) currentViewerMedia.style.cursor = 'grab';
    else currentViewerMedia.style.cursor = 'zoom-in';
  }

  function resetZoom() {
    zoomScale = 1;
    zoomPanX = 0;
    zoomPanY = 0;
    panOffsetX = 0;
    panOffsetY = 0;
    isZoomed = false;
    isMouseDragging = false;
    if (currentViewerMedia) {
      currentViewerMedia.style.transition = 'transform 0.25s ease';
      currentViewerMedia.style.transform = '';
      updateCursor();
    }
  }

  function toggleZoom() {
    // Custom zoom is desktop-only. On mobile the browser's native pinch /
    // double-tap zoom handles everything (re-rasterizes, so no GPU grid).
    // Guarding here makes every caller — double-tap, dblclick, future ones —
    // a no-op on mobile in one place.
    if (isMobile) return;
    if (isZoomed) {
      resetZoom();
    } else {
      zoomScale = 2.5;
      isZoomed = true;
      currentViewerMedia.style.transition = 'transform 0.25s ease';
      applyTransform();
    }
  }

  // ---- Collect media ----
  function collectAllMedia() {
    mediaGroups = [];
    // Each container is its own gallery: one event card's .media-preview,
    // the event detail #media-group, or the index #layout-container.
    // Scoping per container keeps the viewer (and its thumb strip) small —
    // a page with dozens of events won't load every video at once.
    let containers = document.querySelectorAll('.media-preview, #media-group, #layout-container');
    containers.forEach(function(container) {
      let group = [];
      let imgs = container.querySelectorAll('img');
      let videos = container.querySelectorAll('video');
      imgs.forEach(function(img) {
        // Store both viewer derivative and original — displayCurrentMedia
        // will try the viewer image first and fall back on error.
        let viewerSrc = img.dataset.viewerFull || null;
        let fallbackSrc = img.dataset.full || img.src;
        group.push({ el: img, src: viewerSrc || fallbackSrc, fallbackSrc: fallbackSrc, type: 'image', title: img.dataset.eventTitle || '' });
      });
      videos.forEach(function(video) {
        let src = video.dataset.full || (video.querySelector('source') ? video.querySelector('source').src : video.src);
        group.push({ el: video, src: src, type: 'video', title: video.dataset.eventTitle || '' });
      });
      if (group.length) mediaGroups.push(group);
    });
  }

  // ---- Open viewer ----
  // ---- Viewport control ----
  let savedViewportContent = null;
  function lockViewport() {
    let vp = document.getElementById('viewport-meta');
    if (vp) {
      savedViewportContent = vp.getAttribute('content');
      vp.setAttribute('content', 'width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover');
    }
  }
  function unlockViewport() {
    let vp = document.getElementById('viewport-meta');
    if (vp && savedViewportContent !== null) {
      vp.setAttribute('content', savedViewportContent);
      savedViewportContent = null;
    }
  }

  function openViewer() {
    lockViewport();
    if (header) header.style.display = 'none';
    let alreadyOpen = viewer.classList.contains('active');
    showChrome();
    resetZoom();
    preloadAdjacent();
    displayCurrentMedia();
    updateThumbStrip();
    viewer.classList.add('active');
    if (!alreadyOpen) {
      // Lock background scroll without losing position: remember where we are,
      // then offset the fixed body by that amount so the page doesn't jump.
      // Only on first open — once body is fixed, scrollY reads 0 and a
      // second call would clobber the saved position.
      savedScrollY = window.scrollY || window.pageYOffset || 0;
      document.body.style.overflow = 'hidden';
      document.body.style.position = 'fixed';
      document.body.style.width = '100%';
      document.body.style.top = '-' + savedScrollY + 'px';
      document.body.style.right = '0';
      document.body.style.bottom = '';
      document.body.style.left = '0';
    }
  }

  // ---- Display media ----
  function displayCurrentMedia() {
    if (allMedia.length === 0) return;
    let media = allMedia[currentMediaIndex];

    if (titleOverlay) {
      titleOverlay.textContent = media.title;
      titleOverlay.style.display = media.title ? 'block' : 'none';
    }
    if (counter) {
      counter.textContent = (currentMediaIndex + 1) + ' / ' + allMedia.length;
      counter.style.display = allMedia.length > 1 ? 'block' : 'none';
    }

    let existing = currentViewerMedia;
    let el = null;

    if (existing && existing.tagName.toLowerCase() === (media.type === 'image' ? 'img' : 'video')) {
      el = existing;
      resetZoom();
      // Pause old playback before the src swap (the swap resets the pipeline)
      if (existing.tagName === 'VIDEO') {
        try { existing.pause(); } catch (err) {}
      }
      el.src = media.src;
    } else {
      viewer.querySelectorAll('.viewer-media').forEach(function(e) {
        stopVideo(e);
        e.remove();
      });
      el = document.createElement(media.type === 'image' ? 'img' : 'video');
      el.className = 'viewer-media';
      el.style.transform = '';
      el.src = media.src;
      viewer.appendChild(el);
    }

    // For images: if viewer derivative fails to load, fall back to original
    if (media.type === 'image' && media.fallbackSrc && media.src !== media.fallbackSrc) {
      el.onerror = function() {
        if (el.src !== media.fallbackSrc) {
          el.src = media.fallbackSrc;
          el.onerror = null;
        }
      };
    } else {
      el.onerror = null;
    }

    currentViewerMedia = el;
    updateCursor();

    if (media.type === 'video') {
      viewer.classList.add('video-playing');
      el.controls = true;
      el.autoplay = true;
      el.playsInline = true;
      el.setAttribute('playsinline', '');
      el.setAttribute('webkit-playsinline', '');
      el.muted = false;
      // Same behavior on all platforms: play inline inside the overlay.
      // If autoplay with sound is blocked, the user taps play in the controls.
      // Cancel any pending play from a previous item, then schedule with tracking
      // so we can cancel on navigation/close.
      clearTimeout(pendingPlayTimer);
      pendingPlayTimer = setTimeout(function() {
        // Only play if this element is still the current media and still in DOM
        if (el === currentViewerMedia && el.parentNode) {
          el.play().catch(function(){});
        }
        pendingPlayTimer = null;
      }, 150);
    } else {
      viewer.classList.remove('video-playing');
      clearTimeout(pendingPlayTimer);
      pendingPlayTimer = null;
    }

    if (prevArrow) prevArrow.style.display = allMedia.length > 1 ? '' : 'none';
    if (nextArrow) nextArrow.style.display = allMedia.length > 1 ? '' : 'none';
    preloadAdjacent();
  }

  // ---- Preload ----
  function preloadAdjacent() {
    preloadImages.forEach(function(img) { if (img && img.parentNode) img.parentNode.removeChild(img); });
    preloadImages = [];
    if (allMedia.length <= 1) return;
    let prevIdx = (currentMediaIndex - 1 + allMedia.length) % allMedia.length;
    let nextIdx = (currentMediaIndex + 1) % allMedia.length;
    [prevIdx, nextIdx].forEach(function(idx) {
      let item = allMedia[idx];
      if (item.type === 'image') {
        let img = new Image();
        img.src = item.src;
        preloadImages.push(img);
      }
    });
  }

  // ---- Thumb strip ----
  function makeBridgeTile(gIdx, dir) {
    let tile = document.createElement('div');
    tile.className = 'thumb-strip-bridge';
    let name = (mediaGroups[gIdx][0] && mediaGroups[gIdx][0].title) ||
               (dir > 0 ? 'Next' : 'Previous');
    tile.innerHTML = dir > 0
      ? '<span class="bridge-label">' + escapeHtml(name) + '</span> <span class="bridge-arrow">&#10095;</span>'
      : '<span class="bridge-arrow">&#10094;</span> <span class="bridge-label">' + escapeHtml(name) + '</span>';
    tile.title = name;
    tile.addEventListener('click', function(e) {
      e.stopPropagation();
      switchGroup(gIdx, dir > 0 ? 0 : -1);
    });
    return tile;
  }

  function escapeHtml(str) {
    let d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
  }

  function updateThumbStrip() {
    if (!thumbStrip) return;
    if (!thumbStripBuilt) {
      // Only thumbs actually visible in the strip get to decode video,
      // and only a short burst each time — enough motion to read as
      // "this is a video" without dozens of looping decoders.
      stripObserver = new IntersectionObserver(function(entries) {
        entries.forEach(function(entry) {
          let vid = entry.target;
          if (entry.isIntersecting) {
            try { vid.currentTime = 0; } catch (err) {}
            let p = vid.play();
            if (p !== undefined) p.catch(function(){});
            clearTimeout(vid._burstTimer);
            vid._burstTimer = setTimeout(function() { vid.pause(); }, 1500);
          } else {
            clearTimeout(vid._burstTimer);
            vid.pause();
          }
        });
      }, { root: thumbStrip, threshold: 0.5 });

      allMedia.forEach(function(item, idx) {
        let thumb = document.createElement('div');
        thumb.className = 'thumb-strip-item' + (idx === currentMediaIndex ? ' active' : '');
        thumb.dataset.index = idx;
        if (item.type === 'image') {
          let img = document.createElement('img');
          img.src = item.src;
          // Fall back to original if viewer derivative missing
          if (item.fallbackSrc && item.src !== item.fallbackSrc) {
            img.onerror = function() {
              img.src = item.fallbackSrc;
              img.onerror = null;
            };
          }
          thumb.appendChild(img);
        } else {
          let vid = document.createElement('video');
          vid.src = item.src;
          vid.muted = true;
          vid.loop = true;
          vid.playsInline = true;
          vid.setAttribute('playsinline', '');
          vid.setAttribute('webkit-playsinline', '');
          vid.preload = 'metadata';
          thumb.appendChild(vid);
          let badge = document.createElement('div');
          badge.className = 'thumb-video-badge';
          badge.innerHTML = '&#9654;';
          thumb.appendChild(badge);
          stripObserver.observe(vid);
        }
        thumbStrip.appendChild(thumb);
      });

      // Bridge tiles to adjacent events (shows the event's name)
      if (currentGroupIndex > 0) {
        thumbStrip.insertBefore(
          makeBridgeTile(currentGroupIndex - 1, -1), thumbStrip.firstChild);
      }
      if (currentGroupIndex < mediaGroups.length - 1) {
        thumbStrip.appendChild(makeBridgeTile(currentGroupIndex + 1, 1));
      }

      thumbStripBuilt = true;
    } else {
      let items = thumbStrip.querySelectorAll('.thumb-strip-item');
      items.forEach(function(thumb) {
        thumb.classList.toggle('active', parseInt(thumb.dataset.index) === currentMediaIndex);
      });
    }
    let activeThumb = thumbStrip.querySelector('.thumb-strip-item.active');
    if (activeThumb) activeThumb.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });
  }

  // ---- Nav ----
  function navMedia(dir) {
    // At an event boundary, flow into the adjacent event's gallery
    if (dir > 0 && currentMediaIndex === allMedia.length - 1 &&
        currentGroupIndex < mediaGroups.length - 1) {
      switchGroup(currentGroupIndex + 1, 0);
      return;
    }
    if (dir < 0 && currentMediaIndex === 0 && currentGroupIndex > 0) {
      switchGroup(currentGroupIndex - 1, -1);
      return;
    }
    if (allMedia.length <= 1) return;
    currentMediaIndex = (currentMediaIndex + dir + allMedia.length) % allMedia.length;
    displayCurrentMedia();
    updateThumbStrip();
    showChrome();
  }

  // ---- Switch to a different media group (event) while viewer is open ----
  function switchGroup(gIdx, mIdx) {
    if (gIdx < 0 || gIdx >= mediaGroups.length) return;
    showChrome();
    // Cancel pending play timer
    clearTimeout(pendingPlayTimer);
    pendingPlayTimer = null;
    // Stop the current viewer media
    if (currentViewerMedia) {
      if (currentViewerMedia.tagName === 'VIDEO') {
        stopVideo(currentViewerMedia);
      }
      currentViewerMedia.remove();
      currentViewerMedia = null;
    }
    // Stop all strip videos before clearing
    let stripVideos = thumbStrip.querySelectorAll('video');
    stripVideos.forEach(function(v) { stopVideo(v); });

    currentGroupIndex = gIdx;
    allMedia = mediaGroups[gIdx];
    currentMediaIndex = mIdx < 0 ? allMedia.length - 1 : mIdx;
    if (stripObserver) { stripObserver.disconnect(); stripObserver = null; }
    thumbStrip.innerHTML = '';
    thumbStripBuilt = false;
    resetZoom();
    preloadAdjacent();
    displayCurrentMedia();
    updateThumbStrip();
  }

  // ---- Close ----
  function closeViewer() {
    // Cancel any pending play timer
    clearTimeout(pendingPlayTimer);
    pendingPlayTimer = null;

    // Stop the main viewer media element
    if (currentViewerMedia) {
      if (currentViewerMedia.tagName === 'VIDEO') {
        stopVideo(currentViewerMedia);
      }
      currentViewerMedia.remove();
      currentViewerMedia = null;
    }

    resetZoom();
    viewer.classList.remove('active');
    viewer.classList.remove('video-playing');

    // Stop all strip videos before clearing (releases decoders immediately)
    let stripVideos = thumbStrip.querySelectorAll('video');
    stripVideos.forEach(function(v) { stopVideo(v); });

    // Clean up remaining viewer-media elements
    viewer.querySelectorAll('.viewer-media').forEach(function(e) { e.remove(); });

    // Clean up chrome timers and reset chrome visibility
    clearTimeout(chromeTimer);
    clearTimeout(singleTapTimer);
    viewer.classList.remove('chrome-hidden');

    if (stripObserver) { stripObserver.disconnect(); stripObserver = null; }
    thumbStrip.innerHTML = '';
    thumbStripBuilt = false;
    document.body.style.overflow = '';
    document.body.style.position = '';
    document.body.style.width = '';
    document.body.style.top = '';
    document.body.style.right = '';
    document.body.style.bottom = '';
    document.body.style.left = '';
    // Body is back in normal flow — return to where the user was
    window.scrollTo(0, savedScrollY);
    unlockViewport();
    if (header) header.style.display = '';
  }

  // ---- Keyboard ----
  function handleKeyboard(e) {
    if (!viewer || !viewer.classList.contains('active')) return;
    showChrome();
    switch(e.key) {
      case 'ArrowRight': e.preventDefault(); navMedia(1); break;
      case 'ArrowLeft': e.preventDefault(); navMedia(-1); break;
      case 'Escape':
        if (isZoomed) resetZoom();
        else closeViewer();
        break;
      case ' ':
        let video = viewer.querySelector('video');
        if (video) { e.preventDefault(); if (video.paused) video.play(); else video.pause(); }
        break;
      case 'i': case 'I':
        if (titleOverlay) titleOverlay.style.display = titleOverlay.style.display === 'none' ? 'block' : 'none';
        break;
    }
  }

  // ---- Expose globals for inline onclick handlers in templates ----
  window.closeViewer = closeViewer;
  window.navMedia = navMedia;

  // ---- showMedia for backward compatibility with inline onclick ----
  window.showMedia = function(src, type, eventTitle) {
    collectAllMedia();
    for (let g = 0; g < mediaGroups.length; g++) {
      let group = mediaGroups[g];
      for (let i = 0; i < group.length; i++) {
        if (group[i].src === src ||
            (group[i].type === type &&
             (group[i].src.indexOf(src) !== -1 || src.indexOf(group[i].src) !== -1))) {
          allMedia = group;
          currentMediaIndex = i;
          openViewer();
          return;
        }
      }
    }
    // No match — fall back to the first group
    if (mediaGroups.length) {
      allMedia = mediaGroups[0];
      currentMediaIndex = 0;
      openViewer();
    }
  };

})();

// ============================================================
//  PAGE-SPECIFIC LOGIC (events.html only)
//  ============================================================

document.addEventListener('DOMContentLoaded', function() {
  // ---- Year dropdown default to current year on focus ----
  let yearSelect = document.getElementById('yearSelect');
  if (yearSelect) {
    let currentYear = new Date().getFullYear();
    yearSelect.addEventListener('focus', function() {
      if (this.value === '') {
        for (let i = 0; i < this.options.length; i++) {
          if (this.options[i].value == currentYear) {
            this.selectedIndex = i;
            break;
          }
        }
      }
    });
  }

  // ---- Insert "Today" divider between past and upcoming events ----
  let cards = document.querySelectorAll('.event-card');
  if (cards.length > 0) {
    let dividerInserted = false;
    for (let i = 0; i < cards.length - 1; i++) {
      let currentIsPast = cards[i].dataset.past === 'true';
      let nextIsPast = cards[i + 1].dataset.past === 'true';
      if (currentIsPast && !nextIsPast) {
        let wrapper = document.createElement('div');
        wrapper.id = 'today-divider-target';
        wrapper.className = 'divider-wrapper';
        let divider = document.createElement('div');
        divider.className = 'today-divider';
        var listEl = document.getElementById('event-list');
        divider.textContent = (listEl && listEl.getAttribute('data-today-label')) || 'Today';
        wrapper.appendChild(divider);
        cards[i].after(wrapper);
        dividerInserted = true;
        break;
      }
    }
    if (!dividerInserted && cards.length > 0) {
      let midIdx = Math.floor(cards.length / 2);
      cards[midIdx].id = 'today-divider-target';
    }
  }

  // ---- Scroll to Today divider on initial visit ----
  let url = new URL(window.location);
  if (url.searchParams.get('scroll') === '1') {
    url.searchParams.delete('scroll');
    window.history.replaceState({}, '', url);
    let target = document.getElementById('today-divider-target');
    if (target) {
      target.scrollIntoView({ behavior: 'auto', block: 'center' });
    }
  }

  // ---- Autoplay preview videos only when in view (events strips, event detail, home feed) ----
  let videos = document.querySelectorAll('.media-preview video, #layout-container video');
  if (videos.length) {
    let observer = new IntersectionObserver(function(entries) {
      entries.forEach(function(entry) {
        let video = entry.target;
        if (entry.isIntersecting) {
          let playPromise = video.play();
          if (playPromise !== undefined) {
            playPromise.catch(function() {
              video.addEventListener('loadeddata', function retryPlay() {
                video.removeEventListener('loadeddata', retryPlay);
                video.play().catch(function(){});
              });
            });
          }
        } else {
          video.pause();
        }
      });
    }, { threshold: 0.5 });

    videos.forEach(function(video) {
      if (video.readyState === 0) video.load();
      observer.observe(video);
    });
  }
});
