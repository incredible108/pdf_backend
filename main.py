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

STORAGE_STATE = "deepseek_auth.json"

class PromptRequest(BaseModel):
    prompt: str

def load_auth_state():
    """Load Playwright auth state from Render env var or local fallback."""
    if os.path.exists(STORAGE_STATE):
        with open(STORAGE_STATE, "r") as f:
            return json.load(f)
    
    print("No local auth state file found. Checking environment variable...")
    return None

async def scrape_deepseek(prompt: str) -> str:
    auth_state = load_auth_state()
    if not auth_state:
        raise Exception("AUTH_MISSING: Set DEEKSEEK_AUTH_STATE env var")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=False,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
            )
            context = await browser.new_context(
                storage_state=STORAGE_STATE,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"
            )
            # Block unnecessary resources
            await context.route("**/*.{png,jpg,jpeg,svg,webp,gif,woff,woff2,ttf,eot}", lambda route: route.abort())
            await context.route("**/analytics/**", lambda route: route.abort())
            await context.route("**/track/**", lambda route: route.abort())
            await context.route("**/telemetry/**", lambda route: route.abort())
            page = await context.new_page()
            # Go to DeepSeek
            await page.goto("https://chat.deepseek.com", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(10000)
            # Verify login state (DeepSeek)
            if "login" in page.url.lower() or "auth" in page.url.lower():
                raise Exception("AUTH_EXPIRED: Please regenerate DEEPSEEK_AUTH_STATE")
            # Find input textarea (DeepSeek selector)
            textarea = page.locator("textarea").first
            await textarea.wait_for(state="visible", timeout=20000)
            await textarea.fill(prompt)
            await page.wait_for_timeout(1000)
            # Find and click send button (DeepSeek selector)
            send_btn = page.locator("div._52c986b[role=\"button\"]:has(svg[xmlns=\"http://www.w3.org/2000/svg\"])").first
            await send_btn.click()
            # Wait for response container to appear (DeepSeek selector)
            response_container = page.locator(".ds-markdown").first
            await response_container.wait_for(state="visible", timeout=600000)
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
            print("[DeepSeek Response]:", result)
            if not result:
                raise Exception("Empty response from DeepSeek chat")
            return result
    except Exception as e:
        raise e
    finally:
        await browser.close()

# DeepSeek endpoint
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
        if "Timeout" in error_msg or "timeout" in error_msg.lower():
            raise HTTPException(status_code=504, detail=f"Page load timed out: {error_msg[:150]}")
        raise HTTPException(status_code=500, detail=f"Scraper failed: {error_msg[:200]}")


# Health check for DeepSeek
@app.get("/health")
async def health_check():
    auth = load_auth_state()
    if not auth:
        return {"status": "unhealthy", "reason": "DEEPSEEK_AUTH_STATE not configured"}
    return {"status": "healthy", "service": "deepseek-scraper", "timestamp": int(time.time())}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 11000))
    uvicorn.run("main:app", host="127.0.0.1", port=port, reload=False)