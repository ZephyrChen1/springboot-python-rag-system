package org.example.computerbizservice.dto.response.rag;

import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.Data;

/** GET /v1/kb/ingest/{job_id} 的返回体 */
@Data
public class TaskQueryResp {
    @JsonProperty("job_id")
    private String jobId;

    @JsonProperty("file_name")
    private String fileName;

    private Boolean done;      // true = 入库已结束（成功或失败都算结束）

    /** 运行中为空串；结束后以 [成功] / [跳过] / [失败] 开头，前端可据此判断 */
    private String result;

    /** 人类可读进度文案，直接展示即可，如 "实体/关系抽取中（3 个文本块）…" */
    private String stage;

    @JsonProperty("elapsed_sec")
    private Double elapsedSec;  // Python 是 float，用 Double 别用 BigDecimal
}
