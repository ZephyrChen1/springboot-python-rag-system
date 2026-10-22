package org.example.computerbizservice.controller;

import lombok.RequiredArgsConstructor;
import org.example.computerbizservice.dto.response.rag.NewsItemPageResp;
import org.example.computerbizservice.dto.response.rag.NewsItemResp;
import org.example.computerbizservice.dto.response.rag.NewsOverviewResp;
import org.example.computerbizservice.service.NewsService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * 抓取动态：把 news_ingest 的 SQLite 状态通过 FastAPI 只读展示给 Web。
 *
 * <p>这些接口不触发抓取、不写入知识库，也不落 MySQL。
 */
@RestController
@RequestMapping("/api/rag/news")
@RequiredArgsConstructor
public class NewsController {

    private final NewsService newsService;

    /** 统计、源状态、最近运行记录 */
    @GetMapping("/overview")
    public NewsOverviewResp overview(@RequestParam(defaultValue = "10") int limit) {
        return newsService.overview(limit);
    }

    /** 抓取文章分页列表 */
    @GetMapping("/items")
    public NewsItemPageResp items(
            @RequestParam(required = false) String status,
            @RequestParam(name = "source_id", required = false) String sourceId,
            @RequestParam(required = false) String q,
            @RequestParam(defaultValue = "1") int page,
            @RequestParam(name = "page_size", defaultValue = "20") int pageSize) {
        return newsService.items(status, sourceId, q, page, pageSize);
    }

    /** 单篇文章详情（摘要、关键事实、正文） */
    @GetMapping("/items/{id}")
    public NewsItemResp item(@PathVariable long id) {
        return newsService.item(id);
    }
}
