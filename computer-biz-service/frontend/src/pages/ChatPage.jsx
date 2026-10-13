import { useEffect, useRef, useState } from 'react'
import { Button, Card, Empty, Input, Space, Typography } from 'antd'
import { ClearOutlined, SendOutlined, StopOutlined } from '@ant-design/icons'
import ChatMessage from '../components/ChatMessage.jsx'
import { useChat } from '../hooks/useChat.js'

const { Text } = Typography

/** 贴底滚动的容忍距离：离底部 40px 以内才算「还在看最新内容」 */
const STICK_THRESHOLD_PX = 40

export default function ChatPage() {
  const { messages, sessionId, streaming, sendMessage, stopStreaming, resetConversation } = useChat()
  const [draft, setDraft] = useState('')
  const listRef = useRef(null)
  // 用户往上翻看历史时，不能被新到的增量硬拽回底部 ——
  // 生成中的回答会一直涨，这个问题尤其烦人
  const stickRef = useRef(true)

  // 依赖里放最后一条消息的文本长度，才能做到「每来一段增量就滚一次」
  const lastLen = messages.length ? messages[messages.length - 1].text.length : 0

  useEffect(() => {
    const el = listRef.current
    if (el && stickRef.current) el.scrollTop = el.scrollHeight
  }, [messages.length, lastLen])

  const handleScroll = () => {
    const el = listRef.current
    if (!el) return
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight <= STICK_THRESHOLD_PX
  }

  const submit = () => {
    const text = draft.trim()
    if (!text || streaming) return
    setDraft('')
    stickRef.current = true // 自己发出去的东西一定要看见
    sendMessage(text)
  }

  const handlePressEnter = (e) => {
    if (e.shiftKey) return // Shift+Enter 换行
    e.preventDefault()
    submit()
  }

  return (
    <Card
      variant="outlined"
      title={<Text strong>知识库问答</Text>}
      extra={
        <Space size={8}>
          <Text type="secondary" style={{ fontSize: 12 }} title={sessionId}>
            会话 {sessionId.slice(0, 8)}
          </Text>
          <Button
            size="small"
            icon={<ClearOutlined />}
            onClick={resetConversation}
            disabled={messages.length === 0}
          >
            重新开始
          </Button>
        </Space>
      }
    >
      <div className="chat-list" ref={listRef} onScroll={handleScroll}>
        {messages.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="还没有对话。先到「文档入库」上传资料，再回来提问。"
          />
        ) : (
          messages.map((m) => <ChatMessage key={m.id} message={m} />)
        )}
      </div>

      <div className="chat-composer">
        {/* 生成中不禁用输入框：用户可以先把下一个问题打好，
            禁掉只会让人等。真正的闸门在 store 里（streaming 时 sendMessage 直接返回） */}
        <Input.TextArea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onPressEnter={handlePressEnter}
          placeholder="问点什么…（Enter 发送，Shift+Enter 换行）"
          autoSize={{ minRows: 2, maxRows: 6 }}
        />

        <div className="chat-composer__actions">
          {streaming ? (
            <Button danger icon={<StopOutlined />} onClick={stopStreaming}>
              停止生成
            </Button>
          ) : (
            <Button type="primary" icon={<SendOutlined />} onClick={submit} disabled={!draft.trim()}>
              发送
            </Button>
          )}
          <Text type="secondary" style={{ fontSize: 12 }}>
            {streaming
              ? '可以随时停止，已经生成的部分会保留'
              : '同一个会话内会带上上下文；刷新页面等于开一个新会话'}
          </Text>
        </div>
      </div>
    </Card>
  )
}
