import React from 'react'
import { createRoot } from 'react-dom/client'
import { App as AntdApp, ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import 'antd/dist/reset.css'
import App from './App.jsx'
import './styles.css'

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <ConfigProvider locale={zhCN} theme={{ token: { colorPrimary: '#1677ff' } }}>
      {/* 用 antd 的 <App> 包一层：它提供 message / notification / modal 的
          context 版本，能拿到上面 ConfigProvider 的主题和中文 locale。
          直接调 message.success() 这种静态方法会脱离主题上下文。 */}
      <AntdApp>
        <App />
      </AntdApp>
    </ConfigProvider>
  </React.StrictMode>,
)
