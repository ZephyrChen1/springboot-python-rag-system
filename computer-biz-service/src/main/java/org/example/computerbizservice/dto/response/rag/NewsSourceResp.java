package org.example.computerbizservice.dto.response.rag;

import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.Data;

/** 抓取源及其最近一次 feed 抓取状态。 */
@Data
public class NewsSourceResp {
    @JsonProperty("source_id")
    private String sourceId;

    private String name;

    private Boolean enabled;

    @JsonProperty("last_fetched_at")
    private String lastFetchedAt;

    @JsonProperty("last_status")
    private Integer lastStatus;

    @JsonProperty("last_error")
    private String lastError;

    @JsonProperty("consecutive_failures")
    private Integer consecutiveFailures;
}
