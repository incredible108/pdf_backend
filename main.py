import os, re, asyncio, json, time
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.async_api import async_playwright
from contextlib import asynccontextmanager
import threading

# Thread-safe slot manager for auth states
class AuthSlotManager:
    def __init__(self, num_slots: int = 19):
        self.num_slots = num_slots
        self.slots = [False] * (num_slots + 1)  # Index 1-19 (0 unused)
        self.lock = threading.Lock()
        self.condition = threading.Condition(self.lock)
    
    def acquire_slot(self, timeout: float = 60.0) -> int:
        """Thread-safe slot acquisition with timeout. Returns slot number or raises."""
        with self.condition:
            end_time = time.time() + timeout
            while True:
                # Try to find an available slot
                for i in range(1, self.num_slots + 1):
                    if not self.slots[i]:
                        self.slots[i] = True
                        print(f"[SlotManager] Acquired slot {i}")
                        return i
                
                # No slot available, wait
                remaining = end_time - time.time()
                if remaining <= 0:
                    raise Exception("NO_SLOTS_AVAILABLE: All auth slots are busy, try again later")
                
                print(f"[SlotManager] All slots busy, waiting up to {remaining:.1f}s...")
                self.condition.wait(timeout=min(remaining, 5.0))
    
    def release_slot(self, slot_number: int):
        """Thread-safe slot release."""
        with self.condition:
            if 1 <= slot_number <= self.num_slots:
                self.slots[slot_number] = False
                print(f"[SlotManager] Released slot {slot_number}")
                self.condition.notify_all()
    
    def get_status(self) -> dict:
        """Get current slot usage status."""
        with self.lock:
            used = sum(1 for i in range(1, self.num_slots + 1) if self.slots[i])
            return {
                "total_slots": self.num_slots,
                "used_slots": used,
                "available_slots": self.num_slots - used
            }

# Global slot manager
slot_manager = AuthSlotManager(num_slots=19)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    print("[Startup] DeepSeek scraper initialized with 19 auth slots")
    yield
    # Shutdown
    print("[Shutdown] DeepSeek scraper shutting down")

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class PromptRequest(BaseModel):
    prompt: str

def load_auth_state(storage_state: str):
    """Load Playwright auth state from file."""
    if os.path.exists(storage_state):
        with open(storage_state, "r") as f:
            return json.load(f)
    
    print(f"No auth state file found: {storage_state}")
    return None

async def scrape_deepseek(prompt: str) -> str:
    # Acquire a slot (thread-safe with timeout)
    slot_number = await asyncio.get_event_loop().run_in_executor(
        None, lambda: slot_manager.acquire_slot(timeout=120.0)
    )
    
    STORAGE_STATE = f"auth/deepseek_auth_{slot_number}.json"
    print(f"-------------------------------- Using auth state file: {STORAGE_STATE} --------------------------------")

    auth_state = load_auth_state(STORAGE_STATE)
    if not auth_state:
        slot_manager.release_slot(slot_number)
        raise Exception(f"AUTH_MISSING: Auth file not found for slot {slot_number}")

    browser = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
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
            print("[DeepSeek Response]:", result[:200] if result else "EMPTY")
            if not result:
                raise Exception("Empty response from DeepSeek chat")
            return result
    except Exception as e:
        raise e
    finally:
        slot_manager.release_slot(slot_number)
        if browser:
            await browser.close()

# DeepSeek endpoint
@app.post("/scrape-deepseek")
async def scrape_with_prompt(req: PromptRequest):
    try:
        print(f"Received prompt: {req.prompt[:10]}...")
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
    # Check if at least one auth file exists
    auth_exists = any(
        os.path.exists(f"auth/deepseek_auth_{i}.json") 
        for i in range(1, 20)
    )
    if not auth_exists:
        return {"status": "unhealthy", "reason": "No DEEPSEEK_AUTH_STATE files configured"}
    
    slot_status = slot_manager.get_status()
    return {
        "status": "healthy", 
        "service": "deepseek-scraper", 
        "timestamp": int(time.time()),
        **slot_status
    }

# Slot status endpoint
@app.get("/slots")
async def get_slots():
    """Get current slot usage status."""
    return slot_manager.get_status()

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 11000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
