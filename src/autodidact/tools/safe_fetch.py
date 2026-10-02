# 文件职责：限制公网 HTTP(S) 读取，固定已校验 IP 并保留 TLS/Host 身份及响应上限。
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from autodidact.config import agent_config, runtime_settings


# 功能：校验 URL 无凭据、端口合法且全部解析地址为公网，返回原解析结果和固定 IP 请求 URL。
async def public_address(url: str):
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("只允许无凭据的 HTTP(S) 公网来源")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in {80, 443}:
        raise ValueError("来源端口必须是 80 或 443")
    records = await asyncio.get_running_loop().getaddrinfo(
        parsed.hostname, port, type=socket.SOCK_STREAM
    )
    addresses = list(dict.fromkeys(item[4][0] for item in records))
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise ValueError("来源解析到了非公网地址")
    address = addresses[0]
    literal = f"[{address}]" if ":" in address else address
    pinned = urlunsplit((parsed.scheme, f"{literal}:{port}", parsed.path or "/", parsed.query, ""))
    return parsed, pinned


# 功能：逐跳校验并读取有界响应，禁用代理/压缩且保留 TLS SNI；返回最终 URL、头、正文 bytes 和编码。
async def fetch_public(url: str):
    cfg = agent_config().learning
    current = url
    for hop in range(cfg.max_redirects + 1):
        parsed, pinned = await public_address(current)
        headers = {
            "Host": parsed.netloc,
            "User-Agent": runtime_settings().http_user_agent,
            "Accept-Encoding": "identity",
        }
        # Connect to the validated literal address; preserve TLS certificate/SNI validation.
        # Environment proxies are disabled so they cannot redirect this pinned request.
        async with httpx.AsyncClient(timeout=35, trust_env=False, follow_redirects=False) as client:
            request = client.build_request("GET", pinned, headers=headers)
            request.extensions["sni_hostname"] = parsed.hostname
            response = await client.send(request, stream=True)
            try:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location or hop == cfg.max_redirects:
                        raise ValueError("无效重定向或重定向过多")
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                length = response.headers.get("content-length")
                if length and int(length) > cfg.max_response_bytes:
                    raise ValueError("来源响应超出大小限制")
                if response.headers.get("content-encoding", "identity").lower() not in {
                    "",
                    "identity",
                }:
                    raise ValueError("来源忽略了 identity 编码要求，暂不接收压缩响应")
                body = bytearray()
                async for chunk in response.aiter_raw():
                    if len(body) + len(chunk) > cfg.max_response_bytes:
                        raise ValueError("来源响应超出大小限制")
                    body.extend(chunk)
                return current, response.headers, bytes(body), response.encoding or "utf-8"
            finally:
                await response.aclose()
    raise ValueError("重定向过多")
