# -*- coding: utf-8 -*-
"""测试文件上传功能"""
import asyncio
import os
from dotenv import load_dotenv
load_dotenv()

# 确保能找到 feishu_mcp 模块
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from feishu_mcp import get_feishu_client, FeishuClient

async def test_upload():
    """测试文件上传"""
    # 使用环境变量
    app_id = os.environ.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET")
    open_id = os.environ.get("FEISHU_MY_ADMIN_OPEN_ID")

    if not app_id or not app_secret:
        print("请在 .env 文件中配置 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
        return

    client = FeishuClient(app_id, app_secret)

    # 测试上传文件
    file_path = "test_file.md"
    print(f"正在上传文件: {file_path}")

    file_key = await client.upload_file(file_path, "stream")

    if file_key:
        print(f"文件上传成功! file_key: {file_key}")

        # 发送文件消息
        if open_id:
            result = await client.send_file_message(open_id, file_key)
            print(f"发送结果: {result}")
            if result.get("code") == 0:
                print("✅ 文件消息已发送给用户!")
            else:
                print(f"❌ 发送失败: {result}")
        else:
            print("未配置 OPEN_ID，跳过发送")
    else:
        print("❌ 文件上传失败")

if __name__ == "__main__":
    asyncio.run(test_upload())
