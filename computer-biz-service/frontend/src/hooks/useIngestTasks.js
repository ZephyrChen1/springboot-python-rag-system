import { useSyncExternalStore } from 'react'
import {
  clearFinished,
  enqueueFiles,
  getSnapshot,
  removeTask,
  retryTask,
  subscribe,
} from '../ingest/ingestStore.js'

/**
 * 只是 store 的订阅壳。
 *
 * 组件卸载不会停止轮询 —— 轮询的生命周期属于这个标签页，不属于组件。
 * 这也让 React StrictMode 的「挂载→卸载→再挂载」探测变成无害操作。
 */
export function useIngestTasks() {
  const { tasks } = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
  return { tasks, enqueueFiles, retryTask, removeTask, clearFinished }
}
