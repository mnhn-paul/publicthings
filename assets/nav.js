/* publicthings — shared top bar + light/dark toggle.
   Injected on every tool page: one persistent rectangular bar with the
   brand (link back to the overview), the tool navigation, the current
   tool's name and description, and the theme switcher. The page's own
   title block is folded into the bar (hidden via .pt-folded).
   Styles live in assets/theme.css. Existing page scripts are never
   touched — this only adds new elements. */
(function () {
  'use strict';

  /* ----- theme preference (applied immediately to avoid a flash) ----- */
  var KEY = 'pt-theme', LEGACY_KEY = 'mets-theme'; /* legacy key of the METS viewer's own toggle */
  function pref() {
    try {
      var v = localStorage.getItem(KEY) || localStorage.getItem(LEGACY_KEY);
      return v === 'light' || v === 'dark' ? v : 'auto';
    } catch (e) { return 'auto'; }
  }
  function applyTheme() {
    var p = pref(), h = document.documentElement;
    if (p === 'auto') h.removeAttribute('data-theme');
    else h.setAttribute('data-theme', p);
    try { localStorage.setItem(KEY, p); localStorage.setItem(LEGACY_KEY, p); } catch (e) {}
  }
  applyTheme();

  /* ----- repo root, derived from this script's own URL ----- */
  var src = (document.currentScript && document.currentScript.src) || '';
  var root = src.split('assets/nav.js')[0] || './';

  /* The repo's folders are the main menu points; the tools inside each
     folder are the submenu entries of that folder's menu point. */
  var GROUPS = [
    { folder: 'gbif-col', tools: [
      { path: 'gbif-col/gbif IPT lux.html', label: 'GBIF ↔ IPT', desc: 'Datasets hosted on the MNHNL IPT — GBIF registry vs. IPT publication status.' },
      { path: 'gbif-col/mnhnl-checklists-overview.html', label: 'Checklists', desc: 'Checklist datasets published by MNHNL on ChecklistBank — select and download merged taxa tables.' }
    ]},
    { folder: 'data.public', tools: [
      { path: 'data.public/MNHNL_dataPublic_dashboard.html', label: 'Dashboard', desc: 'Insights into the datasets MNHNL publishes on the Luxembourg open data portal.' }
    ]},
    { folder: 'goobi', tools: [
      { path: 'goobi/cungs-goobi-mets-viewer.html', label: 'METS viewer', desc: 'Extracts event notes from METS/MODS files and exports table, DwC Event Core or Excel.' },
      { path: 'goobi/digiento-viewer.html', label: 'Specimen browser', desc: 'Browse boxes and specimens from face.mnhn.gov.lu.' }
    ]}
  ];
  var PAGES = [{ path: 'index.html', label: 'Overview', desc: 'Tools and pages published in this repository via GitHub Pages.' }]
    .concat(GROUPS.reduce(function (acc, g) { return acc.concat(g.tools); }, []));

  /* page title/description blocks that get folded into the top bar */
  var FOLD = {
    'index.html': ['main>h1', 'main>p.sub'],
    'gbif-col/gbif IPT lux.html': ['body>header'],
    'gbif-col/mnhnl-checklists-overview.html': ['body>header'],
    'data.public/MNHNL_dataPublic_dashboard.html': ['body>h1', 'body>.subtitle'],
    'goobi/cungs-goobi-mets-viewer.html': ['main>header.hdr'],
    'goobi/digiento-viewer.html': ['body>header h1']
  };

  var CARET = '<svg class="pt-caret" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>';

  var ORDER = ['auto', 'light', 'dark'];
  var ICONS = {
    auto: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor" stroke="none"/></svg>',
    light: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
    dark: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z"/></svg>'
  };

  /* Match the full repo-relative path (folder + file) against the URL,
     so pages with the same file name in different folders stay distinct. */
  function currentPath() {
    var path = decodeURIComponent(location.pathname).replace(/\/+/g, '/');
    var hit = PAGES.map(function (p) { return p.path; }).filter(function (p) {
      return path === '/' + p || path.slice(-(p.length + 1)) === '/' + p;
    })[0];
    return hit || 'index.html';
  }
  function pageMeta(full) {
    var p = PAGES.filter(function (x) { return x.path === full; })[0] || {};
    var title = (document.title || '').trim() || p.label || 'publicthings';
    var desc = '';
    var m = document.querySelector('meta[name="description"]');
    if (m) desc = (m.getAttribute('content') || '').trim();
    if (!desc) {
      var el = document.querySelector('header .sub, header p, .sub, .subtitle, main>p');
      if (el) desc = el.textContent.replace(/\s+/g, ' ').trim();
    }
    return { title: title, desc: desc || p.desc || '' };
  }

  function build() {
    if (document.querySelector('.pt-nav')) return;
    document.body.classList.add('has-pt-nav');

    var full = currentPath();
    (FOLD[full] || []).forEach(function (sel) {
      document.querySelectorAll(sel).forEach(function (e) { e.classList.add('pt-folded'); });
    });
    var meta = pageMeta(full);

    var nav = document.createElement('nav');
    nav.className = 'pt-nav';
    nav.setAttribute('aria-label', 'publicthings tools');

    var brand = document.createElement('a');
    brand.className = 'pt-brand' + (full === 'index.html' ? ' active' : '');
    brand.href = root + 'index.html';
    brand.title = 'Back to the publicthings overview';
    brand.innerHTML = '<img src="' + root + 'assets/favicon.svg" alt=""><span>publicthings</span>';
    nav.appendChild(brand);

    /* one menu point per folder, with the folder's tools as submenu */
    var links = document.createElement('span');
    links.className = 'pt-links';
    GROUPS.forEach(function (g) {
      var grp = document.createElement('span');
      grp.className = 'pt-group';

      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'pt-link pt-group-btn' + (g.tools.some(function (t) { return t.path === full; }) ? ' active' : '');
      btn.setAttribute('aria-haspopup', 'true');
      btn.setAttribute('aria-expanded', 'false');
      btn.innerHTML = '<span>' + g.folder + '</span>' + CARET;

      var sub = document.createElement('span');
      sub.className = 'pt-sub';
      sub.setAttribute('role', 'menu');
      g.tools.forEach(function (t) {
        var a = document.createElement('a');
        a.className = 'pt-sub-link' + (t.path === full ? ' active' : '');
        a.setAttribute('role', 'menuitem');
        a.href = root + encodeURI(t.path);
        a.textContent = t.label;
        a.title = t.desc;
        sub.appendChild(a);
      });

      btn.addEventListener('click', function () {
        var open = grp.classList.toggle('open');
        btn.setAttribute('aria-expanded', open ? 'true' : 'false');
      });
      btn.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') {
          grp.classList.remove('open');
          btn.setAttribute('aria-expanded', 'false');
        }
      });

      grp.appendChild(btn);
      grp.appendChild(sub);
      links.appendChild(grp);
    });
    document.addEventListener('click', function (e) {
      links.querySelectorAll('.pt-group.open').forEach(function (grp) {
        if (!grp.contains(e.target)) {
          grp.classList.remove('open');
          grp.querySelector('.pt-group-btn').setAttribute('aria-expanded', 'false');
        }
      });
    });
    nav.appendChild(links);

    var cur = document.createElement('span');
    cur.className = 'pt-current';
    var nm = document.createElement('b');
    nm.className = 'pt-name';
    nm.textContent = meta.title;
    var ds = document.createElement('span');
    ds.className = 'pt-desc';
    ds.textContent = meta.desc;
    ds.title = meta.desc;
    cur.appendChild(nm);
    cur.appendChild(ds);
    nav.appendChild(cur);

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

    /* full-bleed bar: cancel whatever horizontal margin/padding the page
       puts on <body> (varies per tool), so the bar always spans the
       whole viewport width; recompute on resize (media queries) */
    function fit() {
      var cs = getComputedStyle(document.body);
      nav.style.marginLeft = 'calc((' + cs.marginLeft + ' + ' + cs.paddingLeft + ') * -1)';
      nav.style.marginRight = 'calc((' + cs.marginRight + ' + ' + cs.paddingRight + ') * -1)';
    }
    fit();
    window.addEventListener('resize', fit);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', build);
  else build();
})();
