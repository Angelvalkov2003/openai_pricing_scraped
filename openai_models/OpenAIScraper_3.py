"""
OpenAI pricing scraper for developer docs.

``extracted_tables`` walks ``main`` in document order, maps each table to its
``[role=tablist]`` (TreeWalker + per-H2 gap fill), then for each tablist cycles only that list's
tabs so other sections do not reset prices. Tier labels come from tab text, not
fixed strings. Optional ``pricing_data.markdown_tables`` is a generic markdown
pass. PEP 8 oriented.
"""

import json
import time
import re
from collections import defaultdict
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, asdict
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException


@dataclass
class PricingTier:
    """Represents a pricing tier with input, cached input, and output costs."""
    input_price: Optional[str] = None
    cached_input_price: Optional[str] = None
    output_price: Optional[str] = None


@dataclass
class ModelPricing:
    """Represents pricing information for a single model."""
    model_name: str
    pricing_tiers: Dict[str, PricingTier]
    category: str
    additional_info: Optional[Dict[str, Any]] = None


class OpenAIScraper:
    """Scraper for OpenAI pricing documentation."""

    # Developer docs (React tables + "All models" expanders); see
    # https://developers.openai.com/api/docs/pricing
    BASE_URL = "https://developers.openai.com/api/docs/pricing"
    LEGACY_PLATFORM_PRICING_URL = "https://platform.openai.com/docs/pricing"
    COPY_BUTTON_SELECTORS = (
        'button[class*="copy-button"]',
        'button[class*="copy"]',
        'button[aria-label*="Copy"]',
        'button[aria-label*="copy"]',
        '[data-testid*="copy"]',
    )
    WAIT_TIMEOUT = 30
    # Max length for a <p> line emitted as a standalone row (tier labels, short notes).
    _DOM_SHORT_PARAGRAPH_MAX_LEN = 160
    # Rolling context lines kept before each markdown table (no fixed section names).
    _MARKDOWN_CONTEXT_MAX_LINES = 8

    @staticmethod
    def _looks_like_primary_model_id(value: str) -> bool:
        """
        Distinguish a real model / product id cell from a short sub-row label
        (e.g. modality line after rowspan) without hardcoding label strings.
        """
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

    def __init__(
        self,
        pricing_url: Optional[str] = None,
        *,
        click_pricing_tabs: bool = True,
        max_expand_rounds: int = 25,
    ):
        """
        Args:
            pricing_url: Page to open (default: developer docs pricing).
            click_pricing_tabs: If True, discover tablists vs tables in the DOM,
                then for each tablist click each of its tabs (labels from the page)
                and capture rows only for tables that tablist controls.
            max_expand_rounds: Max passes when clicking "All models" toggles.
        """
        self.driver = None
        self.pricing_data = {}
        self.pricing_url = pricing_url or self.BASE_URL
        self.click_pricing_tabs = click_pricing_tabs
        self.max_expand_rounds = max_expand_rounds
        self.extracted_tables: List[Dict[str, Any]] = []
        
    def _setup_driver(self) -> None:
        """Configure and initialize the Chrome WebDriver."""
        options = webdriver.ChromeOptions()
        # options.add_argument('--headless')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-blink-features=AutomationControlled')
        
        # Handle clipboard permissions automatically
        prefs = {
            "profile.default_content_setting_values.clipboard": 1,
            "profile.content_settings.exceptions.clipboard": {
                "[*.]openai.com,*": {"setting": 1}
            }
        }
        options.add_experimental_option("prefs", prefs)
        
        self.driver = webdriver.Chrome(options=options)
        
    def _extract_clipboard_content(self) -> Optional[str]:
        """
        Extract content from clipboard after clicking copy button.
        
        Returns:
            Clipboard content as string, or None if extraction fails.
        """
        try:
            # Use JavaScript to get clipboard content
            clipboard_text = self.driver.execute_script(
                "return navigator.clipboard.readText();"
            )
            return clipboard_text
        except Exception as e:
            print(f"Error extracting clipboard content: {e}")
            return None
    
    def _wait_for_pricing_content(self) -> None:
        """Wait until doc body has loaded pricing-related content."""
        wait = WebDriverWait(self.driver, self.WAIT_TIMEOUT)
        for locator in (
            (By.XPATH, "//*[contains(., 'Flagship') or contains(., 'Pricing') or contains(., 'gpt-')]"),
            (By.TAG_NAME, "main"),
        ):
            try:
                wait.until(EC.presence_of_element_located(locator))
                return
            except TimeoutException:
                continue

    def _pick_best_pricing_text(
        self, dom_text: Optional[str], copy_text: Optional[str]
    ) -> Optional[str]:
        """Prefer clipboard markdown when it looks like full pricing; else use DOM."""
        dom_text = (dom_text or "").strip()
        copy_text = (copy_text or "").strip()
        copy_has_tables = "|" in copy_text and len(copy_text) >= 200
        copy_has_models = "gpt-" in copy_text.lower() or "\n| Model |" in copy_text
        if copy_has_tables and copy_has_models and len(copy_text) >= len(dom_text):
            return copy_text
        if dom_text and len(dom_text) >= 120:
            return dom_text
        return copy_text or dom_text or None

    def _click_copy_button(self) -> Optional[str]:
        """
        Locate and click a copy-to-clipboard control, then read the clipboard.

        Returns:
            Copied text content or None if operation fails.
        """
        for sel in self.COPY_BUTTON_SELECTORS:
            try:
                wait = WebDriverWait(self.driver, 8)
                btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, sel)))
                self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                time.sleep(0.3)
                btn.click()
                time.sleep(1.2)
                return self._extract_clipboard_content()
            except (TimeoutException, Exception):
                continue
        print("Copy button not found (tried multiple selectors)")
        return None

    _EXTRACT_PRICING_MARKDOWN_JS = r"""
    return (function() {
        function cellText(c) {
            if (!c) return '';
            return (c.innerText || '').trim().replace(/\s+/g, ' ');
        }
        function rowLine(parts) {
            if (!parts.length) return '';
            return '| ' + parts.join(' | ') + ' |';
        }
        function linesFromTableLike(el) {
            var lines = [];
            if (el.tagName === 'TABLE') {
                el.querySelectorAll('tr').forEach(function(tr) {
                    var cells = tr.querySelectorAll('th, td');
                    if (!cells.length) return;
                    var parts = Array.from(cells).map(cellText);
                    lines.push(rowLine(parts));
                });
                return lines;
            }
            var role = el.getAttribute && el.getAttribute('role');
            if (role === 'table' || role === 'grid') {
                var rows = el.querySelectorAll('[role="row"]');
                if (rows.length) {
                    rows.forEach(function(row) {
                        var cells = row.querySelectorAll(
                            '[role="cell"], [role="columnheader"], [role="gridcell"]'
                        );
                        if (!cells.length) return;
                        var parts = Array.from(cells).map(cellText);
                        lines.push(rowLine(parts));
                    });
                    return lines;
                }
            }
            return lines;
        }
        function isNestedTableOrGrid(el) {
            var p = el.parentElement;
            while (p) {
                if (p.tagName === 'TABLE' && p !== el) return true;
                var r = p.getAttribute && p.getAttribute('role');
                if ((r === 'table' || r === 'grid') && p !== el) return true;
                p = p.parentElement;
            }
            return false;
        }
        var root = document.querySelector('main') || document.body;
        var out = [];
        var sel = 'h2, h3, h4, h5, table, [role="table"], [role="grid"], p';
        var nodes = root.querySelectorAll(sel);
        Array.prototype.forEach.call(nodes, function(el) {
            if (el.tagName === 'TABLE' && isNestedTableOrGrid(el)) {
                return;
            }
            var tag = el.tagName && el.tagName.toLowerCase();
            if (tag === 'h2' || tag === 'h3' || tag === 'h4' || tag === 'h5') {
                var ht = cellText(el);
                if (!ht) return;
                if (tag === 'h4') out.push('#### ' + ht);
                else if (tag === 'h5') out.push('##### ' + ht);
                else out.push(ht);
                return;
            }
            if (tag === 'p') {
                if (el.querySelector('table')) return;
                var pt = cellText(el);
                if (!pt || pt.length > 160 || (el.innerText && el.innerText.indexOf('\n') >= 0)) return;
                out.push(pt);
                return;
            }
            var L = linesFromTableLike(el);
            if (L.length) L.forEach(function(line) { out.push(line); });
        });
        return out.join('\n');
    })();
    """

    def _wait_for_tables_or_main(self) -> None:
        """Best-effort wait for pricing tables or grids; continues even if none match."""
        wait = WebDriverWait(self.driver, 25)
        for locator in (
            (By.CSS_SELECTOR, "main table"),
            (By.CSS_SELECTOR, "main [role='grid']"),
            (By.CSS_SELECTOR, "article table"),
            (By.CSS_SELECTOR, "[role='main'] table"),
            (By.CSS_SELECTOR, "[role='grid']"),
            (By.TAG_NAME, "table"),
        ):
            try:
                wait.until(EC.presence_of_element_located(locator))
                return
            except TimeoutException:
                continue

    def _scroll_pricing_page(self) -> None:
        """Scroll main content so lazy / below-the-fold widgets mount."""
        try:
            self.driver.execute_script(
                "var m=document.querySelector('main')||document.body;"
                "if(!m)return;"
                "var y=0,step=Math.max(400,innerHeight*0.85);"
                "function down(){"
                "m.scrollTop=Math.min(m.scrollHeight,m.scrollTop+step);"
                "y=m.scrollTop;"
                "}"
                "for(var i=0;i<60;i++){down();}"
                "m.scrollTop=0;"
            )
        except Exception:
            pass

    def _expand_all_models_buttons(self) -> int:
        """
        Click every visible control whose label is 'All models' (expand full table).
        Skips 'Fewer models' / other labels. Returns number of clicks this pass.
        """
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
        """Repeatedly click 'All models' until no new clicks or cap reached."""
        for _ in range(self.max_expand_rounds):
            n = self._expand_all_models_buttons()
            if n == 0:
                break
            time.sleep(0.25)

    def _expand_models_after_scrolling_each_table(self) -> None:
        """
        Each pricing block can own an 'All models' control; scroll every main table
        into view and run one expand pass so lower tables (e.g. 4-col extended rows)
        are not left collapsed.
        """
        if not self.driver:
            return
        try:
            tables = self.driver.find_elements(By.CSS_SELECTOR, "main table")
        except Exception:
            return
        for tbl in tables:
            try:
                if not tbl.is_displayed():
                    continue
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center',inline:'nearest'});",
                    tbl,
                )
                time.sleep(0.22)
                self._expand_all_models_until_stable()
            except Exception:
                continue

    def _expand_after_pricing_tier_change(self) -> None:
        """
        Changing Standard/Batch/Flex/Priority remounts pricing tables; each block
        shows the outline **All models** pill again. We must scroll, click every
        **All models** (not *Fewer models*), repeat per-table, several sweeps so
        extra rows load for the new tier before scraping.
        """
        if not self.driver:
            return
        time.sleep(0.5)
        for _ in range(3):
            self._scroll_pricing_page()
            time.sleep(0.3)
            self._expand_all_models_until_stable()
            self._expand_models_after_scrolling_each_table()
            time.sleep(0.35)
        self._expand_all_models_until_stable()

    def _expand_after_tab_change(self) -> None:
        """Backward-compatible alias for tier-switch expand cycle."""
        self._expand_after_pricing_tier_change()

    _DISCOVER_PRICING_LAYOUT_JS = r"""
    return JSON.stringify((function() {
        function isNestedTable(t) {
            var p = t.parentElement;
            while (p) {
                if (p.tagName === 'TABLE' && p !== t) return true;
                p = p.parentElement;
            }
            return false;
        }
        /**
         * Real document-order walk (TreeWalker). A first-child-only walk can see a
         * table before its [role=tablist] in DOM and assign owner -1, so only
         * ``default`` appears in by_pricing_tier. We then fill -1 within each H2
         * section from the nearest tablist-owned table.
         */
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
        var tablists = Array.prototype.slice.call(
            main.querySelectorAll('[role="tablist"]')
        );
        var tablistMeta = tablists.map(function(tl, idx) {
            var tabs = tl.querySelectorAll('[role="tab"]');
            var labels = [];
            for (var i = 0; i < tabs.length; i++) {
                var lab = (tabs[i].textContent || '').replace(/\s+/g, ' ').trim();
                if (lab) labels.push(lab);
            }
            return { index: idx, labels: labels };
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

    def _discover_pricing_layout(self) -> Dict[str, Any]:
        """
        Map each top-level table to its ``[role=tablist]`` (index in ``main``
        tablist order). Uses document-order TreeWalker; tables that appear before
        their tablist in DOM get the same owner as sibling tables in that H2
        section after a gap-fill pass.
        """
        if not self.driver:
            return {"tablists": [], "tableOwners": []}
        try:
            raw = self.driver.execute_script(self._DISCOVER_PRICING_LAYOUT_JS)
            data = json.loads(raw) if raw else {}
            return data if isinstance(data, dict) else {"tablists": [], "tableOwners": []}
        except Exception as e:
            print(f"Pricing layout discovery failed: {e}")
            return {"tablists": [], "tableOwners": []}

    def _click_tab_in_tablist(self, tablist_index: int, label: str) -> bool:
        if not self.driver:
            return False
        try:
            return bool(
                self.driver.execute_script(
                    self._CLICK_TAB_IN_TABLIST_JS, tablist_index, label
                )
            )
        except Exception:
            return False

    _READ_MAIN_TAB_STATE_JS = r"""
    return (function() {
        function cellText(c) {
            if (!c) return '';
            return (c.innerText || '').trim().replace(/\s+/g, ' ');
        }
        function pickSelectedTab(tl) {
            var tabs = tl.querySelectorAll('[role="tab"]');
            var j;
            for (j = 0; j < tabs.length; j++) {
                var t = tabs[j];
                var asel = t.getAttribute('aria-selected');
                if (asel === 'true') return t;
                if (t.getAttribute('data-state') === 'active') return t;
            }
            for (j = 0; j < tabs.length; j++) {
                var inner = tabs[j].querySelector('[data-state="active"]');
                if (inner) return tabs[j];
            }
            var pressed = tl.querySelector('[role="tab"][aria-pressed="true"]');
            if (pressed) return pressed;
            return tabs.length ? tabs[0] : null;
        }
        var lists = document.querySelectorAll('main [role="tablist"]');
        var out = [];
        for (var i = 0; i < lists.length; i++) {
            var tl = lists[i];
            var sel = pickSelectedTab(tl);
            out.push({
                tablist_order: i,
                label: sel ? cellText(sel) : ''
            });
        }
        return JSON.stringify(out);
    })();
    """

    def _read_main_tab_state(self) -> List[Dict[str, Any]]:
        """Labels of the selected tab in each main tablist (DOM order); not hardcoded."""
        if not self.driver:
            return []
        try:
            raw = self.driver.execute_script(self._READ_MAIN_TAB_STATE_JS)
            data = json.loads(raw) if raw else []
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _merge_tab_state_with_click(
        self,
        state: List[Dict[str, Any]],
        tablist_order: int,
        clicked_label: str,
    ) -> List[Dict[str, Any]]:
        """Record the tier we clicked (Standard/Batch/…) even when ARIA is empty."""
        out: List[Dict[str, Any]] = [dict(x) for x in state] if state else []
        while len(out) <= tablist_order:
            out.append({"tablist_order": len(out), "label": ""})
        entry = dict(out[tablist_order])
        entry["tablist_order"] = tablist_order
        lab = (clicked_label or "").strip()
        if lab:
            entry["label"] = lab
            entry["from_explicit_tier_click"] = True
        out[tablist_order] = entry
        return out

    def _snapshot_tables(
        self,
        click_context: Optional[Tuple[int, str]] = None,
    ) -> Dict[str, Any]:
        """
        Read tab strip + all tables. ``click_context`` is (strip_index, tier_label).
        """
        state = self._read_main_tab_state()
        pricing_tier: Optional[str] = None
        tier_strip_index: Optional[int] = None
        if click_context is not None:
            tlo, lab = click_context
            state = self._merge_tab_state_with_click(state, tlo, lab)
            pricing_tier = (lab or "").strip() or None
            tier_strip_index = tlo
        elif state:
            for row in state:
                lab = (row.get("label") or "").strip()
                if lab:
                    pricing_tier = lab
                    break
        return {
            "selected_tabs": state,
            "selected_tab_labels": [
                str(x.get("label") or "").strip() for x in state
            ],
            "pricing_tier": pricing_tier,
            "pricing_tier_strip_index": tier_strip_index,
            "tables": self._collect_html_tables(),
        }

    def _gather_tables_across_tabs(self) -> List[Dict[str, Any]]:
        """
        Discover which ``[role=tablist]`` controls which tables (DOM walk). For each
        tablist, switch tabs by their **visible labels** only—never click other
        tablists for that pass—so Flagship Batch prices are not reset by a lower
        section. Tables without a tablist are captured once under ``default``.
        """
        snapshots: List[Dict[str, Any]] = []

        self._scroll_pricing_page()
        time.sleep(0.4)
        self._expand_all_models_until_stable()
        self._expand_models_after_scrolling_each_table()

        if not self.click_pricing_tabs or not self.driver:
            s = self._snapshot_tables()
            s["owned_table_indices"] = None
            snapshots.append(s)
            return self._finalize_merged_tables(snapshots)

        layout = self._discover_pricing_layout()
        owners_raw = layout.get("tableOwners") or []
        owners: List[int] = []
        for ow in owners_raw:
            try:
                owners.append(int(ow))
            except (TypeError, ValueError):
                owners.append(-1)
        tablists_meta: List[Dict[str, Any]] = layout.get("tablists") or []

        if not tablists_meta:
            s = self._snapshot_tables()
            s["owned_table_indices"] = None
            snapshots.append(s)
            return self._finalize_merged_tables(snapshots)

        unowned_1based = [
            tbl_idx + 1 for tbl_idx, ow in enumerate(owners) if ow < 0
        ]
        if unowned_1based:
            base = self._snapshot_tables()
            base["pricing_tier"] = "default"
            base["owned_table_indices"] = unowned_1based
            base["controlling_tablist_index"] = None
            snapshots.append(base)

        for o, meta in enumerate(tablists_meta):
            labels = meta.get("labels") or []
            if not labels:
                continue
            owned_1based = [ti + 1 for ti, ow in enumerate(owners) if ow == o]
            if not owned_1based:
                continue
            for label in labels:
                if not self._click_tab_in_tablist(o, label):
                    continue
                time.sleep(1.15)
                self._expand_after_pricing_tier_change()
                snap = self._snapshot_tables()
                snap["pricing_tier"] = label
                snap["controlling_tablist_index"] = o
                snap["owned_table_indices"] = owned_1based
                snapshots.append(snap)

        self._scroll_pricing_page()
        self._expand_all_models_until_stable()
        return self._finalize_merged_tables(snapshots)

    def _nest_modality_subrows(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        If a table has a Modality column, merge short first-column continuation rows
        (heuristic via _looks_like_primary_model_id) under the previous row as by_modality.
        """
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

    def _merge_table_snapshots(
        self, snapshots: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        One output table per (section, headers, table_index); variants are merged
        from snapshots, then :meth:`_collapse_modes_to_by_pricing_tier` rewrites
        them to ``by_pricing_tier``.

        Output order follows the first snapshot pass (DOM ``querySelectorAll``
        order), not alphabetical section titles — so the list starts at the
        first table on the page.
        """
        bucket: Dict[Tuple[str, Tuple[str, ...], int], List[Dict[str, Any]]] = (
            defaultdict(list)
        )
        key_order: List[Tuple[str, Tuple[str, ...], int]] = []
        seen_keys: set = set()
        for snap in snapshots:
            state = snap.get("selected_tabs") or []
            tab_labels = snap.get("selected_tab_labels")
            ptier = snap.get("pricing_tier")
            ptidx = snap.get("pricing_tier_strip_index")
            owned_raw = snap.get("owned_table_indices")
            allowed: Optional[set] = None
            if owned_raw is not None:
                if not owned_raw:
                    continue
                try:
                    allowed = {int(x) for x in owned_raw}
                except (TypeError, ValueError):
                    continue
            tables = snap.get("tables") or []
            for tbl in tables:
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
                bucket[key].append(
                    {
                        "selected_tabs": state,
                        "selected_tab_labels": tab_labels,
                        "pricing_tier": ptier,
                        "pricing_tier_strip_index": ptidx,
                        "rows": tbl.get("rows") or [],
                    }
                )

        merged: List[Dict[str, Any]] = []
        for (sec, heads, tidx) in key_order:
            variants = bucket[(sec, heads, tidx)]
            seen_sig: set = set()
            modes: List[Dict[str, Any]] = []
            for v in variants:
                try:
                    sig = json.dumps(v, sort_keys=True, ensure_ascii=False)
                except (TypeError, ValueError):
                    sig = repr(v)
                if sig in seen_sig:
                    continue
                seen_sig.add(sig)
                modes.append(v)

            merged.append(
                {
                    "section_heading": sec,
                    "headers": list(heads),
                    "table_index": tidx,
                    "pricing_modes": modes,
                }
            )
        return merged

    def _collapse_modes_to_by_pricing_tier(
        self, merged: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Replace repeated ``pricing_modes`` blobs with::

            "by_pricing_tier": {
                "Standard": {"rows": [...]},
                "Batch": {"rows": [...]},
            }
        """
        for block in merged:
            modes = block.pop("pricing_modes", None) or []
            by_tier: Dict[str, Dict[str, Any]] = {}
            for m in modes:
                tier = (m.get("pricing_tier") or "").strip()
                if not tier and m.get("selected_tab_labels"):
                    tier = str(m["selected_tab_labels"][0] or "").strip()
                if not tier:
                    tier = "default"
                raw_rows = m.get("rows") or []
                rows = (
                    self._nest_modality_subrows(
                        [r for r in raw_rows if isinstance(r, dict)]
                    )
                    if isinstance(raw_rows, list)
                    else []
                )
                by_tier[tier] = {"rows": rows}
            block["by_pricing_tier"] = by_tier
        return merged

    def _finalize_merged_tables(
        self, snapshots: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        merged = self._merge_table_snapshots(snapshots)
        merged = self._collapse_modes_to_by_pricing_tier(merged)
        return merged

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
        /** One entry per visual column (honours colspan). */
        function expandHeaderRow(tr) {
            var out = [];
            Array.prototype.forEach.call(tr.querySelectorAll('th,td'), function(cell) {
                var t = cellText(cell);
                var cs = colspanOf(cell);
                for (var i = 0; i < cs; i++) out.push(t);
            });
            return out;
        }
        /**
         * Merge multi-row <thead> (e.g. group row "Short context" colspan=3 +
         * leaf row Model / Input / …) into one header per column.
         */
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
        var root = document.querySelector('main') || document.body;
        var tables = root.querySelectorAll('table');
        var out = [];
        var idx = 0;
        Array.prototype.forEach.call(tables, function(tbl) {
            if (isNestedTable(tbl)) return;
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

    def _collect_html_tables(self) -> List[Dict[str, Any]]:
        """Parse every top-level <table> in main into headers + row dicts."""
        if not self.driver:
            return []
        try:
            raw = self.driver.execute_script(self._EXTRACT_HTML_TABLES_JS)
            data = json.loads(raw) if raw else []
            return data if isinstance(data, list) else []
        except Exception as e:
            print(f"HTML table extraction failed: {e}")
            return []

    def _extract_pricing_text_from_dom(self) -> Optional[str]:
        """
        Build pipe-table markdown from the live page: headings, short <p> tier labels,
        HTML <table> rows, and ARIA grid/table rows. OpenAI docs use React grids, not
        only classic tables.
        """
        self._wait_for_tables_or_main()
        time.sleep(1.5)
        try:
            text = self.driver.execute_script(self._EXTRACT_PRICING_MARKDOWN_JS)
        except Exception as e:
            print(f"DOM extraction script failed: {e}")
            return None
        if not text or len(text.strip()) < 80:
            return None
        return text

    def _parse_table_section(
        self, lines: List[str], start_idx: int
    ) -> Tuple[List[Dict[str, str]], int, List[str]]:
        """
        Parse one pipe-markdown table. Returns (row dicts, next index, headers).
        """
        data: List[Dict[str, str]] = []
        idx = start_idx
        while idx < len(lines) and not lines[idx].strip().startswith('|'):
            idx += 1
        if idx >= len(lines):
            return data, idx, []
        headers = [h.strip() for h in lines[idx].split('|')[1:-1]]
        idx += 1
        if idx < len(lines) and '|--' in lines[idx]:
            idx += 1
        while idx < len(lines):
            line = lines[idx].strip()
            if not line or not line.startswith('|'):
                break
            if '|--' in line:
                idx += 1
                continue
            row = [cell.strip() for cell in line.split('|')[1:-1]]
            if len(row) == len(headers):
                data.append(dict(zip(headers, row)))
            idx += 1
        return data, idx, headers

    def _parse_pricing_text(self, text: str) -> Dict[str, Any]:
        """
        Single linear pass: rolling non-table lines are free-form context; each
        markdown table becomes one block. Works for any future doc sections.
        """
        lines = text.strip().split('\n')
        context_lines: List[str] = []
        blocks: List[Dict[str, Any]] = []
        max_ctx = self._MARKDOWN_CONTEXT_MAX_LINES

        def push_context(s: str) -> None:
            if not s or s.startswith('|'):
                return
            if len(s) > 200:
                return
            context_lines.append(s)
            while len(context_lines) > max_ctx:
                context_lines.pop(0)

        idx = 0
        while idx < len(lines):
            s = lines[idx].strip()
            if not s:
                idx += 1
                continue
            if s.startswith('|'):
                rows, new_idx, headers = self._parse_table_section(lines, idx)
                blocks.append(
                    {
                        "context_before_table": list(context_lines),
                        "headers": headers,
                        "rows": rows,
                        "row_count": len(rows),
                    }
                )
                idx = new_idx
                continue
            push_context(s)
            idx += 1

        return {"markdown_tables": blocks}
    
    def _model_name_from_row(self, row: Dict[str, str]) -> Optional[str]:
        for key, val in row.items():
            if key.strip().lower() == 'model':
                return val.strip() if isinstance(val, str) else val
        for key, val in row.items():
            if 'model' in key.lower():
                return val.strip() if isinstance(val, str) else val
        if row:
            first = next(iter(row.values()))
            return first.strip() if isinstance(first, str) else first
        return None

    def _row_to_pricing_tier_and_extras(
        self, row: Dict[str, str]
    ) -> Tuple[PricingTier, Optional[Dict[str, Any]]]:
        """
        Map row keys to PricingTier fields heuristically; unknown columns go to extras.
        """
        input_p: Optional[str] = None
        cached_p: Optional[str] = None
        output_p: Optional[str] = None
        extras: Dict[str, Any] = {}

        for key, val in row.items():
            lk = key.lower().strip().replace(' ', '_')
            if lk == 'model':
                continue
            if 'cached' in lk and 'input' in lk:
                cached_p = cached_p or val
            elif 'output' in lk:
                output_p = output_p or val
            elif lk == 'input' or lk.endswith('_input'):
                if 'cached' not in lk and 'cache' not in lk:
                    input_p = input_p or val
            elif 'input' in lk and 'cached' not in lk and 'cache' not in lk:
                input_p = input_p or val
            else:
                extras[key] = val

        tier = PricingTier(
            input_price=input_p,
            cached_input_price=cached_p,
            output_price=output_p,
        )
        return tier, (extras if extras else None)

    def _convert_to_model_pricing_objects(self, data: Dict[str, Any]) -> List[ModelPricing]:
        """
        Build ModelPricing rows from generic ``markdown_tables`` blocks (context path
        used as category label).
        """
        model_pricing_list: List[ModelPricing] = []
        for block in data.get("markdown_tables") or []:
            if not isinstance(block, dict):
                continue
            ctx = block.get("context_before_table") or []
            category = "markdown » " + " » ".join(str(x) for x in ctx[-3:]) if ctx else "markdown"
            for model_data in block.get("rows") or []:
                if not isinstance(model_data, dict):
                    continue
                name = self._model_name_from_row(model_data)
                if not name:
                    continue
                pricing_tier, extras = self._row_to_pricing_tier_and_extras(model_data)
                model_pricing_list.append(
                    ModelPricing(
                        model_name=name,
                        pricing_tiers={"table": pricing_tier},
                        category=category,
                        additional_info=extras,
                    )
                )
        return model_pricing_list
    
    def scrape_all_model_data(self) -> Dict[str, Any]:
        """
        Main public method to scrape all pricing data from OpenAI.
        
        Returns:
            Complete pricing data as a dictionary
        """
        try:
            self._setup_driver()
            self.driver.get(self.pricing_url)

            self._wait_for_pricing_content()
            time.sleep(1.2)

            self.extracted_tables = self._gather_tables_across_tabs()
            dom_text = self._extract_pricing_text_from_dom()
            copy_text = self._click_copy_button()
            raw_text = self._pick_best_pricing_text(dom_text, copy_text)
            if not raw_text:
                print("Failed to retrieve pricing data (DOM and copy both empty)")
                return {}

            structured_data = self._parse_pricing_text(raw_text)
            
            # Store the data
            self.pricing_data = {
                'provider': 'openai',
                'last_updated': time.strftime('%Y-%m-%d %H:%M:%S'),
                'source_url': self.pricing_url,
                'extracted_tables': self.extracted_tables,
                'pricing_data': structured_data
            }
            
            return self.pricing_data
            
        except Exception as e:
            print(f"Error during scraping: {e}")
            return {}
            
        finally:
            if self.driver:
                self.driver.quit()
    
    def save_to_json(self, filename: str = 'openai_pricing.json') -> None:
        """
        Save scraped data to a JSON file.
        
        Args:
            filename: Output filename for JSON data
        """
        if not self.pricing_data:
            print("No data to save. Run scrape_all_model_data() first.")
            return
        
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(self.pricing_data, f, indent=2, ensure_ascii=False)
        
        print(f"Pricing data saved to {filename}")


def main():
    """Main execution function."""
    scraper = OpenAIScraper()
    
    print("Starting OpenAI pricing scraper...")
    data = scraper.scrape_all_model_data()
    
    if data:
        print("\nScraping completed successfully!")
        scraper.save_to_json()
        
        # Print summary with better detail
        print("\nData Summary:")
        et = data.get("extracted_tables") or []
        print(f"  extracted_tables: {len(et)} merged HTML table(s)")
        pd = data.get("pricing_data") or {}
        mtables = pd.get("markdown_tables") or []
        print(f"  pricing_data.markdown_tables: {len(mtables)} block(s)")
        total_md_rows = sum(int(b.get("row_count") or 0) for b in mtables if isinstance(b, dict))
        print(f"  markdown table rows (total): {total_md_rows}")
    else:
        print("Scraping failed. Please check the logs.")


if __name__ == "__main__":
    main()







