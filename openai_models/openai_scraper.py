import json
import time

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options


class OpenAIScraper:
    def __init__(self):
        chrome_options = Options()

        # chrome_options.add_argument('--headless')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')

        # Grant clipboard permissions
        prefs = {
            "profile.default_content_setting_values.clipboard": 1,
            "profile.content_settings.exceptions.clipboard": {
                "https://platform.openai.com,*": {"setting": 1}
            }
        }
        chrome_options.add_experimental_option("prefs", prefs)
        chrome_options.add_argument('--enable-clipboard')
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')

        self._chrome_options = chrome_options
        self._chrome_driver = None

    def _init_chrome_driver(self):
        self._chrome_driver = webdriver.Chrome(options=self._chrome_options)

    def _find_copy_button(self):
        print("Navigating to OpenAI pricing page...")
        self._chrome_driver.get("https://platform.openai.com/docs/pricing")

        # Grant clipboard permissions via CDP
        self._chrome_driver.execute_cdp_cmd('Browser.grantPermissions', {
            'permissions': ['clipboardReadWrite', 'clipboardSanitizedWrite'],
            'origin': 'https://platform.openai.com'
        })

        print("Waiting for page to load...")
        time.sleep(5)

        print("Looking for copy button...")
        copy_button = None
        selectors = [
            "button.lkCln.copy-button",
            "button.copy-button",
            "button[class*='copy']",
            "//button[contains(@class, 'copy')]"
        ]

        wait = WebDriverWait(self._chrome_driver, 15)

        for selector in selectors:
            try:
                if selector.startswith("//"):
                    copy_button = wait.until(EC.element_to_be_clickable((By.XPATH, selector)))
                else:
                    copy_button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
                print(f"✓ Found button: {selector}")
                break
            except:
                continue

        return copy_button

    def _get_page_text_from_html(self):
        content_div = self._chrome_driver.find_element(By.TAG_NAME, "main")
        page_text = content_div.text
        return page_text

    def _get_page_text_from_clipboard(self):
        copy_button = self._find_copy_button()

        if not copy_button:
            return None

        print("Clicking copy button...")
        self._chrome_driver.execute_script("arguments[0].scrollIntoView(true);", copy_button)
        time.sleep(1)
        self._chrome_driver.execute_script("arguments[0].click();", copy_button)

        print("Reading clipboard...")
        time.sleep(3)

        clipboard_content = self._chrome_driver.execute_async_script("""
            var callback = arguments[arguments.length - 1];
            navigator.clipboard.readText()
                .then(text => callback(text))
                .catch(err => callback('ERROR: ' + err));
        """)

        if not clipboard_content.startswith('ERROR:'):
            return clipboard_content

        return None

    def _get_page_text(self):
        clipboard_content = self._get_page_text_from_clipboard()
        if clipboard_content:
            return clipboard_content

        return self._get_page_text_from_html()

    @staticmethod
    def _process_image_generation(data, text):
        """Special parsing for Image generation with multi-row model entries"""
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
                # Skip "Prices per image" description
                if "Prices per image" in line:
                    i += 1
                    continue

                # Skip header rows
                if "|Model|Quality|" in line or "|---|" in line or line == "||":
                    i += 1
                    continue

                # Parse data rows
                if line.startswith("|") and line != "||":
                    cells = [c.strip() for c in line.split('|')[1:-1]]

                    # Skip empty rows
                    if not any(cells):
                        i += 1
                        continue

                    # Row with model name in first column
                    if len(cells) >= 4 and cells[0] in ["GPT Image 1", "GPT Image 1 Mini", "DALL·E 3", "DALL·E 2"]:
                        # This is a row with model name
                        model_display_name = cells[0]
                        if model_display_name == "GPT Image 1":
                            current_model = "gpt-image-1"
                        elif model_display_name == "GPT Image 1 Mini":
                            current_model = "gpt-image-1-mini"
                        else:
                            current_model = model_display_name

                        if current_model not in result:
                            result[current_model] = {}

                        quality = cells[1]  # Quality is in second column
                        result[current_model][quality] = {
                            "1024x1024": cells[2] if len(cells) > 2 else "-",
                            "1024x1536": cells[3] if len(cells) > 3 else "-",
                            "1536x1024": cells[4] if len(cells) > 4 else "-"
                        }

                    # Row with only quality (continuation of previous model)
                    elif len(cells) >= 4 and cells[0] and current_model:
                        # This is a quality row without model name
                        quality = cells[0]
                        result[current_model][quality] = {
                            "1024x1024": cells[1] if len(cells) > 1 else "-",
                            "1024x1536": cells[2] if len(cells) > 2 else "-",
                            "1536x1024": cells[3] if len(cells) > 3 else "-"
                        }

            i += 1

        if len(result) > 1:
            data["sections"]["Image generation"] = result

    @staticmethod
    def _process_video_section(data):
        """Fix Video section to handle duplicate model names"""
        if "Video" not in data["sections"] or "models" not in data["sections"]["Video"]:
            return

        models = data["sections"]["Video"]["models"]

        # If sora-2-pro appears multiple times, convert to list
        if "sora-2-pro" in models:
            # The current parsing only captured the last one, we need to reconstruct from raw data
            # For now, we'll keep the structure but this should be handled in the main parsing
            pass

    @staticmethod
    def _process_built_in_tools(data):
        """Convert Built-in tools from models dict to direct tool dict"""
        if "Built-in tools" in data["sections"] and "models" in data["sections"]["Built-in tools"]:
            tools = data["sections"]["Built-in tools"].copy()
            del tools["models"]
            for tool, values in data["sections"]["Built-in tools"]["models"].items():
                tools[tool] = values
            data["sections"]["Built-in tools"] = tools

    def _get_structured_data_from_page_text(self, text):
        data = {
            "source": "https://platform.openai.com/docs/pricing",
            "sections": {}
        }

        lines = [line for line in text.split('\n') if line.strip()]

        i = 0
        current_section = None
        current_tier = None
        current_subsection = None
        headers = []

        while i < len(lines):
            line = lines[i].strip()

            # Skip header lines
            if line in ["Pricing", "===", "###"]:
                i += 1
                continue

            # Detect main section headers
            if line in ["Text tokens", "Image tokens", "Audio tokens", "Video", "Fine-tuning",
                        "Built-in tools", "AgentKit", "Transcription and speech generation",
                        "Image generation", "Embeddings", "Moderation", "Legacy models"]:
                current_section = line
                data["sections"][current_section] = {}
                current_tier = None
                current_subsection = None
                headers = []
                i += 1

                # Check for description
                if i < len(lines) and ("Prices per" in lines[i] or lines[i].startswith("Build, deploy")):
                    data["sections"][current_section]["description"] = lines[i].strip()
                    i += 1
                continue

            # Detect tier headers
            if line in ["Batch", "Flex", "Standard", "Priority"]:
                current_tier = line
                if current_section:
                    if "tiers" not in data["sections"][current_section]:
                        data["sections"][current_section]["tiers"] = {}
                    data["sections"][current_section]["tiers"][current_tier] = {}
                headers = []
                current_subsection = None
                i += 1
                continue

            # Detect subsection headers
            if line.startswith("####"):
                current_subsection = line.replace("#### ", "").strip()
                if current_section:
                    data["sections"][current_section][current_subsection] = {}
                headers = []
                i += 1
                continue

            # Parse table headers
            if line.startswith("|") and "|---|" not in line and not headers:
                headers = [h.strip() for h in line.split('|')[1:-1]]
                i += 1
                # Skip separator line
                if i < len(lines) and "|---|" in lines[i]:
                    i += 1
                continue

            # Parse table data rows
            if line.startswith("|") and "|---|" not in line and headers:
                cells = [c.strip() for c in line.split('|')[1:-1]]

                if len(cells) > 0 and cells[0]:
                    model_name = cells[0]
                    row_data = {}

                    # Map cells to headers
                    for j in range(1, min(len(cells), len(headers))):
                        if headers[j]:
                            row_data[headers[j]] = cells[j] if cells[j] else "-"

                    # Store based on context
                    if current_subsection:
                        data["sections"][current_section][current_subsection][model_name] = row_data
                    elif current_tier and current_section:
                        data["sections"][current_section]["tiers"][current_tier][model_name] = row_data
                    elif current_section:
                        if "models" not in data["sections"][current_section]:
                            data["sections"][current_section]["models"] = {}
                        data["sections"][current_section]["models"][model_name] = row_data

                i += 1
                continue

            i += 1

        return data

    def _parse_pricing_data(self, text):
        """Parse the copied pricing data into structured JSON"""
        data = self._get_structured_data_from_page_text(text)

        # Post-process specific sections
        self._process_image_generation(data, text)
        self._process_video_section(data)
        self._process_built_in_tools(data)

        return data

    def scrape_all(self):
        self._init_chrome_driver()

        try:
            page_text = self._get_page_text()
            structured_data = self._parse_pricing_data(page_text)
            return structured_data
        except Exception as e:
            print(f"\n❌ Error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            print("\nClosing browser...")
            time.sleep(2)
            self._chrome_driver.quit()
            self._chrome_driver = None


if __name__ == "__main__":
    scraper = OpenAIScraper()
    data = scraper.scrape_all()
    print(json.dumps(data, indent=4))
