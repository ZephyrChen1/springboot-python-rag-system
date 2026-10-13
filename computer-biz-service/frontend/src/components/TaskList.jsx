import { Button, Card, Empty, Typography } from 'antd'
import TaskItem from './TaskItem.jsx'
import { isTerminal } from '../ingest/ingestStore.js'

const { Text } = Typography

export default function TaskList({ tasks, onRetry, onRemove, onClearFinished }) {
  const finishedCount = tasks.filter((t) => isTerminal(t.status)).length

  if (tasks.length === 0) {
    return (
      <Card variant="outlined">
        <Empty description="还没有入库任务，拖入文件开始吧" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      </Card>
    )
  }

  return (
    <Card
      variant="outlined"
      title={<Text strong>入库任务（{tasks.length}）</Text>}
      extra={
        finishedCount > 0 && (
          <Button type="link" size="small" onClick={onClearFinished}>
            清理已完成（{finishedCount}）
          </Button>
        )
      }
    >
      <div className="task-list">
        {tasks.map((task) => (
          <TaskItem key={task.id} task={task} onRetry={onRetry} onRemove={onRemove} />
        ))}
      </div>
    </Card>
  )
}
