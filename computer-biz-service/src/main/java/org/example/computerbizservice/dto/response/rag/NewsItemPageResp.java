package org.example.computerbizservice.dto.response.rag;

import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.Data;

import java.util.List;

/** 抓取文章分页结果。 */
@Data
public class NewsItemPageResp {
    private Boolean available;

    private Integer page;

    @JsonProperty("page_size")
    private Integer pageSize;

    private Integer total;

    private List<NewsItemResp> items;
}
