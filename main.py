# scraper.py
# Install: pip install fastapi uvicorn playwright
# Then: playwright install chromium
import os, re, asyncio, json
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.async_api import async_playwright

app = FastAPI()

# Allow Next.js to call this
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Persistent browser context to avoid re-login
STORAGE_STATE = "qwen_auth.json"

class PromptRequest(BaseModel):
    prompt: str

async def scrape_qwen(prompt: str) -> str:
    async with async_playwright() as p:
        # Launch browser with saved auth state if exists
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        context = await browser.new_context(
            storage_state=STORAGE_STATE if os.path.exists(STORAGE_STATE) else None,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        
        try:
            # Navigate to Qwen chat
            await page.goto("https://chat.qwen.ai", wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)  # Let JS load
            
            # Find input textarea - adjust selector if UI changes
            textarea = page.locator("textarea[placeholder*='message'], textarea[aria-label*='message'], textarea").first
            await textarea.wait_for(state="visible", timeout=10000)
            await textarea.fill(prompt)
            
            await page.wait_for_timeout(2000) 
            # Find and click send button
            send_btn = page.locator("button.send-button").first
            await send_btn.click()
            
            # Wait for AI response to finish streaming
            # Strategy: wait until response container stops changing for 3 seconds
            response_container = page.locator(".response-message-content").first
            await response_container.wait_for(state="visible", timeout=600000)
            
            last_text = ""
            stable_count = 0
            for _ in range(40):  # ~80 seconds max
                await asyncio.sleep(2)
                current_text = await response_container.inner_text()
                if current_text and current_text == last_text:
                    stable_count += 1
                    if stable_count >= 2:  # Stable for ~4 seconds
                        break
                else:
                    stable_count = 0
                    last_text = current_text
            
            result = last_text.strip()
            print("[Qwen Response]:", result)
            if not result:
                raise Exception("Empty response from Qwen chat")
            return result
            
        except Exception as e:
            # Save auth state for next run (if login succeeded)
            await context.storage_state(path=STORAGE_STATE)
            raise e
        finally:
            await browser.close()

@app.post("/scrape-qwen")
async def scrape_with_prompt(req: PromptRequest):
    try:
        from asyncio import wait_for
        tailored = await wait_for(scrape_qwen(req.prompt), timeout=55.0)
        # Clean up common artifacts
        tailored = re.sub(r'^```(?:text)?\s*', '', tailored, flags=re.MULTILINE)
        tailored = re.sub(r'\s*```$', '', tailored, flags=re.MULTILINE)
        return {"tailored_resume": tailored.strip()}
    except asyncio.TimeoutError:  # 👇 CATCH TIMEOUT SPECIFICALLY
        print("Request timed out")
        raise HTTPException(status_code=504, detail="Request timed out - Qwen response took too long")
    except Exception as e:
        print(f"Scrape error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Scraper failed: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)