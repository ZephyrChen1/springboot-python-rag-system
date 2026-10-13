import { useEffect, useState } from 'react'
import { Button, Progress, Space, Spin, Tag, Typography } from 'antd'
import {
  CheckCircleFilled,
  CloseCircleFilled,
  DeleteOutlined,
  MinusCircleFilled,
  QuestionCircleFilled,
  RedoOutlined,
} from '@ant-design/icons'
import { STATUS } from '../ingest/ingestStore.js'
import { formatSize } from '../utils/format.js'

const { Text, Paragraph } = Typography

const OUTCOME = {
  [STATUS.SUCCESS]: { color: 'success', icon: <CheckCircleFilled />, text: '成功' },
  [STATUS.SKIPPED]: { color: 'processing', icon: <MinusCircleFilled />, text: '跳过' },
  [STATUS.FAILED]: { color: 'error', icon: <CloseCircleFilled />, text: '失败' },
  [STATUS.UNKNOWN]: { color: 'warning', icon: <QuestionCircleFilled />, text: '状态未知' },
}

// 只有 INGESTING 需要本地心跳。COMMITTING 通常不到 1 秒，不必上心跳
const LIVE = new Set([STATUS.INGESTING])

export default function TaskItem({ task, onRetry, onRemove }) {
  const live = LIVE.has(task.status)
  const [, forceTick] = useState(0)

  // 服务端 elapsed_sec 每 1~5 秒才更新一次，直接展示会一跳一跳。
  // 本地用 1s 心跳从服务端基准值往外插值，读数才是连续走动的。
  useEffect(() => {
    if (!live) return undefined
    const timer = setInterval(() => forceTick((n) => n + 1), 1000)
    return () => clearInterval(timer)
  }, [live])

  const elapsed =
    task.elapsedSec == null
      ? null
      : task.elapsedSec +
        (live && task.elapsedAt ? (Date.now() - task.elapsedAt) / 1000 : 0)

  const outcome = OUTCOME[task.status]
  const canRetry = task.status === STATUS.FAILED || task.status === STATUS.UNKNOWN
  const uploading = task.status === STATUS.UPLOADING || task.status === STATUS.COMMITTING

  return (
    <div className="task-row">
      <div className="task-row__head">
        <Text strong ellipsis={{ tooltip: task.fileName }} className="task-row__name">
          {task.fileName}
        </Text>
        {task.fileSize > 0 && (
          <Text type="secondary" className="task-row__size">
            {formatSize(task.fileSize)}
          </Text>
        )}
        {outcome && (
          <Tag color={outcome.color} icon={outcome.icon}>
            {outcome.text}
          </Tag>
        )}
      </div>

      {/* 阶段 A：字节在传，这是真实的百分比 */}
      {(task.status === STATUS.QUEUED || task.status === STATUS.UPLOADING) && (
        <div className="task-row__phase">
          <Progress
            percent={task.status === STATUS.QUEUED ? 0 : task.percent}
            size="small"
            status={task.status === STATUS.QUEUED ? 'normal' : 'active'}
            showInfo={false}
          />
          <Text type="secondary">
            {task.status === STATUS.QUEUED ? '排队中…' : `上传中 ${task.percent}%`}
          </Text>
        </div>
      )}

      {/* 阶段 A→B 之间的空档：字节发完了，但服务端还没受理完。
          这一段最容易被漏掉 —— onUploadProgress 到 100% 只代表数据进了
          本地 socket 缓冲区，距离拿到 job_id 还有一段时间 */}
      {task.status === STATUS.COMMITTING && (
        <div className="task-row__phase">
          <Progress percent={100} size="small" status="active" showInfo={false} />
          <Text type="secondary">文件已发送，等待服务端受理…</Text>
        </div>
      )}

      {/* 阶段 B：没有任何数字进度可用（后端只给人类可读的 stage 文案）。
          刻意不用「percent=100 的进度条」—— 那会渲染成一根填满的条，
          用户读作"已完成"，然后卡住几十秒，比转圈更像 bug。 */}
      {task.status === STATUS.INGESTING && (
        <div className="task-row__phase">
          <Spin size="small" />
          <Text type="secondary" className="task-row__stage">
            {task.stage || '入库处理中…'}
            {elapsed != null && `（已耗时 ${elapsed.toFixed(0)}s）`}
          </Text>
        </div>
      )}

      {/* 终态：展示服务端原文，"[跳过]" 也是完整信息 */}
      {outcome && task.result && (
        <Paragraph
          type="secondary"
          className="task-row__result"
          ellipsis={{ rows: 3, expandable: true, symbol: '展开' }}
        >
          {task.result}
        </Paragraph>
      )}

      {/* 重试中的提示：此时状态仍是 INGESTING，stage 和耗时都保留着 */}
      {task.error && (
        <Text type={outcome ? 'secondary' : 'warning'} className="task-row__error">
          {task.error}
        </Text>
      )}

      <Space size={4} className="task-row__actions">
        {canRetry && (
          <Button
            type="link"
            size="small"
            icon={<RedoOutlined />}
            onClick={() => onRetry(task.id)}
          >
            重试
          </Button>
        )}
        <Button
          type="link"
          size="small"
          icon={<DeleteOutlined />}
          onClick={() => onRemove(task.id)}
          disabled={uploading}
        >
          移除
        </Button>
      </Space>
    </div>
  )
}
