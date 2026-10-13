import { Spin, Tag, Typography } from 'antd'
import { MSG } from '../chat/chatStore.js'

const { Text } = Typography

// 正常结束不打标签（一条绿标签糊在每个回答上只会变成噪音），
// 只有「没正常结束」才需要让用户一眼看见
const HINT = {
  [MSG.CANCELED]: { color: 'default', text: '已停止' },
  [MSG.INTERRUPTED]: { color: 'warning', text: '回答中断' },
  [MSG.ERROR]: { color: 'error', text: '未收到回答' },
}

/**
 * 一条消息气泡。
 *
 * 刻意只渲染纯文本（white-space: pre-wrap）：上游给的是纯文本流，
 * 引一个 markdown 渲染库意味着多一个依赖 + 一套 XSS 面，
 * 而这里真正要保证的是「一个字都不能少」和「换行不能丢」。
 */
export default function ChatMessage({ message }) {
  const mine = message.role === 'user'
  const streaming = message.status === MSG.STREAMING
  const hint = HINT[message.status]

  return (
    <div className={`chat-msg ${mine ? 'chat-msg--user' : 'chat-msg--ai'}`}>
      <div className="chat-msg__bubble">
        <div className="chat-msg__head">
          <Text type="secondary" className="chat-msg__role">
            {mine ? '我' : 'AI'}
          </Text>
          {hint && (
            <Tag color={hint.color} bordered={false}>
              {hint.text}
            </Tag>
          )}
        </div>

        {/* 还没吐第一个字：给个明确的活动指示，别让气泡是空的 */}
        {streaming && !message.text ? (
          <div className="chat-msg__pending">
            <Spin size="small" />
            <Text type="secondary">正在检索文档并生成…</Text>
          </div>
        ) : (
          <div className="chat-msg__text">
            {message.text}
            {/* 光标只在生成中闪。已经结束时留一个方块，看起来像渲染坏了 */}
            {streaming && <span className="chat-caret" />}
          </div>
        )}

        {message.error && (
          <Text type="warning" className="chat-msg__error">
            {message.error}
          </Text>
        )}
      </div>
    </div>
  )
}
