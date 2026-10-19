package org.example.computerbizservice.dto.request.rag;

import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.Data;

/**
 * 前端 → Java → FastAPI /ai/agent/chat 的请求体。
 *
 * <p>★ 这里<b>没有 user_id</b>：身份由 Java 在转发时通过 {@code X-User-Id} 请求头注入。
 * 前端传什么用户身份都不认 —— 没有鉴权的现在，「客户端说我是谁就是谁」是白送的越权。
 * 上游的 {@code AgentChatRequest} 也不接收请求体里的 user_id。
 */
@Data
public class AgentChatReq {

    @JsonProperty("session_id")
    private String sessionId;

    private String message;
}
