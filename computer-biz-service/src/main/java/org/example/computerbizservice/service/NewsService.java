package org.example.computerbizservice.service;

import lombok.RequiredArgsConstructor;
import org.example.computerbizservice.client.RagClient;
import org.example.computerbizservice.dto.response.rag.NewsItemPageResp;
import org.example.computerbizservice.dto.response.rag.NewsItemResp;
import org.example.computerbizservice.dto.response.rag.NewsOverviewResp;
import org.springframework.stereotype.Service;

/**
 * 抓取动态的只读业务层：只做边界参数校验，然后原样转发给 FastAPI。
 *
 * <p>这条链路不写 MySQL，也不触发抓取/入库，因此没有 {@link RagService}
 * 里那套旁路落库逻辑。
 */
@Service
@RequiredArgsConstructor
public class NewsService {

    private static final int MAX_LIMIT = 50;
    private static final int MAX_PAGE_SIZE = 100;
    private static final int MAX_QUERY_CHARS = 200;

    private final RagClient ragClient;

    public NewsOverviewResp overview(int limit) {
        if (limit < 1) {
            throw new IllegalArgumentException("limit 必须大于 0");
        }
        return ragClient.newsOverview(Math.min(limit, MAX_LIMIT));
    }

    public NewsItemPageResp items(String status, String sourceId, String q, int page, int pageSize) {
        if (page < 1) {
            throw new IllegalArgumentException("page 必须大于 0");
        }
        if (pageSize < 1 || pageSize > MAX_PAGE_SIZE) {
            throw new IllegalArgumentException("page_size 必须在 1..100 之间");
        }
        String cleanQ = blankToNull(q);
        if (cleanQ != null && cleanQ.length() > MAX_QUERY_CHARS) {
            throw new IllegalArgumentException("q 最长 200 个字符");
        }
        String cleanStatus = blankToNull(status);
        if ("all".equalsIgnoreCase(cleanStatus)) {
            cleanStatus = null;
        }
        return ragClient.newsItems(cleanStatus, blankToNull(sourceId), cleanQ, page, pageSize);
    }

    public NewsItemResp item(long id) {
        if (id <= 0) {
            throw new IllegalArgumentException("id 必须大于 0");
        }
        return ragClient.newsItem(id);
    }

    private static String blankToNull(String value) {
        return value == null || value.isBlank() ? null : value.trim();
    }
}
