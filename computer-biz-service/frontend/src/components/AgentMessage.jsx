import { Spin, Tag, Typography } from 'antd'
import AgentSteps from './AgentSteps.jsx'
import { MSG } from '../agent/agentStore.js'

const { Text } = Typography

// 和旧问答页同一套规则：正常结束不打标签，只有没正常结束才需要被看见
const HINT = {
  [MSG.CANCELED]: { color: 'default', text: '已停止' },
  [MSG.INTERRUPTED]: { color: 'warning', text: '回答中断' },
  [MSG.ERROR]: { color: 'error', text: '未收到回答' },
}

/**
 * 一条 Agent 消息。
 *
 * 结构：气泡头（角色 + 状态）→ 决策轨迹（仅助手）→ 回答正文。
 * 轨迹放在正文上方，是因为它按时间发生；把「调了什么工具」放在答案下面，
 * 会让人以为那是答案的补充说明。
 */
export default function AgentMessage({ message }) {
  const mine = message.role === 'user'
  const streaming = message.status === MSG.STREAMING
  const hint = HINT[message.status]
  const hasSteps = !mine && message.steps?.length > 0

  return (
    <div className={`chat-msg ${mine ? 'chat-msg--user' : 'chat-msg--ai'}`}>
      <div className="chat-msg__bubble">
        <div className="chat-msg__head">
          <Text type="secondary" className="chat-msg__role">
            {mine ? '我' : 'Agent'}
          </Text>

          {!mine && message.runId && (
            <Text type="secondary" className="agent-run-id" title={`run_id: ${message.runId}`}>
              run {String(message.runId).slice(0, 8)}
            </Text>
          )}

          {hint && (
            <Tag color={hint.color} bordered={false}>
              {hint.text}
            </Tag>
          )}
        </div>

        {!mine && <AgentSteps steps={message.steps} />}

        {message.pendingConfirm && (
          <div className="agent-waiting-confirm">
            <Text type="warning">等待你确认写操作「{message.pendingConfirm.name}」…</Text>
          </div>
        )}

        {streaming && !message.text ? (
          <div className="chat-msg__pending">
            <Spin size="small" />
            <Text type="secondary">
              {hasSteps ? '正在调用工具…' : '正在规划…'}
            </Text>
          </div>
        ) : (
          <div className="chat-msg__text">
            {message.text}
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
