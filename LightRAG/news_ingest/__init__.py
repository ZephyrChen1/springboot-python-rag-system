"""AI 前沿发布自动抓取并入库的独立进程包。

设计上刻意不依赖 agent / rag_api / lightrag_service：
抓取、清洗、筛选、状态管理都在本包内完成，入库只通过运行中
FastAPI 的 HTTP 接口，保证 LightRAG 仍然只有一个写入者。
"""
