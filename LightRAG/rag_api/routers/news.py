"""Web 端“抓取动态”只读接口。

只读 crawler_state/news.db，不触发抓取、不入库、不创建状态库。
"""

from fastapi import APIRouter, HTTPException, Query

from ..news_store import NewsStore

router = APIRouter(prefix="/v1/news", tags=["抓取动态"])


@router.get("/overview")
def news_overview(limit: int = Query(10, ge=1, le=50)):
    return NewsStore().overview(limit=limit)


@router.get("/items")
def news_items(
    status: str | None = Query(None, max_length=32),
    source_id: str | None = Query(None, max_length=64),
    q: str | None = Query(None, max_length=200),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    try:
        return NewsStore().items(
            status=status,
            source_id=source_id,
            q=q,
            page=page,
            page_size=page_size,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/items/{item_id}")
def news_item(item_id: int):
    if item_id <= 0:
        raise HTTPException(status_code=400, detail="item_id 必须大于 0")
    store = NewsStore()
    if not store.available:
        raise HTTPException(
            status_code=404,
            detail="抓取状态库尚未生成，请先运行 python -m news_ingest --once",
        )
    item = store.detail(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="文章不存在")
    return item
