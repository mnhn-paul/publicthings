/* publicthings — shared top navigation + light/dark toggle.
   Injected on every tool page; adds a .pt-nav bar as first element
   of <body> with a link back to the overview page and to each tool.
   Styles live in assets/theme.css. Pages keep working unchanged:
   this script only adds new elements, it never touches existing ones. */
(function () {
  'use strict';

  /* ----- theme preference (applied immediately to avoid a flash) ----- */
  var KEY = 'pt-theme';
  function pref() {
    try {
      var v = localStorage.getItem(KEY);
      return v === 'light' || v === 'dark' ? v : 'auto';
    } catch (e) { return 'auto'; }
  }
  function applyTheme() {
    var p = pref(), h = document.documentElement;
    if (p === 'auto') h.removeAttribute('data-theme');
    else h.setAttribute('data-theme', p);
  }
  applyTheme();

  /* ----- repo root, derived from this script's own URL ----- */
  var src = (document.currentScript && document.currentScript.src) || '';
  var root = src.split('assets/nav.js')[0] || './';

  var PAGES = [
    { path: 'index.html', label: 'Overview' },
    { path: 'gbif IPT lux.html', label: 'GBIF ↔ IPT' },
    { path: 'mnhnl-checklists-overview.html', label: 'Checklists' },
    { path: 'data_public/MNHNL_dataPublic_dashboard.html', label: 'data.public' },
    { path: 'goobi/Cungs-goobi-mets-viewer.html', label: 'METS viewer' },
    { path: 'goobi/Digiento-viewer.html', label: 'Specimen browser' }
  ];

  var ORDER = ['auto', 'light', 'dark'];
  var ICONS = {
    auto: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor" stroke="none"/></svg>',
    light: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
    dark: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z"/></svg>'
  };

  function currentFile() {
    var seg = decodeURIComponent(location.pathname.split('/').filter(Boolean).pop() || 'index.html');
    return /\.html?$/i.test(seg) ? seg : 'index.html';
  }

  function build() {
    if (document.querySelector('.pt-nav')) return;
    var here = currentFile();

    var nav = document.createElement('nav');
    nav.className = 'pt-nav';
    nav.setAttribute('aria-label', 'publicthings tools');

    var brand = document.createElement('a');
    brand.className = 'pt-brand' + (here === 'index.html' ? ' active' : '');
    brand.href = root + 'index.html';
    brand.title = 'Back to the publicthings overview';
    brand.innerHTML = '<img src="' + root + 'assets/favicon.svg" alt=""><span>publicthings</span>';
    nav.appendChild(brand);

    PAGES.forEach(function (p) {
      if (p.path === 'index.html') return; /* overview = brand link */
      var a = document.createElement('a');
      a.className = 'pt-link' + (here === p.path.split('/').pop() ? ' active' : '');
      a.href = root + encodeURI(p.path);
      a.textContent = p.label;
      nav.appendChild(a);
    });

    var spacer = document.createElement('span');
    spacer.className = 'pt-spacer';
    nav.appendChild(spacer);

    var btn = document.createElement('button');
    btn.className = 'pt-theme';
    btn.type = 'button';
    function renderBtn() {
      var p = pref();
      btn.innerHTML = ICONS[p] + '<span>' + p + '</span>';
      btn.title = 'Colour theme: ' + p + ' — click to switch (auto / light / dark)';
      btn.setAttribute('aria-label', btn.title);
    }
    btn.addEventListener('click', function () {
      var next = ORDER[(ORDER.indexOf(pref()) + 1) % ORDER.length];
      try { localStorage.setItem(KEY, next); } catch (e) {}
      applyTheme();
      renderBtn();
    });
    renderBtn();
    nav.appendChild(btn);

    document.body.insertBefore(nav, document.body.firstChild);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', build);
  else build();
})();
