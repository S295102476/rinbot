"""GBVSR 角色使用率排行榜截图。

指令：
  #GB使用率       -> ALL
  #GB使用率 大师  -> GRAND MASTER / MASTER
  #GB使用率 S段   -> S++ / S+
"""

import asyncio
import time
from datetime import datetime
from pathlib import Path

import yaml
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment
from nonebot.log import logger
from nonebot.params import CommandArg
from nonebot.adapters.onebot.v11 import Message


_USAGE_RATE_URL = "https://rising.granbluefantasy.jp/en/usagerate"
_USAGE_TEXT = "可用：#GB使用率 / #GB使用率 大师 / #GB使用率 S段"

with open("config.yaml", "r", encoding="utf-8") as _f:
    _config = yaml.safe_load(_f)

_ai_cfg = _config.get("ai", {})
_gb_cfg = _config.get("gb_usage_rate", {})

_CACHE_TTL = int(_gb_cfg.get("cache_ttl", 21600))
_TIMEOUT = int(_gb_cfg.get("timeout", 60000))
_CACHE_DIR = Path(_gb_cfg.get("cache_dir", "data/gb_usage_rate"))
_VIEWPORT_WIDTH = int(_gb_cfg.get("viewport_width", 1920))
_VIEWPORT_HEIGHT = int(_gb_cfg.get("viewport_height", 900))

_proxy_value = _gb_cfg.get("proxy", "")
_PROXY: str | None = str(_proxy_value).strip() if _proxy_value else (_ai_cfg.get("proxy") or None)

_TARGETS = {
    "all": {"index": 0, "cache": "all.png", "label": "ALL"},
    "master": {"index": 1, "cache": "master.png", "label": "MASTER"},
    "s": {"index": 2, "cache": "s.png", "label": "S++ / S+"},
}
_LOCKS: dict[str, asyncio.Lock] = {key: asyncio.Lock() for key in _TARGETS}

_CAPTURE_CSS = """
* {
  animation: none !important;
  transition: none !important;
}
[id*="cookie" i],
[class*="cookie" i],
[id*="consent" i],
[class*="consent" i],
[id*="privacy" i],
[class*="privacy" i] {
  display: none !important;
}
"""


def _parse_target(arg: str) -> str | None:
    text = (arg or "").strip()
    compact = text.replace(" ", "").replace("\u3000", "").lower()
    if compact in ("", "all", "全部", "全段位", "全"):
        return "all"
    if "大师" in compact or compact in ("master", "gm", "grandmaster", "grand", "m"):
        return "master"
    if compact in ("s", "s段", "s++", "s+", "s++s+", "s++/s+") or "s段" in compact:
        return "s"
    return None


def _cache_period() -> str:
    """官方按月更新排行榜，缓存按当前年月隔离，跨月必定重新截图。"""
    return datetime.now().strftime("%Y%m")


def _cache_path(target: str) -> Path:
    return _CACHE_DIR / _cache_period() / str(_TARGETS[target]["cache"])


def _is_cache_fresh(path: Path) -> bool:
    if _CACHE_TTL <= 0 or not path.exists():
        return False
    try:
        return path.stat().st_size > 1024 and time.time() - path.stat().st_mtime < _CACHE_TTL
    except OSError:
        return False


async def _read_or_capture(target: str) -> bytes:
    path = _cache_path(target)
    if _is_cache_fresh(path):
        return path.read_bytes()

    async with _LOCKS[target]:
        if _is_cache_fresh(path):
            return path.read_bytes()

        img_bytes = await _capture_usage_rate(target)
        if len(img_bytes) <= 1024:
            raise RuntimeError(f"截图结果异常，图片过小: {len(img_bytes)} bytes")

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(img_bytes)
        return img_bytes


async def _capture_usage_rate(target: str) -> bytes:
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeout
        from playwright.async_api import async_playwright
    except Exception as e:
        raise RuntimeError("Playwright 未安装，请执行：python -m playwright install chromium") from e

    target_index = int(_TARGETS[target]["index"])

    async def _capture_once(proxy: str | None) -> bytes:
        browser = None
        try:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
                proxy={"server": proxy} if proxy else None,
            )
            context = await browser.new_context(
                viewport={"width": _VIEWPORT_WIDTH, "height": _VIEWPORT_HEIGHT},
                device_scale_factor=2,
            )
            page = await context.new_page()
            await page.goto(_USAGE_RATE_URL, wait_until="domcontentloaded", timeout=_TIMEOUT)
            try:
                await page.wait_for_load_state("networkidle", timeout=min(_TIMEOUT, 30000))
            except PlaywrightTimeout:
                pass

            await page.wait_for_selector("#js-ranking .ranking-list li", timeout=_TIMEOUT)

            # 导航到目标 swiper 滑块（index=0 为默认激活，无需操作）
            if target_index > 0:
                await page.evaluate(
                    """
                    (idx) => {
                        const bullets = document.querySelectorAll(
                            '#js-ranking .swiper-pagination-bullet'
                        );
                        if (bullets[idx]) { bullets[idx].click(); return; }
                        const next = document.querySelector('#js-ranking .swiper-button-next');
                        if (next) { for (let i = 0; i < idx; i++) next.click(); }
                    }
                    """,
                    target_index,
                )
                await asyncio.sleep(0.8)

            # 等待目标列表图片全部加载完毕
            await page.wait_for_function(
                """
                (index) => {
                  const list = document.querySelectorAll('#js-ranking .ranking-list')[index];
                  if (!list) return false;
                  const items = list.querySelectorAll('li');
                  if (items.length < 5) return false;
                  const imgs = Array.from(list.querySelectorAll('img'));
                  return imgs.length >= 5 && imgs.every(img => img.complete && img.naturalWidth > 0);
                }
                """,
                arg=target_index,
                timeout=_TIMEOUT,
            )
            await page.evaluate(
                "() => document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve()"
            )

            # 注入 CSS：停止动画并隐藏 cookie/consent 浮层，不改排行榜内部布局
            await page.add_style_tag(content=_CAPTURE_CSS)

            # JS 强制展开：用 ul 的完整高度扩展 slide / wrapper / container，
            # 保留官网 li/rank/rate/detail 的原始布局和字体。
            metrics = await page.evaluate(
                """
                (idx) => {
                    for (const node of Array.from(document.body.querySelectorAll('*'))) {
                        const text = (node.innerText || '').slice(0, 160).toLowerCase();
                        const style = window.getComputedStyle(node);
                        if (
                            (style.position === 'fixed' || style.position === 'sticky') &&
                            (text.includes('cookie') || text.includes('consent'))
                        ) {
                            node.remove();
                        }
                    }

                    const el = document.querySelectorAll('#js-ranking .ranking-list')[idx];
                    if (!el) return null;
                    const body = el.querySelector('.ranking-list--body');
                    const ul = body ? body.querySelector('ul') : null;
                    const wrapper = document.querySelector('#js-ranking');
                    const container = wrapper ? wrapper.closest('.swiper-container') : null;

                    if (!body || !ul) return null;

                    const liBottom = Array.from(ul.children).reduce((max, item) => {
                        return Math.max(max, item.offsetTop + item.offsetHeight);
                    }, 0);
                    const bodyHeight = Math.ceil(Math.max(
                        body.scrollHeight,
                        ul.scrollHeight,
                        ul.offsetHeight,
                        liBottom
                    ));
                    const fullHeight = Math.ceil(body.offsetTop + bodyHeight);

                    body.style.overflow = 'visible';
                    body.style.height = `${bodyHeight}px`;
                    body.style.maxHeight = 'none';

                    el.style.overflow = 'visible';
                    el.style.height = `${fullHeight}px`;
                    el.style.maxHeight = 'none';

                    if (wrapper) {
                        wrapper.style.overflow = 'visible';
                        wrapper.style.height = `${fullHeight}px`;
                        wrapper.style.maxHeight = 'none';
                    }
                    if (container) {
                        container.style.overflow = 'visible';
                        container.style.height = `${fullHeight}px`;
                        container.style.maxHeight = 'none';
                    }

                    let node = el;
                    while (node && node !== document.body) {
                        node.style.overflow = 'visible';
                        node.style.maxHeight = 'none';
                        node.style.minHeight = `${Math.max(node.offsetHeight, fullHeight)}px`;
                        node = node.parentElement;
                    }

                    void el.offsetHeight;  // force reflow

                    const rect = el.getBoundingClientRect();
                    return {
                        x: rect.left + window.scrollX,
                        y: rect.top + window.scrollY,
                        width: rect.width,
                        height: Math.max(fullHeight, el.scrollHeight, el.offsetHeight, rect.height),
                        bodyHeight,
                        fullHeight,
                        itemCount: ul.children.length,
                    };
                }
                """,
                target_index,
            )

            if not metrics or metrics["width"] <= 0:
                raise RuntimeError(f"无法获取排行榜元素尺寸 index={target_index}")
            if metrics["width"] < 500:
                raise RuntimeError(f"官网布局宽度异常，可能进入移动版: {metrics}")

            # 动态调整视口高度以容纳全部内容（clip 受视口边界限制）
            viewport_h = min(int(metrics["y"] + metrics["height"] + 50), 16384)
            await page.set_viewport_size({
                "width": _VIEWPORT_WIDTH,
                "height": max(viewport_h, _VIEWPORT_HEIGHT),
            })
            await asyncio.sleep(0.2)

            # 视口变化后重新测量，确保坐标正确
            final_metrics = await page.evaluate(
                """
                (idx) => {
                    const el = document.querySelectorAll('#js-ranking .ranking-list')[idx];
                    if (!el) return null;
                    const body = el.querySelector('.ranking-list--body');
                    const ul = body ? body.querySelector('ul') : null;
                    void el.offsetHeight;
                    const rect = el.getBoundingClientRect();
                    const liBottom = ul ? Array.from(ul.children).reduce((max, item) => {
                        return Math.max(max, item.offsetTop + item.offsetHeight);
                    }, 0) : 0;
                    const bodyHeight = body && ul ? Math.ceil(Math.max(
                        body.scrollHeight,
                        ul.scrollHeight,
                        ul.offsetHeight,
                        liBottom
                    )) : 0;
                    const fullHeight = body ? Math.ceil(body.offsetTop + bodyHeight) : 0;
                    return {
                        x: rect.left + window.scrollX,
                        y: rect.top + window.scrollY,
                        width: rect.width,
                        height: Math.max(fullHeight, el.scrollHeight, el.offsetHeight, rect.height),
                        bodyHeight,
                        fullHeight,
                        itemCount: ul ? ul.children.length : 0,
                    };
                }
                """,
                target_index,
            ) or metrics

            clip = {
                "x": max(0, float(final_metrics["x"])),
                "y": max(0, float(final_metrics["y"])),
                "width": max(1, float(final_metrics["width"])),
                "height": max(1, float(final_metrics["height"])),
            }
            logger.debug(
                f"[gb_usage_rate] clip target={target} "
                f"x={clip['x']:.1f} y={clip['y']:.1f} "
                f"w={clip['width']:.1f} h={clip['height']:.1f} "
                f"body={float(final_metrics.get('bodyHeight', 0)):.1f} "
                f"full={float(final_metrics.get('fullHeight', 0)):.1f} "
                f"items={int(final_metrics.get('itemCount', 0))}"
            )

            return await page.screenshot(type="png", clip=clip, timeout=_TIMEOUT)
        finally:
            if browser:
                await browser.close()

    async with async_playwright() as pw:
        if not _PROXY:
            return await _capture_once(None)
        try:
            return await _capture_once(_PROXY)
        except Exception as e:
            logger.warning(
                f"[gb_usage_rate] 代理截图失败，尝试直连: proxy={_PROXY} "
                f"err={type(e).__name__}: {e}"
            )
            return await _capture_once(None)


gb_usage_cmd = on_command("#GB使用率", priority=5, block=True)


@gb_usage_cmd.handle()
async def handle_gb_usage_rate(bot: Bot, event: GroupMessageEvent, cmd_arg: Message = CommandArg()):
    target = _parse_target(cmd_arg.extract_plain_text())
    if target is None:
        await gb_usage_cmd.finish(_USAGE_TEXT)

    assert target is not None
    try:
        img_bytes = await _read_or_capture(target)
        await bot.send(event, MessageSegment.image(img_bytes))
        logger.info(
            f"[gb_usage_rate] 完成 | group={event.group_id} "
            f"target={target} label={_TARGETS[target]['label']} size={len(img_bytes)}"
        )
    except Exception as e:
        logger.exception(
            f"[gb_usage_rate] 截图失败 | group={event.group_id} "
            f"target={target} err={type(e).__name__}: {e}"
        )
        await gb_usage_cmd.finish("GBVSR 使用率页面暂时截取失败，稍后再试。")
