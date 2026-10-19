package org.example.computerbizservice.dto.agent;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.Data;

import java.util.Map;

/**
 * 上游 /ai/agent/chat 的 NDJSON 事件在 Java 侧的<b>只读视图</b>。
 *
 * <p>它只用于「攒回答正文」和「落 chat_step」这两件旁路，<b>不参与转发</b>：
 * 转发给前端的永远是上游那一行原始 JSON（原样，零解释）。原因和 chat_message.content
 * 存上游原始文本是同一条 —— 在 Java 侧再翻译一遍事件协议，就是在维护第二份前端契约。
 *
 * <p>字段名以 {@code agent/events.py} 为准。{@code @JsonIgnoreProperties(ignoreUnknown=true)}
 * 让 Python 侧新增字���时 Java 不需要跟着改（协议是向后兼容的）。
 */
@Data
@JsonIgnoreProperties(ignoreUnknown = true)
public class AgentEventView {

    /** 协议版本 */
    private Integer v;

    /** start / thought / tool_call / tool_result / confirm_required / answer / done / error */
    private String type;

    @JsonProperty("run_id")
    private String runId;

    /** 事件时间戳（epoch 毫秒），用来算工具耗时 */
    private Long ts;

    @JsonProperty("session_id")
    private String sessionId;

    private Integer step;

    @JsonProperty("call_id")
    private String callId;

    /** tool_call / tool_result / confirm_required 的工具名 */
    private String name;

    /** thought 事件的模型思考文本 */
    private String text;

    /** answer 事件的增量正文 */
    private String delta;

    /** tool_result 是否成功 */
    private Boolean ok;

    /** tool_result 给模型看的摘要（已在上游截断） */
    private String summary;

    /** tool_result 的失败码（timeout / unknown_tool / ...） */
    private String error;

    /** error 事件的失败码 */
    private String code;

    /** error 事件给用户看的说明 */
    private String message;

    /** confirm_required 的确认令牌 */
    private String token;

    @JsonProperty("expires_at")
    private Double expiresAt;

    /** done 事件的总步数 */
    private Integer steps;

    /** tool_call / confirm_required 的参数对象 */
    private Map<String, Object> args;

    /** tool_result 的 data 是否因过大被上游省略 */
    private Boolean truncated;

    /**
     * tool_result 由 Python 实测的工具耗时（毫秒）。
     *
     * <p>比「tool_result.ts - tool_call.ts」更准：两侧时钟可能有偏差，
     * 而且这个值不包含网络分帧的抖动。缺失时才回退到 ts 相减。
     */
    @JsonProperty("elapsed_ms")
    private Integer elapsedMs;
}
