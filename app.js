/* Site visit photo map. Plain JS + Leaflet; data comes from data/photos.json (written by build.py). */
(function () {
  'use strict';

  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  var DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var GROUP_RADIUS_M = 1;
  var WEDGE_PX = 70;      // wedge length on screen
  var WEDGE_HALF = 32;    // half the iPhone main camera's horizontal field of view, in degrees

  var els = {
    title: document.getElementById('title'),
    count: document.getElementById('count'),
    chips: document.getElementById('chips'),
    sort: document.getElementById('sort'),
    gallery: document.getElementById('gallery'),
    grid: document.getElementById('grid'),
    empty: document.getElementById('empty'),
    map: document.getElementById('map')
  };

  var photos = [];
  var site = null;            // {label, address, lat, lon} from config.json
  var students = {};          // name -> {name, color, count}
  var groups = [];
  var selected = new Set();   // student names; empty = all
  var map, wedgeLayer;
  var hovered = null;         // photo under the pointer / keyboard focus
  var selectedGroup = null;   // dot last clicked
  var panTimer = null;

  // ---------------------------------------------------------------- utilities

  function motion() { return !reduceMotion.matches; }

  function parseTaken(s) {
    var m = s && /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)/.exec(s);
    if (!m) return null;
    var y = +m[1], mo = +m[2], d = +m[3];
    // Use the photo's own wall-clock time, not the viewer's timezone.
    var dow = new Date(Date.UTC(y, mo - 1, d)).getUTCDay();
    return { short: DAYS[dow] + ' ' + m[4] + ':' + m[5],
             long: DAYS[dow] + ' ' + d + ' ' + MONTHS[mo - 1] + ' ' + y + ', ' + m[4] + ':' + m[5] };
  }

  function metres(a, b) {
    var k = Math.PI / 180;
    var x = (b.lon - a.lon) * k * Math.cos(((a.lat + b.lat) / 2) * k);
    var y = (b.lat - a.lat) * k;
    return Math.sqrt(x * x + y * y) * 6371000;
  }

  function el(tag, attrs, children) {
    var n = document.createElement(tag);
    for (var k in attrs) {
      if (k === 'text') n.textContent = attrs[k];
      else if (k === 'style') n.style.cssText = attrs[k];
      else n.setAttribute(k, attrs[k]);
    }
    (children || []).forEach(function (c) { if (c) n.appendChild(c); });
    return n;
  }

  function isVisible(p) { return selected.size === 0 || selected.has(p.student); }

  // ---------------------------------------------------------------- data

  fetch('data/photos.json', { cache: 'no-cache' })
    .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(init)
    .catch(function (e) {
      els.empty.hidden = false;
      els.empty.textContent = 'Could not load data/photos.json (' + e.message + ').';
    });

  function init(data) {
    if (data.title) { els.title.textContent = data.title; document.title = data.title; }
    data.students.forEach(function (s) { students[s.name] = s; });
    if (data.site && data.site.lat != null) {
      site = data.site;
      document.getElementById('sort-distance').hidden = false;
    }
    photos = data.photos.map(function (p, i) {
      p.index = i;
      p.color = (students[p.student] || {}).color || '#888';
      p.time = parseTaken(p.taken_at);
      p.ts = p.taken_at ? Date.parse(p.taken_at) : Infinity;
      p.dist = site && p.lat != null ? metres(site, p) : Infinity;
      return p;
    });
    buildGroups();
    buildChips(data.students);
    buildGrid();
    buildMap();
    applySort();
    applyFilter();
    els.sort.addEventListener('change', applySort);
  }

  // Photos by the same student within ~1 m share one dot.
  function buildGroups() {
    var byStudent = {};
    photos.forEach(function (p) {
      if (p.lat == null) return;
      var list = byStudent[p.student] || (byStudent[p.student] = []);
      var g = null;
      for (var i = 0; i < list.length; i++) {
        if (metres(list[i], p) <= GROUP_RADIUS_M) { g = list[i]; break; }
      }
      if (!g) {
        g = { lat: p.lat, lon: p.lon, student: p.student, color: p.color, photos: [] };
        list.push(g);
        groups.push(g);
      }
      g.photos.push(p);
      p.group = g;
    });
  }

  // ---------------------------------------------------------------- header

  function buildChips(list) {
    var all = el('button', { type: 'button', class: 'chip all', 'aria-pressed': 'true', text: 'All' });
    all.addEventListener('click', function () { selected.clear(); applyFilter(); });
    els.chips.appendChild(all);
    list.forEach(function (s) {
      var b = el('button', { type: 'button', class: 'chip', 'aria-pressed': 'false', style: '--c:' + s.color,
                             'data-student': s.name, title: 'Show or hide ' + s.name + '’s photos' }, [
        el('span', { class: 'sw' }),
        el('span', { text: s.name }),
        el('span', { class: 'n', text: String(s.count) })
      ]);
      b.addEventListener('click', function () {
        if (selected.has(s.name)) selected.delete(s.name); else selected.add(s.name);
        if (selected.size === list.length) selected.clear();
        applyFilter();
      });
      els.chips.appendChild(b);
    });
  }

  function applyFilter() {
    els.chips.querySelectorAll('.chip').forEach(function (b) {
      var name = b.getAttribute('data-student');
      b.setAttribute('aria-pressed', String(name ? selected.has(name) : selected.size === 0));
    });
    var shown = 0, noLoc = 0;
    photos.forEach(function (p) {
      var v = isVisible(p);
      p.tile.hidden = !v;
      if (v) { shown++; if (p.lat == null) noLoc++; }
    });
    groups.forEach(function (g) {
      var on = g.photos.some(isVisible);
      var e = g.marker.getElement();
      if (e) e.classList.toggle('off', !on);
      g.marker.setZIndexOffset(on ? 0 : -1000);
    });
    var total = photos.length;
    var txt = (shown === total ? total : shown + ' of ' + total) + ' photo' + (total === 1 ? '' : 's');
    if (noLoc) txt += ' · ' + noLoc + ' without location';
    els.count.textContent = txt;
    els.empty.hidden = total > 0;
  }

  function applySort() {
    var mode = els.sort.value;
    var order = photos.slice().sort(function (a, b) {
      if (mode === 'student' && a.student !== b.student) return a.student.localeCompare(b.student);
      // Distance from site: nearest first; photos with no location go last.
      if (mode === 'distance' && a.dist !== b.dist) return a.dist === Infinity ? 1 : b.dist === Infinity ? -1 : a.dist - b.dist;
      return (a.ts - b.ts) || (a.index - b.index);
    });
    var frag = document.createDocumentFragment();
    order.forEach(function (p) { frag.appendChild(p.tile); });
    els.grid.appendChild(frag);
  }

  // ---------------------------------------------------------------- grid

  function buildGrid() {
    var frag = document.createDocumentFragment();
    photos.forEach(function (p) {
      var ar = (p.width / p.height).toFixed(4);
      var label = p.student + (p.time ? ', ' + p.time.long : '') + ' — ' + p.original_filename;
      var img = el('img', { src: p.thumb, alt: label, loading: 'lazy', decoding: 'async',
                            width: String(p.width), height: String(p.height) });
      var tile = el('a', { class: 'tile', href: p.full, target: '_blank', rel: 'noopener',
                           style: '--ar:' + ar + ';--c:' + p.color, title: label }, [
        el('div', { class: 'frame' }, [img, p.lat == null ? el('span', { class: 'badge', text: 'no location' }) : null]),
        el('div', { class: 'cap' }, [
          el('span', { class: 'sw' }),
          el('span', { class: 'who', text: p.student }),
          p.time ? el('span', { class: 'when', text: p.time.short }) : null
        ])
      ]);
      tile.addEventListener('mouseenter', function () { hoverPhoto(p); });
      tile.addEventListener('mouseleave', function () { unhoverPhoto(p); });
      tile.addEventListener('focus', function () { hoverPhoto(p); });
      tile.addEventListener('blur', function () { unhoverPhoto(p); });
      p.tile = tile;
      frag.appendChild(tile);
    });
    els.grid.appendChild(frag);
  }

  function flash(tiles) {
    tiles.forEach(function (t) {
      t.classList.remove('flash');
      void t.offsetWidth; // restart the animation
      t.classList.add('flash');
      clearTimeout(t._flashTimer);
      t._flashTimer = setTimeout(function () { t.classList.remove('flash'); }, 2100);
    });
  }

  // ---------------------------------------------------------------- map

  function buildMap() {
    var street = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 21, maxNativeZoom: 19, opacity: 0.5,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
    });
    var satellite = L.tileLayer(
      'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
        maxZoom: 21, maxNativeZoom: 19, opacity: 0.5,
        attribution: 'Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community'
      });
    map = L.map(els.map, { layers: [street], zoomSnap: 0.25, zoomControl: true, maxZoom: 21 });
    L.control.layers({ Street: street, Satellite: satellite }, null, { collapsed: window.innerWidth <= 800 }).addTo(map);
    L.control.scale({ position: 'bottomleft' }).addTo(map);
    wedgeLayer = L.layerGroup().addTo(map);

    if (site) {
      L.marker([site.lat, site.lon], {
        icon: L.divIcon({ className: 'site-wrap', html: '<span class="site"></span>', iconSize: [14, 14] }),
        keyboard: false, interactive: true, zIndexOffset: -2000, title: site.label || 'Site'
      }).bindTooltip((site.label || 'Site') + (site.address ? '<br><span class="addr">' + site.address + '</span>' : ''),
                     { direction: 'top', offset: [0, -9] }).addTo(map);
    }

    groups.forEach(function (g) {
      var n = g.photos.length;
      var size = n > 1 ? 16 : 12;
      var label = g.student + ' — ' + n + ' photo' + (n > 1 ? 's' : '');
      g.marker = L.marker([g.lat, g.lon], {
        icon: L.divIcon({
          className: 'dot-wrap',
          html: '<span class="dot" style="--c:' + g.color + '">' + (n > 1 ? '<b class="n">' + n + '</b>' : '') + '</span>',
          iconSize: [size, size]
        }),
        keyboard: true, title: label, alt: label
      }).addTo(map);
      g.marker.bindTooltip(label, { direction: 'top', offset: [0, -size / 2 - 2] });
      g.marker.on('click', function () { clickGroup(g); });
    });

    fitAll();
    // If the page loaded in a hidden tab the map had no size, so fit again once it does.
    var fitted = els.map.clientWidth > 0 && els.map.clientHeight > 0;
    map.on('click', function () { selectGroup(null); });
    map.on('zoomend', drawWedges);
    if (window.ResizeObserver) new ResizeObserver(function () {
      map.invalidateSize();
      if (!fitted && els.map.clientWidth > 0 && els.map.clientHeight > 0) { fitted = true; fitAll(); }
    }).observe(els.map);
  }

  // Fit to the site, ignoring stray photos taken far away (they still get dots).
  function fitAll() {
    var located = photos.filter(function (p) { return p.lat != null; });
    var median = function (xs) { xs = xs.slice().sort(function (a, b) { return a - b; }); return xs[xs.length >> 1]; };
    if (located.length) {
      var mid = { lat: median(located.map(function (p) { return p.lat; })),
                  lon: median(located.map(function (p) { return p.lon; })) };
      var near = located.filter(function (p) { return metres(mid, p) <= 3000; });
      if (near.length) located = near;
      var b = L.latLngBounds(located.map(function (p) { return [p.lat, p.lon]; }));
      map.fitBounds(b, { padding: [30, 30], maxZoom: 19, animate: false });
    } else {
      map.setView([42.333, -71.105], 16);
    }
  }

  function markerEl(g) { return g && g.marker && g.marker.getElement(); }

  function hoverPhoto(p) {
    hovered = p;
    p.tile.classList.add('hover');
    if (!p.group) return;           // no location: nothing on the map
    var e = markerEl(p.group);
    els.map.classList.add('dim');
    if (e) e.classList.add('hl');
    p.group.marker.setZIndexOffset(10000);
    drawWedges();
    clearTimeout(panTimer);
    panTimer = setTimeout(function () {
      if (hovered !== p) return;
      var ll = p.group.marker.getLatLng();
      if (!map.getBounds().pad(-0.08).contains(ll)) {
        map.panTo(ll, { animate: motion(), duration: 0.6, easeLinearity: 0.4 });
      }
    }, 120);
  }

  function unhoverPhoto(p) {
    p.tile.classList.remove('hover');
    if (hovered === p) hovered = null;
    if (!p.group) return;
    var e = markerEl(p.group);
    if (e) e.classList.remove('hl');
    p.group.marker.setZIndexOffset(p.group.photos.some(isVisible) ? 0 : -1000);
    if (!hovered || !hovered.group) els.map.classList.remove('dim');
    drawWedges();
  }

  function clickGroup(g) {
    if (!g.photos.every(isVisible)) { selected.clear(); applyFilter(); }
    var tiles = g.photos.map(function (p) { return p.tile; });
    tiles.sort(function (a, b) {
      return a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
    });
    selectGroup(g);
    var view = els.gallery.getBoundingClientRect(), r = tiles[0].getBoundingClientRect();
    var inView = r.top >= view.top && r.bottom <= view.bottom;
    tiles[0].scrollIntoView({ block: 'center', inline: 'nearest', behavior: motion() ? 'smooth' : 'auto' });
    if (!motion() || inView) { flash(tiles); return; }
    // Smooth scroll: start the 2 s highlight when the photo arrives, not while it's still off-screen.
    var done = false;
    var go = function () {
      if (done) return;
      done = true;
      els.gallery.removeEventListener('scrollend', go);
      flash(tiles);
    };
    els.gallery.addEventListener('scrollend', go);
    setTimeout(go, 1500);
  }

  function selectGroup(g) {
    var prev = markerEl(selectedGroup);
    if (prev) prev.classList.remove('sel');
    selectedGroup = g;
    var e = markerEl(g);
    if (e) e.classList.add('sel');
    drawWedges();
  }

  // Faint view-direction wedge(s) for the hovered photo, else the selected dot's photos.
  function drawWedges() {
    wedgeLayer.clearLayers();
    var list = hovered && hovered.group ? [hovered] : selectedGroup ? selectedGroup.photos : [];
    list.forEach(function (p) {
      if (p.bearing_deg == null || p.lat == null) return;
      var mPerPx = 40075016.686 * Math.cos(p.lat * Math.PI / 180) / Math.pow(2, map.getZoom() + 8);
      var r = WEDGE_PX * mPerPx;
      var pts = [[p.lat, p.lon]];
      for (var a = -WEDGE_HALF; a <= WEDGE_HALF; a += 4) {
        var brg = (p.bearing_deg + a) * Math.PI / 180;
        var dn = r * Math.cos(brg), de = r * Math.sin(brg);
        pts.push([p.lat + dn / 111320, p.lon + de / (111320 * Math.cos(p.lat * Math.PI / 180))]);
      }
      L.polygon(pts, { color: p.color, weight: 1, opacity: 0.6, fillColor: p.color, fillOpacity: 0.18,
                       interactive: false }).addTo(wedgeLayer);
    });
  }
})();
