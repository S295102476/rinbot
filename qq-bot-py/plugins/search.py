import json
import io
import re
import httpx
import yaml
from datetime import datetime, timedelta
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.params import CommandArg
from nonebot.adapters.onebot.v11 import Message
import redis as redis_lib
from minio import Minio

with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

ai_cfg = config["ai"]
minio_cfg = config["meme"]["minio"]
rds = redis_lib.Redis(host=config["redis"]["host"], port=config["redis"]["port"], decode_responses=True)

_minio = Minio(
    minio_cfg["endpoint"],
    access_key=minio_cfg["access_key"],
    secret_key=minio_cfg["secret_key"],
    secure=minio_cfg.get("secure", False),
)
_FILE_BUCKET = "generated-files"

# ── 文件生成触发关键词及对应后缀 ──
_FILE_KEYWORDS: list[tuple[list[str], str]] = [
    (["html", "网页", "前端页面", "h5"], "html"),
    (["python", ".py", "py文件", "python文件"], "py"),
    (["markdown", ".md", "md文件"], "md"),
    (["json文件", ".json"], "json"),
    (["csv文件", ".csv"], "csv"),
    (["txt文件", ".txt", "文本文件"], "txt"),
    (["代码文件", "发送文件", "生成文件", "给我文件", "直接发送"], None),
]


def _detect_file_request(message: str) -> str | None:
    """检测消息中是否包含文件生成意图，返回后缀或 None。"""
    msg_lower = message.lower()
    matched_ext = None
    has_file_intent = False

    for keywords, ext in _FILE_KEYWORDS:
        for kw in keywords:
            if kw in msg_lower:
                if ext:
                    matched_ext = ext
                has_file_intent = True
                break

    if not has_file_intent:
        return None
    return matched_ext or "txt"


search_cmd = on_command("#搜索", priority=5, block=True)

@search_cmd.handle()
async def handle_search(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    keyword = args.extract_plain_text().strip()
    if not keyword:
        await search_cmd.send("请输入搜索内容，例如：#搜索 Java是什么")
        return

    # 检测是否为文件生成请求
    file_ext = _detect_file_request(keyword)
    if file_ext:
        await _handle_file_request(bot, event, keyword, file_ext)
        return

    reply = await search(event.user_id, keyword)
    await search_cmd.send(reply)


async def search(user_id: int, user_message: str) -> str:
    key = f"ai:search:{user_id}"
    raw_history = rds.lrange(key, 0, -1)
    history = [json.loads(item) for item in raw_history]

    messages = [{"role": "system", "content": ai_cfg["search_system_prompt"]}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                ai_cfg["api_url"],
                headers={"Authorization": f"Bearer {ai_cfg['api_key']}"},
                json={"model": ai_cfg["model"], "messages": messages}
            )
            reply = resp.json()["choices"][0]["message"]["content"].strip()

        rds.rpush(key, json.dumps({"role": "user", "content": user_message}, ensure_ascii=False))
        rds.rpush(key, json.dumps({"role": "assistant", "content": reply}, ensure_ascii=False))
        size = rds.llen(key)
        if size > 100:
            rds.ltrim(key, size - 100, -1)

        return reply
    except Exception as e:
        return f"搜索出错了：{e}"


async def _handle_file_request(bot: Bot, event: GroupMessageEvent, message: str, ext: str):
    """处理文件生成请求：调用 AI 生成纯文件内容，写入临时文件，上传到群文件。"""
    group_id = event.group_id

    ext_desc = {
        "html": "一个完整的 HTML 文件（含内联 CSS 和 JS）",
        "py": "一个完整的 Python 脚本",
        "md": "一个完整的 Markdown 文档",
        "json": "一个合法的 JSON 文件",
        "csv": "一个 CSV 格式数据文件（首行为表头）",
        "txt": "一个纯文本文件",
    }.get(ext, "一个文本文件")

    system_prompt = (
        f"你是一个文件生成助手。根据用户需求，生成{ext_desc}。"
        f"要求：\n"
        f"1. 第一行只输出你为该文件取的文件名（不含后缀，后缀由系统添加），文件名用英文、数字或下划线，简洁概括内容，例如：company_intro\n"
        f"2. 第二行开始输出文件的纯内容\n"
        f"3. 不要添加任何额外解释、说明文字\n"
        f"4. 不要用 markdown 代码块包裹"
    )

    await bot.send_group_msg(group_id=group_id, message="正在生成文件，请稍等……")

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                ai_cfg["api_url"],
                headers={"Authorization": f"Bearer {ai_cfg['api_key']}"},
                json={
                    "model": ai_cfg["model"],
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": message},
                    ],
                },
            )
            file_content = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        await bot.send_group_msg(group_id=group_id, message=f"文件生成失败：{e}")
        return

    # 剥离 markdown 代码块
    file_content = re.sub(r"^```[^\n]*\n", "", file_content)
    file_content = re.sub(r"\n```$", "", file_content)

    # 解析第一行作为文件名，其余为文件内容
    lines = file_content.split("\n", 1)
    ai_filename = lines[0].strip()
    file_body = lines[1] if len(lines) > 1 else file_content

    # 清理文件名：只保留字母、数字、下划线、中划线
    ai_filename = re.sub(r"[^\w\-]", "_", ai_filename)
    if not ai_filename or len(ai_filename) > 80:
        ai_filename = f"ai_output_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    # 再次剥离内容开头可能残留的代码块标记
    file_body = re.sub(r"^```[^\n]*\n", "", file_body)
    file_body = re.sub(r"\n```$", "", file_body)

    filename = f"{ai_filename}.{ext}"

    # 上传到 MinIO，生成 presigned URL 供 NapCat 下载
    try:
        if not _minio.bucket_exists(_FILE_BUCKET):
            _minio.make_bucket(_FILE_BUCKET)

        file_bytes = file_body.encode("utf-8")
        _minio.put_object(
            _FILE_BUCKET,
            filename,
            io.BytesIO(file_bytes),
            length=len(file_bytes),
            content_type="application/octet-stream",
        )
        file_url = _minio.presigned_get_object(_FILE_BUCKET, filename, expires=timedelta(hours=24))

        await bot.call_api(
            "upload_group_file",
            group_id=group_id,
            file=file_url,
            name=filename,
        )
        await bot.send_group_msg(group_id=group_id, message=f"✅ 文件已生成，请在群文件中查看：{filename}")
    except Exception as e:
        try:
            preview = file_body[:1800] + ("\n…（内容已截断）" if len(file_body) > 1800 else "")
            await bot.send_group_msg(
                group_id=group_id,
                message=f"⚠️ 文件上传失败（{e}），以下为文件内容：\n\n{preview}",
            )
        except Exception:
            pass  # bot 可能已离线，静默忽略