import { useEffect, useRef, useState } from 'react'
import { Alert, App as AntdApp, Button, Card, Empty, Input, Modal, Space, Typography, Upload } from 'antd'
import {
  ClearOutlined,
  SendOutlined,
  StopOutlined,
  UploadOutlined,
  WarningOutlined,
} from '@ant-design/icons'
import AgentMessage from '../components/AgentMessage.jsx'
import { useAgentChat } from '../hooks/useAgentChat.js'
import { MAX_MESSAGE_CHARS } from '../agent/agentStore.js'
import { formatTime, isWriteTool, summarizeArgs, toolLabel } from '../agent/toolMeta.js'
import { stageAgentFile } from '../api/agent.js'

const { Text } = Typography

const STICK_THRESHOLD_PX = 40
// 与 Java multipart / Python AGENT_STAGE_MAX_BYTES 的默认值一致
const MAX_STAGE_BYTES = 100 * 1024 * 1024

export default function AgentChatPage() {
  const { messages, sessionId, streaming, sendMessage, stopStreaming, resetConversation, respondConfirm } =
    useAgentChat()
  const { message: toast } = AntdApp.useApp()

  const [draft, setDraft] = useState('')
  const [confirmLoading, setConfirmLoading] = useState(false)
  const [confirmError, setConfirmError] = useState('')
  const [staging, setStaging] = useState(false)

  const listRef = useRef(null)
  const stickRef = useRef(true)

  const lastLen = messages.length ? messages[messages.length - 1].text.length : 0
  const lastSteps = messages.length ? messages[messages.length - 1].steps?.length ?? 0 : 0

  // 正文和轨迹都会让气泡变高，两个都要触发贴底
  useEffect(() => {
    const el = listRef.current
    if (el && stickRef.current) el.scrollTop = el.scrollHeight
  }, [messages.length, lastLen, lastSteps])

  const handleScroll = () => {
    const el = listRef.current
    if (!el) return
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight <= STICK_THRESHOLD_PX
  }

  // 同一时刻只会有一个待确认操作（Python 是逐个调用、逐个等确认）
  const pendingMsg = messages.find((m) => m.pendingConfirm)
  const pending = pendingMsg?.pendingConfirm ?? null

  // 弹窗关闭（批准/拒绝/流结束）后清掉上一次的错误，别让下次打开先看到旧文案
  useEffect(() => {
    if (!pending) {
      setConfirmError('')
      setConfirmLoading(false)
    }
  }, [pending])

  const submit = () => {
    const text = draft.trim()
    if (!text || streaming) return
    const result = sendMessage(text)
    if (result === 'too_long') {
      toast.warning(`问题太长：最多 ${MAX_MESSAGE_CHARS} 字`)
      return // 保留草稿，别让用户辛苦写的内容消失
    }
    if (result !== 'ok') return
    setDraft('')
    stickRef.current = true
  }

  const handlePressEnter = (e) => {
    if (e.shiftKey) return
    e.preventDefault()
    submit()
  }

  const handleConfirm = async (approved) => {
    if (!pending || confirmLoading) return
    setConfirmLoading(true)
    setConfirmError('')
    try {
      await respondConfirm(approved)
    } catch (err) {
      setConfirmError(err?.message || '确认请求失败')
      // 404 = token 已过期：弹窗已经被 store 清掉，这里只提示，不重试
      if (err?.status === 404) toast.warning('确认已过期，Agent 会按「未批准」继续')
    } finally {
      setConfirmLoading(false)
    }
  }

  const pendingIsWrite = pending ? isWriteTool(pending.name) : false

  /**
   * 上传到暂存区，不直接入库。
   *
   * 这里只把文件名填进草稿，让用户自己按下发送 —— 「是否入库」必须由用户在
   * 对话里明确表达，Agent 再走 kb_ingest（还会再弹一次写操作确认）。
   */
  const handleStage = async (file) => {
    if (file.size > MAX_STAGE_BYTES) {
      toast.warning(`文件太大：上限 ${MAX_STAGE_BYTES / 1024 / 1024}MB`)
      return
    }
    setStaging(true)
    try {
      const staged = await stageAgentFile(file)
      setDraft(`请把「${staged.fileName}」入库`)
      stickRef.current = true
      toast.success(`已暂存 ${staged.fileName}，发送消息即可让 Agent 入库`)
    } catch (err) {
      toast.error(err?.message || '暂存失败')
    } finally {
      setStaging(false)
    }
  }

  // 返回 LIST_IGNORE：antd 不会自行发起上传，也不会把文件塞进受控列表
  const beforeStage = (file) => {
    handleStage(file)
    return Upload.LIST_IGNORE
  }

  return (
    <Card
      variant="outlined"
      title={
        <Space size={8}>
          <Text strong>Agent 问答</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            会规划并调用工具
          </Text>
        </Space>
      }
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
            description="问点什么。Agent 会自己决定是否检索知识库、查业务数据或调用其他工具。"
          />
        ) : (
          messages.map((m) => <AgentMessage key={m.id} message={m} />)
        )}
      </div>

      <div className="chat-composer">
        <Input.TextArea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onPressEnter={handlePressEnter}
          placeholder="问点什么…（Enter 发送，Shift+Enter 换行）"
          autoSize={{ minRows: 2, maxRows: 6 }}
        />

        <div className="chat-composer__actions">
          <Space size={8} wrap>
            <Upload
              accept=".txt"
              showUploadList={false}
              beforeUpload={beforeStage}
              disabled={streaming || staging}
            >
              <Button icon={<UploadOutlined />} loading={staging} disabled={streaming}>
                上传待入库文件
              </Button>
            </Upload>
            {streaming ? (
              <Button danger icon={<StopOutlined />} onClick={stopStreaming}>
                停止生成
              </Button>
            ) : (
              <Button type="primary" icon={<SendOutlined />} onClick={submit} disabled={!draft.trim()}>
                发送
              </Button>
            )}
          </Space>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {streaming
              ? '可以随时停止；已经完成的部分会保留'
              : '上传只暂存；入库/删除需要你确认后才会执行'}
          </Text>
        </div>
      </div>

      <Modal
        open={!!pending}
        title={
          <Space size={8}>
            <WarningOutlined style={{ color: '#faad14' }} />
            <span>需要确认写操作</span>
          </Space>
        }
        onOk={() => handleConfirm(true)}
        onCancel={() => handleConfirm(false)}
        okText="批准执行"
        cancelText="拒绝"
        okButtonProps={{ danger: pendingIsWrite, loading: confirmLoading }}
        cancelButtonProps={{ disabled: confirmLoading }}
        closable={false}
        maskClosable={false}
        keyboard={false}
        destroyOnHidden
      >
        {pending && (
          <div className="agent-confirm">
            <p style={{ marginTop: 0 }}>
              Agent 想执行 <Text strong>{toolLabel(pending.name)}</Text>
              <Text type="secondary" code style={{ marginLeft: 6 }}>
                {pending.name}
              </Text>
            </p>

            {summarizeArgs(pending.args) && (
              <pre className="agent-confirm__args">{JSON.stringify(pending.args, null, 2)}</pre>
            )}

            <Text type="secondary" style={{ fontSize: 12 }}>
              批准后工具会真实执行；拒绝则本次调用不执行，Agent 会继续完成回答。
              {pending.expiresAt && ` 该确认在 ${formatTime(pending.expiresAt)} 前有效。`}
            </Text>

            {confirmError && (
              <Alert
                type="error"
                showIcon
                style={{ marginTop: 12 }}
                message={confirmError}
              />
            )}
          </div>
        )}
      </Modal>
    </Card>
  )
}
