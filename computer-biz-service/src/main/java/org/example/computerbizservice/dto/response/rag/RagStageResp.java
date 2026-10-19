package org.example.computerbizservice.dto.response.rag;

import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.Data;

/**
 * 上游 POST /v1/kb/stage 的返回：只暂存、不入库。
 *
 * <p>文件内容不经过模型：前端拿到 staged_id 后，由用户在对话框里说
 * 「把这份文档入库」，Agent 的 kb_ingest 再按 staged_id / 文件名读取。
 * 前端也可以用文件名让 Agent 解析最近一次同名暂存记录。
 */
@Data
public class RagStageResp {
    @JsonProperty("staged_id")
    private String stagedId;

    @JsonProperty("file_name")
    private String fileName;

    /** UTF-8 原始文件的字节数 */
    private Long size;
}
