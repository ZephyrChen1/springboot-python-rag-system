import { useSyncExternalStore } from 'react'
import {
  getSnapshot,
  resetConversation,
  respondConfirm,
  sendMessage,
  stopStreaming,
  subscribe,
} from '../agent/agentStore.js'

/**
 * agentStore 的订阅壳。和 useChat 一样，组件卸载不会中断回答 ——
 * 流的生命周期属于标签页。
 */
export function useAgentChat() {
  const snap = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
  return { ...snap, sendMessage, stopStreaming, resetConversation, respondConfirm }
}
