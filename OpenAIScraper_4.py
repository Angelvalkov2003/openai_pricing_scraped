"""
OpenAI API pricing scraper (v4) for developers.openai.com.

Uses the segmented **Content switcher** (``data-value`` from the page, not a fixed list)
and optionally ``[role=tablist]``. Discovered ``options_from_dom`` are stored under
``pricing_control``; ``by_pricing_tier`` drops **consecutive** duplicate fingerprints
(tier N skipped when row data matches tier N-1 that was kept), so inactive tables
do not repeat the same block under batch/flex/priority while real standard→batch
changes are kept.
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
        function fillSectionGaps(owners, topTables, main) {
            var h2s = Array.prototype.slice.call(main.querySelectorAll('h2'));
            function nearestPrecedingH2(tbl) {
                var best = null;
                for (var i = 0; i < h2s.length; i++) {
                    if (h2s[i].compareDocumentPosition(tbl) & Node.DOCUMENT_POSITION_PRECEDING) {
                        best = h2s[i];
                    }
                }
                return best;
            }
            var buckets = {};
            for (var ti = 0; ti < topTables.length; ti++) {
                var h2 = nearestPrecedingH2(topTables[ti]);
                var key = h2 ? ('h2_' + h2s.indexOf(h2)) : '_root';
                if (!buckets[key]) buckets[key] = [];
                buckets[key].push(ti);
            }
            function fillIndices(indices) {
                var pending = [];
                var lastPos = -1;
                for (var k = 0; k < indices.length; k++) {
                    var idx = indices[k];
                    if (owners[idx] >= 0) {
                        for (var p = 0; p < pending.length; p++) {
                            owners[pending[p]] = owners[idx];
                        }
                        pending = [];
                        lastPos = owners[idx];
                    } else {
                        pending.push(idx);
                    }
                }
                for (var p = 0; p < pending.length; p++) {
                    if (lastPos >= 0) owners[pending[p]] = lastPos;
                }
            }
            for (var key in buckets) {
                if (buckets.hasOwnProperty(key)) fillIndices(buckets[key]);
            }
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
            fillSectionGaps(owners, topTables, main);
        }
        return { switchers: switchers, tableOwners: owners };
    })());
    """

    _CLICK_SWITCHER_OPTION_JS = r"""
    var main = document.querySelector('main');
    if (!main) return false;
    var list = main.querySelectorAll('[aria-label="Content switcher"]');
    var si = arguments[0];
    var val = (arguments[1] || '').trim().toLowerCase();
    if (si < 0 || si >= list.length) return false;
    var sw = list[si];
    var btns = sw.querySelectorAll('button[data-content-switcher-option][data-value]');
    for (var i = 0; i < btns.length; i++) {
        if ((btns[i].getAttribute('data-value') || '').trim().toLowerCase() === val) {
            btns[i].click();
            return true;
        }
    }
    return false;
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
        function fillSectionGaps(owners, topTables, main) {
            var h2s = Array.prototype.slice.call(main.querySelectorAll('h2'));
            function nearestPrecedingH2(tbl) {
                var best = null;
                for (var i = 0; i < h2s.length; i++) {
                    if (h2s[i].compareDocumentPosition(tbl) & Node.DOCUMENT_POSITION_PRECEDING) {
                        best = h2s[i];
                    }
                }
                return best;
            }
            var buckets = {};
            for (var ti = 0; ti < topTables.length; ti++) {
                var h2 = nearestPrecedingH2(topTables[ti]);
                var key = h2 ? ('h2_' + h2s.indexOf(h2)) : '_root';
                if (!buckets[key]) buckets[key] = [];
                buckets[key].push(ti);
            }
            function fillIndices(indices) {
                var pending = [];
                var lastPos = -1;
                for (var k = 0; k < indices.length; k++) {
                    var idx = indices[k];
                    if (owners[idx] >= 0) {
                        for (var p = 0; p < pending.length; p++) {
                            owners[pending[p]] = owners[idx];
                        }
                        pending = [];
                        lastPos = owners[idx];
                    } else {
                        pending.push(idx);
                    }
                }
                for (var p = 0; p < pending.length; p++) {
                    if (lastPos >= 0) owners[pending[p]] = lastPos;
                }
            }
            for (var key in buckets) {
                if (buckets.hasOwnProperty(key)) fillIndices(buckets[key]);
            }
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
        } else {
            fillSectionGaps(owners, topTables, main);
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

    def _click_switcher(self, switcher_index: int, value: str) -> bool:
        assert self.driver
        try:
            return bool(
                self.driver.execute_script(
                    self._CLICK_SWITCHER_OPTION_JS, int(switcher_index), value
                )
            )
        except Exception:
            return False

    _SWITCHER_OPTION_SELECTED_JS = r"""
    var main = document.querySelector('main');
    if (!main) return false;
    var list = main.querySelectorAll('[aria-label="Content switcher"]');
    var si = arguments[0];
    var val = (arguments[1] || '').trim().toLowerCase();
    if (si < 0 || si >= list.length) return false;
    var sw = list[si];
    var btns = sw.querySelectorAll('button[data-content-switcher-option][data-value]');
    for (var i = 0; i < btns.length; i++) {
        var b = btns[i];
        if ((b.getAttribute('data-value') || '').trim().toLowerCase() !== val) continue;
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

    def _merge_snapshots(
        self,
        snapshots: List[Dict[str, Any]],
        *,
        table_pricing_control_by_index: Optional[Dict[int, Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Merge tier snapshots. For each table, skip a tier when its row fingerprint
        equals the **last kept** tier's fingerprint (consecutive duplicates). Same
        body repeated across many clicks collapses to one entry; ``standard / batch /
        flex / priority`` with flex identical to batch still yields three keys.
        """
        bucket: Dict[Tuple[str, Tuple[str, ...], int], List[Dict[str, Any]]] = (
            defaultdict(list)
        )
        key_order: List[Tuple[str, Tuple[str, ...], int]] = []
        seen_keys: set = set()

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
                key = (sec, heads, tidx)
                if key not in seen_keys:
                    seen_keys.add(key)
                    key_order.append(key)
                bucket[key].append({"pricing_tier": ptier, "rows": tbl.get("rows") or []})

        ctrl_map = table_pricing_control_by_index or {}
        merged: List[Dict[str, Any]] = []
        for key in key_order:
            sec, heads, tidx = key
            variants = bucket[key]
            by_tier: Dict[str, Dict[str, Any]] = {}
            last_kept_fp: Optional[str] = None
            for m in variants:
                tier = (m.get("pricing_tier") or "").strip() or "default"
                raw_rows = m.get("rows") or []
                rows = (
                    self._nest_modality_subrows([r for r in raw_rows if isinstance(r, dict)])
                    if isinstance(raw_rows, list)
                    else []
                )
                fp = self._rows_fingerprint(rows)
                if last_kept_fp is not None and fp == last_kept_fp:
                    continue
                last_kept_fp = fp
                by_tier[tier] = {"rows": rows}

            block: Dict[str, Any] = {
                "section_heading": sec,
                "headers": list(heads),
                "table_index": tidx,
                "by_pricing_tier": by_tier,
            }
            ctrl = ctrl_map.get(tidx)
            if ctrl:
                block["pricing_control"] = ctrl
            merged.append(block)
        return merged

    def _control_map_for_switchers(
        self, layout: Dict[str, Any], owners: List[int]
    ) -> Dict[int, Dict[str, Any]]:
        out: Dict[int, Dict[str, Any]] = {}
        for o, meta in enumerate(layout.get("switchers") or []):
            if not isinstance(meta, dict):
                continue
            values = meta.get("values") or []
            owned_1based = [ti + 1 for ti, ow in enumerate(owners) if ow == o]
            for t1 in owned_1based:
                detail = meta.get("options_detail")
                if not isinstance(detail, list):
                    detail = []
                out[int(t1)] = {
                    "type": "content_switcher",
                    "dom_index": o,
                    "aria_label": "Content switcher",
                    "options_from_dom": [str(v) for v in values],
                    "options_detail": detail,
                }
        return out

    def _control_map_for_tablists(
        self, layout: Dict[str, Any], owners: List[int]
    ) -> Dict[int, Dict[str, Any]]:
        out: Dict[int, Dict[str, Any]] = {}
        for o, meta in enumerate(layout.get("tablists") or []):
            if not isinstance(meta, dict):
                continue
            labels = meta.get("labels") or []
            owned_1based = [ti + 1 for ti, ow in enumerate(owners) if ow == o]
            for t1 in owned_1based:
                detail = meta.get("options_detail")
                if not isinstance(detail, list):
                    detail = []
                out[int(t1)] = {
                    "type": "tablist",
                    "dom_index": o,
                    "options_from_dom": [str(x) for x in labels],
                    "options_detail": detail,
                }
        return out

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
        control_map = self._control_map_for_switchers(layout, owners)

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
            if not values:
                continue
            disabled_vals = set()
            for d in meta.get("options_detail") or []:
                if isinstance(d, dict) and d.get("disabled"):
                    dv = str(d.get("value") or "").strip().lower()
                    if dv:
                        disabled_vals.add(dv)
            owned_1based = [ti + 1 for ti, ow in enumerate(owners) if ow == o]
            if not owned_1based:
                continue
            for val in values:
                if str(val).strip().lower() in disabled_vals:
                    continue
                if not self._click_switcher(o, str(val)):
                    print(f"  switcher[{o}] could not select {val!r}")
                    continue
                if not self._wait_switcher_selected(o, str(val)):
                    print(f"  switcher[{o}] timeout waiting for {val!r}")
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
        return self._merge_snapshots(
            snapshots, table_pricing_control_by_index=control_map
        )

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
        control_map = self._control_map_for_tablists(layout, owners)

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
        return self._merge_snapshots(
            snapshots, table_pricing_control_by_index=control_map
        )

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

            sw_layout = self._discover_switcher_layout()
            switchers = sw_layout.get("switchers") or []
            has_switcher = any(
                isinstance(s, dict) and (s.get("values") or []) for s in switchers
            )

            if has_switcher:
                extracted = self._gather_with_switchers(sw_layout)
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
