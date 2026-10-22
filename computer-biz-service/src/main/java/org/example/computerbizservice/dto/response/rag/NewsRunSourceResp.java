package org.example.computerbizservice.dto.response.rag;

import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.Data;

/** 某一轮抓取中单个源的统计。 */
@Data
public class NewsRunSourceResp {
    @JsonProperty("source_id")
    private String sourceId;

    @JsonProperty("source_name")
    private String sourceName;

    private Integer fetched;

    private Integer candidates;

    private Integer filtered;

    private Integer ingested;

    private Integer skipped;

    private Integer failed;

    private String error;
}
