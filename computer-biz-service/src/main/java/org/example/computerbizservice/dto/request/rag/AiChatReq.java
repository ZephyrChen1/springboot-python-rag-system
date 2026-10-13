package org.example.computerbizservice.dto.request.rag;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.Data;

/**
 * 转发给 FastAPI /ai/chat 的请求体。
 *
 * ★ 类上的 @JsonInclude(NON_NULL) 不是「美化 JSON」，是**必需的**：
 * Jackson 默认会把值为 null 的字段也写出去，而 FastAPI 那边的模型是
 *   user_id: int = 0
 * 一个**非 Optional 的 int**，收到显式的 null 会直接 422：
 *   {"detail":[{"loc":["body","user_id"],"msg":"Input should be a valid integer","input":null}]}
 * 语义上「字段缺失 = 用上游默认值」，所以空值就该整个不发。
 */
@Data
@JsonInclude(JsonInclude.Include.NON_NULL)
public class AiChatReq {

    @JsonProperty("user_id")
    private Integer userId;

    @JsonProperty("session_id")
    private String sessionId;

    private String message;

    /**
     * 上游是 Optional[int] 且限定 1~100。这里用包装类型而不是 int：
     * 前端不传时保持 null，字段整个不发；若用基本类型 int，Jackson 会发出
     * "top_k": 0 —— 上游范围校验过不去，又是一个 422。
     */
    @JsonProperty("top_k")
    private Integer topK;
}
