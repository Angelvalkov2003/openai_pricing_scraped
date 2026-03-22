"""
OpenAI API pricing scraper (v4) for developers.openai.com.

Uses each **Content switcher** in document order (``aria-label="Content switcher"``,
``data-value`` on visible buttons — no fixed tier list). Tables are mapped to the
nearest preceding switcher; when several precede a table, the one with the **fewest**
options is preferred if it has at most two. Sparse ranges between adjacent
switchers extend ``owned`` sets for two-option strips. Merge uses
``(section_heading, headers)`` as key so tier clicks are not lost when
``table_index`` shifts. Every snapshot is merged under its tier key even when
row bodies match another tier (e.g. Flex and Batch). After the switcher pass, a
**tablist** pass (when present) is merged in. A final pass finds each table's
closest preceding **Content switcher**, reads every visible ``data-value``,
clicks each in DOM order, and merges rows under that tier key.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException

DEFAULT_PRICING_URL = (
    "https://developers.openai.com/api/docs/pricing"
    "?latest-pricing=batch&ft-pricing=standard"
)


class OpenAIScraper4:
    WAIT_TIMEOUT = 30
    TIER_CLICK_PAUSE = 1.15
    # Skip local switcher supplement when this many tables share one closest
    # switcher (global bar); main ``_gather_with_switchers`` already covers it.
    LOCAL_CONTENT_STRIP_MAX_TABLES = 6

    _DISCOVER_SWITCHER_LAYOUT_JS = r"""
    return JSON.stringify((function() {
        function isNestedTable(t) {
            var p = t.parentElement;
            while (p) {
                if (p.tagName === 'TABLE' && p !== t) return true;
                p = p.parentElement;
            }
            return false;
        }
        function btnVisible(b) {
            if (!b) return false;
            var r = b.getBoundingClientRect();
            if (r.width < 1.5 || r.height < 1.5) return false;
            var st = window.getComputedStyle(b);
            if (st.display === 'none' || st.visibility === 'hidden') return false;
            if (parseFloat(st.opacity || '1') < 0.05) return false;
            return true;
        }
        var main = document.querySelector('main');
        if (!main) return { switchers: [], tableOwners: [] };
        var switcherEls = Array.prototype.slice.call(
            main.querySelectorAll('[aria-label="Content switcher"]')
        );
        var switchers = switcherEls.map(function(el, idx) {
            var opts = el.querySelectorAll('button[data-content-switcher-option][data-value]');
            var values = [];
            var seen = {};
            var options_detail = [];
            for (var i = 0; i < opts.length; i++) {
                var b = opts[i];
                if (!btnVisible(b)) continue;
                var v = (b.getAttribute('data-value') || '').trim().toLowerCase();
                if (!v) continue;
                var dis = !!(b.disabled || b.getAttribute('aria-disabled') === 'true');
                options_detail.push({ value: v, disabled: dis });
                if (!seen[v]) { seen[v] = 1; values.push(v); }
            }
            return { index: idx, values: values, options_detail: options_detail };
        });
        var topTables = [];
        Array.prototype.forEach.call(main.querySelectorAll('table'), function(tbl) {
            if (!isNestedTable(tbl)) topTables.push(tbl);
        });
        var owners = [];
        var cur = -1;
        var tw = document.createTreeWalker(main, NodeFilter.SHOW_ELEMENT, null, false);
        var n = tw.currentNode;
        while (n) {
            if (n.tagName === 'H2') {
                cur = -1;
            } else if (n.getAttribute && n.getAttribute('aria-label') === 'Content switcher') {
                var ix = switcherEls.indexOf(n);
                cur = ix >= 0 ? ix : -1;
            } else if (n.tagName === 'TABLE' && !isNestedTable(n)) {
                owners.push(cur);
            }
            n = tw.nextNode();
        }
        if (owners.length !== topTables.length) {
            owners = topTables.map(function() { return -1; });
        } else {
            for (var ti = 0; ti < owners.length; ti++) {
                if (owners[ti] >= 0) continue;
                var tbl = topTables[ti];
                var prevTbl = ti > 0 ? topTables[ti - 1] : null;
                var nextTbl = ti + 1 < topTables.length ? topTables[ti + 1] : null;
                var bestPre = -1;
                var s;
                for (s = 0; s < switcherEls.length; s++) {
                    var sw = switcherEls[s];
                    if (!(sw.compareDocumentPosition(tbl) & Node.DOCUMENT_POSITION_FOLLOWING))
                        continue;
                    if (prevTbl && !(prevTbl.compareDocumentPosition(sw) & Node.DOCUMENT_POSITION_FOLLOWING))
                        continue;
                    bestPre = s;
                }
                if (bestPre >= 0) {
                    owners[ti] = bestPre;
                    continue;
                }
                for (s = 0; s < switcherEls.length; s++) {
                    var sw2 = switcherEls[s];
                    if (!(tbl.compareDocumentPosition(sw2) & Node.DOCUMENT_POSITION_FOLLOWING))
                        continue;
                    if (nextTbl && !(sw2.compareDocumentPosition(nextTbl) & Node.DOCUMENT_POSITION_FOLLOWING))
                        continue;
                    owners[ti] = s;
                    break;
                }
            }
        }
        for (var ti2 = 0; ti2 < owners.length; ti2++) {
            var tblZ = topTables[ti2];
            var prevZ = ti2 > 0 ? topTables[ti2 - 1] : null;
            var cand = [];
            for (var s2 = 0; s2 < switcherEls.length; s2++) {
                var swZ = switcherEls[s2];
                if (!(swZ.compareDocumentPosition(tblZ) & Node.DOCUMENT_POSITION_FOLLOWING))
                    continue;
                if (prevZ && !(prevZ.compareDocumentPosition(swZ) & Node.DOCUMENT_POSITION_FOLLOWING))
                    continue;
                cand.push(s2);
            }
            if (cand.length === 0) continue;
            if (cand.length === 1) {
                owners[ti2] = cand[0];
                continue;
            }
            var small = cand.filter(function (cx) {
                return ((switchers[cx].values || []).length <= 2);
            });
            var pool = small.length ? small : cand;
            var bestS = pool[0];
            var bestLen = (switchers[bestS].values || []).length || 999;
            for (var ci = 1; ci < pool.length; ci++) {
                var cx = pool[ci];
                var lx = (switchers[cx].values || []).length || 999;
                if (lx < bestLen) {
                    bestS = cx;
                    bestLen = lx;
                }
            }
            owners[ti2] = bestS;
        }
        var sparseOwnedTablesBySwitcher = {};
        for (var os = 0; os < switcherEls.length; os++) {
            var nOpt = (switchers[os].values || []).length;
            if (nOpt > 2) continue;
            if (os + 1 >= switcherEls.length) {
                sparseOwnedTablesBySwitcher[String(os)] = [];
                continue;
            }
            var swB = switcherEls[os];
            var nextB = switcherEls[os + 1];
            var lst = [];
            for (var tib = 0; tib < topTables.length; tib++) {
                var tbB = topTables[tib];
                if (!(swB.compareDocumentPosition(tbB) & Node.DOCUMENT_POSITION_FOLLOWING))
                    continue;
                if (!(tbB.compareDocumentPosition(nextB) & Node.DOCUMENT_POSITION_FOLLOWING))
                    continue;
                lst.push(tib + 1);
            }
            sparseOwnedTablesBySwitcher[String(os)] = lst;
        }
        return {
            switchers: switchers,
            tableOwners: owners,
            sparseOwnedTablesBySwitcher: sparseOwnedTablesBySwitcher
        };
    })());
    """

    _CLICK_SWITCHER_OPTION_JS = r"""
    var main = document.querySelector('main');
    if (!main) return false;
    function btnVisible(b) {
        if (!b) return false;
        var r = b.getBoundingClientRect();
        if (r.width < 1.5 || r.height < 1.5) return false;
        var st = window.getComputedStyle(b);
        if (st.display === 'none' || st.visibility === 'hidden') return false;
        if (parseFloat(st.opacity || '1') < 0.05) return false;
        return true;
    }
    var list = main.querySelectorAll('[aria-label="Content switcher"]');
    var si = arguments[0];
    var val = (arguments[1] || '').trim().toLowerCase();
    if (si < 0 || si >= list.length) return false;
    var sw = list[si];
    var btns = sw.querySelectorAll('button[data-content-switcher-option][data-value]');
    var matches = [];
    for (var i = 0; i < btns.length; i++) {
        var b = btns[i];
        if ((b.getAttribute('data-value') || '').trim().toLowerCase() !== val) continue;
        if (b.disabled || b.getAttribute('aria-disabled') === 'true') continue;
        matches.push(b);
    }
    for (var j = 0; j < matches.length; j++) {
        if (btnVisible(matches[j])) {
            matches[j].click();
            return true;
        }
    }
    if (matches.length) {
        matches[0].click();
        return true;
    }
    return false;
    """

    _VISIBLE_CONTENT_SWITCHER_VALUES_JS = r"""
    return JSON.stringify((function() {
        var main = document.querySelector('main');
        if (!main) return [];
        var si = arguments[0];
        var list = main.querySelectorAll('[aria-label="Content switcher"]');
        if (si < 0 || si >= list.length) return [];
        var sw = list[si];
        var opts = sw.querySelectorAll('button[data-content-switcher-option][data-value]');
        var values = [];
        var seen = {};
        for (var i = 0; i < opts.length; i++) {
            var b = opts[i];
            if (b.disabled || b.getAttribute('aria-disabled') === 'true') continue;
            var st = window.getComputedStyle(b);
            if (st.display === 'none' || st.visibility === 'hidden') continue;
            var v = (b.getAttribute('data-value') || '').trim().toLowerCase();
            if (!v || seen[v]) continue;
            seen[v] = 1;
            values.push(v);
        }
        return values;
    })());
    """

    _DISCOVER_LOCAL_CONTENT_SWITCHER_STRIPS_JS = r"""
    return JSON.stringify((function() {
        function cellText(c) {
            if (!c) return '';
            return (c.innerText || '').trim().replace(/\s+/g, ' ');
        }
        function isNestedTable(t) {
            var p = t.parentElement;
            while (p) {
                if (p.tagName === 'TABLE' && p !== t) return true;
                p = p.parentElement;
            }
            return false;
        }
        function nearestHeading(el) {
            var cur = el;
            while (cur) {
                var sib = cur.previousElementSibling;
                while (sib) {
                    var tag = sib.tagName;
                    if (tag === 'H2' || tag === 'H3' || tag === 'H4' || tag === 'H5')
                        return cellText(sib);
                    var h = sib.querySelector('h2,h3,h4,h5');
                    if (h) return cellText(h);
                    sib = sib.previousElementSibling;
                }
                cur = cur.parentElement;
            }
            return '';
        }
        function colspanOf(cell) {
            var c = parseInt(cell.getAttribute('colspan') || '1', 10);
            return isNaN(c) || c < 1 ? 1 : c;
        }
        function expandHeaderRow(tr) {
            var out = [];
            Array.prototype.forEach.call(tr.querySelectorAll('th,td'), function(cell) {
                var t = cellText(cell);
                var cs = colspanOf(cell);
                for (var i = 0; i < cs; i++) out.push(t);
            });
            return out;
        }
        function headersFromThead(thead) {
            var trs = thead.querySelectorAll('tr');
            if (!trs.length) return [];
            if (trs.length === 1) return expandHeaderRow(trs[0]);
            var rows = Array.prototype.map.call(trs, expandHeaderRow);
            var w = 0;
            rows.forEach(function(r) { w = Math.max(w, r.length); });
            for (var i = 0; i < rows.length; i++) {
                while (rows[i].length < w) rows[i].push('');
            }
            var headers = [];
            for (var c = 0; c < w; c++) {
                var parts = [];
                for (var r = 0; r < rows.length; r++) {
                    var t = (rows[r][c] || '').trim();
                    if (!t) continue;
                    if (parts.length && parts[parts.length - 1] === t) continue;
                    parts.push(t);
                }
                headers.push(parts.length ? parts.join(' | ') : ('col_' + c));
            }
            return headers;
        }
        function dedupeKeys(keys) {
            var seen = {};
            return keys.map(function(k, i) {
                var base = k || ('col_' + i);
                if (!seen[base]) {
                    seen[base] = 1;
                    return base;
                }
                seen[base] += 1;
                return base + ' (' + seen[base] + ')';
            });
        }
        function tableHeaders(tbl) {
            var headers = [];
            var thead = tbl.querySelector('thead');
            if (thead) headers = dedupeKeys(headersFromThead(thead));
            var bodyRows = [];
            var bodies = tbl.tBodies && tbl.tBodies.length
                ? Array.from(tbl.tBodies)
                : [tbl];
            bodies.forEach(function(tb) {
                Array.prototype.forEach.call(tb.querySelectorAll('tr'), function(tr) {
                    if (tr.closest('thead')) return;
                    var cells = Array.from(tr.querySelectorAll('th,td')).map(cellText);
                    if (cells.length) bodyRows.push(cells);
                });
            });
            if (!headers.length && bodyRows.length) {
                headers = dedupeKeys(bodyRows[0].slice());
            }
            return headers;
        }
        var main = document.querySelector('main');
        if (!main) return [];
        var switchers = Array.prototype.slice.call(
            main.querySelectorAll('[aria-label="Content switcher"]')
        );
        var topTables = [];
        Array.prototype.forEach.call(main.querySelectorAll('table'), function(tbl) {
            if (!isNestedTable(tbl)) topTables.push(tbl);
        });
        var bySw = {};
        for (var ti = 0; ti < topTables.length; ti++) {
            var tbl = topTables[ti];
            var swIdx = -1;
            for (var si = switchers.length - 1; si >= 0; si--) {
                if (!(switchers[si].compareDocumentPosition(tbl) & Node.DOCUMENT_POSITION_FOLLOWING))
                    continue;
                swIdx = si;
                break;
            }
            if (swIdx < 0) continue;
            var sw = switchers[swIdx];
            var opts = sw.querySelectorAll('button[data-content-switcher-option][data-value]');
            var vals = [];
            for (var oi = 0; oi < opts.length; oi++) {
                var b = opts[oi];
                if (b.disabled || b.getAttribute('aria-disabled') === 'true') continue;
                var stb = window.getComputedStyle(b);
                if (stb.display === 'none' || stb.visibility === 'hidden') continue;
                var dv = (b.getAttribute('data-value') || '').trim().toLowerCase();
                if (!dv) continue;
                vals.push(dv);
            }
            if (vals.length < 2) continue;
            var sec = nearestHeading(tbl);
            var hdrs = tableHeaders(tbl);
            var key = swIdx + '';
            if (!bySw[key]) {
                bySw[key] = {
                    switcher_index: swIdx,
                    visible_values: vals,
                    tables: []
                };
            }
            var sig = sec + '\0' + hdrs.join('\1');
            var dup = false;
            for (var j = 0; j < bySw[key].tables.length; j++) {
                var tj = bySw[key].tables[j];
                if (tj.section_heading === sec && tj.headers.length === hdrs.length) {
                    var same = true;
                    for (var c = 0; c < hdrs.length; c++) {
                        if (tj.headers[c] !== hdrs[c]) { same = false; break; }
                    }
                    if (same) { dup = true; break; }
                }
            }
            if (!dup) {
                bySw[key].tables.push({ section_heading: sec, headers: hdrs });
            }
        }
        return Object.keys(bySw).map(function(k) { return bySw[k]; });
    })());
    """

    _DISCOVER_TABLIST_LAYOUT_JS = r"""
    return JSON.stringify((function() {
        function isNestedTable(t) {
            var p = t.parentElement;
            while (p) {
                if (p.tagName === 'TABLE' && p !== t) return true;
                p = p.parentElement;
            }
            return false;
        }
        var main = document.querySelector('main');
        if (!main) return { tablists: [], tableOwners: [] };
        var tablists = Array.prototype.slice.call(main.querySelectorAll('[role="tablist"]'));
        var tablistMeta = tablists.map(function(tl, idx) {
            var tabs = tl.querySelectorAll('[role="tab"]');
            var labels = [];
            var options_detail = [];
            for (var i = 0; i < tabs.length; i++) {
                var t = tabs[i];
                var lab = (t.textContent || '').replace(/\s+/g, ' ').trim();
                if (!lab) continue;
                labels.push(lab);
                var dis = !!(t.disabled || t.getAttribute('aria-disabled') === 'true');
                options_detail.push({ label: lab, disabled: dis });
            }
            return { index: idx, labels: labels, options_detail: options_detail };
        });
        var topTables = [];
        Array.prototype.forEach.call(main.querySelectorAll('table'), function(tbl) {
            if (!isNestedTable(tbl)) topTables.push(tbl);
        });
        var owners = [];
        var currentTl = -1;
        var tw = document.createTreeWalker(main, NodeFilter.SHOW_ELEMENT, null, false);
        var n = tw.currentNode;
        while (n) {
            if (n.tagName === 'H2') {
                currentTl = -1;
            } else if (n.getAttribute && n.getAttribute('role') === 'tablist') {
                var ix = tablists.indexOf(n);
                currentTl = ix >= 0 ? ix : -1;
            } else if (n.tagName === 'TABLE' && !isNestedTable(n)) {
                owners.push(currentTl);
            }
            n = tw.nextNode();
        }
        if (owners.length !== topTables.length) {
            owners = topTables.map(function() { return -1; });
        }
        return { tablists: tablistMeta, tableOwners: owners };
    })());
    """

    _CLICK_TAB_IN_TABLIST_JS = r"""
    var tls = document.querySelectorAll('main [role="tablist"]');
    var idx = arguments[0];
    var label = (arguments[1] || '').trim().replace(/\s+/g, ' ');
    if (idx < 0 || idx >= tls.length) return false;
    var tl = tls[idx];
    var tabs = tl.querySelectorAll('[role="tab"]');
    for (var i = 0; i < tabs.length; i++) {
        var t = (tabs[i].textContent || '').replace(/\s+/g, ' ').trim();
        if (t === label) {
            tabs[i].click();
            return true;
        }
    }
    return false;
    """

    _EXTRACT_HTML_TABLES_JS = r"""
    return JSON.stringify((function() {
        function cellText(c) {
            if (!c) return '';
            return (c.innerText || '').trim().replace(/\s+/g, ' ');
        }
        function isNestedTable(t) {
            var p = t.parentElement;
            while (p) {
                if (p.tagName === 'TABLE' && p !== t) return true;
                p = p.parentElement;
            }
            return false;
        }
        function nearestHeading(el) {
            var cur = el;
            while (cur) {
                var sib = cur.previousElementSibling;
                while (sib) {
                    var tag = sib.tagName;
                    if (tag === 'H2' || tag === 'H3' || tag === 'H4' || tag === 'H5')
                        return cellText(sib);
                    var h = sib.querySelector('h2,h3,h4,h5');
                    if (h) return cellText(h);
                    sib = sib.previousElementSibling;
                }
                cur = cur.parentElement;
            }
            return '';
        }
        function colspanOf(cell) {
            var c = parseInt(cell.getAttribute('colspan') || '1', 10);
            return isNaN(c) || c < 1 ? 1 : c;
        }
        function expandHeaderRow(tr) {
            var out = [];
            Array.prototype.forEach.call(tr.querySelectorAll('th,td'), function(cell) {
                var t = cellText(cell);
                var cs = colspanOf(cell);
                for (var i = 0; i < cs; i++) out.push(t);
            });
            return out;
        }
        function headersFromThead(thead) {
            var trs = thead.querySelectorAll('tr');
            if (!trs.length) return [];
            if (trs.length === 1) return expandHeaderRow(trs[0]);
            var rows = Array.prototype.map.call(trs, expandHeaderRow);
            var w = 0;
            rows.forEach(function(r) { w = Math.max(w, r.length); });
            for (var i = 0; i < rows.length; i++) {
                while (rows[i].length < w) rows[i].push('');
            }
            var headers = [];
            for (var c = 0; c < w; c++) {
                var parts = [];
                for (var r = 0; r < rows.length; r++) {
                    var t = (rows[r][c] || '').trim();
                    if (!t) continue;
                    if (parts.length && parts[parts.length - 1] === t) continue;
                    parts.push(t);
                }
                headers.push(parts.length ? parts.join(' | ') : ('col_' + c));
            }
            return headers;
        }
        function dedupeKeys(keys) {
            var seen = {};
            return keys.map(function(k, i) {
                var base = k || ('col_' + i);
                if (!seen[base]) {
                    seen[base] = 1;
                    return base;
                }
                seen[base] += 1;
                return base + ' (' + seen[base] + ')';
            });
        }
        function isVisible(el) {
            if (!el) return false;
            var r = el.getBoundingClientRect();
            if (r.width < 4 || r.height < 4) return false;
            var st = window.getComputedStyle(el);
            if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity || '1') < 0.05)
                return false;
            var p = el.parentElement;
            while (p && p !== document.body) {
                var ps = window.getComputedStyle(p);
                if (ps.display === 'none' || ps.visibility === 'hidden') return false;
                p = p.parentElement;
            }
            return true;
        }
        var root = document.querySelector('main') || document.body;
        var tables = root.querySelectorAll('table');
        var out = [];
        var idx = 0;
        Array.prototype.forEach.call(tables, function(tbl) {
            if (isNestedTable(tbl)) return;
            if (!isVisible(tbl)) return;
            idx += 1;
            var section = nearestHeading(tbl);
            var headers = [];
            var thead = tbl.querySelector('thead');
            if (thead) {
                headers = headersFromThead(thead);
            }
            headers = dedupeKeys(headers);
            var bodyRows = [];
            var bodies = tbl.tBodies && tbl.tBodies.length
                ? Array.from(tbl.tBodies)
                : [tbl];
            bodies.forEach(function(tb) {
                Array.prototype.forEach.call(tb.querySelectorAll('tr'), function(tr) {
                    if (tr.closest('thead')) return;
                    var cells = Array.from(tr.querySelectorAll('th,td')).map(cellText);
                    if (cells.length) bodyRows.push(cells);
                });
            });
            if (!headers.length && bodyRows.length) {
                headers = bodyRows[0].slice();
                bodyRows = bodyRows.slice(1);
            }
            var objects = [];
            bodyRows.forEach(function(cells) {
                if (!cells.length) return;
                var row = {};
                for (var i = 0; i < headers.length; i++) {
                    var key = headers[i] || ('col_' + i);
                    row[key] = i < cells.length ? cells[i] : '';
                }
                objects.push(row);
            });
            out.push({
                table_index: idx,
                section_heading: section,
                headers: headers,
                rows: objects,
                row_count: objects.length
            });
        });
        return out;
    })());
    """

    def __init__(
        self,
        pricing_url: str = DEFAULT_PRICING_URL,
        *,
        headless: bool = False,
        max_expand_rounds: int = 25,
        use_tablist_fallback: bool = True,
    ) -> None:
        self.pricing_url = pricing_url
        self.headless = headless
        self.max_expand_rounds = max_expand_rounds
        self.use_tablist_fallback = use_tablist_fallback
        self.driver: Optional[webdriver.Chrome] = None
        self.pricing_data: Dict[str, Any] = {}

    def _setup_driver(self) -> None:
        opts = webdriver.ChromeOptions()
        if self.headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option(
            "prefs",
            {
                "profile.default_content_setting_values.clipboard": 1,
                "profile.content_settings.exceptions.clipboard": {
                    "[*.]openai.com,*": {"setting": 1}
                },
            },
        )
        self.driver = webdriver.Chrome(options=opts)

    def _wait_for_main(self) -> None:
        assert self.driver
        wait = WebDriverWait(self.driver, self.WAIT_TIMEOUT)
        for locator in (
            (By.CSS_SELECTOR, "main"),
            (By.TAG_NAME, "main"),
        ):
            try:
                wait.until(EC.presence_of_element_located(locator))
                return
            except TimeoutException:
                continue

    def _scroll_main(self) -> None:
        if not self.driver:
            return
        try:
            self.driver.execute_script(
                "var m=document.querySelector('main')||document.body;"
                "if(!m)return;var step=Math.max(400,innerHeight*0.85);"
                "for(var i=0;i<55;i++){m.scrollTop=Math.min(m.scrollHeight,m.scrollTop+step);}"
                "m.scrollTop=0;"
            )
        except Exception:
            pass

    def _expand_all_models_buttons(self) -> int:
        if not self.driver:
            return 0
        xpath = (
            "//main//button[contains(normalize-space(.), 'All models') "
            "and not(contains(normalize-space(.), 'Fewer'))]"
        )
        clicked = 0
        try:
            buttons = self.driver.find_elements(By.XPATH, xpath)
        except Exception:
            return 0
        for btn in buttons:
            try:
                if not btn.is_displayed() or not btn.is_enabled():
                    continue
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center',inline:'nearest'});",
                    btn,
                )
                time.sleep(0.08)
                self.driver.execute_script("arguments[0].click();", btn)
                clicked += 1
                time.sleep(0.35)
            except Exception:
                continue
        return clicked

    def _expand_all_models_until_stable(self) -> None:
        for _ in range(self.max_expand_rounds):
            if self._expand_all_models_buttons() == 0:
                break
            time.sleep(0.25)

    def _expand_after_tier_change(self) -> None:
        if not self.driver:
            return
        time.sleep(0.45)
        for _ in range(3):
            self._scroll_main()
            time.sleep(0.28)
            self._expand_all_models_until_stable()
            try:
                tables = self.driver.find_elements(By.CSS_SELECTOR, "main table")
            except Exception:
                tables = []
            for tbl in tables:
                try:
                    if not tbl.is_displayed():
                        continue
                    self.driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center',inline:'nearest'});",
                        tbl,
                    )
                    time.sleep(0.18)
                    self._expand_all_models_until_stable()
                except Exception:
                    continue
            time.sleep(0.3)
        self._expand_all_models_until_stable()
        time.sleep(0.55)

    def _discover_switcher_layout(self) -> Dict[str, Any]:
        assert self.driver
        raw = self.driver.execute_script(self._DISCOVER_SWITCHER_LAYOUT_JS)
        if isinstance(raw, dict):
            return raw
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {"switchers": [], "tableOwners": []}

    def _discover_tablist_layout(self) -> Dict[str, Any]:
        assert self.driver
        raw = self.driver.execute_script(self._DISCOVER_TABLIST_LAYOUT_JS)
        if isinstance(raw, dict):
            return raw
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {"tablists": [], "tableOwners": []}

    def _scroll_content_switcher_into_view(self, switcher_index: int) -> None:
        assert self.driver
        try:
            self.driver.execute_script(
                """
                var main = document.querySelector('main');
                if (!main) return;
                var list = main.querySelectorAll('[aria-label="Content switcher"]');
                var si = arguments[0];
                if (si < 0 || si >= list.length) return;
                list[si].scrollIntoView({block: 'center', inline: 'nearest'});
                """,
                int(switcher_index),
            )
            time.sleep(0.12)
        except Exception:
            pass

    def _click_switcher(self, switcher_index: int, value: str) -> bool:
        assert self.driver
        try:
            self._scroll_content_switcher_into_view(switcher_index)
            return bool(
                self.driver.execute_script(
                    self._CLICK_SWITCHER_OPTION_JS, int(switcher_index), value
                )
            )
        except Exception:
            return False

    def _visible_content_switcher_values(self, switcher_index: int) -> List[str]:
        """Live DOM order of visible ``data-value`` options (``role="group"`` switcher)."""
        assert self.driver
        try:
            raw = self.driver.execute_script(
                self._VISIBLE_CONTENT_SWITCHER_VALUES_JS, int(switcher_index)
            )
            data = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(data, list):
                return []
            return [str(x).strip().lower() for x in data if str(x).strip()]
        except Exception:
            return []

    _SWITCHER_OPTION_SELECTED_JS = r"""
    var main = document.querySelector('main');
    if (!main) return false;
    function btnVisible(b) {
        if (!b) return false;
        var r = b.getBoundingClientRect();
        if (r.width < 1.5 || r.height < 1.5) return false;
        var st = window.getComputedStyle(b);
        if (st.display === 'none' || st.visibility === 'hidden') return false;
        if (parseFloat(st.opacity || '1') < 0.05) return false;
        return true;
    }
    var list = main.querySelectorAll('[aria-label="Content switcher"]');
    var si = arguments[0];
    var val = (arguments[1] || '').trim().toLowerCase();
    if (si < 0 || si >= list.length) return false;
    var sw = list[si];
    var btns = sw.querySelectorAll('button[data-content-switcher-option][data-value]');
    for (var i = 0; i < btns.length; i++) {
        var b = btns[i];
        if ((b.getAttribute('data-value') || '').trim().toLowerCase() !== val) continue;
        if (!btnVisible(b)) continue;
        if (b.getAttribute('aria-checked') === 'true') return true;
        if (b.getAttribute('data-state') === 'on') return true;
    }
    return false;
    """

    def _wait_switcher_selected(
        self, switcher_index: int, value: str, timeout: float = 12.0
    ) -> bool:
        assert self.driver
        deadline = time.time() + timeout
        v = (value or "").strip().lower()
        while time.time() < deadline:
            try:
                if self.driver.execute_script(
                    self._SWITCHER_OPTION_SELECTED_JS, int(switcher_index), v
                ):
                    return True
            except Exception:
                pass
            time.sleep(0.12)
        return False

    def _click_tablist(self, tablist_index: int, label: str) -> bool:
        assert self.driver
        try:
            return bool(
                self.driver.execute_script(
                    self._CLICK_TAB_IN_TABLIST_JS, int(tablist_index), label
                )
            )
        except Exception:
            return False

    def _collect_html_tables(self) -> List[Dict[str, Any]]:
        assert self.driver
        try:
            raw = self.driver.execute_script(self._EXTRACT_HTML_TABLES_JS)
            data = json.loads(raw) if isinstance(raw, str) else raw
            return data if isinstance(data, list) else []
        except Exception as e:
            print(f"HTML table extraction failed: {e}")
            return []

    @staticmethod
    def _looks_like_primary_model_id(value: str) -> bool:
        t = (value or "").strip()
        if not t:
            return False
        if len(t) >= 12:
            return True
        if re.search(r"\d", t):
            return True
        if "." in t:
            return True
        if "-" in t and len(t) >= 5:
            return True
        parts = t.split()
        if len(parts) >= 2 and len(t) >= 8:
            return True
        return False

    def _nest_modality_subrows(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not rows:
            return []
        keys = {str(k) for k in rows[0].keys()}
        if "Modality" not in keys:
            return rows
        out: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            model_cell = str(row.get("Model", "")).strip()
            if out and not self._looks_like_primary_model_id(model_cell):
                parent = out[-1]
                if "by_modality" not in parent:
                    parent["by_modality"] = {}
                label = model_cell or "_continuation"
                parent["by_modality"][label] = {
                    k: v for k, v in row.items() if k != "Model"
                }
            else:
                out.append(dict(row))
        return out

    @staticmethod
    def _normalize_for_fingerprint(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {
                str(k): OpenAIScraper4._normalize_for_fingerprint(v)
                for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))
            }
        if isinstance(obj, list):
            return [OpenAIScraper4._normalize_for_fingerprint(x) for x in obj]
        if isinstance(obj, str):
            return obj.strip()
        return obj

    @classmethod
    def _rows_fingerprint(cls, rows: List[Dict[str, Any]]) -> str:
        norm = cls._normalize_for_fingerprint(rows)
        return json.dumps(norm, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    def _snapshot_tables(
        self,
        *,
        pricing_tier: Optional[str],
        controller_kind: str,
        controller_index: Optional[int],
        owned_1based: Optional[List[int]],
    ) -> Dict[str, Any]:
        return {
            "pricing_tier": pricing_tier,
            "controlling_switcher_index": controller_index
            if controller_kind == "switcher"
            else None,
            "controlling_tablist_index": controller_index
            if controller_kind == "tablist"
            else None,
            "controller_kind": controller_kind,
            "owned_table_indices": owned_1based,
            "tables": self._collect_html_tables(),
        }

    def _merge_snapshots(self, snapshots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Merge tier snapshots: each ``pricing_tier`` overwrites or sets
        ``by_pricing_tier[tier]`` for that table key. Identical row bodies under
        different tier names are all kept.
        """
        # Key by (section, headers) only: visible table_index can shift between tier
        # clicks when the DOM reflows, which would drop batch rows for image/video.
        bucket: Dict[Tuple[str, Tuple[str, ...]], List[Dict[str, Any]]] = defaultdict(list)
        key_order: List[Tuple[str, Tuple[str, ...]]] = []
        seen_keys: set = set()
        table_index_by_key: Dict[Tuple[str, Tuple[str, ...]], int] = {}

        for snap in snapshots:
            ptier = (snap.get("pricing_tier") or "").strip() or None
            owned_raw = snap.get("owned_table_indices")
            allowed: Optional[set] = None
            if owned_raw is not None:
                if not owned_raw:
                    continue
                try:
                    allowed = {int(x) for x in owned_raw}
                except (TypeError, ValueError):
                    continue
            for tbl in snap.get("tables") or []:
                if not isinstance(tbl, dict):
                    continue
                sec = str(tbl.get("section_heading") or "")
                heads = tuple(tbl.get("headers") or [])
                tidx = int(tbl.get("table_index") or 0)
                if allowed is not None and tidx not in allowed:
                    continue
                key = (sec, heads)
                if key not in seen_keys:
                    seen_keys.add(key)
                    key_order.append(key)
                    table_index_by_key[key] = tidx
                bucket[key].append({"pricing_tier": ptier, "rows": tbl.get("rows") or []})

        merged: List[Dict[str, Any]] = []
        for key in key_order:
            sec, heads = key
            tidx = table_index_by_key.get(key, 0)
            variants = bucket[key]
            by_tier: Dict[str, Dict[str, Any]] = {}
            for m in variants:
                tier = (m.get("pricing_tier") or "").strip() or "default"
                raw_rows = m.get("rows") or []
                rows = (
                    self._nest_modality_subrows([r for r in raw_rows if isinstance(r, dict)])
                    if isinstance(raw_rows, list)
                    else []
                )
                by_tier[tier] = {"rows": rows}

            merged.append(
                {
                    "section_heading": sec,
                    "headers": list(heads),
                    "table_index": tidx,
                    "by_pricing_tier": by_tier,
                }
            )
        return merged

    @staticmethod
    def _extracted_table_key(block: Dict[str, Any]) -> Tuple[str, Tuple[str, ...]]:
        return (
            str(block.get("section_heading") or ""),
            tuple(block.get("headers") or []),
        )

    def _merge_extracted_table_lists(
        self,
        primary: List[Dict[str, Any]],
        secondary: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Merge ``by_pricing_tier`` from ``secondary`` into matching blocks in
        ``primary`` (same section + headers). Keeps primary document order; appends
        blocks only present in secondary.
        """
        order: List[Tuple[str, Tuple[str, ...]]] = []
        by_k: Dict[Tuple[str, Tuple[str, ...]], Dict[str, Any]] = {}
        for b in primary:
            k = self._extracted_table_key(b)
            if k not in by_k:
                order.append(k)
            by_k[k] = {
                "section_heading": b.get("section_heading"),
                "headers": list(b.get("headers") or []),
                "table_index": b.get("table_index"),
                "by_pricing_tier": dict(b.get("by_pricing_tier") or {}),
            }
        for eb in secondary:
            k = self._extracted_table_key(eb)
            if k not in by_k:
                by_k[k] = {
                    "section_heading": eb.get("section_heading"),
                    "headers": list(eb.get("headers") or []),
                    "table_index": eb.get("table_index"),
                    "by_pricing_tier": dict(eb.get("by_pricing_tier") or {}),
                }
                order.append(k)
                continue
            tmap = by_k[k].setdefault("by_pricing_tier", {})
            for tier, payload in (eb.get("by_pricing_tier") or {}).items():
                if tier not in tmap:
                    tmap[tier] = payload
        return [by_k[k] for k in order]

    def _discover_local_content_switcher_strips(self) -> List[Dict[str, Any]]:
        assert self.driver
        try:
            raw = self.driver.execute_script(
                self._DISCOVER_LOCAL_CONTENT_SWITCHER_STRIPS_JS
            )
            data = json.loads(raw) if isinstance(raw, str) else raw
            return data if isinstance(data, list) else []
        except Exception:
            return []

    @staticmethod
    def _tier_key_from_switcher_value(raw: Any) -> str:
        return str(raw or "").strip().lower()

    @staticmethod
    def _rows_from_collect(
        collected: List[Dict[str, Any]], sec: str, headers: List[str]
    ) -> Optional[List[Dict[str, Any]]]:
        heads_t = tuple(headers)
        for t in collected:
            if str(t.get("section_heading") or "") != sec:
                continue
            if tuple(t.get("headers") or []) == heads_t:
                rows = t.get("rows")
                if isinstance(rows, list):
                    return list(rows)
        return None

    def _supplement_local_content_switcher_tiers(
        self, extracted: List[Dict[str, Any]]
    ) -> None:
        """
        For each Content switcher that sits immediately above one or more tables
        (closest preceding in document order), read visible ``data-value`` tiers
        from the DOM, click each in order, and write rows into
        ``by_pricing_tier[<value>]``. Tier names are never hardcoded.
        """
        assert self.driver
        strips = self._discover_local_content_switcher_strips()
        if not strips:
            return
        for strip in strips:
            if not isinstance(strip, dict):
                continue
            try:
                sw_idx = int(strip.get("switcher_index", -1))
            except (TypeError, ValueError):
                continue
            if sw_idx < 0:
                continue
            raw_vals = strip.get("visible_values") or []
            if not isinstance(raw_vals, list) or len(raw_vals) < 2:
                continue
            ordered_keys: List[str] = []
            seen_k: set = set()
            for v in raw_vals:
                k = self._tier_key_from_switcher_value(v)
                if not k or k in seen_k:
                    continue
                seen_k.add(k)
                ordered_keys.append(k)
            if len(ordered_keys) < 2:
                continue
            live_k = self._visible_content_switcher_values(sw_idx)
            for lk in live_k:
                if lk not in ordered_keys:
                    ordered_keys.append(lk)
            restore_val = ordered_keys[0]
            tables_spec = strip.get("tables") or []
            if not isinstance(tables_spec, list) or not tables_spec:
                continue
            if len(tables_spec) > self.LOCAL_CONTENT_STRIP_MAX_TABLES:
                if len(ordered_keys) < 4:
                    continue

            for tier_val in ordered_keys:
                self._scroll_content_switcher_into_view(sw_idx)
                if not self._click_switcher(sw_idx, tier_val):
                    continue
                wait_to = 18.0 if len(ordered_keys) > 3 else 12.0
                if not self._wait_switcher_selected(sw_idx, tier_val, timeout=wait_to):
                    continue
                time.sleep(self.TIER_CLICK_PAUSE)
                self._expand_after_tier_change()
                collected = self._collect_html_tables()
                for spec in tables_spec:
                    if not isinstance(spec, dict):
                        continue
                    sec = str(spec.get("section_heading") or "")
                    hdrs = spec.get("headers") or []
                    if not isinstance(hdrs, list):
                        continue
                    hdrs_str = [str(h) for h in hdrs]
                    rows = self._rows_from_collect(collected, sec, hdrs_str)
                    if not rows:
                        continue
                    nested = self._nest_modality_subrows(rows)
                    k = (sec, tuple(hdrs_str))
                    for b in extracted:
                        if self._extracted_table_key(b) == k:
                            b.setdefault("by_pricing_tier", {})[tier_val] = {
                                "rows": nested
                            }
                            break

            self._scroll_content_switcher_into_view(sw_idx)
            self._click_switcher(sw_idx, restore_val)
            self._wait_switcher_selected(sw_idx, restore_val)
            time.sleep(0.35)

    def _gather_with_switchers(self, layout: Dict[str, Any]) -> List[Dict[str, Any]]:
        assert self.driver
        owners_raw = layout.get("tableOwners") or []
        owners: List[int] = []
        for ow in owners_raw:
            try:
                owners.append(int(ow))
            except (TypeError, ValueError):
                owners.append(-1)
        switchers_meta: List[Dict[str, Any]] = layout.get("switchers") or []
        snapshots: List[Dict[str, Any]] = []
        sparse_owned = layout.get("sparseOwnedTablesBySwitcher") or {}

        self._scroll_main()
        time.sleep(0.35)
        self._expand_all_models_until_stable()

        unowned_1based = [tbl_idx + 1 for tbl_idx, ow in enumerate(owners) if ow < 0]
        if unowned_1based:
            snapshots.append(
                self._snapshot_tables(
                    pricing_tier="default",
                    controller_kind="none",
                    controller_index=None,
                    owned_1based=unowned_1based,
                )
            )

        for o, meta in enumerate(switchers_meta):
            values = meta.get("values") or []
            disabled_vals = set()
            for d in meta.get("options_detail") or []:
                if isinstance(d, dict) and d.get("disabled"):
                    dv = str(d.get("value") or "").strip().lower()
                    if dv:
                        disabled_vals.add(dv)
            extra_sparse = sparse_owned.get(str(o)) or sparse_owned.get(o)
            if not isinstance(extra_sparse, list):
                extra_sparse = []
            try:
                extra_ints = [int(x) for x in extra_sparse]
            except (TypeError, ValueError):
                extra_ints = []
            owner_based = [ti + 1 for ti, ow in enumerate(owners) if ow == o]
            owned_1based = sorted(set(owner_based + extra_ints))
            if not owned_1based:
                continue
            live = self._visible_content_switcher_values(o)
            base_vals = [
                str(v).strip().lower()
                for v in (meta.get("values") or [])
                if str(v).strip()
            ]
            if live:
                values = list(live)
                for bv in base_vals:
                    if bv not in values:
                        values.append(bv)
                disabled_vals = set()
            if not values:
                continue
            for val in values:
                if str(val).strip().lower() in disabled_vals:
                    continue
                if not self._click_switcher(o, str(val)):
                    print(f"  switcher[{o}] could not select {val!r}")
                    continue
                wait_to = 18.0 if len(values) > 3 else 12.0
                if not self._wait_switcher_selected(o, str(val), timeout=wait_to):
                    print(f"  switcher[{o}] timeout waiting for {val!r}")
                    continue
                time.sleep(self.TIER_CLICK_PAUSE)
                self._expand_after_tier_change()
                snapshots.append(
                    self._snapshot_tables(
                        pricing_tier=str(val),
                        controller_kind="switcher",
                        controller_index=o,
                        owned_1based=owned_1based,
                    )
                )

        self._scroll_main()
        self._expand_all_models_until_stable()
        return self._merge_snapshots(snapshots)

    def _gather_with_tablists(self, layout: Dict[str, Any]) -> List[Dict[str, Any]]:
        assert self.driver
        owners_raw = layout.get("tableOwners") or []
        owners: List[int] = []
        for ow in owners_raw:
            try:
                owners.append(int(ow))
            except (TypeError, ValueError):
                owners.append(-1)
        tablists_meta: List[Dict[str, Any]] = layout.get("tablists") or []
        snapshots: List[Dict[str, Any]] = []

        self._scroll_main()
        time.sleep(0.35)
        self._expand_all_models_until_stable()

        unowned_1based = [tbl_idx + 1 for tbl_idx, ow in enumerate(owners) if ow < 0]
        if unowned_1based:
            snapshots.append(
                self._snapshot_tables(
                    pricing_tier="default",
                    controller_kind="none",
                    controller_index=None,
                    owned_1based=unowned_1based,
                )
            )

        for o, meta in enumerate(tablists_meta):
            labels = meta.get("labels") or []
            if not labels:
                continue
            disabled_labels = set()
            for d in meta.get("options_detail") or []:
                if isinstance(d, dict) and d.get("disabled"):
                    dl = str(d.get("label") or "").strip().replace(
                        "\u00a0", " "
                    )
                    dl = " ".join(dl.split())
                    if dl:
                        disabled_labels.add(dl)
            owned_1based = [ti + 1 for ti, ow in enumerate(owners) if ow == o]
            if not owned_1based:
                continue
            for label in labels:
                lab_norm = str(label).strip().replace("\u00a0", " ")
                lab_norm = " ".join(lab_norm.split())
                if lab_norm in disabled_labels:
                    continue
                if not self._click_tablist(o, str(label)):
                    continue
                time.sleep(self.TIER_CLICK_PAUSE)
                self._expand_after_tier_change()
                tier_key = str(label).strip().lower().replace(" ", "_")
                snapshots.append(
                    self._snapshot_tables(
                        pricing_tier=tier_key,
                        controller_kind="tablist",
                        controller_index=o,
                        owned_1based=owned_1based,
                    )
                )

        self._scroll_main()
        self._expand_all_models_until_stable()
        return self._merge_snapshots(snapshots)

    def _gather_single_pass(self) -> List[Dict[str, Any]]:
        assert self.driver
        self._scroll_main()
        time.sleep(0.35)
        self._expand_after_tier_change()
        snap = self._snapshot_tables(
            pricing_tier="default",
            controller_kind="none",
            controller_index=None,
            owned_1based=None,
        )
        return self._merge_snapshots([snap])

    def scrape(self) -> Dict[str, Any]:
        self._setup_driver()
        assert self.driver
        try:
            self.driver.get(self.pricing_url)
            self._wait_for_main()
            time.sleep(1.0)
            self._scroll_main()
            time.sleep(0.45)
            self._expand_all_models_until_stable()

            sw_layout = self._discover_switcher_layout()
            switchers = sw_layout.get("switchers") or []
            has_switcher = any(
                isinstance(s, dict) and (s.get("values") or []) for s in switchers
            )

            if has_switcher:
                extracted = self._gather_with_switchers(sw_layout)
                if self.use_tablist_fallback:
                    tl_layout = self._discover_tablist_layout()
                    tablists = tl_layout.get("tablists") or []
                    if any(
                        isinstance(t, dict) and (t.get("labels") or [])
                        for t in tablists
                    ):
                        extracted_tabs = self._gather_with_tablists(tl_layout)
                        extracted = self._merge_extracted_table_lists(
                            extracted, extracted_tabs
                        )
            elif self.use_tablist_fallback:
                tl_layout = self._discover_tablist_layout()
                tablists = tl_layout.get("tablists") or []
                has_tabs = any(
                    isinstance(t, dict) and (t.get("labels") or []) for t in tablists
                )
                if has_tabs:
                    extracted = self._gather_with_tablists(tl_layout)
                else:
                    extracted = self._gather_single_pass()
            else:
                extracted = self._gather_single_pass()

            self._supplement_local_content_switcher_tiers(extracted)

            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            self.pricing_data = {
                "provider": "openai",
                "last_updated": now,
                "source_url": self.pricing_url,
                "extracted_tables": extracted,
            }
            return self.pricing_data
        finally:
            self.driver.quit()
            self.driver = None

    def save_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.pricing_data, f, indent=2, ensure_ascii=False)
        print(f"Wrote {path}")


def main() -> None:
    p = argparse.ArgumentParser(description="Scrape OpenAI API pricing (content switcher + tables).")
    p.add_argument(
        "--url",
        default=DEFAULT_PRICING_URL,
        help="Pricing page URL (default: batch+ft-pricing query as requested).",
    )
    p.add_argument(
        "-o",
        "--output",
        default="openai_pricing.json",
        help="Output JSON path",
    )
    p.add_argument("--headless", action="store_true", help="Run Chrome headless")
    p.add_argument(
        "--no-tablist-fallback",
        action="store_true",
        help="Do not fall back to [role=tablist] if no Content switcher",
    )
    args = p.parse_args()

    scraper = OpenAIScraper4(
        pricing_url=args.url,
        headless=args.headless,
        use_tablist_fallback=not args.no_tablist_fallback,
    )
    print(f"Loading {args.url!r} …")
    data = scraper.scrape()
    if not data.get("extracted_tables"):
        print("No tables extracted.")
    else:
        print(f"Tables (merged blocks): {len(data['extracted_tables'])}")
    scraper.save_json(args.output)


if __name__ == "__main__":
    main()
