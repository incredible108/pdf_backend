import os, re, asyncio, json, time
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

STORAGE_STATE = "deepseek_auth.json"

# Global browser state
class BrowserState:
    playwright = None
    browser: Browser = None
    context: BrowserContext = None
    page: Page = None
    lock: asyncio.Lock = None
    is_ready: bool = False

browser_state = BrowserState()

class PromptRequest(BaseModel):
    prompt: str

def load_auth_state():
    """Load Playwright auth state from local file."""
    if os.path.exists(STORAGE_STATE):
        with open(STORAGE_STATE, "r") as f:
            return json.load(f)
    print("No local auth state file found.")
    return None

async def init_browser():
    """Initialize and keep browser ready at DeepSeek."""
    auth_state = load_auth_state()
    if not auth_state:
        raise Exception("AUTH_MISSING: Set DEEPSEEK_AUTH_STATE env var")
    
    browser_state.playwright = await async_playwright().start()
    browser_state.browser = await browser_state.playwright.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
    )
    browser_state.context = await browser_state.browser.new_context(
        storage_state=STORAGE_STATE,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"
    )
    
    # Block unnecessary resources
    await browser_state.context.route("**/*.{png,jpg,jpeg,svg,webp,gif,woff,woff2,ttf,eot}", lambda route: route.abort())
    await browser_state.context.route("**/analytics/**", lambda route: route.abort())
    await browser_state.context.route("**/track/**", lambda route: route.abort())
    await browser_state.context.route("**/telemetry/**", lambda route: route.abort())
    
    browser_state.page = await browser_state.context.new_page()
    browser_state.lock = asyncio.Lock()
    
    # Navigate to DeepSeek and wait for it to be ready
    await browser_state.page.goto("https://chat.deepseek.com", wait_until="domcontentloaded", timeout=30000)
    await browser_state.page.wait_for_timeout(10000)
    
    # Verify login state
    if "login" in browser_state.page.url.lower() or "auth" in browser_state.page.url.lower():
        raise Exception("AUTH_EXPIRED: Please regenerate DEEPSEEK_AUTH_STATE")
    
    # Wait for textarea to be ready
    textarea = browser_state.page.locator("textarea").first
    await textarea.wait_for(state="visible", timeout=20000)
    
    browser_state.is_ready = True
    print("[Browser] DeepSeek browser initialized and ready!")

async def cleanup_browser():
    """Cleanup browser resources with timeout to prevent hanging."""
    browser_state.is_ready = False
    
    try:
        if browser_state.page:
            try:
                await asyncio.wait_for(browser_state.page.close(), timeout=5.0)
            except:
                pass
            browser_state.page = None
        
        if browser_state.context:
            try:
                await asyncio.wait_for(browser_state.context.close(), timeout=5.0)
            except:
                pass
            browser_state.context = None
        
        if browser_state.browser:
            try:
                await asyncio.wait_for(browser_state.browser.close(), timeout=5.0)
            except:
                pass
            browser_state.browser = None
        
        if browser_state.playwright:
            try:
                await asyncio.wait_for(browser_state.playwright.stop(), timeout=5.0)
            except:
                pass
            browser_state.playwright = None
        
        print("[Browser] Browser closed.")
    except Exception as e:
        print(f"[Browser] Cleanup error (ignored): {e}")

async def start_new_chat():
    """Start a new chat by clicking the new chat button or refreshing."""
    page = browser_state.page
    
    try:
        # Try to find and click the "New Chat" button (DeepSeek usually has this)
        new_chat_btn = page.locator("[class*='new-chat'], [aria-label*='New'], button:has-text('New')").first
        if await new_chat_btn.is_visible(timeout=2000):
            await new_chat_btn.click()
            await page.wait_for_timeout(1000)
            return
    except:
        pass
    
    # Fallback: navigate to base URL to start fresh
    await page.goto("https://chat.deepseek.com", wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(3000)

async def scrape_deepseek(prompt: str) -> str:
    """Send prompt to DeepSeek using persistent browser."""
    if not browser_state.is_ready:
        raise Exception("Browser not initialized")
    
    # Use lock to prevent concurrent access
    async with browser_state.lock:
        page = browser_state.page
        
        try:
            # Start a new chat for each request
            await start_new_chat()
            
            # Find input textarea
            textarea = page.locator("textarea").first
            await textarea.wait_for(state="visible", timeout=20000)
            await textarea.fill(prompt)
            await page.wait_for_timeout(1000)
            
            # Find and click send button
            send_btn = page.locator("div._52c986b[role=\"button\"]:has(svg[xmlns=\"http://www.w3.org/2000/svg\"])").first
            await send_btn.click()
            
            # Wait for response container to appear
            response_container = page.locator(".ds-markdown").first
            await response_container.wait_for(state="visible", timeout=600000)
            
            # Wait for response to stabilize
            last_text = ""
            stable_count = 0
            for _ in range(40):
                await asyncio.sleep(2)
                current_text = await response_container.inner_text()
                if current_text and current_text == last_text:
                    stable_count += 1
                    if stable_count >= 2:
                        break
                else:
                    stable_count = 0
                    last_text = current_text
            
            result = last_text.strip()
            print("[DeepSeek Response]:", result[:100] + "..." if len(result) > 100 else result)
            
            if not result:
                raise Exception("Empty response from DeepSeek chat")
            
            return result
            
        except Exception as e:
            # If something goes wrong, try to recover by reinitializing
            print(f"[Error] {str(e)}, attempting recovery...")
            try:
                await start_new_chat()
            except:
                pass
            raise e

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan: start browser on startup, cleanup on shutdown."""
    print("[Startup] Initializing persistent browser...")
    try:
        await init_browser()
        print("[Startup] Browser ready!")
    except Exception as e:
        print(f"[Startup Error] Failed to init browser: {e}")
    
    yield
    
    print("[Shutdown] Cleaning up browser...")
    await cleanup_browser()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/scrape-deepseek")
async def scrape_with_prompt(req: PromptRequest):
    try:
        tailored = await asyncio.wait_for(scrape_deepseek(req.prompt), timeout=600.0)
        tailored = re.sub(r'^```(?:text)?\s*', '', tailored, flags=re.MULTILINE)
        tailored = re.sub(r'\s*```$', '', tailored, flags=re.MULTILINE)
        return {"tailored_resume": tailored.strip()}
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Request timed out - DeepSeek response took too long")
    except Exception as e:
        error_msg = str(e)
        if "AUTH_EXPIRED" in error_msg:
            raise HTTPException(status_code=401, detail="Authentication expired. Regenerate DEEPSEEK_AUTH_STATE.")
        if "AUTH_MISSING" in error_msg:
            raise HTTPException(status_code=401, detail="No auth state configured.")
        if "not initialized" in error_msg.lower():
            raise HTTPException(status_code=503, detail="Browser not ready. Server may be starting up.")
        if "Timeout" in error_msg or "timeout" in error_msg.lower():
            raise HTTPException(status_code=504, detail=f"Page load timed out: {error_msg[:150]}")
        raise HTTPException(status_code=500, detail=f"Scraper failed: {error_msg[:200]}")

@app.get("/health")
async def health_check():
    auth = load_auth_state()
    if not auth:
        return {"status": "unhealthy", "reason": "DEEPSEEK_AUTH_STATE not configured"}
    if not browser_state.is_ready:
        return {"status": "unhealthy", "reason": "Browser not initialized"}
    return {
        "status": "healthy",
        "service": "deepseek-scraper",
        "browser_ready": browser_state.is_ready,
        "timestamp": int(time.time())
    }

@app.post("/reinit-browser")
async def reinit_browser():
    """Manually reinitialize browser if needed."""
    try:
        await cleanup_browser()
        await init_browser()
        return {"status": "success", "message": "Browser reinitialized"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reinit: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 11000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
