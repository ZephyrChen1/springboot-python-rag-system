import { Layout, Tabs, Typography } from 'antd'
import AgentChatPage from './pages/AgentChatPage.jsx'
import ChatPage from './pages/ChatPage.jsx'
import UploadPage from './pages/UploadPage.jsx'

const { Header, Content } = Layout

// 入库和问答放两个页签：它们是两条独立的链路，谁也不依赖谁的界面。
// 注意 Tabs 一旦渲染过就不再卸载（antd 默认 keep-mounted），
// 而两个页面的状态本来也都活在模块级 store 里，切走再切回不会丢。
// Agent 问答和旧问答并存：旧链路是「检索 → 生成」的对照组，
// 新链路是「规划 → 工具 → 收敛」。回滚只需要把 Agent 页签摘掉。
const TABS = [
  { key: 'ingest', label: '文档入库', children: <UploadPage /> },
  { key: 'chat', label: '知识库问答', children: <ChatPage /> },
  { key: 'agent', label: 'Agent 问答', children: <AgentChatPage /> },
]

export default function App() {
  return (
    <Layout style={{ minHeight: '100vh', background: '#f5f5f5' }}>
      <Header
        style={{
          background: '#fff',
          borderBottom: '1px solid #f0f0f0',
          paddingInline: 24,
        }}
      >
        <Typography.Title level={4} style={{ margin: '16px 0' }}>
          知识库
        </Typography.Title>
      </Header>

      <Content style={{ padding: 24 }}>
        <div style={{ maxWidth: 880, margin: '0 auto' }}>
          <Tabs defaultActiveKey="ingest" items={TABS} />
        </div>
      </Content>
    </Layout>
  )
}
