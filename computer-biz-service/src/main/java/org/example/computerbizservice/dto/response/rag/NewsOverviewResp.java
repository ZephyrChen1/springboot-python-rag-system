package org.example.computerbizservice.dto.response.rag;

import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.Data;

import java.util.List;
import java.util.Map;

/** 抓取动态页顶部概览。 */
@Data
public class NewsOverviewResp {
    /** false = crawler_state/news.db 还不存在（爬虫尚未运行过） */
    private Boolean available;

    @JsonProperty("generated_at")
    private String generatedAt;

    /** key 是 articles.status，另加 total；缺失的状态由前端按 0 处理 */
    private Map<String, Long> counts;

    private List<NewsSourceResp> sources;

    private List<NewsRunResp> runs;
}
