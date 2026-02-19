# -*- coding: utf-8 -*-
"""测试飞书 MCP 卡片发送"""
import asyncio
import json
import httpx

APP_ID = "cli_a904cdcca7b89ceb"
APP_SECRET = "eJDwHiE7urNKhWDZI5TqxeAjWiPMeSdh"


async def get_token():
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json={"app_id": APP_ID, "app_secret": APP_SECRET})
        data = resp.json()
        print(f"Token 响应: {data}")
        return data.get("tenant_access_token")


async def send_card(token, open_id, title, content):
    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }

    card_content = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "blue"
        },
        "elements": [
            {"tag": "markdown", "content": content}
        ]
    }

    payload = {
        "receive_id": open_id,
        "msg_type": "interactive",
        "content": json.dumps(card_content, ensure_ascii=False),
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        result = resp.json()
        print(f"发送响应: {result}")
        return result


async def main():
    print("=" * 50)
    print("飞书卡片发送测试")
    print("=" * 50)

    # 1. 获取 token
    token = await get_token()
    if not token:
        print("❌ 获取 token 失败")
        return

    print(f"✅ Token 获取成功: {token[:20]}...")

    # 2. 发送卡片（需要真实的 open_id）
    # 这里用测试 ID，会返回错误但能看到 API 是否正常
    test_open_id = input("请输入你的 open_id（从 app.py 日志中获取）: ").strip()

    result = await send_card(token, test_open_id, "测试标题", "这是测试内容")
    if result.get("code") == 0:
        print("✅ 卡片发送成功!")
    else:
        print(f"❌ 发送失败: {result}")


if __name__ == "__main__":
    asyncio.run(main())
