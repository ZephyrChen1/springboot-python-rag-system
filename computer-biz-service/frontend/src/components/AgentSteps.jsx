import { useState } from 'react'
import { Collapse, Space, Spin, Tag, Typography } from 'antd'
import {
  BulbOutlined,
  CheckCircleFilled,
  CloseCircleFilled,
  ToolOutlined,
} from '@ant-design/icons'
import { formatElapsed, summarizeArgs, toolLabel } from '../agent/toolMeta.js'

const { Text } = Typography

/**
 * 一条助手消息的决策轨迹。
 *
 * 轨迹可能很长（多步工具调用），所以默认展开、可折叠 —— 用户第一次看到
 * Agent 在「想什么」时，把它藏起来反而像什么都没发生。
 */
export default function AgentSteps({ steps }) {
  const [open, setOpen] = useState(true)
  if (!steps?.length) return null

  const toolCount = steps.filter((s) => s.kind === 'tool').length

  return (
    <Collapse
      ghost
      size="small"
      className="agent-steps"
      activeKey={open ? ['steps'] : []}
      onChange={(keys) => setOpen(keys.includes('steps'))}
      items={[
        {
          key: 'steps',
          label: (
            <Space size={6} wrap>
              <Text type="secondary" style={{ fontSize: 12 }}>
                决策过程
              </Text>
              <Tag bordered={false} style={{ marginInlineEnd: 0 }}>
                {steps.length} 步
              </Tag>
              {toolCount > 0 && (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  · {toolCount} 次工具调用
                </Text>
              )}
            </Space>
          ),
          children: (
            <div className="agent-steps__body">
              {steps.map((s) => (s.kind === 'tool' ? (
                <ToolStep key={s.key} step={s} />
              ) : (
                <ThoughtStep key={s.key} step={s} />
              )))}
            </div>
          ),
        },
      ]}
    />
  )
}

function ThoughtStep({ step }) {
  return (
    <div className="agent-thought">
      <div className="agent-thought__head">
        <BulbOutlined />
        <Text type="secondary" style={{ fontSize: 12 }}>
          {step.step != null ? `第 ${step.step} 步思考` : '思考'}
        </Text>
      </div>
      <div className="agent-thought__text">{step.text}</div>
    </div>
  )
}

function ToolStep({ step }) {
  const running = step.status === 'running'
  const ok = step.status === 'ok'

  return (
    <div className={`agent-tool ${step.status === 'failed' ? 'agent-tool--failed' : ''}`}>
      <div className="agent-tool__head">
        <ToolOutlined className="agent-tool__icon" />
        <Text strong style={{ fontSize: 13 }}>
          {toolLabel(step.name)}
        </Text>
        {/* 中文标签之外把原始工具名也留着：排查时对日志有用 */}
        <Text type="secondary" className="agent-tool__name">
          {step.name}
        </Text>

        <span className="agent-tool__spacer" />

        {running && <Spin size="small" />}
        {!running && <span className={`agent-tool__status agent-tool__status--${ok ? 'ok' : 'failed'}`}>
          {ok ? <CheckCircleFilled /> : <CloseCircleFilled />}
          <span>{ok ? '成功' : '失败'}</span>
        </span>}
        {formatElapsed(step.elapsedMs) && (
          <Text type="secondary" className="agent-tool__elapsed">
            {formatElapsed(step.elapsedMs)}
          </Text>
        )}
      </div>

      {summarizeArgs(step.args) && (
        <div className="agent-tool__args">
          <Text type="secondary">参数：</Text>
          <code>{summarizeArgs(step.args)}</code>
        </div>
      )}

      {step.summary && (
        <div className="agent-tool__summary">{step.summary}</div>
      )}

      {step.errorCode && (
        <Text type="warning" className="agent-tool__error">
          {step.errorCode}
        </Text>
      )}
    </div>
  )
}
