# 测试文件上传功能

## 基本信息

- 操作：获取群聊消息成功
- 状态：✅ 成功

## 数据详情

### 群聊ID
`oc_test123`

### 总数
20 条消息

### 消息列表

1. [text] - ID: `om_msg_001` - 时间: 1700000001 - 发送者: `ou_user_001`
2. [text] - ID: `om_msg_002` - 时间: 1700000002 - 发送者: `ou_user_002`
3. [image] - ID: `om_msg_003` - 时间: 1700000003 - 发送者: `ou_user_003`
4. [post] - ID: `om_msg_004` - 时间: 1700000004 - 发送者: `ou_user_001`
5. [file] - ID: `om_msg_005` - 时间: 1700000005 - 发送者: `ou_user_002`
6. [text] - ID: `om_msg_006` - 时间: 1700000006 - 发送者: `ou_user_003`
7. [text] - ID: `om_msg_007` - 时间: 1700000007 - 发送者: `ou_user_001`
8. [image] - ID: `om_msg_008` - 时间: 1700000008 - 发送者: `ou_user_002`
9. [post] - ID: `om_msg_009` - 时间: 1700000009 - 发送者: `ou_user_003`
10. [text] - ID: `om_msg_010` - 时间: 1700000010 - 发送者: `ou_user_001`

... 共20条消息

### 详细内容示例

```json
{
  "message_id": "om_msg_001",
  "msg_type": "text",
  "content": {
    "text": "这是一段测试消息内容，用于验证文件上传功能是否正常工作。"
  },
  "sender_id": {
    "user_id": "ou_123456789",
    "open_id": "ou_abcdefg"
  },
  "create_time": "1700000001",
  "update_time": "1700000001"
}
```

---

*由 Feishu MCP 工具自动生成*
