"""JM 番号搜索插件 — jmXXXXXX

用户发送 jm + 纯数字时，直接访问 album/{id}/ 判断是否存在：
- final_url 包含 /album/{id}/  → 存在，返回链接
- 跳转到 error/not_found 等页面 → 不存在
使用 curl_cffi + SOCKS5 代理绕过 Cloudflare。
域名列表（18comic.org 有 502，已移除）：18comic.vip → jmcomic.me
"""
import re

import yaml
from curl_cffi.requests import AsyncSession
from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.log import logger
from nonebot.rule import Rule

with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

jm_cfg = config.get("jm_search", {})
ALLOWED_GROUPS: set[int] = set(jm_cfg.get("allowed_groups", []))
# HTTP 代理（curl_cffi 在 Linux 上用 SOCKS5 有 BoringSSL TLS bug，固定用 HTTP）
PROXY: str | None = jm_cfg.get("proxy") or None

# 直接访问 album 页面，比搜索接口更稳定（搜索接口有关键字限制和跳转歧义）
# 18comic.org 当前 502 已移除
JM_ALBUM_HOSTS = [
    "https://18comic.vip",
    "https://jmcomic.me",
]


def _response_text(resp) -> str:
    try:
        return resp.text or ""
    except Exception:
        return ""


def _is_album_url(url: str, number: str) -> bool:
    return bool(re.search(rf"/album/{re.escape(number)}(?:/|$)", url))


def _looks_missing(url: str, body: str = "") -> bool:
    lowered_url = (url or "").lower()
    if "/error/album_missing" in lowered_url:
        return True

    lowered_body = (body or "").lower()
    missing_markers = (
        "/error/album_missing",
        "error/album_missing",
        "album_missing",
        "album-missing",
        "album missing",
        "本子不存在",
        "作品不存在",
        "不存在",
        "沒有找到",
        "没有找到",
        "找不到",
    )
    return any(marker in lowered_body for marker in missing_markers)


def _is_missing_album(resp, number: str) -> bool:
    if resp.status_code in (404, 410):
        return True
    return _looks_missing(str(resp.url), _response_text(resp))


def _is_album_page(resp, number: str) -> bool:
    if _is_missing_album(resp, number):
        return False
    return _is_album_url(str(resp.url), number)


def _needs_browser_confirm(resp, number: str) -> bool:
    return resp.status_code == 403 and _is_album_url(str(resp.url), number)


async def _confirm_album_with_browser(url: str) -> tuple[str, str] | None:
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeout
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
                proxy={"server": PROXY} if PROXY else None,
            )
            try:
                page = await browser.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except PlaywrightTimeout:
                    logger.warning(f"[jm_search] browser confirm goto timeout: {url}")
                await page.wait_for_timeout(2200)
                final_url = page.url
                body = await page.content()
                logger.info(f"[jm_search] browser confirm {url} final={final_url}")
                return final_url, body
            finally:
                await browser.close()
    except Exception as e:
        logger.warning(f"[jm_search] browser confirm failed: {type(e).__name__}: {e}")
        return None


def _jm_rule() -> Rule:
    async def check(event: GroupMessageEvent) -> bool:
        if ALLOWED_GROUPS and event.group_id not in ALLOWED_GROUPS:
            return False
        text = event.get_plaintext().strip()
        return bool(re.fullmatch(r"[Jj][Mm]\d+", text))
    return Rule(check)


jm_cmd = on_message(rule=_jm_rule(), priority=4, block=True)


@jm_cmd.handle()
async def handle_jm(bot: Bot, event: GroupMessageEvent):
    text = event.get_plaintext().strip()
    number = re.search(r"\d+", text).group()

    try:
        proxies = {"https": PROXY, "http": PROXY} if PROXY else None
        resp = None
        album_found = False
        album_missing = False
        async with AsyncSession(impersonate="chrome136") as session:
            for host in JM_ALBUM_HOSTS:
                url = f"{host}/album/{number}/"
                try:
                    r = await session.get(
                        url,
                        proxies=proxies,
                        timeout=20,
                        allow_redirects=True,
                    )
                    logger.info(f"[jm_search] {url} status={r.status_code} final={r.url}")
                    if _is_missing_album(r, number):
                        resp = r
                        album_missing = True
                        break
                    if _needs_browser_confirm(r, number):
                        confirmed = await _confirm_album_with_browser(url)
                        if confirmed:
                            confirmed_url, confirmed_body = confirmed
                            if _looks_missing(confirmed_url, confirmed_body):
                                resp = r
                                album_missing = True
                                break
                            if _is_album_url(confirmed_url, number):
                                resp = r
                                album_found = True
                                break
                        resp = r
                        break
                    if r.status_code not in (502, 503):
                        resp = r
                        break
                    logger.warning(f"[jm_search] {url} 返回 {r.status_code}，尝试下一个域名")
                except Exception as e:
                    logger.warning(f"[jm_search] {url} 请求失败: {e}，尝试下一个域名")

        if resp is None:
            await jm_cmd.finish("所有域名均不可用，请稍后再试。")
            return

        final_url = str(resp.url)
        # 存在 → 最终落在 /album/{id}/ 页面
        if album_missing:
            msg_text = f"未找到 jm{number} 的相关结果。"
        elif album_found or _is_album_page(resp, number):
            msg_text = f"https://18comic.vip/album/{number}/"
        else:
            msg_text = f"未找到 jm{number} 的相关结果。"

        node = {
            "type": "node",
            "data": {
                "name": "匿名用户",
                "uin": "10000",
                "content": [{"type": "text", "data": {"text": msg_text}}],
            },
        }
        try:
            await bot.call_api(
                "send_group_forward_msg",
                group_id=event.group_id,
                messages=[node],
            )
        except Exception:
            await jm_cmd.finish(msg_text)

    except Exception as e:
        from nonebot.exception import FinishedException
        if isinstance(e, FinishedException):
            raise
        logger.warning(f"[jm_search] 查询 jm{number} 出错: {type(e).__name__}: {e}")
        await jm_cmd.finish("搜索出错，请稍后再试。")
