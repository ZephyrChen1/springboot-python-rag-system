import { useSyncExternalStore } from 'react'
import { getSnapshot, resetConversation, sendMessage, stopStreaming, subscribe } from '../chat/chatStore.js'

/**
 * 只是 chatStore 的订阅壳，和 useIngestTasks 一个写法。
 *
 * 组件卸载不会中断正在生成的回答 —— 流的生命周期属于这个标签页，
 * 不属于组件。所以 StrictMode 的「挂载→卸载→再挂载」在这里是无害操作。
 */
export function useChat() {
  const snap = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
  return { ...snap, sendMessage, stopStreaming, resetConversation }
}
