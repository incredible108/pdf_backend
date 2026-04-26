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
                
                # Find input textarea - adjust selector if UI changes
                textarea = page.locator("textarea[placeholder*='message'], textarea[aria-label*='message'], textarea").first
                await textarea.wait_for(state="visible", timeout=10000)
                await textarea.fill(prompt)
                
                await page.wait_for_timeout(2000) 
                # Find and click send button
                send_btn = page.locator("button.send-button").first
                await send_btn.click()
                
                # Wait for response container to appear
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