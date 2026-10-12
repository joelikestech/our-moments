// ============================================================
//  DYNAMIC HEADER CLEARANCE
//  Measures actual header height and adjusts .main-content margin
//  No breakpoints needed — adapts to any number of button rows.
// ============================================================
(function() {
  'use strict';

  function adjust() {
    var header = document.querySelector('.header-nav');
    var content = document.querySelector('.main-content');
    if (!header || !content) return;

    // Skip if header is in desktop sidebar mode (fixed left sidebar)
    var style = window.getComputedStyle(header);
    if (style.position === 'fixed' && style.width !== 'auto' && parseInt(style.width) < 300) {
      content.style.marginTop = '0px';
      return;
    }

    var h = header.offsetHeight;
    // 10px buffer below header
    content.style.marginTop = (h + 10) + 'px';
  }

  // Run after DOM is ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', adjust);
  } else {
    adjust();
  }

  // Re-adjust on resize and orientation change
  var resizeTimer = null;
  window.addEventListener('resize', function() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(adjust, 100);
  });
  window.addEventListener('orientationchange', function() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(adjust, 300);
  });
})();
