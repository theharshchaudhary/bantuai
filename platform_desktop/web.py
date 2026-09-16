"""Web search, page reading and browser automation.

Search and page-fetching need no API key and no quota, so they are the cheap
way to answer anything current — reach for them before spending a model call
on guesswork.

Browser automation is different: Selenium needs a matching browser driver,
which is a runtime download. That sits awkwardly with the project's
zero-install rule, so the browser tools are lazy — nothing is fetched until
one is actually used, and if no driver can be resolved they fail with a clear
explanation rather than breaking the app. Search and fetch keep working
regardless.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

from core.tools.registry import Tier, ToolError, ToolRegistry

from .paths import guard_write, human_size

log = logging.getLogger("bantu.web")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
MAX_PAGE_CHARS = 8000
#: Elements that never carry the content someone actually wants.
STRIP_TAGS = ("script", "style", "nav", "header", "footer", "aside", "noscript", "form", "svg")

_driver: Any = None
_driver_lock = threading.Lock()


def _extract_text(html: str) -> tuple[str, str]:
    """Return (title, readable text) from an HTML document."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.string or "").strip() if soup.title else ""
    for tag in soup(list(STRIP_TAGS)):
        tag.decompose()
    # Prefer the semantic content container when the page provides one.
    main = soup.find("article") or soup.find("main") or soup.body or soup
    text = main.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return title, text


def _get_driver(headless: bool = False):
    """Start a browser once and reuse it. Raises a readable error if impossible."""
    global _driver
    with _driver_lock:
        if _driver is not None:
            try:
                _ = _driver.current_url  # cheap liveness probe
                return _driver
            except Exception:
                _driver = None

        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
        except ImportError as e:
            raise ToolError(f"selenium is not installed: {e}") from e

        opts = Options()
        if headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument(f"--user-agent={UA}")
        opts.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])

        try:
            _driver = webdriver.Chrome(options=opts)
        except Exception as e:
            raise ToolError(
                "could not start a browser. Selenium needs a Chrome/Edge driver matching "
                f"your installed browser, which it downloads on first use ({type(e).__name__}). "
                "web_search and fetch_page still work without one — prefer those unless the "
                "task genuinely needs clicking or typing on a live page."
            ) from e
        _driver.set_page_load_timeout(45)
        return _driver


def shutdown() -> None:
    """Close the browser if one was started. Called on exit."""
    global _driver
    with _driver_lock:
        if _driver is not None:
            try:
                _driver.quit()
            except Exception:
                pass
            _driver = None


def register(reg: ToolRegistry) -> None:
    # --- search and read ----------------------------------------------------

    @reg.register(tier=Tier.AUTO, category="web")
    def web_search(query: str, count: int = 6) -> str:
        """Search the web. Free, unlimited, and needs no API key.

        Use this for anything current or factual you are unsure about, rather
        than answering from memory. Costs nothing, so use it freely.

        Args:
            query: What to search for.
            count: How many results to return.
        """
        if not query.strip():
            raise ToolError("nothing to search for")
        try:
            from ddgs import DDGS

            with DDGS() as d:
                results = list(d.text(query, max_results=max(1, min(count, 20))))
        except Exception as e:
            raise ToolError(f"search failed: {type(e).__name__}: {e}") from e

        if not results:
            return f"No results for {query!r}."
        out = [f"{len(results)} results for {query!r}:\n"]
        for i, r in enumerate(results, 1):
            body = (r.get("body") or "").strip().replace("\n", " ")
            out.append(f"{i}. {r.get('title', '(untitled)')}\n   {r.get('href', '')}\n   {body[:280]}")
        return "\n".join(out)

    @reg.register(tier=Tier.AUTO, category="web")
    def search_news(query: str, count: int = 6) -> str:
        """Search recent news. Free and unlimited.

        Args:
            query: What to search for.
            count: How many results to return.
        """
        if not query.strip():
            raise ToolError("nothing to search for")
        try:
            from ddgs import DDGS

            with DDGS() as d:
                results = list(d.news(query, max_results=max(1, min(count, 20))))
        except Exception as e:
            raise ToolError(f"news search failed: {type(e).__name__}: {e}") from e

        if not results:
            return f"No news for {query!r}."
        out = [f"{len(results)} news results for {query!r}:\n"]
        for i, r in enumerate(results, 1):
            body = (r.get("body") or "").strip().replace("\n", " ")
            out.append(
                f"{i}. {r.get('title', '(untitled)')}  [{r.get('source', '?')}, {r.get('date', '?')}]"
                f"\n   {r.get('url', '')}\n   {body[:240]}"
            )
        return "\n".join(out)

    @reg.register(tier=Tier.AUTO, category="web")
    def fetch_page(url: str, max_chars: int = MAX_PAGE_CHARS) -> str:
        """Fetch a web page and return its readable text.

        Navigation, scripts and boilerplate are stripped. Use this after
        web_search to actually read a result rather than relying on the snippet.

        Args:
            url: The page to read.
            max_chars: Stop after this much text.
        """
        import requests

        clean = url.strip()
        if not clean.startswith(("http://", "https://")):
            clean = "https://" + clean
        try:
            resp = requests.get(clean, headers={"User-Agent": UA}, timeout=25)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise ToolError(f"could not fetch {clean}: {type(e).__name__}: {e}") from e

        ctype = resp.headers.get("content-type", "")
        if "html" not in ctype and "text" not in ctype:
            return f"{clean} is {ctype or 'not text'} ({human_size(len(resp.content))}), not a readable page."

        title, text = _extract_text(resp.text)
        if not text.strip():
            return f"{clean} returned no readable text — it may render entirely in JavaScript."
        limit = max(50, min(max_chars, 20000))
        body = text[:limit] + (f"\n... [truncated, {len(text)} chars total]" if len(text) > limit else "")
        return f"{title}\n{clean}\n\n{body}"

    @reg.register(tier=Tier.CONFIRM, category="web")
    def download_file(url: str, destination: str) -> str:
        """Download a file from the web to disk.

        Args:
            url: What to download.
            destination: Where to save it.
        """
        import requests

        out = guard_write(destination)
        if out.exists():
            raise ToolError(f"{out} already exists; choose another name")
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            with requests.get(url, headers={"User-Agent": UA}, timeout=60, stream=True) as r:
                r.raise_for_status()
                size = 0
                with open(out, "wb") as fh:
                    for chunk in r.iter_content(65536):
                        fh.write(chunk)
                        size += len(chunk)
        except requests.RequestException as e:
            out.unlink(missing_ok=True)
            raise ToolError(f"download failed: {type(e).__name__}: {e}") from e
        return f"Downloaded {human_size(size)} to {out}"

    # --- live browser -------------------------------------------------------

    @reg.register(tier=Tier.AUTO, category="web")
    def browser_open(url: str) -> str:
        """Open a page in a controlled browser you can then read and interact with.

        Only needed when a task requires clicking, typing or a page that renders
        in JavaScript. For simply reading a page, fetch_page is faster and needs
        no driver.

        Args:
            url: The page to open.
        """
        clean = url.strip()
        if not clean.startswith(("http://", "https://")):
            clean = "https://" + clean
        drv = _get_driver()
        try:
            drv.get(clean)
        except Exception as e:
            raise ToolError(f"could not load {clean}: {type(e).__name__}: {e}") from e
        return f"Opened {drv.title or clean}. Use browser_read to see what is on it."

    @reg.register(tier=Tier.AUTO, category="web")
    def browser_read(max_chars: int = MAX_PAGE_CHARS) -> str:
        """Read the text and form fields of the page currently open in the browser.

        Args:
            max_chars: Stop after this much text.
        """
        drv = _get_driver()
        try:
            html = drv.page_source
            url = drv.current_url
        except Exception as e:
            raise ToolError(f"no page is open: {e}") from e

        title, text = _extract_text(html)
        limit = max(50, min(max_chars, 20000))
        body = text[:limit] + (f"\n... [truncated, {len(text)} chars]" if len(text) > limit else "")

        fields = []
        try:
            from selenium.webdriver.common.by import By

            for el in drv.find_elements(By.CSS_SELECTOR, "input, textarea, select")[:25]:
                label = el.get_attribute("name") or el.get_attribute("id") or el.get_attribute(
                    "placeholder"
                ) or el.get_attribute("aria-label")
                if label:
                    fields.append(f"  {el.tag_name}[{el.get_attribute('type') or 'text'}]: {label}")
        except Exception:
            pass

        out = f"{title}\n{url}\n\n{body}"
        if fields:
            out += "\n\nForm fields on this page:\n" + "\n".join(fields)
        return out

    @reg.register(tier=Tier.CONFIRM, category="web")
    def browser_click(target: str, by_text: bool = True) -> str:
        """Click something on the open page. This can submit forms or make purchases.

        Args:
            target: Visible text of the element, or a CSS selector if by_text is false.
            by_text: Match on visible text rather than a CSS selector.
        """
        from selenium.webdriver.common.by import By

        drv = _get_driver()
        try:
            if by_text:
                safe = target.replace('"', '\\"')
                el = drv.find_element(
                    By.XPATH,
                    f'//*[self::button or self::a or self::input or self::span or self::div]'
                    f'[contains(normalize-space(.), "{safe}")]',
                )
            else:
                el = drv.find_element(By.CSS_SELECTOR, target)
        except Exception as e:
            raise ToolError(
                f"could not find {target!r} on the page ({type(e).__name__}). "
                f"Use browser_read to see what is actually there."
            ) from e
        try:
            drv.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            el.click()
        except Exception as e:
            raise ToolError(f"found {target!r} but could not click it: {e}") from e
        return f"Clicked {target!r}. Page is now: {drv.title}"

    @reg.register(tier=Tier.CONFIRM, category="web")
    def browser_type(field: str, text: str, submit: bool = False) -> str:
        """Type into a field on the open page, optionally submitting the form.

        Args:
            field: The field's name, id, placeholder or a CSS selector.
            text: What to type.
            submit: Press Enter afterwards.
        """
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys

        drv = _get_driver()
        el = None
        for how, what in (
            (By.NAME, field), (By.ID, field), (By.CSS_SELECTOR, field),
            (By.XPATH, f'//input[@placeholder="{field}"]|//textarea[@placeholder="{field}"]'),
        ):
            try:
                el = drv.find_element(how, what)
                break
            except Exception:
                continue
        if el is None:
            raise ToolError(
                f"no field matching {field!r}. browser_read lists the fields on the page."
            )
        try:
            el.clear()
            el.send_keys(text)
            if submit:
                el.send_keys(Keys.RETURN)
        except Exception as e:
            raise ToolError(f"could not type into {field!r}: {e}") from e
        return f"Typed into {field!r}{' and submitted' if submit else ''}. Page: {drv.title}"
