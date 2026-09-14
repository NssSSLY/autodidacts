from __future__ import annotations

import hashlib

import httpx
from bs4 import BeautifulSoup

from autodidact.config import agent_config, runtime_settings
from autodidact.schemas import SourceDocument


class WebReader:
    async def read(self, url: str) -> SourceDocument:
        headers = {"User-Agent": runtime_settings().http_user_agent}
        async with httpx.AsyncClient(timeout=35, headers=headers, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
        content_type = r.headers.get("content-type", "")
        if "text/html" not in content_type and "text/plain" not in content_type:
            raise ValueError(f"Unsupported content type: {content_type}")
        if "text/html" in content_type:
            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup(["script", "style", "noscript", "nav", "footer"]):
                tag.decompose()
            title = soup.title.get_text(" ", strip=True) if soup.title else url
            text = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())
        else:
            title, text = url, r.text
        limit = agent_config().learning.max_chars_per_source
        return SourceDocument(url=url, title=title, text=text[:limit])

    @staticmethod
    def hash_text(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()
