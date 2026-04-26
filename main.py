import os, re, asyncio, json, base64, time
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.async_api import async_playwright

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class PromptRequest(BaseModel):
    prompt: str

def load_auth_state():
    """Load Playwright auth state from Render env var or local fallback."""
    encoded = os.getenv("QWEN_AUTH_STATE")
    if encoded:
        try:
            return json.loads(base64.b64decode(encoded).decode())
        except Exception as e:
            print(f"⚠️ Failed to decode QWEN_AUTH_STATE: {e}")
    if os.path.exists("qwen_auth.json"):
        with open("qwen_auth.json", "r") as f:
            return json.load(f)
    return None

async def scrape_qwen(prompt: str, max_retries: int = 2) -> str:
    auth_state = load_auth_state()
    if not auth_state:
        raise Exception("AUTH_MISSING: Set QWEN_AUTH_STATE env var")

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
                )
                context = await browser.new_context(
                    storage_state=auth_state,
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 800},
                    # 🚀 Block unnecessary resources to speed up load
                    extra_http_headers={"sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Linux"'}
                )
                
                # ⚡ Block analytics/tracking that prevent networkidle
                await context.route("**/*.{png,jpg,jpeg,svg,webp,gif,woff,woff2,ttf,eot}", lambda route: route.abort())
                await context.route("**/analytics/**", lambda route: route.abort())
                await context.route("**/track/**", lambda route: route.abort())
                await context.route("**/telemetry/**", lambda route: route.abort())
                
                page = await context.new_page()
                
                # 🎯 KEY FIX: Use domcontentloaded + manual UI wait instead of networkidle
                await page.goto("https://chat.qwen.ai", wait_until="domcontentloaded", timeout=20000)
                
                # Wait for DOM to be interactive, then wait for specific chat element
                await page.wait_for_timeout(2000)  # Let JS frameworks initialize
                
                # Verify login state
                if "login" in page.url.lower() or "auth" in page.url.lower():
                    raise Exception("AUTH_EXPIRED: Please regenerate QWEN_AUTH_STATE")
                
                # Wait for the actual chat input to be ready (more reliable than generic selectors)
                textarea = page.locator("textarea.message-input-textarea").first
                await textarea.wait_for(state="attached", timeout=15000)
                await textarea.wait_for(state="visible", timeout=10000)
                
                # Ensure element is truly interactable
                await textarea.scroll_into_view_if_needed()
                await page.wait_for_timeout(300)
                await textarea.fill(prompt)
                await page.wait_for_timeout(3000)
                # Send message with fallback strategies
                send_clicked = False
                send_selectors = [
                    "button.send-button",
                    "button[class*='send']",
                    "button[aria-label*='send']",
                    "button:has-text('Send')",
                    ".message-input-area button:last-child"
                ]
                for sel in send_selectors:
                    btn = page.locator(sel).first
                    if await btn.is_visible() and await btn.is_enabled():
                        await btn.click()
                        send_clicked = True
                        break
                if not send_clicked:
                    await textarea.press("Enter")
                
                # Wait for response container to appear
                response_container = None
                resp_selectors = [
                    ".response-message-content",
                    "[class*='response']",
                    ".message-bubble.assistant",
                    "[data-testid='assistant-message']"
                ]
                for sel in resp_selectors:
                    candidate = page.locator(sel).first
                    try:
                        await candidate.wait_for(state="visible", timeout=20000)
                        response_container = candidate
                        break
                    except Exception:
                        continue
                
                if not response_container:
                    response_container = page.locator("body").first
                    await page.wait_for_timeout(15000)
                
                # Poll for stable response (ignore streaming artifacts)
                last_text = ""
                stable_count = 0
                for _ in range(20):  # ~40 seconds max
                    await asyncio.sleep(2)
                    current_text = await response_container.inner_text()
                    
                    # Skip loading states
                    if not current_text or "thinking" in current_text.lower() or current_text.rstrip().endswith("..."):
                        continue
                    
                    if len(current_text) > 30 and current_text == last_text:
                        stable_count += 1
                        if stable_count >= 2:
                            break
                    else:
                        stable_count = 0
                        last_text = current_text
                
                result = last_text.strip()
                if not result or len(result) < 15:
                    raise Exception(f"Empty/invalid response: '{result[:100]}'")
                
                await browser.close()
                return result
                
        except Exception as e:
            last_error = e
            print(f"⚠️ Attempt {attempt + 1}/{max_retries + 1} failed: {str(e)}")
            if attempt < max_retries:
                await asyncio.sleep(2 ** attempt)  # Exponential backoff
                continue
            raise last_error

@app.post("/scrape-qwen")
async def scrape_with_prompt(req: PromptRequest):
    try:
        tailored = await asyncio.wait_for(scrape_qwen(req.prompt), timeout=600.0)
        tailored = re.sub(r'^```(?:text)?\s*', '', tailored, flags=re.MULTILINE)
        tailored = re.sub(r'\s*```$', '', tailored, flags=re.MULTILINE)
        return {"tailored_resume": tailored.strip()}
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Request timed out - Qwen response took too long")
    except Exception as e:
        error_msg = str(e)
        if "AUTH_EXPIRED" in error_msg:
            raise HTTPException(status_code=401, detail="Authentication expired. Regenerate QWEN_AUTH_STATE.")
        if "AUTH_MISSING" in error_msg:
            raise HTTPException(status_code=401, detail="No auth state configured.")
        if "Timeout" in error_msg or "timeout" in error_msg.lower():
            raise HTTPException(status_code=504, detail=f"Page load timed out: {error_msg[:150]}")
        raise HTTPException(status_code=500, detail=f"Scraper failed: {error_msg[:200]}")

@app.get("/health")
async def health_check():
    auth = load_auth_state()
    if not auth:
        return {"status": "unhealthy", "reason": "QWEN_AUTH_STATE not configured"}
    return {"status": "healthy", "service": "qwen-scraper", "timestamp": int(time.time())}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)