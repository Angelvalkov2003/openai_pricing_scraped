"""
OpenAI Pricing Scraper
Conforms to PEP8 and The Zen of Python principles.
"""
import json
import time
import traceback
from typing import Dict, List, Optional

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


class OpenAIScraper:
    """Scraper for OpenAI pricing documentation."""
    
    BASE_URL = "https://platform.openai.com/docs/pricing"
    
    def __init__(self, headless: bool = True):
        """
        Initialize the OpenAI scraper.
        
        Args:
            headless: Whether to run browser in headless mode
        """
        self.headless = headless
        self.driver = None
    
    def scrape_all_model_data(self) -> Dict:
        """
        Main public method to scrape all OpenAI pricing data.
        
        Returns:
            Dictionary containing structured pricing data
        """
        try:
            self._setup_driver()
            self._navigate_to_page()
            raw_content = self._extract_page_content()
            structured_data = self._parse_pricing_data(raw_content)
            return structured_data
        except Exception as e:
            print(f"Error during scraping: {e}")
            traceback.print_exc()
            return {}
        finally:
            self._cleanup()
    
    def _setup_driver(self) -> None:
        """Initialize Chrome WebDriver with appropriate options."""
        chrome_options = Options()
        # if self.headless:
        #     chrome_options.add_argument('--headless')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        
        prefs = {
            "profile.default_content_setting_values.clipboard": 1,
            "profile.content_settings.exceptions.clipboard": {
                "https://platform.openai.com,*": {"setting": 1}
            }
        }
        chrome_options.add_experimental_option("prefs", prefs)
        chrome_options.add_argument('--enable-clipboard')
        
        self.driver = webdriver.Chrome(options=chrome_options)
    
    def _navigate_to_page(self) -> None:
        """Navigate to OpenAI pricing page and wait for load."""
        print(f"Navigating to {self.BASE_URL}...")
        self.driver.get(self.BASE_URL)
        
        self.driver.execute_cdp_cmd('Browser.grantPermissions', {
            'permissions': ['clipboardReadWrite', 'clipboardSanitizedWrite'],
            'origin': 'https://platform.openai.com'
        })
        
        print("Waiting for page to load...")
        time.sleep(5)
    
    def _extract_page_content(self) -> str:
        """
        Extract content from the page via copy button or direct scrape.
        
        Returns:
            Raw text content from the page
        """
        copy_button = self._find_copy_button()
        
        if not copy_button:
            print("Copy button not found. Scraping directly...")
            return self._scrape_directly()
        
        return self._extract_via_clipboard(copy_button)
    
    def _find_copy_button(self) -> Optional[object]:
        """
        Attempt to locate the copy button using multiple selectors.
        
        Returns:
            WebElement if found, None otherwise
        """
        print("Looking for copy button...")
        selectors = [
            "button.lkCln.copy-button",
            "button.copy-button",
            "button[class*='copy']",
            "//button[contains(@class, 'copy')]"
        ]
        
        wait = WebDriverWait(self.driver, 15)
        
        for selector in selectors:
            try:
                if selector.startswith("//"):
                    elem = wait.until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                else:
                    elem = wait.until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                    )
                print(f"✓ Found button: {selector}")
                return elem
            except Exception:
                continue
        
        return None
    
    def _scrape_directly(self) -> str:
        """Fallback method to scrape content directly from page."""
        content_div = self.driver.find_element(By.TAG_NAME, "main")
        return content_div.text
    
    def _extract_via_clipboard(self, copy_button: object) -> str:
        """
        Extract content by clicking copy button and reading clipboard.
        
        Args:
            copy_button: WebElement of the copy button
            
        Returns:
            Clipboard content as string
        """
        print("Clicking copy button...")
        self.driver.execute_script(
            "arguments[0].scrollIntoView(true);", copy_button
        )
        time.sleep(1)
        self.driver.execute_script("arguments[0].click();", copy_button)
        
        print("Reading clipboard...")
        time.sleep(3)
        
        try:
            content = self.driver.execute_async_script("""
                var callback = arguments[arguments.length - 1];
                navigator.clipboard.readText()
                    .then(text => callback(text))
                    .catch(err => callback('ERROR: ' + err));
            """)
            
            if content.startswith('ERROR:'):
                raise Exception(content)
            
            print(f"✓ Clipboard read ({len(content)} chars)")
            return content
        except Exception as e:
            print(f"Clipboard read failed: {e}")
            try:
                import pyperclip
                content = pyperclip.paste()
                print(f"✓ Used pyperclip ({len(content)} chars)")
                return content
            except Exception:
                print("Falling back to direct scrape...")
                return self._scrape_directly()
    
    def _parse_pricing_data(self, text: str) -> Dict:
        """
        Parse raw text into structured pricing data.
        
        Args:
            text: Raw text content from page
            
        Returns:
            Structured dictionary of pricing information
        """
        data = {
            "source": self.BASE_URL,
            "sections": {}
        }
        
        lines = [line for line in text.split('\n') if line.strip()]
        
        i = 0
        current_section = None
        current_tier = None
        current_subsection = None
        headers = []
        
        section_names = [
            "Text tokens", "Image tokens", "Audio tokens", "Video",
            "Fine-tuning", "Built-in tools", "AgentKit",
            "Transcription and speech generation", "Image generation",
            "Embeddings", "Moderation", "Legacy models"
        ]
        tier_names = ["Batch", "Flex", "Standard", "Priority"]
        
        while i < len(lines):
            line = lines[i].strip()
            
            if line in ["Pricing", "===", "###"]:
                i += 1
                continue
            
            if line in section_names:
                current_section = line
                data["sections"][current_section] = {}
                current_tier = None
                current_subsection = None
                headers = []
                i += 1
                
                if i < len(lines) and (
                    "Prices per" in lines[i] or
                    lines[i].startswith("Build, deploy")
                ):
                    data["sections"][current_section]["description"] = \
                        lines[i].strip()
                    i += 1
                continue
            
            if line in tier_names:
                current_tier = line
                if current_section:
                    if "tiers" not in data["sections"][current_section]:
                        data["sections"][current_section]["tiers"] = {}
                    data["sections"][current_section]["tiers"][current_tier] = {}
                headers = []
                current_subsection = None
                i += 1
                continue
            
            if line.startswith("####"):
                current_subsection = line.replace("#### ", "").strip()
                if current_section:
                    data["sections"][current_section][current_subsection] = {}
                headers = []
                i += 1
                continue
            
            if line.startswith("|") and "|---|" not in line and not headers:
                headers = [h.strip() for h in line.split('|')[1:-1]]
                i += 1
                if i < len(lines) and "|---|" in lines[i]:
                    i += 1
                continue
            
            if line.startswith("|") and "|---|" not in line and headers:
                self._parse_table_row(
                    line, headers, data, current_section,
                    current_tier, current_subsection
                )
            
            i += 1
        
        self._post_process_sections(data, text)
        return data
    
    def _parse_table_row(
        self, line: str, headers: List[str], data: Dict,
        current_section: Optional[str], current_tier: Optional[str],
        current_subsection: Optional[str]
    ) -> None:
        """Parse a single table row and add to data structure."""
        cells = [c.strip() for c in line.split('|')[1:-1]]
        
        if len(cells) > 0 and cells[0]:
            model_name = cells[0]
            row_data = {}
            
            for j in range(1, min(len(cells), len(headers))):
                if headers[j]:
                    row_data[headers[j]] = cells[j] if cells[j] else "-"
            
            if current_subsection:
                data["sections"][current_section][current_subsection][
                    model_name
                ] = row_data
            elif current_tier and current_section:
                data["sections"][current_section]["tiers"][current_tier][
                    model_name
                ] = row_data
            elif current_section:
                if "models" not in data["sections"][current_section]:
                    data["sections"][current_section]["models"] = {}
                
                if (current_section == "Video" and
                    model_name in data["sections"][current_section]["models"]):
                    existing = data["sections"][current_section]["models"][
                        model_name
                    ]
                    if not isinstance(existing, list):
                        data["sections"][current_section]["models"][
                            model_name
                        ] = [existing]
                    data["sections"][current_section]["models"][
                        model_name
                    ].append(row_data)
                else:
                    data["sections"][current_section]["models"][
                        model_name
                    ] = row_data
    
    def _post_process_sections(self, data: Dict, text: str) -> None:
        """Apply post-processing fixes to specific sections."""
        self._process_image_generation(data, text)
        self._process_built_in_tools(data)
        self._process_moderation(data)
    
    def _process_image_generation(self, data: Dict, text: str) -> None:
        """Parse Image generation section with multi-row model entries."""
        if "Image generation" not in data["sections"]:
            return
        
        lines = text.split('\n')
        result = {"description": "Prices per image."}
        
        in_section = False
        current_model = None
        
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            
            if line == "Image generation":
                in_section = True
                i += 1
                continue
            
            if in_section and line in ["Embeddings", "### Moderation", "###"]:
                break
            
            if in_section:
                if "Prices per image" in line:
                    i += 1
                    continue
                
                if ("|Model|Quality|" in line or "|---|" in line or
                    line == "||"):
                    i += 1
                    continue
                
                if line.startswith("|") and line != "||":
                    cells = [c.strip() for c in line.split('|')[1:-1]]
                    
                    if not any(cells):
                        i += 1
                        continue
                    
                    model_map = {
                        "GPT Image 1": "gpt-image-1",
                        "GPT Image 1 Mini": "gpt-image-1-mini"
                    }
                    
                    if len(cells) >= 4 and cells[0] in [
                        "GPT Image 1", "GPT Image 1 Mini", "DALL·E 3",
                        "DALL·E 2"
                    ]:
                        model_display_name = cells[0]
                        current_model = model_map.get(
                            model_display_name, model_display_name
                        )
                        
                        if current_model not in result:
                            result[current_model] = {}
                        
                        quality = cells[1]
                        result[current_model][quality] = {
                            "1024x1024": cells[2] if len(cells) > 2 else "-",
                            "1024x1536": cells[3] if len(cells) > 3 else "-",
                            "1536x1024": cells[4] if len(cells) > 4 else "-"
                        }
                    
                    elif len(cells) >= 4 and cells[0] and current_model:
                        quality = cells[0]
                        result[current_model][quality] = {
                            "1024x1024": cells[1] if len(cells) > 1 else "-",
                            "1024x1536": cells[2] if len(cells) > 2 else "-",
                            "1536x1024": cells[3] if len(cells) > 3 else "-"
                        }
            
            i += 1
        
        if len(result) > 1:
            data["sections"]["Image generation"] = result
    
    def _process_built_in_tools(self, data: Dict) -> None:
        """Convert Built-in tools from models dict to direct tool dict."""
        if ("Built-in tools" in data["sections"] and
            "models" in data["sections"]["Built-in tools"]):
            tools = data["sections"]["Built-in tools"].copy()
            del tools["models"]
            for tool, values in data["sections"]["Built-in tools"][
                "models"
            ].items():
                tools[tool] = values
            data["sections"]["Built-in tools"] = tools
    
    def _process_moderation(self, data: Dict) -> None:
        """Add note for Moderation section."""
        if "Moderation" in data["sections"]:
            if (not data["sections"]["Moderation"] or
                len(data["sections"]["Moderation"]) == 0):
                data["sections"]["Moderation"] = {
                    "note": "Our `omni-moderation` models are made "
                            "available free of charge."
                }
    
    def _cleanup(self) -> None:
        """Clean up resources and close browser."""
        if self.driver:
            print("\nClosing browser...")
            time.sleep(2)
            self.driver.quit()


def main():
    """Main entry point for the scraper."""
    print("=" * 70)
    print("OpenAI Pricing Scraper")
    print("=" * 70)
    print()
    
    scraper = OpenAIScraper(headless=True)
    data = scraper.scrape_all_model_data()
    
    if data:
        with open("openai_pricing.json", 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        print(f"\n✅ Success! Data saved to openai_pricing.json")
        print(f"\nSections: {list(data['sections'].keys())}")
        
        for section, content in data['sections'].items():
            if isinstance(content, dict):
                if 'tiers' in content:
                    for tier, models in content['tiers'].items():
                        print(f"  • {section} > {tier}: {len(models)} models")
                elif 'models' in content:
                    count = sum(
                        len(v) if isinstance(v, list) else 1
                        for v in content['models'].values()
                    )
                    print(f"  • {section}: {count} items")
                elif section == "Image generation" and len(content) > 1:
                    for model in content:
                        if (model != "description" and
                            isinstance(content[model], dict)):
                            print(
                                f"  • {section} > {model}: "
                                f"{len(content[model])} qualities"
                            )


if __name__ == "__main__":
    main()