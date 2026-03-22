from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
import json
import time


def parse_pricing_data(text):
    """Parse the copied pricing data into structured JSON"""
    
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
    
    # Post-process specific sections
    process_image_generation(data, text)
    process_video_section(data)
    process_built_in_tools(data)
    process_moderation(data)
    
    return data

def process_image_generation(data, text):
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

def process_video_section(data):
    """Fix Video section to handle duplicate model names"""
    if "Video" not in data["sections"] or "models" not in data["sections"]["Video"]:
        return
    
    models = data["sections"]["Video"]["models"]
    
    # If sora-2-pro appears multiple times, convert to list
    if "sora-2-pro" in models:
        # The current parsing only captured the last one, we need to reconstruct from raw data
        # For now, we'll keep the structure but this should be handled in the main parsing
        pass

def process_built_in_tools(data):
    """Convert Built-in tools from models dict to direct tool dict"""
    if "Built-in tools" in data["sections"] and "models" in data["sections"]["Built-in tools"]:
        tools = data["sections"]["Built-in tools"].copy()
        del tools["models"]
        for tool, values in data["sections"]["Built-in tools"]["models"].items():
            tools[tool] = values
        data["sections"]["Built-in tools"] = tools

def process_moderation(data):
    """Add note for Moderation section"""
    if "Moderation" in data["sections"]:
        if not data["sections"]["Moderation"] or len(data["sections"]["Moderation"]) == 0:
            data["sections"]["Moderation"] = {
                "note": "Our `omni-moderation` models are made available free of charge."
            }

def save_to_file(text, filename="clipboard_data.txt"):
    """Save content to file"""
    with open(filename, 'w', encoding='utf-8') as f:
        f.write(text)
    print(f"Content saved to {filename}")

def main():
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
    
    driver = webdriver.Chrome(options=chrome_options)
    
    try:
        print("Navigating to OpenAI pricing page...")
        driver.get("https://platform.openai.com/docs/pricing")
        
        # Grant clipboard permissions via CDP
        driver.execute_cdp_cmd('Browser.grantPermissions', {
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
        
        wait = WebDriverWait(driver, 15)
        
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
        
        if not copy_button:
            print("❌ Copy button not found. Scraping directly...")
            content_div = driver.find_element(By.TAG_NAME, "main")
            page_text = content_div.text
            save_to_file(page_text, "scraped_content.txt")
            
            structured_data = parse_pricing_data(page_text)
            
            with open("openai_pricing.json", 'w', encoding='utf-8') as f:
                json.dump(structured_data, f, indent=2, ensure_ascii=False)
            
            print(f"\n✅ Success! Data saved to openai_pricing.json")
            return
        
        print("Clicking copy button...")
        driver.execute_script("arguments[0].scrollIntoView(true);", copy_button)
        time.sleep(1)
        driver.execute_script("arguments[0].click();", copy_button)
        
        print("Reading clipboard...")
        time.sleep(3)
        
        try:
            clipboard_content = driver.execute_async_script("""
                var callback = arguments[arguments.length - 1];
                navigator.clipboard.readText()
                    .then(text => callback(text))
                    .catch(err => callback('ERROR: ' + err));
            """)
            
            if clipboard_content.startswith('ERROR:'):
                raise Exception(clipboard_content)
            
            print(f"✓ Clipboard read ({len(clipboard_content)} chars)")
            save_to_file(clipboard_content)
            
        except Exception as e:
            print(f"Clipboard read failed: {e}")
            try:
                import pyperclip
                clipboard_content = pyperclip.paste()
                print(f"✓ Used pyperclip ({len(clipboard_content)} chars)")
                save_to_file(clipboard_content)
            except:
                print("Using direct scrape...")
                content_div = driver.find_element(By.TAG_NAME, "main")
                clipboard_content = content_div.text
                save_to_file(clipboard_content, "scraped_content.txt")
        
        print("\nParsing data...")
        structured_data = parse_pricing_data(clipboard_content)
        
        with open("openai_pricing.json", 'w', encoding='utf-8') as f:
            json.dump(structured_data, f, indent=2, ensure_ascii=False)
        
        print(f"\n✅ Success! Data saved to openai_pricing.json")
        print(f"\nSections: {list(structured_data['sections'].keys())}")
        
        # Print summary
        for section, content in structured_data['sections'].items():
            if isinstance(content, dict):
                if 'tiers' in content:
                    for tier, models in content['tiers'].items():
                        print(f"  • {section} > {tier}: {len(models)} models")
                elif 'models' in content:
                    print(f"  • {section}: {len(content['models'])} items")
                elif section == "Image generation" and len(content) > 1:
                    for model in content:
                        if model != "description" and isinstance(content[model], dict):
                            print(f"  • {section} > {model}: {len(content[model])} qualities")
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        print("\nClosing browser...")
        time.sleep(2)
        driver.quit()

if __name__ == "__main__":
    print("=" * 70)
    print("OpenAI Pricing Scraper")
    print("=" * 70)
    print()
    main()