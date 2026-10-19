package org.example.computerbizservice.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * chat_step 表：agent 一次 run 的决策轨迹（思考、工具调用、工具结果）。
 *
 * <p>为什么单独一张表而不塞进 chat_message：一轮 agent 问答有 1~6 步、每步可能有多个
 * 并行工具调用，是一条流水；塞进消息正文会既难查又破坏「一轮两行」的语义。
 *
 * <p>写入方式：{@code tool_call} 事件先 INSERT 一行（status=RUNNING），
 * 对应的 {@code tool_result} 到达后按 {@code run_id + call_id} UPDATE 成 OK/FAILED。
 * 两条写入排在同一条单线程队列上，所以不会出现「更新先于插入」。
 *
 * <p>★ 同样别加 {@code @Builder} / {@code @AllArgsConstructor}（理由见 {@link User}）。
 */
@Data
@TableName("chat_step")
public class ChatStep {

    @TableId(type = IdType.AUTO)
    private Long id;

    /** 一次 agent run 的 ID，上游 start 事件里的 run_id */
    private String runId;

    /**
     * 对应的助手消息 id（chat_message.id）。
     *
     * <p>轨迹与回答的关联键。只按 session_id + 时间猜，在同一会话连续追问时会把
     * 轨迹挂到错误的回答上。beginExchange 落库失败时为 null（轨迹仍然保留）。
     */
    private Long assistantId;

    private String sessionId;

    /** 模型给这次工具调用分配的 ID，用来把 result 对回 call */
    private String callId;

    /** 决策循环的轮次（1 起） */
    private Integer stepNo;

    /** thought / tool_call */
    private String type;

    /** 工具名；thought 行为 null */
    private String name;

    /** 工具参数（JSON 文本）；thought 行为 null */
    private String argsJson;

    /** 给模型看的工具结果摘要；thought 行为思考文本 */
    private String resultSummary;

    /** 工具执行耗时（毫秒）；优先取上游 elapsed_ms，缺失时用 call/result 的 ts 相减 */
    private Integer elapsedMs;

    /** RUNNING / OK / FAILED / THOUGHT */
    private String status;

    private LocalDateTime createdAt;
}
