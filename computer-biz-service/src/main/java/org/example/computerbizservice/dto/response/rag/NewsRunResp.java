package org.example.computerbizservice.dto.response.rag;

import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.Data;

import java.util.List;

/** 一轮 news_ingest 运行的摘要。 */
@Data
public class NewsRunResp {
    @JsonProperty("run_id")
    private String runId;

    @JsonProperty("started_at")
    private String startedAt;

    @JsonProperty("finished_at")
    private String finishedAt;

    private String mode;

    @JsonProperty("exit_code")
    private Integer exitCode;

    @JsonProperty("elapsed_s")
    private Double elapsedS;

    private Integer fetched;

    private Integer candidates;

    private Integer filtered;

    /** 本轮过期清理标记为 filtered_out(too_old) 的篇数。 */
    private Integer expired;

    private Integer ingested;

    private Integer skipped;

    private Integer failed;

    private Integer pending;

    @JsonProperty("sources_ok")
    private Integer sourcesOk;

    @JsonProperty("sources_failed")
    private Integer sourcesFailed;

    @JsonProperty("fatal_error")
    private String fatalError;

    private List<NewsRunSourceResp> sources;
}
