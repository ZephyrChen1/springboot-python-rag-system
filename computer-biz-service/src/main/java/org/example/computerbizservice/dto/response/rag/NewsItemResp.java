package org.example.computerbizservice.dto.response.rag;

import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.Data;

import java.util.List;

/**
 * 抓取文章。列表接口只填基础字段；详情接口再填 keyFacts/content 等。
 */
@Data
public class NewsItemResp {
    private Long id;

    @JsonProperty("source_id")
    private String sourceId;

    @JsonProperty("source_name")
    private String sourceName;

    private String title;

    @JsonProperty("title_zh")
    private String titleZh;

    @JsonProperty("summary_zh")
    private String summaryZh;

    /** 列表用摘要片段；没有中文摘要时来自清洗后的 feed 内容 */
    private String excerpt;

    private String status;

    private String category;

    private Integer importance;

    @JsonProperty("published_at")
    private String publishedAt;

    @JsonProperty("fetched_at")
    private String fetchedAt;

    @JsonProperty("doc_filename")
    private String docFilename;

    @JsonProperty("canonical_url")
    private String canonicalUrl;

    /** 规则/LLM 判定理由 */
    private String reason;

    @JsonProperty("last_error")
    private String lastError;

    // ---- 详情字段（列表返回时为 null） ----

    @JsonProperty("key_facts")
    private List<String> keyFacts;

    /** 生成的 KB 文档正文；没有文档时是清洗后的 feed 正文 */
    private String content;

    /** document / feed / none */
    @JsonProperty("content_kind")
    private String contentKind;

    @JsonProperty("ingest_result")
    private String ingestResult;

    @JsonProperty("job_id")
    private String jobId;

    @JsonProperty("retry_count")
    private Integer retryCount;

    @JsonProperty("next_retry_at")
    private String nextRetryAt;

    @JsonProperty("updated_at")
    private String updatedAt;
}
