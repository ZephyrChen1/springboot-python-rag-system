import { useState } from 'react'
import { Alert, Typography, Upload } from 'antd'
import { InboxOutlined } from '@ant-design/icons'
import { useIngestTasks } from '../hooks/useIngestTasks.js'
import TaskList from '../components/TaskList.jsx'

const { Dragger } = Upload

// 和后端 application.yaml 的 spring.servlet.multipart.max-file-size 对齐。
// 注意 max-request-size 也是 100MB，而 multipart 还有边界和头部的额外开销，
// 所以实际可接受的文件会略小于 100MB。前端压到 95MB 更稳。
const MAX_FILE_BYTES = 95 * 1024 * 1024
const MAX_BATCH = 20

// 上游 FastAPI 目前只接受 .txt（实测：.md/.pdf/.docx 都会被
// 「不支持的文件格式: .md（支持 ['.txt']）」拒绝）。
// 这里跟上游保持一致，避免用户选了文件再吃一个错误。
// 等上游放开格式时同步改这里。
const ACCEPT = '.txt'

export default function UploadPage() {
  const { tasks, enqueueFiles, retryTask, removeTask, clearFinished } = useIngestTasks()
  const [notice, setNotice] = useState('')

  const handleBeforeUpload = (file, batch) => {
    // antd 会对同一批里的每个文件各调一次 beforeUpload，
    // 但每次传进来的 batch 都是同一个完整整批数组。
    // 只让第一个负责把整批交出去，否则拖 10 个文件会入队 100 个任务。
    const firstUid = batch?.[0]?.uid ?? file.uid
    if (file.uid !== firstUid) return Upload.LIST_IGNORE

    const files = batch || [file]
    const accepted = []
    const rejected = []

    files.forEach((f) => {
      if (f.size > MAX_FILE_BYTES) {
        rejected.push(`${f.name}（${(f.size / 1024 / 1024).toFixed(1)}MB）`)
      } else {
        accepted.push(f)
      }
    })

    if (accepted.length > 0) enqueueFiles(accepted.slice(0, MAX_BATCH))
    if (files.length > MAX_BATCH) {
      rejected.push(`单次最多 ${MAX_BATCH} 个文件，超出部分已忽略`)
    }

    setNotice(rejected.length > 0 ? `以下内容已跳过：${rejected.join('、')}` : '')

    // 关键：所有分支都必须返回 LIST_IGNORE。
    // 返回 undefined 会让 rc-upload 认为「可以上传」，然后拿空的 action 去发请求。
    // 这里把 Dragger 当纯粹的「文件选择器 + 拖拽区」用，上传流程完全自己接管。
    return Upload.LIST_IGNORE
  }

  return (
    <>
      <Dragger
        multiple
        accept={ACCEPT}
        showUploadList={false} // 列表由我们自己的任务列表渲染（语义不一样，见 TaskItem）
        beforeUpload={handleBeforeUpload}
      >
        <p className="ant-upload-drag-icon">
          <InboxOutlined />
        </p>
        <p className="ant-upload-text">点击或把文件拖到这里上传</p>
        <p className="ant-upload-hint">
          支持一次拖入多个文件，单个文件不超过 95MB。目前仅支持 .txt。
        </p>
      </Dragger>

      {notice && (
        <Alert
          type="warning"
          showIcon
          closable
          message={notice}
          onClose={() => setNotice('')}
          style={{ marginTop: 16 }}
        />
      )}

      <Typography.Paragraph type="secondary" style={{ marginTop: 12, fontSize: 12 }}>
        提示：入库服务同一时刻只处理一个任务，多个文件会按顺序逐个入库，请耐心等待。
        <br />
        进度依赖服务端任务状态，刷新页面会丢失当前进度，需要重新上传。
      </Typography.Paragraph>

      <div style={{ marginTop: 24 }}>
        <TaskList
          tasks={tasks}
          onRetry={retryTask}
          onRemove={removeTask}
          onClearFinished={clearFinished}
        />
      </div>
    </>
  )
}
